"""Strict, immutable import of C8 committed Parquet files."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import Any

from ascend_maze.contracts.recording import ExecutionEvent, RunRecordingContext
from ascend_maze.core.canonical import FrozenMap, decode_canonical_bytes

CONTEXT_METADATA = {b"ascend_maze_schema": b"run_recording_context_v1"}
EVENT_METADATA = {b"ascend_maze_schema": b"execution_event_v1"}


class ParquetImportFailure(Exception):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class FileObservation:
    source_path: str
    size_bytes: int
    sha256: str
    device: int
    inode: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class ImportedParquet:
    observation: FileObservation
    kind: str
    contexts: tuple[RunRecordingContext, ...]
    events: tuple[ExecutionEvent, ...]

    @property
    def row_count(self) -> int:
        return len(self.contexts) + len(self.events)


def observe_regular_file(path: Path) -> FileObservation:
    if not path.is_absolute():
        raise ParquetImportFailure("committed_file_path_invalid")
    if _is_temporary(path):
        raise ParquetImportFailure("committed_file_temporary")
    try:
        status = path.lstat()
    except FileNotFoundError as exc:
        raise ParquetImportFailure("committed_file_missing") from exc
    except OSError as exc:
        raise ParquetImportFailure("committed_file_path_invalid") from exc
    if not stat.S_ISREG(status.st_mode) or path.is_symlink():
        raise ParquetImportFailure("committed_file_not_regular")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ParquetImportFailure("committed_file_path_invalid") from exc
    if resolved != path:
        raise ParquetImportFailure("committed_file_path_invalid")
    return FileObservation(
        source_path=str(path),
        size_bytes=status.st_size,
        sha256=_file_sha256(path),
        device=status.st_dev,
        inode=status.st_ino,
        mtime_ns=status.st_mtime_ns,
    )


def import_committed_parquet(path: Path) -> ImportedParquet:
    before = observe_regular_file(path)
    try:
        import pyarrow.parquet as pq

        parquet = pq.ParquetFile(path)
        schema = parquet.schema_arrow
    except Exception as exc:
        raise ParquetImportFailure("parquet_footer_invalid") from exc

    metadata = schema.metadata
    if metadata == CONTEXT_METADATA:
        kind = "context"
        expected = context_schema()
    elif metadata == EVENT_METADATA:
        kind = "event"
        expected = event_schema()
    else:
        raise ParquetImportFailure("parquet_metadata_invalid")
    if schema != expected:
        raise ParquetImportFailure("parquet_schema_invalid")
    try:
        rows = parquet.read().to_pylist()
    except Exception as exc:
        raise ParquetImportFailure("parquet_footer_invalid") from exc

    contexts: tuple[RunRecordingContext, ...] = ()
    events: tuple[ExecutionEvent, ...] = ()
    try:
        if kind == "context":
            if len(rows) != 1:
                raise ParquetImportFailure("context_row_invalid")
            contexts = (_context_from_row(rows[0]),)
        else:
            events = tuple(_event_from_row(row) for row in rows)
    except ParquetImportFailure:
        raise
    except Exception as exc:
        code = "context_row_invalid" if kind == "context" else "event_row_invalid"
        raise ParquetImportFailure(code) from exc

    after = observe_regular_file(path)
    if before != after:
        raise ParquetImportFailure("committed_file_hash_changed")
    return ImportedParquet(before, kind, contexts, events)


def context_schema() -> Any:
    import pyarrow as pa

    return pa.schema(
        [
            ("schema_version", pa.int32()),
            ("experiment_id", pa.string()),
            ("run_id", pa.string()),
            ("workflow_fingerprint", pa.string()),
            ("config_fingerprint", pa.string()),
            ("environment_fingerprint", pa.string()),
            ("build_revision", pa.string()),
            ("started_wall_time_ms", pa.int64()),
            ("initial_expected_producer_ids", pa.list_(pa.string())),
        ],
        metadata=CONTEXT_METADATA,
    )


def event_schema() -> Any:
    import pyarrow as pa

    return pa.schema(
        [
            ("schema_version", pa.int32()),
            ("event_id", pa.string()),
            ("experiment_id", pa.string()),
            ("run_id", pa.string()),
            ("task_id", pa.string()),
            ("attempt", pa.int32()),
            ("lease_id", pa.string()),
            ("route_lease_id", pa.string()),
            ("model_instance_id", pa.string()),
            ("event_type", pa.string()),
            ("producer_id", pa.string()),
            ("producer_sequence", pa.int64()),
            ("node_id", pa.string()),
            ("device_id", pa.string()),
            ("monotonic_time_ms", pa.int64()),
            ("wall_time_ms", pa.int64()),
            ("duration_ms", pa.int64()),
            ("payload", pa.binary()),
        ],
        metadata=EVENT_METADATA,
    )


def _event_from_row(row: dict[str, object]) -> ExecutionEvent:
    payload_bytes = row.get("payload")
    if not isinstance(payload_bytes, bytes):
        raise TypeError("event payload is not bytes")
    payload = decode_canonical_bytes(payload_bytes)
    if not isinstance(payload, FrozenMap):
        raise TypeError("event payload is not a mapping")
    return ExecutionEvent(
        schema_version=_required_int(row, "schema_version"),
        event_id=_required_str(row, "event_id"),
        experiment_id=_required_str(row, "experiment_id"),
        run_id=_required_str(row, "run_id"),
        task_id=_optional_str(row, "task_id"),
        attempt=_optional_int(row, "attempt"),
        lease_id=_optional_str(row, "lease_id"),
        route_lease_id=_optional_str(row, "route_lease_id"),
        model_instance_id=_optional_str(row, "model_instance_id"),
        event_type=_required_str(row, "event_type"),
        producer_id=_required_str(row, "producer_id"),
        producer_sequence=_required_int(row, "producer_sequence"),
        node_id=_optional_str(row, "node_id"),
        device_id=_optional_str(row, "device_id"),
        monotonic_time_ms=_required_int(row, "monotonic_time_ms"),
        wall_time_ms=_required_int(row, "wall_time_ms"),
        duration_ms=_optional_int(row, "duration_ms"),
        payload=payload,
    )


def _context_from_row(row: dict[str, object]) -> RunRecordingContext:
    producers = row.get("initial_expected_producer_ids")
    if not isinstance(producers, list) or any(
        not isinstance(item, str) or not item for item in producers
    ):
        raise TypeError("context producer IDs are invalid")
    return RunRecordingContext(
        schema_version=_required_int(row, "schema_version"),
        experiment_id=_required_str(row, "experiment_id"),
        run_id=_required_str(row, "run_id"),
        workflow_fingerprint=_required_str(row, "workflow_fingerprint"),
        config_fingerprint=_required_str(row, "config_fingerprint"),
        environment_fingerprint=_required_str(row, "environment_fingerprint"),
        build_revision=_required_str(row, "build_revision"),
        started_wall_time_ms=_required_int(row, "started_wall_time_ms"),
        initial_expected_producer_ids=tuple(producers),
    )


def _required_str(row: dict[str, object], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} is invalid")
    return value


def _optional_str(row: dict[str, object], name: str) -> str | None:
    value = row.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} is invalid")
    return value


def _required_int(row: dict[str, object], name: str) -> int:
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} is invalid")
    return value


def _optional_int(row: dict[str, object], name: str) -> int | None:
    value = row.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} is invalid")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ParquetImportFailure("committed_file_path_invalid") from exc
    return digest.hexdigest()


def _is_temporary(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".tmp") or (name.startswith(".") and ".tmp" in name)
