"""Discover complete existing faster-whisper models without downloading anything."""
from __future__ import annotations

import os
from pathlib import Path


def complete_model(path: Path) -> bool:
    try:
        return (all((path / name).is_file() and (path / name).stat().st_size > 0
                    for name in ("model.bin", "config.json", "tokenizer.json"))
                and any((path / name).is_file() and (path / name).stat().st_size > 0
                        for name in ("vocabulary.txt", "vocabulary.json")))
    except OSError:
        return False


def find_local_model(root: Path, *, settings=None, options=None, home=None, environment=None) -> dict | None:
    """Explicit selection wins; otherwise reuse Easel/Hugging Face caches.

    Paths are validated locally. A missing explicit selection is an error rather
    than silently replacing the user's model with a different one.
    """
    settings, options = settings or {}, options or {}
    env = os.environ if environment is None else environment
    home = Path.home() if home is None else Path(home)
    root = Path(root).resolve()
    explicit = (options.get("asr_model_path") or settings.get("asr_model_path")
                or env.get("EASEL_ASR_MODEL_PATH") or env.get("WHISPER_MODEL_PATH"))
    if explicit:
        path = Path(explicit).expanduser()
        path = (path if path.is_absolute() else root / path).resolve()
        if not complete_model(path):
            raise ValueError("指定的 ASR 模型不完整：需要 model.bin、config.json、tokenizer.json 和 vocabulary 文件。")
        return {"path": str(path), "name": path.name, "source": "configured"}

    hf_home = Path(env.get("HF_HOME") or (Path(env.get("XDG_CACHE_HOME") or home / ".cache") / "huggingface"))
    hub = Path(env.get("HF_HUB_CACHE") or env.get("HUGGINGFACE_HUB_CACHE") or hf_home / "hub")
    candidates = []
    for base in (home / ".cache/easel-models", home / "models", root / "models"):
        if base.is_dir():
            candidates.extend((path, path.name, "local") for path in base.glob("*whisper*") if path.is_dir())
    if hub.is_dir():
        for repo in hub.glob("models--*whisper*"):
            if not repo.is_dir():
                continue
            name = repo.name.rsplit("--", 1)[-1]
            snapshots = repo / "snapshots"
            # Prefer the cached main revision; fall back to complete snapshots.
            ref = repo / "refs/main"
            if ref.is_file():
                revision = ref.read_text(encoding="utf-8").strip()
                if revision and all(c in "0123456789abcdef" for c in revision.lower()):
                    candidates.append((snapshots / revision, name, "huggingface-cache"))
            if snapshots.is_dir():
                candidates.extend((path, name, "huggingface-cache") for path in sorted(snapshots.iterdir()) if path.is_dir())
    # CPU-friendly small is preferred over tiny when both are already installed.
    preferred = ("small", "medium", "large-v3", "large-v2", "large", "base", "tiny")
    def rank(item):
        name = item[1].removeprefix("faster-whisper-")
        return preferred.index(name) if name in preferred else len(preferred)
    seen = set()
    for path, name, source in sorted(candidates, key=rank):
        path = path.resolve()
        if path not in seen and complete_model(path):
            return {"path": str(path), "name": name, "source": source}
        seen.add(path)
    return None
