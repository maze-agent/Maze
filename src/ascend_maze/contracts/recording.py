"""Lightweight recording sink contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ascend_maze.core.canonical import CanonicalValue, FrozenMap, freeze_canonical
from ascend_maze.core.errors import ContractValidationError


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    schema_version: int
    event_id: str
    experiment_id: str
    run_id: str | None
    task_id: str | None
    attempt: int | None
    lease_id: str | None
    route_lease_id: str | None
    model_instance_id: str | None
    event_type: str
    producer_id: str
    producer_sequence: int
    node_id: str | None
    device_id: str | None
    monotonic_time_ms: int
    wall_time_ms: int
    duration_ms: int | None
    payload: FrozenMap[CanonicalValue, CanonicalValue] = field(
        default_factory=FrozenMap
    )

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version < 1
        ):
            raise ContractValidationError("schema_version must be a positive integer")
        for name in ("event_id", "experiment_id", "event_type", "producer_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractValidationError(f"{name} is required")
        if self.attempt is not None and (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 0
        ):
            raise ContractValidationError("attempt must be non-negative")
        for name in (
            "producer_sequence",
            "monotonic_time_ms",
            "wall_time_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"{name} must be non-negative")
        if self.duration_ms is not None and (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or self.duration_ms < 0
        ):
            raise ContractValidationError("duration_ms must be non-negative")
        frozen = freeze_canonical(self.payload)
        if not isinstance(frozen, FrozenMap):
            raise ContractValidationError("payload must be a mapping")
        object.__setattr__(self, "payload", frozen)


@runtime_checkable
class RecorderSink(Protocol):
    def emit(self, event: ExecutionEvent) -> bool: ...


@dataclass(frozen=True, slots=True)
class RunRecordingContext:
    schema_version: int
    experiment_id: str
    run_id: str
    workflow_fingerprint: str
    config_fingerprint: str
    environment_fingerprint: str
    build_revision: str
    started_wall_time_ms: int
    initial_expected_producer_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version < 1
        ):
            raise ContractValidationError("schema_version must be a positive integer")
        for name in (
            "experiment_id",
            "run_id",
            "workflow_fingerprint",
            "config_fingerprint",
            "environment_fingerprint",
            "build_revision",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ContractValidationError(f"{name} is required")
        if (
            isinstance(self.started_wall_time_ms, bool)
            or not isinstance(self.started_wall_time_ms, int)
            or self.started_wall_time_ms < 0
        ):
            raise ContractValidationError("started_wall_time_ms must be non-negative")
        if (
            not isinstance(self.initial_expected_producer_ids, tuple)
            or any(
                not isinstance(item, str) or not item
                for item in self.initial_expected_producer_ids
            )
            or len(self.initial_expected_producer_ids)
            != len(set(self.initial_expected_producer_ids))
        ):
            raise ContractValidationError(
                "initial_expected_producer_ids must contain unique producer IDs"
            )


@dataclass(frozen=True, slots=True)
class FlushResult:
    run_id: str
    committed_files: tuple[str, ...]
    dropped_control_event_count: int
    dropped_telemetry_count: int
    sequence_gap_count: int
    missing_producer_count: int
    writer_errors: tuple[str, ...]
    recording_complete: bool
    flush_duration_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ContractValidationError("run_id is required")
        if any(not isinstance(path, str) or not path for path in self.committed_files):
            raise ContractValidationError("committed_files must contain paths")
        if len(self.committed_files) != len(set(self.committed_files)):
            raise ContractValidationError("committed_files must be unique")
        for name in (
            "dropped_control_event_count",
            "dropped_telemetry_count",
            "sequence_gap_count",
            "missing_producer_count",
            "flush_duration_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"{name} must be non-negative")
        if any(not isinstance(error, str) or not error for error in self.writer_errors):
            raise ContractValidationError("writer_errors must contain messages")
        if not isinstance(self.recording_complete, bool):
            raise ContractValidationError("recording_complete must be a boolean")


@dataclass(frozen=True, slots=True)
class ProducerFlushResult:
    producer_id: str
    result: FlushResult | None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.producer_id, str) or not self.producer_id:
            raise ContractValidationError("producer_id is required")
        if (self.result is None) == (self.error is None):
            raise ContractValidationError(
                "producer flush must contain exactly one result or error"
            )
        if self.error is not None and (
            not isinstance(self.error, str) or not self.error
        ):
            raise ContractValidationError("producer flush error is invalid")


@dataclass(frozen=True, slots=True)
class RunEventPage:
    events: tuple[ExecutionEvent, ...]
    next_cursor: str | None
    exhausted: bool

    def __post_init__(self) -> None:
        if self.next_cursor is not None and (
            not isinstance(self.next_cursor, str) or not self.next_cursor
        ):
            raise ContractValidationError("next_cursor is invalid")
        if not isinstance(self.exhausted, bool):
            raise ContractValidationError("exhausted must be a boolean")
        if self.exhausted and self.next_cursor is not None:
            raise ContractValidationError("exhausted page cannot have a cursor")


@dataclass(frozen=True, slots=True)
class RecorderStatus:
    schema_version: int
    backend: str
    accepting_run_count: int
    control_queue_depth: int
    control_queue_capacity: int
    telemetry_queue_depth: int
    telemetry_queue_capacity: int
    dropped_control_event_count: int
    dropped_telemetry_count: int
    sequence_gap_count: int
    writer_error_count: int
    recent_writer_errors: tuple[str, ...]
    committed_file_count: int
    closed: bool

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not self.backend:
            raise ContractValidationError("invalid RecorderStatus identity")
        for name in (
            "accepting_run_count",
            "control_queue_depth",
            "control_queue_capacity",
            "telemetry_queue_depth",
            "telemetry_queue_capacity",
            "dropped_control_event_count",
            "dropped_telemetry_count",
            "sequence_gap_count",
            "writer_error_count",
            "committed_file_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"{name} must be non-negative")
        if not isinstance(self.closed, bool):
            raise ContractValidationError("closed must be a boolean")
        if any(
            not isinstance(message, str) or not message
            for message in self.recent_writer_errors
        ):
            raise ContractValidationError(
                "recent_writer_errors must contain non-empty strings"
            )
        if len(self.recent_writer_errors) > 10:
            raise ContractValidationError("recent_writer_errors exceeds status limit")


@dataclass(frozen=True, slots=True)
class ParquetRecorderConfig:
    root_directory: str
    control_queue_capacity: int = 8_192
    telemetry_queue_capacity: int = 4_096
    batch_size: int = 256
    flush_interval_ms: int = 1_000
    compression: str = "zstd"
    max_page_size: int = 1_000

    def __post_init__(self) -> None:
        if not isinstance(self.root_directory, str) or not self.root_directory:
            raise ContractValidationError("Recorder root_directory is required")
        object.__setattr__(
            self,
            "root_directory",
            str(Path(self.root_directory).expanduser().resolve(strict=False)),
        )
        for name in (
            "control_queue_capacity",
            "telemetry_queue_capacity",
            "batch_size",
            "flush_interval_ms",
            "max_page_size",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ContractValidationError(f"{name} must be positive")
        if self.compression not in {"none", "snappy", "zstd"}:
            raise ContractValidationError("unsupported Parquet compression")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "backend": "parquet",
            "root_directory": self.root_directory,
            "control_queue_capacity": self.control_queue_capacity,
            "telemetry_queue_capacity": self.telemetry_queue_capacity,
            "batch_size": self.batch_size,
            "flush_interval_ms": self.flush_interval_ms,
            "compression": self.compression,
            "max_page_size": self.max_page_size,
        }


@runtime_checkable
class ExecutionRecorder(RecorderSink, Protocol):
    def open_run(self, context: RunRecordingContext) -> None: ...

    def abort_run(self, run_id: str) -> bool: ...

    def expect_producer(self, run_id: str, producer_id: str) -> None: ...

    def record_writer_error(self, run_id: str, message: str) -> None: ...

    def merge_producer_flush(
        self,
        run_id: str,
        producer_id: str,
        result: FlushResult,
    ) -> None: ...

    async def flush_run(self, run_id: str, timeout_ms: int) -> FlushResult: ...

    async def close(self, timeout_ms: int) -> None: ...

    def status(self) -> RecorderStatus: ...


@runtime_checkable
class HistoricalEventReader(Protocol):
    def get_run_events(
        self,
        run_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> RunEventPage: ...
