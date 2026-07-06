from __future__ import annotations

import copy
from typing import Any, Dict

from maze.core.scheduler.error import looks_like_oom, utc_timestamp
from maze.core.scheduler.result_summary import to_json_safe


def init_fault_tolerance_trace() -> Dict[str, Any]:
    return {
        "enabled": True,
        "status": "idle",
        "attempts": [],
    }


def fault_tolerance_trace(task: Any) -> Dict[str, Any]:
    trace = getattr(task, "fault_tolerance", None)
    if not isinstance(trace, dict):
        trace = init_fault_tolerance_trace()
        setattr(task, "fault_tolerance", trace)
    trace.setdefault("enabled", True)
    trace.setdefault("status", "idle")
    trace.setdefault("attempts", [])
    return trace


def trace_snapshot(task: Any) -> Dict[str, Any]:
    return to_json_safe(copy.deepcopy(fault_tolerance_trace(task)))


def diagnose_failure(error: Dict[str, Any], task: Any) -> Dict[str, Any]:
    error_type = str(error.get("error_type") or "unknown")
    message = str(error.get("message") or "")
    diagnosis = {
        "category": "unknown",
        "reason": error_type,
        "recoverable": bool(error.get("retryable", False)),
        "details": {},
    }

    if error_type == "resource_insufficient":
        diagnosis.update({
            "category": "resource",
            "reason": "gpu_oom" if looks_like_oom(error) or looks_like_oom(message) else "resource_insufficient",
            "recoverable": True,
        })
        return diagnosis

    if error_type in {"node_lost", "resource_unavailable"}:
        recoverability = _node_recoverability(task, error_type)
        diagnosis.update({
            "category": "worker",
            "reason": "worker_lost" if error_type == "node_lost" else "worker_resource_unavailable",
            "recoverable": recoverability["recoverable"],
            "details": recoverability,
        })
        return diagnosis

    if error_type == "invocation_error":
        reason = (
            error.get("invocation_reason")
            or (error.get("details") or {}).get("reason")
            or _invocation_reason(message)
        )
        correction_count = int(getattr(task, "invocation_correction_count", 0) or 0)
        diagnosis.update({
            "category": "invocation",
            "reason": reason,
            "recoverable": correction_count < 1,
            "details": {
                "correction_count": correction_count,
                "max_corrections": 1,
            },
        })
        return diagnosis

    return diagnosis


def apply_repair_action(task: Any, error: Dict[str, Any], diagnosis: Dict[str, Any]) -> Dict[str, Any]:
    if not diagnosis.get("recoverable"):
        return {
            "type": "none",
            "applied": False,
            "reason": diagnosis.get("reason") or "not_recoverable",
        }

    category = diagnosis.get("category")
    if category == "resource":
        return _apply_resource_reanchor(task)
    if category == "worker":
        return _apply_node_recovery(task, error, diagnosis)
    if category == "invocation":
        return _apply_invocation_correction(task, error, diagnosis)

    return {
        "type": "retry",
        "applied": True,
        "reason": "retryable_failure",
    }


def record_retry_decision(
    task: Any,
    error: Dict[str, Any],
    diagnosis: Dict[str, Any],
    repair_action: Dict[str, Any],
    *,
    retry_scheduled: bool,
    next_attempt: int | None = None,
) -> Dict[str, Any]:
    trace = fault_tolerance_trace(task)
    if retry_scheduled:
        _close_open_attempt(trace, {
            "status": "failed",
            "attempt": getattr(task, "attempt", None),
            "timestamp": utc_timestamp(),
            "message": "Retry attempt failed before recovery completed.",
        })

    entry = {
        "attempt": error.get("attempt") or getattr(task, "attempt", None),
        "failure": _failure_snapshot(error),
        "diagnosis": to_json_safe(diagnosis),
        "repair_action": to_json_safe(repair_action),
        "retry": {
            "scheduled": bool(retry_scheduled),
            "next_attempt": next_attempt if retry_scheduled else None,
            "backoff_seconds": getattr(task, "retry_backoff_seconds", None) if retry_scheduled else None,
        },
        "outcome": None,
        "timestamp": utc_timestamp(),
    }
    if not retry_scheduled:
        entry["outcome"] = {
            "status": "failed",
            "attempt": getattr(task, "attempt", None),
            "timestamp": utc_timestamp(),
        }

    trace["attempts"].append(entry)
    trace["status"] = "retrying" if retry_scheduled else "failed"
    return trace_snapshot(task)


