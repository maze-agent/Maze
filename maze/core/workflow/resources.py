from __future__ import annotations

import copy
import math
from typing import Any, Dict


TASK_KINDS = {"cpu", "gpu", "io"}

DEFAULT_TASK_KIND = "cpu"
DEFAULT_RESOURCES = {
    "cpu_num": 1,
    "gpu_mem": 0,
    "io_num": 0,
}
RESOURCE_HINT_KEYS = {
    "target_node_id",
    "node_id",
    "avoid_node_ids",
    "required_capability",
}


class ResourceSpecError(ValueError):
    """Raised when task kind or resource semantics are invalid."""


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def normalize_resources(resources: Dict[str, Any] | None) -> Dict[str, Any]:
    """Normalize public Maze resources to cpu_num/gpu_mem/io_num."""
    raw = dict(resources or {})
    normalized = dict(DEFAULT_RESOURCES)

    if "cpu_num" in raw and raw["cpu_num"] is not None:
        normalized["cpu_num"] = _int_value(raw["cpu_num"], DEFAULT_RESOURCES["cpu_num"])
    elif "cpu" in raw and raw["cpu"] is not None:
        normalized["cpu_num"] = _int_value(raw["cpu"], DEFAULT_RESOURCES["cpu_num"])

    if "io_num" in raw and raw["io_num"] is not None:
        normalized["io_num"] = _int_value(raw["io_num"], DEFAULT_RESOURCES["io_num"])

    if "gpu_mem" in raw and raw["gpu_mem"] is not None:
        normalized["gpu_mem"] = _int_value(raw["gpu_mem"], DEFAULT_RESOURCES["gpu_mem"])
    elif "gpu_memory_mb" in raw and raw["gpu_memory_mb"] is not None:
        normalized["gpu_mem"] = _int_value(raw["gpu_memory_mb"], DEFAULT_RESOURCES["gpu_mem"])

    for key, value in normalized.items():
        if value < 0:
            raise ResourceSpecError(f"resources.{key} must be non-negative")

    if normalized["cpu_num"] <= 0:
        normalized["cpu_num"] = 1

    for key in RESOURCE_HINT_KEYS:
        if key in raw and raw[key] not in (None, ""):
            normalized[key] = copy.deepcopy(raw[key])

    return normalized


def model_anchor_gpu_mem_mb(model_anchor: Dict[str, Any] | None) -> int:
    if not isinstance(model_anchor, dict):
        return 0
    for key in ("estimated_gpu_mem_mb", "gpu_mem", "gpu_memory_mb"):
        value = model_anchor.get(key)
        if value:
            return max(0, int(math.ceil(float(value))))
    bytes_value = model_anchor.get("estimated_weight_memory_bytes") or model_anchor.get("weight_bytes")
    if bytes_value:
        return max(0, int(math.ceil(float(bytes_value) * 1.2 / (1024 * 1024))))
    return 0


def apply_model_anchor_estimate(
    resources: Dict[str, Any] | None,
    model_anchor: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    normalized = normalize_resources(resources)
    estimate = model_anchor_gpu_mem_mb(model_anchor)
    if estimate:
        normalized["gpu_mem"] = max(normalized["gpu_mem"], estimate)
    return normalized


def normalize_task_kind(
    task_kind: Any = None,
    *,
    resources: Dict[str, Any] | None = None,
    model_anchor: Dict[str, Any] | None = None,
) -> str:
    if task_kind is not None:
        normalized = str(task_kind).strip().lower()
        if normalized not in TASK_KINDS:
            allowed = ", ".join(sorted(TASK_KINDS))
            raise ResourceSpecError(f"task_kind must be one of: {allowed}")
        return normalized

    resources = resources or {}
    if _int_value(resources.get("gpu_mem")) > 0:
        return "gpu"
    if isinstance(model_anchor, dict) and model_anchor.get("local_model"):
        return "gpu"
    return DEFAULT_TASK_KIND


def normalize_task_semantics(
    *,
    task_kind: Any = None,
    resources: Dict[str, Any] | None = None,
    model_anchor: Dict[str, Any] | None = None,
) -> tuple[str, Dict[str, Any]]:
    normalized_model_anchor = dict(model_anchor or {}) or None
    normalized_resources = apply_model_anchor_estimate(resources, normalized_model_anchor)
    normalized_kind = normalize_task_kind(
        task_kind,
        resources=normalized_resources,
        model_anchor=normalized_model_anchor,
    )
    validate_task_resources(normalized_kind, normalized_resources, normalized_model_anchor)
    return normalized_kind, normalized_resources


def validate_task_resources(
    task_kind: str,
    resources: Dict[str, Any],
    model_anchor: Dict[str, Any] | None = None,
) -> None:
    if task_kind not in TASK_KINDS:
        allowed = ", ".join(sorted(TASK_KINDS))
        raise ResourceSpecError(f"task_kind must be one of: {allowed}")

    has_model_anchor = isinstance(model_anchor, dict) and bool(model_anchor.get("local_model"))
    gpu_mem = _int_value((resources or {}).get("gpu_mem"))
    if task_kind in {"cpu", "io"}:
        if gpu_mem > 0:
            raise ResourceSpecError(f"{task_kind} tasks must not request gpu_mem")
        if has_model_anchor:
            raise ResourceSpecError(f"{task_kind} tasks must not include model_anchor.local_model")


def require_schedulable_resources(
    task_kind: str,
    resources: Dict[str, Any],
    model_anchor: Dict[str, Any] | None = None,
) -> None:
    """Validate resource semantics after model/resource-history estimates are applied."""
    validate_task_resources(task_kind, resources, model_anchor)
    if task_kind == "gpu" and _int_value((resources or {}).get("gpu_mem")) <= 0:
        raise ResourceSpecError(
            "gpu tasks must declare resources.gpu_mem or use a model/resource-history estimate before scheduling"
        )


def to_internal_scheduler_resources(
    resources: Dict[str, Any] | None,
    *,
    task_kind: str | None = None,
    model_anchor: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Adapt public Maze resources to the current scheduler/Ray internals."""
    normalized = normalize_resources(resources)
    kind = normalize_task_kind(task_kind, resources=normalized, model_anchor=model_anchor)
    internal = copy.deepcopy(normalized)
    internal["cpu"] = normalized["cpu_num"]
    internal["cpu_mem"] = 0
    internal["gpu"] = 1 if normalized["gpu_mem"] > 0 else 0
    internal["gpu_mem"] = normalized["gpu_mem"]
    return internal
