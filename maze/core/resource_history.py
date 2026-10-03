from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict

from maze.core.scheduler.result_summary import to_json_safe
from maze.core.workflow.dynamic_store import default_workspace_dir
from maze.core.workflow.resources import normalize_resources
from maze.core.resource_policy import (
    GPU_PEAK_KEYS,
    ResourceContext,
    observed_peak_bytes,
    recommend_resources,
)


SCHEMA_VERSION = 3
MAX_PROFILES = 1024
MAX_OBSERVATION_IDS = 8192


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
            "profiles": {},
            "observation_ids": [],
        }

    def load(self) -> Dict[str, Any]:
        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    if int(data.get("schema_version", 1)) > SCHEMA_VERSION:
                        raise ValueError(
                            "Resource history was written by a newer Maze version"
                        )
                    data["schema_version"] = SCHEMA_VERSION
                    data.setdefault("models", {})
                    data.setdefault("tasks", {})
                    data.setdefault("recent_observations", [])
                    # Old aggregates remain diagnostic; only scoped profiles feed decisions.
                    data.setdefault("profiles", {})
                    data.setdefault("observation_ids", [])
                    return data
                raise ValueError("Resource history must contain an object")
            except FileNotFoundError:
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
        context: ResourceContext | None = None,
    ) -> Dict[str, Any]:
        if context is None:
            return normalize_resources(resources)
        return recommend_resources(
            context, resources or {}, self.lookup(context), model_anchor=model_anchor
        ).effective

    def lookup(self, context: ResourceContext) -> Dict[str, Any]:
        with self._lock:
            profiles = self.load()["profiles"]
            result = dict(profiles.get(context.key, {}))
            if context.scope == "model_instance" and context.features:
                base = profiles.get(replace(context, features=()).key, {})
                for key in ("success_peak_bytes", "oom_lower_bound_mib"):
                    result[key] = max(int(result.get(key, 0)), int(base.get(key, 0)))
                result["success_samples"] = int(result.get("success_samples", 0)) + int(
                    base.get("success_samples", 0)
                )
            return result

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
        context: ResourceContext | None = None,
        observation_id: str | None = None,
        required_gpu_mem_mib: int = 0,
    ) -> Dict[str, Any]:
        requested_resources = normalize_resources(requested_resources)
        metrics = dict(metrics or {})
        error = error if isinstance(error, dict) else None
        peak_bytes = _peak_gpu_bytes(metrics)
        success = status == "succeeded"
        error_type = error.get("error_type") if error else None
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
            "error": to_json_safe(error),
            "error_type": error_type,
            "peak_gpu_memory_bytes": peak_bytes,
            "observation_id": observation_id,
            "resource_context": context.to_dict() if context else None,
        }

        with self._lock:
            data = self.load()
            if observation_id and observation_id in data["observation_ids"]:
                return to_json_safe(observation)
            self._update_record(data.setdefault("tasks", {}), str(task_name or task_id), observation)
            model_key = model_anchor_key(model_anchor)
            if model_key:
                self._update_record(data.setdefault("models", {}), model_key, observation)
            if context:
                profile = data["profiles"].setdefault(
                    context.key,
                    {
                        "context": context.to_dict(),
                        "success_samples": 0,
                        "success_peak_bytes": 0,
                        "oom_lower_bound_mib": 0,
                    },
                )
                peak = observed_peak_bytes(metrics)
                # A routed caller's allocator statistics belong to its worker, not the instance.
                measurement_scope = metrics.get("memory_scope", "task")
                if success and peak is not None and measurement_scope == context.scope:
                    profile["success_samples"] += 1
                    profile["success_peak_bytes"] = max(
                        profile["success_peak_bytes"], peak
                    )
                if (
                    not success
                    and required_gpu_mem_mib
                    and (
                        context.scope == "task"
                        or context.features
                        or (error or {}).get("memory_failure", {}).get("phase")
                        == "load"
                    )
                ):
                    profile["oom_lower_bound_mib"] = max(
                        profile["oom_lower_bound_mib"], required_gpu_mem_mib
                    )
                profile["last_observed_time"] = observation["recorded_time"]
                if len(data["profiles"]) > MAX_PROFILES:
                    oldest = min(
                        data["profiles"],
                        key=lambda k: data["profiles"][k].get("last_observed_time", 0),
                    )
                    del data["profiles"][oldest]
            if observation_id:
                data["observation_ids"].append(observation_id)
                del data["observation_ids"][:-MAX_OBSERVATION_IDS]
            recent = data.setdefault("recent_observations", [])
            recent.append(to_json_safe(observation))
            del recent[:-100]
            for bucket_name in ("models", "tasks"):
                bucket = data[bucket_name]
                if len(bucket) > MAX_PROFILES:
                    del bucket[
                        min(
                            bucket,
                            key=lambda key: bucket[key].get("last_observed_time", 0),
                        )
                    ]
            self._save(data)
        return to_json_safe(observation)

    def _update_record(self, bucket: Dict[str, Any], key: str, observation: Dict[str, Any]) -> None:
        record = bucket.setdefault(key, {
            "key": key,
            "runs": 0,
            "successes": 0,
            "failures": 0,
            "peak_gpu_memory_bytes": 0,
        })
        record["runs"] = int(record.get("runs") or 0) + 1
        if observation["success"]:
            record["successes"] = int(record.get("successes") or 0) + 1
        else:
            record["failures"] = int(record.get("failures") or 0) + 1

        peak_bytes = int(observation.get("peak_gpu_memory_bytes") or 0)
        if peak_bytes:
            record["peak_gpu_memory_bytes"] = max(int(record.get("peak_gpu_memory_bytes") or 0), peak_bytes)

        record["last_status"] = observation.get("status")
        record["last_error_type"] = observation.get("error_type")
        record["last_observed_time"] = observation.get("recorded_time")
