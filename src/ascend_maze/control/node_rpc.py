"""Long-lived Controller/NodeAgent stream and node-local Worker event RPC."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
import hmac
from pathlib import Path
from typing import Any

import grpc

from ascend_maze.contracts.recording import (
    ExecutionEvent,
    ExecutionRecorder,
    FlushResult,
    RunRecordingContext,
)
from ascend_maze.contracts.runtime import RuntimeDeviceMapping, RuntimeNodeBinding
from ascend_maze.core.canonical import (
    FrozenMap,
    canonical_bytes,
    decode_canonical_bytes,
    freeze_canonical,
)
from ascend_maze.core.clock import Clock, SystemClock
from ascend_maze.core.errors import ContractValidationError
from ascend_maze.core.identifiers import new_id
from ascend_maze.inference.contracts import ServiceProcessExit
from ascend_maze.placement import (
    NodeCapacity,
    NodeObservation,
    NpuCapacity,
    NpuObservation,
)
from ascend_maze.runtime.events import (
    TERMINAL_RUNTIME_EVENT_KINDS,
    RuntimeEvent,
    RuntimeEventKind,
)
from ascend_maze.runtime.ray_node_registry import (
    RayNodeRegistry,
    RuntimeNodeStatus,
)

from ascend_maze.control.proto import control_pb2 as _control_pb2
from ascend_maze.control.proto import control_pb2_grpc
from ascend_maze.control.contracts import NodeRuntimePolicy
from ascend_maze.control.proto_codec import decode_runtime_event, encode_runtime_event
from ascend_maze.control.service_process import (
    NodeServiceProcessManager,
    decode_model_placement,
    decode_port_lease,
    decode_service_handle,
    decode_service_launch,
    encode_port_lease,
    encode_service_handle,
)

control_pb2: Any = _control_pb2


def _encode_node_capacity(capacity: NodeCapacity) -> Any:
    message = control_pb2.NodeCapacityMessage(
        node_id=capacity.node_id,
        boot_id=capacity.boot_id,
        node_ip=capacity.node_ip,
        cpu_total=capacity.cpu_total,
        mem_total_mb=capacity.mem_total_mb,
        cpu_system_reserved=capacity.cpu_system_reserved,
        mem_system_reserved_mb=capacity.mem_system_reserved_mb,
        io_slots_total=capacity.io_slots_total,
        observed_free_mem_mb=capacity.observed_free_mem_mb or 0,
        has_observed_free_mem_mb=capacity.observed_free_mem_mb is not None,
        canonical_capabilities=canonical_bytes(capacity.capabilities),
    )
    for npu in capacity.npus:
        message.npus.add(
            device_id=npu.device_id,
            chip_type=npu.chip_type,
            total_hbm_mb=npu.total_hbm_mb,
            system_reserved_hbm_mb=npu.system_reserved_hbm_mb,
            task_slots_total=npu.task_slots_total,
            observed_free_hbm_mb=npu.observed_free_hbm_mb or 0,
            has_observed_free_hbm_mb=npu.observed_free_hbm_mb is not None,
            healthy=npu.healthy,
        )
    return message


def _decode_node_capacity(message: Any) -> NodeCapacity:
    capabilities = decode_canonical_bytes(bytes(message.canonical_capabilities))
    if not isinstance(capabilities, FrozenMap):
        raise ContractValidationError("NodeCapacity capabilities must be a mapping")
    return NodeCapacity(
        node_id=str(message.node_id),
        boot_id=str(message.boot_id),
        node_ip=str(message.node_ip),
        cpu_total=int(message.cpu_total),
        mem_total_mb=int(message.mem_total_mb),
        cpu_system_reserved=int(message.cpu_system_reserved),
        mem_system_reserved_mb=int(message.mem_system_reserved_mb),
        io_slots_total=int(message.io_slots_total),
        npus=tuple(
            NpuCapacity(
                device_id=str(item.device_id),
                chip_type=str(item.chip_type),
                total_hbm_mb=int(item.total_hbm_mb),
                system_reserved_hbm_mb=int(item.system_reserved_hbm_mb),
                task_slots_total=int(item.task_slots_total),
                observed_free_hbm_mb=(
                    int(item.observed_free_hbm_mb)
                    if item.has_observed_free_hbm_mb
                    else None
                ),
                healthy=bool(item.healthy),
            )
            for item in message.npus
        ),
        observed_free_mem_mb=(
            int(message.observed_free_mem_mb)
            if message.has_observed_free_mem_mb
            else None
        ),
        capabilities=capabilities,
    )


def _execution_event_from_runtime(
    *,
    producer_id: str,
    producer_sequence: int,
    producer_monotonic_time_ms: int,
    node_id: str,
    event: RuntimeEvent,
    wall_time_ms: int,
) -> ExecutionEvent:
    payload_items: list[tuple[str, object]] = [
        ("dispatch_id", event.dispatch_id),
        ("source_occurred_at_ms", event.occurred_at_ms),
    ]
    if event.worker_pid is not None:
        payload_items.append(("worker_pid", event.worker_pid))
    if event.device_id is not None:
        payload_items.extend(
            (
                ("physical_device_id", event.device_id),
                ("binding_verified", event.binding_verified),
            )
        )
    observation = event.resource_observation
    if observation is not None:
        payload_items.extend(
            (
                ("peak_host_rss_mb", observation.peak_host_rss_mb),
                ("peak_npu_allocated_mb", observation.peak_npu_allocated_mb),
                ("peak_npu_reserved_mb", observation.peak_npu_reserved_mb),
                (
                    "peak_npu_process_hbm_mb",
                    observation.peak_npu_process_hbm_mb,
                ),
                ("npu_metric_source", observation.npu_metric_source),
                ("npu_metric_quality", observation.npu_metric_quality),
            )
        )
    request = event.inference_request
    if event.inference_call_index is not None:
        payload_items.append(("call_index", event.inference_call_index))
    if request is not None:
        payload_items.extend(
            (
                ("call_index", request.call_index),
                ("model_id", request.model_id),
                ("instance_generation", request.instance_generation),
                (
                    "instance_placement_lease_id",
                    request.instance_placement_lease_id,
                ),
                ("started_at_ms", request.started_at_ms),
                ("duration_ms", request.duration_ms),
                ("status", request.status),
                ("input_tokens", request.input_tokens),
                ("output_tokens", request.output_tokens),
                ("engine_queue_depth", request.engine_queue_depth),
                ("prefix_cache_hit", request.prefix_cache_hit),
                ("ttft_ms", request.ttft_ms),
                ("error_code", request.error_code),
            )
        )
    summary = event.inference_summary
    if summary is not None:
        payload_items.extend(
            (
                ("request_count", summary.request_count),
                ("request_inflight", summary.request_inflight),
                ("context_cleared", summary.context_cleared),
            )
        )
    payload = freeze_canonical(dict(payload_items))
    if not isinstance(payload, FrozenMap):
        raise AssertionError("node event payload must be a mapping")
    return ExecutionEvent(
        schema_version=1,
        event_id=f"node_record:{event.event_id}",
        experiment_id=event.run_id,
        run_id=event.run_id,
        task_id=event.task_id,
        attempt=event.attempt,
        lease_id=event.lease_id,
        route_lease_id=event.route_lease_id,
        model_instance_id=None if request is None else request.instance_id,
        event_type=event.kind.value,
        producer_id=producer_id,
        producer_sequence=producer_sequence,
        node_id=node_id,
        device_id=event.device_id,
        monotonic_time_ms=producer_monotonic_time_ms,
        wall_time_ms=wall_time_ms,
        duration_ms=None if request is None else request.duration_ms,
        payload=payload,
    )


@dataclass(frozen=True, slots=True)
class NodeAgentIdentity:
    cluster_id: str
    node_id: str
    boot_id: str
    ray_node_id: str
    agent_generation: str
    environment_fingerprint: str
    producer_id: str
    device_mappings: tuple[RuntimeDeviceMapping, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "cluster_id",
            "node_id",
            "boot_id",
            "ray_node_id",
            "agent_generation",
            "environment_fingerprint",
            "producer_id",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractValidationError(f"{name} is required")
        if not isinstance(self.device_mappings, tuple) or any(
            not isinstance(item, RuntimeDeviceMapping)
            for item in self.device_mappings
        ):
            raise ContractValidationError(
                "device_mappings must contain RuntimeDeviceMapping values"
            )
        ordered = tuple(sorted(self.device_mappings))
        if len({item.physical_device_id for item in ordered}) != len(ordered):
            raise ContractValidationError(
                "NodeAgent physical device mappings must be unique"
            )
        object.__setattr__(self, "device_mappings", ordered)


@dataclass(frozen=True, slots=True)
class NodeRecoveryInventory:
    active_lease_ids: tuple[str, ...]
    service_handle_ids: tuple[str, ...]
    reported_controller_generation: str | None = None

    def __post_init__(self) -> None:
        for name in ("active_lease_ids", "service_handle_ids"):
            values = getattr(self, name)
            if len(values) != len(set(values)) or any(not item for item in values):
                raise ContractValidationError(f"invalid node recovery {name}")


@dataclass(frozen=True, slots=True)
class _ActiveWorkerLease:
    device_id: str | None
    process_id: int | None
    process_start_time: str | None
    controller_generation: str
    runtime_generation: int


class _NodeControlServicer:
    def __init__(self, owner: "NodeControlServer") -> None:
        self.owner = owner

    async def GetBootstrap(self, request: Any, context: Any) -> Any:
        del context
        if (
            int(request.schema_version) != 1
            or str(request.cluster_id) != self.owner.cluster_id
            or not hmac.compare_digest(
                bytes(request.authorization_token), self.owner.authorization_token
            )
        ):
            return control_pb2.NodeBootstrapResponse(
                schema_version=1,
                request_id=str(request.request_id),
                status_code="error",
                error_code="authorization_failed",
                message="node bootstrap authorization failed",
            )
        return control_pb2.NodeBootstrapResponse(
            schema_version=1,
            request_id=str(request.request_id),
            status_code="ok",
            cluster_id=self.owner.cluster_id,
            controller_generation=self.owner.controller_generation,
            config_fingerprint=self.owner.config_fingerprint,
            environment_fingerprint=self.owner.environment_fingerprint,
            ray_address=self.owner.ray_address,
            ray_namespace=self.owner.ray_namespace,
            task_slots_total=self.owner.node_runtime_policy.task_slots_total,
            allow_colocation=self.owner.node_runtime_policy.allow_colocation,
            npu_system_reserved_hbm_mb=(
                self.owner.node_runtime_policy.npu_system_reserved_hbm_mb
            ),
            npu_hbm_headroom_mb=(self.owner.node_runtime_policy.npu_hbm_headroom_mb),
            host_mem_headroom_mb=(self.owner.node_runtime_policy.host_mem_headroom_mb),
            io_slots_total=self.owner.node_runtime_policy.io_slots_total,
            hbm_recovery_tolerance_mb=(
                self.owner.node_runtime_policy.hbm_recovery_tolerance_mb
            ),
            recording_backend=self.owner.node_runtime_policy.recording_backend,
            recording_control_queue_capacity=(
                self.owner.node_runtime_policy.recording_control_queue_capacity
            ),
            recording_telemetry_queue_capacity=(
                self.owner.node_runtime_policy.recording_telemetry_queue_capacity
            ),
            recording_batch_size=(self.owner.node_runtime_policy.recording_batch_size),
            recording_flush_interval_ms=(
                self.owner.node_runtime_policy.recording_flush_interval_ms
            ),
            recording_compression=(
                self.owner.node_runtime_policy.recording_compression
            ),
            recording_max_page_size=(
                self.owner.node_runtime_policy.recording_max_page_size
            ),
        )

    async def Connect(
        self,
        request_iterator: AsyncIterator[Any],
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> AsyncIterator[Any]:
        try:
            first = await anext(request_iterator)
        except StopAsyncIteration:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "registration required"
            )
            return
        if first.WhichOneof("body") != "register":
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "registration must be first"
            )
            return
        registration = first.register
        try:
            binding = self.owner._accept_registration(registration)
        except (ContractValidationError, ValueError) as exc:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, str(exc))
            return
        yield control_pb2.ControllerStreamMessage(
            registration=control_pb2.RegistrationAccepted(
                request_id=registration.meta.message_id,
                controller_generation=self.owner.controller_generation,
                runtime_generation=binding.runtime_generation,
                status_code="accepted",
                message="node registration accepted",
            )
        )
        try:
            async for request in request_iterator:
                ack = self.owner._handle_message(binding, request)
                yield control_pb2.ControllerStreamMessage(ack=ack)
        finally:
            changed = self.owner.registry.set_status(
                binding.node_id,
                RuntimeNodeStatus.STALE,
                boot_id=binding.boot_id,
                agent_generation=binding.agent_generation,
            )
            if changed and self.owner.on_binding_disconnected is not None:
                self.owner.on_binding_disconnected(binding)


class NodeControlServer:
    def __init__(
        self,
        *,
        cluster_id: str,
        authorization_token: bytes,
        controller_generation: str,
        environment_fingerprint: str,
        config_fingerprint: str = "unconfigured",
        ray_address: str = "unconfigured",
        ray_namespace: str = "default",
        node_runtime_policy: NodeRuntimePolicy | None = None,
        registry: RayNodeRegistry,
        recorder: ExecutionRecorder,
        event_sink: Callable[[RuntimeEvent], None],
        on_binding_replaced: Callable[[RuntimeNodeBinding], None] | None = None,
        on_binding_disconnected: Callable[[RuntimeNodeBinding], None] | None = None,
        on_binding_registered: (
            Callable[[RuntimeNodeBinding, RuntimeNodeBinding | None], None] | None
        ) = None,
        registration_validator: (
            Callable[[str, NodeCapacity | None], None] | None
        ) = None,
        on_node_observation: Callable[[NodeObservation], object] | None = None,
        on_service_process_exited: (
            Callable[[ServiceProcessExit], object] | None
        ) = None,
        on_recovery_inventory: (
            Callable[[RuntimeNodeBinding, NodeRecoveryInventory], object] | None
        ) = None,
        on_node_capacity: Callable[[NodeCapacity], object] | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not cluster_id or not authorization_token or not controller_generation:
            raise ValueError("cluster, token and controller generation are required")
        self.cluster_id = cluster_id
        self.authorization_token = authorization_token
        self.controller_generation = controller_generation
        self.environment_fingerprint = environment_fingerprint
        self.config_fingerprint = config_fingerprint
        self.ray_address = ray_address
        self.ray_namespace = ray_namespace
        self.node_runtime_policy = node_runtime_policy or NodeRuntimePolicy()
        self.registry = registry
        self.recorder = recorder
        self.event_sink = event_sink
        self.on_binding_replaced = on_binding_replaced
        self.on_binding_disconnected = on_binding_disconnected
        self.on_binding_registered = on_binding_registered
        self.registration_validator = registration_validator
        self.on_node_observation = on_node_observation
        self.on_service_process_exited = on_service_process_exited
        self.on_recovery_inventory = on_recovery_inventory
        self.on_node_capacity = on_node_capacity
        self.clock = clock or SystemClock()
        self._server: grpc.aio.Server | None = None
        self.endpoint: str | None = None

    async def start(
        self,
        bind_address: str = "127.0.0.1:0",
        *,
        advertised_host: str | None = None,
    ) -> str:
        if self._server is not None:
            assert self.endpoint is not None
            return self.endpoint
        host = advertised_host or bind_address.rsplit(":", 1)[0]
        if host in {"0.0.0.0", "::", "[::]"}:
            raise ValueError("wildcard RPC bind requires an advertised_host")
        server = grpc.aio.server()
        control_pb2_grpc.add_NodeControlServicer_to_server(
            _NodeControlServicer(self), server
        )
        port = server.add_insecure_port(bind_address)
        if port == 0:
            raise RuntimeError(f"failed to bind NodeControl RPC: {bind_address}")
        self.endpoint = f"{host}:{port}"
        await server.start()
        self._server = server
        return self.endpoint

    async def close(self, grace_seconds: float = 1.0) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        await server.stop(grace_seconds)

    def _accept_registration(self, registration: Any) -> RuntimeNodeBinding:
        meta = registration.meta
        if int(meta.schema_version) != 1:
            raise ContractValidationError("unsupported NodeAgent schema version")
        if str(meta.cluster_id) != self.cluster_id:
            raise ContractValidationError("NodeAgent cluster_id mismatch")
        if not hmac.compare_digest(
            bytes(registration.authorization_token), self.authorization_token
        ):
            raise ContractValidationError("NodeAgent authorization failed")
        device_mappings = tuple(
            RuntimeDeviceMapping(
                physical_device_id=str(item.physical_device_id),
                runtime_visible_device_id=str(item.runtime_visible_device_id),
                visible_device_index=int(item.visible_device_index),
            )
            for item in registration.device_mappings
        )
        RuntimeNodeBinding(
            node_id=str(meta.node_id),
            boot_id=str(meta.boot_id),
            ray_node_id=str(registration.ray_node_id),
            runtime_generation=1,
            agent_generation=str(meta.agent_generation),
            agent_endpoint=str(registration.agent_endpoint),
            producer_id=str(registration.producer_id),
            records_locally=bool(registration.records_locally),
            device_mappings=device_mappings,
        )
        capacity: NodeCapacity | None = None
        if registration.has_capacity:
            capacity = _decode_node_capacity(registration.capacity)
            if capacity.node_id != str(meta.node_id) or capacity.boot_id != str(
                meta.boot_id
            ):
                raise ContractValidationError(
                    "NodeAgent capacity identity does not match registration"
                )
            capacity_device_ids = {item.device_id for item in capacity.npus}
            mapping_device_ids = {
                item.physical_device_id for item in device_mappings
            }
            if device_mappings and mapping_device_ids != capacity_device_ids:
                raise ContractValidationError(
                    "NodeAgent device mappings do not match reported capacity"
                )
        if self.registration_validator is not None:
            self.registration_validator(str(meta.node_id), capacity)
        if capacity is not None and self.on_node_capacity is not None:
            self.on_node_capacity(capacity)
        status = (
            RuntimeNodeStatus.HEALTHY
            if str(registration.environment_fingerprint) == self.environment_fingerprint
            else RuntimeNodeStatus.UNSCHEDULABLE
        )
        binding, previous = self.registry.register(
            node_id=str(meta.node_id),
            boot_id=str(meta.boot_id),
            ray_node_id=str(registration.ray_node_id),
            agent_generation=str(meta.agent_generation),
            agent_endpoint=str(registration.agent_endpoint),
            producer_id=str(registration.producer_id),
            records_locally=bool(registration.records_locally),
            device_mappings=device_mappings,
            status=status,
        )
        if previous is not None and self.on_binding_replaced is not None:
            self.on_binding_replaced(previous)
        if self.on_recovery_inventory is not None:
            self.on_recovery_inventory(
                binding,
                NodeRecoveryInventory(
                    active_lease_ids=tuple(sorted(registration.active_lease_ids)),
                    service_handle_ids=tuple(sorted(registration.service_handle_ids)),
                    reported_controller_generation=(
                        str(registration.meta.controller_generation) or None
                    ),
                ),
            )
        if self.on_binding_registered is not None:
            self.on_binding_registered(binding, previous)
        return binding

    def _handle_message(self, binding: RuntimeNodeBinding, request: Any) -> Any:
        body = request.WhichOneof("body")
        if body not in {"heartbeat", "runtime_event", "service_process_exit"}:
            return self._ack("", "rejected", "unsupported stream message")
        value = getattr(request, body)
        meta = value.meta
        if not self._meta_matches(binding, meta):
            return self._ack(str(meta.message_id), "stale", "node generation mismatch")
        accepted = self.registry.accept_message(
            node_id=binding.node_id,
            boot_id=binding.boot_id,
            agent_generation=binding.agent_generation,
            sequence=int(meta.sequence),
        )
        if not accepted:
            return self._ack(str(meta.message_id), "duplicate", "old message sequence")
        if body == "runtime_event":
            try:
                event = decode_runtime_event(value.event)
                if not binding.records_locally:
                    self._record_node_event(
                        binding, int(value.producer_sequence), event
                    )
                self.event_sink(event)
            except Exception as exc:
                self.recorder.record_writer_error(
                    str(value.event.run_id), f"{type(exc).__name__}: {exc}"
                )
                return self._ack(str(meta.message_id), "rejected", str(exc))
        elif body == "heartbeat" and value.has_observation:
            try:
                observation = NodeObservation(
                    node_id=binding.node_id,
                    boot_id=binding.boot_id,
                    sequence=int(meta.sequence),
                    received_at_ms=self.clock.monotonic_ms(),
                    observed_free_mem_mb=int(value.observed_free_mem_mb),
                    npus=tuple(
                        NpuObservation(
                            device_id=str(item.device_id),
                            health=str(item.health),
                            observed_free_hbm_mb=int(item.observed_free_hbm_mb),
                            utilization=(
                                float(item.utilization)
                                if item.has_utilization
                                else None
                            ),
                        )
                        for item in value.npus
                    ),
                )
                if self.on_node_observation is not None:
                    self.on_node_observation(observation)
            except Exception as exc:
                return self._ack(str(meta.message_id), "rejected", str(exc))
        if body == "heartbeat" and self.on_recovery_inventory is not None:
            try:
                self.on_recovery_inventory(
                    binding,
                    NodeRecoveryInventory(
                        active_lease_ids=tuple(sorted(value.active_lease_ids)),
                        service_handle_ids=tuple(sorted(value.service_handle_ids)),
                        reported_controller_generation=(
                            str(value.meta.controller_generation) or None
                        ),
                    ),
                )
            except Exception as exc:
                return self._ack(str(meta.message_id), "rejected", str(exc))
        elif body == "service_process_exit":
            try:
                service_exit = ServiceProcessExit(
                    service_handle_id=str(value.service_handle_id),
                    instance_id=str(value.instance_id),
                    generation=int(value.generation),
                    process_id=int(value.process_id),
                    exit_code=int(value.exit_code),
                )
                if self.on_service_process_exited is not None:
                    self.on_service_process_exited(service_exit)
            except Exception as exc:
                return self._ack(str(meta.message_id), "rejected", str(exc))
        return self._ack(str(meta.message_id), "accepted", "")

    def _record_node_event(
        self,
        binding: RuntimeNodeBinding,
        producer_sequence: int,
        event: RuntimeEvent,
    ) -> None:
        accepted = self.recorder.emit(
            _execution_event_from_runtime(
                producer_id=binding.producer_id,
                producer_sequence=producer_sequence,
                producer_monotonic_time_ms=self.clock.monotonic_ms(),
                node_id=binding.node_id,
                event=event,
                wall_time_ms=self.clock.wall_ms(),
            )
        )
        if not accepted:
            self.recorder.record_writer_error(
                event.run_id, "NodeAgent recorder rejected a control event"
            )

    def _meta_matches(self, binding: RuntimeNodeBinding, meta: Any) -> bool:
        return (
            int(meta.schema_version) == 1
            and str(meta.cluster_id) == self.cluster_id
            and str(meta.node_id) == binding.node_id
            and str(meta.boot_id) == binding.boot_id
            and str(meta.agent_generation) == binding.agent_generation
            and str(meta.controller_generation) == self.controller_generation
        )

    def _ack(self, message_id: str, status_code: str, message: str) -> Any:
        return control_pb2.NodeMessageAck(
            message_id=message_id,
            controller_generation=self.controller_generation,
            status_code=status_code,
            message=message,
        )


class _WorkerEventServicer:
    def __init__(self, owner: "NodeAgent") -> None:
        self.owner = owner

    async def Report(
        self,
        request: Any,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> Any:
        del context
        return self.owner._accept_worker_event(request)

    async def OpenRunRecording(
        self,
        request: Any,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> Any:
        del context
        return self.owner._open_run_recording(request)

    async def FlushRunRecording(
        self,
        request: Any,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> Any:
        del context
        return await self.owner._flush_run_recording(request)


def _port_error(code: str, message: str) -> Any:
    return control_pb2.PortLeaseResponse(
        accepted=False,
        error_code=code,
        message=message,
    )


def _launch_error(code: str, message: str) -> Any:
    return control_pb2.ServiceLaunchResponse(
        accepted=False,
        error_code=code,
        message=message,
    )


def _probe_error(code: str, message: str) -> Any:
    return control_pb2.ServiceProcessProbeMessage(
        accepted=False,
        error_code=code,
        message=message,
    )


def _stop_error(code: str, message: str) -> Any:
    return control_pb2.ServiceStopResultMessage(
        accepted=False,
        error_code=code,
        message=message,
    )


class _ServiceProcessServicer:
    def __init__(self, owner: "NodeAgent") -> None:
        self.owner = owner

    async def AcquirePort(self, request: Any, context: Any) -> Any:
        del context
        error = self.owner._service_request_error(request.meta)
        if error is not None:
            return _port_error(error[0], error[1])
        manager = self.owner.service_process_manager
        if manager is None:
            return _port_error(
                "service_process_disabled", "service process manager is disabled"
            )
        try:
            lease = await manager.acquire_port(
                node_id=self.owner.identity.node_id,
                boot_id=self.owner.identity.boot_id,
                owner_instance_id=str(request.owner_instance_id),
                generation=int(request.generation),
            )
        except Exception as exc:
            return _port_error(
                "service_port_acquire_failed", f"{type(exc).__name__}: {exc}"
            )
        return control_pb2.PortLeaseResponse(
            accepted=True,
            lease=encode_port_lease(lease),
            has_lease=True,
        )

    async def ReleasePort(self, request: Any, context: Any) -> Any:
        del context
        error = self.owner._service_request_error(request.meta)
        if error is not None:
            return _port_error(error[0], error[1])
        manager = self.owner.service_process_manager
        if manager is None:
            return _port_error(
                "service_process_disabled", "service process manager is disabled"
            )
        try:
            released = await manager.release_port(decode_port_lease(request.lease))
        except Exception as exc:
            return _port_error(
                "service_port_release_failed", f"{type(exc).__name__}: {exc}"
            )
        if not released:
            return _port_error("port_lease_not_found", "PortLease is already released")
        return control_pb2.PortLeaseResponse(accepted=True)

    async def Launch(self, request: Any, context: Any) -> Any:
        del context
        error = self.owner._service_request_error(request.meta)
        if error is not None:
            return _launch_error(error[0], error[1])
        manager = self.owner.service_process_manager
        if manager is None:
            return _launch_error(
                "service_process_disabled", "service process manager is disabled"
            )
        try:
            handle = await manager.launch(
                decode_service_launch(request.request),
                decode_model_placement(request.lease),
            )
        except Exception as exc:
            return _launch_error(
                "service_launch_failed", f"{type(exc).__name__}: {exc}"
            )
        return control_pb2.ServiceLaunchResponse(
            accepted=True,
            handle=encode_service_handle(handle),
            has_handle=True,
        )

    async def Probe(self, request: Any, context: Any) -> Any:
        del context
        error = self.owner._service_request_error(request.meta)
        if error is not None:
            return _probe_error(error[0], error[1])
        manager = self.owner.service_process_manager
        if manager is None:
            return _probe_error(
                "service_process_disabled", "service process manager is disabled"
            )
        try:
            probe = await manager.probe_process(
                decode_service_handle(request.handle),
                timeout_ms=int(request.timeout_ms),
            )
        except Exception as exc:
            return _probe_error("service_probe_failed", f"{type(exc).__name__}: {exc}")
        return control_pb2.ServiceProcessProbeMessage(
            accepted=True,
            process_alive=probe.process_alive,
            port_open=probe.port_open,
            binding_verified=probe.binding_verified,
            physical_device_id=probe.physical_device_id,
            process_hbm_mb=probe.process_hbm_mb or 0,
            has_process_hbm_mb=probe.process_hbm_mb is not None,
            exit_code=probe.exit_code or 0,
            has_exit_code=probe.exit_code is not None,
        )

    async def Stop(self, request: Any, context: Any) -> Any:
        del context
        error = self.owner._service_request_error(request.meta)
        if error is not None:
            return _stop_error(error[0], error[1])
        manager = self.owner.service_process_manager
        if manager is None:
            return _stop_error(
                "service_process_disabled", "service process manager is disabled"
            )
        try:
            result = await manager.stop(
                decode_service_handle(request.handle),
                timeout_ms=int(request.timeout_ms),
            )
        except Exception as exc:
            return _stop_error("service_stop_failed", f"{type(exc).__name__}: {exc}")
        return control_pb2.ServiceStopResultMessage(
            accepted=True,
            process_exited=result.process_exited,
            port_released=result.port_released,
            hbm_recovered=result.hbm_recovered,
            exit_code=result.exit_code or 0,
            has_exit_code=result.exit_code is not None,
            forced_termination=result.forced_termination,
            final_hbm_mb=result.final_hbm_mb or 0,
            has_final_hbm_mb=result.final_hbm_mb is not None,
        )


class NodeAgent:
    def __init__(
        self,
        *,
        identity: NodeAgentIdentity,
        authorization_token: bytes,
        heartbeat_interval_ms: int = 1_000,
        event_queue_capacity: int = 1_024,
        recorder: ExecutionRecorder | None = None,
        worker_device_verifier: Callable[[int, str], bool] | None = None,
        node_observation_provider: (
            Callable[[int, int], NodeObservation] | None
        ) = None,
        service_process_manager: NodeServiceProcessManager | None = None,
        node_capacity: NodeCapacity | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not authorization_token:
            raise ValueError("NodeAgent authorization token is required")
        if heartbeat_interval_ms <= 0 or event_queue_capacity <= 0:
            raise ValueError("NodeAgent intervals and capacities must be positive")
        self.identity = identity
        self.authorization_token = authorization_token
        self.heartbeat_interval_ms = heartbeat_interval_ms
        self.clock = clock or SystemClock()
        self.recorder = recorder
        self.worker_device_verifier = worker_device_verifier
        self.node_observation_provider = node_observation_provider
        self.service_process_manager = service_process_manager
        self.node_capacity = node_capacity
        if service_process_manager is not None:
            service_process_manager.set_unexpected_exit_sink(
                self._service_process_exited
            )
        self._queue: asyncio.Queue[Any] = asyncio.Queue(event_queue_capacity)
        self._sequence = 0
        self._producer_sequence = 0
        self._recording_contexts: dict[str, RunRecordingContext] = {}
        self._active_leases: dict[str, dict[str, _ActiveWorkerLease]] = {}
        self._event_ids: set[str] = set()
        self._event_id_order: deque[str] = deque()
        self._event_dedup_capacity = max(1_024, event_queue_capacity * 4)
        self._message_acks: dict[str, str] = {}
        self._message_ack_order: deque[str] = deque()
        self._server: grpc.aio.Server | None = None
        self._channel: grpc.aio.Channel | None = None
        self._call: Any = None
        self._response_task: asyncio.Task[None] | None = None
        self._registered = asyncio.Event()
        self._closed = False
        self._controller_endpoint: str | None = None
        self.endpoint: str | None = None
        self.controller_generation: str | None = None
        self.runtime_generation: int | None = None

    async def start(
        self,
        *,
        controller_endpoint: str,
        worker_bind_address: str = "127.0.0.1:0",
        worker_advertised_host: str | None = None,
    ) -> str:
        if self._server is not None:
            assert self.endpoint is not None
            return self.endpoint
        host = worker_advertised_host or worker_bind_address.rsplit(":", 1)[0]
        if host in {"0.0.0.0", "::", "[::]"}:
            raise ValueError("wildcard Worker RPC bind requires an advertised_host")
        server = grpc.aio.server()
        control_pb2_grpc.add_WorkerEventSinkServicer_to_server(
            _WorkerEventServicer(self), server
        )
        control_pb2_grpc.add_ServiceProcessControlServicer_to_server(
            _ServiceProcessServicer(self), server
        )
        port = server.add_insecure_port(worker_bind_address)
        if port == 0:
            raise RuntimeError(f"failed to bind WorkerEvent RPC: {worker_bind_address}")
        self.endpoint = f"{host}:{port}"
        await server.start()
        self._server = server
        self._controller_endpoint = controller_endpoint
        await self._connect(controller_endpoint)
        return self.endpoint

    async def reconnect(self, controller_endpoint: str | None = None) -> str:
        """Reconnect the long-lived node authority to a new Controller generation."""

        if self._closed or self._server is None or self.endpoint is None:
            raise RuntimeError("NodeAgent is not running")
        endpoint = controller_endpoint or self._controller_endpoint
        if endpoint is None:
            raise RuntimeError("Controller endpoint is unavailable")
        call = self._call
        if call is not None:
            call.cancel()
        task = self._response_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._channel is not None:
            await self._channel.close()
        self._registered.clear()
        self._controller_endpoint = endpoint
        await self._connect(endpoint)
        return self.endpoint

    async def _connect(self, controller_endpoint: str) -> None:
        self._channel = grpc.aio.insecure_channel(controller_endpoint)
        stub = control_pb2_grpc.NodeControlStub(self._channel)
        self._call = stub.Connect(self._request_stream())
        self._response_task = asyncio.create_task(self._consume_responses())
        registration_waiter = asyncio.create_task(self._registered.wait())
        try:
            done, _ = await asyncio.wait(
                {registration_waiter, self._response_task},
                timeout=5,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if registration_waiter in done and self._registered.is_set():
                return
            if self._response_task in done:
                await self._response_task
                raise RuntimeError("NodeControl stream ended before registration")
            raise TimeoutError("NodeAgent registration timed out")
        except Exception:
            await self.close()
            raise
        finally:
            registration_waiter.cancel()
            await asyncio.gather(registration_waiter, return_exceptions=True)

    async def close(self, grace_seconds: float = 1.0) -> None:
        if self._closed:
            return
        self._closed = True
        call = self._call
        if call is not None:
            call.cancel()
        task = self._response_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._channel is not None:
            await self._channel.close()
        if self._server is not None:
            await self._server.stop(grace_seconds)
        self._server = None
        if self.service_process_manager is not None:
            await self.service_process_manager.close(
                max(1_000, int(grace_seconds * 1_000))
            )
        if self.recorder is not None:
            await self.recorder.close(max(1, int(grace_seconds * 1_000) or 1_000))

    async def stop_worker_event_server(self, grace_seconds: float = 0) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        await server.stop(grace_seconds)

    def message_ack(self, message_id: str) -> str | None:
        return self._message_acks.get(message_id)

    async def _request_stream(self) -> AsyncIterator[Any]:
        assert self.endpoint is not None
        self._prune_exited_worker_leases()
        service_handle_ids: tuple[str, ...] = ()
        if self.service_process_manager is not None:
            service_handle_ids = tuple(
                item.service_handle_id
                for item in await self.service_process_manager.active_handles()
            )
        registration = control_pb2.RegisterNode(
            meta=self._next_meta(),
            ray_node_id=self.identity.ray_node_id,
            agent_endpoint=self.endpoint,
            producer_id=self.identity.producer_id,
            environment_fingerprint=self.identity.environment_fingerprint,
            authorization_token=self.authorization_token,
            records_locally=self.recorder is not None,
            active_lease_ids=tuple(
                sorted(
                    lease_id
                    for leases in self._active_leases.values()
                    for lease_id in leases
                )
            ),
            service_handle_ids=tuple(sorted(service_handle_ids)),
        )
        for mapping in self.identity.device_mappings:
            registration.device_mappings.add(
                physical_device_id=mapping.physical_device_id,
                runtime_visible_device_id=mapping.runtime_visible_device_id,
                visible_device_index=mapping.visible_device_index,
            )
        if self.node_capacity is not None:
            registration.capacity.CopyFrom(_encode_node_capacity(self.node_capacity))
            registration.has_capacity = True
        yield control_pb2.AgentStreamMessage(register=registration)
        await self._registered.wait()
        while not self._closed:
            try:
                message = await asyncio.wait_for(
                    self._queue.get(), self.heartbeat_interval_ms / 1_000
                )
            except asyncio.TimeoutError:
                self._prune_exited_worker_leases()
                meta = self._next_meta()
                service_handle_ids = ()
                if self.service_process_manager is not None:
                    service_handle_ids = tuple(
                        item.service_handle_id
                        for item in await self.service_process_manager.active_handles()
                    )
                heartbeat = control_pb2.NodeHeartbeat(
                    meta=meta,
                    active_lease_ids=tuple(
                        sorted(
                            lease_id
                            for leases in self._active_leases.values()
                            for lease_id in leases
                        )
                    ),
                    service_handle_ids=tuple(sorted(service_handle_ids)),
                )
                provider = self.node_observation_provider
                if provider is not None:
                    try:
                        observation = provider(
                            int(meta.sequence), self.clock.monotonic_ms()
                        )
                    except Exception:
                        observation = None
                    if observation is not None:
                        self._record_observation(observation)
                        heartbeat.has_observation = True
                        heartbeat.observed_free_mem_mb = (
                            observation.observed_free_mem_mb
                        )
                        for item in observation.npus:
                            heartbeat.npus.add(
                                device_id=item.device_id,
                                health=item.health,
                                observed_free_hbm_mb=item.observed_free_hbm_mb,
                                utilization=item.utilization or 0.0,
                                has_utilization=item.utilization is not None,
                            )
                message = control_pb2.AgentStreamMessage(heartbeat=heartbeat)
            else:
                self._stamp_queued_message(message)
            yield message

    def _stamp_queued_message(self, message: Any) -> None:
        """Assign sequence identity in wire order, immediately before yielding."""

        body = message.WhichOneof("body")
        if body not in {"heartbeat", "runtime_event", "service_process_exit"}:
            raise RuntimeError(f"unsupported queued NodeAgent message: {body}")
        meta = getattr(message, body).meta
        if int(meta.sequence) == 0:
            meta.CopyFrom(self._next_meta())

    async def _consume_responses(self) -> None:
        assert self._call is not None
        async for response in self._call:
            body = response.WhichOneof("body")
            if body == "registration":
                if response.registration.status_code != "accepted":
                    raise RuntimeError(response.registration.message)
                self.controller_generation = str(
                    response.registration.controller_generation
                )
                self.runtime_generation = int(response.registration.runtime_generation)
                self._registered.set()
            elif body == "ack":
                message_id = str(response.ack.message_id)
                self._message_acks[message_id] = str(response.ack.status_code)
                self._message_ack_order.append(message_id)
                if len(self._message_ack_order) > self._event_dedup_capacity:
                    expired = self._message_ack_order.popleft()
                    self._message_acks.pop(expired, None)

    def _accept_worker_event(self, request: Any) -> Any:
        event_id = str(request.event.event_id)
        node_identity_matches = (
            int(request.schema_version) == 1
            and str(request.cluster_id) == self.identity.cluster_id
            and str(request.node_id) == self.identity.node_id
            and str(request.boot_id) == self.identity.boot_id
            and str(request.agent_generation) == self.identity.agent_generation
        )
        authority_matches = (
            str(request.controller_generation) == self.controller_generation
            and int(request.runtime_generation) == self.runtime_generation
        )
        if not node_identity_matches or not authority_matches:
            if node_identity_matches:
                self._consume_stale_worker_terminal(request)
            return control_pb2.WorkerEventAck(
                event_id=event_id,
                accepted=False,
                error_code="stale_worker_generation",
                message="Worker event identity does not match NodeAgent",
            )
        if event_id in self._event_ids:
            return control_pb2.WorkerEventAck(event_id=event_id, accepted=True)
        if str(request.event.kind) == RuntimeEventKind.WORKER_STARTED.value and str(
            request.event.device_id
        ):
            verifier = self.worker_device_verifier
            if (
                verifier is None
                or not request.event.has_worker_pid
                or not request.event.binding_verified
                or not verifier(
                    int(request.event.worker_pid),
                    str(request.event.device_id),
                )
            ):
                return control_pb2.WorkerEventAck(
                    event_id=event_id,
                    accepted=False,
                    error_code="device_bind_failed",
                    message=(
                        "NodeAgent could not verify Worker PID on the leased "
                        "physical NPU"
                    ),
                )
        try:
            event = decode_runtime_event(request.event)
        except Exception as exc:
            return control_pb2.WorkerEventAck(
                event_id=event_id,
                accepted=False,
                error_code="invalid_worker_event",
                message=str(exc),
            )
        producer_sequence = self._next_producer_sequence()
        self._record_runtime_event(event, producer_sequence)
        message = control_pb2.AgentStreamMessage(
            runtime_event=control_pb2.NodeRuntimeEvent(
                event=request.event,
                producer_sequence=producer_sequence,
            )
        )
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            return control_pb2.WorkerEventAck(
                event_id=event_id,
                accepted=False,
                error_code="node_event_queue_full",
                message="NodeAgent control event queue is full",
            )
        if event.kind is RuntimeEventKind.WORKER_STARTED:
            process_id = event.worker_pid
            self._active_leases.setdefault(event.run_id, {})[event.lease_id] = (
                _ActiveWorkerLease(
                    device_id=event.device_id,
                    process_id=process_id,
                    process_start_time=self._process_start_time(process_id),
                    controller_generation=str(request.controller_generation),
                    runtime_generation=int(request.runtime_generation),
                )
            )
        elif event.kind in TERMINAL_RUNTIME_EVENT_KINDS:
            self._remove_active_worker_lease(event.run_id, event.lease_id)
        self._event_ids.add(event_id)
        self._event_id_order.append(event_id)
        if len(self._event_id_order) > self._event_dedup_capacity:
            expired = self._event_id_order.popleft()
            self._event_ids.discard(expired)
        return control_pb2.WorkerEventAck(event_id=event_id, accepted=True)

    def _consume_stale_worker_terminal(self, request: Any) -> None:
        try:
            event = decode_runtime_event(request.event)
        except Exception:
            return
        if event.kind not in TERMINAL_RUNTIME_EVENT_KINDS:
            return
        active = self._active_leases.get(event.run_id, {}).get(event.lease_id)
        if active is None:
            return
        if active.controller_generation != str(
            request.controller_generation
        ) or active.runtime_generation != int(request.runtime_generation):
            return
        self._remove_active_worker_lease(event.run_id, event.lease_id)

    def _remove_active_worker_lease(self, run_id: str, lease_id: str) -> None:
        active = self._active_leases.get(run_id)
        if active is None:
            return
        active.pop(lease_id, None)
        if not active:
            self._active_leases.pop(run_id, None)

    def _prune_exited_worker_leases(self) -> None:
        for run_id, leases in tuple(self._active_leases.items()):
            for lease_id, worker in tuple(leases.items()):
                process_id = worker.process_id
                if process_id is None:
                    continue
                current_start_time = self._process_start_time(process_id)
                if (
                    current_start_time is None
                    or current_start_time != worker.process_start_time
                ):
                    self._remove_active_worker_lease(run_id, lease_id)

    @staticmethod
    def _process_start_time(process_id: int | None) -> str | None:
        if process_id is None or process_id <= 0:
            return None
        try:
            stat = Path(f"/proc/{process_id}/stat").read_text(encoding="ascii")
        except OSError:
            return None
        _, separator, suffix = stat.rpartition(")")
        if not separator:
            return None
        fields = suffix.split()
        return fields[19] if len(fields) > 19 else None

    def _open_run_recording(self, request: Any) -> Any:
        run_id = str(request.context.run_id)
        error = self._recording_request_error(request)
        if error is not None:
            return control_pb2.RecorderControlAck(
                run_id=run_id,
                accepted=False,
                error_code=error[0],
                message=error[1],
            )
        recorder = self.recorder
        if recorder is None:
            return control_pb2.RecorderControlAck(
                run_id=run_id,
                accepted=False,
                error_code="recording_disabled",
                message="NodeAgent has no local recorder",
            )
        try:
            received = _decode_recording_context(request.context)
            local = replace(
                received,
                initial_expected_producer_ids=(self.identity.producer_id,),
            )
            existing = self._recording_contexts.get(local.run_id)
            if existing is not None and existing != local:
                raise ContractValidationError("run recording context conflict")
            recorder.open_run(local)
            self._recording_contexts[local.run_id] = local
            if existing is None:
                self._emit_local(
                    ExecutionEvent(
                        schema_version=1,
                        event_id=new_id("node_record"),
                        experiment_id=local.experiment_id,
                        run_id=local.run_id,
                        task_id=None,
                        attempt=None,
                        lease_id=None,
                        route_lease_id=None,
                        model_instance_id=None,
                        event_type="recorder_producer_joined",
                        producer_id=self.identity.producer_id,
                        producer_sequence=self._next_producer_sequence(),
                        node_id=self.identity.node_id,
                        device_id=None,
                        monotonic_time_ms=self.clock.monotonic_ms(),
                        wall_time_ms=self.clock.wall_ms(),
                        duration_ms=None,
                    )
                )
        except Exception as exc:
            return control_pb2.RecorderControlAck(
                run_id=run_id,
                accepted=False,
                error_code="recording_open_failed",
                message=f"{type(exc).__name__}: {exc}",
            )
        return control_pb2.RecorderControlAck(run_id=run_id, accepted=True)

    async def _flush_run_recording(self, request: Any) -> Any:
        run_id = str(request.run_id)
        error = self._recording_request_error(request)
        if error is not None:
            return _encode_flush_error(run_id, error[0], error[1])
        recorder = self.recorder
        if recorder is None:
            return _encode_flush_error(
                run_id, "recording_disabled", "NodeAgent has no local recorder"
            )
        if run_id not in self._recording_contexts:
            return _encode_flush_error(
                run_id, "unknown_recording_run", "Run recording was not opened"
            )
        try:
            result = await recorder.flush_run(run_id, int(request.timeout_ms))
        except Exception as exc:
            return _encode_flush_error(
                run_id,
                "recording_flush_failed",
                f"{type(exc).__name__}: {exc}",
            )
        return _encode_flush_result(result)

    def _recording_request_error(self, request: Any) -> tuple[str, str] | None:
        if int(request.schema_version) != 1:
            return "unsupported_schema", "Unsupported recorder control schema"
        if not hmac.compare_digest(
            bytes(request.authorization_token), self.authorization_token
        ):
            return "authorization_failed", "Recorder control authorization failed"
        expected = (
            self.identity.cluster_id,
            self.identity.node_id,
            self.identity.boot_id,
            self.identity.agent_generation,
            self.controller_generation,
            self.runtime_generation,
        )
        actual = (
            str(request.cluster_id),
            str(request.node_id),
            str(request.boot_id),
            str(request.agent_generation),
            str(request.controller_generation),
            int(request.runtime_generation),
        )
        if actual != expected:
            return "stale_recording_generation", "Recorder control generation mismatch"
        return None

    def _service_request_error(self, meta: Any) -> tuple[str, str] | None:
        if int(meta.schema_version) != 1:
            return "unsupported_schema", "Unsupported service control schema"
        if not hmac.compare_digest(
            bytes(meta.authorization_token), self.authorization_token
        ):
            return "authorization_failed", "Service control authorization failed"
        expected = (
            self.identity.cluster_id,
            self.identity.node_id,
            self.identity.boot_id,
            self.identity.agent_generation,
            self.controller_generation,
            self.runtime_generation,
        )
        actual = (
            str(meta.cluster_id),
            str(meta.node_id),
            str(meta.boot_id),
            str(meta.agent_generation),
            str(meta.controller_generation),
            int(meta.runtime_generation),
        )
        if actual != expected:
            return "stale_service_generation", "Service control generation mismatch"
        return None

    async def _service_process_exited(self, event: ServiceProcessExit) -> None:
        if self._closed:
            return
        await self._queue.put(
            control_pb2.AgentStreamMessage(
                service_process_exit=control_pb2.ServiceProcessExitMessage(
                    service_handle_id=event.service_handle_id,
                    instance_id=event.instance_id,
                    generation=event.generation,
                    process_id=event.process_id,
                    exit_code=event.exit_code,
                )
            )
        )

    def _record_runtime_event(
        self, event: RuntimeEvent, producer_sequence: int
    ) -> None:
        if self.recorder is None:
            return
        self._emit_local(
            _execution_event_from_runtime(
                producer_id=self.identity.producer_id,
                producer_sequence=producer_sequence,
                producer_monotonic_time_ms=self.clock.monotonic_ms(),
                node_id=self.identity.node_id,
                event=event,
                wall_time_ms=self.clock.wall_ms(),
            )
        )

    def _record_observation(self, observation: NodeObservation) -> None:
        if self.recorder is None:
            return
        for run_id, leases in tuple(self._active_leases.items()):
            context = self._recording_contexts.get(run_id)
            if context is None:
                continue
            node_payload = freeze_canonical(
                {
                    "observed_free_mem_mb": observation.observed_free_mem_mb,
                    "active_lease_count": len(leases),
                    "observation_sequence": observation.sequence,
                }
            )
            assert isinstance(node_payload, FrozenMap)
            self._emit_local(
                ExecutionEvent(
                    schema_version=1,
                    event_id=new_id("node_record"),
                    experiment_id=context.experiment_id,
                    run_id=run_id,
                    task_id=None,
                    attempt=None,
                    lease_id=None,
                    route_lease_id=None,
                    model_instance_id=None,
                    event_type="node_resource_sample",
                    producer_id=self.identity.producer_id,
                    producer_sequence=self._next_producer_sequence(),
                    node_id=self.identity.node_id,
                    device_id=None,
                    monotonic_time_ms=observation.received_at_ms,
                    wall_time_ms=self.clock.wall_ms(),
                    duration_ms=None,
                    payload=node_payload,
                )
            )
            for npu in observation.npus:
                device_payload = freeze_canonical(
                    {
                        "health": npu.health,
                        "observed_free_hbm_mb": npu.observed_free_hbm_mb,
                        "utilization": npu.utilization,
                        "active_lease_count": sum(
                            worker.device_id == npu.device_id
                            for worker in leases.values()
                        ),
                        "observation_sequence": observation.sequence,
                    }
                )
                assert isinstance(device_payload, FrozenMap)
                self._emit_local(
                    ExecutionEvent(
                        schema_version=1,
                        event_id=new_id("node_record"),
                        experiment_id=context.experiment_id,
                        run_id=run_id,
                        task_id=None,
                        attempt=None,
                        lease_id=None,
                        route_lease_id=None,
                        model_instance_id=None,
                        event_type="device_resource_sample",
                        producer_id=self.identity.producer_id,
                        producer_sequence=self._next_producer_sequence(),
                        node_id=self.identity.node_id,
                        device_id=npu.device_id,
                        monotonic_time_ms=observation.received_at_ms,
                        wall_time_ms=self.clock.wall_ms(),
                        duration_ms=None,
                        payload=device_payload,
                    )
                )

    def _emit_local(self, event: ExecutionEvent) -> None:
        recorder = self.recorder
        if recorder is None:
            return
        try:
            if not recorder.emit(event) and event.run_id is not None:
                recorder.record_writer_error(
                    event.run_id,
                    f"NodeAgent recorder rejected {event.event_type}",
                )
        except Exception as exc:
            if event.run_id is not None:
                try:
                    recorder.record_writer_error(
                        event.run_id, f"{type(exc).__name__}: {exc}"
                    )
                except Exception:
                    pass

    def _next_meta(self) -> Any:
        self._sequence += 1
        return control_pb2.AgentMeta(
            schema_version=1,
            cluster_id=self.identity.cluster_id,
            node_id=self.identity.node_id,
            boot_id=self.identity.boot_id,
            agent_generation=self.identity.agent_generation,
            sequence=self._sequence,
            message_id=new_id("node_message"),
            sent_at_ms=self.clock.wall_ms(),
            controller_generation=self.controller_generation or "",
        )

    def _next_producer_sequence(self) -> int:
        self._producer_sequence += 1
        return self._producer_sequence


def _encode_recording_context(context: RunRecordingContext) -> Any:
    return control_pb2.RunRecordingContextMessage(
        schema_version=context.schema_version,
        experiment_id=context.experiment_id,
        run_id=context.run_id,
        workflow_fingerprint=context.workflow_fingerprint,
        config_fingerprint=context.config_fingerprint,
        environment_fingerprint=context.environment_fingerprint,
        build_revision=context.build_revision,
        started_wall_time_ms=context.started_wall_time_ms,
        initial_expected_producer_ids=context.initial_expected_producer_ids,
    )


def _decode_recording_context(message: Any) -> RunRecordingContext:
    return RunRecordingContext(
        schema_version=int(message.schema_version),
        experiment_id=str(message.experiment_id),
        run_id=str(message.run_id),
        workflow_fingerprint=str(message.workflow_fingerprint),
        config_fingerprint=str(message.config_fingerprint),
        environment_fingerprint=str(message.environment_fingerprint),
        build_revision=str(message.build_revision),
        started_wall_time_ms=int(message.started_wall_time_ms),
        initial_expected_producer_ids=tuple(message.initial_expected_producer_ids),
    )


def _encode_flush_result(result: FlushResult) -> Any:
    return control_pb2.FlushResultMessage(
        run_id=result.run_id,
        committed_files=result.committed_files,
        dropped_control_event_count=result.dropped_control_event_count,
        dropped_telemetry_count=result.dropped_telemetry_count,
        sequence_gap_count=result.sequence_gap_count,
        missing_producer_count=result.missing_producer_count,
        writer_errors=result.writer_errors,
        recording_complete=result.recording_complete,
        flush_duration_ms=result.flush_duration_ms,
        accepted=True,
    )


def _encode_flush_error(run_id: str, error_code: str, message: str) -> Any:
    return control_pb2.FlushResultMessage(
        run_id=run_id,
        accepted=False,
        error_code=error_code,
        message=message,
    )


def _decode_flush_result(message: Any) -> FlushResult:
    if not bool(message.accepted):
        raise RuntimeError(f"{message.error_code}: {message.message}")
    return FlushResult(
        run_id=str(message.run_id),
        committed_files=tuple(message.committed_files),
        dropped_control_event_count=int(message.dropped_control_event_count),
        dropped_telemetry_count=int(message.dropped_telemetry_count),
        sequence_gap_count=int(message.sequence_gap_count),
        missing_producer_count=int(message.missing_producer_count),
        writer_errors=tuple(message.writer_errors),
        recording_complete=bool(message.recording_complete),
        flush_duration_ms=int(message.flush_duration_ms),
    )


def open_node_recording(
    *,
    binding: RuntimeNodeBinding,
    cluster_id: str,
    controller_generation: str,
    authorization_token: bytes,
    context: RunRecordingContext,
    timeout_seconds: float,
) -> None:
    request = control_pb2.OpenRunRecordingRequest(
        schema_version=1,
        cluster_id=cluster_id,
        node_id=binding.node_id,
        boot_id=binding.boot_id,
        agent_generation=binding.agent_generation,
        controller_generation=controller_generation,
        runtime_generation=binding.runtime_generation,
        authorization_token=authorization_token,
        context=_encode_recording_context(context),
    )
    with grpc.insecure_channel(binding.agent_endpoint) as channel:
        response = control_pb2_grpc.WorkerEventSinkStub(channel).OpenRunRecording(
            request, timeout=timeout_seconds
        )
    if not response.accepted:
        raise RuntimeError(f"{response.error_code}: {response.message}")


def flush_node_recording(
    *,
    binding: RuntimeNodeBinding,
    cluster_id: str,
    controller_generation: str,
    authorization_token: bytes,
    run_id: str,
    timeout_ms: int,
) -> FlushResult:
    request = control_pb2.FlushRunRecordingRequest(
        schema_version=1,
        cluster_id=cluster_id,
        node_id=binding.node_id,
        boot_id=binding.boot_id,
        agent_generation=binding.agent_generation,
        controller_generation=controller_generation,
        runtime_generation=binding.runtime_generation,
        authorization_token=authorization_token,
        run_id=run_id,
        timeout_ms=timeout_ms,
    )
    with grpc.insecure_channel(binding.agent_endpoint) as channel:
        response = control_pb2_grpc.WorkerEventSinkStub(channel).FlushRunRecording(
            request, timeout=max(0.001, timeout_ms / 1_000)
        )
    return _decode_flush_result(response)


def report_worker_event(
    *,
    endpoint: str,
    identity: NodeAgentIdentity,
    controller_generation: str,
    runtime_generation: int,
    event: RuntimeEvent,
    timeout_seconds: float,
) -> None:
    request = control_pb2.WorkerEventRequest(
        schema_version=1,
        cluster_id=identity.cluster_id,
        node_id=identity.node_id,
        boot_id=identity.boot_id,
        agent_generation=identity.agent_generation,
        controller_generation=controller_generation,
        runtime_generation=runtime_generation,
        event=encode_runtime_event(event),
    )
    with grpc.insecure_channel(endpoint) as channel:
        stub = control_pb2_grpc.WorkerEventSinkStub(channel)
        response = stub.Report(request, timeout=timeout_seconds)
    if not response.accepted:
        raise RuntimeError(f"{response.error_code}: {response.message}")
