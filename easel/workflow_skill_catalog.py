"""Read-only access to Easel's original installed Skills for node assistants.

The Web skill library and ``easel skill`` enumerate ``skills/openclaw`` and
accept both ``name`` and ``skill-name``. This module uses that same registry,
plus explicitly configured OpenClaw workspace/extra skill directories. It
does not import the Web application, launch an agent, execute a script, read
dotenv files, or claim that merely loading a Skill executes its tools.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Mapping, Sequence


NODES = ("brief", "script", "source", "transcript", "storyboard", "build", "review", "deliver", "publish", "archive")
MAX_FILE_BYTES = 262_144
MAX_CHARS = 24_000
MAX_CONFIG_BYTES = 1_048_576
TEXT_EXTENSIONS = {".md", ".markdown", ".txt", ".rst", ".json", ".yaml", ".yml", ".csv"}
PRIVATE_NAMES = {
    "private", "secrets", "secret", "credentials", "credential", "auth", "authentication",
    "cookies", "cookie", "tokens", "token", "passwords", "password", "apikey", "api-key",
    "api_key", "api-keys", "api_keys", "config", "configuration", "settings", "openclaw",
    "sessions", "accounts", "profiles", "node_modules", "id_rsa", "id_ed25519",
}
_SECRET_LITERALS = re.compile(
    r"(?:sk-(?:ant-[\w-]+|[A-Za-z0-9_-]{16,})|xox[baprs]-[A-Za-z0-9-]{12,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|"
    r"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,})"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?im)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"token|password|passwd|secret|authorization|cookie)[\"']?\s*[:=]\s*)"
    r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}]+)"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----.*?(?:-----END (?:[A-Z ]+)?PRIVATE KEY-----|\Z)", re.S)
_SECRET_HEADER = re.compile(r"(?im)([\"']?(?:authorization|cookie)[\"']?\s*[:=]\s*)[^\r\n]+")
_SAFE_NAME = re.compile(r"[\w][\w.-]{0,127}\Z", re.UNICODE)
_LINK = re.compile(r"\[[^\]\n]*\]\(<?([^\s)>]+)>?(?:\s+['\"][^\n]*?['\"])?\)|`([^`\n]+)`")


class WorkflowSkillCatalogError(ValueError):
    """A safe, user-facing explanation of an unavailable Skill read."""


def _redact(text: str) -> str:
    text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    text = _SECRET_LITERALS.sub("[REDACTED]", text)
    text = _SECRET_HEADER.sub(r"\1[REDACTED]", text)
    def assignment(match: re.Match) -> str:
        value = match[2]
        # Environment variable references are instructions, not credential values.
        if re.fullmatch(r"[\"']?\$\{?[A-Z][A-Z0-9_]*\}?[\"']?", value):
            return match[0]
        return match[1] + "[REDACTED]"
    return _SECRET_ASSIGNMENT.sub(assignment, text)


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        if value[0] == '"':
            try:
                return str(json.loads(value))
            except ValueError:
                pass
        return value[1:-1]
    return value


def _frontmatter(text: str) -> dict[str, str]:
    """Parse the small scalar subset used by the existing Web/CLI registry."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}
    result: dict[str, str] = {}
    i = 1
    while i < end:
        match = re.fullmatch(r"([A-Za-z][\w-]*):\s*(.*)", lines[i])
        if not match:
            i += 1
            continue
        key, value = match.groups()
        if value and value[0] in "|>":
            parts = []
            i += 1
            while i < end and (not lines[i].strip() or lines[i][:1].isspace()):
                if lines[i].strip():
                    parts.append(lines[i].strip())
                i += 1
            result[key] = " ".join(parts)
        else:
            result[key] = _scalar(value)
            i += 1
    # OpenClaw's per-Skill config key may be nested YAML or inline JSON metadata.
    header = "\n".join(lines[1:end])
    found = re.search(r'(?:\bskillKey\s*:|"skillKey"\s*:)\s*["\']?([\w.-]+)', header)
    if found:
        result["skillKey"] = found[1]
    return result