def record_success(task: Any) -> Dict[str, Any]:
    trace = fault_tolerance_trace(task)
    _close_open_attempt(trace, {
        "status": "succeeded",
        "attempt": getattr(task, "attempt", None),
        "timestamp": utc_timestamp(),
    })
    trace["status"] = "recovered" if trace.get("attempts") else "succeeded"
    return trace_snapshot(task)


def record_final_failure(task: Any, error: Dict[str, Any] | None = None) -> Dict[str, Any]:
    trace = fault_tolerance_trace(task)
    _close_open_attempt(trace, {
        "status": "failed",
        "attempt": getattr(task, "attempt", None),
        "timestamp": utc_timestamp(),
        "error": _failure_snapshot(error or {}),
    })
    trace["status"] = "failed"
    return trace_snapshot(task)


def _failure_snapshot(error: Dict[str, Any]) -> Dict[str, Any]:
    return to_json_safe({
        "error_type": error.get("error_type"),
        "message": error.get("message"),
        "origin": error.get("origin"),
        "node_id": error.get("node_id"),
        "node_ip": error.get("node_ip"),
        "attempt": error.get("attempt"),
        "timestamp": error.get("timestamp"),
        "details": error.get("details"),
    })


def _close_open_attempt(trace: Dict[str, Any], outcome: Dict[str, Any]) -> None:
    for entry in reversed(trace.get("attempts") or []):
        if entry.get("outcome") is None:
            entry["outcome"] = to_json_safe(outcome)
            return


def _apply_resource_reanchor(task: Any) -> Dict[str, Any]:
    resources = getattr(task, "resources", None)
    if not isinstance(resources, dict):
        return {"type": "resource_reanchor", "applied": False, "reason": "missing_resources"}

    current = _int_resource(resources.get("gpu_mem"))
    if current <= 0:
        return {"type": "resource_reanchor", "applied": False, "reason": "missing_gpu_mem_anchor"}

    updated = int(current * 1.25)
    resources["gpu_mem"] = updated
    return {
        "type": "resource_reanchor",
        "applied": True,
        "adjusted_resources": copy.deepcopy(resources),
        "changes": {
            "gpu_mem": {"from": current, "to": updated},
        },
    }


def _apply_node_recovery(task: Any, error: Dict[str, Any], diagnosis: Dict[str, Any]) -> Dict[str, Any]:
    resources = getattr(task, "resources", None)
    if not isinstance(resources, dict):
        return {"type": "node_reselect", "applied": False, "reason": "missing_resources"}

    failed_node_id = error.get("node_id")
    selected_node = getattr(task, "selected_node", None)
    if not failed_node_id and selected_node is not None:
        failed_node_id = getattr(selected_node, "node_id", None)
    if not failed_node_id:
        return {"type": "node_reselect", "applied": False, "reason": "missing_failed_node"}

    avoid_node_ids = list(resources.get("avoid_node_ids") or [])
    if failed_node_id not in avoid_node_ids:
        avoid_node_ids.append(failed_node_id)
    resources["avoid_node_ids"] = avoid_node_ids
    return {
        "type": "node_reselect",
        "applied": True,
        "adjusted_resources": copy.deepcopy(resources),
        "avoid_node_id": failed_node_id,
        "artifact_recoverable": diagnosis.get("details", {}).get("artifact_recoverable"),
    }


