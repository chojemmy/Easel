"""Reviewable, versioned skills for the ten video-workflow nodes.

Reading a baseline never creates files in the shared vault. ``apply`` is the
explicit approval boundary: callers must show the proposal diff before calling
it. Execution should snapshot ``get(node)['content']`` and its version when a
node starts, so approving a change only affects subsequent executions.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Iterator
import uuid
import zipfile


NODE_SPECS: dict[str, dict[str, Any]] = {
    "brief": {
        "title": "创作需求", "inputs": ["本次需求、指定受众和内容目标"],
        "outputs": ["可确认的创作简报，包含主题、受众、篇幅、画幅、风格和验收条件"],
        "dependencies": [],
        "rules": "只整理创作需求。已有明确偏好直接沿用；缺失的关键选择集中提出，不替用户编造事实。不在本节点写全文或制作视频。",
    },
    "script": {
        "title": "口播写稿", "inputs": ["已确认简报、用户指定的笔记摘录与引用"],
        "outputs": ["可供本人录制的口播稿、需核实的事实与来源"],
        "dependencies": ["本机笔记读取能力；知识飞轮 Skill 可选"],
        "rules": "围绕一个明确观点写适合口头表达的稿件，遵循用户的语气和长度要求。只读取任务相关的 Obsidian 内容并保留引用；区分用户观点与外部证据。不覆盖原始笔记，不生成假引文，不开始剪辑。",
    },
    "source": {
        "title": "素材准备", "inputs": ["确认稿件、录制文件及用户提供的素材"],
        "outputs": ["素材清单，记录路径引用、内容摘要、哈希、真实时长、画幅、音轨和来源"],
        "dependencies": ["ffprobe", "本机文件读取能力"],
        "rules": "优先复用已有素材，以 ffprobe 读取媒体事实，禁止凭文件名猜内容。保留原片，引用素材而不移动或覆盖。缺失素材列为待补项；外部素材须记录来源与使用条件。本节点不转录或渲染。",
    },
    "transcript": {
        "title": "转录与校对", "inputs": ["已登记的录音或原片、确认稿件、术语表"],
        "outputs": ["真实时间戳的字幕与转录、低置信度片段和校对记录"],
        "dependencies": ["已配置的 ASR 工具", "ffprobe"],
        "rules": "按音频哈希复用转录缓存。字幕时间来自 ASR 或已有 SRT，不按字数估时。用确认稿校对术语、数字和人名，保留实际口误的时间位置；没有听清的词明确标注。本节点不改原音频或生成剪辑代码。",
    },
    "storyboard": {
        "title": "剪辑分镜", "inputs": ["稿件、带时间戳转录、素材清单、已确认风格"],
        "outputs": ["结构化时间线：每镜起止时间、对应台词、视觉目的、素材引用和字幕映射"],
        "dependencies": ["本机媒体抽帧能力"],
        "rules": "以原始旁白时间轴为主时钟规划 A-roll、B-roll、信息卡和切点。B-roll 与具体台词对应，先抽帧检查语义和可用时长；不足时切回人物或卡片，不冻结尾帧补时长。保持用户规定的节奏和留白。本节点只交付可检查分镜，不渲染成片。",
    },
    "build": {
        "title": "Remotion 制作", "inputs": ["已确认时间线、字幕、媒体引用和风格参数"],
        "outputs": ["Remotion 源码与参数、短样片或指定渲染产物、渲染回执"],
        "dependencies": ["Easel video-pipeline-sdk 或兼容的 Remotion 项目", "Node.js 与 Remotion", "Remotion 官方 Skills"],
        "rules": "复用已安装 SDK 和固定模板。当前有 documentary（纪录片）与 editorial（编辑排版）两套布局，由项目 template 设置选择；均保留原片连续时间轴，叠加字幕与章节信息卡，不执行自动粗剪或任意 B-roll 拼接。Skill 中的风格偏好会用于选择六项受支持参数：background/accent/textColor 为六位 HEX 颜色，subtitleSize 为 36–72（1080p 基准），cardPosition 为 left/right，titleCase 为 normal/bold。字体固定采用本机 Noto Sans SC、Microsoft YaHei 和 sans-serif 回退，不能声称任意自然语言字体、转场、布局或动效要求均已实现；超出参数范围时明确列出尚需模板开发的要求。可移植导出包在模板源码可用时附带 templates/workflow-template/，以 manifest.json 为准。开始前读取当前版本的 Remotion 官方规则；动画使用帧驱动 API，保持 remotion 包版本一致。先预检、边界静帧和短样片，再按本次授权渲染；按任务 ID 查询进度，复用未变输入的结果。不得自动安装另一套大型 SDK 或发布视频。",
    },
    "review": {
        "title": "成片检查", "inputs": ["短样片或成片、时间线、字幕、简报与验收条件"],
        "outputs": ["带时间位置的通过项、待修项、用户反馈与验收结论"],
        "dependencies": ["ffprobe", "本机视频播放或抽帧能力"],
        "rules": "检查音画、字幕同步、专有名词、文字可读性、画面安全区、转场和尾帧。只报告观察证据，不能把未观看的全片声称已验收。反馈定位到具体片段和节点；修改偏好先形成 Skill 差异提议，不自动更新共享 Skill 或替用户发布。",
    },
    "deliver": {
        "title": "导出与交付", "inputs": ["已通过审核的制作产物、目标平台规格"],
        "outputs": ["平台适用的视频与封面、元数据、文件哈希和交付清单"],
        "dependencies": ["ffprobe", "已配置的编码与封面工具"],
        "rules": "按目标规格导出并验证视频可解码、真实画幅、帧率、音轨和时长。复用满足规格的现有文件，避免重复编码。保留原片及制作源；仅生成交付包，不上传或发布。",
    },
    "publish": {
        "title": "分发草稿", "inputs": ["验收视频与封面、文案、明确账号与本次草稿或发布授权"],
        "outputs": ["逐账号的草稿或发布回执、目标类型、状态与失败原因"],
        "dependencies": ["yxer CLI 或已配置的平台专用适配器", "本机凭证和有效账号授权"],
        "rules": "默认准备蚁小二内部草稿，准确区分内部草稿、平台草稿和正式发布。读取当前 schema，按 validate 与对应目标 dry-run 校验；按哈希复用上传资源、按回执避免重复提交。正式发布须有明确授权；配额或登录失败停止无效重试，超时先查询任务状态。不要把微信视频号当公众号，不把第三方 API 存在视为所有账号都能发布。不把凭证写入 Skill、笔记或导出包。",
    },
    "archive": {
        "title": "项目归档", "inputs": ["稿件、最终产物引用、实际验收和发布回执"],
        "outputs": ["项目结果索引、各平台真实状态、可选经验提议"],
        "dependencies": ["本机项目文件能力；笔记回写需本次授权"],
        "rules": "归档项目索引与结果引用，保留用户原始材料。只以实际回执记录草稿或发布状态，不将已上传写成已发布。仅按授权回写 Obsidian；把可复用经验转为单节点 Skill 提议供用户看差异后批准，不自动改共享规则。",
    },
}
NODES = tuple(NODE_SPECS)


class WorkflowSkillConflict(ValueError):
    """The reviewed base changed, or another process is applying this node."""


def _text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def _version(content: str) -> str:
    return "sha256:" + hashlib.sha256(_text(content).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_secrets(content: str) -> None:
    """Reject recognizable credentials; this is not arbitrary-secret detection."""
    patterns = (
        r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----",
        r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{15,})\b",
        r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}",
        r"(?im)(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|密码|密钥)\s*[=:：]\s*['\"]?[A-Za-z0-9_./+=-]{4,}",
        r"(?im)^\s*(?:cookie|authorization)\s*:\s*\S+",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
    )
    if any(re.search(pattern, content) for pattern in patterns):
        raise ValueError("Skill 中疑似包含凭证，请移除具体值，仅保留本机凭证别名。")


def _baseline(node: str) -> str:
    spec = NODE_SPECS[node]
    bullets = lambda values: "\n".join(f"- {item}" for item in values)
    return (
        f"---\nname: easel-node-{node}\n"
        f"description: 仅用于 Easel 视频工作流的{spec['title']}节点，按明确输入交付本节点产物。\n---\n\n"
        f"# {spec['title']}\n\n只执行当前节点，不自行启动下游。用户本次明确要求优先于默认偏好。\n\n"
        f"## 输入\n\n{bullets(spec['inputs'])}\n\n"
        f"## 输出\n\n{bullets(spec['outputs'])}\n\n"
        f"## 执行规则\n\n{spec['rules']}\n\n"
        "## 依赖边界\n\n"
        + (bullets(spec["dependencies"]) if spec["dependencies"] else "无需专用运行依赖。")
        + "\n\n依赖须由运行环境提供；本 Skill 不包含项目素材、凭证、模型权重或制作 SDK。\n"
    )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _owned_path(root: Path, *parts: str) -> Path:
    """Stay below a trusted root without resolving paths being created.

    Windows realpath can transiently return inconsistent names while another
    process creates an ancestor. All components here are application-generated;
    lexical containment plus rejection of symlinks/reparse points is sufficient
    and also prevents an existing junction from targeting an unrelated skill.
    """
    path = root.joinpath(*parts)
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError("Skill 路径超出指定目录。") from None
    if ".." in relative.parts:
        raise ValueError("Skill 路径超出指定目录。")
    current = root
    for component in (None, *relative.parts):
        if component is not None:
            current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("Skill 路径不能通过软链接或目录联接跳转到其他位置。")
    return path


class NodeSkillStore:
    """``personal_root`` is the _Agent directory, not its skills subdirectory."""

    def __init__(self, personal_root: Path, project_root: Path):
        self.personal_root = Path(personal_root).expanduser().resolve()
        self.project_root = Path(project_root).expanduser().resolve()
        self.proposal_root = self.project_root / "outputs" / "_workflow_skill_proposals"

    @staticmethod
    def _node(node: str) -> str:
        if not isinstance(node, str) or node not in NODE_SPECS:
            raise ValueError("未知工作流节点。")
        return node

    def _skill_path(self, node: str) -> Path:
        node = self._node(node)
        return _owned_path(self.personal_root, "skills", f"easel-node-{node}", "SKILL.md")

    def _project_path(self, *parts: str) -> Path:
        return _owned_path(self.project_root, "outputs", "_workflow_skill_proposals", *parts)

    def get(self, node: str) -> dict[str, Any]:
        """Read the live personal skill, or a built-in baseline, without writes."""
        path = self._skill_path(node)
        personalized = path.is_file()
        content = _text(path.read_text(encoding="utf-8-sig")) if personalized else _baseline(node)
        self._validate_content(node, content)
        spec = NODE_SPECS[node]
        return {
            "node": node, "name": f"easel-node-{node}", "title": spec["title"],
            "version": _version(content), "content": content,
            "source": str(path) if personalized else "builtin", "personalized": personalized,
            "target_path": str(path), "inputs": list(spec["inputs"]),
            "outputs": list(spec["outputs"]), "dependencies": list(spec["dependencies"]),
        }

    @staticmethod
    def _validate_content(node: str, content: str) -> None:
        if not content.startswith("---\n"):
            raise ValueError("节点 Skill 缺少 YAML frontmatter。")
        frontmatter, separator, _ = content[4:].partition("\n---\n")
        if not separator or not re.search(rf"(?m)^name:\s*easel-node-{node}\s*$", frontmatter):
            raise ValueError("节点 Skill 的 name 与节点不匹配。")
        if not re.search(r"(?m)^description:\s*\S", frontmatter):
            raise ValueError("节点 Skill 缺少 description。")
        _check_secrets(content)

    @staticmethod
    def _context_refs(context: Any) -> dict[str, str]:
        # Deliberately do not persist transcripts, original drafts or raw errors.
        if not isinstance(context, dict):
            return {}
        return {
            key: value for key, value in context.items()
            if key in {"run_id", "node_run_id", "feedback_id"}
            and isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value)
            and not re.match(r"(?:sk-|gh[pousr]_)", value)
        }

    def propose(self, node: str, instruction: str, context: Any = None) -> dict[str, Any]:
        """Persist a reviewable preference addition; never modify personal skills."""
        self._node(node)
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("请提供一条可复用的节点规则或偏好。")
        instruction = instruction.strip()
        if len(instruction) > 8000:
            raise ValueError("偏好过长，请提炼为可复用规则，不要粘贴完整原稿。")
        _check_secrets(instruction)
        before = self.get(node)
        after = before["content"].rstrip() + "\n\n## 个人规则补充\n\n" + instruction + "\n"
        self._validate_content(node, after)
        proposal_id = uuid.uuid4().hex
        proposal = {
            "proposal_id": proposal_id, "node": node, "name": before["name"],
            "created_at": _now(), "status": "pending", "base_version": before["version"],
            "after_version": _version(after), "before": before["content"], "after": after,
            "instruction": instruction, "context": self._context_refs(context),
            "context_omitted": bool(context), "target_path": before["target_path"],
            "diff": "".join(difflib.unified_diff(
                before["content"].splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=f"a/{before['name']}/SKILL.md", tofile=f"b/{before['name']}/SKILL.md",
            )),
        }
        _write_json(self._project_path(f"{proposal_id}.json"), proposal)
        return proposal

    def get_proposal(self, node: str, proposal_id: str) -> dict[str, Any]:
        self._node(node)
        if not isinstance(proposal_id, str) or not re.fullmatch(r"[0-9a-f]{32}", proposal_id):
            raise ValueError("无效的 Skill 提议编号。")
        proposal = json.loads(self._project_path(f"{proposal_id}.json").read_text(encoding="utf-8"))
        if not isinstance(proposal, dict) or proposal.get("node") != node or proposal.get("proposal_id") != proposal_id:
            raise ValueError("提议不属于此节点。")
        for key in ("before", "after", "base_version", "after_version"):
            if not isinstance(proposal.get(key), str):
                raise ValueError("Skill 提议格式无效。")
        if _version(proposal["before"]) != proposal["base_version"] or _version(proposal["after"]) != proposal["after_version"]:
            raise WorkflowSkillConflict("Skill 提议内容已改变，请重新生成并审核差异。")
        self._validate_content(node, proposal["after"])
        return proposal

    @contextmanager
    def _apply_lock(self, node: str) -> Iterator[None]:
        # OS locks release on process exit and coordinate different projects too.
        path = self._skill_path(node).parent / ".apply.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError("节点锁文件不能是软链接。")
        with path.open("a+b") as handle:
            # Byte-range locks may extend past EOF; avoid an unlocked initial
            # write racing another process that has already acquired the lock.
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise WorkflowSkillConflict("此节点正在更新，请稍后重新检查版本。") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _snapshot(self, node: str, content: str, proposal_id: str) -> None:
        version = _version(content)
        path = self._project_path("history", node, f"{version.split(':')[1]}.json")
        if not path.exists():
            _write_json(path, {"node": node, "version": version, "content": content,
                               "recorded_at": _now(), "proposal_id": proposal_id})

    def apply(self, node: str, proposal_id: str) -> dict[str, Any]:
        """Apply a user-approved proposal with a locked compare-and-swap."""
        proposal = self.get_proposal(node, proposal_id)
        with self._apply_lock(node):
            current = self.get(node)
            if current["version"] == proposal["after_version"]:
                # Recover safely if a previous process committed but lost its reply.
                proposal.update(status="applied", applied_at=proposal.get("applied_at") or _now())
                _write_json(self._project_path(f"{proposal_id}.json"), proposal)
                return current
            if proposal.get("status") != "pending" or current["version"] != proposal["base_version"]:
                raise WorkflowSkillConflict("个人 Skill 已更新，本次提议基于旧版本；请重新查看并生成差异。")
            self._snapshot(node, current["content"], proposal_id)
            self._snapshot(node, proposal["after"], proposal_id)
            _atomic_write(self._skill_path(node), proposal["after"])
            proposal.update(status="applied", applied_at=_now())
            _write_json(self._project_path(f"{proposal_id}.json"), proposal)
            return self.get(node)

    def history(self, node: str) -> list[dict[str, Any]]:
        """Return snapshots recorded by this project, newest first."""
        self._node(node)
        folder = self._project_path("history", node)
        values = [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("*.json")]
        return sorted(values, key=lambda item: item["recorded_at"], reverse=True)

    def _template_export(self) -> tuple[dict[str, Any], dict[str, bytes]]:
        """Package distributable template source, never a render workspace.

        Keep this allowlist intentionally shallow: public/, node_modules/,
        generated props and nested runtime files are not portable skill assets.
        """
        root = _owned_path(self.project_root, "assets", "workflow-template")
        src = _owned_path(root, "src")
        candidates = ["package.json", "README.md"]
        if src.is_dir():
            candidates += [f"src/{path.name}" for path in sorted(src.iterdir())
                           if path.suffix in {".ts", ".tsx"}]
        files: dict[str, bytes] = {}
        records = []
        prefix = "templates/workflow-template/"
        for relative in candidates:
            path = _owned_path(root, *relative.split("/"))
            if not path.is_file():
                continue
            content = path.read_bytes()
            _check_secrets(content.decode("utf-8-sig"))
            archived_path = prefix + relative
            files[archived_path] = content
            records.append({"path": archived_path, "sha256": hashlib.sha256(content).hexdigest()})
        missing = [relative for relative in ("package.json", "src/index.ts", "src/Root.tsx")
                   if prefix + relative not in files]
        return {
            "id": "workflow-template", "node": "build", "path": prefix,
            "bundled": bool(files), "complete_source": not missing,
            "missing_required_files": missing, "readme_bundled": prefix + "README.md" in files,
            "variants": ["documentary", "editorial"], "files": records,
            "allowlist": ["package.json", "README.md", "src/*.ts", "src/*.tsx"],
            "reason": "source_available" if files else "template_source_unavailable",
            "requires": ["Node.js", "install dependencies declared in package.json",
                         "project-specific input props and licensed source media",
                         "locally available Chinese fonts", "host rendering and review workflow"],
            "boundary": "Source only; no Easel SDK, dependencies, media, credentials, or workflow execution service.",
        }, files

    def export(self, nodes: list[str] | None = None) -> bytes:
        """Export current skills and allowlisted build-template source if present."""
        if nodes is None:
            nodes = list(NODES)
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("请至少选择一个节点。")
        selected = list(dict.fromkeys(self._node(node) for node in nodes))
        skills = [self.get(node) for node in selected]
        templates, template_files = [], {}
        if "build" in selected:
            template, template_files = self._template_export()
            templates.append(template)
        manifest = {
            "format": "easel-node-skills", "format_version": 1, "created_at": _now(),
            "contains": ["approved current node instructions", "dependency declarations"],
            "excludes": ["credentials", "project media", "drafts and transcripts", "proposal context and history", "SDKs and model weights"],
            "skills": [{key: skill[key] for key in ("node", "name", "version", "personalized", "dependencies")} for skill in skills],
            "templates": templates,
        }
        if template_files:
            manifest["contains"].append("allowlisted reusable Remotion template source")
        guide = (
            "# Easel 节点 Skill 导出包\n\n"
            "将所需 skills/easel-node-*/ 文件夹接入目标 Agent 支持的 Skill 目录，"
            "或将对应 SKILL.md 内容作为单节点执行说明。按目标宿主文档完成接入；本包不会自动安装。\n\n"
            "每个节点只处理自己列出的输入并产出指定结果。由宿主提供模型、文件权限、"
            "任务状态、输入引用和用户确认界面；移植后先运行小样片验证。\n\n"
            "manifest.json 记录正文版本、依赖及模板源码清单和 SHA-256。包含 build 节点且源码可用时，"
            "templates/workflow-template/ 附带 documentary/editorial 模板的 package.json、README.md（如有）"
            "及 src 直接子层的 TypeScript 源文件；bundled/complete_source 字段说明是否附带且完整。"
            "没有模板源码时仍可使用节点规范，但须另行准备兼容模板。\n\n"
            "制作节点需要另外配置匹配版本的 Node.js/Remotion 依赖、中文字体和官方规则，"
            "由目标 Agent 提供实际媒体、字幕时间戳、场景与视觉参数，并建立预览、审核及渲染流程。"
            "模板不自带 Easel 工作流执行服务，包中不包含 public/ 或 node_modules/。"
            "分发节点需要可用账号及本机凭证。"
            "不包含 Easel video-pipeline-sdk、大型模型、媒体素材、原稿、提议上下文或凭证。\n\n"
            "包内规则均为当前已生效版本；未批准提议不会导出。个人规则中主动填写的文字也会随包导出，"
            "分享前请检查其内容。应用偏好不能代替上传、发布或修改共享文件所需的用户授权。\n"
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for skill in skills:
                archive.writestr(f"skills/{skill['name']}/SKILL.md", skill["content"])
            for path, content in template_files.items():
                archive.writestr(path, content)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            archive.writestr("INSTALL.md", guide)
        return buffer.getvalue()