def _relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or len(value) > 512 or "\0" in value:
        raise WorkflowSkillCatalogError("Skill 引用路径无效。")
    raw = value.replace("\\", "/")
    if raw.startswith("/") or PureWindowsPath(value).drive or ":" in raw:
        raise WorkflowSkillCatalogError("Skill 只能读取技能目录内的相对路径。")
    path = PurePosixPath(raw)
    if not path.parts or any(part == ".." or part != part.rstrip(" .") for part in path.parts):
        raise WorkflowSkillCatalogError("Skill 引用不能越过技能目录。")
    for part in path.parts:
        stem = part.lower().split(".", 1)[0]
        if part.startswith((".", "_")) or stem in PRIVATE_NAMES:
            raise WorkflowSkillCatalogError("不能读取私有文件、配置或凭证。")
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        raise WorkflowSkillCatalogError("仅可读取 Skill 正文和文字参考资料；不能读取或执行程序、二进制或密钥文件。")
    return path


def _safe_path(skill_root: Path, relative: str) -> Path:
    path = _relative(relative)
    try:
        target = skill_root.joinpath(*path.parts).resolve(strict=True)
        if not target.is_relative_to(skill_root) or not target.is_file():
            raise WorkflowSkillCatalogError("Skill 引用超出技能目录或不是文件。")
        # Re-check canonical names: a harmless alias must not reveal .env/private.
        _relative(target.relative_to(skill_root).as_posix())
        return target
    except (OSError, RuntimeError) as exc:
        raise WorkflowSkillCatalogError("Skill 参考资料不存在或无法读取。") from exc


def _text(path: Path) -> str:
    try:
        # The bounded read also covers a file that grows after stat().
        with path.open("rb") as stream:
            if path.resolve(strict=True) != path:
                raise WorkflowSkillCatalogError("Skill 文件位置发生变化，请重新读取。")
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise WorkflowSkillCatalogError("Skill 文字文件超过 256 KB，请缩小参考资料范围。")
        value = data.decode("utf-8-sig")
        if "\0" in value:
            raise WorkflowSkillCatalogError("Skill 参考资料必须是 UTF-8 文字文件。")
        return _redact(value)
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise WorkflowSkillCatalogError("Skill 参考资料无法作为 UTF-8 文字读取。") from exc


def _stages(name: str, description: str, layer: str) -> list[str]:
    """Routing hints limit prompt size; capability is always read-only guidance."""
    key = name.removeprefix("skill-").lower()
    exact = {
        "text-polisher": {"brief", "script", "transcript", "storyboard", "review", "deliver", "publish"},
        "video-script": {"script", "storyboard"}, "copywriting": {"script", "deliver", "publish"},
        "text-condenser": {"brief", "script", "transcript", "deliver", "publish"},
        "video-production": {"source", "transcript", "storyboard", "build", "review", "deliver"},
        "remotion-video-production": {"storyboard", "build", "review", "deliver"},
        "remotion-best-practices": {"storyboard", "build", "review", "deliver"},
        "asset-manager": {"source", "storyboard", "build", "deliver", "archive"},
        "template-library": {"storyboard", "build", "deliver"},
        "obsidian-knowledge-flywheel": {"brief", "script", "archive"},
    }
    if key in exact:
        return [node for node in NODES if node in exact[key]]
    searchable = f"{key} {description}".lower()
    groups = {
        "brief": r"strateg|topic|audience|brand|campaign|competitor|positioning|intelligence|rss|选题|策划|定位|受众|调研|资料",
        "script": r"script|copywrit|article|outline|hook|text-|social-content|novel|paper|voice-builder|口播|写作|文案|润色|大纲",
        "source": r"asset|video-edit|audio-edit|录制|原片|媒体导入|素材管理",
        "transcript": r"subtitle|transcri|audio-|字幕|转录|语音识别",
        "storyboard": r"video|story|card|design|image|visual|mindmap|分镜|画面|镜头|视觉",
        "build": r"video|remotion|audio|image|card|design|chart|poster|visual|配音|制作|渲染|动画",
        "review": r"quality|risk|compliance|persona-check|post-scorer|seo|校对|检查|质检|审核|评分",
        "deliver": r"formatter|convert|cover|design|card|封面|交付|标题|导出|排版",
        "publish": r"publish|upload|cross-platform|post-format|发布|分发|平台适配",
        "archive": r"calendar|publish-log|postmortem|report|tracker|archive|归档|存档|复盘|记录",
    }
    stages = [node for node in NODES if re.search(groups[node], searchable)]
    if not stages:
        stages = {"strategy": ["brief"], "produce": ["script", "storyboard", "build"],
                  "publish": ["deliver", "publish"], "review": ["review", "archive"]}.get(layer, list(NODES))
    return stages


