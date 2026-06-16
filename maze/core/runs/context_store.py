"""Minimal context store (C6).

A small namespaced key/value store persisted to the workspace so agents /
workflows can keep lightweight context across tasks and runs (e.g. memory,
intermediate notes). This is intentionally simple: JSON values, file-backed,
namespaced. It is not a full memory/vector backend.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from maze.core.workflow.dynamic_store import default_workspace_dir


_SAFE = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")


class ContextStore:
    def __init__(self, workspace_dir: str | os.PathLike[str] | None = None):
        base = Path(workspace_dir).expanduser().resolve() if workspace_dir else default_workspace_dir()
        self.root = base / "context"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _ns_dir(self, namespace: str) -> Path:
        if not _SAFE.fullmatch(namespace or ""):
            raise ValueError(f"invalid context namespace: {namespace!r}")
        path = self.root / namespace
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _key_path(self, namespace: str, key: str) -> Path:
        if not _SAFE.fullmatch(key or ""):
            raise ValueError(f"invalid context key: {key!r}")
        return self._ns_dir(namespace) / f"{key}.json"

    def set(self, namespace: str, key: str, value: Any) -> Dict[str, Any]:
        record = {"namespace": namespace, "key": key, "value": value, "updated_time": time.time()}
        path = self._key_path(namespace, key)
        tmp = path.with_suffix(".json.tmp")
        with self._lock:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False)
            os.replace(tmp, path)
        return record

    def get(self, namespace: str, key: str) -> Dict[str, Any] | None:
        path = self._key_path(namespace, key)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def list(self, namespace: str) -> List[Dict[str, Any]]:
        path = self._ns_dir(namespace)
        records = []
        for item in sorted(path.glob("*.json")):
            try:
                with item.open("r", encoding="utf-8") as handle:
                    records.append(json.load(handle))
            except Exception:
                continue
        return records

    def delete(self, namespace: str, key: str) -> bool:
        path = self._key_path(namespace, key)
        if path.exists():
            path.unlink()
            return True
        return False
