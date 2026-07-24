"""C11 composition consumed by SchedulerCore and service client Workers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import inspect
from threading import RLock

from ascend_maze.compiler.ir import CompiledWorkflow
from ascend_maze.contracts.runtime import ModelRouteLease
from ascend_maze.core.clock import Clock, SystemClock
from ascend_maze.core.canonical import FrozenMap, freeze_canonical
from ascend_maze.inference.catalog import ModelCatalog
from ascend_maze.inference.context import AttemptInferenceSession
from ascend_maze.inference.contracts import (
    AttemptInferenceSummary,
    InferenceRequestRecord,
    InferenceWorkerConfig,
    ModelControlEvent,
    ModelInstance,
    ModelInstanceState,
    ModelRouteAcquireResult,
    ModelRouteLeaseSnapshot,
    PortLeaseManager,
    ServiceProcessBackend,
)
from ascend_maze.inference.instance_manager import ModelInstanceManager
from ascend_maze.inference.replica_controller import ReplicaController
from ascend_maze.inference.router import InferenceRouter
from ascend_maze.placement import PlacementManager


class InferenceCoordinator:
    def __init__(
        self,
        *,
        catalog: ModelCatalog,
        placement: PlacementManager,
        service_backend: ServiceProcessBackend,
        clock: Clock | None = None,
        affinity_ttl_ms: int = 300_000,
        affinity_capacity: int = 10_000,
        port_leases: PortLeaseManager | None = None,
        reconcile_interval_ms: int = 100,
    ) -> None:
        self.catalog = catalog
        self.service_backend = service_backend
        self.clock = clock or SystemClock()
        self._events: list[ModelControlEvent] = []
        self._request_records: dict[str, list[InferenceRequestRecord]] = {}
        self._sessions: dict[str, AttemptInferenceSession] = {}
        self._worker_configs: dict[str, InferenceWorkerConfig] = {}
        self._worker_request_started: dict[str, tuple[int, int]] = {}
        self._worker_summaries: dict[str, AttemptInferenceSummary] = {}
        self._capacity_sink: Callable[[str, str | None], object] | None = None
        self._state_change_sink: Callable[[], object] | None = None
        self._route_failure_sink: Callable[[ModelRouteLease, str], object] | None = None
        self._lock = RLock()
        self._model_changed = asyncio.Event()
        self.instances = ModelInstanceManager(
            catalog=catalog,
            placement=placement,
            service_backend=service_backend,
            event_sink=self._record_event,
            clock=self.clock,
            port_leases=port_leases,
        )
        self.router = InferenceRouter(
            instances=self.instances,
            event_sink=self._record_event,
            clock=self.clock,
            affinity_ttl_ms=affinity_ttl_ms,
            affinity_capacity=affinity_capacity,
        )
        self.replicas = ReplicaController(
            catalog=catalog,
            instances=self.instances,
            router=self.router,
            event_sink=self._record_event,
            clock=self.clock,
            reconcile_interval_ms=reconcile_interval_ms,
        )

    async def start(self) -> None:
        await self.replicas.start()

    def set_capacity_sink(
        self,
        sink: Callable[[str, str | None], object] | None,
    ) -> None:
        self._capacity_sink = sink

    def set_state_change_sink(
        self,
        sink: Callable[[], object] | None,
    ) -> None:
        self._state_change_sink = sink

    def set_route_failure_sink(
        self,
        sink: Callable[[ModelRouteLease, str], object] | None,
    ) -> None:
        self._route_failure_sink = sink

    def report_instance_failure(
        self,
        instance_id: str,
        generation: int,
        *,
        reason: str,
    ) -> tuple[ModelRouteLease, ...]:
        self.instances.mark_failed(
            instance_id,
            generation,
            reason=reason,
        )
        affected = self.router.invalidate_instance(
            instance_id,
            generation,
            reason=reason,
        )
        sink = self._route_failure_sink
        if sink is not None:
            for lease in affected:
                sink(lease, reason)
        self.replicas.wake()
        self._notify_capacity("model_instance_unhealthy", instance_id=instance_id)
        return affected

    def report_process_exited(
        self,
        instance_id: str,
        generation: int,
        *,
        reason: str,
    ) -> tuple[ModelRouteLease, ...]:
        instance = self.instances.snapshot(instance_id)
        if instance.generation != generation:
            raise RuntimeError("model process exit generation is stale")
        payload = freeze_canonical(
            {
                "reason": reason,
                "service_handle_id": instance.service_handle_id,
                "placement_lease_id": instance.placement_lease_id,
                "node_id": instance.node_id,
                "boot_id": instance.boot_id,
                "npu_device_id": instance.npu_device_id,
            }
        )
        assert isinstance(payload, FrozenMap)
        self._record_event(
            ModelControlEvent(
                event_type="model_process_exited",
                occurred_at_ms=self.clock.monotonic_ms(),
                model_id=instance.model_id,
                instance_id=instance_id,
                instance_generation=generation,
                payload=payload,
            )
        )
        return self.report_instance_failure(
            instance_id,
            generation,
            reason=reason,
        )

    def report_node_generation_lost(
        self,
        node_id: str,
        boot_id: str,
        *,
        reason: str = "node_generation_lost",
    ) -> tuple[ModelRouteLease, ...]:
        affected: list[ModelRouteLease] = []
        for instance in self.model_instances():
            if (
                instance.node_id == node_id
                and instance.boot_id == boot_id
                and instance.state
                not in {ModelInstanceState.STOPPING, ModelInstanceState.STOPPED}
            ):
                affected.extend(
                    self.report_instance_failure(
                        instance.instance_id,
                        instance.generation,
                        reason=reason,
                    )
                )
        return tuple(affected)

    def validate_workflow(self, compiled: CompiledWorkflow) -> None:
        self.catalog.validate_workflow(compiled)

    def register_demand(self, *, run_id: str, task_id: str, model_id: str) -> None:
        self.replicas.register_demand(
            run_id=run_id,
            task_id=task_id,
            model_id=model_id,
        )

    async def acquire_route(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        model_id: str,
        session_key_hash: str | None,
        dispatch_deadline_ms: int,
    ) -> ModelRouteAcquireResult:
        await self.replicas.reconcile()
        return self.router.acquire(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            model_id=model_id,
            session_key_hash=session_key_hash,
            dispatch_deadline_ms=dispatch_deadline_ms,
        )

    def activate_route(self, route_lease_id: str) -> bool:
        return self.router.activate(route_lease_id)

    async def release_route(
        self,
        lease: ModelRouteLease,
        *,
        reason: str,
    ) -> bool:
        released = self.router.release(
            lease.route_lease_id,
            run_id=lease.run_id,
            task_id=lease.task_id,
            attempt=lease.attempt,
            instance_generation=lease.instance_generation,
            reason=reason,
        )
        if released:
            self.replicas.remove_demand(
                run_id=lease.run_id,
                task_id=lease.task_id,
                model_id=lease.model_id,
            )
            try:
                await self.replicas.reconcile()
            except Exception as exc:
                payload = freeze_canonical(
                    {
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                assert isinstance(payload, FrozenMap)
                self._record_event(
                    ModelControlEvent(
                        event_type="model_reconcile_failed",
                        occurred_at_ms=self.clock.monotonic_ms(),
                        model_id=lease.model_id,
                        instance_id=lease.instance_id,
                        instance_generation=lease.instance_generation,
                        route_lease_id=lease.route_lease_id,
                        run_id=lease.run_id,
                        task_id=lease.task_id,
                        attempt=lease.attempt,
                        payload=payload,
                    )
                )
            self._notify_capacity("model_route_released", lease.model_id)
        return released

    def abandon_route(self, lease: ModelRouteLease, *, reason: str) -> bool:
        return self.router.abandon_reserved(
            lease.route_lease_id,
            reason=reason,
        )

    def create_attempt_session(self, lease: ModelRouteLease) -> AttemptInferenceSession:
        with self._lock:
            existing = self._sessions.get(lease.route_lease_id)
            if existing is not None:
                return existing
            adapter = self.catalog.adapter(lease.model_id)
            instance = self.instances.snapshot(lease.instance_id)
            if (
                instance.generation != lease.instance_generation
                or instance.placement_lease_id is None
            ):
                raise RuntimeError("ModelRouteLease instance resources are stale")
            session = AttemptInferenceSession(
                lease=lease,
                router=self.router,
                adapter=adapter,
                instance_placement_lease_id=instance.placement_lease_id,
                record_sink=self._record_request,
                clock=self.clock,
            )
            self._sessions[lease.route_lease_id] = session
            return session

    def worker_config(self, lease: ModelRouteLease) -> InferenceWorkerConfig:
        snapshot = self.router.snapshot(lease.route_lease_id)
        if snapshot.lease != lease:
            raise RuntimeError("ModelRouteLease payload does not match C11 authority")
        instance = self.instances.snapshot(lease.instance_id)
        if (
            instance.generation != lease.instance_generation
            or instance.placement_lease_id is None
            or instance.npu_device_id is None
        ):
            raise RuntimeError("ModelRouteLease instance resources are stale")
        adapter = self.catalog.adapter(lease.model_id)
        config = adapter.worker_config(
            self.catalog.get(lease.model_id),
            instance_placement_lease_id=instance.placement_lease_id,
            npu_device_id=instance.npu_device_id,
        )
        if config.adapter_name != lease.adapter_name:
            raise RuntimeError("Worker adapter does not match ModelRouteLease")
        with self._lock:
            existing = self._worker_configs.get(lease.route_lease_id)
            if existing is not None and existing != config:
                raise RuntimeError("Worker inference config changed within an Attempt")
            self._worker_configs[lease.route_lease_id] = config
        return config

    def worker_request_started(
        self,
        lease: ModelRouteLease,
        *,
        call_index: int,
        started_at_ms: int,
    ) -> None:
        if call_index < 1 or started_at_ms < 0:
            raise RuntimeError("Worker inference request identity is invalid")
        self._require_worker_route(lease)
        with self._lock:
            route_id = lease.route_lease_id
            if route_id in self._worker_request_started:
                raise RuntimeError("Worker route already has a request inflight")
            expected = len(self._request_records.get(route_id, ())) + 1
            if call_index != expected:
                raise RuntimeError("Worker inference call_index is not monotonic")
            self.router.request_started(route_id)
            self._worker_request_started[route_id] = (call_index, started_at_ms)

    def worker_request_finished(
        self,
        lease: ModelRouteLease,
        record: InferenceRequestRecord,
    ) -> None:
        self._require_worker_route(lease)
        route_id = lease.route_lease_id
        if (
            record.route_lease_id != route_id
            or record.run_id != lease.run_id
            or record.task_id != lease.task_id
            or record.attempt != lease.attempt
            or record.model_id != lease.model_id
            or record.instance_id != lease.instance_id
            or record.instance_generation != lease.instance_generation
        ):
            raise RuntimeError("Worker InferenceRequestRecord identity is stale")
        with self._lock:
            started = self._worker_request_started.get(route_id)
            if started is None or started[0] != record.call_index:
                raise RuntimeError("Worker inference finish does not match its start")
            config = self._worker_configs.get(route_id)
            if (
                config is None
                or record.instance_placement_lease_id
                != config.instance_placement_lease_id
            ):
                raise RuntimeError("Worker inference instance Placement is stale")
            self.router.request_finished(route_id)
            del self._worker_request_started[route_id]
            self._request_records.setdefault(route_id, []).append(record)

    def worker_attempt_finished(
        self,
        lease: ModelRouteLease,
        summary: AttemptInferenceSummary,
    ) -> None:
        self._require_worker_route(lease)
        route_id = lease.route_lease_id
        with self._lock:
            if summary.route_lease_id != route_id:
                raise RuntimeError("Worker inference summary route is stale")
            if route_id in self._worker_request_started or summary.request_inflight:
                raise RuntimeError("Worker inference request remained inflight")
            records = self._request_records.get(route_id, ())
            if summary.request_count != len(records):
                raise RuntimeError("Worker inference summary count is incomplete")
            if not summary.context_cleared:
                raise RuntimeError("Worker inference context was not cleared")
            existing = self._worker_summaries.get(route_id)
            if existing is not None and existing != summary:
                raise RuntimeError("Worker inference summary payload conflict")
            self._worker_summaries[route_id] = summary

    def abort_worker_attempt(
        self,
        lease: ModelRouteLease,
        *,
        error_code: str,
    ) -> AttemptInferenceSummary:
        route_id = lease.route_lease_id
        with self._lock:
            active = self._worker_request_started.pop(route_id, None)
            if active is not None:
                call_index, started_at_ms = active
                try:
                    self.router.request_finished(route_id)
                except Exception:
                    pass
                config = self._worker_configs.get(route_id)
                if config is not None:
                    self._request_records.setdefault(route_id, []).append(
                        InferenceRequestRecord(
                            route_lease_id=route_id,
                            call_index=call_index,
                            run_id=lease.run_id,
                            task_id=lease.task_id,
                            attempt=lease.attempt,
                            model_id=lease.model_id,
                            instance_id=lease.instance_id,
                            instance_generation=lease.instance_generation,
                            instance_placement_lease_id=(
                                config.instance_placement_lease_id
                            ),
                            started_at_ms=started_at_ms,
                            duration_ms=max(
                                0, self.clock.monotonic_ms() - started_at_ms
                            ),
                            status="failed",
                            input_tokens=None,
                            output_tokens=None,
                            engine_queue_depth=None,
                            prefix_cache_hit=None,
                            ttft_ms=None,
                            error_code=error_code,
                        )
                    )
            summary = AttemptInferenceSummary(
                route_lease_id=route_id,
                request_count=len(self._request_records.get(route_id, ())),
                request_inflight=False,
                context_cleared=True,
            )
            self._worker_summaries[route_id] = summary
            return summary

    def attempt_summary(self, route_lease_id: str) -> AttemptInferenceSummary | None:
        with self._lock:
            session = self._sessions.get(route_lease_id)
            if session is not None:
                return session.summary()
            return self._worker_summaries.get(route_lease_id)

    def request_records(
        self, route_lease_id: str | None = None
    ) -> tuple[InferenceRequestRecord, ...]:
        with self._lock:
            if route_lease_id is not None:
                return tuple(self._request_records.get(route_lease_id, ()))
            return tuple(
                record
                for route_id in sorted(self._request_records)
                for record in self._request_records[route_id]
            )

    def route_snapshot(self, route_lease_id: str) -> ModelRouteLeaseSnapshot:
        return self.router.snapshot(route_lease_id)

    def model_instances(self, model_id: str | None = None) -> tuple[ModelInstance, ...]:
        return self.instances.instances(model_id=model_id)

    async def wait_ready(
        self,
        model_id: str,
        *,
        replicas: int = 1,
        timeout_seconds: float | None = None,
    ) -> tuple[ModelInstance, ...]:
        if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 1:
            raise ValueError("replicas must be positive")
        spec = self.catalog.get(model_id)
        demands = self.replicas.demands(model_id)
        desired = max(
            spec.min_replicas,
            (len(demands) + spec.request_capacity - 1) // spec.request_capacity,
        )
        if replicas > desired:
            raise RuntimeError(
                "requested ready replicas exceed min_replicas and current demand"
            )
        loop = asyncio.get_running_loop()
        deadline = None if timeout_seconds is None else loop.time() + timeout_seconds
        while True:
            ready = tuple(
                item
                for item in self.model_instances(model_id)
                if item.state is ModelInstanceState.READY
            )
            if len(ready) >= replicas:
                return ready
            with self._lock:
                changed = self._model_changed
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("model readiness deadline expired")
            await asyncio.wait_for(changed.wait(), timeout=remaining)

    def events(self) -> tuple[ModelControlEvent, ...]:
        with self._lock:
            return tuple(self._events)

    async def reconcile(self) -> None:
        await self.replicas.reconcile()

    def begin_node_drain(self, node_id: str, boot_id: str) -> None:
        self.replicas.begin_node_drain(node_id, boot_id)
        for instance in self.model_instances():
            if instance.node_id != node_id or instance.boot_id != boot_id:
                continue
            self.router.forget_instance_affinity(
                instance.instance_id, instance.generation
            )
            self.instances.begin_drain(instance.instance_id, instance.generation)
        self.replicas.wake()

    async def advance_node_drain(self, node_id: str, boot_id: str) -> None:
        self.begin_node_drain(node_id, boot_id)
        await self.replicas.reconcile()
        for instance in self.model_instances():
            if instance.node_id != node_id or instance.boot_id != boot_id:
                continue
            await self.instances.stop_if_drained(
                instance.instance_id, instance.generation
            )

    def end_node_drain(self, node_id: str, boot_id: str) -> None:
        self.replicas.end_node_drain(node_id, boot_id)

    def active_routes_for_node(
        self, node_id: str, boot_id: str
    ) -> tuple[ModelRouteLease, ...]:
        instance_keys = {
            (instance.instance_id, instance.generation)
            for instance in self.model_instances()
            if instance.node_id == node_id and instance.boot_id == boot_id
        }
        return tuple(
            lease
            for lease in self.router.active_leases()
            if (lease.instance_id, lease.instance_generation) in instance_keys
        )

    async def close(self) -> None:
        await self.replicas.close()
        if self.router.active_count() != 0:
            raise RuntimeError("cannot close C11 while RouteLeases are active")
        await self.instances.close()
        for adapter in self.catalog.adapters():
            close = getattr(adapter, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
        close_backend = getattr(self.service_backend, "close", None)
        if callable(close_backend):
            result = close_backend()
            if inspect.isawaitable(result):
                await result

    async def abandon(self) -> None:
        await self.replicas.abandon()
        self._capacity_sink = None
        self._route_failure_sink = None
        self._state_change_sink = None

    def destroy_run(self, run_id: str) -> int:
        self.replicas.remove_run(run_id)
        with self._lock:
            route_ids = {
                route_id
                for route_id, session in self._sessions.items()
                if session.lease.run_id == run_id
            }
            route_ids.update(
                route_id
                for route_id in self._worker_configs
                if self.router.snapshot(route_id).lease.run_id == run_id
            )
            route_ids.update(
                route_id
                for route_id in self._worker_summaries
                if self.router.snapshot(route_id).lease.run_id == run_id
            )
            route_ids.update(
                route_id
                for route_id, records in self._request_records.items()
                if records and records[0].run_id == run_id
            )
            for route_id in route_ids:
                self._sessions.pop(route_id, None)
                self._worker_configs.pop(route_id, None)
                self._worker_request_started.pop(route_id, None)
                self._worker_summaries.pop(route_id, None)
                self._request_records.pop(route_id, None)
            self._events = [event for event in self._events if event.run_id != run_id]
        return self.router.destroy_run(run_id)

    def _require_worker_route(self, lease: ModelRouteLease) -> None:
        snapshot = self.router.snapshot(lease.route_lease_id)
        if snapshot.lease != lease:
            raise RuntimeError("Worker ModelRouteLease payload is stale")
        with self._lock:
            if lease.route_lease_id not in self._worker_configs:
                raise RuntimeError("Worker inference config is not registered")

    def _record_event(self, event: ModelControlEvent) -> None:
        with self._lock:
            self._events.append(event)
            changed = self._model_changed
            self._model_changed = asyncio.Event()
            changed.set()
        if self._state_change_sink is not None:
            self._state_change_sink()
        if event.event_type in {
            "model_instance_ready",
            "model_instance_stopped",
        }:
            self._notify_capacity(event.event_type, event.model_id)

    def _record_request(self, record: InferenceRequestRecord) -> None:
        with self._lock:
            self._request_records.setdefault(record.route_lease_id, []).append(record)

    def _notify_capacity(
        self,
        reason: str,
        model_id: str | None = None,
        *,
        instance_id: str | None = None,
    ) -> None:
        if model_id is None and instance_id is not None:
            model_id = self.instances.spec_for_instance(instance_id).model_id
        sink = self._capacity_sink
        if sink is not None:
            sink(reason, model_id)