class WorkflowSkillCatalog:
    def __init__(self, project_root: str | Path | None = None, *, extra_roots: Sequence[str | Path] = (),
                 config_path: str | Path | None = None, environment: Mapping[str, str] | None = None):
        self.project_root = Path(project_root).resolve() if project_root is not None else Path(__file__).resolve().parents[1]
        self.environment = dict(os.environ if environment is None else environment)
        self.state_dir = Path(self.environment.get("EASEL_OPENCLAW_STATE_DIR", str(Path.home() / ".openclaw-easel"))).expanduser().resolve()
        self.config_path = Path(config_path).expanduser().resolve() if config_path is not None else self.state_dir / "openclaw.json"
        self.extra_roots = tuple(Path(value).expanduser() for value in extra_roots)

    def _configuration(self) -> dict:
        try:
            with self.config_path.open("rb") as stream:
                raw = stream.read(MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                raise ValueError
            value = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, ValueError) as exc:
            raise WorkflowSkillCatalogError("OpenClaw 技能配置无法读取，无法可靠判断哪些技能已禁用。") from exc

    def _registry(self) -> dict[str, dict]:
        config = self._configuration()
        skills = config.get("skills") if isinstance(config.get("skills"), dict) else {}
        entries = skills.get("entries") if isinstance(skills.get("entries"), dict) else {}
        disabled = {name for name, value in entries.items() if isinstance(value, dict) and value.get("enabled") is False}
        roots = [self.project_root / "skills" / "openclaw"]
        configured_project = self.environment.get("EASEL_ROOT")
        if configured_project:
            roots.append(Path(configured_project).expanduser() / "skills" / "openclaw")
        override = self.environment.get("EASEL_OPENCLAW_WORKSPACE")
        agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
        agent_list = agents.get("list") if isinstance(agents.get("list"), list) else []
        selected = next((item for item in agent_list if isinstance(item, dict) and item.get("id") == "main"), {})
        legacy = agents.get("entries") if isinstance(agents.get("entries"), dict) else {}
        main = legacy.get("main") if isinstance(legacy.get("main"), dict) else {}
        defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
        workspace = override or main.get("workspace") or selected.get("workspace") or defaults.get("workspace")
        if isinstance(workspace, str) and workspace.strip():
            roots.append(Path(os.path.expanduser(workspace)) / "skills")
        else:
            roots.append(self.state_dir / "workspace" / "skills")
        load = skills.get("load") if isinstance(skills.get("load"), dict) else {}
        configured_extra = load.get("extraDirs") if isinstance(load.get("extraDirs"), list) else []
        roots.extend(Path(os.path.expanduser(value)) for value in configured_extra if isinstance(value, str) and value.strip())
        roots.extend(self.extra_roots)
        registry: dict[str, dict] = {}
        seen_roots: set[Path] = set()
        for root in roots:
            if not root.is_absolute():
                root = self.project_root / root
            try:
                root = root.resolve(strict=True)
                if root in seen_roots or not root.is_dir():
                    continue
                seen_roots.add(root)
                candidates = sorted(root.iterdir(), key=lambda value: value.name.casefold())
            except (OSError, RuntimeError):
                continue
            for directory in candidates[:512]:
                name = directory.name
                if not _SAFE_NAME.fullmatch(name) or name.startswith((".", "_")) or name in registry:
                    continue
                try:
                    directory = directory.resolve(strict=True)
                    if not directory.is_relative_to(root) or not directory.is_dir():
                        continue
                    path = _safe_path(directory, "SKILL.md")
                    body = _text(path)
                    metadata = _frontmatter(body)
                except (OSError, RuntimeError, WorkflowSkillCatalogError):
                    continue
                aliases = {name, name.removeprefix("skill-"), f"skill-{name}"}
                aliases.update(value for value in (metadata.get("name"), metadata.get("skillKey")) if value)
                if aliases & disabled:
                    continue
                description = _redact(metadata.get("description", ""))[:800]
                layer = metadata.get("layer", "")[:80]
                requires_tools = (directory / "scripts").is_dir() or bool(re.search(
                    r"(?i)(?:\b(?:python3?|bash|node|ffmpeg|npx|uv run)\s+|scripts/|subprocess|执行脚本|调用.{0,12}API)", body))
                registry[name] = {"id": name, "name": name, "description": description, "layer": layer,
                                  "stages": _stages(name, description, layer), "requires_tools": requires_tools,
                                  "capability": "read_only_guidance",
                                  "execution_note": "仅加载方法说明；其中脚本/API操作需要对应执行工具，本目录不会执行。" if requires_tools else "可将文字方法用于本节点；加载 Skill 不代表已执行工具。",
                                  "_root": directory, "_aliases": aliases}
        return registry

    @staticmethod
    def _node(node: str | None) -> None:
        if node is not None and node not in NODES:
            raise WorkflowSkillCatalogError("未知的工作流节点。")

    def list_skills(self, node: str | None = None, *, limit: int = 120, include_source: bool = False) -> list[dict]:
        self._node(node)
        if type(limit) is not int or not 1 <= limit <= 256:
            raise WorkflowSkillCatalogError("Skill 目录数量上限必须在 1–256 之间。")
        values = [item for item in self._registry().values() if node is None or node in item["stages"]]
        preferred = {"text-polisher": 0, "video-script": 1, "copywriting": 2, "text-condenser": 3}
        values.sort(key=lambda item: (preferred.get(item["name"], 100), item["name"]))
        result = []
        for item in values[:limit]:
            public = {key: value for key, value in item.items() if not key.startswith("_")}
            if include_source:
                public["source_path"] = str(_safe_path(item["_root"], "SKILL.md"))
            result.append(public)
        return result

    def _entry(self, name: str, node: str | None) -> dict:
        self._node(node)
        if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name) or name.startswith((".", "_")):
            raise WorkflowSkillCatalogError("Skill 名称无效。")
        registry = self._registry()
        item = registry.get(name) or registry.get(f"skill-{name}")
        if item is None:
            matches = [value for value in registry.values() if name in value["_aliases"]]
            item = matches[0] if len(matches) == 1 else None
        if item is None:
            raise WorkflowSkillCatalogError("未找到已安装且启用的 Skill。")
        if node is not None and node not in item["stages"]:
            raise WorkflowSkillCatalogError("这份 Skill 不适用于当前节点，请在对应节点调用。")
        return item

    def resolve_source(self, name: str, relative_path: str = "SKILL.md", *, node: str | None = None) -> Path:
        """Re-resolve an enabled source. This method only validates; it never writes.

        An authorized learning store may use this result after separate preview,
        confirmation, content validation and compare-and-swap checks of its own.
        It must resolve again immediately before applying a proposed change.
        """
        item = self._entry(name, node)
        return _safe_path(item["_root"], relative_path)

    def read_skill(self, name: str, relative_path: str = "SKILL.md", *, node: str | None = None,
                   offset: int = 0, max_chars: int = MAX_CHARS) -> dict:
        if type(offset) is not int or offset < 0 or type(max_chars) is not int or not 1 <= max_chars <= MAX_CHARS:
            raise WorkflowSkillCatalogError("Skill 读取范围无效，单次最多 24000 字符。")
        item = self._entry(name, node)
        relative = _relative(relative_path).as_posix()
        path = _safe_path(item["_root"], relative)
        text = _text(path)
        if offset > len(text):
            raise WorkflowSkillCatalogError("Skill 读取起点超过正文长度。")
        references = []
        for match in _LINK.finditer(text):
            target = (match[1] or match[2]).split("#", 1)[0].strip()
            if not target or ":" in target or target.startswith("/"):
                continue
            try:
                # References inside a nested document are relative to that document.
                pieces = list(PurePosixPath(relative).parent.parts)
                for part in target.replace("\\", "/").split("/"):
                    if part in {"", "."}:
                        continue
                    if part == "..":
                        if not pieces:
                            raise WorkflowSkillCatalogError("引用超出 Skill 目录。")
                        pieces.pop()
                    else:
                        pieces.append(part)
                candidate = "/".join(pieces)
                _safe_path(item["_root"], candidate)
                candidate = _relative(candidate).as_posix()
                if candidate != relative and candidate not in references:
                    references.append(candidate)
            except WorkflowSkillCatalogError:
                continue
            if len(references) == 48:
                break
        end = min(offset + max_chars, len(text))
        return {"id": item["id"], "name": item["name"], "path": relative, "content": text[offset:end],
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "offset": offset,
                "total_chars": len(text), "truncated": end < len(text), "next_offset": end if end < len(text) else None,
                "references": references, "requires_tools": item["requires_tools"],
                "capability": item["capability"], "execution_note": item["execution_note"]}
