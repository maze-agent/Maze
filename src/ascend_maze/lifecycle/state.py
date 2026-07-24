"""C3 run/task/attempt state machine with idempotent late-event handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import RLock

from ascend_maze.compiler.ir import CompiledWorkflow
from ascend_maze.contracts.errors import ErrorInfo
from ascend_maze.core.errors import StateTransitionError


class RunStatus(str, Enum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"


class TaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class AttemptStatus(str, Enum):
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


RUN_TERMINAL = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
        RunStatus.INTERRUPTED,
    }
)
TASK_TERMINAL = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.TIMED_OUT,
        TaskStatus.CANCELLED,
    }
)
ATTEMPT_TERMINAL = frozenset(
    {
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED,
        AttemptStatus.TIMED_OUT,
        AttemptStatus.CANCELLED,
    }
)


@dataclass(slots=True)
class _AttemptRecord:
    attempt: int
    dispatch_id: str
    lease_id: str
    status: AttemptStatus
    dispatched_at_ms: int
    worker_started_at_ms: int | None = None
    finished_at_ms: int | None = None
    node_id: str | None = None
    device_ids: tuple[str, ...] = ()
    anchor_revision: int = 1
    error: ErrorInfo | None = None


@dataclass(slots=True)
class _TaskRecord:
    task_id: str
    status: TaskStatus
    remaining_predecessors: int
    ready_at_ms: int | None = None
    queued_at_ms: int | None = None
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    attempt_count: int = 0
    attempts: list[_AttemptRecord] = field(default_factory=list)
    pending_reason: str | None = None
    cancellation_reason: str | None = None
    last_error: ErrorInfo | None = None


@dataclass(slots=True)
class _RunRecord:
    run_id: str
    workflow_id: str
    workflow_fingerprint: str
    routing_session_key_hash: str | None
    status: RunStatus
    submitted_at_ms: int
    started_at_ms: int | None
    finished_at_ms: int | None
    deadline_at_ms: int | None
    compiled: CompiledWorkflow
    task_states: dict[str, _TaskRecord]
    failure: ErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class AttemptSnapshot:
    task_id: str
    attempt: int
    dispatch_id: str
    lease_id: str
    status: AttemptStatus
    dispatched_at_ms: int
    worker_started_at_ms: int | None
    finished_at_ms: int | None
    node_id: str | None
    device_ids: tuple[str, ...]
    anchor_revision: int
    error: ErrorInfo | None


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task_id: str
    status: TaskStatus
    remaining_predecessors: int
    ready_at_ms: int | None
    queued_at_ms: int | None
    started_at_ms: int | None
    finished_at_ms: int | None
    attempt_count: int
    attempts: tuple[AttemptSnapshot, ...]
    pending_reason: str | None
    cancellation_reason: str | None
    last_error: ErrorInfo | None


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    run_id: str
    workflow_id: str
    workflow_fingerprint: str
    routing_session_key_hash: str | None
    status: RunStatus
    submitted_at_ms: int
    started_at_ms: int | None
    finished_at_ms: int | None
    deadline_at_ms: int | None
    task_states: tuple[TaskSnapshot, ...]
    failure: ErrorInfo | None

    def task(self, task_id: str) -> TaskSnapshot:
        for task in self.task_states:
            if task.task_id == task_id:
                return task
        raise KeyError(task_id)

    @property
    def terminal(self) -> bool:
        return self.status in RUN_TERMINAL


@dataclass(frozen=True, slots=True)
class TransitionResult:
    accepted: bool
    ready_task_ids: tuple[str, ...] = ()
    cancelled_attempts: tuple[AttemptSnapshot, ...] = ()
    run_terminal: bool = False


class RunStateManager:
    """Only authority allowed to mutate C3 lifecycle records."""

    def __init__(self) -> None:
        self._runs: dict[str, _RunRecord] = {}
        self._lock = RLock()

    def create_run(
        self,
        *,
        run_id: str,
        compiled: CompiledWorkflow,
        routing_session_key_hash: str | None,
        submitted_at_ms: int,
        deadline_at_ms: int | None,
    ) -> None:
        with self._lock:
            if run_id in self._runs:
                raise StateTransitionError(f"run already exists: {run_id}")
            tasks = {
                task_id: _TaskRecord(
                    task_id=task_id,
                    status=TaskStatus.PENDING,
                    remaining_predecessors=len(compiled.predecessors[task_id]),
                )
                for task_id in compiled.topological_order
            }
            self._runs[run_id] = _RunRecord(
                run_id=run_id,
                workflow_id=compiled.workflow_id,
                workflow_fingerprint=compiled.workflow_fingerprint,
                routing_session_key_hash=routing_session_key_hash,
                status=RunStatus.SUBMITTED,
                submitted_at_ms=submitted_at_ms,
                started_at_ms=None,
                finished_at_ms=None,
                deadline_at_ms=deadline_at_ms,
                compiled=compiled,
                task_states=tasks,
            )

    def restore_run(
        self,
        *,
        compiled: CompiledWorkflow,
        snapshot: RunSnapshot,
    ) -> None:
        """Restore an authoritative snapshot before startup reconciliation."""

        if compiled.workflow_id != snapshot.workflow_id:
            raise StateTransitionError("restored workflow_id does not match snapshot")
        if compiled.workflow_fingerprint != snapshot.workflow_fingerprint:
            raise StateTransitionError(
                "restored workflow fingerprint does not match snapshot"
            )
        by_task = {item.task_id: item for item in snapshot.task_states}
        if set(by_task) != set(compiled.tasks):
            raise StateTransitionError("restored task set does not match workflow")
        tasks: dict[str, _TaskRecord] = {}
        for task_id in compiled.topological_order:
            item = by_task[task_id]
            tasks[task_id] = _TaskRecord(
                task_id=item.task_id,
                status=item.status,
                remaining_predecessors=item.remaining_predecessors,
                ready_at_ms=item.ready_at_ms,
                queued_at_ms=item.queued_at_ms,
                started_at_ms=item.started_at_ms,
                finished_at_ms=item.finished_at_ms,
                attempt_count=item.attempt_count,
                attempts=[
                    _AttemptRecord(
                        attempt=attempt.attempt,
                        dispatch_id=attempt.dispatch_id,
                        lease_id=attempt.lease_id,
                        status=attempt.status,
                        dispatched_at_ms=attempt.dispatched_at_ms,
                        worker_started_at_ms=attempt.worker_started_at_ms,
                        finished_at_ms=attempt.finished_at_ms,
                        node_id=attempt.node_id,
                        device_ids=attempt.device_ids,
                        anchor_revision=attempt.anchor_revision,
                        error=attempt.error,
                    )
                    for attempt in item.attempts
                ],
                pending_reason=item.pending_reason,
                cancellation_reason=item.cancellation_reason,
                last_error=item.last_error,
            )
        with self._lock:
            if snapshot.run_id in self._runs:
                raise StateTransitionError(f"run already exists: {snapshot.run_id}")
            self._runs[snapshot.run_id] = _RunRecord(
                run_id=snapshot.run_id,
                workflow_id=snapshot.workflow_id,
                workflow_fingerprint=snapshot.workflow_fingerprint,
                routing_session_key_hash=snapshot.routing_session_key_hash,
                status=snapshot.status,
                submitted_at_ms=snapshot.submitted_at_ms,
                started_at_ms=snapshot.started_at_ms,
                finished_at_ms=snapshot.finished_at_ms,
                deadline_at_ms=snapshot.deadline_at_ms,
                compiled=compiled,
                task_states=tasks,
                failure=snapshot.failure,
            )

    def snapshots(self) -> tuple[RunSnapshot, ...]:
        with self._lock:
            return tuple(self.snapshot(run_id) for run_id in sorted(self._runs))

    def remove_submitted_run(self, run_id: str) -> None:
        with self._lock:
            run = self._require_run(run_id)
            if run.status is not RunStatus.SUBMITTED:
                raise StateTransitionError("only an unstarted submitted run can be removed")
            del self._runs[run_id]

    def start_run(self, run_id: str, now_ms: int) -> TransitionResult:
        with self._lock:
            run = self._require_run(run_id)
            if run.status is RunStatus.RUNNING:
                return TransitionResult(False)
            if run.status is not RunStatus.SUBMITTED:
                raise StateTransitionError(
                    f"cannot start run from {run.status.value}"
                )
            run.status = RunStatus.RUNNING
            run.started_at_ms = now_ms
            ready: list[str] = []
            for task_id in run.compiled.entry_tasks:
                task = run.task_states[task_id]
                task.status = TaskStatus.READY
                task.ready_at_ms = now_ms
                ready.append(task_id)
            return TransitionResult(True, tuple(ready))

    def mark_queued(self, run_id: str, task_id: str, now_ms: int) -> bool:
        with self._lock:
            run, task = self._require_task(run_id, task_id)
            if run.status in RUN_TERMINAL or task.status is TaskStatus.QUEUED:
                return False
            if task.status is not TaskStatus.READY:
                raise StateTransitionError(
                    f"cannot queue task from {task.status.value}"
                )
            task.status = TaskStatus.QUEUED
            task.queued_at_ms = now_ms
            task.pending_reason = None
            return True

    def set_pending_reason(
        self,
        run_id: str,
        task_id: str,
        reason: str | None,
    ) -> bool:
        with self._lock:
            run, task = self._require_task(run_id, task_id)
            if run.status in RUN_TERMINAL or task.status is not TaskStatus.QUEUED:
                return False
            task.pending_reason = reason
            return True

    def create_attempt(
        self,
        *,
        run_id: str,
        task_id: str,
        dispatch_id: str,
        lease_id: str,
        node_id: str,
        device_ids: tuple[str, ...],
        anchor_revision: int,
        now_ms: int,
    ) -> AttemptSnapshot:
        with self._lock:
            run, task = self._require_task(run_id, task_id)
            if run.status is not RunStatus.RUNNING or task.status is not TaskStatus.QUEUED:
                raise StateTransitionError("attempt requires a queued task in a running run")
            active = self._active_attempt(task)
            if active is not None:
                if active.dispatch_id == dispatch_id:
                    return self._attempt_snapshot(active, task_id)
                raise StateTransitionError("task already has an active attempt")
            attempt_number = task.attempt_count + 1
            attempt = _AttemptRecord(
                attempt=attempt_number,
                dispatch_id=dispatch_id,
                lease_id=lease_id,
                status=AttemptStatus.DISPATCHED,
                dispatched_at_ms=now_ms,
                node_id=node_id,
                device_ids=device_ids,
                anchor_revision=anchor_revision,
            )
            task.attempt_count = attempt_number
            task.attempts.append(attempt)
            task.status = TaskStatus.STARTING
            task.pending_reason = None
            return self._attempt_snapshot(attempt, task_id)

    def worker_started(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        dispatch_id: str,
        now_ms: int,
    ) -> TransitionResult:
        with self._lock:
            run, task, record = self._match_attempt(
                run_id, task_id, attempt, dispatch_id
            )
            if run.status in RUN_TERMINAL or task.status in TASK_TERMINAL:
                return TransitionResult(False)
            if record.status is AttemptStatus.RUNNING:
                return TransitionResult(False)
            if record.status is not AttemptStatus.DISPATCHED:
                return TransitionResult(False)
            if task.status is not TaskStatus.STARTING:
                raise StateTransitionError("WorkerStarted requires starting task")
            record.status = AttemptStatus.RUNNING
            record.worker_started_at_ms = now_ms
            task.status = TaskStatus.RUNNING
            task.started_at_ms = now_ms
            return TransitionResult(True)

    def attempt_succeeded(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        dispatch_id: str,
        now_ms: int,
    ) -> TransitionResult:
        with self._lock:
            run, task, record = self._match_attempt(
                run_id, task_id, attempt, dispatch_id
            )
            if run.status in RUN_TERMINAL or task.status in TASK_TERMINAL:
                return TransitionResult(False)
            if record.status is AttemptStatus.SUCCEEDED:
                return TransitionResult(False)
            if record.status is not AttemptStatus.RUNNING or task.status is not TaskStatus.RUNNING:
                return TransitionResult(False)
            record.status = AttemptStatus.SUCCEEDED
            record.finished_at_ms = now_ms
            task.status = TaskStatus.SUCCEEDED
            task.finished_at_ms = now_ms
            task.pending_reason = None
            ready: list[str] = []
            for successor_id in run.compiled.successors[task_id]:
                successor = run.task_states[successor_id]
                if successor.status is not TaskStatus.PENDING:
                    continue
                successor.remaining_predecessors -= 1
                if successor.remaining_predecessors < 0:
                    raise StateTransitionError("remaining_predecessors became negative")
                if successor.remaining_predecessors == 0:
                    successor.status = TaskStatus.READY
                    successor.ready_at_ms = now_ms
                    ready.append(successor_id)
            terminal = all(
                item.status is TaskStatus.SUCCEEDED
                for item in run.task_states.values()
            )
            if terminal:
                run.status = RunStatus.SUCCEEDED
                run.finished_at_ms = now_ms
            return TransitionResult(
                True,
                tuple(ready),
                run_terminal=terminal,
            )

    def attempt_retry_wait(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        dispatch_id: str,
        attempt_status: AttemptStatus,
        error: ErrorInfo,
        now_ms: int,
    ) -> TransitionResult:
        if attempt_status not in {AttemptStatus.FAILED, AttemptStatus.TIMED_OUT}:
            raise StateTransitionError("retry_wait requires failed or timed_out attempt")
        with self._lock:
            run, task, record = self._match_attempt(
                run_id, task_id, attempt, dispatch_id
            )
            if run.status in RUN_TERMINAL or record.status in ATTEMPT_TERMINAL:
                return TransitionResult(False)
            record.status = attempt_status
            record.finished_at_ms = now_ms
            record.error = error
            task.status = TaskStatus.RETRY_WAIT
            task.finished_at_ms = None
            task.last_error = error
            task.pending_reason = "retry_backoff"
            return TransitionResult(True)

    def retry_eligible(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        now_ms: int,
    ) -> TransitionResult:
        with self._lock:
            run, task = self._require_task(run_id, task_id)
            if run.status in RUN_TERMINAL:
                return TransitionResult(False)
            if task.status is not TaskStatus.RETRY_WAIT or task.attempt_count != attempt:
                return TransitionResult(False)
            task.status = TaskStatus.READY
            task.ready_at_ms = now_ms
            task.pending_reason = None
            return TransitionResult(True, (task_id,))

    def attempt_final_failure(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        dispatch_id: str,
        attempt_status: AttemptStatus,
        task_status: TaskStatus,
        error: ErrorInfo,
        now_ms: int,
    ) -> TransitionResult:
        if attempt_status not in {AttemptStatus.FAILED, AttemptStatus.TIMED_OUT}:
            raise StateTransitionError("final failure requires a failure attempt status")
        if task_status not in {TaskStatus.FAILED, TaskStatus.TIMED_OUT}:
            raise StateTransitionError("final failure requires a failure task status")
        with self._lock:
            run, task, record = self._match_attempt(
                run_id, task_id, attempt, dispatch_id
            )
            if run.status in RUN_TERMINAL or record.status in ATTEMPT_TERMINAL:
                return TransitionResult(False)
            record.status = attempt_status
            record.finished_at_ms = now_ms
            record.error = error
            task.status = task_status
            task.finished_at_ms = now_ms
            task.last_error = error
            run.status = RunStatus.FAILED
            run.finished_at_ms = now_ms
            run.failure = error
            cancelled = self._cancel_other_tasks(
                run,
                except_task_id=task_id,
                reason="upstream_failed",
                now_ms=now_ms,
            )
            return TransitionResult(
                True,
                cancelled_attempts=cancelled,
                run_terminal=True,
            )

    def pre_attempt_final_failure(
        self,
        *,
        run_id: str,
        task_id: str,
        error: ErrorInfo,
        now_ms: int,
    ) -> TransitionResult:
        """Fail a queued task when admission proves it can never be placed."""

        with self._lock:
            run, task = self._require_task(run_id, task_id)
            if run.status in RUN_TERMINAL or task.status in TASK_TERMINAL:
                return TransitionResult(False, run_terminal=run.status in RUN_TERMINAL)
            if task.status is not TaskStatus.QUEUED or task.attempt_count != 0:
                raise StateTransitionError(
                    "pre-attempt failure requires a queued task with no Attempt"
                )
            task.status = TaskStatus.FAILED
            task.finished_at_ms = now_ms
            task.pending_reason = None
            task.last_error = error
            run.status = RunStatus.FAILED
            run.finished_at_ms = now_ms
            run.failure = error
            cancelled = self._cancel_other_tasks(
                run,
                except_task_id=task_id,
                reason="upstream_failed",
                now_ms=now_ms,
            )
            return TransitionResult(
                True,
                cancelled_attempts=cancelled,
                run_terminal=True,
            )

    def terminate_run(
        self,
        *,
        run_id: str,
        target: RunStatus,
        reason: str,
        now_ms: int,
    ) -> TransitionResult:
        if target not in {
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
            RunStatus.INTERRUPTED,
        }:
            raise StateTransitionError("terminate_run target must be a control terminal")
        with self._lock:
            run = self._require_run(run_id)
            if run.status in RUN_TERMINAL:
                return TransitionResult(False, run_terminal=True)
            run.status = target
            run.finished_at_ms = now_ms
            cancelled = self._cancel_other_tasks(
                run,
                except_task_id=None,
                reason=reason,
                now_ms=now_ms,
            )
            return TransitionResult(
                True,
                cancelled_attempts=cancelled,
                run_terminal=True,
            )

    def matches_active_attempt(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        dispatch_id: str,
    ) -> bool:
        with self._lock:
            try:
                run, task, record = self._match_attempt(
                    run_id, task_id, attempt, dispatch_id
                )
            except (KeyError, StateTransitionError):
                return False
            return (
                run.status not in RUN_TERMINAL
                and task.status not in TASK_TERMINAL
                and record.status not in ATTEMPT_TERMINAL
            )

    def snapshot(self, run_id: str) -> RunSnapshot:
        with self._lock:
            run = self._require_run(run_id)
            tasks = tuple(
                self._task_snapshot(run.task_states[task_id])
                for task_id in run.compiled.topological_order
            )
            return RunSnapshot(
                run_id=run.run_id,
                workflow_id=run.workflow_id,
                workflow_fingerprint=run.workflow_fingerprint,
                routing_session_key_hash=run.routing_session_key_hash,
                status=run.status,
                submitted_at_ms=run.submitted_at_ms,
                started_at_ms=run.started_at_ms,
                finished_at_ms=run.finished_at_ms,
                deadline_at_ms=run.deadline_at_ms,
                task_states=tasks,
                failure=run.failure,
            )

    def compiled(self, run_id: str) -> CompiledWorkflow:
        with self._lock:
            return self._require_run(run_id).compiled

    def _require_run(self, run_id: str) -> _RunRecord:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise KeyError(f"unknown run_id: {run_id}") from exc

    def _require_task(self, run_id: str, task_id: str) -> tuple[_RunRecord, _TaskRecord]:
        run = self._require_run(run_id)
        try:
            return run, run.task_states[task_id]
        except KeyError as exc:
            raise KeyError(f"unknown task_id for run {run_id}: {task_id}") from exc

    def _match_attempt(
        self,
        run_id: str,
        task_id: str,
        attempt: int,
        dispatch_id: str,
    ) -> tuple[_RunRecord, _TaskRecord, _AttemptRecord]:
        run, task = self._require_task(run_id, task_id)
        for record in task.attempts:
            if record.attempt == attempt:
                if record.dispatch_id != dispatch_id:
                    raise StateTransitionError(
                        "one attempt cannot use two different dispatch IDs"
                    )
                return run, task, record
        raise StateTransitionError("attempt does not exist")

    @staticmethod
    def _active_attempt(task: _TaskRecord) -> _AttemptRecord | None:
        if not task.attempts:
            return None
        candidate = task.attempts[-1]
        return candidate if candidate.status not in ATTEMPT_TERMINAL else None

    def _cancel_other_tasks(
        self,
        run: _RunRecord,
        *,
        except_task_id: str | None,
        reason: str,
        now_ms: int,
    ) -> tuple[AttemptSnapshot, ...]:
        active: list[AttemptSnapshot] = []
        for task_id, task in run.task_states.items():
            if task_id == except_task_id or task.status in TASK_TERMINAL:
                continue
            attempt = self._active_attempt(task)
            if attempt is not None:
                active.append(self._attempt_snapshot(attempt, task_id))
                attempt.status = AttemptStatus.CANCELLED
                attempt.finished_at_ms = now_ms
            task.status = TaskStatus.CANCELLED
            task.finished_at_ms = now_ms
            task.pending_reason = None
            task.cancellation_reason = reason
        return tuple(active)

    @staticmethod
    def _attempt_snapshot(record: _AttemptRecord, task_id: str) -> AttemptSnapshot:
        return AttemptSnapshot(
            task_id=task_id,
            attempt=record.attempt,
            dispatch_id=record.dispatch_id,
            lease_id=record.lease_id,
            status=record.status,
            dispatched_at_ms=record.dispatched_at_ms,
            worker_started_at_ms=record.worker_started_at_ms,
            finished_at_ms=record.finished_at_ms,
            node_id=record.node_id,
            device_ids=record.device_ids,
            anchor_revision=record.anchor_revision,
            error=record.error,
        )

    def _task_snapshot(self, task: _TaskRecord) -> TaskSnapshot:
        return TaskSnapshot(
            task_id=task.task_id,
            status=task.status,
            remaining_predecessors=task.remaining_predecessors,
            ready_at_ms=task.ready_at_ms,
            queued_at_ms=task.queued_at_ms,
            started_at_ms=task.started_at_ms,
            finished_at_ms=task.finished_at_ms,
            attempt_count=task.attempt_count,
            attempts=tuple(
                self._attempt_snapshot(item, task.task_id) for item in task.attempts
            ),
            pending_reason=task.pending_reason,
            cancellation_reason=task.cancellation_reason,
            last_error=task.last_error,
        )
