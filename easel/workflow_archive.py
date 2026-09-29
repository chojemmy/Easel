"""Preview and explicitly archive workflow deliverables into an Obsidian vault.

The vault contains only a readable deliverable. Ownership receipts stay under
the Easel project's artifacts directory. Original manuscripts and media are
never moved, overwritten or copied. Publication claims in request JSON are not
evidence: only the workflow publisher's on-disk, version-matched receipt counts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any


class ArchiveError(ValueError):
    """Invalid or unsafe archive input."""


class ArchiveConflict(ArchiveError):
    """A preview became stale or a user-owned file would be overwritten."""


def _digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _safe_path(value: str | Path, roots: list[Path], base: Path) -> Path:
    raw = str(value)
    if not raw or "\x00" in raw or ".." in re.split(r"[\\/]", raw):
        raise ArchiveError("路径为空或包含越界片段")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve()
    if not any(_under(resolved, root) for root in roots):
        raise ArchiveError("路径不在允许的目录内")
    return resolved


def _text(value: Any, *, single_line: bool = False) -> str:
    text = str(value or "").strip()
    # Do not leak credentials accidentally included in a manuscript or caption.
    secret = re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+[A-Za-z0-9_.+/=-]{12,}|"
        r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*"
        r"[\"']?[^\s\"']{8,}|\bsk-[A-Za-z0-9_-]{20,}", re.I,
    )
    if secret.search(text):
        raise ArchiveError("归档内容疑似包含凭证，请先移除")
    if single_line:
        text = re.sub(r"[\r\n\t]+", " ", text)
    return text


def _filename_title(title: str) -> str:
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f\[\]#]', "_", title).strip(" .")[:100]
    if not title:
        raise ArchiveError("归档标题不能为空")
    return title


def _primary(project: dict, vault: Path, root: Path) -> tuple[dict, str]:
    manuscripts = project.get("manuscripts") or []
    if not isinstance(manuscripts, list) or not manuscripts:
        raise ArchiveError("归档需要一份主稿")
    selected_id = project.get("primary_manuscript_id")
    flagged = [m for m in manuscripts if m.get("is_primary") is True]
    if selected_id:
        selected = [m for m in manuscripts if m.get("id") == selected_id]
        if len(selected) != 1 or any(m.get("id") != selected_id for m in flagged):
            raise ArchiveError("主稿选择不一致")
        manuscript = selected[0]
    elif len(flagged) == 1:
        manuscript = flagged[0]
    elif len(manuscripts) == 1 and not flagged:
        manuscript = manuscripts[0]
    else:
        raise ArchiveError("多稿归档必须明确选择唯一主稿")
    content = manuscript.get("content")
    if content is None and manuscript.get("source_path"):
        source = _safe_path(manuscript["source_path"], [vault, root], root)
        if source.suffix.lower() not in {".md", ".txt"} or not source.is_file():
            raise ArchiveError("主稿来源必须是存在的 Markdown 或文本文件")
        content = source.read_text(encoding="utf-8-sig")
    if not isinstance(content, str) or not content.strip():
        raise ArchiveError("主稿内容不能为空")
    # Source frontmatter belongs to its original note, not the new deliverable.
    content = re.sub(r"\A\ufeff?---\s*\n.*?\n---\s*(?:\n|$)", "", content, count=1, flags=re.S)
    return manuscript, _text(content)


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveError("本地归档或发布回执无法读取") from exc
    if not isinstance(value, dict):
        raise ArchiveError("本地回执格式无效")
    return value


def _number(vault: Path, requested: Any, owned: dict) -> int:
    seen: set[int] = set()
    output = vault / "3-输出"
    if output.is_dir():
        for note in output.rglob("*.md"):
            if not _under(note.resolve(), vault):
                continue
            # Read only small metadata headers, never the whole vault.
            with note.open("r", encoding="utf-8-sig", errors="replace") as handle:
                header = handle.read(4096)
            match = re.match(r"\A---\s*\n(.*?)\n---", header, flags=re.S)
            number = re.search(r"(?m)^project_id:\s*[\"']?(\d+)[\"']?\s*$", match[1]) if match else None
            if number:
                seen.add(int(number[1]))
    if owned:
        return int(owned["project_id"])
    if requested is not None:
        if not re.fullmatch(r"[0-9]{1,8}", str(requested)) or int(requested) <= 0:
            raise ArchiveError("归档编号必须是正整数")
        if int(requested) in seen:
            raise ArchiveConflict("归档编号已存在；现有成果不会被覆盖")
        return int(requested)
    return max(seen, default=0) + 1


def _publication(project: dict, receipt_path: Path, manuscript: dict) -> tuple[str, list[str], list[str], str | None]:
    """Only publisher-owned disk evidence can upgrade a public status."""
    version = project.get("content_version", manuscript.get("version"))
    warnings: list[str] = []
    if not receipt_path.is_file():
        return "待发布" if project.get("kind") == "video" else "待审核", [], warnings, None
    receipt = _read_object(receipt_path)
    digest = _digest(receipt_path.read_bytes())
    matches = (
        receipt.get("workflow_id") == project["id"]
        and version is not None
        and receipt.get("content_version") == version
        and receipt.get("verified") is True
        and bool(receipt.get("verified_at"))
    )
    if not matches:
        warnings.append("发布回执未验证或与当前稿件版本不一致，保留未发布状态。")
        return "待发布" if project.get("kind") == "video" else "待审核", [], warnings, digest
    names = {"weixin-channels": "视频号", "douyin": "抖音", "kuaishou": "快手", "bilibili": "B站", "xiaohongshu": "小红书"}
    platform = _text(names.get(receipt.get("platform"), receipt.get("platform", "")), single_line=True)
    platforms = [platform] if platform else []
    if receipt.get("action") == "publish" and receipt.get("outcome") == "published" and receipt.get("evidence"):
        return "已发布", platforms, warnings, digest
    if receipt.get("action") == "platform_draft" and receipt.get("outcome") == "draft_saved":
        return "已存草稿", platforms, warnings, digest
    warnings.append("已提交或未知结果不等于发布成功，待核实平台结果。")
    return "待发布", platforms, warnings, digest


def _media(project: dict, vault: Path, root: Path) -> list[dict]:
    settings = project.get("settings") or {}
    roots = [vault, root]
    if settings.get("media_root"):
        media_root = Path(settings["media_root"])
        if not media_root.is_absolute() or not media_root.is_dir():
            raise ArchiveError("媒体根目录必须是存在的绝对路径")
        roots.append(media_root.resolve())
    found: list[dict] = []
    seen: set[str] = set()
    for node in project.get("nodes") or []:
        for artifact in node.get("artifacts") or []:
            kind = artifact.get("kind", "")
            # Logs, receipts, raw recordings and arbitrary data never enter notes.
            if kind not in {"video", "final_video", "rendered_video", "cover", "cover_vertical", "cover_horizontal", "image"}:
                continue
            path = _safe_path(artifact.get("path", ""), roots, root)
            is_video = path.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}
            is_image = path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            if not (is_video or is_image) or not path.is_file():
                raise ArchiveError("归档媒体必须是存在的视频或图片文件")
            if str(path) in seen:
                continue
            seen.add(str(path))
            label = _text(artifact.get("name") or path.name, single_line=True).replace("[", "(").replace("]", ")")
            if _under(path, vault):
                relative = path.relative_to(vault).as_posix()
                if any(character in relative for character in ["[", "]", "|", "\n", "\r"]):
                    raise ArchiveError("媒体文件名不适合 Obsidian 链接")
                link = f"![[{relative}]]"
            else:
                link = f"[{label}](<{path.as_uri()}>)"
            found.append({"kind": "video" if is_video else "cover", "name": label, "path": str(path), "link": link})
    return found


def preview_archive(project: dict, vault: Path, project_root: Path) -> dict:
    """Build a side-effect-free plan. ``hash`` guards the exact reviewed plan."""
    vault, root = Path(vault).resolve(), Path(project_root).resolve()
    if not vault.is_dir() or not root.is_dir():
        raise ArchiveError("知识库与项目根目录必须存在")
    workflow_id = project.get("id", "")
    if not isinstance(workflow_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", workflow_id):
        raise ArchiveError("工作流 ID 无效")
    if project.get("kind") not in {"article", "video"}:
        raise ArchiveError("归档类型必须是 article 或 video")
    # Reject redirected output directories before even enumerating note headers.
    output_root = _safe_path("3-输出", [vault], vault)
    config = (project.get("settings") or {}).get("archive") or {}
    artifacts = _safe_path(Path("outputs") / "视频工作流" / workflow_id / "artifacts", [root], root)
    state_path = _safe_path(artifacts / "archive-state.json", [root], root)
    receipt_path = _safe_path(artifacts / "publication-receipt.json", [root], root)
    owned = _read_object(state_path) if state_path.is_file() else {}
    if owned and (owned.get("workflow_id") != workflow_id or owned.get("vault") != str(vault)):
        raise ArchiveConflict("归档回执与当前工作流或知识库不匹配")
    manuscript, body = _primary(project, vault, root)
    title = _text(project.get("title") or manuscript.get("title"), single_line=True)
    if not title:
        raise ArchiveError("归档标题不能为空")
    requested_number = config.get("project_id", config.get("number"))
    number = _number(vault, requested_number, owned)
    raw_date = owned.get("create_date") or config.get("create_date") or str(project.get("created_at") or "")[:10] or date.today().isoformat()
    try:
        created = date.fromisoformat(raw_date).isoformat()
    except (TypeError, ValueError) as exc:
        raise ArchiveError("创建日期必须是 YYYY-MM-DD") from exc
    status, verified_platforms, warnings, receipt_hash = _publication(project, receipt_path, manuscript)
    settings = project.get("settings") or {}
    platform_names = {"weixin-channels": "视频号", "douyin": "抖音", "kuaishou": "快手", "bilibili": "B站", "xiaohongshu": "小红书"}
    flat_platform = settings.get("publish_platform") or settings.get("platform")
    fallback_platforms = [platform_names.get(flat_platform, flat_platform)] if flat_platform else []
    platforms = config.get("platforms", settings.get("platforms", fallback_platforms))
    if not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms):
        raise ArchiveError("归档平台必须是文本列表")
    platforms = list(dict.fromkeys([*[_text(p, single_line=True) for p in platforms if p.strip()], *verified_platforms]))
    # One platform's publication must not mark every intended platform published.
    if status == "已发布" and set(platforms) - set(verified_platforms):
        status = "部分已发布"
    folder = "已发布" if status == "已发布" else "草稿"
    archive_folder = settings.get("archive_folder") or str(Path("3-输出") / folder)
    default = Path(archive_folder) / f"{number:03d}_{created.replace('-', '')}_{_filename_title(title)}.md"
    target = _safe_path(owned.get("note_path") or config.get("target_path") or default, [vault], vault)
    if not _under(target, output_root) or target.suffix.lower() != ".md":
        raise ArchiveError("归档目标必须是 3-输出 内的 Markdown 文件")
    media = _media(project, vault, root)
    lines = ["---", f"project_id: {number}", f"create_date: {created}", f"status: {status}"]
    lines += ["platform:", *[f"  - {json.dumps(p, ensure_ascii=False)}" for p in platforms]] if platforms else ["platform: []"]
    lines += ["---", "", f"# {title}", ""]
    if media:
        lines += ["## 发布内容", ""]
        for category, heading in [("video", "发布视频"), ("cover", "发布封面")]:
            items = [item for item in media if item["kind"] == category]
            if items:
                lines += [f"### {heading}", ""]
                for item in items:
                    lines += [item["link"], ""]
    lines += ["## 视频脚本" if project["kind"] == "video" else "## 正文", "", body, ""]
    copy = settings.get("publication_copy")
    if copy is None:
        copy = {"title": settings.get("publish_title"), "description": settings.get("publish_description", settings.get("description")), "topics": settings.get("publish_tags", settings.get("tags"))}
    if copy:
        lines += ["## 发布文案", ""]
        for key, label in [("title", "通用标题"), ("description", "通用简介"), ("topics", "通用话题")]:
            value = copy.get(key)
            if isinstance(value, list):
                value = " ".join(str(part) for part in value)
            if value:
                lines += [f"### {label}", "", _text(value), ""]
    lines += ["## 平台发布信息", "", f"当前状态：{status}。", ""]
    if platforms:
        lines += ["| 平台 | 状态 |", "| --- | --- |"]
        for platform in platforms:
            platform_status = "已发布" if status in {"已发布", "部分已发布"} and platform in verified_platforms else "已存草稿" if status == "已存草稿" and platform in verified_platforms else "待发布"
            lines.append(f"| {platform.replace('|', '／')} | {platform_status} |")
        lines.append("")
    lines += ["## 存档说明", "", f"主稿：{_text(manuscript.get('title') or title, single_line=True)}（版本 {_text(manuscript.get('version', 1), single_line=True)}）。", "", "原稿和原始素材保留原位；本页媒体链接指向现有文件。", ""]
    if status not in {"已发布", "部分已发布"}:
        lines += ["归档不代表公开发布；实际发布结果核实后再更新状态。", ""]
    content = "\n".join(lines)
    before = _digest(target.read_bytes()) if target.is_file() else None
    after = _digest(content)
    conflict = None
    if target.exists() and not target.is_file():
        conflict = "目标不是普通文件"
    elif before and not owned:
        conflict = "目标已有内容且不是本工作流创建的归档，禁止覆盖"
    elif before and before != owned.get("content_hash"):
        conflict = "归档已被用户或其他程序修改，请先核对差异"
    elif owned and not before:
        conflict = "已有归档被移走或删除，不自动重建"
    action = "conflict" if conflict else "unchanged" if before == after else "update" if before else "create"
    plan = {"workflow_id": workflow_id, "project_id": number, "create_date": created,
            "note_path": str(target), "primary_manuscript_id": manuscript.get("id"),
            "publication_status": status, "platforms": platforms, "media": media,
            "ownership_receipt": str(state_path), "publication_receipt_hash": receipt_hash,
            "warnings": warnings}
    targets = [{"path": str(target), "action": action, "before_hash": before, "after_hash": after}]
    plan_hash = _digest(_json({"plan": plan, "targets": targets, "content": content}))
    return {"status": "conflict" if conflict else "ready", "hash": plan_hash,
            "content": content, "targets": targets, "plan": plan, "conflict": conflict}


def _atomic_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".archive-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _archive_lock(root: Path):
    directory = _safe_path(Path("outputs") / "视频工作流", [root], root)
    directory.mkdir(parents=True, exist_ok=True)
    lock = _safe_path(directory / ".archive.lock", [root], root)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ArchiveConflict("另一个归档正在执行，请稍后重试") from exc
    try:
        os.close(descriptor)
        yield
    finally:
        lock.unlink(missing_ok=True)


def apply_archive(project: dict, vault: Path, project_root: Path, expected_hash: str | None = None) -> dict:
    """Apply an explicitly requested plan; never overwrite user-owned content."""
    initial = preview_archive(project, vault, project_root)
    if initial["status"] == "conflict":
        raise ArchiveConflict(initial["conflict"])
    if expected_hash is not None and initial["hash"] != expected_hash:
        raise ArchiveConflict("预览已过期，请重新预览并确认")
    root = Path(project_root).resolve()
    with _archive_lock(root):
        preview = preview_archive(project, vault, root)
        if preview["status"] == "conflict" or preview["hash"] != initial["hash"]:
            raise ArchiveConflict(preview["conflict"] or "归档计划已变化，请重新预览")
        target_info = preview["targets"][0]
        if target_info["action"] == "unchanged":
            return {**preview, "status": "unchanged"}
        target = Path(target_info["path"])
        state_path = Path(preview["plan"]["ownership_receipt"])
        target.parent.mkdir(parents=True, exist_ok=True)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        # Recheck resolved paths after directory creation (including junctions).
        _safe_path(target, [Path(vault).resolve()], Path(vault).resolve())
        _safe_path(state_path, [root], root)
        if target_info["action"] == "create":
            with target.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(preview["content"])
                handle.flush()
                os.fsync(handle.fileno())
        else:
            # Compare immediately before replacement, not just when previewed.
            if _digest(target.read_bytes()) != target_info["before_hash"]:
                raise ArchiveConflict("归档文件在写入前发生变化")
            descriptor, temporary = tempfile.mkstemp(prefix=".easel-", suffix=".tmp", dir=target.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(preview["content"])
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        _atomic_json(state_path, {"workflow_id": project["id"], "vault": str(Path(vault).resolve()),
                     "project_id": preview["plan"]["project_id"], "create_date": preview["plan"]["create_date"],
                     "note_path": str(target), "content_hash": target_info["after_hash"]})
        return {**preview, "status": "applied"}
