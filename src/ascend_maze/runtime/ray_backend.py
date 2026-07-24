"""Ray RuntimeBackend with C6 hard placement and NodeAgent event delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
import inspect
from typing import Any, cast

import ray
from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

from ascend_maze.contracts.data import DataHandle, DataOwner
from ascend_maze.contracts.errors import ErrorInfo
from ascend_maze.contracts.recording import (
    ProducerFlushResult,
    RunRecordingContext,
)
from ascend_maze.contracts.resources import ExecutionTarget, PlacementLease
from ascend_maze.contracts.runtime import (
    CodeHandle,
    CodePackage,
    DeviceBinding,
    DispatchHandle,
    ExecutionRequest,
    RuntimeNodeBinding,
)
from ascend_maze.contracts.worker import WorkerLease
from ascend_maze.core.errors import ContractValidationError
from ascend_maze.core.canonical import FrozenMap
from ascend_maze.core.identifiers import stable_id
from ascend_maze.core.time import monotonic_time_ms
from ascend_maze.data.ray_store import RayDataStore
from ascend_maze.runtime.events import RuntimeEvent, RuntimeEventKind
from ascend_maze.runtime.code_loader import load_code_package
from ascend_maze.runtime.ray_node_registry import RayNodeRegistry
from ascend_maze.runtime.ray_cluster import validate_ray_version
from ascend_maze.runtime.ray_worker import (
    RAY_ONE_SHOT_WORKER,
    RayWorkerOutcome,
    ray_data_argument_keyword,
)
from ascend_maze.runtime.worker_broker import ColdWorkerBroker
from ascend_maze.runtime.worker_pool import StandbyWorkerBroker
from ascend_maze.inference.coordinator import InferenceCoordinator
from ascend_maze.inference.contracts import InferenceWorkerConfig

from ascend_maze.control.node_rpc import (
    NodeAgentIdentity,
    flush_node_recording,
    open_node_recording,
    report_worker_event,
)


@dataclass(slots=True)
class _CodeRecord:
    handle: CodeHandle
    package_handle: DataHandle
    reference_count: int


@dataclass(slots=True)
class _DispatchRecord:
    request: ExecutionRequest
    lease: PlacementLease
    binding: RuntimeNodeBinding
    worker_lease: WorkerLease
    handle: DispatchHandle
    dispatch_started_at_ms: int
    ray_submitted_at_ms: int | None
    object_ref: Any | None
    monitor: asyncio.Task[None] | None
    worker_started_event: RuntimeEvent | None = None
    worker_started_received_at_ms: int | None = None
    cancel_requested: bool = False
    invalidated: bool = False
    terminal: bool = False
    outcome: RayWorkerOutcome | None = None
    node_terminal_event: RuntimeEvent | None = None
    node_terminal_received: asyncio.Event = field(default_factory=asyncio.Event)
    inference_protocol_error: str | None = None


def _bind_transformers_local_worker(
    config: InferenceWorkerConfig,
    binding: RuntimeNodeBinding,
    *,
    physical_device_id: str,
) -> tuple[InferenceWorkerConfig, DeviceBinding]:
    mapping = binding.device_mapping(physical_device_id)
    device_binding = DeviceBinding(
        lease_id=config.instance_placement_lease_id,
        node_id=binding.node_id,
        boot_id=binding.boot_id,
        runtime_generation=binding.runtime_generation,
        physical_device_id=physical_device_id,
        runtime_visible_device_id=mapping.runtime_visible_device_id,
        visible_device_index=mapping.visible_device_index,
        environment_variables=FrozenMap(
            (
                (
                    "ASCEND_RT_VISIBLE_DEVICES",
                    mapping.runtime_visible_device_id,
                ),
            )
        ),
    )
    options = dict(config.adapter_options.items_tuple())
    options["device_id"] = mapping.runtime_visible_device_id
    return replace(config, adapter_options=FrozenMap(options.items())), device_binding


class RayRuntimeBackend:
    backend_name = "ray"

    def __init__(
        self,
        *,
        data_store: RayDataStore,
        node_registry: RayNodeRegistry,
        worker_broker: ColdWorkerBroker | StandbyWorkerBroker,
        cluster_id: str,
        owner_generation: str,
        controller_generation: str | None = None,
        environment_fingerprint: str,
        authorization_token: bytes = b"",
        event_timeout_seconds: float = 2.0,
        event_sink: Callable[[RuntimeEvent], None] | None = None,
        recording_error_sink: Callable[[str, str], None] | None = None,
        inference: InferenceCoordinator | None = None,
    ) -> None:
        if event_timeout_seconds <= 0:
            raise ValueError("event_timeout_seconds must be positive")
        self.data_store = data_store
        self.node_registry = node_registry
        self.worker_broker = worker_broker
        self.cluster_id = cluster_id
        self.owner_generation = owner_generation
        self.controller_generation = controller_generation or owner_generation
        self.environment_fingerprint = environment_fingerprint
        self.authorization_token = authorization_token
        self.event_timeout_seconds = event_timeout_seconds
        self._event_sink = event_sink
        self._recording_error_sink = recording_error_sink
        self.inference = inference
        self._code: dict[str, _CodeRecord] = {}
        self._dispatches: dict[str, _DispatchRecord] = {}
        self._attempt_dispatches: dict[tuple[str, str, int], str] = {}
        self._emitted_events: dict[str, str] = {}
        self._retired_runs: set[str] = set()
        self._run_recording_bindings: dict[str, dict[str, RuntimeNodeBinding]] = {}
        self._opened_run_recordings: set[tuple[str, str]] = set()
        self._started = False
        self._closed = False

    def set_event_sink(self, sink: Callable[[RuntimeEvent], None]) -> None:
        self._event_sink = sink

    def post_node_event(self, event: RuntimeEvent) -> None:
        record = self._dispatches.get(event.dispatch_id)
        if event.kind in {
            RuntimeEventKind.INFERENCE_REQUEST_STARTED,
            RuntimeEventKind.INFERENCE_REQUEST_FINISHED,
        }:
            if record is not None and not record.terminal:
                self._handle_inference_event(record, event)
            return
        if (
            event.kind is RuntimeEventKind.WORKER_STARTED
            and record is not None
            and not record.terminal
        ):
            if record.worker_started_event is None:
                record.worker_started_event = event
                record.worker_started_received_at_ms = monotonic_time_ms()
            self._emit(event)
            return
        if (
            event.kind is not RuntimeEventKind.WORKER_STARTED
            and record is not None
            and not record.terminal
        ):
            self._ingest_inference_summary(record, event)
            record.node_terminal_event = event
            record.node_terminal_received.set()
            return
        self._emit(event)

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("runtime backend is closed")
        if not ray.is_initialized():
            raise RuntimeError("Ray must be initialized before RayRuntimeBackend")
        validate_ray_version()
        self._started = True

    async def prepare(
        self, definitions: tuple[CodePackage, ...]
    ) -> tuple[CodeHandle, ...]:
        self._require_running()
        definition_ids = [package.definition_id for package in definitions]
        if len(definition_ids) != len(set(definition_ids)):
            raise ContractValidationError("CodePackage definitions must be unique")
        prepared: list[tuple[CodePackage, CodeHandle, DataHandle | None]] = []
        staged: list[DataHandle] = []
        try:
            for package in definitions:
                if package.environment_fingerprint != self.environment_fingerprint:
                    raise ContractValidationError("code package environment mismatch")
                existing = self._code.get(package.definition_id)
                if existing is not None:
                    if (
                        existing.handle.code_hash != package.code_hash
                        or existing.handle != self._code_handle_for_package(package)
                    ):
                        raise ContractValidationError("definition code hash conflict")
                    prepared.append((package, existing.handle, None))
                    continue
                await asyncio.to_thread(load_code_package, package)
                code_handle = self._code_handle_for_package(package)
                package_handle = await asyncio.to_thread(
                    self.data_store.put_staged_for_code_package,
                    package,
                    self.owner_generation,
                )
                staged.append(package_handle)
                prepared.append((package, code_handle, package_handle))
            if staged:
                await asyncio.to_thread(
                    self.data_store.adopt,
                    tuple(staged),
                    DataOwner(
                        owner_kind="code_registry",
                        owner_id=self.controller_generation,
                        owner_generation=self.owner_generation,
                    ),
                )
        except Exception:
            if staged:
                await asyncio.to_thread(self.data_store.release_many, tuple(staged))
            raise

        handles: list[CodeHandle] = []
        for package, code_handle, prepared_package_handle in prepared:
            existing = self._code.get(package.definition_id)
            if existing is None:
                assert prepared_package_handle is not None
                self._code[package.definition_id] = _CodeRecord(
                    code_handle, prepared_package_handle, 1
                )
            else:
                existing.reference_count += 1
            handles.append(code_handle)
        return tuple(handles)

    async def dispatch(
        self,
        request: ExecutionRequest,
        lease: PlacementLease,
    ) -> DispatchHandle:
        self._require_running()
        existing = self._dispatches.get(request.dispatch_id)
        if existing is not None:
            if existing.request != request or existing.lease != lease:
                raise ContractValidationError("dispatch_id payload conflict")
            return existing.handle
        attempt_key = (request.run_id, request.task_id, request.attempt)
        conflicting = self._attempt_dispatches.get(attempt_key)
        if conflicting is not None and conflicting != request.dispatch_id:
            raise ContractValidationError("attempt already has another dispatch_id")
        if (
            lease.run_id != request.run_id
            or lease.task_id != request.task_id
            or lease.attempt != request.attempt
        ):
            raise ContractValidationError("PlacementLease does not match request")
        if request.environment_fingerprint != self.environment_fingerprint:
            raise ContractValidationError("execution environment mismatch")
        dispatch_started_at_ms = monotonic_time_ms()
        inference_config = None
        if request.execution_target is ExecutionTarget.MODEL_SERVICE:
            if self.inference is None or request.model_route is None:
                raise ContractValidationError("Ray model service dispatch requires C11")
            if (
                self.inference.route_snapshot(request.model_route.route_lease_id).lease
                != request.model_route
            ):
                raise ContractValidationError(
                    "ModelRouteLease payload does not match C11 authority"
                )
            inference_config = self.inference.worker_config(request.model_route)
        code = self._code.get(request.code_handle.definition_id)
        if code is None or code.handle != request.code_handle:
            raise ContractValidationError("CodeHandle is not prepared")
        data_arguments = tuple(
            (index, argument)
            for index, argument in enumerate(request.arguments)
            if argument.kind == "data_handle"
        )
        resolved_refs = await asyncio.to_thread(
            self.data_store.resolve_refs,
            (
                code.package_handle,
                *(
                    argument.data_handle
                    for _, argument in data_arguments
                    if argument.data_handle is not None
                ),
            ),
        )
        if len(resolved_refs) != len(data_arguments) + 1:
            raise RuntimeError("Ray input resolution returned an invalid reference count")
        binding = self.node_registry.resolve_lease(lease)
        worker_lease = await self._maybe_await(
            self.worker_broker.acquire(
                placement_lease=lease,
                task_kind=request.task_kind,
                execution_target=request.execution_target,
                now_ms=monotonic_time_ms(),
            )
        )
        device_binding: DeviceBinding | None = None
        transformers_local_service = (
            request.execution_target is ExecutionTarget.MODEL_SERVICE
            and inference_config is not None
            and inference_config.adapter_name == "transformers_local"
        )
        if transformers_local_service:
            assert self.inference is not None
            assert inference_config is not None
            assert request.model_route is not None
            instance = self.inference.instances.snapshot(
                request.model_route.instance_id
            )
            if (
                instance.generation != request.model_route.instance_generation
                or instance.node_id != binding.node_id
                or instance.boot_id != binding.boot_id
                or instance.npu_device_id is None
                or instance.placement_lease_id
                != inference_config.instance_placement_lease_id
            ):
                await self._maybe_await(
                    self.worker_broker.release(
                        worker_lease.worker_lease_id, disposition="discard"
                    )
                )
                raise ContractValidationError(
                    "transformers_local model placement does not match Task node"
                )
            inference_config, device_binding = _bind_transformers_local_worker(
                inference_config,
                binding,
                physical_device_id=instance.npu_device_id,
            )
        elif (
            request.execution_target is ExecutionTarget.LOCAL_WORKER
            and request.task_kind == "npu"
        ):
            device_binding = DeviceBinding.from_lease(lease, binding)
            if worker_lease.bound_device_id != device_binding.physical_device_id:
                await self._maybe_await(
                    self.worker_broker.release(
                        worker_lease.worker_lease_id, disposition="discard"
                    )
                )
                raise ContractValidationError(
                    "WorkerLease device does not match PlacementLease"
                )
        elif lease.npu_device_id is not None or lease.resources.npu_slots != 0:
            await self._maybe_await(
                self.worker_broker.release(
                    worker_lease.worker_lease_id, disposition="discard"
                )
            )
            raise ContractValidationError(
                "CPU/I/O Worker cannot receive an NPU PlacementLease"
            )
        handle = DispatchHandle(
            dispatch_id=request.dispatch_id,
            backend_name=self.backend_name,
            run_id=request.run_id,
            task_id=request.task_id,
            attempt=request.attempt,
            lease_id=lease.lease_id,
            route_lease_id=(
                None
                if request.model_route is None
                else request.model_route.route_lease_id
            ),
            worker_endpoint_id=worker_lease.worker_endpoint_id,
        )
        identity = NodeAgentIdentity(
            cluster_id=self.cluster_id,
            node_id=binding.node_id,
            boot_id=binding.boot_id,
            ray_node_id=binding.ray_node_id,
            agent_generation=binding.agent_generation,
            environment_fingerprint=self.environment_fingerprint,
            producer_id=binding.producer_id,
        )
        record = _DispatchRecord(
            request=request,
            lease=lease,
            binding=binding,
            worker_lease=worker_lease,
            handle=handle,
            dispatch_started_at_ms=dispatch_started_at_ms,
            ray_submitted_at_ms=None,
            object_ref=None,
            monitor=None,
        )
        self._dispatches[request.dispatch_id] = record
        self._attempt_dispatches[attempt_key] = request.dispatch_id
        try:
            worker_kwargs: dict[str, object] = dict(
                request=request,
                placement_lease=lease,
                worker_lease=worker_lease,
                binding=binding,
                agent_identity=identity,
                controller_generation=self.controller_generation,
                data_store_descriptor=self.data_store.descriptor,
                code_package=resolved_refs[0],
                event_timeout_seconds=self.event_timeout_seconds,
                device_binding=device_binding,
                inference_config=inference_config,
            )
            for resolved_index, (argument_index, _) in enumerate(
                data_arguments, start=1
            ):
                worker_kwargs[ray_data_argument_keyword(argument_index)] = (
                    resolved_refs[resolved_index]
                )
            record.ray_submitted_at_ms = monotonic_time_ms()
            if isinstance(self.worker_broker, StandbyWorkerBroker):
                record.object_ref = self.worker_broker.submit(
                    worker_lease.worker_lease_id,
                    worker_kwargs,
                )
            else:
                record.object_ref = RAY_ONE_SHOT_WORKER.options(
                    scheduling_strategy=NodeAffinitySchedulingStrategy(
                        binding.ray_node_id, soft=False
                    ),
                    name=f"maze:{request.dispatch_id}",
                ).remote(**worker_kwargs)
            if request.model_route is not None:
                assert self.inference is not None
                if not self.inference.activate_route(
                    request.model_route.route_lease_id
                ):
                    raise ContractValidationError(
                        "ModelRouteLease could not become active"
                    )
        except Exception:
            if record.object_ref is not None:
                if isinstance(self.worker_broker, StandbyWorkerBroker):
                    await self.worker_broker.cancel(worker_lease.worker_lease_id)
                else:
                    ray.cancel(record.object_ref, force=True, recursive=True)
            if request.model_route is not None and self.inference is not None:
                self.inference.abort_worker_attempt(
                    request.model_route,
                    error_code="model_dispatch_failed",
                )
            self._drop_dispatch(request.dispatch_id)
            await self._maybe_await(
                self.worker_broker.release(
                    worker_lease.worker_lease_id, disposition="discard"
                )
            )
            raise
        record.monitor = asyncio.create_task(self._monitor(record))
        return handle

    async def cancel(self, handle: DispatchHandle, reason: str) -> None:
        del reason
        record = self._dispatches.get(handle.dispatch_id)
        if record is None:
            return
        if record.handle != handle:
            raise ContractValidationError("DispatchHandle payload conflict")
        if record.cancel_requested:
            return
        record.cancel_requested = True
        if not record.terminal:
            if isinstance(self.worker_broker, StandbyWorkerBroker):
                await self.worker_broker.cancel(record.worker_lease.worker_lease_id)
            elif record.object_ref is not None:
                ray.cancel(record.object_ref, force=True, recursive=True)
            if record.monitor is not None:
                await asyncio.gather(record.monitor, return_exceptions=True)
        elif record.outcome is not None:
            await self._release_staged_outputs(record.outcome.terminal_event)

    async def release_code(self, handles: tuple[CodeHandle, ...]) -> None:
        for handle in handles:
            record = self._code.get(handle.definition_id)
            if record is None:
                continue
            if record.handle != handle:
                raise ContractValidationError("CodeHandle payload conflict")
            if record.reference_count > 0:
                record.reference_count -= 1
            if record.reference_count == 0:
                await asyncio.to_thread(self.data_store.release, record.package_handle)
                del self._code[handle.definition_id]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for record in self._dispatches.values():
            if not record.terminal:
                record.cancel_requested = True
                if isinstance(self.worker_broker, StandbyWorkerBroker):
                    await self.worker_broker.cancel(record.worker_lease.worker_lease_id)
                elif record.object_ref is not None:
                    ray.cancel(record.object_ref, force=True, recursive=True)
        monitors = [
            record.monitor
            for record in self._dispatches.values()
            if record.monitor is not None and not record.monitor.done()
        ]
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)
        for code_record in tuple(self._code.values()):
            await asyncio.to_thread(self.data_store.release, code_record.package_handle)
        self._code.clear()
        self._started = False

    async def wait_idle(self) -> None:
        monitors = [
            record.monitor
            for record in self._dispatches.values()
            if record.monitor is not None and not record.monitor.done()
        ]
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)

    def producer_for_lease(self, lease: PlacementLease) -> str | None:
        return self.node_registry.producer_for_lease(lease)

    def producer_is_persistent(self, lease: PlacementLease) -> bool:
        return self.node_registry.resolve_lease(lease).records_locally

    async def prepare_run_recording(
        self,
        context: RunRecordingContext,
        lease: PlacementLease,
    ) -> None:
        binding = self.node_registry.resolve_lease(lease)
        if not binding.records_locally:
            return
        run_bindings = self._run_recording_bindings.setdefault(context.run_id, {})
        existing = run_bindings.get(binding.producer_id)
        if existing is not None and existing != binding:
            raise ContractValidationError("recording producer binding conflict")
        run_bindings[binding.producer_id] = binding
        key = (context.run_id, binding.producer_id)
        if key in self._opened_run_recordings:
            return
        if not self.authorization_token:
            raise RuntimeError("NodeAgent recording authorization is not configured")
        await asyncio.to_thread(
            open_node_recording,
            binding=binding,
            cluster_id=self.cluster_id,
            controller_generation=self.controller_generation,
            authorization_token=self.authorization_token,
            context=context,
            timeout_seconds=self.event_timeout_seconds,
        )
        self._opened_run_recordings.add(key)

    async def flush_run_recorders(
        self,
        run_id: str,
        timeout_ms: int,
    ) -> tuple[ProducerFlushResult, ...]:
        bindings = self._run_recording_bindings.get(run_id, {})

        async def flush_one(
            producer_id: str, binding: RuntimeNodeBinding
        ) -> ProducerFlushResult:
            try:
                result = await asyncio.to_thread(
                    flush_node_recording,
                    binding=binding,
                    cluster_id=self.cluster_id,
                    controller_generation=self.controller_generation,
                    authorization_token=self.authorization_token,
                    run_id=run_id,
                    timeout_ms=timeout_ms,
                )
            except Exception as exc:
                return ProducerFlushResult(
                    producer_id=producer_id,
                    result=None,
                    error=f"{type(exc).__name__}: {exc}",
                )
            return ProducerFlushResult(producer_id=producer_id, result=result)

        return tuple(
            await asyncio.gather(
                *(
                    flush_one(producer_id, bindings[producer_id])
                    for producer_id in sorted(bindings)
                )
            )
        )

    def dispatch_invalidated(self, dispatch_id: str) -> bool:
        record = self._dispatches.get(dispatch_id)
        return (
            record is None
            or record.cancel_requested
            or record.invalidated
            or record.terminal
        )

    def worker_released(self, dispatch_id: str) -> bool:
        record = self._dispatches.get(dispatch_id)
        return record is None or (
            record.terminal
            and self.worker_broker.is_released(record.worker_lease.worker_lease_id)
        )

    async def release_run(self, run_id: str) -> int:
        self._retired_runs.add(run_id)
        bindings = self._run_recording_bindings.pop(run_id, {})
        for producer_id in bindings:
            self._opened_run_recordings.discard((run_id, producer_id))
        dispatch_ids = [
            dispatch_id
            for dispatch_id, record in self._dispatches.items()
            if record.request.run_id == run_id and record.terminal
        ]
        for dispatch_id in dispatch_ids:
            record = self._dispatches[dispatch_id]
            if record.outcome is not None:
                await self._release_staged_outputs(record.outcome.terminal_event)
            self._drop_dispatch(dispatch_id)
        if not any(
            record.request.run_id == run_id for record in self._dispatches.values()
        ):
            self._retired_runs.discard(run_id)
        for event_id in [
            event_id
            for event_id, event_run_id in self._emitted_events.items()
            if event_run_id == run_id
        ]:
            del self._emitted_events[event_id]
        return len(dispatch_ids)

    def invalidate_binding(self, binding: RuntimeNodeBinding) -> None:
        self.data_store.release_staged_for_runtime_node(
            node_id=binding.node_id,
            boot_id=binding.boot_id,
            runtime_generation=binding.runtime_generation,
        )
        self.worker_broker.invalidate_node(binding.node_id, binding.boot_id)
        for record in self._dispatches.values():
            if (
                not record.terminal
                and record.binding.node_id == binding.node_id
                and record.binding.boot_id == binding.boot_id
                and record.binding.runtime_generation == binding.runtime_generation
            ):
                record.invalidated = True
                self._record_delivery_error(
                    record.request.run_id,
                    "NodeAgent binding disconnected during an active Attempt",
                )
                if (
                    not isinstance(self.worker_broker, StandbyWorkerBroker)
                    and record.object_ref is not None
                ):
                    ray.cancel(record.object_ref, force=True, recursive=True)

    def active_dispatch_count(self, run_id: str | None = None) -> int:
        return sum(
            not record.terminal and (run_id is None or record.request.run_id == run_id)
            for record in self._dispatches.values()
        )

    def active_dispatch_ids_for_node(
        self, node_id: str, boot_id: str
    ) -> tuple[str, ...]:
        return tuple(
            sorted(
                record.request.dispatch_id
                for record in self._dispatches.values()
                if not record.terminal
                and record.binding.node_id == node_id
                and record.binding.boot_id == boot_id
            )
        )

    def code_reference_count(self) -> int:
        return sum(record.reference_count for record in self._code.values())

    def worker_outcome(self, dispatch_id: str) -> RayWorkerOutcome | None:
        record = self._dispatches.get(dispatch_id)
        return None if record is None else record.outcome

    def worker_started_event(self, dispatch_id: str) -> RuntimeEvent | None:
        record = self._dispatches.get(dispatch_id)
        return None if record is None else record.worker_started_event

    def task_timing_records(
        self,
        run_id: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        records: list[dict[str, object]] = []
        for dispatch in self._dispatches.values():
            if run_id is not None and dispatch.request.run_id != run_id:
                continue
            outcome = dispatch.outcome
            if outcome is None or outcome.task_timing is None:
                continue
            item = outcome.task_timing.as_dict()
            submitted_at_ms = dispatch.ray_submitted_at_ms
            started_received_at_ms = dispatch.worker_started_received_at_ms
            item["dispatch_prepare_ms"] = (
                0
                if submitted_at_ms is None
                else max(0, submitted_at_ms - dispatch.dispatch_started_at_ms)
            )
            item["worker_startup_ms"] = (
                0
                if submitted_at_ms is None or started_received_at_ms is None
                else max(0, started_received_at_ms - submitted_at_ms)
            )
            item["worker_startup_scope"] = (
                "ray_schedule_input_materialization_and_worker_started_delivery"
            )
            item["dispatch_wait_ms"] = (
                0
                if started_received_at_ms is None
                else max(
                    0,
                    started_received_at_ms - dispatch.dispatch_started_at_ms,
                )
            )
            records.append(item)
        return tuple(
            sorted(
                records,
                key=lambda item: (
                    cast(int, item["started_at_ms"]),
                    str(item["task_id"]),
                    cast(int, item["attempt"]),
                ),
            )
        )

    async def _monitor(self, record: _DispatchRecord) -> None:
        terminal_to_publish: RuntimeEvent | None = None
        try:
            if record.object_ref is None:
                raise RuntimeError("Ray dispatch has no ObjectRef")
            outcome = await asyncio.to_thread(ray.get, record.object_ref)
            if not isinstance(outcome, RayWorkerOutcome):
                raise TypeError("Ray Worker returned an invalid control outcome")
            if (
                outcome.dispatch_id != record.request.dispatch_id
                or outcome.ray_node_id != record.binding.ray_node_id
            ):
                raise RuntimeError("Ray Worker outcome identity mismatch")
            record.outcome = outcome
            if record.cancel_requested or record.invalidated:
                await self._release_staged_outputs(outcome.terminal_event)
                terminal_to_publish = self._cancelled_or_lost_event(
                    record, lost=record.invalidated
                )
                await self._deliver_synthesized_event(
                    record,
                    terminal_to_publish,
                )
            else:
                terminal_to_publish = outcome.terminal_event
                if not outcome.terminal_event_delivered:
                    self._record_delivery_error(
                        record.request.run_id,
                        "NodeAgent did not accept the terminal Worker event",
                    )
                    await self._deliver_synthesized_event(
                        record, outcome.terminal_event
                    )
        except Exception as exc:
            terminal_to_publish = self._cancelled_or_lost_event(
                record,
                lost=not record.cancel_requested,
                message=f"{type(exc).__name__}: {exc}",
            )
            await self._deliver_synthesized_event(
                record,
                terminal_to_publish,
            )
        finally:
            if not record.invalidated and not record.node_terminal_received.is_set():
                try:
                    await asyncio.wait_for(
                        record.node_terminal_received.wait(),
                        timeout=self.event_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    self._record_delivery_error(
                        record.request.run_id,
                        "Controller did not receive the NodeAgent terminal event",
                    )
            disposition = (
                "reuse"
                if record.worker_lease.profile.value in {"cpu", "io"}
                and record.outcome is not None
                and record.outcome.reuse_safe
                and not record.cancel_requested
                and not record.invalidated
                else "discard"
            )
            if (
                isinstance(self.worker_broker, StandbyWorkerBroker)
                and record.outcome is not None
                and record.outcome.cleanup_reason is not None
            ):
                self.worker_broker.record_cleanup_failure(
                    record.worker_lease.worker_lease_id,
                    record.outcome.cleanup_reason,
                )
            try:
                await self._maybe_await(
                    self.worker_broker.release(
                        record.worker_lease.worker_lease_id,
                        disposition=disposition,
                    )
                )
            except Exception as exc:
                if record.outcome is not None:
                    await self._release_staged_outputs(record.outcome.terminal_event)
                terminal_to_publish = self._worker_cleanup_failed_event(
                    record,
                    message=f"{type(exc).__name__}: {exc}",
                )
                self._record_delivery_error(
                    record.request.run_id,
                    "Worker cleanup did not reach its process-exit barrier",
                )
                await self._deliver_synthesized_event(record, terminal_to_publish)
            record.terminal = True
            publish = record.node_terminal_event or terminal_to_publish
            if record.request.model_route is not None and self.inference is not None:
                route = record.request.model_route
                if self.inference.attempt_summary(route.route_lease_id) is None:
                    self.inference.abort_worker_attempt(
                        route,
                        error_code=(
                            "model_protocol_failed"
                            if record.inference_protocol_error is not None
                            else "worker_lost"
                        ),
                    )
                if record.inference_protocol_error is not None:
                    if publish is not None:
                        await self._release_staged_outputs(publish)
                    publish = self._inference_protocol_failed_event(
                        record,
                        message=record.inference_protocol_error,
                    )
            if publish is not None:
                self._emit(publish)
            if record.request.run_id in self._retired_runs:
                self._drop_dispatch(record.request.dispatch_id)

    def _cancelled_or_lost_event(
        self,
        record: _DispatchRecord,
        *,
        lost: bool,
        message: str | None = None,
    ) -> RuntimeEvent:
        kind = RuntimeEventKind.TASK_FAILED if lost else RuntimeEventKind.TASK_CANCELLED
        error = None
        if lost:
            error = ErrorInfo(
                schema_version=1,
                error_code="worker_lost",
                category="worker",
                origin="runtime",
                message=message or "Ray Worker or NodeAgent generation was lost",
                retryable_hint=True,
                classification_confidence="exact",
                execution_phase="running",
                run_id=record.request.run_id,
                task_id=record.request.task_id,
                attempt=record.request.attempt,
                dispatch_id=record.request.dispatch_id,
                lease_id=record.lease.lease_id,
                route_lease_id=(
                    None
                    if record.request.model_route is None
                    else record.request.model_route.route_lease_id
                ),
                node_id=record.binding.node_id,
                boot_id=record.binding.boot_id,
                worker_id=record.worker_lease.worker_id,
                model_instance_id=(
                    None
                    if record.request.model_route is None
                    else record.request.model_route.instance_id
                ),
                occurred_at_ms=monotonic_time_ms(),
            )
        return RuntimeEvent.create(
            kind=kind,
            dispatch_id=record.request.dispatch_id,
            run_id=record.request.run_id,
            task_id=record.request.task_id,
            attempt=record.request.attempt,
            lease_id=record.lease.lease_id,
            route_lease_id=(
                None
                if record.request.model_route is None
                else record.request.model_route.route_lease_id
            ),
            occurred_at_ms=monotonic_time_ms(),
            error=error,
        )

    def _worker_cleanup_failed_event(
        self,
        record: _DispatchRecord,
        *,
        message: str,
    ) -> RuntimeEvent:
        error = ErrorInfo(
            schema_version=1,
            error_code="worker_cleanup_failed",
            category="worker",
            origin="runtime",
            message=message,
            retryable_hint=False,
            classification_confidence="exact",
            execution_phase="cleanup",
            run_id=record.request.run_id,
            task_id=record.request.task_id,
            attempt=record.request.attempt,
            dispatch_id=record.request.dispatch_id,
            lease_id=record.lease.lease_id,
            route_lease_id=(
                None
                if record.request.model_route is None
                else record.request.model_route.route_lease_id
            ),
            node_id=record.binding.node_id,
            boot_id=record.binding.boot_id,
            worker_id=record.worker_lease.worker_id,
            model_instance_id=(
                None
                if record.request.model_route is None
                else record.request.model_route.instance_id
            ),
            device_id=record.worker_lease.bound_device_id,
            occurred_at_ms=monotonic_time_ms(),
        )
        return RuntimeEvent.create(
            kind=RuntimeEventKind.TASK_FAILED,
            dispatch_id=record.request.dispatch_id,
            run_id=record.request.run_id,
            task_id=record.request.task_id,
            attempt=record.request.attempt,
            lease_id=record.lease.lease_id,
            route_lease_id=(
                None
                if record.request.model_route is None
                else record.request.model_route.route_lease_id
            ),
            occurred_at_ms=error.occurred_at_ms,
            error=error,
        )

    def _handle_inference_event(
        self,
        record: _DispatchRecord,
        event: RuntimeEvent,
    ) -> None:
        route = record.request.model_route
        if route is None or self.inference is None:
            record.inference_protocol_error = (
                "Worker emitted inference lifecycle for a local Attempt"
            )
            return
        if (
            event.run_id != route.run_id
            or event.task_id != route.task_id
            or event.attempt != route.attempt
            or event.route_lease_id != route.route_lease_id
            or event.lease_id != record.lease.lease_id
        ):
            record.inference_protocol_error = (
                "Worker inference lifecycle identity is stale"
            )
            return
        try:
            if event.kind is RuntimeEventKind.INFERENCE_REQUEST_STARTED:
                if event.inference_call_index is None:
                    raise RuntimeError("inference request start omitted call_index")
                self.inference.worker_request_started(
                    route,
                    call_index=event.inference_call_index,
                    started_at_ms=event.occurred_at_ms,
                )
            else:
                if event.inference_request is None:
                    raise RuntimeError("inference request finish omitted its record")
                self.inference.worker_request_finished(
                    route,
                    event.inference_request,
                )
        except Exception as exc:
            record.inference_protocol_error = f"{type(exc).__name__}: {exc}"
            self.inference.abort_worker_attempt(
                route,
                error_code="model_protocol_failed",
            )
            self._record_delivery_error(
                record.request.run_id,
                "Worker inference lifecycle was rejected: "
                + record.inference_protocol_error,
            )

    def _ingest_inference_summary(
        self,
        record: _DispatchRecord,
        event: RuntimeEvent,
    ) -> None:
        route = record.request.model_route
        if route is None or self.inference is None:
            return
        if (
            event.kind is RuntimeEventKind.DISPATCH_FAILED
            and event.inference_summary is None
        ):
            return
        try:
            if event.inference_summary is None:
                raise RuntimeError(
                    "service Worker terminal event omitted inference summary"
                )
            self.inference.worker_attempt_finished(route, event.inference_summary)
        except Exception as exc:
            record.inference_protocol_error = f"{type(exc).__name__}: {exc}"
            self.inference.abort_worker_attempt(
                route,
                error_code="model_protocol_failed",
            )

    def _inference_protocol_failed_event(
        self,
        record: _DispatchRecord,
        *,
        message: str,
    ) -> RuntimeEvent:
        route = record.request.model_route
        assert route is not None
        error = ErrorInfo(
            schema_version=1,
            error_code="model_protocol_failed",
            category="model",
            origin="runtime",
            message=message,
            retryable_hint=False,
            classification_confidence="exact",
            execution_phase="cleanup",
            run_id=record.request.run_id,
            task_id=record.request.task_id,
            attempt=record.request.attempt,
            dispatch_id=record.request.dispatch_id,
            lease_id=record.lease.lease_id,
            route_lease_id=route.route_lease_id,
            node_id=record.binding.node_id,
            boot_id=record.binding.boot_id,
            worker_id=record.worker_lease.worker_id,
            model_instance_id=route.instance_id,
            occurred_at_ms=monotonic_time_ms(),
        )
        return RuntimeEvent.create(
            kind=RuntimeEventKind.TASK_FAILED,
            dispatch_id=record.request.dispatch_id,
            run_id=record.request.run_id,
            task_id=record.request.task_id,
            attempt=record.request.attempt,
            lease_id=record.lease.lease_id,
            route_lease_id=route.route_lease_id,
            occurred_at_ms=error.occurred_at_ms,
            error=error,
        )

    async def _release_staged_outputs(self, event: RuntimeEvent) -> None:
        def release() -> None:
            for _, handle in event.output_handles:
                try:
                    if self.data_store.state_of(handle) == "staged":
                        self.data_store.release(handle)
                except Exception:
                    pass

        await asyncio.to_thread(release)

    async def _deliver_synthesized_event(
        self,
        record: _DispatchRecord,
        event: RuntimeEvent,
    ) -> None:
        if record.invalidated:
            return
        try:
            await asyncio.to_thread(
                report_worker_event,
                endpoint=record.binding.agent_endpoint,
                identity=self._agent_identity(record.binding),
                controller_generation=self.controller_generation,
                runtime_generation=record.binding.runtime_generation,
                event=event,
                timeout_seconds=self.event_timeout_seconds,
            )
        except Exception as exc:
            self._record_delivery_error(
                record.request.run_id,
                f"NodeAgent rejected synthesized RuntimeEvent: {type(exc).__name__}: {exc}",
            )

    def _emit(self, event: RuntimeEvent) -> None:
        if self._event_sink is None:
            raise RuntimeError("runtime event sink is not configured")
        if event.event_id in self._emitted_events:
            return
        self._emitted_events[event.event_id] = event.run_id
        self._event_sink(event)

    def _record_delivery_error(self, run_id: str, message: str) -> None:
        if self._recording_error_sink is not None:
            self._recording_error_sink(run_id, message)

    def _drop_dispatch(self, dispatch_id: str) -> None:
        record = self._dispatches.pop(dispatch_id, None)
        if record is None:
            return
        self._attempt_dispatches.pop(
            (record.request.run_id, record.request.task_id, record.request.attempt),
            None,
        )
        run_id = record.request.run_id
        if not any(item.request.run_id == run_id for item in self._dispatches.values()):
            self._retired_runs.discard(run_id)

    def _require_running(self) -> None:
        if not self._started or self._closed:
            raise RuntimeError("runtime backend is not running")

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    def _agent_identity(self, binding: RuntimeNodeBinding) -> NodeAgentIdentity:
        return NodeAgentIdentity(
            cluster_id=self.cluster_id,
            node_id=binding.node_id,
            boot_id=binding.boot_id,
            ray_node_id=binding.ray_node_id,
            agent_generation=binding.agent_generation,
            environment_fingerprint=self.environment_fingerprint,
            producer_id=binding.producer_id,
        )

    @staticmethod
    def _code_handle_for_package(package: CodePackage) -> CodeHandle:
        return CodeHandle(
            code_handle_id=stable_id(
                "code",
                package.definition_id,
                package.code_hash,
                package.environment_fingerprint,
            ),
            definition_id=package.definition_id,
            code_hash=package.code_hash,
        )
