import json
from collections.abc import Mapping
from itertools import islice
from typing import Any


SUMMARY_MARKER = "__maze_summary__"


def _safe_repr(value: Any, max_length: int = 200) -> str:
    try:
        rendered = repr(value)
    except Exception:
        rendered = f"<unrepresentable {type(value).__name__}>"

    if len(rendered) > max_length:
        return rendered[: max_length - 3] + "..."
    return rendered


def _is_json_serializable(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError, OverflowError):
        return False


def _type_name(value: Any) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__name__}"


def _summarize_special_value(value: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        SUMMARY_MARKER: True,
        "type": _type_name(value),
        "repr": _safe_repr(value),
    }

    shape = getattr(value, "shape", None)
    if shape is not None:
        try:
            summary["shape"] = list(shape)
        except TypeError:
            summary["shape"] = _safe_repr(shape)

    dtype = getattr(value, "dtype", None)
    if dtype is not None:
        summary["dtype"] = str(dtype)

    columns = getattr(value, "columns", None)
    if columns is not None:
        try:
            summary["columns"] = [str(column) for column in list(columns)[:20]]
        except Exception:
            summary["columns"] = _safe_repr(columns)

    try:
        summary["length"] = len(value)
    except Exception:
        pass

    return summary


def to_json_safe(value: Any, *, max_depth: int = 6, _depth: int = 0) -> Any:
    """Return a JSON-safe representation for execution events.

    The scheduler keeps the original Python objects internally for downstream
    task inputs. This function is only for messages that leave the scheduler
    process through JSON/WS/API surfaces.
    """
    if _is_json_serializable(value):
        return value

    if _depth >= max_depth:
        return _summarize_special_value(value)

    if isinstance(value, Mapping):
        return {
            str(key): to_json_safe(item, max_depth=max_depth, _depth=_depth + 1)
            for key, item in islice(value.items(), 100)
        }

    if isinstance(value, tuple):
        return [
            to_json_safe(item, max_depth=max_depth, _depth=_depth + 1)
            for item in value[:100]
        ]

    if isinstance(value, list):
        return [
            to_json_safe(item, max_depth=max_depth, _depth=_depth + 1)
            for item in value[:100]
        ]

    if isinstance(value, set):
        return {
            SUMMARY_MARKER: True,
            "type": _type_name(value),
            "length": len(value),
            "sample": [
                to_json_safe(item, max_depth=max_depth, _depth=_depth + 1)
                for item in islice(value, 10)
            ],
        }

    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            SUMMARY_MARKER: True,
            "type": _type_name(value),
            "length": len(value),
            "repr": _safe_repr(value),
        }

    return _summarize_special_value(value)


def summarize_task_result(result: Any) -> Any:
    """Summarize a task result for JSON event streams."""
    return to_json_safe(result)
