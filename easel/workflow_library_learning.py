"""Review and explicitly apply learning to an installed Skill's real source.

Proposals and history stay in this project's outputs. Only ``apply`` writes the
registered source; callers must show the diff and obtain approval first.
"""
from __future__ import annotations

from contextlib import contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path
import re

from .workflow_skill_catalog import WorkflowSkillCatalog
from .workflow_skills import (
    NodeSkillStore, WorkflowSkillConflict, _atomic_write, _check_secrets, _now,
    _owned_path, _write_json,
)
import uuid


MAX_BYTES = 262_144
WorkflowLibraryConflict = WorkflowSkillConflict


def _version(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _frontmatter(content: str) -> str:
    match = re.match(r"\A\ufeff?---\r?\n.*?\r?\n---(?:\r?\n|\Z)", content, re.S)
    return match[0].replace("\r\n", "\n") if match else ""


def _diff(before: str, after: str, name: str, relative: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                       fromfile=f"a/{name}/{relative}", tofile=f"b/{name}/{relative}"))


def _seal(proposal: dict) -> str:
    # Detect damaged or edited persisted proposals, including path/identity edits.
    fields = ("proposal_id", "node", "scope", "skill_name", "relative_path", "target_path",
              "base_version", "after_version", "before", "after", "instruction", "diff")
    return hashlib.sha256(json.dumps({key: proposal.get(key) for key in fields},
                         ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class WorkflowLibraryLearning:
    def __init__(self, root: Path, catalog: WorkflowSkillCatalog | None = None):
        self.root = Path(root).expanduser().resolve()
        self.catalog = catalog if catalog is not None else WorkflowSkillCatalog(self.root)
        self.proposal_root = self.root / "outputs" / "_workflow_library_proposals"

    def _path(self, *parts: str) -> Path:
        return _owned_path(self.root, "outputs", "_workflow_library_proposals", *parts)

    def _source(self, node: str, name: str, relative: str) -> Path:
        NodeSkillStore._node(node)
        if not isinstance(relative, str) or Path(relative).suffix.lower() not in {".md", ".markdown"}:
            raise ValueError("经验只能写入已注册 Skill 的 Markdown 正文或参考指南。")
        source = self.catalog.resolve_source(name, relative_path=relative, node=node)
        if source.suffix.lower() not in {".md", ".markdown"}:
            raise ValueError("Skill 经验目标的真实源必须是 Markdown 文件。")
        return source

    @staticmethod
    def _read(path: Path) -> str:
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Skill 文件超过 256 KB，无法生成可审核的经验提议。")
        content = data.decode("utf-8")
        if "\0" in content:
            raise ValueError("Skill 必须是 UTF-8 Markdown 文字。")
        _check_secrets(content)
        for key, value in os.environ.items():
            if len(value) >= 8 and re.search(r"(?i)(key|token|secret|password)", key) and value in content:
                raise ValueError("Skill 内容疑似包含本机凭证，不能保存为经验提议。")
        return content

    @staticmethod
    def _validate(before: str, after: str, relative: str) -> None:
        if not isinstance(after, str) or not after.strip() or len(after.encode("utf-8")) > MAX_BYTES or "\0" in after:
            raise ValueError("经验提议必须是非空、最多 256 KB 的 Markdown 文字。")
        _check_secrets(after)
        for key, value in os.environ.items():
            if len(value) >= 8 and re.search(r"(?i)(key|token|secret|password)", key) and value in after:
                raise ValueError("经验提议疑似包含本机凭证。")
        old_header, new_header = _frontmatter(before), _frontmatter(after)
        if old_header != new_header:
            raise ValueError("请完整保留原 Skill 的 frontmatter 与名称，只修改正文规则。")
        if Path(relative).name.casefold() == "skill.md" and not re.search(r"(?m)^(?:name|skill-name):\s*\S", old_header):
            raise ValueError("原 Skill 缺少可保留的 name/skill-name frontmatter。")

    def propose(self, node: str, skill_name: str, relative_path: str, instruction: str,
                after_content: str | None = None, context=None) -> dict:
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 4000:
            raise ValueError("请将经验提炼为最多 4000 字的可复用规则，不要粘贴项目全文。")
        _check_secrets(instruction)
        source = self._source(node, skill_name, relative_path)
        before = self._read(source)
        after = after_content if after_content is not None else before.rstrip() + "\n\n## 已确认的经验\n\n" + instruction.strip() + "\n"
        self._validate(before, after, relative_path)
        if after == before:
            raise ValueError("经验提议没有产生实际差异。")
        # Raw project context is never persisted or appended to shared guidance.
        # Reject accidental inclusion of supplied full drafts in the new text.
        if isinstance(context, dict):
            private = [context.get(key) for key in ("draft", "transcript", "manuscript", "source_text", "content")]
            if isinstance(context.get("manuscripts"), list):
                private.extend(item.get("content") for item in context["manuscripts"] if isinstance(item, dict))
            if any(isinstance(text, str) and len(text) >= 200 and text not in before and text in after for text in private):
                raise ValueError("请沉淀通用规则，不要把项目完整稿件写入共享 Skill。")
        identifier = uuid.uuid4().hex
        proposal = {
            "id": identifier, "proposal_id": identifier, "node": node, "scope": "library",
            "name": skill_name, "skill_name": skill_name, "relative_path": relative_path,
            "target_path": str(source), "before": before, "after": after,
            "base_version": _version(before), "after_version": _version(after),
            "diff": _diff(before, after, skill_name, relative_path), "instruction": instruction.strip(),
            "context": NodeSkillStore._context_refs(context), "context_omitted": bool(context),
            "created_at": _now(), "status": "pending",
        }
        proposal["integrity"] = _seal(proposal)
        _write_json(self._path(f"{identifier}.json"), proposal)
        return proposal

    def get_proposal(self, node: str, proposal_id: str) -> dict:
        NodeSkillStore._node(node)
        if not isinstance(proposal_id, str) or not re.fullmatch(r"[0-9a-f]{32}", proposal_id):
            raise ValueError("无效的 Skill 经验提议编号。")
        path = self._path(f"{proposal_id}.json")
        if path.stat().st_size > 4 * MAX_BYTES:
            raise ValueError("Skill 经验提议文件过大。")
        proposal = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(proposal, dict) or proposal.get("scope") != "library" or proposal.get("node") != node or proposal.get("proposal_id") != proposal_id or proposal.get("id") != proposal_id:
            raise ValueError("经验提议不属于此节点。")
        if proposal.get("integrity") != _seal(proposal):
            raise WorkflowSkillConflict("Skill 经验提议已改变，请重新生成并审核差异。")
        for key in ("before", "after", "skill_name", "relative_path", "target_path"):
            if not isinstance(proposal.get(key), str):
                raise ValueError("Skill 经验提议格式无效。")
        if _version(proposal["before"]) != proposal["base_version"] or _version(proposal["after"]) != proposal["after_version"]:
            raise WorkflowSkillConflict("Skill 经验提议版本不匹配，请重新生成。")
        self._validate(proposal["before"], proposal["after"], proposal["relative_path"])
        if proposal["diff"] != _diff(proposal["before"], proposal["after"], proposal["skill_name"], proposal["relative_path"]):
            raise WorkflowSkillConflict("Skill 经验提议差异不匹配，请重新审核。")
        return proposal

    @contextmanager
    def _lock(self, source: Path):
        # Same physical source shares a lock even across project output roots.
        lock_path = _owned_path(source.parent, f".{source.name}.workflow-learning.lock")
        with lock_path.open("a+b") as handle:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise WorkflowSkillConflict("原 Skill 正在被另一项目更新，请稍后重新查看。") from exc
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

    def _identity(self, source: Path) -> str:
        return hashlib.sha256(os.path.normcase(str(source)).encode("utf-8")).hexdigest()

    def _snapshot(self, proposal: dict, source: Path, content: str) -> None:
        version = _version(content)
        target = self._path("history", self._identity(source), version.split(":")[1] + ".json")
        if not target.exists():
            _write_json(target, {"node": proposal["node"], "skill_name": proposal["skill_name"],
                                "relative_path": proposal["relative_path"], "target_path": str(source),
                                "version": version, "content": content, "recorded_at": _now(),
                                "proposal_id": proposal["proposal_id"]})

    def apply(self, node: str, proposal_id: str) -> dict:
        """Approval boundary: apply precisely the reviewed change to its source."""
        proposal = self.get_proposal(node, proposal_id)
        source = self._source(node, proposal["skill_name"], proposal["relative_path"])
        if str(source) != proposal["target_path"]:
            raise WorkflowSkillConflict("原 Skill 路径已改变，请重新生成并审核提议。")
        with self._lock(source):
            if self._source(node, proposal["skill_name"], proposal["relative_path"]) != source:
                raise WorkflowSkillConflict("原 Skill 注册位置已改变，请重新审核。")
            current = self._read(source)
            version = _version(current)
            if version != proposal["after_version"]:
                if proposal.get("status") != "pending" or version != proposal["base_version"]:
                    raise WorkflowSkillConflict("原 Skill 已更新，本提议基于旧版本；请重新查看差异。")
                self._snapshot(proposal, source, current)
                self._snapshot(proposal, source, proposal["after"])
                if self._source(node, proposal["skill_name"], proposal["relative_path"]) != source or _version(self._read(source)) != version:
                    raise WorkflowSkillConflict("原 Skill 在应用前已变化，请重新审核。")
                _atomic_write(source, proposal["after"])
                current = proposal["after"]
            proposal.update(status="applied", applied_at=proposal.get("applied_at") or _now())
            _write_json(self._path(f"{proposal_id}.json"), proposal)
            return {"node": node, "scope": "library", "name": proposal["skill_name"],
                    "skill_name": proposal["skill_name"], "relative_path": proposal["relative_path"],
                    "target_path": str(source), "source": str(source), "content": current,
                    "version": _version(current), "proposal_id": proposal_id, "status": "applied"}

    def history(self, node: str, skill_name: str, relative_path: str = "SKILL.md") -> list[dict]:
        source = self._source(node, skill_name, relative_path)
        folder = self._path("history", self._identity(source))
        values = [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("*.json")]
        return sorted(values, key=lambda item: item["recorded_at"], reverse=True)
