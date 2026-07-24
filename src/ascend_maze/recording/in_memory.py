"""In-memory C8 implementation for deterministic control-plane tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

from ascend_maze.contracts.recording import (
    ExecutionEvent,
    FlushResult,
    RecorderStatus,
    RunRecordingContext,
)
from ascend_maze.core.errors import ContractValidationError
from ascend_maze.core.time import monotonic_time_ms


@dataclass(slots=True)
class _RunRecording:
    context: RunRecordingContext
    expected_producers: set[str]
    seen_producers: set[str] = field(default_factory=set)
    last_sequence: dict[str, int] = field(default_factory=dict)
    event_ids: set[str] = field(default_factory=set)
    events_by_id: dict[str, ExecutionEvent] = field(default_factory=dict)
    events: list[ExecutionEvent] = field(default_factory=list)
    dropped_control: int = 0
    sequence_gaps: int = 0
    writer_errors: list[str] = field(default_factory=list)
    producer_flushes: dict[str, FlushResult] = field(default_factory=dict)
    accepting: bool = True
    flushed: FlushResult | None = None


class InMemoryRecorder:
    def __init__(self, *, control_capacity_per_run: int = 10_000) -> None:
        if control_capacity_per_run < 1:
            raise ValueError("control_capacity_per_run must be positive")
        self.control_capacity_per_run = control_capacity_per_run
        self._runs: dict[str, _RunRecording] = {}
        self._aborted_runs: set[str] = set()
        self._event_owners: dict[str, str] = {}
        self._last_sequence: dict[str, int] = {}
        self._closed = False
        self._fail_next_open = False
        self._fail_next_emit = False
        self._lock = RLock()

    def inject_open_failure(self) -> None:
        with self._lock:
            self._fail_next_open = True

    def inject_emit_failure(self) -> None:
        with self._lock:
            self._fail_next_emit = True

    def open_run(self, context: RunRecordingContext) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("recorder is closed")
            if context.run_id in self._aborted_runs:
                raise RuntimeError("run recording was aborted")
            if self._fail_next_open:
                self._fail_next_open = False
                raise RuntimeError("injected recorder open failure")
            existing = self._runs.get(context.run_id)
            if existing is not None:
                if existing.context != context:
                    raise ContractValidationError(
                        "run recording context conflicts with existing context"
                    )
                return
            self._runs[context.run_id] = _RunRecording(
                context=context,
                expected_producers=set(context.initial_expected_producer_ids),
            )

    def abort_run(self, run_id: str) -> bool:
        with self._lock:
            existing = self._runs.get(run_id)
            if existing is not None and existing.flushed is not None:
                raise RuntimeError("cannot abort a flushed recording")
            recording = self._runs.pop(run_id, None)
            existed = recording is not None
            if recording is not None:
                for event_id in recording.event_ids:
                    self._event_owners.pop(event_id, None)
            self._aborted_runs.add(run_id)
            return existed

    def expect_producer(self, run_id: str, producer_id: str) -> None:
        if not isinstance(producer_id, str) or not producer_id:
            raise ContractValidationError("producer_id is required")
        with self._lock:
            recording = self._require_run(run_id)
            if recording.flushed is not None or not recording.accepting:
                raise RuntimeError("run recording no longer accepts producers")
            recording.expected_producers.add(producer_id)

    def emit(self, event: ExecutionEvent) -> bool:
        if event.run_id is None:
            return False
        with self._lock:
            recording = self._runs.get(event.run_id)
            if (
                self._closed
                or recording is None
                or not recording.accepting
                or recording.flushed is not None
            ):
                return False
            if self._fail_next_emit:
                self._fail_next_emit = False
                raise RuntimeError("injected recorder emit failure")
            if event.experiment_id != recording.context.experiment_id:
                raise ContractValidationError(
                    "event experiment_id does not match RunRecordingContext"
                )
            if event.producer_id not in recording.expected_producers:
                raise ContractValidationError("event producer is not expected for run")
            owner = self._event_owners.get(event.event_id)
            if owner is not None:
                existing = self._runs[owner].events_by_id[event.event_id]
                if existing != event:
                    raise ContractValidationError("event_id identifies conflicting events")
                return True
            if len(recording.events) >= self.control_capacity_per_run:
                recording.dropped_control += 1
                return False
            previous = self._last_sequence.get(event.producer_id)
            if previous is not None and event.producer_sequence != previous + 1:
                recording.sequence_gaps += 1
            self._last_sequence[event.producer_id] = event.producer_sequence
            recording.last_sequence[event.producer_id] = event.producer_sequence
            recording.seen_producers.add(event.producer_id)
            recording.event_ids.add(event.event_id)
            recording.events_by_id[event.event_id] = event
            recording.events.append(event)
            self._event_owners[event.event_id] = event.run_id
            return True

    def record_writer_error(self, run_id: str, message: str) -> None:
        if not isinstance(message, str) or not message:
            raise ContractValidationError("writer error message is required")
        with self._lock:
            recording = self._runs.get(run_id)
            if recording is not None and recording.flushed is None:
                recording.writer_errors.append(message)

    def merge_producer_flush(
        self,
        run_id: str,
        producer_id: str,
        result: FlushResult,
    ) -> None:
        if not isinstance(producer_id, str) or not producer_id:
            raise ContractValidationError("producer_id is required")
        if result.run_id != run_id:
            raise ContractValidationError("producer FlushResult run_id mismatch")
        with self._lock:
            recording = self._require_run(run_id)
            if recording.flushed is not None:
                raise RuntimeError("run recording is already flushed")
            if not recording.accepting:
                raise RuntimeError("run recording no longer accepts producer flushes")
            if producer_id not in recording.expected_producers:
                raise ContractValidationError("producer FlushResult is not expected for run")
            existing = recording.producer_flushes.get(producer_id)
            if existing is not None and existing != result:
                raise ContractValidationError("producer FlushResult conflict")
            recording.producer_flushes[producer_id] = result

    async def flush_run(self, run_id: str, timeout_ms: int) -> FlushResult:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
            raise ValueError("timeout_ms must be positive")
        started = monotonic_time_ms()
        with self._lock:
            recording = self._require_run(run_id)
            if recording.flushed is not None:
                return recording.flushed
            recording.accepting = False
            remote_producers = set(recording.producer_flushes)
            missing = (
                recording.expected_producers
                - recording.seen_producers
                - remote_producers
            )
            remote_results = tuple(recording.producer_flushes.values())
            committed_files = tuple(
                dict.fromkeys(
                    path
                    for result in remote_results
                    for path in result.committed_files
                )
            )
            dropped_control = recording.dropped_control + sum(
                result.dropped_control_event_count for result in remote_results
            )
            dropped_telemetry = sum(
                result.dropped_telemetry_count for result in remote_results
            )
            sequence_gaps = recording.sequence_gaps + sum(
                result.sequence_gap_count for result in remote_results
            )
            missing_count = len(missing) + sum(
                result.missing_producer_count for result in remote_results
            )
            writer_errors = tuple(recording.writer_errors) + tuple(
                error
                for result in remote_results
                for error in result.writer_errors
            )
            complete = (
                dropped_control == 0
                and dropped_telemetry == 0
                and sequence_gaps == 0
                and missing_count == 0
                and not writer_errors
                and all(result.recording_complete for result in remote_results)
            )
            result = FlushResult(
                run_id=run_id,
                committed_files=committed_files,
                dropped_control_event_count=dropped_control,
                dropped_telemetry_count=dropped_telemetry,
                sequence_gap_count=sequence_gaps,
                missing_producer_count=missing_count,
                writer_errors=writer_errors,
                recording_complete=complete,
                flush_duration_ms=max(0, monotonic_time_ms() - started),
            )
            recording.flushed = result
            return result

    async def close(self, timeout_ms: int) -> None:
        del timeout_ms
        with self._lock:
            self._closed = True

    def status(self) -> RecorderStatus:
        with self._lock:
            recordings = tuple(self._runs.values())
            dropped_control = 0
            dropped_telemetry = 0
            sequence_gaps = 0
            committed_files: set[str] = set()
            for recording in recordings:
                if recording.flushed is not None:
                    dropped_control += recording.flushed.dropped_control_event_count
                    dropped_telemetry += recording.flushed.dropped_telemetry_count
                    sequence_gaps += recording.flushed.sequence_gap_count
                    committed_files.update(recording.flushed.committed_files)
                    continue
                producer_results = tuple(recording.producer_flushes.values())
                dropped_control += recording.dropped_control + sum(
                    result.dropped_control_event_count for result in producer_results
                )
                dropped_telemetry += sum(
                    result.dropped_telemetry_count for result in producer_results
                )
                sequence_gaps += recording.sequence_gaps + sum(
                    result.sequence_gap_count for result in producer_results
                )
                committed_files.update(
                    path for result in producer_results for path in result.committed_files
                )
            writer_errors = tuple(
                error
                for recording in recordings
                for error in (
                    recording.flushed.writer_errors
                    if recording.flushed is not None
                    else tuple(recording.writer_errors)
                    + tuple(
                        message
                        for result in recording.producer_flushes.values()
                        for message in result.writer_errors
                    )
                )
            )
            return RecorderStatus(
                schema_version=1,
                backend="memory",
                accepting_run_count=sum(item.accepting for item in recordings),
                control_queue_depth=0,
                control_queue_capacity=self.control_capacity_per_run,
                telemetry_queue_depth=0,
                telemetry_queue_capacity=0,
                dropped_control_event_count=dropped_control,
                dropped_telemetry_count=dropped_telemetry,
                sequence_gap_count=sequence_gaps,
                writer_error_count=len(writer_errors),
                recent_writer_errors=writer_errors[-10:],
                committed_file_count=len(committed_files),
                closed=self._closed,
            )

    def events(self, run_id: str) -> tuple[ExecutionEvent, ...]:
        with self._lock:
            return tuple(self._require_run(run_id).events)

    def context(self, run_id: str) -> RunRecordingContext:
        with self._lock:
            return self._require_run(run_id).context

    def is_aborted(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._aborted_runs

    @property
    def active_run_count(self) -> int:
        with self._lock:
            return len(self._runs)

    def _require_run(self, run_id: str) -> _RunRecording:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise KeyError(f"unknown recording run: {run_id}") from exc


class NoopRecorder:
    def open_run(self, context: RunRecordingContext) -> None:
        del context

    def abort_run(self, run_id: str) -> bool:
        del run_id
        return False

    def expect_producer(self, run_id: str, producer_id: str) -> None:
        del run_id, producer_id

    def emit(self, event: ExecutionEvent) -> bool:
        del event
        return True

    def record_writer_error(self, run_id: str, message: str) -> None:
        del run_id, message

    def merge_producer_flush(
        self,
        run_id: str,
        producer_id: str,
        result: FlushResult,
    ) -> None:
        del run_id, producer_id, result

    async def flush_run(self, run_id: str, timeout_ms: int) -> FlushResult:
        del timeout_ms
        return FlushResult(run_id, (), 0, 0, 0, 0, (), True, 0)

    async def close(self, timeout_ms: int) -> None:
        del timeout_ms

    def status(self) -> RecorderStatus:
        return RecorderStatus(
            schema_version=1,
            backend="noop",
            accepting_run_count=0,
            control_queue_depth=0,
            control_queue_capacity=0,
            telemetry_queue_depth=0,
            telemetry_queue_capacity=0,
            dropped_control_event_count=0,
            dropped_telemetry_count=0,
            sequence_gap_count=0,
            writer_error_count=0,
            recent_writer_errors=(),
            committed_file_count=0,
            closed=False,
        )
