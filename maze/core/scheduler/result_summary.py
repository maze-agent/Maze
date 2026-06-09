import io
import json
import os
from collections.abc import Mapping
from itertools import islice
from pathlib import Path
from typing import Any

import cloudpickle

from maze.core.files.artifact_store import LocalCASArtifactStore, artifact_uri, sha256_bytes


SUMMARY_MARKER = "__maze_summary__"
DEFAULT_ARTIFACT_THRESHOLD_BYTES = 256 * 1024


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


def _artifact_threshold_bytes(artifact_threshold_bytes: int | None = None) -> int:
    if artifact_threshold_bytes is not None:
        return max(0, int(artifact_threshold_bytes))
    configured = os.environ.get("MAZE_RESULT_SUMMARY_ARTIFACT_THRESHOLD_BYTES")
    if configured:
        try:
            return max(0, int(configured))
        except ValueError:
            pass
    return DEFAULT_ARTIFACT_THRESHOLD_BYTES


def _artifact_record(
    *,
    data: bytes,
    path: str,
    mime: str,
    format: str,
    serializer: str,
    run_id: str | None,
    task_id: str | None,
) -> dict[str, Any]:
    sha = sha256_bytes(data)
    artifact_info = LocalCASArtifactStore().put_bytes(sha, data)
    record = {
        "path": path,
        "name": Path(path).name,
        "size": artifact_info["size"],
        "sha256": sha,
        "artifact_id": artifact_info["artifact_id"],
        "storage_uri": artifact_uri(sha),
        "mime": mime,
        "kind": "task_result_summary",
        "format": format,
        "serializer": serializer,
    }
    if run_id:
        record["run_id"] = run_id
    if task_id:
        record["task_id"] = task_id
        record["producer_task_id"] = task_id
    if run_id and task_id:
        record["uri"] = f"maze://runs/{run_id}/artifacts/tasks/{task_id}/{path}"
    return record


def _artifact_path(*, sha_hint: str, extension: str, task_id: str | None = None) -> str:
    task_part = task_id or "workflow"
    return f"result_summaries/{task_part}/{sha_hint[:16]}.{extension}"


def _artifact_summary(
    value: Any,
    *,
    data: bytes,
    reason: str,
    mime: str,
    format: str,
    serializer: str,
    extension: str,
    run_id: str | None,
    task_id: str | None,
) -> dict[str, Any]:
    sha = sha256_bytes(data)
    summary = _summarize_special_value(
        value,
        persist_large=False,
        run_id=run_id,
        task_id=task_id,
    )
    summary.pop("repr", None)
    summary["artifact"] = _artifact_record(
        data=data,
        path=_artifact_path(sha_hint=sha, extension=extension, task_id=task_id),
        mime=mime,
        format=format,
        serializer=serializer,
        run_id=run_id,
        task_id=task_id,
    )
    summary["persistence"] = "artifact_reference"
    summary["persistence_reason"] = reason
    summary["inline"] = False
    return summary


def _json_artifact_summary(
    value: Any,
    *,
    json_data: bytes,
    reason: str,
    run_id: str | None,
    task_id: str | None,
) -> dict[str, Any]:
    summary = {
        SUMMARY_MARKER: True,
        "type": _type_name(value),
        "repr": _safe_repr(value),
    }
    try:
        summary["length"] = len(value)
    except Exception:
        pass
    summary["artifact"] = _artifact_record(
        data=json_data,
        path=_artifact_path(
            sha_hint=sha256_bytes(json_data),
            extension="json",
            task_id=task_id,
        ),
        mime="application/json",
        format="json",
        serializer="json",
        run_id=run_id,
        task_id=task_id,
    )
    summary["persistence"] = "artifact_reference"
    summary["persistence_reason"] = reason
    summary["inline"] = False
    return summary


def _numpy_array_bytes(value: Any) -> bytes | None:
    if type(value).__module__.split(".", 1)[0] != "numpy":
        return None
    if not hasattr(value, "shape") or not hasattr(value, "dtype"):
        return None

    buffer = io.BytesIO()
    try:
        import numpy as np

        np.save(buffer, value, allow_pickle=False)
    except Exception:
        return None
    return buffer.getvalue()