def _apply_invocation_correction(task: Any, error: Dict[str, Any], diagnosis: Dict[str, Any]) -> Dict[str, Any]:
    correction_count = int(getattr(task, "invocation_correction_count", 0) or 0)
    if correction_count >= 1:
        return {"type": "invocation_correction", "applied": False, "reason": "correction_limit_reached"}
    if not _has_task_input(task, "invocation_repair"):
        return {"type": "invocation_correction", "applied": False, "reason": "no_invocation_context"}

    reason = diagnosis.get("reason") or "invocation_error"
    details = error.get("details") or {}
    correction_payload = {
        "reason": reason,
        "message": error.get("message"),
        "details": details,
    }
    _set_task_input(task, "invocation_repair", correction_payload, data_type="dict")

    changes = {"invocation_repair": {"to": correction_payload}}
    if reason == "max_tokens_insufficient":
        current = _int_resource(details.get("max_tokens"), 512)
        updated = max(current + 256, current * 2)
        if _has_task_input(task, "max_tokens_override"):
            _set_task_input(task, "max_tokens_override", updated, data_type="int")
            changes["max_tokens_override"] = {"from": current, "to": updated}

    setattr(task, "invocation_correction_count", correction_count + 1)
    return {
        "type": "invocation_correction",
        "applied": True,
        "strategy": reason,
        "changes": changes,
    }


def _set_task_input(task: Any, key: str, value: Any, *, data_type: str = "any") -> None:
    task_input = getattr(task, "task_input", None)
    if not isinstance(task_input, dict):
        return
    input_params = task_input.setdefault("input_params", {})
    for param in input_params.values():
        if isinstance(param, dict) and param.get("key") == key:
            param.update({
                "input_schema": "from_user",
                "data_type": data_type,
                "value": value,
                "has_value": True,
            })
            return

    numeric_keys = []
    for existing_key in input_params:
        try:
            numeric_keys.append(int(existing_key))
        except (TypeError, ValueError):
            pass
    next_key = str(max(numeric_keys, default=0) + 1)
    input_params[next_key] = {
        "key": key,
        "input_schema": "from_user",
        "data_type": data_type,
        "value": value,
        "has_value": True,
    }


def _has_task_input(task: Any, key: str) -> bool:
    task_input = getattr(task, "task_input", None)
    if not isinstance(task_input, dict):
        return False
    input_params = task_input.get("input_params") or {}
    return any(
        isinstance(param, dict) and param.get("key") == key
        for param in input_params.values()
    )


def _node_recoverability(task: Any, error_type: str) -> Dict[str, Any]:
    resources = getattr(task, "resources", None) or {}
    selected_node = getattr(task, "selected_node", None)
    failed_node_id = getattr(selected_node, "node_id", None) if selected_node is not None else None
    target_node_id = resources.get("target_node_id") or resources.get("node_id")
    file_context = getattr(task, "file_context", None)

    if target_node_id and failed_node_id and target_node_id == failed_node_id:
        return {
            "recoverable": False,
            "reason": "task_pinned_to_failed_node",
            "artifact_recoverable": False,
            "target_node_id": target_node_id,
        }

    if isinstance(file_context, dict) and file_context.get("enabled"):
        artifact_recoverable = bool(file_context.get("artifact_store"))
        return {
            "recoverable": artifact_recoverable,
            "reason": "artifact_store_available" if artifact_recoverable else "file_context_not_portable_without_artifact_store",
            "artifact_recoverable": artifact_recoverable,
        }

    return {
        "recoverable": error_type == "node_lost",
        "reason": "inputs_replayable_from_scheduler_state",
        "artifact_recoverable": True,
    }


def _invocation_reason(message: str) -> str:
    text = str(message or "").lower()
    if "max_tokens" in text or "finish_reason" in text or "length" in text:
        return "max_tokens_insufficient"
    if "json" in text or "expecting value" in text or "malformed" in text:
        return "json_parse_failed"
    if "schema" in text or "validation" in text or "required field" in text:
        return "schema_mismatch"
    if "missing" in text and ("arg" in text or "tool" in text):
        return "tool_invocation_args_missing"
    return "invocation_error"


def _int_resource(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
