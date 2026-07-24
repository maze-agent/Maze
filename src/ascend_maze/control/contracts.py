"""C13 request envelopes and bounded write-request idempotency."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Generic, TypeVar

from ascend_maze.core.errors import ContractValidationError, SubmissionConflictError


@dataclass(frozen=True, slots=True)
class NodeRuntimePolicy:
    task_slots_total: int = 1
    allow_colocation: bool = False
    npu_system_reserved_hbm_mb: int = 4_096
    npu_hbm_headroom_mb: int = 1_024
    host_mem_headroom_mb: int = 1_024
    io_slots_total: int = 8
    hbm_recovery_tolerance_mb: int = 64
    recording_backend: str = "parquet"
    recording_control_queue_capacity: int = 8_192
    recording_telemetry_queue_capacity: int = 4_096
    recording_batch_size: int = 256
    recording_flush_interval_ms: int = 1_000
    recording_compression: str = "zstd"
    recording_max_page_size: int = 1_000

    def __post_init__(self) -> None:
        for name in (
            "task_slots_total",
            "io_slots_total",
            "recording_control_queue_capacity",
            "recording_telemetry_queue_capacity",
            "recording_batch_size",
            "recording_flush_interval_ms",
            "recording_max_page_size",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ContractValidationError(f"NodeRuntimePolicy.{name} must be positive")
        for name in (
            "npu_system_reserved_hbm_mb",
            "npu_hbm_headroom_mb",
            "host_mem_headroom_mb",
            "hbm_recovery_tolerance_mb",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(
                    f"NodeRuntimePolicy.{name} must be non-negative"
                )
        if not isinstance(self.allow_colocation, bool):
            raise ContractValidationError(
                "NodeRuntimePolicy.allow_colocation must be a boolean"
            )
        if not self.allow_colocation and self.task_slots_total != 1:
            raise ContractValidationError(
                "NodeRuntimePolicy.task_slots_total must be 1 without colocation"
            )
        if self.recording_backend not in {"parquet", "noop"}:
            raise ContractValidationError(
                "NodeRuntimePolicy.recording_backend is unsupported"
            )
        if self.recording_compression not in {"none", "snappy", "zstd"}:
            raise ContractValidationError(
                "NodeRuntimePolicy.recording_compression is unsupported"
            )


@dataclass(frozen=True, slots=True)
class ControlRequestMeta:
    schema_version: int
    request_id: str
    client_version: str
    config_fingerprint: str | None
    deadline_ms: int
    controller_generation: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractValidationError("unsupported control request schema_version")
        for name in ("request_id", "client_version"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractValidationError(f"{name} is required")
        if isinstance(self.deadline_ms, bool) or not isinstance(self.deadline_ms, int) or self.deadline_ms < 1:
            raise ContractValidationError("deadline_ms must be positive")


@dataclass(frozen=True, slots=True)
class ControlResponseMeta:
    schema_version: int
    request_id: str
    controller_generation: str
    status_code: str
    error_code: str | None
    message: str
    snapshot_version: int | None = None
    resulting_event_id: str | None = None


_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class _JournalEntry(Generic[_T]):
    operation: str
    payload_digest: str
    result: _T


class RequestJournal:
    """Remember completed writes; repeated IDs cannot execute a new payload."""

    def __init__(self, *, capacity: int = 10_000) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("request journal capacity must be positive")
        self.capacity = capacity
        self._entries: dict[str, _JournalEntry[object]] = {}
        self._order: list[str] = []
        self._lock = RLock()

    def lookup(
        self,
        request_id: str,
        *,
        operation: str,
        payload_digest: str,
    ) -> object | None:
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is None:
                return None
            if entry.operation != operation or entry.payload_digest != payload_digest:
                raise SubmissionConflictError(
                    "request_id already completed with a different operation or payload"
                )
            return entry.result

    def remember(
        self,
        request_id: str,
        *,
        operation: str,
        payload_digest: str,
        result: object,
    ) -> object:
        with self._lock:
            existing = self._entries.get(request_id)
            if existing is not None:
                if existing.operation != operation or existing.payload_digest != payload_digest:
                    raise SubmissionConflictError(
                        "request_id already completed with a different operation or payload"
                    )
                return existing.result
            self._entries[request_id] = _JournalEntry(operation, payload_digest, result)
            self._order.append(request_id)
            while len(self._order) > self.capacity:
                expired = self._order.pop(0)
                self._entries.pop(expired, None)
            return result

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)
