"""Serial model-instance state and global PlacementLease ownership."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Callable

from ascend_maze.contracts.resources import PlacementLease
from ascend_maze.core.clock import Clock, SystemClock
from ascend_maze.core.canonical import FrozenMap, freeze_canonical
from ascend_maze.core.errors import StateTransitionError
from ascend_maze.core.identifiers import new_id
from ascend_maze.inference.catalog import ModelCatalog
from ascend_maze.inference.contracts import (
    EngineProbe,
    ModelControlEvent,
    ModelInstance,
    ModelInstanceState,
    ModelSpec,
    PortLease,
    PortLeaseManager,
    ServiceHandle,
    ServiceProcessBackend,
)
from ascend_maze.inference.ports import InMemoryPortLeaseManager
from ascend_maze.placement import PlacementManager


@dataclass(slots=True)
class _InstanceRecord:
    instance_id: str
    spec: ModelSpec
    generation: int
    state: ModelInstanceState
    created_at_ms: int
    state_changed_at_ms: int
    ready_at_ms: int | None = None
    placement_lease: PlacementLease | None = None
    port_lease: PortLease | None = None
    service_handle: ServiceHandle | None = None
    route_occupancy: int = 0
    actual_request_inflight: int = 0
    last_used_at_ms: int = 0
    failure_reason: str | None = None
    cleanup_deadline_at_ms: int | None = None
    cleanup_timeout_reported: bool = False


@dataclass(frozen=True, slots=True)
class ModelInstanceRecoveryRecord:
    instance_id: str
    spec: ModelSpec
    generation: int
    state: ModelInstanceState
    created_at_ms: int
    state_changed_at_ms: int
    ready_at_ms: int | None
    placement_lease: PlacementLease | None
    port_lease: PortLease | None
    service_handle: ServiceHandle | None
    route_occupancy: int
    actual_request_inflight: int
    last_used_at_ms: int
    failure_reason: str | None
    cleanup_deadline_at_ms: int | None
    cleanup_timeout_reported: bool


class ModelInstanceManager:
    def __init__(
        self,
        *,
        catalog: ModelCatalog,
        placement: PlacementManager,
        service_backend: ServiceProcessBackend,
        event_sink: Callable[[ModelControlEvent], None] | None = None,
        clock: Clock | None = None,
        first_port: int = 25_000,
        last_port: int = 65_535,
        port_leases: PortLeaseManager | None = None,
    ) -> None:
        self.catalog = catalog
        self.placement = placement
        self.service_backend = service_backend
        self.event_sink = event_sink
        self.clock = clock or SystemClock()
        self.port_leases = port_leases or InMemoryPortLeaseManager(
            first_port=first_port,
            last_port=last_port,
        )
        self._records: dict[str, _InstanceRecord] = {}
        self._ready_index: dict[tuple[str, str], set[tuple[str, int]]] = {}
        self._lock = RLock()

    def create_requested(self, model_id: str) -> ModelInstance:
        spec = self.catalog.get(model_id)
        now = self.clock.monotonic_ms()
        record = _InstanceRecord(
            instance_id=new_id("model_instance"),
            spec=spec,
            generation=1,
            state=ModelInstanceState.REQUESTED,
            created_at_ms=now,
            state_changed_at_ms=now,
            last_used_at_ms=now,
        )
        with self._lock:
            self._records[record.instance_id] = record
        self._emit(record, "model_instance_requested")
        return self._snapshot(record)

    def restart_stopped(self, instance_id: str) -> ModelInstance:
        with self._lock:
            record = self._require(instance_id)
            if record.state is not ModelInstanceState.STOPPED:
                raise StateTransitionError("only a stopped model instance can restart")
            if (
                record.placement_lease is not None
                or record.port_lease is not None
                or record.service_handle is not None
                or record.route_occupancy
                or record.actual_request_inflight
            ):
                raise StateTransitionError("stopped model instance still owns resources")
            previous_generation = record.generation
            now = self.clock.monotonic_ms()
            record.generation += 1
            record.state = ModelInstanceState.REQUESTED
            record.created_at_ms = now
            record.state_changed_at_ms = now
            record.ready_at_ms = None
            record.last_used_at_ms = now
            record.failure_reason = None
            snapshot = self._snapshot(record)
        self._emit(
            record,
            "model_instance_restarted",
            {"previous_generation": previous_generation},
        )
        return snapshot

    async def start_instance(self, instance_id: str) -> ModelInstance:
        with self._lock:
            record = self._require(instance_id)
            if record.state is ModelInstanceState.READY:
                return self._snapshot(record)
            if record.state not in {
                ModelInstanceState.REQUESTED,
                ModelInstanceState.RESERVING,
            }:
                raise StateTransitionError(
                    f"cannot start instance from {record.state.value}"
                )
            self._transition(record, ModelInstanceState.RESERVING)
            generation = record.generation
            spec = record.spec
        startup_deadline = monotonic() + spec.startup_timeout_ms / 1_000
        try:
            now = self.clock.monotonic_ms()
            with self._lock:
                current = self._require_generation(instance_id, generation)
                placement = self.placement.reserve_model_instance(
                    instance_id=current.instance_id,
                    generation=current.generation,
                    resources=spec.reservation,
                    allow_colocation=spec.allow_colocation,
                    now_ms=now,
                    startup_deadline_ms=now + spec.startup_timeout_ms,
                )
                if not placement.selected:
                    self._emit(
                        current,
                        "model_placement_pending",
                        {"reason": placement.rejection_reason},
                    )
                    return self._snapshot(current)
                assert placement.lease is not None
                current.placement_lease = placement.lease
                self._transition(
                    current,
                    ModelInstanceState.STARTING,
                    {"startup_deadline_ms": placement.lease.dispatch_deadline_ms},
                )
            adapter = self.catalog.adapter(spec.model_id)
            port = await asyncio.wait_for(
                self.port_leases.acquire(
                    node_id=placement.lease.node_id,
                    boot_id=placement.lease.boot_id,
                    owner_instance_id=instance_id,
                    generation=generation,
                ),
                timeout=self._remaining(startup_deadline),
            )
            with self._lock:
                current = self._require_generation(instance_id, generation)
                current.port_lease = port
            request = adapter.build_launch_request(spec, placement.lease, port)
            handle = await asyncio.wait_for(
                self.service_backend.launch(request, placement.lease),
                timeout=self._remaining(startup_deadline),
            )
            with self._lock:
                current = self._require_generation(instance_id, generation)
                current.service_handle = handle
            self._validate_service_handle(
                instance_id=instance_id,
                generation=generation,
                lease=placement.lease,
                request_endpoint_id=request.endpoint_id,
                request_port_lease_id=request.port_lease_id,
                request_port=request.port,
                handle=handle,
            )
            attach = getattr(self.service_backend, "attach_spec", None)
            if callable(attach):
                attach(handle, spec)
            with self._lock:
                current = self._require_generation(instance_id, generation)
                if not self.placement.bind_lease(
                    placement.lease.lease_id,
                    now_ms=self.clock.monotonic_ms(),
                ):
                    raise RuntimeError("model PlacementLease could not be bound")
                self._transition(
                    current,
                    ModelInstanceState.WARMING,
                    {"process_id": handle.process_id},
                )
            probe = await asyncio.wait_for(
                adapter.probe(handle, spec),
                timeout=self._remaining(startup_deadline),
            )
            self._validate_probe(spec, placement.lease, probe)
            warmup = await asyncio.wait_for(
                adapter.warmup(handle, spec),
                timeout=self._remaining(startup_deadline),
            )
            if not warmup.succeeded or not warmup.response_digest:
                raise RuntimeError("model warmup did not produce a valid response")
            metrics = await asyncio.wait_for(
                adapter.read_metrics(handle),
                timeout=self._remaining(startup_deadline),
            )
            if metrics.actual_request_inflight != 0:
                raise RuntimeError("new model instance reported active requests")
            with self._lock:
                current = self._require_generation(instance_id, generation)
                now = self.clock.monotonic_ms()
                current.ready_at_ms = now
                current.last_used_at_ms = now
                self._transition(
                    current,
                    ModelInstanceState.READY,
                    {
                        "warmup_duration_ms": warmup.duration_ms,
                        "warmup_response_digest": warmup.response_digest,
                        "process_hbm_mb": probe.process_hbm_mb,
                        "request_capacity": probe.request_capacity,
                        "engine_queue_depth": metrics.queue_depth,
                    },
                )
                return self._snapshot(current)
        except Exception as exc:
            with self._lock:
                current = self._require_generation(instance_id, generation)
                if current.state is ModelInstanceState.STOPPED:
                    return self._snapshot(current)
                current.failure_reason = f"{type(exc).__name__}: {exc}"
                self._transition(
                    current,
                    ModelInstanceState.FAILED,
                    {"reason": current.failure_reason},
                )
            await self._cleanup_failed(instance_id, generation)
            return self.snapshot(instance_id)

    def begin_drain(self, instance_id: str, generation: int) -> bool:
        with self._lock:
            record = self._require_generation(instance_id, generation)
            if record.state is ModelInstanceState.DRAINING:
                return False
            if record.state is not ModelInstanceState.READY:
                return False
            self._transition(record, ModelInstanceState.DRAINING)
            record.cleanup_deadline_at_ms = (
                self.clock.monotonic_ms() + record.spec.drain_timeout_ms
            )
            record.cleanup_timeout_reported = False
            return True

    def cancel_drain(self, instance_id: str, generation: int) -> bool:
        with self._lock:
            record = self._require_generation(instance_id, generation)
            if record.state is not ModelInstanceState.DRAINING:
                return False
            record.cleanup_deadline_at_ms = None
            record.cleanup_timeout_reported = False
            self._transition(record, ModelInstanceState.READY)
            return True

    def mark_failed(
        self,
        instance_id: str,
        generation: int,
        *,
        reason: str,
    ) -> ModelInstance:
        if not reason:
            raise ValueError("model failure reason is required")
        with self._lock:
            record = self._require_generation(instance_id, generation)
            if record.state in {ModelInstanceState.STOPPED, ModelInstanceState.STOPPING}:
                return self._snapshot(record)
            if record.state is ModelInstanceState.FAILED:
                return self._snapshot(record)
            record.failure_reason = reason
            self._transition(
                record,
                ModelInstanceState.FAILED,
                {"reason": reason},
            )
            record.cleanup_deadline_at_ms = (
                self.clock.monotonic_ms() + record.spec.drain_timeout_ms
            )
            record.cleanup_timeout_reported = False
            self._emit(
                record,
                "model_instance_unhealthy",
                {"reason": reason},
            )
            return self._snapshot(record)

    def check_cleanup_timeout(
        self,
        instance_id: str,
        generation: int,
    ) -> bool:
        with self._lock:
            record = self._require_generation(instance_id, generation)
            if record.state not in {
                ModelInstanceState.DRAINING,
                ModelInstanceState.FAILED,
            }:
                return False
            if not (record.route_occupancy or record.actual_request_inflight):
                return False
            deadline = record.cleanup_deadline_at_ms
            if (
                deadline is None
                or self.clock.monotonic_ms() < deadline
                or record.cleanup_timeout_reported
            ):
                return False
            record.cleanup_timeout_reported = True
            self._emit(
                record,
                "model_drain_timed_out",
                {
                    "deadline_at_ms": deadline,
                    "route_occupancy": record.route_occupancy,
                    "actual_request_inflight": record.actual_request_inflight,
                },
            )
            return True

    async def stop_if_drained(
        self, instance_id: str, generation: int
    ) -> ModelInstance:
        with self._lock:
            record = self._require_generation(instance_id, generation)
            if record.state is ModelInstanceState.STOPPED:
                return self._snapshot(record)
            if record.state not in {
                ModelInstanceState.DRAINING,
                ModelInstanceState.FAILED,
            }:
                return self._snapshot(record)
            if record.route_occupancy or record.actual_request_inflight:
                return self._snapshot(record)
            self._transition(record, ModelInstanceState.STOPPING)
            handle = record.service_handle
            lease = record.placement_lease
            port_lease = record.port_lease
            timeout_ms = record.spec.drain_timeout_ms
        try:
            if handle is not None:
                result = await asyncio.wait_for(
                    self.service_backend.stop(handle, timeout_ms=timeout_ms),
                    timeout=timeout_ms / 1_000,
                )
                if not (
                    result.process_exited
                    and result.port_released
                    and result.hbm_recovered
                ):
                    raise RuntimeError(
                        "service stop did not confirm process, port and HBM recovery"
                    )
            if port_lease is not None:
                if not await self.port_leases.release(port_lease):
                    raise RuntimeError("PortLease authority could not confirm release")
                with self._lock:
                    current = self._require_generation(instance_id, generation)
                    if current.port_lease == port_lease:
                        current.port_lease = None
            if lease is not None:
                self.placement.release_lease(
                    lease.lease_id,
                    now_ms=self.clock.monotonic_ms(),
                    reason="model_instance_stopped",
                )
            with self._lock:
                current = self._require_generation(instance_id, generation)
                current.placement_lease = None
                current.service_handle = None
                current.cleanup_deadline_at_ms = None
                self._transition(
                    current,
                    ModelInstanceState.STOPPED,
                    {
                        "process_exited": True,
                        "port_released": True,
                        "hbm_recovered": True,
                    },
                )
                return self._snapshot(current)
        except Exception as exc:
            with self._lock:
                current = self._require_generation(instance_id, generation)
                if current.state is ModelInstanceState.STOPPED:
                    return self._snapshot(current)
                current.failure_reason = f"{type(exc).__name__}: {exc}"
                self._transition(current, ModelInstanceState.FAILED)
                self._emit(
                    current,
                    "model_resource_release_blocked",
                    {"reason": current.failure_reason},
                )
                return self._snapshot(current)

    def reserve_route(self, instance_id: str, generation: int) -> None:
        with self._lock:
            record = self._require_generation(instance_id, generation)
            if record.state is not ModelInstanceState.READY:
                raise StateTransitionError("model instance is not ready")
            if record.route_occupancy >= record.spec.request_capacity:
                raise StateTransitionError("model route capacity is full")
            record.route_occupancy += 1
            record.last_used_at_ms = self.clock.monotonic_ms()
            self._emit(
                record,
                "model_route_capacity_changed",
                {"reason": "route_reserved"},
            )

    def release_route(self, instance_id: str, generation: int) -> None:
        with self._lock:
            record = self._require_generation(instance_id, generation)
            if record.route_occupancy < 1:
                raise StateTransitionError("model route occupancy underflow")
            record.route_occupancy -= 1
            record.last_used_at_ms = self.clock.monotonic_ms()
            self._emit(
                record,
                "model_route_capacity_changed",
                {"reason": "route_released"},
            )

    def request_started(self, instance_id: str, generation: int) -> None:
        with self._lock:
            record = self._require_generation(instance_id, generation)
            if record.state not in {
                ModelInstanceState.READY,
                ModelInstanceState.DRAINING,
            }:
                raise StateTransitionError("model instance cannot accept requests")
            if record.route_occupancy < 1:
                raise StateTransitionError("model request requires route occupancy")
            record.actual_request_inflight += 1
            record.last_used_at_ms = self.clock.monotonic_ms()
            self._emit(
                record,
                "model_request_inflight_changed",
                {"reason": "request_started"},
            )

    def request_finished(self, instance_id: str, generation: int) -> None:
        with self._lock:
            record = self._require_generation(instance_id, generation)
            if record.actual_request_inflight < 1:
                raise StateTransitionError("actual request inflight underflow")
            record.actual_request_inflight -= 1
            record.last_used_at_ms = self.clock.monotonic_ms()
            self._emit(
                record,
                "model_request_inflight_changed",
                {"reason": "request_finished"},
            )

    def snapshot(self, instance_id: str) -> ModelInstance:
        with self._lock:
            return self._snapshot(self._require(instance_id))

    def instances(
        self,
        *,
        model_id: str | None = None,
        states: frozenset[ModelInstanceState] | None = None,
    ) -> tuple[ModelInstance, ...]:
        with self._lock:
            return tuple(
                self._snapshot(record)
                for record in sorted(
                    self._records.values(), key=lambda item: item.instance_id
                )
                if (model_id is None or record.spec.model_id == model_id)
                and (states is None or record.state in states)
            )

    def recovery_records(self) -> tuple[ModelInstanceRecoveryRecord, ...]:
        with self._lock:
            return tuple(
                ModelInstanceRecoveryRecord(
                    instance_id=record.instance_id,
                    spec=record.spec,
                    generation=record.generation,
                    state=record.state,
                    created_at_ms=record.created_at_ms,
                    state_changed_at_ms=record.state_changed_at_ms,
                    ready_at_ms=record.ready_at_ms,
                    placement_lease=record.placement_lease,
                    port_lease=record.port_lease,
                    service_handle=record.service_handle,
                    route_occupancy=record.route_occupancy,
                    actual_request_inflight=record.actual_request_inflight,
                    last_used_at_ms=record.last_used_at_ms,
                    failure_reason=record.failure_reason,
                    cleanup_deadline_at_ms=record.cleanup_deadline_at_ms,
                    cleanup_timeout_reported=record.cleanup_timeout_reported,
                )
                for _, record in sorted(self._records.items())
            )

    def restore_recovery(
        self,
        records: tuple[ModelInstanceRecoveryRecord, ...],
    ) -> None:
        """Restore ownership while fencing every non-stopped old instance."""

        now = self.clock.monotonic_ms()
        with self._lock:
            if self._records:
                raise StateTransitionError("model recovery requires an empty manager")
            for item in records:
                if self.catalog.get(item.spec.model_id) != item.spec:
                    raise StateTransitionError(
                        f"recovered ModelSpec changed: {item.spec.model_id}"
                    )
                state = item.state
                failure_reason = item.failure_reason
                cleanup_deadline = item.cleanup_deadline_at_ms
                if state is not ModelInstanceState.STOPPED:
                    state = ModelInstanceState.FAILED
                    failure_reason = "controller_generation_changed"
                    cleanup_deadline = now + item.spec.drain_timeout_ms
                self._records[item.instance_id] = _InstanceRecord(
                    instance_id=item.instance_id,
                    spec=item.spec,
                    generation=item.generation,
                    state=state,
                    created_at_ms=item.created_at_ms,
                    state_changed_at_ms=now,
                    ready_at_ms=item.ready_at_ms,
                    placement_lease=item.placement_lease,
                    port_lease=item.port_lease,
                    service_handle=item.service_handle,
                    route_occupancy=0,
                    actual_request_inflight=0,
                    last_used_at_ms=item.last_used_at_ms,
                    failure_reason=failure_reason,
                    cleanup_deadline_at_ms=cleanup_deadline,
                    cleanup_timeout_reported=False,
                )

    def ready_instances(
        self,
        model_id: str,
        catalog_revision: str,
    ) -> tuple[ModelInstance, ...]:
        with self._lock:
            identities = self._ready_index.get(
                (model_id, catalog_revision),
                set(),
            )
            return tuple(
                self._snapshot(self._records[instance_id])
                for instance_id, generation in sorted(identities)
                if self._records[instance_id].generation == generation
                and self._records[instance_id].state is ModelInstanceState.READY
            )

    def spec_for_instance(self, instance_id: str) -> ModelSpec:
        with self._lock:
            return self._require(instance_id).spec

    async def close(self) -> None:
        with self._lock:
            for record in self._records.values():
                if record.state in {
                    ModelInstanceState.REQUESTED,
                    ModelInstanceState.RESERVING,
                }:
                    record.failure_reason = "inference_coordinator_closed"
                    self._transition(record, ModelInstanceState.FAILED)
        for instance in self.instances():
            if instance.state is ModelInstanceState.READY:
                self.begin_drain(instance.instance_id, instance.generation)
        for instance in self.instances():
            await self.stop_if_drained(instance.instance_id, instance.generation)

    async def _cleanup_failed(self, instance_id: str, generation: int) -> None:
        await self.stop_if_drained(instance_id, generation)

    @staticmethod
    def _validate_service_handle(
        *,
        instance_id: str,
        generation: int,
        lease: PlacementLease,
        request_endpoint_id: str,
        request_port_lease_id: str,
        request_port: int,
        handle: ServiceHandle,
    ) -> None:
        expected = (
            instance_id,
            generation,
            request_endpoint_id,
            lease.node_id,
            lease.boot_id,
            lease.npu_device_id,
            request_port_lease_id,
            request_port,
        )
        actual = (
            handle.instance_id,
            handle.generation,
            handle.endpoint_id,
            handle.node_id,
            handle.boot_id,
            handle.npu_device_id,
            handle.port_lease_id,
            handle.port,
        )
        if actual != expected:
            raise RuntimeError("ServiceHandle identity does not match launch request")

    @staticmethod
    def _validate_probe(
        spec: ModelSpec, lease: PlacementLease, probe: EngineProbe
    ) -> None:
        expected = (
            True,
            spec.model_id,
            spec.artifact_revision,
            spec.environment_fingerprint,
            spec.dtype,
            spec.quantization,
            lease.npu_device_id,
            spec.request_capacity,
        )
        actual = (
            probe.process_alive,
            probe.model_id,
            probe.artifact_revision,
            probe.environment_fingerprint,
            probe.dtype,
            probe.quantization,
            probe.physical_device_id,
            probe.request_capacity,
        )
        if actual != expected:
            raise RuntimeError("model probe identity or capacity mismatch")
        if not spec.weight_hbm_mb <= probe.process_hbm_mb <= spec.instance_hbm_mb:
            raise RuntimeError("model process HBM is outside its Lease budget")

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.001, deadline - monotonic())

    def _transition(
        self,
        record: _InstanceRecord,
        target: ModelInstanceState,
        payload: dict[str, object] | None = None,
    ) -> None:
        if record.state is target:
            return
        allowed = {
            ModelInstanceState.REQUESTED: {
                ModelInstanceState.RESERVING,
                ModelInstanceState.FAILED,
            },
            ModelInstanceState.RESERVING: {
                ModelInstanceState.STARTING,
                ModelInstanceState.FAILED,
            },
            ModelInstanceState.STARTING: {
                ModelInstanceState.WARMING,
                ModelInstanceState.FAILED,
            },
            ModelInstanceState.WARMING: {
                ModelInstanceState.READY,
                ModelInstanceState.FAILED,
            },
            ModelInstanceState.READY: {
                ModelInstanceState.DRAINING,
                ModelInstanceState.FAILED,
            },
            ModelInstanceState.DRAINING: {
                ModelInstanceState.READY,
                ModelInstanceState.STOPPING,
                ModelInstanceState.FAILED,
            },
            ModelInstanceState.FAILED: {ModelInstanceState.STOPPING},
            ModelInstanceState.STOPPING: {
                ModelInstanceState.STOPPED,
                ModelInstanceState.FAILED,
            },
            ModelInstanceState.STOPPED: set(),
        }
        if target not in allowed[record.state]:
            raise StateTransitionError(
                f"invalid model instance transition {record.state.value}->{target.value}"
            )
        ready_key = (record.spec.model_id, record.spec.catalog_revision)
        identity = (record.instance_id, record.generation)
        if record.state is ModelInstanceState.READY:
            self._ready_index.get(ready_key, set()).discard(identity)
        record.state = target
        if target is ModelInstanceState.READY:
            self._ready_index.setdefault(ready_key, set()).add(identity)
        record.state_changed_at_ms = self.clock.monotonic_ms()
        self._emit(record, f"model_instance_{target.value}", payload)

    def _emit(
        self,
        record: _InstanceRecord,
        event_type: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        if self.event_sink is None:
            return
        lease = record.placement_lease
        port = record.port_lease
        handle = record.service_handle
        event_payload: dict[str, object] = {
            "state": record.state.value,
            "catalog_revision": record.spec.catalog_revision,
            "placement_lease_id": None if lease is None else lease.lease_id,
            "node_id": None if lease is None else lease.node_id,
            "boot_id": None if lease is None else lease.boot_id,
            "npu_device_id": None if lease is None else lease.npu_device_id,
            "port_lease_id": None if port is None else port.port_lease_id,
            "port": None if port is None else port.port,
            "service_handle_id": (
                None if handle is None else handle.service_handle_id
            ),
            "route_occupancy": record.route_occupancy,
            "actual_request_inflight": record.actual_request_inflight,
        }
        if payload is not None:
            event_payload.update(payload)
        self.event_sink(
            ModelControlEvent(
                event_type=event_type,
                occurred_at_ms=self.clock.monotonic_ms(),
                model_id=record.spec.model_id,
                instance_id=record.instance_id,
                instance_generation=record.generation,
                payload=self._event_payload(event_payload),
            )
        )

    @staticmethod
    def _event_payload(payload: dict[str, object] | None) -> FrozenMap:
        frozen = freeze_canonical(payload or {})
        assert isinstance(frozen, FrozenMap)
        return frozen

    def _require(self, instance_id: str) -> _InstanceRecord:
        try:
            return self._records[instance_id]
        except KeyError as exc:
            raise KeyError(f"unknown model instance: {instance_id}") from exc

    def _require_generation(
        self, instance_id: str, generation: int
    ) -> _InstanceRecord:
        record = self._require(instance_id)
        if record.generation != generation:
            raise StateTransitionError("model instance generation is stale")
        return record

    @staticmethod
    def _snapshot(record: _InstanceRecord) -> ModelInstance:
        lease = record.placement_lease
        handle = record.service_handle
        return ModelInstance(
            instance_id=record.instance_id,
            model_id=record.spec.model_id,
            catalog_revision=record.spec.catalog_revision,
            state=record.state,
            placement_lease_id=None if lease is None else lease.lease_id,
            service_handle_id=None if handle is None else handle.service_handle_id,
            node_id=None if lease is None else lease.node_id,
            boot_id=None if lease is None else lease.boot_id,
            npu_device_id=None if lease is None else lease.npu_device_id,
            endpoint_id=None if handle is None else handle.endpoint_id,
            generation=record.generation,
            created_at_ms=record.created_at_ms,
            ready_at_ms=record.ready_at_ms,
            state_changed_at_ms=record.state_changed_at_ms,
            route_capacity=record.spec.request_capacity,
            route_occupancy=record.route_occupancy,
            actual_request_inflight=record.actual_request_inflight,
            last_used_at_ms=record.last_used_at_ms,
            failure_reason=record.failure_reason,
        )
