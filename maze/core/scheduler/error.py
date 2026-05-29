from __future__ import annotations

import traceback as traceback_module
from datetime import datetime, timezone
from typing import Any, Dict


TASK_ERROR_ENVELOPE = "__maze_task_error_envelope__"

ERROR_RETRYABLE_DEFAULTS = {
    "user_code": False,
    "resource_unavailable": True,
    "node_lost": True,
    "artifact_error": True,
    "timeout": False,
    "scheduler_error": False,
    "cancelled": False,
    "unknown": False,
}


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
    return make_error_envelope(
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
    enriched.setdefault("timestamp", utc_timestamp())
    enriched["attempt"] = enriched.get("attempt") or getattr(task, "attempt", None)
    if selected_node is not None:
        enriched["node_id"] = enriched.get("node_id") or getattr(selected_node, "node_id", None)
        enriched["node_ip"] = enriched.get("node_ip") or getattr(selected_node, "node_ip", None)
    return enriched
