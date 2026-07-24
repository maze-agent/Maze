"""Serial C3-C9 coordination shared by Fake and distributed runtimes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from time import perf_counter_ns
from typing import Any, Protocol

from ascend_maze.compiler.ir import (
    CompiledWorkflow,
    DefaultBinding,
    LiteralBinding,
    OutputBinding,
    TaskDefinition,
    WorkflowInputBinding,
)
from ascend_maze.contracts.data import DataHandle
from ascend_maze.contracts.errors import ErrorInfo
from ascend_maze.contracts.recording import (
    ExecutionEvent,
    ExecutionRecorder,
    FlushResult,
    ProducerFlushResult,
    RunRecordingContext,
)
from ascend_maze.contracts.resources import (
    ExecutionTarget,
    PlacementLease,
    ResourceObservation,
)
from ascend_maze.contracts.runtime import (
    CodeHandle,
    DispatchHandle,
    ExecutionRequest,
    ModelRouteLease,
    RuntimeArgument,
    RuntimeBackend,
)
from ascend_maze.core.clock import Clock, SystemClock
from ascend_maze.core.canonical import FrozenMap, freeze_canonical
from ascend_maze.core.errors import (
    RunDataIndexError,
    RunNotTerminalError,
    StateTransitionError,
)
from ascend_maze.core.identifiers import new_id
from ascend_maze.data.index import (
    RunDataIndexCheckpoint,
    RunDataIndexRef,
    RunDataIndexRegistry,
    RunDataTombstone,
)
from ascend_maze.fault import (
    CleanupBarrier,
    ErrorNormalizer,
    FaultIdentity,
    RecoveryAction,
    RecoveryCoordinator,
    RecoveryDecision,
    RecoveryPolicy,
    ReplayabilityChecker,
    ReplayabilityResult,
)
from ascend_maze.lifecycle.deadlines import DeadlineEvent, DeadlineKind, DeadlineManager
from ascend_maze.lifecycle.state import (
    AttemptSnapshot,
    AttemptStatus,
    RunSnapshot,
    RunStateManager,
    RunStatus,
    TaskStatus,
)
from ascend_maze.inference import InferenceCoordinator, ModelRouteLeaseStatus
from ascend_maze.placement.manager import LeaseStatus, NodeStatus, PlacementManager
from ascend_maze.resources.anchors import ResourceAnchorProvider
from ascend_maze.runtime.events import RuntimeEvent, RuntimeEventKind
from ascend_maze.scheduler.contracts import (
    DispatchProposal,
    QueuePartitioner,
    QueueToken,
    RunLifecycleAwarePolicy,
    SchedulableTaskView,
    SchedulingPolicy,
    TaskKey,
)


@dataclass(frozen=True, slots=True)
class DestroyResult:
    run_id: str
    tombstone: RunDataTombstone
    flush_result: FlushResult
    code_handles_released: int


@dataclass(frozen=True, slots=True)
class SchedulerRunRecovery:
    run_id: str
    submission_id: str
    snapshot: RunSnapshot
    index: RunDataIndexCheckpoint
    recording_context: RunRecordingContext
    session_key_hash: str | None
    destroy_result: DestroyResult | None
    expected_producer_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class _RunExecution:
    submission_id: str
    compiled: CompiledWorkflow
    code_handles: tuple[CodeHandle, ...]
    code_by_definition: dict[str, CodeHandle]
    index_ref: RunDataIndexRef
    recording_context: RunRecordingContext
    session_key_hash: str | None
    expected_producer_ids: set[str] = field(default_factory=set)
    destroyed: DestroyResult | None = None


@dataclass(slots=True)
class _QueuedRecord:
    view: SchedulableTaskView
    partition: str


@dataclass(slots=True)
class _BlockedRecord:
    blocked_since_ms: int
    bypass_count: int
    last_reason: str


@dataclass(frozen=True, slots=True)
class QueueTaskSnapshot:
    run_id: str
    task_id: str
    status: TaskStatus
    pending_reason: str | None
    partition: str | None
    queue_generation: int
    blocked_since_ms: int | None
    bypass_count: int


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    snapshot_version: int
    policy_name: str
    policy_version: str
    partitioner_name: str
    tasks: tuple[QueueTaskSnapshot, ...]


@dataclass(frozen=True, slots=True)
class _DispatchRecord:
    handle: DispatchHandle
    lease_id: str
    route_lease: ModelRouteLease | None


@dataclass(slots=True)
class _DispatchStartupRecord:
    request: ExecutionRequest
    lease: PlacementLease
    route_lease: ModelRouteLease | None
    generation: int
    requested_at_ns: int
    task: asyncio.Task[DispatchHandle | None]


@dataclass(frozen=True, slots=True)
class _DispatchPrepared:
    dispatch_id: str
    generation: int
    handle: DispatchHandle


@dataclass(frozen=True, slots=True)
class _DispatchStartFailed:
    dispatch_id: str
    generation: int
    error: BaseException


@dataclass(slots=True)
class _CommitCommand:
    run_id: str
    submission_id: str
    compiled: CompiledWorkflow
    workflow_inputs: dict[str, DataHandle]
    code_handles: tuple[CodeHandle, ...]
    session_key_hash: str | None
    submitted_at_ms: int
    deadline_at_ms: int | None
    recording_context: RunRecordingContext
    future: asyncio.Future[RunDataIndexRef]


@dataclass(slots=True)
class _CancelCommand:
    run_id: str
    target: RunStatus
    reason: str
    future: asyncio.Future[RunSnapshot]


@dataclass(slots=True)
class _DestroyCommand:
    run_id: str
    force: bool
    future: asyncio.Future[DestroyResult]


@dataclass(slots=True)
class _ShutdownCommand:
    terminate_active_runs: bool
    future: asyncio.Future[None]


@dataclass(slots=True)
class _BeginDrainCommand:
    future: asyncio.Future[None]


@dataclass(slots=True)
class _WakeCommand:
    future: asyncio.Future[None]


@dataclass(frozen=True, slots=True)
class _ResourceChanged:
    reason: str
    model_id: str | None = None


@dataclass(frozen=True, slots=True)
class _ModelRouteFailed:
    lease: ModelRouteLease
    reason: str


@dataclass(frozen=True, slots=True)
class _RuntimeBindingInvalidated:
    node_id: str
    boot_id: str
    reason: str


class SchedulerRuntimeBackend(RuntimeBackend, Protocol):
    environment_fingerprint: str

    def set_event_sink(self, sink: Callable[[RuntimeEvent], None]) -> None: ...

    def dispatch_invalidated(self, dispatch_id: str) -> bool: ...

    def worker_released(self, dispatch_id: str) -> bool: ...

    def producer_for_lease(self, lease: PlacementLease) -> str | None: ...

    def producer_is_persistent(self, lease: PlacementLease) -> bool: ...

    async def prepare_run_recording(
        self,
        context: RunRecordingContext,
        lease: PlacementLease,
    ) -> None: ...

    async def flush_run_recorders(
        self,
        run_id: str,
        timeout_ms: int,
    ) -> tuple[ProducerFlushResult, ...]: ...

    async def release_run(self, run_id: str) -> int: ...


class SchedulerCore:
    """One event-loop authority for lifecycle, deadlines, queues and leases."""

    def __init__(
        self,
        *,
        state: RunStateManager,
        deadlines: DeadlineManager,
        indexes: RunDataIndexRegistry,
        anchors: ResourceAnchorProvider,
        placement: PlacementManager,
        runtime: SchedulerRuntimeBackend,
        recorder: ExecutionRecorder,
        policy: SchedulingPolicy,
        partitioner: QueuePartitioner,
        clock: Clock | None = None,
        placement_lookahead: int = 8,
        max_bypass_count: int = 8,
        dispatch_timeout_ms: int = 5_000,
        recorder_flush_timeout_ms: int = 1_000,
        controller_producer_id: str = "controller",
        recovery: RecoveryPolicy | None = None,
        recovery_coordinator: RecoveryCoordinator | None = None,
        error_normalizer: ErrorNormalizer | None = None,
        replayability: ReplayabilityChecker | None = None,
        inference: InferenceCoordinator | None = None,
        checkpoint_sink: Callable[[], Awaitable[None]] | None = None,
        control_event_sink: Callable[[ExecutionEvent], None] | None = None,
    ) -> None:
        if placement_lookahead < 1:
            raise ValueError("placement_lookahead must be positive")
        if max_bypass_count < 0:
            raise ValueError("max_bypass_count must be non-negative")
        if policy.capabilities.requires_prediction:
            raise ValueError("this SchedulerCore has no task-time prediction provider")
        if policy.capabilities.uses_cluster_snapshot:
            raise ValueError("this SchedulerCore does not expose cluster snapshots to policies")
        self.state = state
        self.deadlines = deadlines
        self.indexes = indexes
        self.anchors = anchors
        self.placement = placement
        self.runtime = runtime
        self.recorder = recorder
        self.policy = policy
        self.partitioner = partitioner
        self.clock = clock or SystemClock()
        self.placement_lookahead = placement_lookahead
        self.max_bypass_count = max_bypass_count
        self.dispatch_timeout_ms = dispatch_timeout_ms
        self.recorder_flush_timeout_ms = recorder_flush_timeout_ms
        self.controller_producer_id = controller_producer_id
        self.recovery = recovery or RecoveryPolicy()
        self.recovery_coordinator = recovery_coordinator or RecoveryCoordinator()
        self.error_normalizer = error_normalizer or ErrorNormalizer()
        self.replayability = replayability or ReplayabilityChecker(indexes.data_store)
        self.inference = inference
        self._checkpoint_sink = checkpoint_sink
        self._control_event_sink = control_event_sink
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._runner: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._dispatch_enabled = False
        self._runs: dict[str, _RunExecution] = {}
        self._queued: dict[TaskKey, _QueuedRecord] = {}
        self._blocked: dict[TaskKey, _BlockedRecord] = {}
        self._queue_generations: dict[TaskKey, int] = {}
        self._enqueue_sequence = 0
        self._partition_cursor = 0
        self._dispatches: dict[str, _DispatchRecord] = {}
        self._pending_dispatches: dict[str, _DispatchStartupRecord] = {}
        self._dispatch_startup_generation = 0
        self._pending_resource_change_keys: set[str | None] = set()
        self._attempt_routes: dict[tuple[str, str, int], ModelRouteLease] = {}
        self._recorded_inference_routes: set[str] = set()
        self._recorded_route_terminals: set[str] = set()
        self._seen_runtime_events: dict[str, str] = {}
        self._producer_sequence = 0
        self._terminal_waiters: dict[str, list[asyncio.Future[RunSnapshot]]] = {}
        self._recovered_runs: set[str] = set()
        self._recording_flushes: dict[str, FlushResult] = {}

    async def start(self) -> None:
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        self.runtime.set_event_sink(self.post_runtime_event)
        await self.runtime.start()
        if self.inference is not None:
            self.inference.set_capacity_sink(self.post_resource_changed)
            self.inference.set_route_failure_sink(self.post_model_route_failure)
            await self.inference.start()
        self._running = True
        self._dispatch_enabled = True
        self._runner = asyncio.create_task(self._run_loop())

    @property
    def running(self) -> bool:
        return self._running

    @property
    def dispatch_enabled(self) -> bool:
        return self._dispatch_enabled

    def set_checkpoint_sink(
        self,
        sink: Callable[[], Awaitable[None]] | None,
    ) -> None:
        if self._running:
            raise RuntimeError("checkpoint sink must be configured before start")
        self._checkpoint_sink = sink

    def restore_run(
        self,
        *,
        compiled: CompiledWorkflow,
        code_handles: tuple[CodeHandle, ...],
        recovery: SchedulerRunRecovery,
    ) -> RunDataIndexRef:
        if self._running:
            raise RuntimeError("runs must be restored before Scheduler start")
        self.state.restore_run(compiled=compiled, snapshot=recovery.snapshot)
        index = self.indexes.restore(recovery.index)
        self._runs[recovery.run_id] = _RunExecution(
            submission_id=recovery.submission_id,
            compiled=compiled,
            code_handles=code_handles,
            code_by_definition={item.definition_id: item for item in code_handles},
            index_ref=index.reference,
            recording_context=recovery.recording_context,
            session_key_hash=recovery.session_key_hash,
            expected_producer_ids=set(recovery.expected_producer_ids),
            destroyed=recovery.destroy_result,
        )
        for producer_id in recovery.expected_producer_ids:
            if producer_id in recovery.recording_context.initial_expected_producer_ids:
                continue
            try:
                self.recorder.expect_producer(recovery.run_id, producer_id)
            except Exception as exc:
                self._record_recorder_error(recovery.run_id, exc)
        self._recovered_runs.add(recovery.run_id)
        return index.reference

    def bind_recovered_code(
        self,
        run_id: str,
        code_handles: tuple[CodeHandle, ...],
    ) -> None:
        execution = self._runs[run_id]
        if execution.destroyed is not None:
            if code_handles:
                raise RuntimeError("destroyed recovered run cannot own CodeHandles")
            return
        execution.code_handles = code_handles
        execution.code_by_definition = {
            item.definition_id: item for item in code_handles
        }

    async def reconcile_recovered_runs(self, previous_generation: str) -> None:
        if not self._running:
            raise RuntimeError("Scheduler must be started before reconciliation")
        now = self.clock.monotonic_ms()
        for run_id in sorted(self._recovered_runs):
            snapshot = self.state.snapshot(run_id)
            if not snapshot.terminal:
                self.state.terminate_run(
                    run_id=run_id,
                    target=RunStatus.INTERRUPTED,
                    reason="controller_generation_changed",
                    now_ms=now,
                )
                self._record(
                    run_id,
                    "run_recovered_interrupted",
                    payload={"previous_controller_generation": previous_generation},
                )
                await self._on_run_terminal(run_id)
        self._recovered_runs.clear()
        await self._checkpoint()

    def recovery_runs(self) -> tuple[SchedulerRunRecovery, ...]:
        return tuple(
            SchedulerRunRecovery(
                run_id=run_id,
                submission_id=execution.submission_id,
                snapshot=self.state.snapshot(run_id),
                index=self.indexes.get(run_id).checkpoint(),
                recording_context=execution.recording_context,
                session_key_hash=execution.session_key_hash,
                destroy_result=execution.destroyed,
                expected_producer_ids=tuple(sorted(execution.expected_producer_ids)),
            )
            for run_id, execution in sorted(self._runs.items())
        )

    async def commit_run(
        self,
        *,
        run_id: str,
        submission_id: str,
        compiled: CompiledWorkflow,
        workflow_inputs: dict[str, DataHandle],
        code_handles: tuple[CodeHandle, ...],
        session_key_hash: str | None,
        submitted_at_ms: int,
        deadline_at_ms: int | None,
        recording_context: RunRecordingContext,
    ) -> RunDataIndexRef:
        future: asyncio.Future[RunDataIndexRef] = self._new_future()
        await self._queue.put(
            _CommitCommand(
                run_id,
                submission_id,
                compiled,
                workflow_inputs,
                code_handles,
                session_key_hash,
                submitted_at_ms,
                deadline_at_ms,
                recording_context,
                future,
            )
        )
        return await asyncio.shield(future)

    async def cancel_run(self, run_id: str, *, reason: str) -> RunSnapshot:
        future: asyncio.Future[RunSnapshot] = self._new_future()
        await self._queue.put(
            _CancelCommand(run_id, RunStatus.CANCELLED, reason, future)
        )
        return await asyncio.shield(future)

    async def destroy_run(self, run_id: str, *, force: bool = False) -> DestroyResult:
        future: asyncio.Future[DestroyResult] = self._new_future()
        await self._queue.put(_DestroyCommand(run_id, force, future))
        return await asyncio.shield(future)

    async def wake_deadlines(self) -> None:
        future: asyncio.Future[None] = self._new_future()
        await self._queue.put(_WakeCommand(future))
        await asyncio.shield(future)

    def post_resource_changed(
        self,
        reason: str,
        model_id: str | None = None,
    ) -> bool:
        """Wake queued placement after an authoritative cluster resource change."""

        if not reason:
            raise ValueError("resource change reason is required")
        if self._loop is None or not self._running:
            return False
        event = _ResourceChanged(reason, model_id)

        def enqueue() -> None:
            key = event.model_id
            if key in self._pending_resource_change_keys:
                return
            self._pending_resource_change_keys.add(key)
            self._queue.put_nowait(event)

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is self._loop:
            enqueue()
        else:
            self._loop.call_soon_threadsafe(enqueue)
        return True

    def post_model_route_failure(
        self,
        lease: ModelRouteLease,
        reason: str,
    ) -> bool:
        if not reason or self._loop is None or not self._running:
            return False
        event = _ModelRouteFailed(lease, reason)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is self._loop:
            self._queue.put_nowait(event)
        else:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
        return True

    def post_runtime_binding_invalidated(
        self,
        node_id: str,
        boot_id: str,
        *,
        reason: str,
    ) -> bool:
        if (
            not node_id
            or not boot_id
            or not reason
            or self._loop is None
            or not self._running
        ):
            return False
        event = _RuntimeBindingInvalidated(node_id, boot_id, reason)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is self._loop:
            self._queue.put_nowait(event)
        else:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
        return True

    async def wait_terminal(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> RunSnapshot:
        snapshot = self.state.snapshot(run_id)
        if snapshot.terminal:
            return snapshot
        future: asyncio.Future[RunSnapshot] = self._new_future()
        self._terminal_waiters.setdefault(run_id, []).append(future)
        if timeout_seconds is None:
            return await future
        return await asyncio.wait_for(future, timeout_seconds)

    async def begin_draining(self) -> None:
        if not self._running or not self._dispatch_enabled:
            return
        future: asyncio.Future[None] = self._new_future()
        await self._queue.put(_BeginDrainCommand(future))
        await asyncio.shield(future)

    async def shutdown(self, *, terminate_active_runs: bool = True) -> None:
        if not self._running:
            return
        future: asyncio.Future[None] = self._new_future()
        await self._queue.put(_ShutdownCommand(terminate_active_runs, future))
        await asyncio.shield(future)
        assert self._runner is not None
        await self._runner
        self._runner = None

    def run_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._runs))

    def recordable_run_ids(self) -> tuple[str, ...]:
        return tuple(
            run_id
            for run_id, execution in sorted(self._runs.items())
            if execution.destroyed is None
        )

    def nonterminal_run_ids(self) -> tuple[str, ...]:
        return tuple(
            run_id
            for run_id in sorted(self._runs)
            if not self.state.snapshot(run_id).terminal
        )

    def active_route_lease_ids(self) -> tuple[str, ...]:
        if self.inference is None:
            return ()
        return tuple(
            sorted(
                route.route_lease_id
                for route in self._attempt_routes.values()
                if self.inference.route_snapshot(route.route_lease_id).status
                in {
                    ModelRouteLeaseStatus.RESERVED,
                    ModelRouteLeaseStatus.ACTIVE,
                }
            )
        )

    def active_dispatch_ids(self) -> tuple[str, ...]:
        active = set(self._pending_dispatches)
        active.update(
            dispatch_id
            for dispatch_id in self._dispatches
            if not self.runtime.dispatch_invalidated(dispatch_id)
            or not self.runtime.worker_released(dispatch_id)
        )
        return tuple(sorted(active))

    def pending_dispatch_count(self, run_id: str | None = None) -> int:
        return sum(
            run_id is None or record.request.run_id == run_id
            for record in self._pending_dispatches.values()
        )

    def record_control_event(
        self,
        run_id: str,
        event_type: str,
        *,
        task_id: str | None = None,
        attempt: int | None = None,
        lease_id: str | None = None,
        route_lease_id: str | None = None,
        model_instance_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        if run_id in self._runs and run_id not in self._recording_flushes:
            self._record(
                run_id,
                event_type,
                task_id=task_id,
                attempt=attempt,
                lease_id=lease_id,
                route_lease_id=route_lease_id,
                model_instance_id=model_instance_id,
                payload=payload,
            )

    async def flush_run_recording(self, run_id: str) -> FlushResult:
        cached = self._recording_flushes.get(run_id)
        if cached is not None:
            return cached
        flush_started = perf_counter_ns()
        try:
            producer_results = await self.runtime.flush_run_recorders(
                run_id, self.recorder_flush_timeout_ms
            )
        except Exception as exc:
            producer_results = ()
            self._record_recorder_error(run_id, exc)
        for producer in producer_results:
            if producer.result is not None:
                try:
                    self.recorder.merge_producer_flush(
                        run_id, producer.producer_id, producer.result
                    )
                except Exception as exc:
                    self._record_recorder_error(run_id, exc)
            else:
                self._record_recorder_error(
                    run_id,
                    RuntimeError(
                        f"producer {producer.producer_id} flush failed: {producer.error}"
                    ),
                )
        elapsed_ms = max(0, (perf_counter_ns() - flush_started) // 1_000_000)
        remaining_ms = max(1, self.recorder_flush_timeout_ms - elapsed_ms)
        result = await self.recorder.flush_run(run_id, remaining_ms)
        self._recording_flushes[run_id] = result
        return result

    def recording_result(self, run_id: str) -> FlushResult | None:
        if run_id not in self._runs:
            raise KeyError(run_id)
        return self._recording_flushes.get(run_id)

    async def abandon(self) -> None:
        """Stop this authority without mutating persisted Run/resource state."""

        if not self._running:
            return
        self._checkpoint_sink = None
        self._dispatch_enabled = False
        self._running = False
        runner = self._runner
        self._runner = None
        if runner is not None:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        pending_ids = tuple(self._pending_dispatches)
        for dispatch_id in pending_ids:
            await self._cancel_pending_dispatch(
                dispatch_id,
                "controller_generation_abandoned",
            )
        if self.inference is not None:
            await self.inference.abandon()
        await self.runtime.close()

    def snapshot(self, run_id: str) -> RunSnapshot:
        return self.state.snapshot(run_id)

    def queue_snapshot(self, *, snapshot_version: int) -> QueueSnapshot:
        tasks: list[QueueTaskSnapshot] = []
        for run in self.state.snapshots():
            for task in run.task_states:
                if task.status not in {
                    TaskStatus.READY,
                    TaskStatus.QUEUED,
                    TaskStatus.STARTING,
                    TaskStatus.RETRY_WAIT,
                }:
                    continue
                key = TaskKey(run.run_id, task.task_id)
                queued = self._queued.get(key)
                blocked = self._blocked.get(key)
                tasks.append(
                    QueueTaskSnapshot(
                        run_id=run.run_id,
                        task_id=task.task_id,
                        status=task.status,
                        pending_reason=task.pending_reason,
                        partition=None if queued is None else queued.partition,
                        queue_generation=self._queue_generations.get(key, 0),
                        blocked_since_ms=(
                            None if blocked is None else blocked.blocked_since_ms
                        ),
                        bypass_count=0 if blocked is None else blocked.bypass_count,
                    )
                )
        return QueueSnapshot(
            snapshot_version=snapshot_version,
            policy_name=self.policy.name,
            policy_version=self.policy.version,
            partitioner_name=self.partitioner.name,
            tasks=tuple(sorted(tasks, key=lambda item: (item.run_id, item.task_id))),
        )

    def run_index_ref(self, run_id: str) -> RunDataIndexRef:
        return self._runs[run_id].index_ref

    def result(self, run_id: str, task_id: str) -> dict[str, object]:
        execution = self._runs[run_id]
        definition_id = execution.compiled.tasks[task_id].definition_id
        output_names = execution.compiled.definitions[definition_id].output_names
        index = self.indexes.get(run_id)
        return index.read_task_result(
            task_id,
            output_names,
            controller_generation=execution.index_ref.controller_generation,
            index_generation=execution.index_ref.index_generation,
        )

    def post_runtime_event(self, event: RuntimeEvent) -> None:
        if self._loop is None or not self._running:
            for _, handle in event.output_handles:
                try:
                    self.indexes.data_store.release(handle)
                except Exception:
                    pass
            return
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is self._loop:
            self._queue.put_nowait(event)
        else:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def _run_loop(self) -> None:
        while self._running:
            item: object | None = None
            timeout = self._next_wait_seconds()
            try:
                if timeout is None:
                    item = await self._queue.get()
                else:
                    item = await asyncio.wait_for(self._queue.get(), timeout)
            except asyncio.TimeoutError:
                pass
            try:
                if item is not None:
                    await self._process_item(item)
                processed_deadlines = await self._process_due_deadlines()
                checkpoint_item = not isinstance(item, _ResourceChanged)
                if self._running and self._dispatch_enabled:
                    if checkpoint_item or processed_deadlines:
                        await self._checkpoint()
                    await self._dispatch_pass()
                if checkpoint_item or processed_deadlines:
                    await self._checkpoint()
            except Exception as exc:
                await self._interrupt_after_scheduler_failure(exc)
                if self._checkpoint_sink is not None:
                    self._running = False

    async def _checkpoint(self) -> None:
        if self._checkpoint_sink is not None:
            await self._checkpoint_sink()

    def _next_wait_seconds(self) -> float | None:
        if not self.clock.automatic_wait:
            return None
        next_due = self.deadlines.next_due_at_ms()
        if next_due is None:
            return None
        return max(0.0, (next_due - self.clock.monotonic_ms()) / 1000)

    async def _process_item(self, item: object) -> None:
        try:
            if isinstance(item, _CommitCommand):
                commit_result = await self._commit(item)
                await self._checkpoint()
                self._complete_future(item.future, commit_result)
                self._activate_run(item.run_id)
            elif isinstance(item, _CancelCommand):
                cancel_result = await self._terminate_run(
                    item.run_id, item.target, item.reason
                )
                await self._checkpoint()
                self._complete_future(item.future, cancel_result)
            elif isinstance(item, _DestroyCommand):
                destroy_result = await self._destroy(item.run_id, force=item.force)
                await self._checkpoint()
                self._complete_future(item.future, destroy_result)
            elif isinstance(item, _WakeCommand):
                self._complete_future(item.future, None)
            elif isinstance(item, _ResourceChanged):
                self._pending_resource_change_keys.discard(item.model_id)
                if self.inference is not None:
                    self.inference.replicas.wake()
                affected_run_ids: set[str] = set()
                for key in self._queued:
                    anchor = self._runs[key.run_id].compiled.tasks[
                        key.task_id
                    ].model_anchor
                    if item.model_id is None or (
                        anchor is not None and anchor.model == item.model_id
                    ):
                        affected_run_ids.add(key.run_id)
                for run_id in sorted(affected_run_ids):
                    self._record(
                        run_id,
                        "resource_changed",
                        payload={
                            "reason": item.reason,
                            "model_id": item.model_id,
                        },
                    )
            elif isinstance(item, _ModelRouteFailed):
                await self._handle_model_route_failure(item)
            elif isinstance(item, _DispatchPrepared):
                await self._dispatch_prepared(item)
            elif isinstance(item, _DispatchStartFailed):
                await self._dispatch_start_failed(item)
            elif isinstance(item, _RuntimeBindingInvalidated):
                await self._runtime_binding_invalidated(item)
            elif isinstance(item, _BeginDrainCommand):
                self._dispatch_enabled = False
                self._complete_future(item.future, None)
            elif isinstance(item, _ShutdownCommand):
                self._dispatch_enabled = False
                if item.terminate_active_runs:
                    await self._shutdown_active_runs()
                elif self.nonterminal_run_ids():
                    raise RuntimeError(
                        "cannot stop SchedulerCore while Runs are nonterminal"
                    )
                if self.inference is not None:
                    await self.inference.close()
                    self.inference.set_capacity_sink(None)
                    self.inference.set_route_failure_sink(None)
                self._running = False
                self._complete_future(item.future, None)
            elif isinstance(item, RuntimeEvent):
                await self._handle_runtime_event(item)
            else:
                raise TypeError(f"unsupported SchedulerCore item: {type(item).__name__}")
        except Exception as exc:
            future = getattr(item, "future", None)
            if future is not None:
                if not future.done():
                    future.set_exception(exc)
                elif not future.cancelled():
                    raise
            elif isinstance(item, RuntimeEvent):
                await self._fail_internal_runtime_event(item, exc)
            else:
                raise

    async def _commit(self, command: _CommitCommand) -> RunDataIndexRef:
        if command.run_id in self._runs:
            raise RuntimeError(f"run already committed: {command.run_id}")
        self.state.create_run(
            run_id=command.run_id,
            compiled=command.compiled,
            routing_session_key_hash=command.session_key_hash,
            submitted_at_ms=command.submitted_at_ms,
            deadline_at_ms=command.deadline_at_ms,
        )
        total_value_tasks = sum(
            command.compiled.definitions[node.definition_id].task_kind == "npu"
            for node in command.compiled.tasks.values()
        )
        try:
            if isinstance(self.policy, RunLifecycleAwarePolicy):
                self.policy.register_run(
                    run_id=command.run_id,
                    submitted_at_ms=command.submitted_at_ms,
                    total_value_tasks=total_value_tasks,
                )
        except Exception:
            self.state.remove_submitted_run(command.run_id)
            raise
        try:
            index = await asyncio.to_thread(
                self.indexes.create_and_adopt,
                run_id=command.run_id,
                workflow_inputs=command.workflow_inputs,
            )
        except Exception:
            if isinstance(self.policy, RunLifecycleAwarePolicy):
                self.policy.unregister_run(command.run_id)
            self.state.remove_submitted_run(command.run_id)
            raise
        code_by_definition = {item.definition_id: item for item in command.code_handles}
        execution = _RunExecution(
            submission_id=command.submission_id,
            compiled=command.compiled,
            code_handles=command.code_handles,
            code_by_definition=code_by_definition,
            index_ref=index.reference,
            recording_context=command.recording_context,
            session_key_hash=command.session_key_hash,
            expected_producer_ids=set(
                command.recording_context.initial_expected_producer_ids
            ),
        )
        self._runs[command.run_id] = execution
        if command.deadline_at_ms is not None:
            self.deadlines.register(
                kind=DeadlineKind.RUN,
                run_id=command.run_id,
                due_at_ms=command.deadline_at_ms,
            )
        return index.reference

    def _activate_run(self, run_id: str) -> None:
        started = self.state.start_run(run_id, self.clock.monotonic_ms())
        self._record(run_id, "run_submitted")
        for task_id in started.ready_task_ids:
            self._enqueue_ready(run_id, task_id)

    def _enqueue_ready(self, run_id: str, task_id: str) -> None:
        execution = self._runs[run_id]
        task_snapshot = self.state.snapshot(run_id).task(task_id)
        if task_snapshot.status is not TaskStatus.READY:
            return
        anchor = self.anchors.resolve(
            run_id=run_id,
            compiled=execution.compiled,
            task_id=task_id,
        )
        node = execution.compiled.tasks[task_id]
        if (
            anchor.execution_target is ExecutionTarget.MODEL_SERVICE
            and self.inference is not None
        ):
            if node.model_anchor is None:
                raise StateTransitionError(
                    "model service Task is missing its compiled model anchor"
                )
            self.inference.register_demand(
                run_id=run_id,
                task_id=task_id,
                model_id=node.model_anchor.model,
            )
        now = self.clock.monotonic_ms()
        if not self.state.mark_queued(run_id, task_id, now):
            return
        key = TaskKey(run_id, task_id)
        generation = self._queue_generations.get(key, 0) + 1
        self._queue_generations[key] = generation
        self._enqueue_sequence += 1
        queued_snapshot = self.state.snapshot(run_id).task(task_id)
        token = QueueToken(key, generation)
        view = SchedulableTaskView(
            queue_token=token,
            task_kind=anchor.task_kind,
            ready_at_ms=queued_snapshot.ready_at_ms or now,
            queued_at_ms=queued_snapshot.queued_at_ms or now,
            enqueue_sequence=self._enqueue_sequence,
            depth_from_entry=execution.compiled.depth_from_entry[task_id],
            depth_to_exit=execution.compiled.depth_to_exit[task_id],
            resource_anchor=anchor,
        )
        partition = self.partitioner.partition(view)
        self._queued[key] = _QueuedRecord(view, partition)
        self.policy.enqueue(partition, view)
        self._record(run_id, "task_queued", task_id=task_id)

    async def _dispatch_pass(self) -> None:
        if not self._queued:
            return
        partitions = sorted({record.partition for record in self._queued.values()})
        if not partitions:
            return
        progress = True
        while progress and self._queued:
            progress = False
            ordered = partitions[
                self._partition_cursor % len(partitions) :
            ] + partitions[: self._partition_cursor % len(partitions)]
            self._partition_cursor = (self._partition_cursor + 1) % len(partitions)
            for partition in ordered:
                policy_started_ns = perf_counter_ns()
                proposals = self.policy.propose(partition, self.placement_lookahead)
                policy_elapsed_ms = (
                    perf_counter_ns() - policy_started_ns
                ) / 1_000_000
                policy_select_ms = max(
                    0.0,
                    policy_elapsed_ms
                    - sum(proposal.score_compute_ms for proposal in proposals),
                )
                blocked_before: list[TaskKey] = []
                for proposal_rank, proposal in enumerate(proposals, start=1):
                    key = proposal.task_key
                    queued = self._queued.get(key)
                    if (
                        queued is None
                        or queued.view.queue_token.queue_generation
                        != proposal.queue_generation
                    ):
                        continue
                    anchor = queued.view.resource_anchor
                    task_snapshot = self.state.snapshot(key.run_id).task(key.task_id)
                    next_attempt = task_snapshot.attempt_count + 1
                    now = self.clock.monotonic_ms()
                    route_lease: ModelRouteLease | None = None
                    if anchor.execution_target is ExecutionTarget.MODEL_SERVICE:
                        execution = self._runs[key.run_id]
                        node = execution.compiled.tasks[key.task_id]
                        if self.inference is None or node.model_anchor is None:
                            route_result = None
                            reason = "model_route_unavailable"
                        else:
                            try:
                                route_result = await self.inference.acquire_route(
                                    run_id=key.run_id,
                                    task_id=key.task_id,
                                    attempt=next_attempt,
                                    model_id=node.model_anchor.model,
                                    session_key_hash=execution.session_key_hash,
                                    dispatch_deadline_ms=(
                                        now + self.dispatch_timeout_ms
                                    ),
                                )
                            except Exception as exc:
                                route_result = None
                                reason = "model_route_unavailable"
                                self._record(
                                    key.run_id,
                                    "model_route_acquire_failed",
                                    task_id=key.task_id,
                                    attempt=next_attempt,
                                    payload={
                                        "exception_type": type(exc).__name__,
                                        "message": str(exc),
                                    },
                                )
                            else:
                                reason = (
                                    route_result.rejection_reason
                                    or "model_route_unavailable"
                                )
                                route_lease = route_result.lease
                                if route_lease is not None:
                                    self._record(
                                        key.run_id,
                                        "model_route_reserved",
                                        task_id=key.task_id,
                                        attempt=next_attempt,
                                        route_lease_id=route_lease.route_lease_id,
                                        model_instance_id=route_lease.instance_id,
                                        payload={
                                            "model_id": route_lease.model_id,
                                            "catalog_revision": (
                                                route_lease.catalog_revision
                                            ),
                                            "instance_generation": (
                                                route_lease.instance_generation
                                            ),
                                            "affinity_hit": route_result.affinity_hit,
                                        },
                                    )
                        if route_lease is None:
                            self.state.set_pending_reason(
                                key.run_id, key.task_id, reason
                            )
                            self._mark_blocked(key, reason)
                            self._record_scheduling_decision(
                                run_id=key.run_id,
                                task_id=key.task_id,
                                partition=partition,
                                proposal=proposal,
                                proposal_rank=proposal_rank,
                                placement_selected=False,
                                pending_reason=reason,
                                policy_select_ms=policy_select_ms,
                                placement_ms=0.0,
                            )
                            blocked_before.append(key)
                            if self._bypass_exhausted(key):
                                break
                            continue
                    placement_started_ns = perf_counter_ns()
                    placement = self.placement.try_reserve(
                        run_id=key.run_id,
                        task_id=key.task_id,
                        attempt=next_attempt,
                        anchor=anchor,
                        now_ms=now,
                        dispatch_deadline_ms=now + self.dispatch_timeout_ms,
                        preferred_node_id=(
                            None
                            if route_lease is None
                            else route_lease.instance_node_id
                        ),
                    )
                    placement_ms = (perf_counter_ns() - placement_started_ns) / 1_000_000
                    if not placement.selected:
                        if route_lease is not None:
                            assert self.inference is not None
                            self.inference.abandon_route(
                                route_lease,
                                reason=(
                                    placement.rejection_reason
                                    or "client_placement_unavailable"
                                ),
                            )
                            self._record_route_terminal(route_lease)
                        if (
                            placement.rejection_reason
                            == "resource_request_unsatisfiable"
                        ):
                            self.policy.depart(queued.view.queue_token)
                            del self._queued[key]
                            self._blocked.pop(key, None)
                            self._record_scheduling_decision(
                                run_id=key.run_id,
                                task_id=key.task_id,
                                partition=partition,
                                proposal=proposal,
                                proposal_rank=proposal_rank,
                                placement_selected=False,
                                pending_reason=placement.rejection_reason,
                                policy_select_ms=policy_select_ms,
                                placement_ms=placement_ms,
                            )
                            await self._fail_pre_attempt_unsatisfiable(
                                key.run_id,
                                key.task_id,
                                anchor.effective.npu_mem_mb,
                            )
                            await self._checkpoint()
                            progress = True
                            break
                        reason = placement.rejection_reason or "placement_unavailable"
                        self.state.set_pending_reason(
                            key.run_id, key.task_id, reason
                        )
                        self._mark_blocked(key, reason)
                        self._record_scheduling_decision(
                            run_id=key.run_id,
                            task_id=key.task_id,
                            partition=partition,
                            proposal=proposal,
                            proposal_rank=proposal_rank,
                            placement_selected=False,
                            pending_reason=reason,
                            policy_select_ms=policy_select_ms,
                            placement_ms=placement_ms,
                        )
                        blocked_before.append(key)
                        if self._bypass_exhausted(key):
                            break
                        continue
                    assert placement.lease is not None
                    if route_lease is not None:
                        self._attempt_routes[
                            (key.run_id, key.task_id, next_attempt)
                        ] = route_lease
                    self._record_scheduling_decision(
                        run_id=key.run_id,
                        task_id=key.task_id,
                        partition=partition,
                        proposal=proposal,
                        proposal_rank=proposal_rank,
                        placement_selected=True,
                        pending_reason=None,
                        policy_select_ms=policy_select_ms,
                        placement_ms=placement_ms,
                    )
                    await self._expect_runtime_producer(key.run_id, placement.lease)
                    dispatch_id = new_id("dispatch")
                    attempt = self.state.create_attempt(
                        run_id=key.run_id,
                        task_id=key.task_id,
                        dispatch_id=dispatch_id,
                        lease_id=placement.lease.lease_id,
                        node_id=placement.lease.node_id,
                        device_ids=(
                            ()
                            if placement.lease.npu_device_id is None
                            else (placement.lease.npu_device_id,)
                        ),
                        anchor_revision=anchor.revision,
                        now_ms=now,
                    )
                    self.policy.depart(queued.view.queue_token)
                    del self._queued[key]
                    self._blocked.pop(key, None)
                    request = self._build_request(
                        key.run_id,
                        key.task_id,
                        attempt.attempt,
                        dispatch_id,
                        route_lease,
                    )
                    self._record(
                        key.run_id,
                        "task_dispatched",
                        task_id=key.task_id,
                        attempt=attempt.attempt,
                        lease_id=placement.lease.lease_id,
                        route_lease_id=(
                            None
                            if route_lease is None
                            else route_lease.route_lease_id
                        ),
                        model_instance_id=(
                            None if route_lease is None else route_lease.instance_id
                        ),
                        payload={
                            "dispatch_id": dispatch_id,
                            "node_id": placement.lease.node_id,
                            "affinity_hit": placement.affinity_hit,
                            "input_object_refs": tuple(
                                {
                                    "input_name": argument.name,
                                    "data_handle_id": argument.data_handle.staged_handle_id,
                                    "object_ref_id": argument.data_handle.metadata.get(
                                        "ray_object_ref_id"
                                    ),
                                }
                                for argument in request.arguments
                                if argument.kind == "data_handle"
                                and argument.data_handle is not None
                                and isinstance(
                                    argument.data_handle.metadata.get(
                                        "ray_object_ref_id"
                                    ),
                                    str,
                                )
                            ),
                            "model_id": (
                                None if route_lease is None else route_lease.model_id
                            ),
                            "instance_generation": (
                                None
                                if route_lease is None
                                else route_lease.instance_generation
                            ),
                        },
                    )
                    self.deadlines.register(
                        kind=DeadlineKind.LEASE,
                        run_id=key.run_id,
                        task_id=key.task_id,
                        attempt=attempt.attempt,
                        due_at_ms=placement.lease.dispatch_deadline_ms,
                    )
                    # Persist the Attempt and Lease before starting an external Worker.
                    await self._checkpoint()
                    self._start_pending_dispatch(
                        request=request,
                        lease=placement.lease,
                        route_lease=route_lease,
                    )
                    for blocked_key in blocked_before:
                        blocked = self._blocked.get(blocked_key)
                        if blocked is not None and blocked_key in self._queued:
                            blocked.bypass_count += 1
                    progress = True
                    break

    def _start_pending_dispatch(
        self,
        *,
        request: ExecutionRequest,
        lease: PlacementLease,
        route_lease: ModelRouteLease | None,
    ) -> None:
        self._dispatch_startup_generation += 1
        generation = self._dispatch_startup_generation
        task = asyncio.create_task(
            self._prepare_dispatch(request, lease, generation),
            name=f"maze-dispatch-start:{request.dispatch_id}",
        )
        self._pending_dispatches[request.dispatch_id] = _DispatchStartupRecord(
            request=request,
            lease=lease,
            route_lease=route_lease,
            generation=generation,
            requested_at_ns=perf_counter_ns(),
            task=task,
        )

    async def _prepare_dispatch(
        self,
        request: ExecutionRequest,
        lease: PlacementLease,
        generation: int,
    ) -> DispatchHandle | None:
        try:
            handle = await self.runtime.dispatch(request, lease)
        except BaseException as exc:
            self._queue.put_nowait(
                _DispatchStartFailed(request.dispatch_id, generation, exc)
            )
            return None
        self._queue.put_nowait(
            _DispatchPrepared(request.dispatch_id, generation, handle)
        )
        return handle

    async def _dispatch_prepared(self, event: _DispatchPrepared) -> None:
        pending = self._pending_dispatches.get(event.dispatch_id)
        if pending is None or pending.generation != event.generation:
            await self.runtime.cancel(event.handle, "late_dispatch_prepared")
            return
        del self._pending_dispatches[event.dispatch_id]
        request = pending.request
        handle = event.handle
        if (
            handle.dispatch_id != request.dispatch_id
            or handle.run_id != request.run_id
            or handle.task_id != request.task_id
            or handle.attempt != request.attempt
            or handle.lease_id != pending.lease.lease_id
            or handle.route_lease_id
            != (
                None
                if pending.route_lease is None
                else pending.route_lease.route_lease_id
            )
        ):
            await self.runtime.cancel(handle, "dispatch_handle_identity_mismatch")
            await self._fail_pending_dispatch(
                pending,
                RuntimeError("Runtime returned a mismatched DispatchHandle"),
            )
            return
        if not self.state.matches_active_attempt(
            run_id=request.run_id,
            task_id=request.task_id,
            attempt=request.attempt,
            dispatch_id=request.dispatch_id,
        ):
            await self.runtime.cancel(handle, "late_dispatch_prepared")
            self._release_attempt_lease(
                lease_id=pending.lease.lease_id,
                run_id=request.run_id,
                task_id=request.task_id,
                attempt=request.attempt,
                reason="late_dispatch_prepared",
            )
            await self._release_attempt_route(
                run_id=request.run_id,
                task_id=request.task_id,
                attempt=request.attempt,
                reason="late_dispatch_prepared",
                expected_route_lease_id=handle.route_lease_id,
                record_inference=True,
            )
            return
        self._dispatches[event.dispatch_id] = _DispatchRecord(
            handle=handle,
            lease_id=pending.lease.lease_id,
            route_lease=pending.route_lease,
        )
        self._record(
            request.run_id,
            "dispatch_prepared",
            task_id=request.task_id,
            attempt=request.attempt,
            lease_id=pending.lease.lease_id,
            route_lease_id=handle.route_lease_id,
            payload={
                "dispatch_id": request.dispatch_id,
                "dispatch_prepare_ms": max(
                    0.0,
                    (perf_counter_ns() - pending.requested_at_ns) / 1_000_000,
                ),
                "worker_endpoint_id": handle.worker_endpoint_id,
            },
        )

    async def _dispatch_start_failed(self, event: _DispatchStartFailed) -> None:
        pending = self._pending_dispatches.get(event.dispatch_id)
        if pending is None or pending.generation != event.generation:
            return
        del self._pending_dispatches[event.dispatch_id]
        if not self.state.matches_active_attempt(
            run_id=pending.request.run_id,
            task_id=pending.request.task_id,
            attempt=pending.request.attempt,
            dispatch_id=pending.request.dispatch_id,
        ):
            return
        await self._fail_pending_dispatch(pending, event.error)

    async def _runtime_binding_invalidated(
        self,
        event: _RuntimeBindingInvalidated,
    ) -> None:
        affected: list[tuple[str, str, int, str, str]] = []
        for pending in tuple(self._pending_dispatches.values()):
            if (
                pending.lease.node_id == event.node_id
                and pending.lease.boot_id == event.boot_id
            ):
                affected.append(
                    (
                        pending.request.run_id,
                        pending.request.task_id,
                        pending.request.attempt,
                        pending.request.dispatch_id,
                        pending.lease.lease_id,
                    )
                )
        for dispatch_id, dispatch in tuple(self._dispatches.items()):
            try:
                lease = self.placement.lease_snapshot(dispatch.lease_id).lease
            except KeyError:
                continue
            if lease.node_id != event.node_id or lease.boot_id != event.boot_id:
                continue
            if lease.run_id is None or lease.task_id is None or lease.attempt is None:
                continue
            task = self.state.snapshot(lease.run_id).task(lease.task_id)
            attempt_snapshot = next(
                (
                    item
                    for item in task.attempts
                    if item.attempt == lease.attempt
                    and item.dispatch_id == dispatch_id
                    and item.status is AttemptStatus.DISPATCHED
                ),
                None,
            )
            if attempt_snapshot is not None:
                affected.append(
                    (
                        lease.run_id,
                        lease.task_id,
                        lease.attempt,
                        dispatch_id,
                        lease.lease_id,
                    )
                )
        for run_id, task_id, attempt, dispatch_id, lease_id in affected:
            await self._fail_worker_start(
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                dispatch_id=dispatch_id,
                lease_id=lease_id,
                reason=event.reason,
            )

    async def _fail_pending_dispatch(
        self,
        pending: _DispatchStartupRecord,
        exc: BaseException,
    ) -> None:
        request = pending.request
        self._record(
            request.run_id,
            "dispatch_start_failed",
            task_id=request.task_id,
            attempt=request.attempt,
            lease_id=pending.lease.lease_id,
            route_lease_id=(
                None
                if pending.route_lease is None
                else pending.route_lease.route_lease_id
            ),
            payload={
                "dispatch_id": request.dispatch_id,
                "dispatch_prepare_ms": max(
                    0.0,
                    (perf_counter_ns() - pending.requested_at_ns) / 1_000_000,
                ),
                "exception_type": type(exc).__name__,
                "message": str(exc),
            },
        )
        error = self._error(
            run_id=request.run_id,
            task_id=request.task_id,
            attempt=request.attempt,
            dispatch_id=request.dispatch_id,
            lease_id=pending.lease.lease_id,
            error_code="worker_start_failed",
            category="worker",
            origin="runtime",
            phase="dispatched",
            message=f"{type(exc).__name__}: {exc}",
        )
        await self._handle_attempt_failure(
            run_id=request.run_id,
            task_id=request.task_id,
            attempt=request.attempt,
            dispatch_id=request.dispatch_id,
            lease_id=pending.lease.lease_id,
            error=error,
            attempt_status=AttemptStatus.FAILED,
            dispatch_handle=None,
        )

    def _build_request(
        self,
        run_id: str,
        task_id: str,
        attempt: int,
        dispatch_id: str,
        model_route: ModelRouteLease | None,
    ) -> ExecutionRequest:
        execution = self._runs[run_id]
        node = execution.compiled.tasks[task_id]
        definition = execution.compiled.definitions[node.definition_id]
        index = self.indexes.get(run_id)
        ref = execution.index_ref
        arguments: list[RuntimeArgument] = []
        for binding in node.inputs:
            if isinstance(binding, LiteralBinding):
                arguments.append(
                    RuntimeArgument(binding.input_name, "literal", literal=binding.value)
                )
            elif isinstance(binding, DefaultBinding):
                arguments.append(RuntimeArgument(binding.input_name, "default_omitted"))
            elif isinstance(binding, WorkflowInputBinding):
                handle = index.workflow_input_handle(
                    binding.workflow_input_name,
                    controller_generation=ref.controller_generation,
                    index_generation=ref.index_generation,
                )
                arguments.append(
                    RuntimeArgument(binding.input_name, "data_handle", data_handle=handle)
                )
            elif isinstance(binding, OutputBinding):
                handle = index.task_output_handle(
                    binding.source_task_id,
                    binding.source_output,
                    controller_generation=ref.controller_generation,
                    index_generation=ref.index_generation,
                )
                arguments.append(
                    RuntimeArgument(binding.input_name, "data_handle", data_handle=handle)
                )
            else:
                raise TypeError(f"unsupported input binding: {type(binding).__name__}")
        return ExecutionRequest(
            dispatch_id=dispatch_id,
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            task_kind=definition.task_kind,
            execution_target=self.anchors.resolve(
                run_id=run_id,
                compiled=execution.compiled,
                task_id=task_id,
            ).execution_target,
            model_route=model_route,
            code_handle=execution.code_by_definition[definition.definition_id],
            arguments=tuple(arguments),
            expected_outputs=definition.output_names,
            timeout_ms=definition.timeout_ms,
            environment_fingerprint=self.runtime.environment_fingerprint,
        )

    async def _handle_runtime_event(self, event: RuntimeEvent) -> None:
        if event.event_id in self._seen_runtime_events:
            return
        self._seen_runtime_events[event.event_id] = event.run_id
        if event.run_id not in self._runs:
            await self._release_event_outputs(event)
            return
        conflict = self._conflicting_active_attempt(event)
        if conflict is not None:
            await self._handle_conflicting_dispatch(event, conflict)
            return
        if event.kind is RuntimeEventKind.WORKER_STARTED:
            await self._worker_started(event)
        elif event.kind is RuntimeEventKind.TASK_RESULT:
            await self._task_result(event)
        elif event.kind in {
            RuntimeEventKind.TASK_FAILED,
            RuntimeEventKind.DISPATCH_FAILED,
        }:
            await self._task_failed(event)
        elif event.kind is RuntimeEventKind.TASK_CANCELLED:
            self._release_event_lease(event, "runtime_cancelled")
            await self._release_attempt_route(
                run_id=event.run_id,
                task_id=event.task_id,
                attempt=event.attempt,
                reason="runtime_cancelled",
                expected_route_lease_id=event.route_lease_id,
                record_inference=True,
            )

    def _conflicting_active_attempt(
        self,
        event: RuntimeEvent,
    ) -> AttemptSnapshot | None:
        try:
            snapshot = self.state.snapshot(event.run_id)
            task = snapshot.task(event.task_id)
        except KeyError:
            return None
        for attempt in task.attempts:
            if (
                attempt.attempt == event.attempt
                and attempt.status
                not in {
                    AttemptStatus.CANCELLED,
                    AttemptStatus.FAILED,
                    AttemptStatus.SUCCEEDED,
                    AttemptStatus.TIMED_OUT,
                }
                and attempt.dispatch_id != event.dispatch_id
            ):
                return attempt
        return None

    async def _handle_conflicting_dispatch(
        self,
        event: RuntimeEvent,
        active: AttemptSnapshot,
    ) -> None:
        await self._release_event_outputs(event)
        await self._cancel_dispatch(event.dispatch_id, "conflicting_dispatch_id")
        await self._cancel_dispatch(active.dispatch_id, "conflicting_dispatch_id")
        error = self._error(
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            dispatch_id=active.dispatch_id,
            lease_id=active.lease_id,
            error_code="backend_internal_error",
            category="control",
            origin="runtime",
            phase="cleanup",
            message=(
                "one Attempt received two dispatch IDs: "
                f"{active.dispatch_id} and {event.dispatch_id}"
            ),
        )
        await self._handle_attempt_failure(
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            dispatch_id=active.dispatch_id,
            lease_id=active.lease_id,
            error=error,
            attempt_status=AttemptStatus.FAILED,
            dispatch_handle=self._dispatches.get(active.dispatch_id),
        )

    async def _handle_model_route_failure(self, event: _ModelRouteFailed) -> None:
        lease = event.lease
        if lease.run_id not in self._runs:
            return
        expected = self._attempt_routes.get(
            (lease.run_id, lease.task_id, lease.attempt)
        )
        if expected is None or expected.route_lease_id != lease.route_lease_id:
            return
        task = self.state.snapshot(lease.run_id).task(lease.task_id)
        attempt = next(
            (
                item
                for item in task.attempts
                if item.attempt == lease.attempt
                and self.state.matches_active_attempt(
                    run_id=lease.run_id,
                    task_id=lease.task_id,
                    attempt=item.attempt,
                    dispatch_id=item.dispatch_id,
                )
            ),
            None,
        )
        if attempt is None:
            return
        assert self.inference is not None
        instance = self.inference.instances.snapshot(lease.instance_id)
        self._record(
            lease.run_id,
            "model_instance_unhealthy",
            task_id=lease.task_id,
            attempt=lease.attempt,
            route_lease_id=lease.route_lease_id,
            model_instance_id=lease.instance_id,
            payload={
                "model_id": lease.model_id,
                "instance_generation": lease.instance_generation,
                "instance_placement_lease_id": instance.placement_lease_id,
                "node_id": instance.node_id,
                "npu_device_id": instance.npu_device_id,
                "reason": event.reason,
            },
        )
        await self._cancel_dispatch(attempt.dispatch_id, "model_instance_unhealthy")
        error = self._error(
            run_id=lease.run_id,
            task_id=lease.task_id,
            attempt=lease.attempt,
            dispatch_id=attempt.dispatch_id,
            lease_id=attempt.lease_id,
            error_code="model_instance_failed",
            category="model_service",
            origin="inference",
            phase="executing",
            message=event.reason,
            route_lease_id=lease.route_lease_id,
            model_instance_id=lease.instance_id,
        )
        await self._handle_attempt_failure(
            run_id=lease.run_id,
            task_id=lease.task_id,
            attempt=lease.attempt,
            dispatch_id=attempt.dispatch_id,
            lease_id=attempt.lease_id,
            error=error,
            attempt_status=AttemptStatus.FAILED,
            dispatch_handle=self._dispatches.get(attempt.dispatch_id),
            expected_route_lease_id=lease.route_lease_id,
        )

    async def _worker_started(self, event: RuntimeEvent) -> None:
        if not self.state.matches_active_attempt(
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            dispatch_id=event.dispatch_id,
        ):
            await self._cancel_dispatch(event.dispatch_id, "late_worker_started")
            self._release_event_lease(event, "late_worker_started")
            await self._release_attempt_route(
                run_id=event.run_id,
                task_id=event.task_id,
                attempt=event.attempt,
                reason="late_worker_started",
                expected_route_lease_id=event.route_lease_id,
            )
            return
        expected_route = self._attempt_routes.get(
            (event.run_id, event.task_id, event.attempt)
        )
        if (
            (expected_route is None and event.route_lease_id is not None)
            or (
                expected_route is not None
                and event.route_lease_id != expected_route.route_lease_id
            )
        ):
            await self._fail_worker_start(
                run_id=event.run_id,
                task_id=event.task_id,
                attempt=event.attempt,
                dispatch_id=event.dispatch_id,
                lease_id=event.lease_id,
                reason="WorkerStarted carried a stale ModelRouteLease",
            )
            return
        if not self.placement.bind_lease(
            event.lease_id, now_ms=self.clock.monotonic_ms()
        ):
            await self._fail_worker_start(
                run_id=event.run_id,
                task_id=event.task_id,
                attempt=event.attempt,
                dispatch_id=event.dispatch_id,
                lease_id=event.lease_id,
                reason="PlacementLease could not be bound before WorkerStarted",
            )
            return
        self.deadlines.cancel(
            kind=DeadlineKind.LEASE,
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
        )
        result = self.state.worker_started(
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            dispatch_id=event.dispatch_id,
            now_ms=self.clock.monotonic_ms(),
        )
        if result.accepted:
            definition = self._definition(event.run_id, event.task_id)
            if definition.timeout_ms is not None:
                self.deadlines.register(
                    kind=DeadlineKind.TASK,
                    run_id=event.run_id,
                    task_id=event.task_id,
                    attempt=event.attempt,
                    due_at_ms=self.clock.monotonic_ms() + definition.timeout_ms,
                )
            self._record(
                event.run_id,
                "worker_started",
                task_id=event.task_id,
                attempt=event.attempt,
                lease_id=event.lease_id,
                route_lease_id=event.route_lease_id,
                model_instance_id=(
                    None if expected_route is None else expected_route.instance_id
                ),
                payload={
                    "node_id": self.placement.lease_snapshot(event.lease_id).lease.node_id,
                    "worker_pid": event.worker_pid,
                },
            )

    async def _task_result(self, event: RuntimeEvent) -> None:
        if not self.state.matches_active_attempt(
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            dispatch_id=event.dispatch_id,
        ):
            if not self._matches_published_result(event):
                await self._release_event_outputs(event)
            self._release_event_lease(event, "late_result")
            await self._release_attempt_route(
                run_id=event.run_id,
                task_id=event.task_id,
                attempt=event.attempt,
                reason="late_result",
                expected_route_lease_id=event.route_lease_id,
                record_inference=True,
            )
            return
        route_released = await self._release_attempt_route(
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            reason="succeeded",
            expected_route_lease_id=event.route_lease_id,
            record_inference=True,
        )
        if not route_released:
            await self._release_event_outputs(event)
            route = self._attempt_routes.get(
                (event.run_id, event.task_id, event.attempt)
            )
            error = self._error(
                run_id=event.run_id,
                task_id=event.task_id,
                attempt=event.attempt,
                dispatch_id=event.dispatch_id,
                lease_id=event.lease_id,
                error_code="model_protocol_failed",
                category="model",
                origin="control",
                phase="cleanup",
                message="model request or context remained active at Task terminal",
                route_lease_id=event.route_lease_id,
                model_instance_id=None if route is None else route.instance_id,
            )
            await self._handle_attempt_failure(
                run_id=event.run_id,
                task_id=event.task_id,
                attempt=event.attempt,
                dispatch_id=event.dispatch_id,
                lease_id=event.lease_id,
                error=error,
                attempt_status=AttemptStatus.FAILED,
                dispatch_handle=self._dispatches.get(event.dispatch_id),
                expected_route_lease_id=event.route_lease_id,
            )
            return
        execution = self._runs[event.run_id]
        definition = self._definition(event.run_id, event.task_id)
        output_handles = dict(event.output_handles)
        try:
            index = self.indexes.get(event.run_id)
            await asyncio.to_thread(
                index.publish_outputs,
                task_id=event.task_id,
                output_handles=output_handles,
                expected_output_names=definition.output_names,
                controller_generation=execution.index_ref.controller_generation,
                index_generation=execution.index_ref.index_generation,
            )
        except Exception as exc:
            await self._release_event_outputs(event)
            error = self._error(
                run_id=event.run_id,
                task_id=event.task_id,
                attempt=event.attempt,
                dispatch_id=event.dispatch_id,
                lease_id=event.lease_id,
                error_code="result_publish_failed",
                category="data",
                origin="data",
                phase="publishing",
                message=f"{type(exc).__name__}: {exc}",
            )
            await self._handle_attempt_failure(
                run_id=event.run_id,
                task_id=event.task_id,
                attempt=event.attempt,
                dispatch_id=event.dispatch_id,
                lease_id=event.lease_id,
                error=error,
                attempt_status=AttemptStatus.FAILED,
                dispatch_handle=self._dispatches.get(event.dispatch_id),
            )
            return
        self.deadlines.cancel(
            kind=DeadlineKind.LEASE,
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
        )
        self.deadlines.cancel(
            kind=DeadlineKind.TASK,
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
        )
        self.placement.release_lease(
            event.lease_id,
            now_ms=self.clock.monotonic_ms(),
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            reason="succeeded",
        )
        result = self.state.attempt_succeeded(
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            dispatch_id=event.dispatch_id,
            now_ms=self.clock.monotonic_ms(),
        )
        if not result.accepted:
            await self._release_event_outputs(event)
            return
        if isinstance(self.policy, RunLifecycleAwarePolicy):
            self.policy.task_succeeded(
                run_id=event.run_id,
                task_id=event.task_id,
                task_kind=definition.task_kind,
            )
        self._record(
            event.run_id,
            "task_succeeded",
            task_id=event.task_id,
            attempt=event.attempt,
            lease_id=event.lease_id,
            route_lease_id=event.route_lease_id,
            model_instance_id=(
                None
                if event.route_lease_id is None
                else self._attempt_routes[
                    (event.run_id, event.task_id, event.attempt)
                ].instance_id
            ),
        )
        if event.attempt > 1:
            previous = self.recovery.decision(
                event.run_id,
                event.task_id,
                event.attempt - 1,
            )
            if previous is not None and previous.action is RecoveryAction.RETRY:
                self._record(
                    event.run_id,
                    "recovery_succeeded",
                    task_id=event.task_id,
                    attempt=event.attempt,
                    lease_id=event.lease_id,
                    route_lease_id=event.route_lease_id,
                    payload={
                        "decision_id": previous.decision_id,
                        "failed_attempt": previous.attempt,
                        "recovered_attempt": event.attempt,
                        "error_code": previous.error.error_code,
                    },
                )
        for task_id in result.ready_task_ids:
            self._enqueue_ready(event.run_id, task_id)
        if result.run_terminal:
            await self._on_run_terminal(event.run_id)

    async def _task_failed(self, event: RuntimeEvent) -> None:
        if event.error is None:
            error = self._error(
                run_id=event.run_id,
                task_id=event.task_id,
                attempt=event.attempt,
                dispatch_id=event.dispatch_id,
                lease_id=event.lease_id,
                error_code="backend_internal_error",
                category="control",
                origin="runtime",
                phase="dispatched",
                message="runtime failure event omitted ErrorInfo",
            )
        else:
            error = self.error_normalizer.normalize(
                event.error,
                identity=FaultIdentity(
                    run_id=event.run_id,
                    task_id=event.task_id,
                    attempt=event.attempt,
                    dispatch_id=event.dispatch_id,
                    lease_id=event.lease_id,
                    route_lease_id=event.route_lease_id,
                ),
            )
        await self._handle_attempt_failure(
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            dispatch_id=event.dispatch_id,
            lease_id=event.lease_id,
            error=error,
            attempt_status=AttemptStatus.FAILED,
            dispatch_handle=self._dispatches.get(event.dispatch_id),
            resource_observation=event.resource_observation,
            expected_route_lease_id=event.route_lease_id,
        )

    async def _handle_attempt_failure(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        dispatch_id: str,
        lease_id: str,
        error: ErrorInfo,
        attempt_status: AttemptStatus,
        dispatch_handle: _DispatchRecord | None,
        resource_observation: ResourceObservation | None = None,
        expected_route_lease_id: str | None = None,
    ) -> None:
        if not self.state.matches_active_attempt(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            dispatch_id=dispatch_id,
        ):
            self._release_attempt_lease(
                lease_id=lease_id,
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                reason="late_failure",
            )
            await self._release_attempt_route(
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                reason="late_failure",
                expected_route_lease_id=expected_route_lease_id,
                record_inference=True,
            )
            return
        self.deadlines.cancel(
            kind=DeadlineKind.LEASE,
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
        )
        self.deadlines.cancel(
            kind=DeadlineKind.TASK,
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
        )
        cleanup_started_ns = perf_counter_ns()
        dispatch_invalidated = self.runtime.dispatch_invalidated(dispatch_id)
        worker_released = self.runtime.worker_released(dispatch_id)
        route_released = await self._release_attempt_route(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            reason=error.error_code,
            expected_route_lease_id=expected_route_lease_id,
            record_inference=True,
        )
        quarantined = False
        if not worker_released:
            quarantined = self._quarantine_attempt_resource(
                lease_id=lease_id,
                error=error,
            )
        if worker_released:
            self._release_attempt_lease(
                lease_id=lease_id,
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                reason=error.error_code,
            )
        elif quarantined:
            try:
                self.placement.invalidate_lease(
                    lease_id,
                    now_ms=self.clock.monotonic_ms(),
                    reason=f"quarantined_after_{error.error_code}",
                )
            except (KeyError, StateTransitionError):
                pass
        definition = self._definition(run_id, task_id)
        task_snapshot = self.state.snapshot(run_id).task(task_id)
        run_snapshot = self.state.snapshot(run_id)
        cleanup = self.recovery_coordinator.finalize_cleanup(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            dispatch_invalidated=dispatch_invalidated,
            worker_released=worker_released,
            unpublished_data_released=True,
            route_released=route_released,
            placement_released=(
                self.placement.lease_snapshot(lease_id).status
                not in {LeaseStatus.RESERVED, LeaseStatus.BOUND}
            ),
            node_or_device_quarantined=quarantined,
        )
        cleanup_duration_ms = max(
            0,
            (perf_counter_ns() - cleanup_started_ns) // 1_000_000,
        )
        self._record_error_info(error)
        precondition_reason = self.recovery.retry_precondition_reason(
            definition=definition,
            error=error,
            attempt_count=task_snapshot.attempt_count,
            cleanup=cleanup,
            now_ms=self.clock.monotonic_ms(),
            run_deadline_at_ms=run_snapshot.deadline_at_ms,
        )
        retry_block_reason: str | None = None
        replayability = None
        if precondition_reason is None:
            replayability = self._check_replayability(run_id, task_id)
            if not replayability.replayable:
                precondition_reason = replayability.reason
                retry_block_reason = replayability.reason
        if error.error_code == "npu_oom":
            execution = self._runs[run_id]
            observed_peak = None
            if resource_observation is not None:
                observed_peak = (
                    resource_observation.peak_npu_process_hbm_mb
                    or resource_observation.peak_npu_reserved_mb
                    or resource_observation.peak_npu_allocated_mb
                )
            if precondition_reason is None:
                reanchor = self.anchors.reanchor_after_oom(
                    run_id=run_id,
                    compiled=execution.compiled,
                    task_id=task_id,
                    observed_peak_npu_mem_mb=observed_peak,
                )
                if not reanchor.created:
                    retry_block_reason = reanchor.reason
                elif (
                    reanchor.anchor.effective.npu_mem_mb
                    > self.placement.max_single_npu_allocatable_hbm_mb()
                ):
                    retry_block_reason = "oom_reanchor_unsatisfiable"
                    error = self._error(
                        run_id=run_id,
                        task_id=task_id,
                        attempt=attempt,
                        dispatch_id=dispatch_id,
                        lease_id=lease_id,
                        error_code="resource_request_unsatisfiable",
                        category="configuration",
                        origin="placement",
                        phase="cleanup",
                        message=(
                            "OOM reanchor exceeds every single-NPU allocatable capacity"
                        ),
                    )
                anchor = reanchor.anchor
                anchor_created = reanchor.created
                anchor_reason = reanchor.reason
                previous_npu_mem_mb = reanchor.previous_npu_mem_mb
            else:
                anchor = self.anchors.resolve(
                    run_id=run_id,
                    compiled=execution.compiled,
                    task_id=task_id,
                )
                anchor_created = False
                anchor_reason = precondition_reason
                previous_npu_mem_mb = anchor.effective.npu_mem_mb
            self._record(
                run_id,
                "resource_anchor_oom",
                task_id=task_id,
                attempt=attempt,
                lease_id=lease_id,
                payload={
                    "created": anchor_created,
                    "reason": anchor_reason,
                    "previous_npu_mem_mb": previous_npu_mem_mb,
                    "new_npu_mem_mb": anchor.effective.npu_mem_mb,
                    "observed_peak_npu_mem_mb": observed_peak,
                    "revision": anchor.revision,
                },
            )
        decision = self.recovery.decide(
            definition=definition,
            error=error,
            attempt_count=task_snapshot.attempt_count,
            cleanup=cleanup,
            now_ms=self.clock.monotonic_ms(),
            run_deadline_at_ms=run_snapshot.deadline_at_ms,
            retry_block_reason=retry_block_reason,
        )
        self._record_recovery_decision(
            decision,
            cleanup=cleanup,
            cleanup_duration_ms=cleanup_duration_ms,
            replayability_reason=(
                None if replayability is None else replayability.reason
            ),
        )
        if decision.action is RecoveryAction.RETRY:
            result = self.state.attempt_retry_wait(
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                dispatch_id=dispatch_id,
                attempt_status=attempt_status,
                error=error,
                now_ms=self.clock.monotonic_ms(),
            )
            if result.accepted:
                assert decision.eligible_at_ms is not None
                eligible_at = decision.eligible_at_ms
                if definition.retry_backoff_ms == 0:
                    eligible = self.state.retry_eligible(
                        run_id=run_id,
                        task_id=task_id,
                        attempt=attempt,
                        now_ms=eligible_at,
                    )
                    for ready_id in eligible.ready_task_ids:
                        self._enqueue_ready(run_id, ready_id)
                else:
                    self.deadlines.register(
                        kind=DeadlineKind.RETRY,
                        run_id=run_id,
                        task_id=task_id,
                        attempt=attempt,
                        due_at_ms=eligible_at,
                    )
                self._record(
                    run_id,
                    "task_retry_wait",
                    task_id=task_id,
                    attempt=attempt,
                    lease_id=lease_id,
                    payload={
                        "decision_id": decision.decision_id,
                        "action": decision.action.value,
                        "reason": decision.reason,
                        "error_code": error.error_code,
                    },
                )
            return
        task_status = (
            TaskStatus.TIMED_OUT
            if attempt_status is AttemptStatus.TIMED_OUT
            else TaskStatus.FAILED
        )
        result = self.state.attempt_final_failure(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            dispatch_id=dispatch_id,
            attempt_status=attempt_status,
            task_status=task_status,
            error=error,
            now_ms=self.clock.monotonic_ms(),
        )
        if result.accepted:
            self._record(
                run_id,
                "task_failed",
                task_id=task_id,
                attempt=attempt,
                lease_id=lease_id,
                payload={
                    "decision_id": decision.decision_id,
                    "action": decision.action.value,
                    "reason": decision.reason,
                    "error_code": error.error_code,
                },
            )
            await self._cleanup_cancelled_attempts(run_id, result.cancelled_attempts)
            await self._on_run_terminal(run_id)

    async def _fail_pre_attempt_unsatisfiable(
        self,
        run_id: str,
        task_id: str,
        requested_npu_hbm_mb: int,
    ) -> None:
        now = self.clock.monotonic_ms()
        error = self._error(
            run_id=run_id,
            task_id=task_id,
            attempt=0,
            dispatch_id=None,
            lease_id=None,
            error_code="resource_request_unsatisfiable",
            category="configuration",
            origin="placement",
            phase="pre_attempt",
            message=(
                f"requested NPU HBM {requested_npu_hbm_mb} MB exceeds every "
                "single-device allocatable capacity"
            ),
        )
        result = self.state.pre_attempt_final_failure(
            run_id=run_id,
            task_id=task_id,
            error=error,
            now_ms=now,
        )
        if result.accepted:
            self._record(
                run_id,
                "task_failed",
                task_id=task_id,
                payload={"error_code": error.error_code, "attempt": 0},
            )
            await self._cleanup_cancelled_attempts(run_id, result.cancelled_attempts)
            await self._on_run_terminal(run_id)

    async def _process_due_deadlines(self) -> bool:
        events = self.deadlines.pop_due(self.clock.monotonic_ms())
        for event in events:
            await self._handle_deadline(event)
        return bool(events)

    async def _handle_deadline(self, event: DeadlineEvent) -> None:
        if event.kind is DeadlineKind.RUN:
            await self._terminate_run(
                event.run_id,
                RunStatus.TIMED_OUT,
                "run_timed_out",
            )
            return
        assert event.task_id is not None and event.attempt is not None
        if event.kind is DeadlineKind.RETRY:
            eligible = self.state.retry_eligible(
                run_id=event.run_id,
                task_id=event.task_id,
                attempt=event.attempt,
                now_ms=self.clock.monotonic_ms(),
            )
            for task_id in eligible.ready_task_ids:
                self._enqueue_ready(event.run_id, task_id)
            return
        task = self.state.snapshot(event.run_id).task(event.task_id)
        if not task.attempts:
            return
        attempt = task.attempts[-1]
        if attempt.attempt != event.attempt:
            return
        if event.kind is DeadlineKind.LEASE:
            if attempt.status is not AttemptStatus.DISPATCHED:
                return
            self.placement.expire_lease(
                attempt.lease_id,
                now_ms=self.clock.monotonic_ms(),
            )
            if self.placement.lease_snapshot(attempt.lease_id).status is LeaseStatus.BOUND:
                return
            await self._fail_worker_start(
                run_id=event.run_id,
                task_id=event.task_id,
                attempt=event.attempt,
                dispatch_id=attempt.dispatch_id,
                lease_id=attempt.lease_id,
                reason="WorkerStarted was not received before the dispatch deadline",
            )
            return
        dispatch = self._dispatches.get(attempt.dispatch_id)
        if dispatch is not None:
            await self.runtime.cancel(dispatch.handle, "task_timeout")
        error = self._error(
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            dispatch_id=attempt.dispatch_id,
            lease_id=attempt.lease_id,
            error_code="task_timeout",
            category="timeout",
            origin="control",
            phase="user_code",
            message="task execution timeout",
        )
        await self._handle_attempt_failure(
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            dispatch_id=attempt.dispatch_id,
            lease_id=attempt.lease_id,
            error=error,
            attempt_status=AttemptStatus.TIMED_OUT,
            dispatch_handle=dispatch,
        )

    async def _terminate_run(
        self,
        run_id: str,
        target: RunStatus,
        reason: str,
    ) -> RunSnapshot:
        result = self.state.terminate_run(
            run_id=run_id,
            target=target,
            reason=reason,
            now_ms=self.clock.monotonic_ms(),
        )
        if result.accepted:
            await self._cleanup_cancelled_attempts(run_id, result.cancelled_attempts)
            self._record(run_id, f"run_{target.value}", payload={"reason": reason})
            await self._on_run_terminal(run_id)
        return self.state.snapshot(run_id)

    async def _cleanup_cancelled_attempts(
        self,
        run_id: str,
        attempts: tuple[AttemptSnapshot, ...],
    ) -> None:
        for attempt in attempts:
            cancel_error: Exception | None = None
            try:
                await self._cancel_dispatch(attempt.dispatch_id, "run_terminal")
            except Exception as exc:
                cancel_error = exc
            self.deadlines.cancel(
                kind=DeadlineKind.LEASE,
                run_id=run_id,
                task_id=attempt.task_id,
                attempt=attempt.attempt,
            )
            self.deadlines.cancel(
                kind=DeadlineKind.TASK,
                run_id=run_id,
                task_id=attempt.task_id,
                attempt=attempt.attempt,
            )
            worker_released = self.runtime.worker_released(attempt.dispatch_id)
            quarantined = False
            if worker_released:
                self._release_attempt_lease(
                    lease_id=attempt.lease_id,
                    run_id=run_id,
                    task_id=attempt.task_id,
                    attempt=attempt.attempt,
                    reason="run_terminal",
                )
            else:
                try:
                    lease = self.placement.lease_snapshot(attempt.lease_id).lease
                    self.placement.set_node_status(
                        lease.node_id,
                        NodeStatus.UNSCHEDULABLE,
                        now_ms=self.clock.monotonic_ms(),
                    )
                    self.placement.invalidate_lease(
                        attempt.lease_id,
                        now_ms=self.clock.monotonic_ms(),
                        reason="run_terminal_cleanup_quarantined",
                    )
                    quarantined = True
                except (KeyError, StateTransitionError):
                    pass
            route_released = await self._release_attempt_route(
                run_id=run_id,
                task_id=attempt.task_id,
                attempt=attempt.attempt,
                reason="run_terminal",
                record_inference=True,
            )
            self._record(
                run_id,
                "cancel_cleanup",
                task_id=attempt.task_id,
                attempt=attempt.attempt,
                lease_id=attempt.lease_id,
                payload={
                    "dispatch_id": attempt.dispatch_id,
                    "dispatch_invalidated": self.runtime.dispatch_invalidated(
                        attempt.dispatch_id
                    ),
                    "worker_released": worker_released,
                    "route_released": route_released,
                    "node_or_device_quarantined": quarantined,
                    "cancel_error": (
                        None
                        if cancel_error is None
                        else f"{type(cancel_error).__name__}: {cancel_error}"
                    ),
                },
            )

    async def _on_run_terminal(self, run_id: str) -> None:
        self.deadlines.clear_run(run_id)
        for key, queued in list(self._queued.items()):
            if key.run_id == run_id:
                self.policy.depart(queued.view.queue_token)
                del self._queued[key]
                self._blocked.pop(key, None)
        snapshot = self.state.snapshot(run_id)
        assert snapshot.finished_at_ms is not None
        self._record(
            run_id,
            "run_terminal",
            payload={
                "status": snapshot.status.value,
                "finished_at_ms": snapshot.finished_at_ms,
            },
        )
        if self.inference is not None:
            self.inference.replicas.remove_run(run_id)
            await self.inference.reconcile()
        if isinstance(self.policy, RunLifecycleAwarePolicy):
            try:
                self.policy.run_terminal(
                    run_id=run_id,
                    status=snapshot.status.value,
                    finished_at_ms=snapshot.finished_at_ms,
                )
            except Exception as exc:
                self._record(
                    run_id,
                    "policy_lifecycle_error",
                    payload={
                        "hook": "run_terminal",
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
        for future in self._terminal_waiters.pop(run_id, []):
            if not future.done():
                future.set_result(snapshot)

    async def _destroy(self, run_id: str, *, force: bool) -> DestroyResult:
        execution = self._runs[run_id]
        if execution.destroyed is not None:
            return execution.destroyed
        snapshot = self.state.snapshot(run_id)
        if not snapshot.terminal:
            raise RunNotTerminalError("run must be terminal before destroy")
        flush = await self.flush_run_recording(run_id)
        if not flush.recording_complete and not force:
            raise RuntimeError("recording is incomplete; force is required")
        if self.placement.active_lease_count(run_id) != 0:
            raise RuntimeError("run still owns placement leases")
        if self.pending_dispatch_count(run_id) != 0:
            raise RuntimeError("run still owns pending dispatch startups")
        if self.inference is not None:
            active_routes = [
                route
                for (route_run_id, _, _), route in self._attempt_routes.items()
                if route_run_id == run_id
                and self.inference.route_snapshot(route.route_lease_id).status
                in {
                    ModelRouteLeaseStatus.RESERVED,
                    ModelRouteLeaseStatus.ACTIVE,
                }
            ]
            if active_routes:
                raise RuntimeError("run still owns ModelRouteLeases")
        tombstone = await asyncio.to_thread(
            self.indexes.destroy,
            run_id,
            completed_at_ms=self.clock.monotonic_ms(),
        )
        self.placement.destroy_run_context(run_id)
        self.anchors.destroy_run(run_id)
        released_code_count = len(execution.code_handles)
        await self.runtime.release_code(execution.code_handles)
        execution.code_handles = ()
        execution.code_by_definition.clear()
        await self.runtime.release_run(run_id)
        if self.inference is not None:
            self.inference.destroy_run(run_id)
        dispatch_ids = [
            dispatch_id
            for dispatch_id, record in self._dispatches.items()
            if record.handle.run_id == run_id
        ]
        for dispatch_id in dispatch_ids:
            del self._dispatches[dispatch_id]
        for key in [key for key in self._queue_generations if key.run_id == run_id]:
            del self._queue_generations[key]
            self._blocked.pop(key, None)
        route_ids = {
            route.route_lease_id
            for route_key, route in self._attempt_routes.items()
            if route_key[0] == run_id
        }
        for route_key in [
            route_key
            for route_key in self._attempt_routes
            if route_key[0] == run_id
        ]:
            del self._attempt_routes[route_key]
        self._recorded_inference_routes.difference_update(route_ids)
        self._recorded_route_terminals.difference_update(route_ids)
        self.recovery.destroy_run(run_id)
        self.recovery_coordinator.destroy_run(run_id)
        for event_id in [
            event_id
            for event_id, event_run_id in self._seen_runtime_events.items()
            if event_run_id == run_id
        ]:
            del self._seen_runtime_events[event_id]
        result = DestroyResult(
            run_id=run_id,
            tombstone=tombstone,
            flush_result=flush,
            code_handles_released=released_code_count,
        )
        execution.destroyed = result
        return result

    async def _shutdown_active_runs(self) -> None:
        for run_id in tuple(self._runs):
            snapshot = self.state.snapshot(run_id)
            if not snapshot.terminal:
                await self._terminate_run(
                    run_id,
                    RunStatus.INTERRUPTED,
                    "scheduler_shutdown",
                )

    async def _interrupt_after_scheduler_failure(self, exc: Exception) -> None:
        for run_id in tuple(self._runs):
            snapshot = self.state.snapshot(run_id)
            if snapshot.terminal:
                continue
            self._record(
                run_id,
                "scheduler_interrupted",
                payload={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            await self._terminate_run(
                run_id,
                RunStatus.INTERRUPTED,
                "scheduler_internal_error",
            )

    async def _cancel_dispatch(self, dispatch_id: str, reason: str) -> None:
        await self._cancel_pending_dispatch(dispatch_id, reason)
        dispatch = self._dispatches.get(dispatch_id)
        if dispatch is not None:
            await self.runtime.cancel(dispatch.handle, reason)

    async def _cancel_pending_dispatch(
        self,
        dispatch_id: str,
        reason: str,
    ) -> bool:
        pending = self._pending_dispatches.pop(dispatch_id, None)
        if pending is None:
            return False
        if not pending.task.done():
            pending.task.cancel()
        try:
            handle = await pending.task
        except asyncio.CancelledError:
            handle = None
        if handle is not None:
            await self.runtime.cancel(handle, reason)
        return True

    async def _fail_worker_start(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        dispatch_id: str,
        lease_id: str,
        reason: str,
    ) -> None:
        if not self.state.matches_active_attempt(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            dispatch_id=dispatch_id,
        ):
            return
        await self._cancel_dispatch(dispatch_id, "worker_start_failed")
        error = self._error(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            dispatch_id=dispatch_id,
            lease_id=lease_id,
            error_code="worker_start_failed",
            category="worker",
            origin="control",
            phase="dispatched",
            message=reason,
        )
        await self._handle_attempt_failure(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            dispatch_id=dispatch_id,
            lease_id=lease_id,
            error=error,
            attempt_status=AttemptStatus.FAILED,
            dispatch_handle=self._dispatches.get(dispatch_id),
        )

    async def _release_event_outputs(self, event: RuntimeEvent) -> None:
        def release() -> None:
            for _, handle in event.output_handles:
                try:
                    self.indexes.data_store.release(handle)
                except Exception:
                    pass

        await asyncio.to_thread(release)

    def _check_replayability(
        self,
        run_id: str,
        task_id: str,
    ) -> ReplayabilityResult:
        execution = self._runs[run_id]
        node = execution.compiled.tasks[task_id]
        index = self.indexes.get(run_id)
        handles: list[DataHandle] = []
        for binding in node.inputs:
            if isinstance(binding, WorkflowInputBinding):
                handles.append(
                    index.workflow_input_handle(
                        binding.workflow_input_name,
                        controller_generation=execution.index_ref.controller_generation,
                        index_generation=execution.index_ref.index_generation,
                    )
                )
            elif isinstance(binding, OutputBinding):
                handles.append(
                    index.task_output_handle(
                        binding.source_task_id,
                        binding.source_output,
                        controller_generation=execution.index_ref.controller_generation,
                        index_generation=execution.index_ref.index_generation,
                    )
                )
        return self.replayability.check(
            code_available=node.definition_id in execution.code_by_definition,
            environment_matches=(
                execution.recording_context.environment_fingerprint
                == self.runtime.environment_fingerprint
            ),
            handles=handles,
        )

    def _quarantine_attempt_resource(
        self,
        *,
        lease_id: str,
        error: ErrorInfo,
    ) -> bool:
        try:
            lease = self.placement.lease_snapshot(lease_id).lease
            node_id = error.node_id or lease.node_id
            status = (
                NodeStatus.OFFLINE
                if error.error_code in {"node_offline", "runtime_node_unavailable"}
                else NodeStatus.UNSCHEDULABLE
            )
            self.placement.set_node_status(
                node_id,
                status,
                now_ms=self.clock.monotonic_ms(),
            )
            return True
        except (KeyError, StateTransitionError):
            return False

    def _record_error_info(self, error: ErrorInfo) -> None:
        self._record(
            error.run_id,
            "error_normalized",
            task_id=error.task_id,
            attempt=error.attempt,
            lease_id=error.lease_id,
            route_lease_id=error.route_lease_id,
            model_instance_id=error.model_instance_id,
            payload={
                "schema_version": error.schema_version,
                "error_code": error.error_code,
                "category": error.category,
                "origin": error.origin,
                "classification_confidence": error.classification_confidence,
                "execution_phase": error.execution_phase,
                "dispatch_id": error.dispatch_id,
                "node_id": error.node_id,
                "boot_id": error.boot_id,
                "device_id": error.device_id,
                "worker_id": error.worker_id,
                "model_instance_id": error.model_instance_id,
                "exception_type": error.exception_type,
                "platform_error_code": error.platform_error_code,
                "occurred_at_ms": error.occurred_at_ms,
                "details": dict(error.details.items_tuple()),
            },
        )

    def _record_recovery_decision(
        self,
        decision: RecoveryDecision,
        *,
        cleanup: CleanupBarrier,
        cleanup_duration_ms: int,
        replayability_reason: str | None,
    ) -> None:
        self._record(
            decision.run_id,
            "recovery_decision",
            task_id=decision.task_id,
            attempt=decision.attempt,
            lease_id=decision.error.lease_id,
            route_lease_id=decision.error.route_lease_id,
            model_instance_id=decision.error.model_instance_id,
            payload={
                "decision_id": decision.decision_id,
                "dispatch_id": decision.error.dispatch_id,
                "action": decision.action.value,
                "reason": decision.reason,
                "error_code": decision.error.error_code,
                "next_attempt": decision.next_attempt,
                "eligible_at_ms": decision.eligible_at_ms,
                "reanchor_required": decision.reanchor_required,
                "avoid_node_id": decision.avoid_node_id,
                "avoid_device_id": decision.avoid_device_id,
                "retry_budget_before": decision.retry_budget_before,
                "retry_budget_after": decision.retry_budget_after,
                "replayability_reason": replayability_reason,
                "cleanup_duration_ms": cleanup_duration_ms,
                "cleanup": {
                    "dispatch_invalidated": cleanup.dispatch_invalidated,
                    "worker_released": cleanup.worker_released,
                    "unpublished_data_released": cleanup.unpublished_data_released,
                    "route_released": cleanup.route_released,
                    "placement_released": cleanup.placement_released,
                    "node_or_device_quarantined": (
                        cleanup.node_or_device_quarantined
                    ),
                    "satisfied": cleanup.satisfied,
                },
            },
        )

    def _release_event_lease(self, event: RuntimeEvent, reason: str) -> bool:
        return self._release_attempt_lease(
            lease_id=event.lease_id,
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            reason=reason,
        )

    def _release_attempt_lease(
        self,
        *,
        lease_id: str,
        run_id: str,
        task_id: str,
        attempt: int,
        reason: str,
    ) -> bool:
        try:
            return self.placement.release_lease(
                lease_id,
                now_ms=self.clock.monotonic_ms(),
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                reason=reason,
            )
        except StateTransitionError:
            return False

    async def _release_attempt_route(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        reason: str,
        expected_route_lease_id: str | None = None,
        record_inference: bool = False,
    ) -> bool:
        route = self._attempt_routes.get((run_id, task_id, attempt))
        if route is None:
            return expected_route_lease_id is None
        if (
            expected_route_lease_id is not None
            and expected_route_lease_id != route.route_lease_id
        ):
            return False
        if self.inference is None:
            return False
        snapshot = self.inference.route_snapshot(route.route_lease_id)
        if snapshot.status not in {
            ModelRouteLeaseStatus.RESERVED,
            ModelRouteLeaseStatus.ACTIVE,
        }:
            self._record_route_terminal(route)
            return True
        if record_inference and not self._record_attempt_inference(route):
            return False
        try:
            await self.inference.release_route(route, reason=reason)
        except StateTransitionError:
            return False
        snapshot = self.inference.route_snapshot(route.route_lease_id)
        released = snapshot.status not in {
            ModelRouteLeaseStatus.RESERVED,
            ModelRouteLeaseStatus.ACTIVE,
        }
        if released:
            self._record_route_terminal(route)
        return released

    def _record_route_terminal(self, route: ModelRouteLease) -> None:
        if (
            self.inference is None
            or route.route_lease_id in self._recorded_route_terminals
        ):
            return
        snapshot = self.inference.route_snapshot(route.route_lease_id)
        if snapshot.status in {
            ModelRouteLeaseStatus.RESERVED,
            ModelRouteLeaseStatus.ACTIVE,
        }:
            return
        self._record(
            route.run_id,
            f"model_route_{snapshot.status.value}",
            task_id=route.task_id,
            attempt=route.attempt,
            route_lease_id=route.route_lease_id,
            model_instance_id=route.instance_id,
            payload={
                "model_id": route.model_id,
                "instance_generation": route.instance_generation,
                "reason": snapshot.finish_reason,
            },
        )
        self._recorded_route_terminals.add(route.route_lease_id)

    def _record_attempt_inference(self, route: ModelRouteLease) -> bool:
        if route.route_lease_id in self._recorded_inference_routes:
            return True
        assert self.inference is not None
        summary = self.inference.attempt_summary(route.route_lease_id)
        if summary is not None and (
            summary.request_inflight or not summary.context_cleared
        ):
            self._record(
                route.run_id,
                "model_route_cleanup_incomplete",
                task_id=route.task_id,
                attempt=route.attempt,
                route_lease_id=route.route_lease_id,
                model_instance_id=route.instance_id,
                payload={
                    "request_inflight": summary.request_inflight,
                    "context_cleared": summary.context_cleared,
                },
            )
            return False
        for request in self.inference.request_records(route.route_lease_id):
            attempt_snapshot = next(
                item
                for item in self.state.snapshot(request.run_id)
                .task(request.task_id)
                .attempts
                if item.attempt == request.attempt
            )
            self._record(
                request.run_id,
                "inference_request",
                task_id=request.task_id,
                attempt=request.attempt,
                lease_id=attempt_snapshot.lease_id,
                route_lease_id=request.route_lease_id,
                model_instance_id=request.instance_id,
                payload={
                    "call_index": request.call_index,
                    "model_id": request.model_id,
                    "instance_generation": request.instance_generation,
                    "instance_placement_lease_id": (
                        request.instance_placement_lease_id
                    ),
                    "started_at_ms": request.started_at_ms,
                    "duration_ms": request.duration_ms,
                    "status": request.status,
                    "input_tokens": request.input_tokens,
                    "output_tokens": request.output_tokens,
                    "engine_queue_depth": request.engine_queue_depth,
                    "prefix_cache_hit": request.prefix_cache_hit,
                    "ttft_ms": request.ttft_ms,
                    "error_code": request.error_code,
                },
            )
        self._record(
            route.run_id,
            "attempt_inference_summary",
            task_id=route.task_id,
            attempt=route.attempt,
            route_lease_id=route.route_lease_id,
            model_instance_id=route.instance_id,
            payload={
                "model_id": route.model_id,
                "instance_generation": route.instance_generation,
                "request_count": 0 if summary is None else summary.request_count,
                "request_inflight": False,
                "context_cleared": summary is not None and summary.context_cleared,
            },
        )
        self._recorded_inference_routes.add(route.route_lease_id)
        return True

    def _matches_published_result(self, event: RuntimeEvent) -> bool:
        execution = self._runs.get(event.run_id)
        if execution is None:
            return False
        try:
            return self.indexes.get(event.run_id).matches_published_outputs(
                task_id=event.task_id,
                output_handles=dict(event.output_handles),
                controller_generation=execution.index_ref.controller_generation,
                index_generation=execution.index_ref.index_generation,
            )
        except RunDataIndexError:
            return False

    async def _fail_internal_runtime_event(
        self,
        event: RuntimeEvent,
        exc: Exception,
    ) -> None:
        await self._release_event_outputs(event)
        if not self.state.matches_active_attempt(
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            dispatch_id=event.dispatch_id,
        ):
            return
        error = self._error(
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            dispatch_id=event.dispatch_id,
            lease_id=event.lease_id,
            error_code="backend_internal_error",
            category="control",
            origin="control",
            phase="cleanup",
            message=f"{type(exc).__name__}: {exc}",
        )
        await self._handle_attempt_failure(
            run_id=event.run_id,
            task_id=event.task_id,
            attempt=event.attempt,
            dispatch_id=event.dispatch_id,
            lease_id=event.lease_id,
            error=error,
            attempt_status=AttemptStatus.FAILED,
            dispatch_handle=self._dispatches.get(event.dispatch_id),
        )

    def _definition(self, run_id: str, task_id: str) -> TaskDefinition:
        execution = self._runs[run_id]
        definition_id = execution.compiled.tasks[task_id].definition_id
        return execution.compiled.definitions[definition_id]

    def _mark_blocked(self, key: TaskKey, reason: str) -> None:
        blocked = self._blocked.get(key)
        if blocked is None:
            self._blocked[key] = _BlockedRecord(
                blocked_since_ms=self.clock.monotonic_ms(),
                bypass_count=0,
                last_reason=reason,
            )
        else:
            blocked.last_reason = reason

    def _bypass_exhausted(self, key: TaskKey) -> bool:
        blocked = self._blocked.get(key)
        return blocked is not None and blocked.bypass_count >= self.max_bypass_count

    def _record_scheduling_decision(
        self,
        *,
        run_id: str,
        task_id: str,
        partition: str,
        proposal: DispatchProposal,
        proposal_rank: int,
        placement_selected: bool,
        pending_reason: str | None,
        policy_select_ms: float,
        placement_ms: float,
    ) -> None:
        blocked = self._blocked.get(proposal.task_key)
        try:
            policy_metadata: object = dict(proposal.policy_metadata)
        except Exception as exc:
            policy_metadata = {
                "metadata_error": f"{type(exc).__name__}: {exc}",
            }
        self._record(
            run_id,
            "scheduling_decision",
            task_id=task_id,
            payload={
                "policy_name": self.policy.name,
                "policy_version": self.policy.version,
                "partition": partition,
                "proposal_rank": proposal_rank,
                "policy_metadata": policy_metadata,
                "placement_selected": placement_selected,
                "pending_reason": pending_reason,
                "score_compute_ms": proposal.score_compute_ms,
                "policy_select_ms": policy_select_ms,
                "placement_ms": placement_ms,
                "queue_length": sum(
                    queued.partition == partition for queued in self._queued.values()
                ),
                "blocked_since_ms": (
                    None if blocked is None else blocked.blocked_since_ms
                ),
                "bypass_count": 0 if blocked is None else blocked.bypass_count,
            },
        )

    async def _expect_runtime_producer(
        self, run_id: str, lease: PlacementLease
    ) -> None:
        producer_id = self.runtime.producer_for_lease(lease)
        if producer_id is not None:
            if self.runtime.producer_is_persistent(lease):
                self._runs[run_id].expected_producer_ids.add(producer_id)
            try:
                self.recorder.expect_producer(run_id, producer_id)
            except Exception as exc:
                self._record_recorder_error(run_id, exc)
        try:
            await self.runtime.prepare_run_recording(
                self._runs[run_id].recording_context, lease
            )
        except Exception as exc:
            self._record_recorder_error(run_id, exc)

    def _record_recorder_error(self, run_id: str, exc: Exception) -> None:
        try:
            self.recorder.record_writer_error(
                run_id,
                f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass

    def _record(
        self,
        run_id: str,
        event_type: str,
        *,
        task_id: str | None = None,
        attempt: int | None = None,
        lease_id: str | None = None,
        route_lease_id: str | None = None,
        model_instance_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        self._producer_sequence += 1
        try:
            frozen_payload = freeze_canonical(payload or {})
            if not isinstance(frozen_payload, FrozenMap):
                raise TypeError("recording payload must freeze to a mapping")
            event = ExecutionEvent(
                schema_version=1,
                event_id=new_id("event"),
                experiment_id=run_id,
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                lease_id=lease_id,
                route_lease_id=route_lease_id,
                model_instance_id=model_instance_id,
                event_type=event_type,
                producer_id=self.controller_producer_id,
                producer_sequence=self._producer_sequence,
                node_id=None,
                device_id=None,
                monotonic_time_ms=self.clock.monotonic_ms(),
                wall_time_ms=self.clock.wall_ms(),
                duration_ms=None,
                payload=frozen_payload,
            )
        except Exception as exc:
            try:
                self.recorder.record_writer_error(
                    run_id,
                    f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
            return
        try:
            self.recorder.emit(event)
        except Exception as exc:
            try:
                self.recorder.record_writer_error(
                    run_id,
                    f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
        if self._control_event_sink is not None:
            try:
                self._control_event_sink(event)
            except Exception:
                pass

    def _error(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        dispatch_id: str | None,
        lease_id: str | None,
        error_code: str,
        category: str,
        origin: str,
        phase: str,
        message: str,
        route_lease_id: str | None = None,
        model_instance_id: str | None = None,
    ) -> ErrorInfo:
        return ErrorInfo(
            schema_version=1,
            error_code=error_code,
            category=category,
            origin=origin,
            message=message,
            retryable_hint=False,
            classification_confidence="exact",
            execution_phase=phase,
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            dispatch_id=dispatch_id,
            lease_id=lease_id,
            route_lease_id=route_lease_id,
            model_instance_id=model_instance_id,
            occurred_at_ms=self.clock.monotonic_ms(),
        )

    def _new_future(self) -> asyncio.Future[Any]:
        return asyncio.get_running_loop().create_future()

    @staticmethod
    def _complete_future(future: asyncio.Future[Any], result: object) -> bool:
        if future.done():
            return False
        future.set_result(result)
        return True
