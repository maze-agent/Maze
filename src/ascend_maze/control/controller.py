"""PREPARING/COMMITTED/ABORTED transaction around SchedulerCore commit."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from time import perf_counter
from typing import cast

from ascend_maze.compiler.ir import CompiledWorkflow
from ascend_maze.contracts.data import (
    DataHandle,
    DataStore,
    shared_file_from_handle,
)
from ascend_maze.contracts.recording import (
    ExecutionEvent,
    ExecutionRecorder,
    FlushResult,
    HistoricalEventReader,
    RunEventPage,
    RunRecordingContext,
)
from ascend_maze.contracts.runtime import CodePackage
from ascend_maze.contracts.runtime import CodeHandle
from ascend_maze.contracts.submission import (
    RunInputIdentity,
    SubmissionContract,
    SubmissionState,
)
from ascend_maze.core.clock import Clock, SystemClock
from ascend_maze.core.errors import (
    ResponseLostError,
    StateTransitionError,
    SubmissionConflictError,
)
from ascend_maze.core.identifiers import new_id
from ascend_maze.data import InMemoryDataStore, RunDataIndexRegistry
from ascend_maze.lifecycle import DeadlineManager, RunStateManager
from ascend_maze.lifecycle import RunSnapshot
from ascend_maze.inference import (
    InferenceCoordinator,
    ModelInstanceRecoveryRecord,
    ModelInstanceState,
)
from ascend_maze.placement import (
    LeaseStatus,
    NodeCapacity,
    NodeSnapshot,
    NodeStatus,
    PlacementManager,
)
from ascend_maze.recording import InMemoryRecorder
from ascend_maze.resources import DeclaredOnlyAnchorProvider, ResourceAnchorProvider
from ascend_maze.runtime import FakeRuntimeBackend
from ascend_maze.scheduler import (
    DestroyResult,
    FcfsPolicy,
    HeterogeneousPartitioner,
    QueuePartitioner,
    SchedulingPolicy,
    SchedulerCore,
)
from ascend_maze.scheduler.core import SchedulerRunRecovery, SchedulerRuntimeBackend

from ascend_maze.control.lifecycle import (
    ControllerLifecycleState,
    NodeAction,
    NodeActionResult,
    ShutdownMode,
    ShutdownResource,
    ShutdownResult,
)

from ascend_maze.control.recovery import (
    ControllerCheckpoint,
    ControllerRecoveryStore,
    RecoveryClaim,
    RecoveryIdentity,
    RunRecoveryRecord,
    SubmissionRecoveryRecord,
)
from ascend_maze.control.contracts import RequestJournal
from ascend_maze.control.snapshots import ControlEventBuffer, SnapshotMeta, WatchRunBatch


@dataclass(frozen=True, slots=True)
class SubmitRequest:
    compiled: CompiledWorkflow
    code_packages: tuple[CodePackage, ...]
    workflow_inputs: tuple[tuple[str, DataHandle], ...]
    contract: SubmissionContract

    def input_map(self) -> dict[str, DataHandle]:
        return dict(self.workflow_inputs)


def _run_input_identity(name: str, handle: DataHandle) -> RunInputIdentity:
    shared_file = shared_file_from_handle(handle)
    if shared_file is not None:
        return RunInputIdentity.from_shared_file(name, shared_file)
    return RunInputIdentity.from_data_handle(name, handle)


@dataclass(frozen=True, slots=True)
class SubmissionOutcome:
    submission_id: str
    state: SubmissionState
    run_id: str | None
    submission_payload_hash: str
    replayed: bool
    error: str | None = None


@dataclass(slots=True)
class _SubmissionRecord:
    payload_hash: str
    state: SubmissionState
    request: SubmitRequest
    run_id: str | None = None
    error: str | None = None


class InMemoryController:
    """Own the stage-two components and expose an in-process control surface."""

    def __init__(
        self,
        *,
        config_fingerprint: str,
        environment_fingerprint: str,
        build_revision: str,
        node_capacities: tuple[NodeCapacity, ...],
        cluster_id: str = "local",
        controller_generation: str | None = None,
        data_owner_generation: str | None = None,
        data_store_descriptor: object | None = None,
        recovery_store: ControllerRecoveryStore | None = None,
        recovery_claim: RecoveryClaim | None = None,
        clock: Clock | None = None,
        data_store: DataStore | None = None,
        recorder: ExecutionRecorder | None = None,
        runtime: SchedulerRuntimeBackend | None = None,
        anchors: ResourceAnchorProvider | None = None,
        placement: PlacementManager | None = None,
        policy: SchedulingPolicy | None = None,
        partitioner: QueuePartitioner | None = None,
        placement_lookahead: int = 8,
        max_bypass_count: int = 8,
        dispatch_timeout_ms: int = 5_000,
        recorder_flush_timeout_ms: int = 1_000,
        inference: InferenceCoordinator | None = None,
        shutdown_drain_timeout_ms: int = 5_000,
        shutdown_cleanup_timeout_ms: int = 30_000,
        control_event_retention_count: int = 10_000,
    ) -> None:
        for name, value, minimum in (
            ("shutdown_drain_timeout_ms", shutdown_drain_timeout_ms, 0),
            ("shutdown_cleanup_timeout_ms", shutdown_cleanup_timeout_ms, 1),
            ("recorder_flush_timeout_ms", recorder_flush_timeout_ms, 1),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < minimum
            ):
                raise ValueError(f"{name} must be at least {minimum}")
        self.config_fingerprint = config_fingerprint
        self.environment_fingerprint = environment_fingerprint
        self.build_revision = build_revision
        self.last_submit_trace: dict[str, object] = {}
        self.cluster_id = cluster_id
        self.controller_generation = controller_generation or new_id("controller")
        self.clock = clock or SystemClock()
        self.recovery_store = recovery_store
        self.recovery_identity = RecoveryIdentity(
            cluster_id=cluster_id,
            config_fingerprint=config_fingerprint,
            environment_fingerprint=environment_fingerprint,
            build_revision=build_revision,
        )
        if recovery_claim is not None and recovery_store is None:
            raise ValueError("recovery_claim requires recovery_store")
        if recovery_claim is not None and (
            recovery_claim.controller_generation != self.controller_generation
        ):
            raise ValueError("recovery claim generation mismatch")
        self._recovery_claim = recovery_claim or (
            None
            if recovery_store is None
            else recovery_store.claim_generation(
                identity=self.recovery_identity,
                controller_generation=self.controller_generation,
            )
        )
        recovered = (
            None if self._recovery_claim is None else self._recovery_claim.checkpoint
        )
        if (
            recovered is not None
            and data_owner_generation is not None
            and recovered.data_owner_generation != data_owner_generation
        ):
            raise ValueError("configured data owner generation conflicts with recovery")
        self.data_owner_generation = (
            recovered.data_owner_generation
            if recovered is not None
            else (data_owner_generation or self.controller_generation)
        )
        self.controller_producer_id = f"controller:{self.data_owner_generation}"
        self.data_store_descriptor = (
            recovered.data_store_descriptor
            if recovered is not None
            else data_store_descriptor
        )
        self.data_store: DataStore = data_store or InMemoryDataStore()
        self.indexes = RunDataIndexRegistry(
            controller_generation=self.controller_generation,
            data_owner_generation=self.data_owner_generation,
            data_store=self.data_store,
        )
        self.state = RunStateManager()
        self.deadlines = DeadlineManager()
        self.anchors = anchors or DeclaredOnlyAnchorProvider(
            environment_fingerprint=environment_fingerprint
        )
        self.placement = placement or PlacementManager()
        for capacity in node_capacities:
            self.placement.register_node(capacity)
        self.recorder: ExecutionRecorder = recorder or InMemoryRecorder()
        self.control_events = ControlEventBuffer(
            retention_count=control_event_retention_count
        )
        self.request_journal = RequestJournal()
        self.inference = inference
        if inference is not None and recovery_store is not None:
            inference.set_state_change_sink(self._schedule_checkpoint)
        if (
            inference is not None
            and inference.instances.placement is not self.placement
        ):
            raise ValueError(
                "InferenceCoordinator and Controller must share PlacementManager"
            )
        if inference is not None:
            mismatched_models = tuple(
                spec.model_id
                for spec in inference.catalog.specs
                if spec.environment_fingerprint != environment_fingerprint
            )
            if mismatched_models:
                raise ValueError(
                    "model environment fingerprint does not match Controller: "
                    + ", ".join(mismatched_models)
                )
        if runtime is None:
            if not isinstance(self.data_store, InMemoryDataStore):
                raise TypeError("default FakeRuntime requires InMemoryDataStore")
            runtime = FakeRuntimeBackend(
                data_store=self.data_store,
                owner_generation=self.data_owner_generation,
                environment_fingerprint=environment_fingerprint,
                inference=inference,
            )
        self.runtime: SchedulerRuntimeBackend = runtime
        self.core = SchedulerCore(
            state=self.state,
            deadlines=self.deadlines,
            indexes=self.indexes,
            anchors=self.anchors,
            placement=self.placement,
            runtime=self.runtime,
            recorder=self.recorder,
            policy=policy or FcfsPolicy(),
            partitioner=partitioner or HeterogeneousPartitioner(),
            clock=self.clock,
            placement_lookahead=placement_lookahead,
            max_bypass_count=max_bypass_count,
            dispatch_timeout_ms=dispatch_timeout_ms,
            recorder_flush_timeout_ms=recorder_flush_timeout_ms,
            controller_producer_id=self.controller_producer_id,
            inference=inference,
            checkpoint_sink=(
                self._save_checkpoint if recovery_store is not None else None
            ),
            control_event_sink=self._capture_control_event,
        )
        self._submissions: dict[str, _SubmissionRecord] = {}
        self._submit_lock = asyncio.Lock()
        self._submission_tasks: set[asyncio.Task[SubmissionOutcome]] = set()
        self._failure_points: set[str] = set()
        self._started = False
        self._checkpoint_sequence = (
            0 if recovered is None else recovered.checkpoint_sequence
        )
        self._checkpoint_lock = asyncio.Lock()
        self._checkpoint_tasks: set[asyncio.Task[None]] = set()
        self._checkpoint_schedule_dirty = False
        self._pending_recovery = recovered
        self.shutdown_drain_timeout_ms = shutdown_drain_timeout_ms
        self.shutdown_cleanup_timeout_ms = shutdown_cleanup_timeout_ms
        self._lifecycle_state = ControllerLifecycleState.CREATED
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_result: ShutdownResult | None = None
        self._started_wall_time_ms: int | None = None
        self._stopped_event = asyncio.Event()
        self._node_action_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        if self._started:
            return
        if self._lifecycle_state is ControllerLifecycleState.STOPPED:
            raise RuntimeError("a stopped Controller cannot be restarted")
        if self._pending_recovery is not None:
            self._restore_checkpoint(self._pending_recovery)
        await self.core.start()
        if self._pending_recovery is not None:
            await self._prepare_recovered_code(self._pending_recovery)
            previous = self._pending_recovery.controller_generation
            await self.core.reconcile_recovered_runs(previous)
            self._pending_recovery = None
        self._started = True
        self._started_wall_time_ms = self.clock.wall_ms()
        self._lifecycle_state = ControllerLifecycleState.READY
        await self._save_checkpoint()

    @property
    def lifecycle_state(self) -> ControllerLifecycleState:
        return self._lifecycle_state

    async def close(
        self,
        *,
        force: bool = False,
        drain_timeout_ms: int | None = None,
    ) -> ShutdownResult:
        return await self.shutdown(
            force=force,
            drain_timeout_ms=drain_timeout_ms,
        )

    async def shutdown(
        self,
        *,
        force: bool = False,
        drain_timeout_ms: int | None = None,
    ) -> ShutdownResult:
        timeout_ms = (
            self.shutdown_drain_timeout_ms
            if drain_timeout_ms is None
            else drain_timeout_ms
        )
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or timeout_ms < 0
        ):
            raise ValueError("drain_timeout_ms must be non-negative")
        async with self._shutdown_lock:
            if self._shutdown_result is not None:
                return self._shutdown_result
            mode = ShutdownMode.FORCE if force else ShutdownMode.GRACEFUL
            started_at_ms = self.clock.monotonic_ms()
            if not self._started:
                self._lifecycle_state = ControllerLifecycleState.STOPPED
                self._stopped_event.set()
                result = self._make_shutdown_result(
                    mode=mode,
                    started_at_ms=started_at_ms,
                    active_run_ids=(),
                    drained_run_ids=(),
                    terminated_run_ids=(),
                    flush_results=(),
                    incomplete_resources=(),
                    steps=("stopped",),
                    errors=(),
                )
                self._shutdown_result = result
                return result

            await self._assert_current_generation()
            steps: list[str] = []
            errors: list[str] = []
            flush_results: list[FlushResult] = []
            recording_run_ids = self.core.recordable_run_ids()
            async with self._submit_lock:
                self._lifecycle_state = ControllerLifecycleState.DRAINING
                await self.core.begin_draining()
            steps.extend(("draining", "new_work_rejected", "dispatch_stopped"))
            active_run_ids = self.core.nonterminal_run_ids()
            for run_id in self.core.recordable_run_ids():
                self.core.record_control_event(
                    run_id,
                    "controller_draining",
                    payload={
                        "mode": mode.value,
                        "drain_timeout_ms": timeout_ms,
                    },
                )

            drained: tuple[str, ...] = ()
            if not force and active_run_ids and timeout_ms > 0:
                drained = await self._wait_for_run_drain(
                    active_run_ids,
                    timeout_ms=timeout_ms,
                )
            steps.append("runs_drained")
            remaining = self.core.nonterminal_run_ids()
            terminated: list[str] = []
            for run_id in remaining:
                try:
                    await self.core.cancel_run(
                        run_id,
                        reason=(
                            "forced_controller_shutdown"
                            if force
                            else "controller_drain_deadline"
                        ),
                    )
                    terminated.append(run_id)
                except Exception as exc:
                    errors.append(
                        f"terminate_run:{run_id}:{type(exc).__name__}:{exc}"
                    )
            steps.append("remaining_runs_cancelled")

            try:
                await self.core.shutdown(terminate_active_runs=False)
            except Exception as exc:
                errors.append(f"scheduler_stop:{type(exc).__name__}:{exc}")
                try:
                    await self.core.shutdown(terminate_active_runs=True)
                except Exception as fallback_exc:
                    errors.append(
                        "scheduler_force_stop:"
                        f"{type(fallback_exc).__name__}:{fallback_exc}"
                    )
            if self.core.running:
                try:
                    await self.core.abandon()
                except Exception as exc:
                    errors.append(f"scheduler_abandon:{type(exc).__name__}:{exc}")
            steps.append("models_stopped")

            try:
                await self._stop_worker_runtime()
            except Exception as exc:
                errors.append(f"worker_runtime_stop:{type(exc).__name__}:{exc}")
            steps.append("worker_pool_stopped")

            incomplete = list(self._collect_incomplete_resources())
            incomplete.extend(self._collect_extra_incomplete_resources())
            steps.append("leases_confirmed_or_quarantined")
            for run_id in self.core.recordable_run_ids():
                self.core.record_control_event(
                    run_id,
                    "controller_shutdown",
                    payload={
                        "forced": force,
                        "cleanup_confirmed": not incomplete and not errors,
                        "incomplete_resources": tuple(
                            f"{item.kind}:{item.resource_id}:{item.state}"
                            for item in incomplete
                        ),
                    },
                )
                try:
                    flush_results.append(
                        await self.core.flush_run_recording(run_id)
                    )
                except Exception as exc:
                    errors.append(
                        f"recorder_flush:{run_id}:{type(exc).__name__}:{exc}"
                    )
            try:
                await self.recorder.close(self.shutdown_cleanup_timeout_ms)
            except Exception as exc:
                errors.append(f"recorder_close:{type(exc).__name__}:{exc}")
            steps.append("recorder_flushed_closed")

            try:
                await self._stop_runtime_generation()
            except Exception as exc:
                errors.append(f"runtime_generation_stop:{type(exc).__name__}:{exc}")
            steps.append("runtime_generation_stopped")
            try:
                await self._stop_control_transports()
            except Exception as exc:
                errors.append(f"control_transport_stop:{type(exc).__name__}:{exc}")
            steps.append("control_transports_stopped")

            self._started = False
            self._lifecycle_state = ControllerLifecycleState.STOPPED
            self._stopped_event.set()
            await self._wait_checkpoint_tasks()
            try:
                await self._save_checkpoint()
            except Exception as exc:
                errors.append(f"checkpoint:{type(exc).__name__}:{exc}")
            steps.append("stopped")
            final_incomplete = list(self._collect_incomplete_resources())
            final_incomplete.extend(self._collect_extra_incomplete_resources())
            result = self._make_shutdown_result(
                mode=mode,
                started_at_ms=started_at_ms,
                active_run_ids=active_run_ids,
                drained_run_ids=tuple(sorted(set(drained) - set(terminated))),
                terminated_run_ids=tuple(sorted(terminated)),
                flush_results=tuple(flush_results),
                recording_run_ids=recording_run_ids,
                incomplete_resources=tuple(final_incomplete),
                steps=tuple(steps),
                errors=tuple(errors),
            )
            self._shutdown_result = result
            return result

    async def _wait_for_run_drain(
        self,
        run_ids: tuple[str, ...],
        *,
        timeout_ms: int,
    ) -> tuple[str, ...]:
        waiters = {
            asyncio.create_task(self.core.wait_terminal(run_id)): run_id
            for run_id in run_ids
            if not self.core.snapshot(run_id).terminal
        }
        if not waiters:
            return tuple(sorted(run_ids))
        done, pending = await asyncio.wait(
            waiters,
            timeout=timeout_ms / 1_000,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        drained = {
            waiters[task]
            for task in done
            if not task.cancelled() and task.exception() is None
        }
        drained.update(
            run_id for run_id in run_ids if self.core.snapshot(run_id).terminal
        )
        return tuple(sorted(drained))

    async def _stop_worker_runtime(self) -> None:
        await self.runtime.close()

    async def _stop_runtime_generation(self) -> None:
        return None

    async def _stop_control_transports(self) -> None:
        return None

    def _collect_extra_incomplete_resources(
        self,
    ) -> tuple[ShutdownResource, ...]:
        return ()

    def _collect_incomplete_resources(self) -> tuple[ShutdownResource, ...]:
        resources: list[ShutdownResource] = []
        if self.core.running:
            resources.append(
                ShutdownResource(
                    kind="scheduler_loop",
                    resource_id="scheduler_core",
                    state="running",
                    details={"dispatch_enabled": self.core.dispatch_enabled},
                )
            )
        for snapshot in self.placement.lease_snapshots():
            if snapshot.status not in {LeaseStatus.RESERVED, LeaseStatus.BOUND}:
                continue
            lease = snapshot.lease
            resources.append(
                ShutdownResource(
                    kind="placement_lease",
                    resource_id=lease.lease_id,
                    state=snapshot.status.value,
                    node_id=lease.node_id,
                    details={
                        "reservation_kind": lease.reservation_kind,
                        "run_id": lease.run_id,
                        "task_id": lease.task_id,
                        "attempt": lease.attempt,
                        "npu_device_id": lease.npu_device_id,
                    },
                )
            )
        for route_lease_id in self.core.active_route_lease_ids():
            resources.append(
                ShutdownResource(
                    kind="model_route_lease",
                    resource_id=route_lease_id,
                    state="active",
                )
            )
        for dispatch_id in self.core.active_dispatch_ids():
            resources.append(
                ShutdownResource(
                    kind="runtime_dispatch",
                    resource_id=dispatch_id,
                    state="cleanup_unconfirmed",
                )
            )
        if self.deadlines.active_count:
            resources.append(
                ShutdownResource(
                    kind="deadline_timer",
                    resource_id="controller_deadline_heap",
                    state="active",
                    details={"active_count": self.deadlines.active_count},
                )
            )
        if self.inference is not None:
            for instance in self.inference.model_instances():
                if instance.state in {
                    ModelInstanceState.FAILED,
                    ModelInstanceState.STOPPED,
                }:
                    continue
                resources.append(
                    ShutdownResource(
                        kind="model_instance",
                        resource_id=instance.instance_id,
                        state=instance.state.value,
                        node_id=instance.node_id,
                        details={
                            "generation": instance.generation,
                            "service_handle_id": instance.service_handle_id,
                            "placement_lease_id": instance.placement_lease_id,
                        },
                    )
                )
        return tuple(
            sorted(
                resources,
                key=lambda item: (item.kind, item.resource_id),
            )
        )

    def _make_shutdown_result(
        self,
        *,
        mode: ShutdownMode,
        started_at_ms: int,
        active_run_ids: tuple[str, ...],
        drained_run_ids: tuple[str, ...],
        terminated_run_ids: tuple[str, ...],
        flush_results: tuple[FlushResult, ...],
        recording_run_ids: tuple[str, ...] = (),
        incomplete_resources: tuple[ShutdownResource, ...],
        steps: tuple[str, ...],
        errors: tuple[str, ...],
    ) -> ShutdownResult:
        recording_complete = (
            {item.run_id for item in flush_results} == set(recording_run_ids)
            and all(item.recording_complete for item in flush_results)
            and not any(error.startswith("recorder_") for error in errors)
        )
        cleanup_confirmed = not incomplete_resources and not errors
        return ShutdownResult(
            mode=mode,
            lifecycle_state=ControllerLifecycleState.STOPPED,
            started_at_ms=started_at_ms,
            finished_at_ms=max(started_at_ms, self.clock.monotonic_ms()),
            active_run_ids_at_start=tuple(sorted(active_run_ids)),
            drained_run_ids=tuple(sorted(drained_run_ids)),
            terminated_run_ids=tuple(sorted(terminated_run_ids)),
            recording_run_ids=tuple(sorted(recording_run_ids)),
            flush_results=tuple(sorted(flush_results, key=lambda item: item.run_id)),
            incomplete_resources=tuple(
                sorted(
                    incomplete_resources,
                    key=lambda item: (item.kind, item.resource_id),
                )
            ),
            steps=steps,
            errors=errors,
            recording_complete=recording_complete,
            cleanup_confirmed=cleanup_confirmed,
            exit_code=0 if cleanup_confirmed and recording_complete else 1,
        )

    async def crash(self) -> None:
        """Simulate abrupt authority loss while preserving recovery-owned state."""

        if not self._started:
            return
        try:
            await self._wait_checkpoint_tasks()
            await self._save_checkpoint()
        except StateTransitionError:
            pass
        await self.core.abandon()
        await self._wait_checkpoint_tasks()
        self._started = False
        self._lifecycle_state = ControllerLifecycleState.STOPPED
        self._stopped_event.set()

    def inject_submit_failure(self, point: str) -> None:
        if point not in {
            "after_prepare",
            "after_open_run",
            "before_commit",
            "after_commit",
        }:
            raise ValueError("unknown submission failure point")
        self._failure_points.add(point)

    async def wait_stopped(self) -> None:
        if self._lifecycle_state is ControllerLifecycleState.STOPPED:
            return
        await self._stopped_event.wait()

    async def submit(
        self,
        request: SubmitRequest,
        *,
        lose_response_after_commit: bool = False,
    ) -> SubmissionOutcome:
        transaction = asyncio.create_task(
            self._submit_transaction(
                request,
                lose_response_after_commit=lose_response_after_commit,
            ),
            name=f"maze-submit:{request.contract.submission_id}",
        )
        self._submission_tasks.add(transaction)
        transaction.add_done_callback(self._submission_task_finished)
        return await asyncio.shield(transaction)

    async def _submit_transaction(
        self,
        request: SubmitRequest,
        *,
        lose_response_after_commit: bool = False,
    ) -> SubmissionOutcome:
        trace_started = perf_counter()
        trace: dict[str, object] = {
            "submission_id": request.contract.submission_id,
            "state": "started",
        }
        if not self._started:
            raise RuntimeError("controller is not started")
        stage_started = perf_counter()
        await self._assert_current_generation()
        trace["assert_generation_before_lock_ms"] = _elapsed_ms(stage_started)
        contract = request.contract
        stage_started = perf_counter()
        async with self._submit_lock:
            trace["lock_wait_ms"] = _elapsed_ms(stage_started)
            stage_started = perf_counter()
            if self._lifecycle_state is not ControllerLifecycleState.READY:
                raise StateTransitionError(
                    f"controller is {self._lifecycle_state.value}; submissions are closed"
                )
            await self._assert_current_generation()
            trace["ready_and_generation_check_ms"] = _elapsed_ms(stage_started)
            stage_started = perf_counter()
            existing = self._submissions.get(contract.submission_id)
            trace["existing_lookup_ms"] = _elapsed_ms(stage_started)
            if existing is not None:
                if existing.payload_hash != contract.submission_payload_hash:
                    raise SubmissionConflictError(
                        "submission_id already exists with a different payload"
                    )
                if existing.state is SubmissionState.COMMITTED:
                    outcome = SubmissionOutcome(
                        submission_id=contract.submission_id,
                        state=existing.state,
                        run_id=existing.run_id,
                        submission_payload_hash=existing.payload_hash,
                        replayed=True,
                    )
                    trace["state"] = "replayed_committed"
                    trace["total_ms"] = _elapsed_ms(trace_started)
                    self.last_submit_trace = trace
                    return outcome
                if existing.state is SubmissionState.ABORTED:
                    outcome = SubmissionOutcome(
                        submission_id=contract.submission_id,
                        state=existing.state,
                        run_id=None,
                        submission_payload_hash=existing.payload_hash,
                        replayed=True,
                        error=existing.error,
                    )
                    trace["state"] = "replayed_aborted"
                    trace["total_ms"] = _elapsed_ms(trace_started)
                    self.last_submit_trace = trace
                    return outcome
                raise RuntimeError("submission is unexpectedly still PREPARING")

            stage_started = perf_counter()
            record = _SubmissionRecord(
                payload_hash=contract.submission_payload_hash,
                state=SubmissionState.PREPARING,
                request=request,
            )
            self._submissions[contract.submission_id] = record
            trace["preparing_record_ms"] = _elapsed_ms(stage_started)
            stage_started = perf_counter()
            await self._save_checkpoint()
            trace["preparing_checkpoint_ms"] = _elapsed_ms(stage_started)
            code_handles: tuple[CodeHandle, ...] = ()
            provisional_run_id: str | None = None
            recording_open = False
            try:
                stage_started = perf_counter()
                self._validate_request(request)
                trace["validate_request_ms"] = _elapsed_ms(stage_started)
                stage_started = perf_counter()
                code_handles = await self.runtime.prepare(request.code_packages)
                trace["runtime_prepare_ms"] = _elapsed_ms(stage_started)
                self._maybe_fail("after_prepare")
                stage_started = perf_counter()
                provisional_run_id = new_id("run")
                recording_context = RunRecordingContext(
                    schema_version=1,
                    experiment_id=provisional_run_id,
                    run_id=provisional_run_id,
                    workflow_fingerprint=request.compiled.workflow_fingerprint,
                    config_fingerprint=self.config_fingerprint,
                    environment_fingerprint=self.environment_fingerprint,
                    build_revision=self.build_revision,
                    started_wall_time_ms=self.clock.wall_ms(),
                    initial_expected_producer_ids=(self.controller_producer_id,),
                )
                trace["recording_context_ms"] = _elapsed_ms(stage_started)
                stage_started = perf_counter()
                self.recorder.open_run(recording_context)
                recording_open = True
                trace["recording_open_ms"] = _elapsed_ms(stage_started)
                self._maybe_fail("after_open_run")
                self._maybe_fail("before_commit")
                stage_started = perf_counter()
                submitted_at = self.clock.monotonic_ms()
                deadline_at = (
                    None
                    if contract.options.run_deadline_ms is None
                    else submitted_at + contract.options.run_deadline_ms
                )
                trace["deadline_compute_ms"] = _elapsed_ms(stage_started)
                stage_started = perf_counter()
                await self.core.commit_run(
                    run_id=provisional_run_id,
                    submission_id=contract.submission_id,
                    compiled=request.compiled,
                    workflow_inputs=request.input_map(),
                    code_handles=code_handles,
                    session_key_hash=contract.session_key_hash,
                    submitted_at_ms=submitted_at,
                    deadline_at_ms=deadline_at,
                    recording_context=recording_context,
                )
                trace["commit_run_ms"] = _elapsed_ms(stage_started)
            except Exception as exc:
                if code_handles:
                    stage_started = perf_counter()
                    await self.runtime.release_code(code_handles)
                    trace["abort_release_code_ms"] = _elapsed_ms(stage_started)
                if recording_open and provisional_run_id is not None:
                    stage_started = perf_counter()
                    self.recorder.abort_run(provisional_run_id)
                    trace["abort_recording_ms"] = _elapsed_ms(stage_started)
                record.state = SubmissionState.ABORTED
                record.error = f"{type(exc).__name__}: {exc}"
                stage_started = perf_counter()
                await self._save_checkpoint()
                trace["abort_checkpoint_ms"] = _elapsed_ms(stage_started)
                trace["state"] = "aborted"
                trace["error"] = record.error
                trace["total_ms"] = _elapsed_ms(trace_started)
                self.last_submit_trace = trace
                return SubmissionOutcome(
                    submission_id=contract.submission_id,
                    state=SubmissionState.ABORTED,
                    run_id=None,
                    submission_payload_hash=contract.submission_payload_hash,
                    replayed=False,
                    error=record.error,
                )

            self._maybe_fail("after_commit")
            stage_started = perf_counter()
            record.state = SubmissionState.COMMITTED
            record.run_id = provisional_run_id
            await self._save_checkpoint()
            trace["committed_checkpoint_ms"] = _elapsed_ms(stage_started)
            outcome = SubmissionOutcome(
                submission_id=contract.submission_id,
                state=SubmissionState.COMMITTED,
                run_id=provisional_run_id,
                submission_payload_hash=contract.submission_payload_hash,
                replayed=False,
            )
            trace["state"] = "committed"
            trace["run_id"] = provisional_run_id
            trace["total_ms"] = _elapsed_ms(trace_started)
            self.last_submit_trace = trace
            if lose_response_after_commit:
                raise ResponseLostError(
                    "submission committed but its response was intentionally lost"
                )
            return outcome

    def _submission_task_finished(
        self,
        task: asyncio.Task[SubmissionOutcome],
    ) -> None:
        self._submission_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def wait_run(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> RunSnapshot:
        return await self.core.wait_terminal(run_id, timeout_seconds=timeout_seconds)

    async def cancel_run(
        self,
        run_id: str,
        *,
        reason: str = "user_cancelled",
    ) -> RunSnapshot:
        await self._assert_current_generation()
        return await self.core.cancel_run(run_id, reason=reason)

    async def destroy_run(
        self,
        run_id: str,
        *,
        force: bool = False,
    ) -> DestroyResult:
        await self._assert_current_generation()
        return await self.core.destroy_run(run_id, force=force)

    def result(self, run_id: str, task_id: str) -> dict[str, object]:
        return self.core.result(run_id, task_id)

    def snapshot(self, run_id: str) -> RunSnapshot:
        return self.core.snapshot(run_id)

    def list_runs(self) -> tuple[RunSnapshot, ...]:
        return self.state.snapshots()

    def _capture_control_event(self, event: ExecutionEvent) -> None:
        self.control_events.append(event)

    def snapshot_meta(self, *, snapshot_version: int = 0) -> SnapshotMeta:
        return SnapshotMeta(
            schema_version=1,
            snapshot_version=snapshot_version,
            controller_generation=self.controller_generation,
            config_fingerprint=self.config_fingerprint,
            generated_at_ms=self.clock.wall_ms(),
        )

    def cluster_snapshot(self) -> object:
        return self.placement.snapshot()

    def cluster_snapshot_version(self) -> int:
        return self.placement.snapshot_version + self.control_events.latest_sequence

    def queue_snapshot(self) -> object:
        version = self.cluster_snapshot_version()
        return self.core.queue_snapshot(snapshot_version=version)

    def run_recording_result(self, run_id: str) -> FlushResult | None:
        return self.core.recording_result(run_id)

    def record_control_request(
        self,
        run_id: str,
        *,
        request_id: str,
        operation: str,
    ) -> None:
        self.core.record_control_event(
            run_id,
            "control_request",
            payload={
                "request_id": request_id,
                "operation": operation,
                "config_fingerprint": self.config_fingerprint,
                "controller_generation": self.controller_generation,
            },
        )

    async def flush_run(self, run_id: str) -> FlushResult:
        await self._assert_current_generation()
        if not self.snapshot(run_id).terminal:
            raise StateTransitionError("run must be terminal before recording flush")
        return await self.core.flush_run_recording(run_id)

    def task_result_handles(self, run_id: str, task_id: str) -> tuple[tuple[str, DataHandle], ...]:
        snapshot = self.snapshot(run_id)
        task = snapshot.task(task_id)
        if task.status.value != "succeeded":
            raise StateTransitionError("task result is unavailable before success")
        checkpoint = self.indexes.get(run_id).checkpoint()
        return tuple(
            (output_name, handle)
            for output_task_id, output_name, handle in checkpoint.task_outputs
            if output_task_id == task_id
        )

    async def drain_node(
        self,
        node_id: str,
        *,
        boot_id: str | None = None,
        force: bool = False,
        timeout_ms: int = 30_000,
    ) -> NodeActionResult:
        await self._assert_current_generation()
        if not boot_id:
            raise ValueError("node drain requires the current boot_id")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 0:
            raise ValueError("node drain timeout_ms must be non-negative")
        lock = self._node_action_locks.setdefault(node_id, asyncio.Lock())
        async with lock:
            started_at_ms = self.clock.monotonic_ms()
            node = self._node_snapshot(node_id)
            if node.capacity.boot_id != boot_id:
                raise StateTransitionError("node boot_id changed")
            if node.status not in {
                NodeStatus.HEALTHY,
                NodeStatus.DRAINING,
                NodeStatus.DRAINED,
            }:
                raise StateTransitionError(
                    f"node cannot drain from {node.status.value}"
                )
            await self._validate_node_drain(node_id, boot_id)
            if node.status is not NodeStatus.DRAINED:
                self.placement.set_node_status(
                    node_id,
                    NodeStatus.DRAINING,
                    now_ms=self.clock.monotonic_ms(),
                )
            await self._begin_node_drain(node_id, boot_id)

            cancelled: list[str] = []
            errors: list[str] = []
            if force:
                for run_id in self._node_affected_run_ids(node_id, boot_id):
                    if self.snapshot(run_id).terminal:
                        continue
                    try:
                        await self.core.cancel_run(
                            run_id,
                            reason=f"forced_node_drain:{node_id}:{boot_id}",
                        )
                        cancelled.append(run_id)
                    except Exception as exc:
                        errors.append(
                            f"cancel_run:{run_id}:{type(exc).__name__}:{exc}"
                        )

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_ms / 1_000
            incomplete: tuple[ShutdownResource, ...] = ()
            while True:
                try:
                    await self._advance_node_drain(node_id, boot_id)
                except Exception as exc:
                    errors.append(
                        f"advance_node_drain:{type(exc).__name__}:{exc}"
                    )
                incomplete = self._collect_node_incomplete_resources(node_id, boot_id)
                if not incomplete and not errors:
                    try:
                        await self._complete_node_drain(node_id, boot_id)
                    except Exception as exc:
                        errors.append(
                            f"complete_node_drain:{type(exc).__name__}:{exc}"
                        )
                        break
                    self.placement.set_node_status(
                        node_id,
                        NodeStatus.DRAINED,
                        now_ms=self.clock.monotonic_ms(),
                    )
                    break
                if errors:
                    break
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.025, remaining))

            finished_at_ms = max(started_at_ms, self.clock.monotonic_ms())
            timed_out = bool(incomplete) and not errors and loop.time() >= deadline
            cleanup_confirmed = not incomplete and not errors and not timed_out
            status = self._node_snapshot(node_id).status.value
            return NodeActionResult(
                action=NodeAction.DRAIN,
                node_id=node_id,
                boot_id=boot_id,
                status=status,
                started_at_ms=started_at_ms,
                finished_at_ms=finished_at_ms,
                forced=force,
                timed_out=timed_out,
                cleanup_confirmed=cleanup_confirmed,
                cancelled_run_ids=tuple(sorted(cancelled)),
                incomplete_resources=incomplete,
                errors=tuple(errors),
                exit_code=0 if cleanup_confirmed else 1,
            )

    async def resume_node(
        self, node_id: str, *, boot_id: str | None = None
    ) -> NodeActionResult:
        await self._assert_current_generation()
        if not boot_id:
            raise ValueError("node resume requires the current boot_id")
        lock = self._node_action_locks.setdefault(node_id, asyncio.Lock())
        async with lock:
            started_at_ms = self.clock.monotonic_ms()
            node = self._node_snapshot(node_id)
            if node.capacity.boot_id != boot_id:
                raise StateTransitionError("node boot_id changed")
            if node.status is NodeStatus.HEALTHY:
                return self._node_resume_result(node_id, boot_id, started_at_ms)
            if node.status is not NodeStatus.DRAINED:
                raise StateTransitionError("only a fully drained node can be resumed")
            incomplete = self._collect_node_incomplete_resources(node_id, boot_id)
            if incomplete:
                raise StateTransitionError("node still owns resources and cannot resume")
            await self._prepare_node_resume(node_id, boot_id)
            self.placement.set_node_status(
                node_id,
                NodeStatus.HEALTHY,
                now_ms=self.clock.monotonic_ms(),
            )
            if self.inference is not None:
                self.inference.end_node_drain(node_id, boot_id)
            self.core.post_resource_changed(f"node_resumed:{node_id}:{boot_id}")
            return self._node_resume_result(node_id, boot_id, started_at_ms)

    def _node_snapshot(self, node_id: str) -> NodeSnapshot:
        node = next(
            (
                item
                for item in self.placement.snapshot().nodes
                if item.capacity.node_id == node_id
            ),
            None,
        )
        if node is None:
            raise KeyError(node_id)
        return node

    async def _begin_node_drain(self, node_id: str, boot_id: str) -> None:
        if self.inference is not None:
            self.inference.begin_node_drain(node_id, boot_id)
        self.core.post_resource_changed(f"node_draining:{node_id}:{boot_id}")

    async def _validate_node_drain(self, node_id: str, boot_id: str) -> None:
        del node_id, boot_id

    async def _advance_node_drain(self, node_id: str, boot_id: str) -> None:
        if self.inference is not None:
            await self.inference.advance_node_drain(node_id, boot_id)

    async def _complete_node_drain(self, node_id: str, boot_id: str) -> None:
        del node_id, boot_id

    async def _prepare_node_resume(self, node_id: str, boot_id: str) -> None:
        node = self._node_snapshot(node_id)
        if node.capacity.boot_id != boot_id:
            raise StateTransitionError("node boot_id changed")
        environment = node.capacity.capabilities.get("environment_fingerprint")
        if environment is not None and environment != self.environment_fingerprint:
            raise StateTransitionError("node environment fingerprint changed")
        if any(
            not npu.healthy or npu.observed_free_hbm_mb is None
            for npu in node.capacity.npus
        ):
            raise StateTransitionError("node physical NPU observation is not healthy")

    def _node_affected_run_ids(self, node_id: str, boot_id: str) -> tuple[str, ...]:
        run_ids = {
            snapshot.lease.run_id
            for snapshot in self.placement.lease_snapshots()
            if snapshot.status in {LeaseStatus.RESERVED, LeaseStatus.BOUND}
            and snapshot.lease.node_id == node_id
            and snapshot.lease.boot_id == boot_id
            and snapshot.lease.run_id is not None
        }
        if self.inference is not None:
            run_ids.update(
                lease.run_id
                for lease in self.inference.active_routes_for_node(node_id, boot_id)
            )
        known_run_ids = set(self.core.run_ids())
        return tuple(sorted(run_id for run_id in run_ids if run_id in known_run_ids))

    def _collect_node_incomplete_resources(
        self, node_id: str, boot_id: str
    ) -> tuple[ShutdownResource, ...]:
        resources: list[ShutdownResource] = []
        for snapshot in self.placement.lease_snapshots():
            lease = snapshot.lease
            if (
                snapshot.status not in {LeaseStatus.RESERVED, LeaseStatus.BOUND}
                or lease.node_id != node_id
                or lease.boot_id != boot_id
            ):
                continue
            resources.append(
                ShutdownResource(
                    kind="placement_lease",
                    resource_id=lease.lease_id,
                    state=snapshot.status.value,
                    node_id=node_id,
                    details={
                        "reservation_kind": lease.reservation_kind,
                        "run_id": lease.run_id,
                        "task_id": lease.task_id,
                        "attempt": lease.attempt,
                        "npu_device_id": lease.npu_device_id,
                    },
                )
            )
        if self.inference is not None:
            for instance in self.inference.model_instances():
                if instance.node_id != node_id or instance.boot_id != boot_id:
                    continue
                if (
                    instance.state in {
                        ModelInstanceState.STOPPED,
                        ModelInstanceState.FAILED,
                    }
                    and instance.placement_lease_id is None
                    and instance.service_handle_id is None
                ):
                    continue
                resources.append(
                    ShutdownResource(
                        kind="model_instance",
                        resource_id=instance.instance_id,
                        state=instance.state.value,
                        node_id=node_id,
                        details={
                            "generation": instance.generation,
                            "route_occupancy": instance.route_occupancy,
                            "actual_request_inflight": instance.actual_request_inflight,
                            "service_handle_id": instance.service_handle_id,
                            "placement_lease_id": instance.placement_lease_id,
                        },
                    )
                )
        return tuple(
            sorted(resources, key=lambda item: (item.kind, item.resource_id))
        )

    def _node_resume_result(
        self, node_id: str, boot_id: str, started_at_ms: int
    ) -> NodeActionResult:
        finished_at_ms = max(started_at_ms, self.clock.monotonic_ms())
        return NodeActionResult(
            action=NodeAction.RESUME,
            node_id=node_id,
            boot_id=boot_id,
            status=self._node_snapshot(node_id).status.value,
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
            forced=False,
            timed_out=False,
            cleanup_confirmed=True,
            cancelled_run_ids=(),
            incomplete_resources=(),
            errors=(),
            exit_code=0,
        )

    def watch_run(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> WatchRunBatch:
        terminal = self.snapshot(run_id).terminal
        return self.control_events.read_run(
            run_id,
            after_sequence=after_sequence,
            limit=limit,
            terminal=terminal,
        )

    async def wait_run_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        timeout_seconds: float | None = None,
    ) -> WatchRunBatch:
        terminal = self.snapshot(run_id).terminal
        return await self.control_events.wait_run(
            run_id,
            after_sequence=after_sequence,
            limit=limit,
            timeout_seconds=timeout_seconds,
            terminal=terminal,
        )

    def get_run_events(
        self,
        run_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> RunEventPage:
        if not isinstance(self.recorder, HistoricalEventReader):
            raise RuntimeError("configured recorder has no historical event reader")
        return self.recorder.get_run_events(run_id, cursor=cursor, limit=limit)

    def submission_outcome(self, submission_id: str) -> SubmissionOutcome:
        record = self._submissions[submission_id]
        return SubmissionOutcome(
            submission_id=submission_id,
            state=record.state,
            run_id=record.run_id,
            submission_payload_hash=record.payload_hash,
            replayed=True,
            error=record.error,
        )

    def _validate_request(self, request: SubmitRequest) -> None:
        compiled = request.compiled
        contract = request.contract
        if self.inference is not None:
            self.inference.validate_workflow(compiled)
        if (
            hashlib.sha256(compiled.canonical_ir_bytes).hexdigest()
            != compiled.workflow_fingerprint
        ):
            raise ValueError("compiled workflow fingerprint does not match IR bytes")
        if contract.workflow_fingerprint != compiled.workflow_fingerprint:
            raise ValueError("submission workflow fingerprint mismatch")
        if contract.config_fingerprint != self.config_fingerprint:
            raise ValueError("submission config fingerprint mismatch")
        inputs = request.input_map()
        if len(inputs) != len(request.workflow_inputs):
            raise ValueError("workflow input names must be unique")
        if set(inputs) != set(compiled.workflow_inputs):
            raise ValueError("workflow input names do not match compiled workflow")
        package_by_definition = {
            package.definition_id: package for package in request.code_packages
        }
        if len(package_by_definition) != len(request.code_packages):
            raise ValueError("CodePackage definition IDs must be unique")
        if set(package_by_definition) != set(compiled.definitions):
            raise ValueError("CodePackage definitions do not match workflow")
        for definition_id, definition in compiled.definitions.items_tuple():
            package = package_by_definition[definition_id]
            if package.code_hash != definition.code_hash:
                raise ValueError("CodePackage code hash does not match definition")
        expected_identities = tuple(
            _run_input_identity(name, inputs[name])
            for name in sorted(inputs)
        )
        if expected_identities != contract.input_identities:
            raise ValueError("submission input identities do not match handles")
        for handle in inputs.values():
            if handle.owner_generation != self.data_owner_generation:
                raise ValueError("input handle belongs to another owner generation")
            if self.data_store.state_of(handle) != "staged":
                raise ValueError("new submission input handle must be staged")

    def _maybe_fail(self, point: str) -> None:
        if point in self._failure_points:
            self._failure_points.remove(point)
            raise RuntimeError(f"injected submission failure at {point}")

    def _restore_checkpoint(self, checkpoint: ControllerCheckpoint) -> None:
        if checkpoint.identity != self.recovery_identity:
            raise RuntimeError("Controller recovery identity changed")
        runs_by_submission = {
            item.submission_id: item for item in checkpoint.runs
        }
        for submission_item in checkpoint.submissions:
            state = submission_item.state
            run_id = submission_item.run_id
            error = submission_item.error
            recovered_run = runs_by_submission.get(submission_item.submission_id)
            if state is SubmissionState.PREPARING:
                if recovered_run is None:
                    state = SubmissionState.ABORTED
                    error = "ControllerRestart: submission interrupted before commit"
                    for _, handle in submission_item.workflow_inputs:
                        try:
                            if self.data_store.state_of(handle) == "staged":
                                self.data_store.release(handle)
                        except Exception:
                            pass
                else:
                    state = SubmissionState.COMMITTED
                    run_id = recovered_run.run_id
                    error = None
            self._submissions[submission_item.submission_id] = _SubmissionRecord(
                payload_hash=submission_item.payload_hash,
                state=state,
                request=SubmitRequest(
                    compiled=submission_item.compiled,
                    code_packages=submission_item.code_packages,
                    workflow_inputs=submission_item.workflow_inputs,
                    contract=submission_item.contract,
                ),
                run_id=run_id,
                error=error,
            )
        self.placement.restore_reconciled_leases(
            checkpoint.leases,
            now_ms=self.clock.monotonic_ms(),
        )
        for run_item in checkpoint.runs:
            submission = self._submissions[run_item.submission_id]
            if run_item.destroy_result is None:
                self.recorder.open_run(run_item.recording_context)
            destroy_result = (
                None
                if run_item.destroy_result is None
                else cast(DestroyResult, run_item.destroy_result)
            )
            self.core.restore_run(
                compiled=submission.request.compiled,
                code_handles=(),
                recovery=SchedulerRunRecovery(
                    run_id=run_item.run_id,
                    submission_id=run_item.submission_id,
                    snapshot=run_item.snapshot,
                    index=run_item.index,
                    recording_context=run_item.recording_context,
                    session_key_hash=run_item.session_key_hash,
                    destroy_result=destroy_result,
                    expected_producer_ids=run_item.expected_producer_ids,
                ),
            )
        if self.inference is not None:
            self.inference.instances.restore_recovery(
                tuple(
                    cast(ModelInstanceRecoveryRecord, item)
                    for item in checkpoint.model_instances
                )
            )

    async def _prepare_recovered_code(
        self,
        checkpoint: ControllerCheckpoint,
    ) -> None:
        for item in checkpoint.runs:
            if item.destroy_result is not None:
                continue
            submission = self._submissions[item.submission_id]
            handles = await self.runtime.prepare(submission.request.code_packages)
            self.core.bind_recovered_code(item.run_id, handles)

    async def _save_checkpoint(self) -> None:
        store = self.recovery_store
        claim = self._recovery_claim
        if store is None or claim is None:
            return
        async with self._checkpoint_lock:
            self._checkpoint_sequence += 1
            scheduler_runs = self.core.recovery_runs()
            submissions = tuple(
                SubmissionRecoveryRecord(
                    submission_id=submission_id,
                    payload_hash=record.payload_hash,
                    state=record.state,
                    run_id=record.run_id,
                    error=record.error,
                    compiled=record.request.compiled,
                    code_packages=record.request.code_packages,
                    workflow_inputs=record.request.workflow_inputs,
                    contract=record.request.contract,
                )
                for submission_id, record in sorted(self._submissions.items())
            )
            runs = tuple(
                RunRecoveryRecord(
                    run_id=item.run_id,
                    submission_id=item.submission_id,
                    snapshot=item.snapshot,
                    index=item.index,
                    recording_context=item.recording_context,
                    session_key_hash=item.session_key_hash,
                    destroy_result=item.destroy_result,
                    expected_producer_ids=item.expected_producer_ids,
                )
                for item in scheduler_runs
            )
            checkpoint = ControllerCheckpoint(
                schema_version=1,
                identity=self.recovery_identity,
                controller_generation=self.controller_generation,
                checkpoint_sequence=self._checkpoint_sequence,
                created_at_ms=self.clock.wall_ms(),
                data_owner_generation=self.data_owner_generation,
                data_store_descriptor=self.data_store_descriptor,
                submissions=submissions,
                runs=runs,
                leases=self.placement.lease_snapshots(),
                model_instances=(
                    ()
                    if self.inference is None
                    else self.inference.instances.recovery_records()
                ),
            )
            await asyncio.to_thread(
                store.save,
                checkpoint,
                controller_generation=self.controller_generation,
                epoch=claim.epoch,
            )

    async def _assert_current_generation(self) -> None:
        store = self.recovery_store
        claim = self._recovery_claim
        if store is None or claim is None:
            return
        await asyncio.to_thread(
            store.assert_current,
            controller_generation=self.controller_generation,
            epoch=claim.epoch,
        )

    def _schedule_checkpoint(self) -> None:
        if self.recovery_store is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._checkpoint_schedule_dirty = True
        if any(not task.done() for task in self._checkpoint_tasks):
            return
        task = loop.create_task(self._drain_scheduled_checkpoints())
        self._checkpoint_tasks.add(task)
        task.add_done_callback(self._checkpoint_tasks.discard)

    async def _drain_scheduled_checkpoints(self) -> None:
        while self._checkpoint_schedule_dirty:
            self._checkpoint_schedule_dirty = False
            await self._save_checkpoint()

    async def _wait_checkpoint_tasks(self) -> None:
        tasks = tuple(self._checkpoint_tasks)
        if tasks:
            await asyncio.gather(*tasks)


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1_000))
