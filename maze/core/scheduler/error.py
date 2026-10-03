from __future__ import annotations

import traceback as traceback_module
import re
from datetime import datetime, timezone
from typing import Any, Dict


TASK_ERROR_ENVELOPE = "__maze_task_error_envelope__"

ERROR_RETRYABLE_DEFAULTS = {
    "user_code": False,
    "resource_unavailable": True,
    "resource_insufficient": True,
    "node_lost": True,
    "artifact_error": True,
    "serialization_error": False,
    "missing_model": False,
    "model_deployment_failed": False,
    "timeout": False,
    "scheduler_error": False,
    "cancelled": False,
    "unknown": False,
    "resource_capacity_exceeded": False,
    "resource_limit_exceeded": False,
    "resource_cleanup_failed": False,
}


def memory_failure_from_message(
    message: str, *, scope: str = "task"
) -> Dict[str, Any] | None:
    """Parse backend text once at the error boundary, never in allocation policy."""
    if not looks_like_oom(message):
        return None
    text = str(message)
    cuda = any(k in text.lower() for k in ("cuda", "gpu", "cublas", "hip"))
    failure: Dict[str, Any] = {
        "scope": scope,
        "device_type": "cuda" if cuda else "unknown",
    }
    patterns = {
        "attempted_allocation_bytes": r"(?:tried to allocate|allocating)\s+([\d.]+)\s*([KMGT]?i?B)",
        "allocated_bytes": r"([\d.]+)\s*([KMGT]?i?B)\s+is allocated by PyTorch",
        "capacity_bytes": r"total capacity of\s+([\d.]+)\s*([KMGT]?i?B)",
    }
    units = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
        "TIB": 1024**4,
    }
    for name, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            failure[name] = int(float(match[1]) * units[match[2].upper()])
    return failure


class ModelInferenceError(RuntimeError):
    def __init__(self, message: str, memory_failure: Dict[str, Any]):
        super().__init__(message)
        self.memory_failure = memory_failure


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_error_envelope(
    error_type: str,
    message: str,
    *,
    retryable: bool | None = None,
    traceback: str | None = None,
    origin: str = "scheduler",
    node_id: str | None = None,
    node_ip: str | None = None,
    attempt: int | None = None,
    timestamp: str | None = None,
    exception_type: str | None = None,
) -> Dict[str, Any]:
    if retryable is None:
        retryable = ERROR_RETRYABLE_DEFAULTS.get(error_type, False)

    envelope = {
        "error_type": error_type,
        "message": str(message),
        "retryable": bool(retryable),
        "traceback": traceback,
        "origin": origin,
        "node_id": node_id,
        "node_ip": node_ip,
        "attempt": attempt,
        "timestamp": timestamp or utc_timestamp(),
    }
    if exception_type:
        envelope["exception_type"] = exception_type
    return envelope


def exception_to_error_envelope(
    error_type: str,
    exc: BaseException,
    *,
    retryable: bool | None = None,
    origin: str = "scheduler",
    node_id: str | None = None,
    node_ip: str | None = None,
    attempt: int | None = None,
) -> Dict[str, Any]:
    traceback_text = "".join(
        traceback_module.format_exception(type(exc), exc, exc.__traceback__)
    )
    result = make_error_envelope(
        error_type,
        str(exc),
        retryable=retryable,
        traceback=traceback_text,
        origin=origin,
        node_id=node_id,
        node_ip=node_ip,
        attempt=attempt,
        exception_type=f"{type(exc).__module__}.{type(exc).__name__}",
    )
    failure = getattr(exc, "memory_failure", None) or memory_failure_from_message(
        str(exc)
    )
    if failure:
        result["memory_failure"] = dict(failure)
    return result


def looks_like_oom(value: Any) -> bool:
    text = str(value or "").lower()
    if "out of memory" in text and any(
        device in text for device in ("cuda", "gpu", "hip")
    ):
        return True
    return any(
        marker in text
        for marker in (
            "cuda out of memory",
            "outofmemoryerror",
            "cublas_status_alloc_failed",
            "hip out of memory",
            "memory allocation failed",
        )
    )


def task_error_result(error: Dict[str, Any]) -> Dict[str, Any]:
    return {
        TASK_ERROR_ENVELOPE: True,
        "error": error,
    }


def is_task_error_result(value: Any) -> bool:
    return isinstance(value, dict) and value.get(TASK_ERROR_ENVELOPE) is True


def enrich_error_for_task(error: Dict[str, Any], task: Any) -> Dict[str, Any]:
    selected_node = getattr(task, "selected_node", None)
    enriched = dict(error)
    if not enriched.get("memory_failure") and not getattr(task, "model_route", None):
        failure = memory_failure_from_message(str(enriched.get("message", "")))
        if failure:
            enriched["memory_failure"] = failure
    enriched.setdefault("timestamp", utc_timestamp())
    enriched["attempt"] = enriched.get("attempt") or getattr(task, "attempt", None)
    if selected_node is not None:
        enriched["node_id"] = enriched.get("node_id") or getattr(selected_node, "node_id", None)
        enriched["node_ip"] = enriched.get("node_ip") or getattr(selected_node, "node_ip", None)
    return enriched
