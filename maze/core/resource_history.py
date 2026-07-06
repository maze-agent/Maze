from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict

from maze.core.scheduler.result_summary import to_json_safe
from maze.core.workflow.dynamic_store import default_workspace_dir
from maze.core.workflow.resources import normalize_resources


SCHEMA_VERSION = 1
GPU_PEAK_KEYS = (
    "gpu_memory_peak_reserved_bytes",
    "gpu_memory_peak_allocated_bytes",
    "peak_cuda_reserved_bytes",
    "peak_cuda_allocated_bytes",
)


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _peak_gpu_bytes(metrics: Dict[str, Any] | None) -> int:
    metrics = metrics or {}
    return max((_int_value(metrics.get(key)) for key in GPU_PEAK_KEYS), default=0)


def model_anchor_key(model_anchor: Dict[str, Any] | None) -> str | None:
    if not isinstance(model_anchor, dict):
        return None
    local_model = model_anchor.get("local_model") or model_anchor.get("model")
    if not local_model:
        return None
    backend = model_anchor.get("backend") or "transformers"
    scope = model_anchor.get("model_scope") or "local"
    return f"{backend}:{scope}:{local_model}"


class ResourceHistoryStore:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        root = default_workspace_dir()
        self.path = Path(path).expanduser().resolve() if path else root / "resource_history.json"
        self._lock = threading.RLock()

    def _empty(self) -> Dict[str, Any]:
        return {
            "schema": "maze_resource_history",
            "schema_version": SCHEMA_VERSION,
            "updated_time": time.time(),
            "models": {},
            "tasks": {},
            "recent_observations": [],
        }

    def load(self) -> Dict[str, Any]:
        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    data.setdefault("models", {})
                    data.setdefault("tasks", {})
                    data.setdefault("recent_observations", [])
                    return data
            except Exception:
                pass
            return self._empty()

    def _save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data["updated_time"] = time.time()
        tmp_path = self.path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(to_json_safe(data), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, self.path)

    def apply(
        self,
        resources: Dict[str, Any] | None,
        model_anchor: Dict[str, Any] | None = None,
        task_name: str | None = None,
    ) -> Dict[str, Any]:
        next_resources = normalize_resources(resources)

        data = self.load()
        records = []
        model_key = model_anchor_key(model_anchor)
        if model_key:
            records.append(data.get("models", {}).get(model_key) or {})
        if task_name:
            records.append(data.get("tasks", {}).get(str(task_name)) or {})

        suggested_gpu_mem = max(_int_value(record.get("recommended_gpu_mem_mb")) for record in records) if records else 0
        if suggested_gpu_mem:
            next_resources["gpu_mem"] = max(next_resources["gpu_mem"], suggested_gpu_mem)
        return next_resources

    def record(
        self,
        *,
        run_id: str,
        task_id: str,
        task_name: str | None,
        status: str,
        requested_resources: Dict[str, Any] | None,
        model_anchor: Dict[str, Any] | None = None,
        metrics: Dict[str, Any] | None = None,
        error: Dict[str, Any] | None = None,
        selected_node: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        requested_resources = normalize_resources(requested_resources)
        metrics = dict(metrics or {})
        error = error if isinstance(error, dict) else None
        peak_bytes = _peak_gpu_bytes(metrics)
        success = status == "succeeded"
        error_type = error.get("error_type") if error else None
        gpu_mem_requested = _int_value(requested_resources.get("gpu_mem"))

        observation = {
            "run_id": run_id,
            "task_id": task_id,
            "task_name": task_name,
            "status": status,
            "success": success,
            "recorded_time": time.time(),
            "requested_resources": requested_resources,
            "model_anchor": model_anchor,
            "selected_node": selected_node,
            "metrics": metrics,
            "error_type": error_type,
            "peak_gpu_memory_bytes": peak_bytes,
        }

        with self._lock:
            data = self.load()
            self._update_record(data.setdefault("tasks", {}), str(task_name or task_id), observation)
            model_key = model_anchor_key(model_anchor)
            if model_key:
                self._update_record(data.setdefault("models", {}), model_key, observation)
            recent = data.setdefault("recent_observations", [])
            recent.append(to_json_safe(observation))
            del recent[:-100]
            self._save(data)
        return to_json_safe(observation)

    def _update_record(self, bucket: Dict[str, Any], key: str, observation: Dict[str, Any]) -> None:
        record = bucket.setdefault(key, {
            "key": key,
            "runs": 0,
            "successes": 0,
            "failures": 0,
            "peak_gpu_memory_bytes": 0,
            "recommended_gpu_mem_mb": 0,
        })
        record["runs"] = int(record.get("runs") or 0) + 1
        if observation["success"]:
            record["successes"] = int(record.get("successes") or 0) + 1
        else:
            record["failures"] = int(record.get("failures") or 0) + 1

        peak_bytes = int(observation.get("peak_gpu_memory_bytes") or 0)
        if peak_bytes:
            record["peak_gpu_memory_bytes"] = max(int(record.get("peak_gpu_memory_bytes") or 0), peak_bytes)
            record["recommended_gpu_mem_mb"] = max(
                int(record.get("recommended_gpu_mem_mb") or 0),
                int(math.ceil(peak_bytes * 1.15 / (1024 * 1024))),
            )

        requested = observation.get("requested_resources") or {}
        if observation.get("error_type") == "resource_insufficient" and requested.get("gpu_mem"):
            record["recommended_gpu_mem_mb"] = max(
                int(record.get("recommended_gpu_mem_mb") or 0),
                int(math.ceil(float(requested["gpu_mem"]) * 1.25)),
            )

        record["last_status"] = observation.get("status")
        record["last_error_type"] = observation.get("error_type")
        record["last_observed_time"] = observation.get("recorded_time")