def _summarize_special_value(
    value: Any,
    *,
    persist_large: bool = False,
    artifact_threshold_bytes: int | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        SUMMARY_MARKER: True,
        "type": _type_name(value),
        "repr": _safe_repr(value),
        "persistence": "metadata_only",
        "inline": True,
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

    if not persist_large:
        return summary

    threshold = _artifact_threshold_bytes(artifact_threshold_bytes)

    array_data = _numpy_array_bytes(value)
    if array_data is not None and len(array_data) >= threshold:
        summary["artifact"] = _artifact_record(
            data=array_data,
            path=_artifact_path(
                sha_hint=sha256_bytes(array_data),
                extension="npy",
                task_id=task_id,
            ),
            mime="application/octet-stream",
            format="numpy.npy",
            serializer="numpy.save",
            run_id=run_id,
            task_id=task_id,
        )
        summary["persistence"] = "artifact_reference"
        summary["persistence_reason"] = "large_array"
        summary["inline"] = False
        return summary

    if isinstance(value, os.PathLike):
        path = Path(value)
        summary["path"] = str(path)
        try:
            if path.is_file() and path.stat().st_size >= threshold:
                data = path.read_bytes()
                summary["artifact"] = _artifact_record(
                    data=data,
                    path=_artifact_path(
                        sha_hint=sha256_bytes(data),
                        extension=path.suffix.lstrip(".") or "bin",
                        task_id=task_id,
                    ),
                    mime="application/octet-stream",
                    format="file",
                    serializer="file_bytes",
                    run_id=run_id,
                    task_id=task_id,
                )
                summary["persistence"] = "artifact_reference"
                summary["persistence_reason"] = "large_file"
                summary["inline"] = False
        except Exception as exc:
            summary["persistence"] = "metadata_only"
            summary["persistence_error"] = f"{type(exc).__name__}: {exc}"
        return summary

    try:
        pickled = cloudpickle.dumps(value)
    except Exception as exc:
        summary["persistence"] = "metadata_only"
        summary["persistence_error"] = f"{type(exc).__name__}: {exc}"
        return summary

    if len(pickled) >= threshold:
        summary["artifact"] = _artifact_record(
            data=pickled,
            path=_artifact_path(
                sha_hint=sha256_bytes(pickled),
                extension="pkl",
                task_id=task_id,
            ),
            mime="application/octet-stream",
            format="python-pickle",
            serializer="cloudpickle",
            run_id=run_id,
            task_id=task_id,
        )
        summary["persistence"] = "artifact_reference"
        summary["persistence_reason"] = "large_object"
        summary["inline"] = False

    return summary


def to_json_safe(
    value: Any,
    *,
    max_depth: int = 6,
    _depth: int = 0,
    persist_large: bool = False,
    artifact_threshold_bytes: int | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
) -> Any:
    """Return a JSON-safe representation for execution events.

    The scheduler keeps the original Python objects internally for downstream
    task inputs. This function is only for messages that leave the scheduler
    process through JSON/WS/API surfaces.
    """
    if _is_json_serializable(value):
        if persist_large:
            json_data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            if len(json_data) >= _artifact_threshold_bytes(artifact_threshold_bytes):
                return _json_artifact_summary(
                    value,
                    json_data=json_data,
                    reason="large_json",
                    run_id=run_id,
                    task_id=task_id,
                )
        return value

    if _depth >= max_depth:
        return _summarize_special_value(
            value,
            persist_large=persist_large,
            artifact_threshold_bytes=artifact_threshold_bytes,
            run_id=run_id,
            task_id=task_id,
        )

    if isinstance(value, Mapping):
        return {
            str(key): to_json_safe(
                item,
                max_depth=max_depth,
                _depth=_depth + 1,
                persist_large=persist_large,
                artifact_threshold_bytes=artifact_threshold_bytes,
                run_id=run_id,
                task_id=task_id,
            )
            for key, item in islice(value.items(), 100)
        }

    if isinstance(value, tuple):
        return [
            to_json_safe(
                item,
                max_depth=max_depth,
                _depth=_depth + 1,
                persist_large=persist_large,
                artifact_threshold_bytes=artifact_threshold_bytes,
                run_id=run_id,
                task_id=task_id,
            )
            for item in value[:100]
        ]

    if isinstance(value, list):
        return [
            to_json_safe(
                item,
                max_depth=max_depth,
                _depth=_depth + 1,
                persist_large=persist_large,
                artifact_threshold_bytes=artifact_threshold_bytes,
                run_id=run_id,
                task_id=task_id,
            )
            for item in value[:100]
        ]

    if isinstance(value, set):
        return {
            SUMMARY_MARKER: True,
            "type": _type_name(value),
            "length": len(value),
            "sample": [
                to_json_safe(
                    item,
                    max_depth=max_depth,
                    _depth=_depth + 1,
                    persist_large=persist_large,
                    artifact_threshold_bytes=artifact_threshold_bytes,
                    run_id=run_id,
                    task_id=task_id,
                )
                for item in islice(value, 10)
            ],
        }

    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        if persist_large and len(data) >= _artifact_threshold_bytes(artifact_threshold_bytes):
            return _artifact_summary(
                value,
                data=data,
                reason="large_binary",
                mime="application/octet-stream",
                format="bytes",
                serializer="bytes",
                extension="bin",
                run_id=run_id,
                task_id=task_id,
            )
        return {
            SUMMARY_MARKER: True,
            "type": _type_name(value),
            "length": len(value),
            "repr": _safe_repr(value),
        }

    return _summarize_special_value(
        value,
        persist_large=persist_large,
        artifact_threshold_bytes=artifact_threshold_bytes,
        run_id=run_id,
        task_id=task_id,
    )


def summarize_task_result(
    result: Any,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
    artifact_threshold_bytes: int | None = None,
) -> Any:
    """Summarize a task result for JSON event streams."""
    return to_json_safe(
        result,
        persist_large=True,
        artifact_threshold_bytes=artifact_threshold_bytes,
        run_id=run_id,
        task_id=task_id,
    )
