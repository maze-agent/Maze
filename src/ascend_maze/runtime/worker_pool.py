"""C10 Standby Worker pool with C6-backed Host reservations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any, Callable, Protocol

from ascend_maze.contracts.resources import ExecutionTarget, PlacementLease
from ascend_maze.contracts.runtime import RuntimeNodeBinding
from ascend_maze.contracts.worker import (
    StandbyWorkerDescriptor,
    StandbyWorkerState,
    StandbyWarmupReport,
    WorkerLease,
    WorkerPoolConfig,
    WorkerPoolProfileConfig,
    WorkerProfile,
)
from ascend_maze.core.errors import StateTransitionError
from ascend_maze.core.identifiers import new_id
from ascend_maze.core.time import monotonic_time_ms
from ascend_maze.placement import (
    PlacementManager,
    StandbyReservationStatus,
)
from ascend_maze.runtime.ray_node_registry import RayNodeRegistry, RuntimeNodeStatus


@dataclass(frozen=True, slots=True)
class WorkerPoolEvent:
    event_type: str
    occurred_at_ms: int
    node_id: str
    boot_id: str
    profile: WorkerProfile
    worker_id: str | None = None
    reason: str | None = None
    worker_lease_id: str | None = None
    placement_lease_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    attempt: int | None = None
    worker_acquire_ms: int | None = None
    cold_start_ms: int | None = None
    host_warmup_ms: int | None = None


@dataclass(frozen=True, slots=True)
class WorkerLeaseSnapshot:
    lease: WorkerLease
    placement_lease_id: str
    released: bool
    releasing: bool
    disposition: str | None


@dataclass(frozen=True, slots=True)
class WorkerPoolSnapshot:
    mode: str
    config_generation: int
    workers: tuple[StandbyWorkerDescriptor, ...]
    worker_leases: tuple[WorkerLeaseSnapshot, ...]
    active_worker_lease_count: int
    standby_hits: int
    cold_starts: int
    replenish_failures: int
    reservation_failures: int
    sanitize_failures: int
    termination_failures: int


class WorkerEndpointFactory(Protocol):
    async def start(
        self,
        *,
        worker_id: str,
        worker_generation: int,
        binding: RuntimeNodeBinding,
        config: WorkerPoolProfileConfig,
        deadline_ms: int,
    ) -> tuple[Any, StandbyWarmupReport]: ...

    def submit(self, endpoint: Any, kwargs: dict[str, object]) -> Any: ...

    async def terminate(
        self,
        endpoint: Any,
        *,
        force: bool = False,
        timeout_ms: int = 10_000,
    ) -> None: ...


@dataclass(slots=True)
class _PooledWorker:
    descriptor: StandbyWorkerDescriptor
    endpoint: Any | None
    retire_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class _PoolLeaseRecord:
    lease: WorkerLease
    placement_lease: PlacementLease
    released: bool = False
    releasing: bool = False
    disposition: str | None = None


class StandbyWorkerBroker:
    """Maintain one local Worker index and one background reconciler."""

    def __init__(
        self,
        *,
        node_registry: RayNodeRegistry,
        placement: PlacementManager,
        environment_fingerprint: str,
        config: WorkerPoolConfig,
        endpoint_factory: WorkerEndpointFactory,
        event_sink: Callable[[WorkerPoolEvent], None] | None = None,
        resource_changed_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.node_registry = node_registry
        self.placement = placement
        self.environment_fingerprint = environment_fingerprint
        self.config = config
        self.endpoint_factory = endpoint_factory
        self._event_sink = event_sink
        self._resource_changed_sink = resource_changed_sink
        self._workers: dict[str, _PooledWorker] = {}
        self._leases: dict[str, _PoolLeaseRecord] = {}
        self._invalidated_worker_leases: set[str] = set()
        self._wake = asyncio.Event()
        self._reconciler: asyncio.Task[None] | None = None
        self._retire_tasks: set[asyncio.Task[bool]] = set()
        self._closed = False
        self._close_complete = False
        self._standby_hits = 0
        self._cold_starts = 0
        self._replenish_failures = 0
        self._reservation_failures = 0
        self._sanitize_failures = 0
        self._termination_failures = 0
        self._lock = RLock()

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Worker Pool is closed")
        if self._reconciler is None:
            self._reconciler = asyncio.create_task(
                self._reconcile_loop(), name="ascend-maze-worker-pool"
            )
        self.notify_changed()

    async def close(self) -> None:
        if self._close_complete:
            return
        self._closed = True
        self._wake.set()
        if self._reconciler is not None:
            self._reconciler.cancel()
            await asyncio.gather(self._reconciler, return_exceptions=True)
            self._reconciler = None
        retire_tasks = tuple(self._retire_tasks)
        for task in retire_tasks:
            task.cancel()
        if retire_tasks:
            await asyncio.gather(*retire_tasks, return_exceptions=True)
        self._retire_tasks.clear()
        with self._lock:
            workers = tuple(self._workers.values())

        async def retire(worker: _PooledWorker) -> str | None:
            timeout_ms = self._require_profile_config(
                worker.descriptor.profile
            ).termination_timeout_ms
            try:
                await asyncio.wait_for(
                    self._retire_worker(
                        worker.descriptor.worker_id,
                        "pool_closed",
                    ),
                    timeout=timeout_ms / 1_000 + 1.0,
                )
            except Exception as exc:
                return (
                    f"{worker.descriptor.worker_id}:"
                    f"{type(exc).__name__}:{exc}"
                )
            return None

        results = await asyncio.gather(*(retire(worker) for worker in workers))
        failures = [item for item in results if item is not None]
        if failures:
            raise RuntimeError("Worker Pool close failed: " + "; ".join(failures))
        self._close_complete = True

    def notify_changed(self) -> None:
        if not self._closed:
            self._wake.set()

    def set_resource_changed_sink(self, sink: Callable[[str], None]) -> None:
        self._resource_changed_sink = sink

    def update_config(self, config: WorkerPoolConfig) -> None:
        if config.config_generation <= self.config.config_generation:
            raise StateTransitionError("Worker Pool config generation must increase")
        self.config = config
        self.notify_changed()

    async def acquire(
        self,
        *,
        placement_lease: PlacementLease,
        task_kind: str,
        execution_target: ExecutionTarget,
        now_ms: int,
    ) -> WorkerLease:
        acquire_started_ms = monotonic_time_ms()
        binding = self.node_registry.resolve_lease(placement_lease)
        profile = self._profile_for(task_kind, execution_target)
        profile_config = self._require_profile_config(profile)
        worker_id = placement_lease.standby_worker_id
        source = "standby" if worker_id is not None else "cold_start"
        worker: _PooledWorker
        if worker_id is not None:
            with self._lock:
                worker = self._workers.get(worker_id)  # type: ignore[assignment]
            valid = False
            if worker is not None:
                async with worker.retire_lock:
                    with self._lock:
                        current = self._workers.get(worker_id)
                        valid = (
                            current is worker
                            and worker.descriptor.state is StandbyWorkerState.IDLE
                            and worker.descriptor.node_id == placement_lease.node_id
                            and worker.descriptor.boot_id == placement_lease.boot_id
                            and worker.descriptor.profile is profile
                            and worker.endpoint is not None
                        )
                        if valid:
                            try:
                                reservation = self.placement.standby_snapshot(worker_id)
                            except KeyError:
                                valid = False
                            else:
                                valid = (
                                    reservation.status
                                    is StandbyReservationStatus.CONVERTED
                                    and reservation.converted_task_lease_id
                                    == placement_lease.lease_id
                                    and reservation.worker_generation
                                    == worker.descriptor.worker_generation
                                )
                        if valid:
                            worker.descriptor = replace(
                                worker.descriptor,
                                state=StandbyWorkerState.ACQUIRED,
                                standby_lease_id=None,
                                idle_since_ms=None,
                            )
                            self._standby_hits += 1
            if not valid:
                self.notify_changed()
                raise StateTransitionError("converted Standby Worker is unavailable")
        else:
            worker_id = new_id("worker")
            generation = 1
            deadline_ms = min(
                placement_lease.dispatch_deadline_ms,
                now_ms + profile_config.acquire_timeout_ms,
            )
            descriptor = StandbyWorkerDescriptor(
                worker_id=worker_id,
                worker_generation=generation,
                worker_endpoint_id=new_id("worker_endpoint"),
                node_id=placement_lease.node_id,
                boot_id=placement_lease.boot_id,
                profile=profile,
                state=StandbyWorkerState.STARTING,
                standby_lease_id=None,
                process_id=None,
                created_at_ms=now_ms,
                idle_since_ms=None,
                tasks_completed=0,
                host_warmup_ms=0,
                config_generation=self.config.config_generation,
            )
            worker = _PooledWorker(descriptor=descriptor, endpoint=None)
            with self._lock:
                live_count = sum(
                    candidate.descriptor.state is not StandbyWorkerState.DEAD
                    and candidate.descriptor.node_id == placement_lease.node_id
                    and candidate.descriptor.boot_id == placement_lease.boot_id
                    and candidate.descriptor.profile is profile
                    for candidate in self._workers.values()
                )
                if live_count >= profile_config.max_total:
                    raise StateTransitionError("Worker Pool max_total is exhausted")
                self._workers[worker_id] = worker
            self._emit("worker_starting", descriptor)
            endpoint: Any | None = None
            try:
                endpoint, report = await self.endpoint_factory.start(
                    worker_id=worker_id,
                    worker_generation=generation,
                    binding=binding,
                    config=profile_config,
                    deadline_ms=deadline_ms,
                )
                if monotonic_time_ms() > deadline_ms:
                    raise TimeoutError("cold Worker became ready after acquire deadline")
                with self._lock:
                    current = self._workers.get(worker_id)
                    if (
                        current is not worker
                        or worker.descriptor.state is not StandbyWorkerState.STARTING
                        or worker.descriptor.config_generation
                        != self.config.config_generation
                        or not self._node_is_healthy(
                            placement_lease.node_id,
                            placement_lease.boot_id,
                        )
                    ):
                        raise StateTransitionError(
                            "cold Worker startup was fenced before ready"
                        )
                    worker.endpoint = endpoint
                    worker.descriptor = replace(
                        descriptor,
                        state=StandbyWorkerState.ACQUIRED,
                        process_id=report.worker_pid,
                        host_warmup_ms=report.host_warmup_ms,
                        zero_hbm_verified=report.zero_hbm_verified,
                        npu_context_device_ids=report.npu_context_device_ids,
                        npu_used_hbm_mb=report.npu_used_hbm_mb,
                    )
                    self._cold_starts += 1
            except BaseException as exc:
                termination_failed = False
                if endpoint is not None:
                    try:
                        await asyncio.shield(
                            self.endpoint_factory.terminate(
                                endpoint,
                                force=True,
                                timeout_ms=profile_config.termination_timeout_ms,
                            )
                        )
                    except Exception as terminate_exc:
                        termination_failed = True
                        self._termination_failures += 1
                        self._emit(
                            "worker_termination_failed",
                            worker.descriptor,
                            str(terminate_exc),
                        )
                with self._lock:
                    current = self._workers.get(worker_id)
                    if current is worker:
                        worker.endpoint = endpoint if termination_failed else None
                        worker.descriptor = replace(
                            worker.descriptor,
                            state=(
                                StandbyWorkerState.RETIRING
                                if termination_failed
                                else StandbyWorkerState.DEAD
                            ),
                        )
                self._emit("cold_start_failed", worker.descriptor, type(exc).__name__)
                self.notify_changed()
                raise
            self._emit("cold_start", worker.descriptor)

        descriptor = worker.descriptor
        acquire_elapsed_ms = max(0, monotonic_time_ms() - acquire_started_ms)
        lease = WorkerLease(
            worker_lease_id=new_id("worker_lease"),
            worker_endpoint_id=descriptor.worker_endpoint_id,
            worker_id=descriptor.worker_id,
            worker_generation=descriptor.worker_generation,
            node_id=descriptor.node_id,
            boot_id=descriptor.boot_id,
            profile=descriptor.profile,
            source=source,
            bound_device_id=placement_lease.npu_device_id,
            acquired_at_ms=monotonic_time_ms(),
            worker_acquire_ms=acquire_elapsed_ms,
            cold_start_ms=(
                acquire_elapsed_ms if source == "cold_start" else 0
            ),
            host_warmup_ms=descriptor.host_warmup_ms,
        )
        with self._lock:
            self._leases[lease.worker_lease_id] = _PoolLeaseRecord(
                lease=lease,
                placement_lease=placement_lease,
            )
        self._emit_lease(
            "standby_hit" if source == "standby" else "worker_acquired",
            descriptor,
            lease,
            placement_lease,
        )
        return lease

    def submit(self, worker_lease_id: str, kwargs: dict[str, object]) -> Any:
        with self._lock:
            lease_record = self._leases[worker_lease_id]
            if lease_record.released:
                raise StateTransitionError("WorkerLease is already released")
            worker = self._workers[lease_record.lease.worker_id]
            if worker.endpoint is None:
                raise StateTransitionError("Worker endpoint is unavailable")
            return self.endpoint_factory.submit(worker.endpoint, kwargs)

    async def release(
        self,
        worker_lease_id: str,
        *,
        disposition: str,
    ) -> bool:
        if disposition not in {"discard", "reuse"}:
            raise ValueError("unsupported WorkerLease disposition")
        with self._lock:
            record = self._leases[worker_lease_id]
            if record.released:
                return False
            if record.releasing:
                return False
            record.releasing = True
            record.disposition = disposition
            worker = self._workers.get(record.lease.worker_id)
        if worker is None:
            with self._lock:
                record.releasing = False
                record.released = True
            return True
        descriptor = worker.descriptor
        profile_config = self._require_profile_config(descriptor.profile)
        can_reuse = (
            self.config.mode == "zero_hbm_standby"
            and disposition == "reuse"
            and self._node_is_healthy(descriptor.node_id, descriptor.boot_id)
            and descriptor.profile in {WorkerProfile.CPU, WorkerProfile.IO}
            and descriptor.tasks_completed + 1 < profile_config.max_tasks_per_worker
            and monotonic_time_ms() - descriptor.created_at_ms
            < profile_config.max_worker_lifetime_ms
            and self._idle_count(descriptor.node_id, descriptor.boot_id, descriptor.profile)
            < profile_config.max_idle
        )
        if can_reuse:
            now_ms = monotonic_time_ms()
            standby_lease = self.placement.restore_task_to_standby(
                task_lease_id=record.placement_lease.lease_id,
                worker_id=descriptor.worker_id,
                worker_generation=descriptor.worker_generation,
                profile=descriptor.profile.value,
                resources=profile_config.standby_resources,
                now_ms=now_ms,
                idle_deadline_ms=now_ms + profile_config.idle_ttl_ms,
            )
            if standby_lease is not None:
                with self._lock:
                    worker.descriptor = replace(
                        descriptor,
                        state=StandbyWorkerState.IDLE,
                        standby_lease_id=standby_lease.lease_id,
                        idle_since_ms=now_ms,
                        tasks_completed=descriptor.tasks_completed + 1,
                    )
                self._emit("worker_reused", worker.descriptor)
                self._resource_changed("standby_worker_reused")
                self.notify_changed()
                with self._lock:
                    record.releasing = False
                    record.released = True
                self._emit_lease(
                    "worker_released",
                    worker.descriptor,
                    record.lease,
                    record.placement_lease,
                    reason=disposition,
                )
                return True
        try:
            await self._retire_worker(descriptor.worker_id, f"worker_{disposition}")
        except Exception:
            with self._lock:
                record.releasing = False
            raise
        with self._lock:
            record.releasing = False
            record.released = True
        self._emit_lease(
            "worker_released",
            descriptor,
            record.lease,
            record.placement_lease,
            reason=disposition,
        )
        self.notify_changed()
        return True

    async def cancel(self, worker_lease_id: str) -> None:
        with self._lock:
            record = self._leases.get(worker_lease_id)
            if record is None or record.released:
                return
            worker_id = record.lease.worker_id
        await self._retire_worker(worker_id, "worker_cancelled", force=True)
        self.notify_changed()

    def record_cleanup_failure(self, worker_lease_id: str, reason: str) -> None:
        if reason in {
            "background_thread_leaked",
            "child_process_leaked",
            "file_descriptor_leaked",
            "rss_limit_exceeded",
        } or reason.startswith("environment_restore_failed:"):
            self._sanitize_failures += 1
        with self._lock:
            record = self._leases.get(worker_lease_id)
            worker = (
                None
                if record is None
                else self._workers.get(record.lease.worker_id)
            )
        if worker is not None:
            assert record is not None
            self._emit_lease(
                "worker_cleanup_rejected",
                worker.descriptor,
                record.lease,
                record.placement_lease,
                reason=reason,
            )

    def invalidate_node(self, node_id: str, boot_id: str) -> tuple[WorkerLease, ...]:
        with self._lock:
            invalidated = tuple(
                record.lease
                for record in self._leases.values()
                if not record.released
                and record.lease.worker_lease_id
                not in self._invalidated_worker_leases
                and record.lease.node_id == node_id
                and record.lease.boot_id == boot_id
            )
            for lease in invalidated:
                self._invalidated_worker_leases.add(lease.worker_lease_id)
            worker_ids = tuple(
                worker_id
                for worker_id, worker in self._workers.items()
                if worker.descriptor.node_id == node_id
                and worker.descriptor.boot_id == boot_id
                and worker.descriptor.state is not StandbyWorkerState.DEAD
            )
        for worker_id in worker_ids:
            self._schedule_retire(worker_id, "node_generation_invalid")
        self.notify_changed()
        return invalidated

    def active_count(self, node_id: str | None = None) -> int:
        with self._lock:
            return sum(
                not record.released
                and (node_id is None or record.lease.node_id == node_id)
                for record in self._leases.values()
            )

    def live_count(
        self, node_id: str | None = None, boot_id: str | None = None
    ) -> int:
        with self._lock:
            return sum(
                worker.descriptor.state is not StandbyWorkerState.DEAD
                and (node_id is None or worker.descriptor.node_id == node_id)
                and (boot_id is None or worker.descriptor.boot_id == boot_id)
                for worker in self._workers.values()
            )

    async def advance_node_drain(self, node_id: str, boot_id: str) -> None:
        try:
            binding = self.node_registry.binding(node_id)
        except KeyError:
            return
        if binding.boot_id != boot_id:
            raise StateTransitionError("Worker Pool node boot generation changed")
        await self.reconcile_once()

    def is_released(self, worker_lease_id: str) -> bool:
        with self._lock:
            record = self._leases.get(worker_lease_id)
            return record is None or record.released

    def purge_released(self) -> int:
        with self._lock:
            keys = [key for key, record in self._leases.items() if record.released]
            for key in keys:
                del self._leases[key]
                self._invalidated_worker_leases.discard(key)
            return len(keys)

    def snapshot(self) -> WorkerPoolSnapshot:
        with self._lock:
            workers = tuple(
                self._workers[worker_id].descriptor
                for worker_id in sorted(self._workers)
            )
            worker_leases = tuple(
                WorkerLeaseSnapshot(
                    lease=record.lease,
                    placement_lease_id=record.placement_lease.lease_id,
                    released=record.released,
                    releasing=record.releasing,
                    disposition=record.disposition,
                )
                for _, record in sorted(self._leases.items())
            )
            return WorkerPoolSnapshot(
                mode=self.config.mode,
                config_generation=self.config.config_generation,
                workers=workers,
                worker_leases=worker_leases,
                active_worker_lease_count=sum(
                    not item.released for item in worker_leases
                ),
                standby_hits=self._standby_hits,
                cold_starts=self._cold_starts,
                replenish_failures=self._replenish_failures,
                reservation_failures=self._reservation_failures,
                sanitize_failures=self._sanitize_failures,
                termination_failures=self._termination_failures,
            )

    async def reconcile_once(self) -> None:
        bindings = {
            (binding.node_id, binding.boot_id): binding
            for binding in self.node_registry.active_bindings()
        }
        with self._lock:
            workers = tuple(self._workers.values())
        now_ms = monotonic_time_ms()
        retire: list[tuple[str, str, frozenset[StandbyWorkerState]]] = []
        for worker in workers:
            descriptor = worker.descriptor
            if descriptor.state is StandbyWorkerState.DEAD:
                continue
            if descriptor.state is StandbyWorkerState.RETIRING:
                retire.append(
                    (
                        descriptor.worker_id,
                        "retry_termination",
                        frozenset({descriptor.state}),
                    )
                )
                continue
            if descriptor.config_generation != self.config.config_generation:
                retire.append(
                    (
                        descriptor.worker_id,
                        "pool_config_replaced",
                        frozenset({descriptor.state}),
                    )
                )
                continue
            if (descriptor.node_id, descriptor.boot_id) not in bindings:
                try:
                    node_status = self.node_registry.status(descriptor.node_id)
                except KeyError:
                    node_status = RuntimeNodeStatus.OFFLINE
                if (
                    node_status
                    in {RuntimeNodeStatus.DRAINING, RuntimeNodeStatus.DRAINED}
                    and descriptor.state is StandbyWorkerState.ACQUIRED
                ):
                    continue
                retire.append(
                    (
                        descriptor.worker_id,
                        "node_not_healthy",
                        frozenset({descriptor.state}),
                    )
                )
                continue
            if descriptor.state is StandbyWorkerState.IDLE:
                try:
                    reservation = self.placement.standby_snapshot(
                        descriptor.worker_id
                    )
                except KeyError:
                    retire.append(
                        (
                            descriptor.worker_id,
                            "standby_reservation_missing",
                            frozenset({StandbyWorkerState.IDLE}),
                        )
                    )
                    continue
                if reservation.status is StandbyReservationStatus.CONVERTED:
                    continue
                if reservation.status is not StandbyReservationStatus.READY:
                    retire.append(
                        (
                            descriptor.worker_id,
                            "standby_reservation_inactive",
                            frozenset({StandbyWorkerState.IDLE}),
                        )
                    )
                    continue
        idle_groups: dict[
            tuple[str, str, WorkerProfile], list[StandbyWorkerDescriptor]
        ] = {}
        for worker in workers:
            descriptor = worker.descriptor
            if descriptor.state is StandbyWorkerState.IDLE:
                idle_groups.setdefault(
                    (descriptor.node_id, descriptor.boot_id, descriptor.profile), []
                ).append(descriptor)
        for (_, _, profile), descriptors in idle_groups.items():
            profile_config = self._require_profile_config(profile)
            excess = max(0, len(descriptors) - profile_config.max_idle)
            eligible = sorted(
                (
                    item
                    for item in descriptors
                    if item.idle_since_ms is not None
                    and now_ms - item.idle_since_ms >= profile_config.idle_ttl_ms
                ),
                key=lambda item: (item.idle_since_ms or 0, item.worker_id),
            )
            retire.extend(
                (
                    item.worker_id,
                    "idle_ttl",
                    frozenset({StandbyWorkerState.IDLE}),
                )
                for item in eligible[:excess]
            )
        for worker_id, reason, expected_states in retire:
            await self._retire_worker(
                worker_id,
                reason,
                expected_states=expected_states,
            )

        if self.config.mode != "zero_hbm_standby":
            self._purge_dead_workers()
            return
        for binding in bindings.values():
            for profile_config in self.config.profiles:
                idle = self._idle_count(
                    binding.node_id, binding.boot_id, profile_config.profile
                )
                total = self._worker_count(
                    binding.node_id, binding.boot_id, profile_config.profile
                )
                needed = min(
                    max(0, profile_config.min_idle - idle),
                    max(0, profile_config.max_total - total),
                    profile_config.replenish_concurrency,
                )
                if needed:
                    await asyncio.gather(
                        *(
                            self._replenish(binding, profile_config)
                            for _ in range(needed)
                        )
                    )
        self._purge_dead_workers()

    async def _reconcile_loop(self) -> None:
        while not self._closed:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._replenish_failures += 1
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self.config.reconcile_interval_ms / 1000,
                )
            except asyncio.TimeoutError:
                pass

    async def _replenish(
        self,
        binding: RuntimeNodeBinding,
        config: WorkerPoolProfileConfig,
    ) -> None:
        worker_id = new_id("worker")
        generation = 1
        now_ms = monotonic_time_ms()
        deadline_ms = now_ms + config.acquire_timeout_ms
        standby_lease = self.placement.reserve_standby(
            worker_id=worker_id,
            worker_generation=generation,
            profile=config.profile.value,
            node_id=binding.node_id,
            boot_id=binding.boot_id,
            resources=config.standby_resources,
            now_ms=now_ms,
            startup_deadline_ms=deadline_ms,
        )
        if standby_lease is None:
            self._reservation_failures += 1
            self._emit_for_binding("reservation_failed", binding, config.profile, worker_id)
            return
        descriptor = StandbyWorkerDescriptor(
            worker_id=worker_id,
            worker_generation=generation,
            worker_endpoint_id=new_id("worker_endpoint"),
            node_id=binding.node_id,
            boot_id=binding.boot_id,
            profile=config.profile,
            state=StandbyWorkerState.STARTING,
            standby_lease_id=standby_lease.lease_id,
            process_id=None,
            created_at_ms=now_ms,
            idle_since_ms=None,
            tasks_completed=0,
            host_warmup_ms=0,
            config_generation=self.config.config_generation,
        )
        worker = _PooledWorker(descriptor=descriptor, endpoint=None)
        registered = False
        with self._lock:
            live_count = sum(
                candidate.descriptor.state is not StandbyWorkerState.DEAD
                and candidate.descriptor.node_id == binding.node_id
                and candidate.descriptor.boot_id == binding.boot_id
                and candidate.descriptor.profile is config.profile
                for candidate in self._workers.values()
            )
            if live_count < config.max_total:
                self._workers[worker_id] = worker
                registered = True
        if not registered:
            self.placement.retire_standby(
                worker_id,
                now_ms=monotonic_time_ms(),
                reason="worker_pool_max_total_race",
            )
            try:
                self.placement.purge_retired_standby(worker_id)
            except KeyError:
                pass
            self._emit_for_binding(
                "replenish_capacity_exhausted",
                binding,
                config.profile,
                worker_id,
            )
            return
        self._emit("worker_starting", descriptor)
        endpoint: Any | None = None
        try:
            endpoint, report = await self.endpoint_factory.start(
                worker_id=worker_id,
                worker_generation=generation,
                binding=binding,
                config=config,
                deadline_ms=deadline_ms,
            )
            with self._lock:
                worker.endpoint = endpoint
                worker.descriptor = replace(
                    descriptor,
                    process_id=report.worker_pid,
                    host_warmup_ms=report.host_warmup_ms,
                    zero_hbm_verified=report.zero_hbm_verified,
                    npu_context_device_ids=report.npu_context_device_ids,
                    npu_used_hbm_mb=report.npu_used_hbm_mb,
                )
            if not self.placement.activate_standby(
                worker_id=worker_id,
                worker_generation=generation,
                lease_id=standby_lease.lease_id,
                now_ms=monotonic_time_ms(),
            ):
                raise RuntimeError("Standby reservation expired before Worker became ready")
            with self._lock:
                worker.descriptor = replace(
                    worker.descriptor,
                    state=StandbyWorkerState.IDLE,
                    idle_since_ms=monotonic_time_ms(),
                )
            self._emit("worker_ready", worker.descriptor)
            self._resource_changed("standby_worker_ready")
        except BaseException as exc:
            termination_failed = False
            if endpoint is not None:
                try:
                    await asyncio.wait_for(
                        self.endpoint_factory.terminate(
                            endpoint,
                            timeout_ms=config.termination_timeout_ms,
                        ),
                        timeout=config.termination_timeout_ms / 1_000 + 1.0,
                    )
                except Exception as terminate_exc:
                    termination_failed = True
                    self._termination_failures += 1
                    with self._lock:
                        worker.descriptor = replace(
                            worker.descriptor, state=StandbyWorkerState.RETIRING
                        )
                    self._emit(
                        "worker_termination_failed",
                        worker.descriptor,
                        str(terminate_exc),
                    )
            if not termination_failed:
                self.placement.retire_standby(
                    worker_id,
                    now_ms=monotonic_time_ms(),
                    reason="standby_start_failed",
                )
                try:
                    self.placement.purge_retired_standby(worker_id)
                except KeyError:
                    pass
            with self._lock:
                if not termination_failed:
                    worker.endpoint = None
                    worker.descriptor = replace(
                        worker.descriptor, state=StandbyWorkerState.DEAD
                    )
                self._replenish_failures += 1
            self._emit("replenish_failed", worker.descriptor, type(exc).__name__)
            self._resource_changed("standby_worker_start_failed")
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def _retire_worker(
        self,
        worker_id: str,
        reason: str,
        *,
        force: bool = False,
        expected_states: frozenset[StandbyWorkerState] | None = None,
    ) -> bool:
        with self._lock:
            worker = self._workers.get(worker_id)
        if worker is None:
            return False
        async with worker.retire_lock:
            with self._lock:
                if worker.descriptor.state is StandbyWorkerState.DEAD:
                    return False
                if (
                    expected_states is not None
                    and worker.descriptor.state not in expected_states
                ):
                    return False
                task_lease_id = next(
                    (
                        record.placement_lease.lease_id
                        for record in self._leases.values()
                        if not record.released
                        and record.lease.worker_id == worker_id
                        and record.placement_lease.standby_worker_id == worker_id
                    ),
                    None,
                )
            reservation_known = False
            try:
                reservation = self.placement.standby_snapshot(worker_id)
            except KeyError:
                pass
            else:
                reservation_known = True
                if (
                    reservation.status is StandbyReservationStatus.CONVERTED
                    and task_lease_id is None
                ):
                    if not force:
                        return False
                    task_lease_id = reservation.converted_task_lease_id
                fenced = self.placement.begin_standby_retirement(
                    worker_id,
                    converted_task_lease_id=task_lease_id,
                )
                if not fenced:
                    latest = self.placement.standby_snapshot(worker_id)
                    if latest.status is not StandbyReservationStatus.RETIRED:
                        return False
            with self._lock:
                if worker.descriptor.state is StandbyWorkerState.DEAD:
                    return False
                worker.descriptor = replace(
                    worker.descriptor, state=StandbyWorkerState.RETIRING
                )
                endpoint = worker.endpoint
            if endpoint is not None:
                profile_config = self._require_profile_config(
                    worker.descriptor.profile
                )
                try:
                    await asyncio.wait_for(
                        self.endpoint_factory.terminate(
                            endpoint,
                            force=force,
                            timeout_ms=profile_config.termination_timeout_ms,
                        ),
                        timeout=profile_config.termination_timeout_ms / 1_000 + 1.0,
                    )
                except Exception as exc:
                    self._termination_failures += 1
                    self._emit(
                        "worker_termination_failed", worker.descriptor, str(exc)
                    )
                    raise
            if reservation_known:
                self.placement.complete_standby_retirement(
                    worker_id,
                    now_ms=monotonic_time_ms(),
                    reason=reason,
                )
            with self._lock:
                worker.endpoint = None
                worker.descriptor = replace(
                    worker.descriptor,
                    state=StandbyWorkerState.DEAD,
                    standby_lease_id=None,
                    idle_since_ms=None,
                )
            self._emit("worker_retired", worker.descriptor, reason)
            self._resource_changed("standby_worker_retired")
            try:
                self.placement.purge_retired_standby(worker_id)
            except KeyError:
                pass
            return True

    def _schedule_retire(self, worker_id: str, reason: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._retire_worker(worker_id, reason, force=True)
        )
        self._retire_tasks.add(task)
        task.add_done_callback(self._retire_tasks.discard)
        task.add_done_callback(self._consume_retire_result)

    @staticmethod
    def _consume_retire_result(task: asyncio.Task[bool]) -> None:
        if not task.cancelled():
            task.exception()

    def _idle_count(self, node_id: str, boot_id: str, profile: WorkerProfile) -> int:
        with self._lock:
            return sum(
                worker.descriptor.state is StandbyWorkerState.IDLE
                and worker.descriptor.node_id == node_id
                and worker.descriptor.boot_id == boot_id
                and worker.descriptor.profile is profile
                for worker in self._workers.values()
            )

    def _worker_count(self, node_id: str, boot_id: str, profile: WorkerProfile) -> int:
        with self._lock:
            return sum(
                worker.descriptor.state is not StandbyWorkerState.DEAD
                and worker.descriptor.node_id == node_id
                and worker.descriptor.boot_id == boot_id
                and worker.descriptor.profile is profile
                for worker in self._workers.values()
            )

    def _node_is_healthy(self, node_id: str, boot_id: str) -> bool:
        try:
            return (
                self.node_registry.binding(node_id).boot_id == boot_id
                and self.node_registry.status(node_id) is RuntimeNodeStatus.HEALTHY
            )
        except KeyError:
            return False

    def _purge_dead_workers(self) -> None:
        with self._lock:
            worker_ids = {
                record.lease.worker_id
                for record in self._leases.values()
                if not record.released
            }
            for worker_id in tuple(self._workers):
                if (
                    self._workers[worker_id].descriptor.state
                    is StandbyWorkerState.DEAD
                    and worker_id not in worker_ids
                ):
                    del self._workers[worker_id]

    def _require_profile_config(
        self, profile: WorkerProfile
    ) -> WorkerPoolProfileConfig:
        config = self.config.profile_config(profile)
        if config is None:
            raise StateTransitionError(
                f"Worker Pool has no configuration for profile {profile.value}"
            )
        return config

    def _emit(
        self,
        event_type: str,
        descriptor: StandbyWorkerDescriptor,
        reason: str | None = None,
    ) -> None:
        if self._event_sink is not None:
            self._event_sink(
                WorkerPoolEvent(
                    event_type=event_type,
                    occurred_at_ms=monotonic_time_ms(),
                    node_id=descriptor.node_id,
                    boot_id=descriptor.boot_id,
                    profile=descriptor.profile,
                    worker_id=descriptor.worker_id,
                    reason=reason,
                )
            )

    def _emit_for_binding(
        self,
        event_type: str,
        binding: RuntimeNodeBinding,
        profile: WorkerProfile,
        worker_id: str | None,
    ) -> None:
        if self._event_sink is not None:
            self._event_sink(
                WorkerPoolEvent(
                    event_type=event_type,
                    occurred_at_ms=monotonic_time_ms(),
                    node_id=binding.node_id,
                    boot_id=binding.boot_id,
                    profile=profile,
                    worker_id=worker_id,
                )
            )

    def _emit_lease(
        self,
        event_type: str,
        descriptor: StandbyWorkerDescriptor,
        worker_lease: WorkerLease,
        placement_lease: PlacementLease,
        *,
        reason: str | None = None,
    ) -> None:
        if self._event_sink is not None:
            self._event_sink(
                WorkerPoolEvent(
                    event_type=event_type,
                    occurred_at_ms=monotonic_time_ms(),
                    node_id=descriptor.node_id,
                    boot_id=descriptor.boot_id,
                    profile=descriptor.profile,
                    worker_id=descriptor.worker_id,
                    worker_lease_id=worker_lease.worker_lease_id,
                    placement_lease_id=placement_lease.lease_id,
                    run_id=placement_lease.run_id,
                    task_id=placement_lease.task_id,
                    attempt=placement_lease.attempt,
                    reason=reason,
                    worker_acquire_ms=worker_lease.worker_acquire_ms,
                    cold_start_ms=worker_lease.cold_start_ms,
                    host_warmup_ms=worker_lease.host_warmup_ms,
                )
            )

    def _resource_changed(self, reason: str) -> None:
        if self._resource_changed_sink is not None:
            self._resource_changed_sink(reason)

    @staticmethod
    def _profile_for(
        task_kind: str,
        execution_target: ExecutionTarget,
    ) -> WorkerProfile:
        if execution_target is ExecutionTarget.MODEL_SERVICE:
            return WorkerProfile.IO
        try:
            return {
                "cpu": WorkerProfile.CPU,
                "io": WorkerProfile.IO,
                "npu": WorkerProfile.NPU_HOST,
            }[task_kind]
        except KeyError as exc:
            raise ValueError(f"unsupported task kind: {task_kind}") from exc
