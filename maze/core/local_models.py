from __future__ import annotations

import contextlib
import importlib.util
import json
import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG_PATH = PROJECT_ROOT / ".maze_runtime.json"
DEFAULT_MODEL_DIR = PROJECT_ROOT / "model_cache"
MODEL_BACKEND_MODULES = {
    "transformers": "transformers",
    "vllm": "vllm",
}


def enrich_model_anchor(anchor: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Attach checkpoint identity and precision before crossing the process boundary."""
    if not anchor:
        return None
    result = dict(anchor)
    name = str(result.get("local_model") or result.get("model") or "")
    path = Path(name).expanduser()
    if not path.is_dir():
        path = model_dir() / name
    config_path = path / "config.json"
    if config_path.is_file():
        raw = config_path.read_bytes()
        config = json.loads(raw)
        result.setdefault(
            "dtype", config.get("dtype") or config.get("torch_dtype") or ""
        )
        index = path / "model.safetensors.index.json"
        if index.is_file():
            result.setdefault(
                "estimated_weight_memory_bytes",
                json.loads(index.read_text()).get("metadata", {}).get("total_size", 0),
            )
        digest = hashlib.sha256(raw + (index.read_bytes() if index.is_file() else b""))
        metadata = path / ".cache/huggingface/download/config.json.metadata"
        if metadata.is_file():
            result.setdefault("revision", metadata.read_text().splitlines()[0])
        else:
            # Custom checkpoints with identical architecture can still contain different weights.
            for weight in sorted(path.glob("*.safetensors")):
                stat = weight.stat()
                digest.update(
                    f"{weight.name}:{stat.st_size}:{stat.st_mtime_ns}".encode()
                )
        result.setdefault("checkpoint_fingerprint", digest.hexdigest())
    return result


def model_dir() -> Path:
    config: Dict[str, Any] = {}
    with contextlib.suppress(Exception):
        config = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    return Path(config.get("model_dir") or os.environ.get("MAZE_MODEL_DIR") or DEFAULT_MODEL_DIR).expanduser().resolve()


def available_model_backends() -> List[str]:
    return [
        backend
        for backend, module in MODEL_BACKEND_MODULES.items()
        if importlib.util.find_spec(module) is not None
    ]


def local_model_supports_backend(model: Dict[str, Any], backend: str) -> bool:
    backends = model.get("backends")
    if isinstance(backends, list):
        return backend in backends
    return (model.get("backend") or "transformers") == backend


def scan_local_model_refs(base_dir: str | os.PathLike[str] | None = None) -> List[Dict[str, Any]]:
    root = Path(base_dir).expanduser().resolve() if base_dir else model_dir()
    if not root.exists():
        return []

    models = []
    backends = available_model_backends()
    for path in sorted(item for item in root.iterdir() if item.is_dir()):
        config_path = path / "config.json"
        if not config_path.is_file():
            continue
        model_type = ""
        with contextlib.suppress(Exception):
            model_type = str(json.loads(config_path.read_text(encoding="utf-8")).get("model_type") or "")
        models.append({
            "id": path.name,
            "name": path.name,
            "path": str(path),
            "backend": backends[0] if backends else None,
            "backends": backends,
            "model_type": model_type,
        })
    return models
