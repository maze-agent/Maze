"""Versioned Controller snapshots and real-time control event retention."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import RLock

from ascend_maze.contracts.recording import ExecutionEvent
from ascend_maze.core.canonical import CanonicalValue, FrozenMap
from ascend_maze.core.errors import ContractValidationError


@dataclass(frozen=True, slots=True)
class SnapshotMeta:
    schema_version: int
    snapshot_version: int
    controller_generation: str
    config_fingerprint: str
    generated_at_ms: int
    measurement_quality: str = "authoritative_snapshot"

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractValidationError("unsupported snapshot schema_version")
        if self.snapshot_version < 0 or self.generated_at_ms < 0:
            raise ContractValidationError("snapshot counters must be non-negative")
        if not self.controller_generation or not self.config_fingerprint:
            raise ContractValidationError("snapshot identity fields are required")


@dataclass(frozen=True, slots=True)
class ControlEvent:
    schema_version: int
    sequence: int
    event_id: str
    run_id: str
    task_id: str | None
    attempt: int | None
    event_type: str
    monotonic_time_ms: int
    wall_time_ms: int
    payload: FrozenMap[CanonicalValue, CanonicalValue]

    @classmethod
    def from_execution_event(
        cls,
        event: ExecutionEvent,
        *,
        sequence: int,
    ) -> "ControlEvent":
        if event.run_id is None:
            raise ContractValidationError("control event requires run_id")
        return cls(
            schema_version=1,
            sequence=sequence,
            event_id=event.event_id,
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            event_type=event.event_type,
            monotonic_time_ms=event.monotonic_time_ms,
            wall_time_ms=event.wall_time_ms,
            payload=event.payload,
        )


@dataclass(frozen=True, slots=True)
class WatchRunBatch:
    events: tuple[ControlEvent, ...]
    next_sequence: int
    snapshot_required: bool
    run_terminal: bool


class ControlEventBuffer:
    """One Controller-local total order, intentionally separate from C8."""

    def __init__(self, *, retention_count: int = 10_000) -> None:
        if isinstance(retention_count, bool) or not isinstance(retention_count, int) or retention_count < 1:
            raise ValueError("control event retention_count must be positive")
        self.retention_count = retention_count
        self._events: list[ControlEvent] = []
        self._next_sequence = 1
        self._terminal_runs: set[str] = set()
        self._changed = asyncio.Event()
        self._lock = RLock()

    def append(self, event: ExecutionEvent) -> ControlEvent:
        with self._lock:
            item = ControlEvent.from_execution_event(
                event,
                sequence=self._next_sequence,
            )
            self._next_sequence += 1
            self._events.append(item)
            if event.event_type == "run_terminal":
                self._terminal_runs.add(item.run_id)
            if len(self._events) > self.retention_count:
                del self._events[: len(self._events) - self.retention_count]
            changed = self._changed
            self._changed = asyncio.Event()
            changed.set()
            return item

    def read_run(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        terminal: bool | None = None,
    ) -> WatchRunBatch:
        if after_sequence < 0 or limit < 1:
            raise ContractValidationError("watch sequence and limit are invalid")
        with self._lock:
            earliest = self._events[0].sequence if self._events else self._next_sequence
            latest = self._next_sequence - 1
            if after_sequence and after_sequence < earliest - 1:
                return WatchRunBatch((), latest, True, bool(terminal))
            selected = tuple(
                event
                for event in self._events
                if event.run_id == run_id and event.sequence > after_sequence
            )[:limit]
            next_sequence = selected[-1].sequence if selected else max(after_sequence, latest)
            is_terminal = run_id in self._terminal_runs if terminal is None else terminal
            return WatchRunBatch(selected, next_sequence, False, is_terminal)

    async def wait_run(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        timeout_seconds: float | None = None,
        terminal: bool | None = None,
    ) -> WatchRunBatch:
        while True:
            with self._lock:
                batch = self.read_run(
                    run_id,
                    after_sequence=after_sequence,
                    limit=limit,
                    terminal=terminal,
                )
                if batch.events or batch.snapshot_required or batch.run_terminal:
                    return batch
                changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), timeout=timeout_seconds)
            except TimeoutError:
                return self.read_run(
                    run_id,
                    after_sequence=after_sequence,
                    limit=limit,
                    terminal=terminal,
                )

    @property
    def latest_sequence(self) -> int:
        with self._lock:
            return self._next_sequence - 1
