from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG_PATH = PROJECT_ROOT / ".maze_runtime.json"
DEFAULT_MODEL_DIR = PROJECT_ROOT / "model_cache"


def model_dir() -> Path:
    config: Dict[str, Any] = {}
    with contextlib.suppress(Exception):
        config = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    return Path(config.get("model_dir") or os.environ.get("MAZE_MODEL_DIR") or DEFAULT_MODEL_DIR).expanduser().resolve()


def scan_local_model_refs(base_dir: str | os.PathLike[str] | None = None) -> List[Dict[str, Any]]:
    root = Path(base_dir).expanduser().resolve() if base_dir else model_dir()
    if not root.exists():
        return []

    models = []
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
            "backend": "transformers",
            "model_type": model_type,
        })
    return models
