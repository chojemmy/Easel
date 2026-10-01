"""Generation limits from the active native Agent model, without credentials."""
from __future__ import annotations

import json
from pathlib import Path

from .openclaw_workspace import state_dir


def generation_limit(generation_budget=None, max_tokens=None, *, config_path: Path | None = None) -> int:
    if generation_budget not in {None, "standard", "large", "maximum"}:
        raise ValueError("生成预算仅接受 standard、large 或 maximum。")
    limit = 65536
    try:
        config = json.loads((config_path or state_dir() / "openclaw.json").read_text(encoding="utf-8-sig"))
        primary = config.get("agents", {}).get("defaults", {}).get("model", {})
        primary = primary.get("primary", "") if isinstance(primary, dict) else primary
        provider, _, model = primary.partition("/")
        models = config.get("models", {}).get("providers", {}).get(provider, {}).get("models", [])
        entry = next((item for item in models if item.get("id") == model), {})
        ceiling = entry.get("maxTokens")
        if type(ceiling) is int and 256 <= ceiling <= 1048576:
            limit = ceiling
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    if max_tokens is not None:
        if type(max_tokens) is not int or not 256 <= max_tokens <= limit:
            raise ValueError(f"原 Agent 的输出预算须为 256 到 {limit} 之间的整数。")
        return max_tokens
    return {"standard": min(8192, limit), "large": min(32768, limit), "maximum": limit}[generation_budget or "maximum"]
