"""Durable, project-scoped content workflows. Runtime data never belongs in Git."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


NODE_DEFINITIONS = [
    {"id": "brief", "title": "需求与材料", "phase": "策划", "description": "确定主题、受众、目标与参考材料。"},
    {"id": "script", "title": "口播稿", "phase": "策划", "description": "多份来源与稿件，选择本次制作的主稿。"},
    {"id": "source", "title": "录制与导入", "phase": "策划", "description": "连接本地原片，检查时长、画幅与声音。"},
    {"id": "transcript", "title": "转录与粗剪", "phase": "制作", "description": "导入或生成时间戳字幕，保留原片时间轴。"},
    {"id": "storyboard", "title": "分镜与素材", "phase": "制作", "description": "先确认分镜，再执行固定画面模板。"},
    {"id": "build", "title": "画面制作", "phase": "制作", "description": "固定 Remotion 模板、可复现的视觉参数。"},
    {"id": "review", "title": "预览与质检", "phase": "交付", "description": "抽帧、短样片、技术检查与人工确认。"},
    {"id": "deliver", "title": "成片与封面", "phase": "交付", "description": "确认预览后渲染全片，保存封面和检查报告。"},
    {"id": "publish", "title": "草稿与发布", "phase": "交付", "description": "准备发布包；平台草稿和公开发布各有独立回执。"},
    {"id": "archive", "title": "Obsidian 归档", "phase": "归档", "description": "预览路径和正文，未发布也可存档。"},
]
NODE_IDS = [n["id"] for n in NODE_DEFINITIONS]
VIDEO_NODES = {"source", "transcript", "storyboard", "build", "review", "deliver"}


class WorkflowConflict(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_error(error: object, limit: int | None = 1400) -> str:
    message = str(error)
    for name, value in os.environ.items():
        if len(value) >= 8 and any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            message = message.replace(value, "[已隐藏凭证]")
    message = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [已隐藏凭证]", message)
    message = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[已隐藏凭证]", message)
    message = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)", r"\1[已隐藏凭证]", message)
    return message[:limit] if limit is not None else message


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex[:8] + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def node_of(project: dict, node: str) -> dict:
    if node not in NODE_IDS:
        raise ValueError("未知流程节点")
    return next(n for n in project["nodes"] if n["id"] == node)


def primary(project: dict) -> dict | None:
    return next((m for m in project.get("manuscripts", []) if m["id"] == project.get("primary_manuscript_id")), None)


def default_vault() -> Path:
    agent_root = os.environ.get("AGENT_VAULT")
    if agent_root:
        return Path(agent_root).expanduser().resolve().parent
    return Path("D:/第二大脑") if os.name == "nt" else Path.home() / "Documents" / "第二大脑"


class ContentWorkflowService:
    def __init__(self, project_root: Path, vault: Path | None = None, executor=None):
        self.root = project_root.resolve()
        self.data = self.root / "outputs" / "视频工作流"
        self.vault = (vault or default_vault()).resolve()
        self._lock = threading.RLock()
        self.tasks: dict[str, asyncio.Task] = {}
        self.executor = executor
        from .workflow_skills import NodeSkillStore
        self.skills = NodeSkillStore(self.vault / "_Agent", self.root)
        # A process restart cannot be represented as a still-running task.
        if self.data.exists():
            for file in self.data.glob("*/project.json"):
                try:
                    project = json.loads(file.read_text(encoding="utf-8"))
                    changed = False
                    for node in project.get("nodes", []):
                        if node.get("chat", {}).get("status") == "running":
                            node["chat"]["status"] = "stopped"
                            for message in node["chat"].get("messages", []):
                                if message.get("status") == "streaming":
                                    message["status"] = "stopped"
                            changed = True
                        if node["status"] == "running":
                            node.update(status="blocked", message="服务已重启，本次运行中断；检查产物后重试。")
                            if node["id"] == "publish" and node.get("runs") and node["runs"][-1].get("action") in ("draft", "publish"):
                                node.update(publication_uncertain=True, message="发布过程被服务重启中断，结果不明；请先到平台核实，不能自动重发。")
                            if node.get("runs"):
                                node["runs"][-1].update(status="interrupted", message=node["message"], finished_at=now())
                                self._sync_chat_execution(node, node["runs"][-1])
                            changed = True
                    if changed:
                        write_json(file, project)
                except (ValueError, OSError, KeyError):
                    continue

    def directory(self, project_id: str) -> Path:
        if not re.fullmatch(r"wf-[a-f0-9]{12}", project_id):
            raise ValueError("无效的工作流编号")
        return self.data / project_id

    def get(self, project_id: str) -> dict:
        with self._lock:
            path = self.directory(project_id) / "project.json"
            if not path.is_file():
                raise FileNotFoundError("工作流不存在")
            return json.loads(path.read_text(encoding="utf-8"))

    def save(self, project: dict) -> dict:
        from .workflow_agent_context import session_key
        project["agent_session_key"] = session_key(project["id"])
        project["updated_at"] = now()
        write_json(self.directory(project["id"]) / "project.json", project)
        return copy.deepcopy(project)

    def list(self) -> dict:
        projects = []
        if self.data.exists():
            for path in self.data.glob("*/project.json"):
                try:
                    projects.append(self.get(path.parent.name))
                except (ValueError, OSError):
                    continue
        projects.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
        return {"projects": projects, "nodes": NODE_DEFINITIONS,
                "defaults": {"vault": str(self.vault), "output_dir": "3-输出/草稿"}}

    def create(self, body: dict) -> dict:
        title = str(body.get("title", "")).strip()[:200]
        if not title:
            raise ValueError("请输入内容标题")
        kind = body.get("kind", "video")
        if kind not in ("video", "article"):
            raise ValueError("内容类型须为 video 或 article")
        project = {"id": "wf-" + uuid.uuid4().hex[:12], "title": title, "kind": kind,
                   "created_at": now(), "updated_at": now(), "content_version": 1,
                   "brief": {"topic": title, "audience": "", "platform": "视频号", "duration": "", "style": ""},
                   "manuscripts": [], "primary_manuscript_id": None, "media": {},
                   "settings": {"output_ratio": "16:9", "visual_style": "克制科技纪录片", "subtitle_style": "清晰双行",
                                "template": "documentary", "publish_platform": "weixin-channels", "archive_folder": "3-输出/草稿"},
                   "nodes": [{**n, "status": "skipped" if kind == "article" and n["id"] in VIDEO_NODES else "idle",
                              "message": "文章无需视频制作" if kind == "article" and n["id"] in VIDEO_NODES else "尚未开始",
                              "version": 0, "artifacts": [], "runs": [], "feedback": []} for n in NODE_DEFINITIONS]}
        if isinstance(body.get("brief"), dict):
            project["brief"].update(body["brief"])
        with self._lock:
            if any(k in body for k in ("manuscripts", "primary_manuscript_id", "media", "settings")):
                return self.patch(project["id"], body, initial=project)
            return self.save(project)

    def _idle(self, project: dict) -> None:
        if any(n["status"] == "running" or n.get("chat", {}).get("status") == "running" for n in project["nodes"]):
            raise WorkflowConflict("该工作流有节点正在执行，请先停止或等待完成。")

    def invalidate(self, project: dict, first: str, reason: str) -> None:
        for n in project["nodes"][NODE_IDS.index(first):]:
            if n["status"] in ("skipped", "idle"):
                continue
            n.update(status="stale", message=reason)
            n.pop("approved_version", None)
        if project.get("archive"):
            project["archive"]["outdated"] = True

    def patch(self, project_id: str, body: dict, *, initial: dict | None = None) -> dict:
        with self._lock:
            p = copy.deepcopy(initial) if initial is not None else self.get(project_id)
            self._idle(p)
            if body.get("content_version") not in (None, p["content_version"]):
                raise WorkflowConflict("内容已在其他窗口更新，请刷新后再保存。")
            earliest = len(NODE_IDS)
            for key in ("title", "brief", "media", "settings", "manuscripts", "primary_manuscript_id"):
                if key not in body:
                    continue
                value = copy.deepcopy(body[key])
                if key in ("brief", "media", "settings"):
                    if not isinstance(value, dict):
                        raise ValueError(f"{key} 须为对象")
                    value = {**p[key], **value}
                if key == "title":
                    value = str(value).strip()[:200]
                    if not value:
                        raise ValueError("标题不能为空")
                if key == "manuscripts":
                    if not isinstance(value, list) or len(value) > 100:
                        raise ValueError("稿件须为列表，最多 100 份")
                    previous = {m["id"]: m for m in p["manuscripts"]}
                    normalized = []
                    for m in value:
                        if not isinstance(m, dict) or len(str(m.get("content", ""))) > 500000:
                            raise ValueError("稿件格式不正确或超过 50 万字")
                        mid = str(m.get("id") or "ms-" + uuid.uuid4().hex[:10])
                        if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", mid):
                            raise ValueError("稿件编号只能包含字母、数字、短横线或下划线")
                        if not isinstance(m.get("content", ""), str):
                            raise ValueError("稿件正文须为文本")
                        old = previous.get(mid, {})
                        entry = {k: m.get(k, "") for k in ("title", "content", "source_kind", "source_path", "source_url")}
                        entry.update(id=mid, title=str(m.get("title") or "未命名稿件"),
                                     source_kind=m.get("source_kind") or "manual",
                                     version=old.get("version", 0) + int(any(entry.get(k) != old.get(k) for k in entry)))
                        if entry["source_kind"] not in ("manual", "file", "obsidian", "generated"):
                            raise ValueError("未知稿件来源")
                        normalized.append(entry)
                    if len({m["id"] for m in normalized}) != len(normalized):
                        raise ValueError("稿件编号不能重复")
                    value = normalized
                if value == p.get(key):
                    continue
                start = {"title": "brief", "brief": "brief", "manuscripts": "script", "primary_manuscript_id": "script", "media": "source", "settings": "storyboard"}[key]
                if key == "media" and value.get("source_path") == p["media"].get("source_path"):
                    start = "transcript"
                elif key == "media":
                    # A subtitle belongs to one recording. Do not silently carry it to another.
                    if "transcript_path" not in body["media"] or body["media"].get("transcript_path") == p["media"].get("transcript_path"):
                        value.pop("transcript_path", None)
                    for generated in ("generated_transcript_path", "transcript_source_hash", "transcript_source_sha256"):
                        value.pop(generated, None)
                if key == "settings":
                    changed = {k for k in value if value[k] != p[key].get(k)}
                    if "generation_budget" in changed and value["generation_budget"] not in {"standard", "large", "maximum"}:
                        raise ValueError("请选择标准、高额度或最大生成预算")
                    if changed <= {"generation_budget"}:
                        p[key] = value
                        continue
                    changed.discard("generation_budget")
                    if changed & {"visual_style", "subtitle_style"} and "visual_parameters" not in changed:
                        # A form sends existing settings back; stale explicit props
                        # must not silently override a newly entered style request.
                        value.pop("visual_parameters", None)
                    if changed <= {"archive_folder", "media_root"}:
                        start = "archive"
                    elif all(k.startswith("publish_") for k in changed):
                        start = "publish"
                    elif changed <= {"final_path", "cover_path"}:
                        start = "deliver"
                    elif changed <= {"output_ratio", "template", "visual_style", "subtitle_style", "visual_parameters"}:
                        start = "build"
                earliest = min(earliest, NODE_IDS.index(start))
                p[key] = value
            mids = [m["id"] for m in p["manuscripts"]]
            if p["primary_manuscript_id"] not in mids:
                if "primary_manuscript_id" in body and body["primary_manuscript_id"]:
                    raise ValueError("主稿必须属于当前工作流")
                p["primary_manuscript_id"] = mids[0] if mids else None
            if earliest < len(NODE_IDS):
                p["content_version"] += 1
                self.invalidate(p, NODE_IDS[earliest], "上游内容已更新，请重新运行或审阅。")
            return self.save(p)

    def obsidian_search(self, query: str) -> list[dict]:
        query = query.strip().lower()
        if len(query) < 2:
            return []
        results = []
        if not self.vault.is_dir():
            raise FileNotFoundError("未找到 Obsidian 库，请设置 AGENT_VAULT")
        for path in self.vault.rglob("*.md"):
            rel = path.relative_to(self.vault)
            if any(part.startswith(".") or part in ("_Agent", "node_modules", ".trash") for part in rel.parts):
                continue
            if query in path.stem.lower():
                resolved = path.resolve()
                if not resolved.is_relative_to(self.vault) or any(part.startswith(".") or part in ("_Agent", "node_modules") for part in resolved.relative_to(self.vault).parts):
                    continue
                if not resolved.is_file() or resolved.stat().st_size > 2_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")[:600]
                results.append({"title": path.stem, "path": str(rel).replace("\\", "/"), "excerpt": text})
                if len(results) >= 30:
                    break
        return results

    def import_note(self, project_id: str, body: dict) -> dict:
        path = (self.vault / str(body.get("source_path", ""))).resolve()
        if not path.is_relative_to(self.vault) or path.suffix.lower() != ".md" or "_Agent" in path.relative_to(self.vault).parts:
            raise ValueError("只能导入当前 Obsidian 库内的 Markdown 内容笔记")
        if path.stat().st_size > 2_000_000:
            raise ValueError("笔记过大，请先拆分")
        p = self.get(project_id)
        m = {"title": body.get("title") or path.stem, "content": path.read_text(encoding="utf-8-sig"),
             "source_kind": "obsidian", "source_path": str(path.relative_to(self.vault)).replace("\\", "/")}
        return self.patch(project_id, {"manuscripts": p["manuscripts"] + [m], "content_version": p["content_version"]})

    def prerequisites(self, p: dict, node: str) -> None:
        dependency = {"transcript": "source", "storyboard": "transcript", "build": "storyboard", "review": "build", "deliver": "review", "publish": "script" if p["kind"] == "article" else "deliver"}.get(node)
        if dependency and node_of(p, dependency)["status"] != "completed":
            raise WorkflowConflict(f"请先完成并确认“{node_of(p, dependency)['title']}”。")

    async def run(self, project_id: str, node: str, options: dict, *, chat_turn_id: str | None = None) -> dict:
        with self._lock:
            options = copy.deepcopy(options)
            p = self.get(project_id)
            self._idle(p)
            n = node_of(p, node)
            if chat_turn_id is not None and (n.get("chat", {}).get("turn_id") != chat_turn_id or
                    not any(m.get("id") == chat_turn_id and m.get("role") == "assistant" for m in n["chat"].get("messages", []))):
                raise WorkflowConflict("对话已更新，未启动旧回复提出的任务。")
            if n["status"] == "skipped":
                raise ValueError("文章工作流跳过视频节点")
            if node == "archive":
                raise ValueError("请先预览归档，再确认存档")
            self.prerequisites(p, node)
            action = options.get("action", "run")
            if node == "publish" and n.get("publication_uncertain") and action != "verify":
                raise WorkflowConflict("上次提交结果不明。请先到平台核对，不能自动重发。")
            if node == "publish" and action == "publish" and options.get("confirm") is not True:
                raise ValueError("公开发布需要单独确认")
            if node == "deliver":
                # A new render/import is a new deliverable even if input text is unchanged.
                p["content_version"] += 1
            self.invalidate(p, node, "当前节点重新运行，下游需要重新检查。")
            skill = self.skills.get(node)
            n.update(status="running", phase="preparing", message="准备执行…", version=n["version"] + 1, skill_version=skill["version"])
            run = {"id": "run-" + uuid.uuid4().hex[:12], "started_at": now(), "status": "running",
                   "action": action, "content_version": p["content_version"], "skill_version": skill["version"]}
            n["runs"].append(run)
            if chat_turn_id is not None:
                run["chat_turn_id"] = chat_turn_id
                self._sync_chat_execution(n, run)
            self.activity(p, node, "任务已启动，执行进展和最终结果会同步到对话。", run["id"])
            options["run_id"] = run["id"]
            saved_feedback = [f["text"] for f in n["feedback"] if not f.get("applied_run_id")]
            if saved_feedback:
                options["feedback"] = "\n".join(([str(options["feedback"])] if options.get("feedback") else []) + saved_feedback)
            result = self.save(p)
            task = asyncio.create_task(self._execute(copy.deepcopy(p), node, options, skill, run["id"]))
            self.tasks[project_id] = task
            return result

    @staticmethod
    def _sync_chat_execution(node: dict, run: dict) -> None:
        """Keep the originating reply tied to real execution, including failure."""
        tid = run.get("chat_turn_id")
        if not tid:
            return
        message = next((m for m in node.get("chat", {}).get("messages", [])
                        if m.get("id") == tid and m.get("role") == "assistant"), None)
        if message is not None:
            message["execution"] = {"run_id": run["id"], "status": run["status"],
                "phase": node.get("phase", ""), "message": safe_error(run.get("message") or node.get("message", "")),
                "started_at": run["started_at"], "finished_at": run.get("finished_at"), "updated_at": now()}

    def activity(self, project: dict, node: str, event: str | dict, run_id: str = "") -> None:
        item = {"kind": "status", "text": event} if isinstance(event, str) else event
        kind = item.get("kind", "status")
        if kind not in {"status", "generation", "tool", "result", "error"}:
            kind = "status"
        # Large generated text is a snapshot, not a new entry for every token.
        raw = str(item.get("text", ""))[:160000 if kind == "generation" else 1400]
        text = safe_error(raw, None) if kind == "generation" else safe_error(raw)
        events = node_of(project, node).setdefault("activity", [])
        if events and events[-1]["kind"] == kind and events[-1].get("run_id") == run_id and (kind == "generation" or events[-1]["text"] == text):
            events[-1].update(text=text, at=now())
        else:
            events.append({"id": uuid.uuid4().hex[:12], "kind": kind, "text": text, "at": now(), "run_id": run_id})
        del events[:-100]

    async def chat(self, project_id: str, node: str, body: dict) -> dict:
        from .workflow_chat import start_chat
        return await start_chat(self, project_id, node, body)

    def progress_chat(self, project_id: str, node: str, turn_id: str, message: str) -> None:
        with self._lock:
            p = self.get(project_id)
            if node_of(p, node).get("chat", {}).get("turn_id") != turn_id:
                return
            self.activity(p, node, message, turn_id)
            self.save(p)

    def progress(self, project_id: str, node: str, run_id: str, message: str | dict) -> None:
        with self._lock:
            p = self.get(project_id)
            n = node_of(p, node)
            if n["status"] == "running" and n["runs"][-1]["id"] == run_id:
                self.activity(p, node, message, run_id)
                if isinstance(message, str):
                    n["message"] = safe_error(message)
                elif message.get("kind") != "generation":
                    n["message"] = safe_error(message.get("text", ""))
                if isinstance(message, dict) and message.get("phase") in {"preparing", "transcribing", "correcting", "segmenting", "validating"}:
                    n["phase"] = message["phase"]
                n["runs"][-1]["message"] = n["message"]
                self._sync_chat_execution(n, n["runs"][-1])
                self.save(p)

    async def _execute(self, snapshot: dict, node: str, options: dict, skill: dict, run_id: str):
        project_id = snapshot["id"]
        try:
            if self.executor is None:
                from .workflow_runner import WorkflowRunner
                self.executor = WorkflowRunner(self.root, skill_agent=getattr(self, "skill_agent", None))
            result = await self.executor.execute(snapshot, node, options, skill, self.directory(project_id),
                lambda text: self.progress(project_id, node, run_id, text))
            with self._lock:
                p = self.get(project_id)
                n = node_of(p, node)
                if n["runs"][-1]["id"] != run_id:
                    return
                inputs_changed = any(key in result and p.get(key) != result[key] for key in ("manuscripts", "primary_manuscript_id", "media", "settings"))
                for key in ("manuscripts", "primary_manuscript_id", "media", "settings"):
                    if key in result:
                        p[key] = result[key]
                if inputs_changed:
                    p["content_version"] += 1
                n.update(status=result.get("status", "awaiting_review"), message=result.get("message", "已生成，等待确认"),
                         artifacts=result.get("artifacts", []))
                if isinstance(result.get("library_skills_used"), list):
                    n["library_skills_used"] = result["library_skills_used"]
                    n["runs"][-1]["library_skills_used"] = result["library_skills_used"]
                if result.get("publication_uncertain"):
                    n["publication_uncertain"] = True
                n["runs"][-1].update(status=n["status"], message=safe_error(n["message"]), finished_at=now())
                n["phase"] = "finished"
                self._sync_chat_execution(n, n["runs"][-1])
                self.activity(p, node, {"kind": "error" if n["status"] in {"blocked", "failed"} else "result", "text": n["message"]}, run_id)
                if n["status"] in ("completed", "awaiting_review"):
                    for feedback in n["feedback"]:
                        if not feedback.get("applied_run_id"):
                            feedback["applied_run_id"] = run_id
                self.save(p)
        except asyncio.CancelledError:
            self._finish_error(project_id, node, run_id, "blocked", "已停止；已有产物保留，可检查后重试。")
            raise
        except Exception as exc:
            # Do not serialize request headers, environment or full process command lines.
            self._finish_error(project_id, node, run_id, "failed", safe_error(exc))
        finally:
            if self.tasks.get(project_id) is asyncio.current_task():
                self.tasks.pop(project_id, None)

    def _finish_error(self, project_id: str, node: str, run_id: str, status: str, message: str):
        with self._lock:
            p = self.get(project_id)
            n = node_of(p, node)
            if n["runs"][-1]["id"] == run_id:
                n.update(status=status, message=message)
                if node == "publish" and n["runs"][-1].get("action") in ("draft", "publish"):
                    n["publication_uncertain"] = True
                    n["message"] += " 提交结果需要到平台核实，已阻止自动重发。"
                n["runs"][-1].update(status=status, message=safe_error(n["message"]), finished_at=now())
                n["phase"] = "finished"
                self._sync_chat_execution(n, n["runs"][-1])
                self.activity(p, node, {"kind": "error", "text": n["message"]}, run_id)
                self.save(p)

    def reconcile_not_submitted(self, project_id: str, note: str) -> dict:
        note = str(note).strip()
        if len(note) < 5 or len(note) > 2000:
            raise ValueError("请填写在平台核实的结果（5–2000 字）")
        with self._lock:
            p = self.get(project_id)
            self._idle(p)
            n = node_of(p, "publish")
            if not n.get("publication_uncertain"):
                raise WorkflowConflict("当前没有需要核实的提交")
            evidence = {"outcome": "not_submitted", "reported_by": "user", "note": safe_error(note), "at": now(),
                        "run_id": n["runs"][-1]["id"] if n["runs"] else None}
            n.setdefault("reconciliations", []).append(evidence)
            artifacts = (self.directory(project_id) / "artifacts").resolve()
            if not artifacts.is_relative_to(self.data.resolve()):
                raise ValueError("回执目录超出工作流范围")
            receipt = artifacts / "publication-receipt.json"
            if receipt.exists():
                destination = artifacts / "publication-history" / (uuid.uuid4().hex + ".json")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not receipt.resolve().is_relative_to(artifacts) or not destination.resolve().is_relative_to(artifacts):
                    raise ValueError("回执历史路径不安全")
                os.replace(receipt, destination)
            n.update(publication_uncertain=False, status="blocked", message="已由你核实平台未提交，可以重新运行。")
            # This is a human retry decision, never a machine publication receipt.
            write_json(self.directory(project_id) / "artifacts" / "publication-reconciliation.json", evidence)
            return self.save(p)

    async def stop(self, project_id: str, node: str) -> dict:
        p = self.get(project_id)
        n = node_of(p, node)
        task = self.tasks.get(project_id)
        if task and (n["status"] == "running" or n.get("chat", {}).get("status") == "running"):
            if n.get("chat", {}).get("status") == "running":
                from .workflow_chat import finish_chat
                finish_chat(self, project_id, node, "stopped", "正在停止；未完成的回复不会应用到内容。")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # Cancellation can win before the coroutine has entered its try/finally.
            current = node_of(self.get(project_id), node)
            if current["status"] == "running":
                self._finish_error(project_id, node, current["runs"][-1]["id"], "blocked", "已停止，尚未开始执行。")
            if current.get("chat", {}).get("status") == "running":
                from .workflow_chat import finish_chat
                finish_chat(self, project_id, node, "stopped", "已停止，尚未完成的回复没有应用到内容。")
            if self.tasks.get(project_id) is task:
                self.tasks.pop(project_id, None)
        return self.get(project_id)

    async def shutdown(self):
        for project_id in list(self.tasks):
            p = self.get(project_id)
            active = next((n for n in p["nodes"] if n["status"] == "running" or n.get("chat", {}).get("status") == "running"), None)
            if active:
                await self.stop(project_id, active["id"])

    def approve(self, project_id: str, node: str, version: int | None, *, reviewer: str = "user") -> dict:
        with self._lock:
            p = self.get(project_id)
            self._idle(p)
            n = node_of(p, node)
            if type(version) is not int or version != n["version"]:
                raise WorkflowConflict("产物版本已变化，请刷新后再确认")
            if n["status"] != "awaiting_review":
                raise WorkflowConflict("当前节点没有可确认的新产物")
            n.update(status="completed", approved_version=n["version"], approved_at=now(), approved_by=reviewer,
                     message="已确认，可继续下一步" if reviewer == "user" else "验证运行：已检查，可继续下一步；最终效果待你验收。")
            return self.save(p)

    def feedback(self, project_id: str, node: str, text: str, target_node: str | None = None) -> dict:
        text = text.strip()
        if not text or len(text) > 10000:
            raise ValueError("修改意见须为 1–10000 字")
        with self._lock:
            p = self.get(project_id)
            self._idle(p)
            node_of(p, node)
            target = target_node or node
            n = node_of(p, target)
            if NODE_IDS.index(target) > NODE_IDS.index(node) or n["status"] == "skipped":
                raise ValueError("修改意见只能交给当前或适用的上游节点")
            record = {"text": text, "at": now(), "created_at": now(), "version": n["version"], "source_node": node, "target_node": target}
            n["feedback"].append(record)
            if target != node:
                node_of(p, node)["feedback"].append({**record, "applied_run_id": "routed-to-" + target})
            self.invalidate(p, target, f"修改意见已交给“{n['title']}”，请从该节点重新执行。")
            return self.save(p)

    def skill_applied(self, project_id: str, node: str) -> None:
        with self._lock:
            p = self.get(project_id)
            self._idle(p)
            self.invalidate(p, node, "节点技能已更新，请按新标准重新运行。")
            self.save(p)

    def archive_preview(self, project_id: str) -> dict:
        from .workflow_archive import preview_archive
        return preview_archive(self.get(project_id), self.vault, self.root)

    def archive_apply(self, project_id: str, expected_hash: str | None) -> dict:
        from .workflow_archive import apply_archive
        with self._lock:
            p = self.get(project_id)
            self._idle(p)
            result = apply_archive(p, self.vault, self.root, expected_hash=expected_hash)
            n = node_of(p, "archive")
            n.update(status="completed", message="已按现有格式归档到 Obsidian", version=n["version"] + 1)
            p["archive"] = {**result, "at": now(), "outdated": False}
            result["project"] = self.save(p)
            return result
