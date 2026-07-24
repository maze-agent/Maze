"""Atomic ready-index routing and ModelRouteLease accounting."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from time import perf_counter_ns
from typing import Callable

from ascend_maze.contracts.runtime import ModelRouteLease
from ascend_maze.core.canonical import FrozenMap, canonical_digest, freeze_canonical
from ascend_maze.core.clock import Clock, SystemClock
from ascend_maze.core.errors import ContractValidationError, StateTransitionError
from ascend_maze.core.identifiers import new_id, stable_id
from ascend_maze.inference.contracts import (
    ModelControlEvent,
    ModelInstanceState,
    ModelRouteAcquireResult,
    ModelRouteLeaseSnapshot,
    ModelRouteLeaseStatus,
)
from ascend_maze.inference.instance_manager import ModelInstanceManager


@dataclass(slots=True)
class _RouteRecord:
    lease: ModelRouteLease
    status: ModelRouteLeaseStatus
    activated_at_ms: int | None = None
    finished_at_ms: int | None = None
    finish_reason: str | None = None
    request_inflight: bool = False
    invalidation_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _AffinityEntry:
    instance_id: str
    instance_generation: int
    expires_at_ms: int


class InferenceRouter:
    def __init__(
        self,
        *,
        instances: ModelInstanceManager,
        event_sink: Callable[[ModelControlEvent], None] | None = None,
        clock: Clock | None = None,
        affinity_ttl_ms: int = 300_000,
        affinity_capacity: int = 10_000,
    ) -> None:
        if affinity_ttl_ms < 1 or affinity_capacity < 1:
            raise ValueError("affinity TTL and capacity must be positive")
        self.instances = instances
        self.event_sink = event_sink
        self.clock = clock or SystemClock()
        self.affinity_ttl_ms = affinity_ttl_ms
        self.affinity_capacity = affinity_capacity
        self._routes: dict[str, _RouteRecord] = {}
        self._attempt_routes: dict[tuple[str, str, int], str] = {}
        self._affinity: OrderedDict[str, _AffinityEntry] = OrderedDict()
        self._lock = RLock()

    def acquire(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        model_id: str,
        session_key_hash: str | None,
        dispatch_deadline_ms: int,
    ) -> ModelRouteAcquireResult:
        started_ns = perf_counter_ns()
        if not run_id or not task_id or not model_id or attempt < 1:
            raise ContractValidationError("route Attempt identity is invalid")
        now = self.clock.monotonic_ms()
        if dispatch_deadline_ms <= now:
            raise ContractValidationError("route dispatch deadline has expired")
        attempt_key = (run_id, task_id, attempt)
        affinity_hash = canonical_digest(
            {
                "run_id": run_id,
                "model_id": model_id,
                "session_key_hash": session_key_hash,
            }
        )
        with self._lock:
            existing_id = self._attempt_routes.get(attempt_key)
            if existing_id is not None:
                existing = self._routes[existing_id]
                if existing.lease.model_id != model_id:
                    raise ContractValidationError("Attempt route model conflict")
                if existing.status in {
                    ModelRouteLeaseStatus.RESERVED,
                    ModelRouteLeaseStatus.ACTIVE,
                }:
                    return ModelRouteAcquireResult(existing.lease, None, False)
                self._emit_rejection(
                    run_id=run_id,
                    task_id=task_id,
                    attempt=attempt,
                    model_id=model_id,
                    reason="attempt_route_already_terminal",
                    started_ns=started_ns,
                )
                return ModelRouteAcquireResult(None, "attempt_route_already_terminal", False)
            spec = self.instances.catalog.get(model_id)
            candidates = list(
                self.instances.ready_instances(model_id, spec.catalog_revision)
            )
            candidates = [
                instance
                for instance in candidates
                if instance.endpoint_id is not None
                and instance.route_occupancy
                < self.instances.spec_for_instance(instance.instance_id).request_capacity
            ]
            if not candidates:
                self._emit_rejection(
                    run_id=run_id,
                    task_id=task_id,
                    attempt=attempt,
                    model_id=model_id,
                    reason="model_route_unavailable",
                    started_ns=started_ns,
                )
                return ModelRouteAcquireResult(None, "model_route_unavailable", False)
            affinity_hit = False
            selected = None
            affinity = self._affinity.get(affinity_hash)
            if affinity is not None:
                if affinity.expires_at_ms <= now:
                    del self._affinity[affinity_hash]
                else:
                    selected = next(
                        (
                            instance
                            for instance in candidates
                            if instance.instance_id == affinity.instance_id
                            and instance.generation == affinity.instance_generation
                        ),
                        None,
                    )
                    if selected is not None:
                        affinity_hit = True
                        self._affinity.move_to_end(affinity_hash)
            if selected is None:
                selected = min(
                    candidates,
                    key=lambda instance: (
                        instance.route_occupancy
                        / self.instances.spec_for_instance(
                            instance.instance_id
                        ).request_capacity,
                        stable_id("route_tie", affinity_hash, instance.instance_id),
                        instance.instance_id,
                    ),
                )
            self.instances.reserve_route(selected.instance_id, selected.generation)
            spec = self.instances.spec_for_instance(selected.instance_id)
            assert selected.endpoint_id is not None
            lease = ModelRouteLease(
                route_lease_id=new_id("route"),
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                model_id=model_id,
                catalog_revision=spec.catalog_revision,
                instance_id=selected.instance_id,
                instance_generation=selected.generation,
                adapter_name=spec.backend,
                endpoint_id=selected.endpoint_id,
                instance_node_id=selected.node_id or "unknown",
                instance_boot_id=selected.boot_id or "unknown",
                affinity_key_hash=affinity_hash,
                created_at_ms=now,
                dispatch_deadline_ms=dispatch_deadline_ms,
            )
            self._routes[lease.route_lease_id] = _RouteRecord(
                lease=lease,
                status=ModelRouteLeaseStatus.RESERVED,
            )
            self._attempt_routes[attempt_key] = lease.route_lease_id
            self._affinity[affinity_hash] = _AffinityEntry(
                selected.instance_id,
                selected.generation,
                now + self.affinity_ttl_ms,
            )
            self._affinity.move_to_end(affinity_hash)
            while len(self._affinity) > self.affinity_capacity:
                self._affinity.popitem(last=False)
            self._emit(
                lease,
                "model_route_reserved",
                {
                    "affinity_hit": affinity_hit,
                    "acquire_duration_ms": (
                        perf_counter_ns() - started_ns
                    )
                    // 1_000_000,
                    "route_occupancy": selected.route_occupancy + 1,
                    "request_capacity": spec.request_capacity,
                },
            )
            return ModelRouteAcquireResult(lease, None, affinity_hit)

    def activate(self, route_lease_id: str, *, now_ms: int | None = None) -> bool:
        with self._lock:
            record = self._require(route_lease_id)
            if record.status is ModelRouteLeaseStatus.ACTIVE:
                return False
            if record.status is not ModelRouteLeaseStatus.RESERVED:
                return False
            now = self.clock.monotonic_ms() if now_ms is None else now_ms
            if record.lease.dispatch_deadline_ms <= now:
                self._finish(record, ModelRouteLeaseStatus.EXPIRED, now, "dispatch_deadline")
                return False
            instance = self.instances.snapshot(record.lease.instance_id)
            if (
                instance.generation != record.lease.instance_generation
                or instance.state
                not in {ModelInstanceState.READY, ModelInstanceState.DRAINING}
            ):
                self._finish(
                    record,
                    ModelRouteLeaseStatus.INVALIDATED,
                    now,
                    "model_instance_generation_invalid",
                )
                return False
            record.status = ModelRouteLeaseStatus.ACTIVE
            record.activated_at_ms = now
            self._emit(record.lease, "model_route_active")
            return True

    def release(
        self,
        route_lease_id: str,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        instance_generation: int,
        reason: str,
    ) -> bool:
        with self._lock:
            record = self._require(route_lease_id)
            lease = record.lease
            if (
                lease.run_id != run_id
                or lease.task_id != task_id
                or lease.attempt != attempt
                or lease.instance_generation != instance_generation
            ):
                raise StateTransitionError("route release identity is stale")
            if record.status not in {
                ModelRouteLeaseStatus.RESERVED,
                ModelRouteLeaseStatus.ACTIVE,
            }:
                return False
            if record.request_inflight:
                raise StateTransitionError("cannot release route with request inflight")
            self._finish(
                record,
                ModelRouteLeaseStatus.RELEASED,
                self.clock.monotonic_ms(),
                reason,
            )
            return True

    def abandon_reserved(
        self,
        route_lease_id: str,
        *,
        reason: str,
    ) -> bool:
        """Release a route selected before any Task Attempt was created."""

        with self._lock:
            record = self._require(route_lease_id)
            if record.status is not ModelRouteLeaseStatus.RESERVED:
                return False
            self._finish(
                record,
                ModelRouteLeaseStatus.RELEASED,
                self.clock.monotonic_ms(),
                reason,
            )
            key = (
                record.lease.run_id,
                record.lease.task_id,
                record.lease.attempt,
            )
            if self._attempt_routes.get(key) == route_lease_id:
                del self._attempt_routes[key]
            return True

    def request_started(self, route_lease_id: str) -> ModelRouteLease:
        with self._lock:
            record = self._require(route_lease_id)
            if record.status is not ModelRouteLeaseStatus.ACTIVE:
                raise StateTransitionError("chat requires an active ModelRouteLease")
            if record.request_inflight:
                raise StateTransitionError("route already has a request inflight")
            self.instances.request_started(
                record.lease.instance_id, record.lease.instance_generation
            )
            record.request_inflight = True
            return record.lease

    def request_finished(self, route_lease_id: str) -> None:
        with self._lock:
            record = self._require(route_lease_id)
            if not record.request_inflight:
                raise StateTransitionError("route request inflight underflow")
            self.instances.request_finished(
                record.lease.instance_id, record.lease.instance_generation
            )
            record.request_inflight = False
            if record.invalidation_reason is not None:
                self._finish(
                    record,
                    ModelRouteLeaseStatus.INVALIDATED,
                    self.clock.monotonic_ms(),
                    record.invalidation_reason,
                )

    def forget_instance_affinity(self, instance_id: str, generation: int) -> int:
        with self._lock:
            keys = [
                key
                for key, entry in self._affinity.items()
                if entry.instance_id == instance_id
                and entry.instance_generation == generation
            ]
            for key in keys:
                del self._affinity[key]
            return len(keys)

    def affinity_count(self, instance_id: str, generation: int) -> int:
        with self._lock:
            return sum(
                entry.instance_id == instance_id
                and entry.instance_generation == generation
                for entry in self._affinity.values()
            )

    def invalidate_instance(
        self, instance_id: str, generation: int, *, reason: str
    ) -> tuple[ModelRouteLease, ...]:
        invalidated: list[ModelRouteLease] = []
        with self._lock:
            for record in self._routes.values():
                if (
                    record.lease.instance_id == instance_id
                    and record.lease.instance_generation == generation
                    and record.status
                    in {
                        ModelRouteLeaseStatus.RESERVED,
                        ModelRouteLeaseStatus.ACTIVE,
                    }
                ):
                    if record.request_inflight:
                        record.invalidation_reason = reason
                        self._emit(
                            record.lease,
                            "model_route_invalidation_pending",
                            {"reason": reason},
                        )
                    else:
                        self._finish(
                            record,
                            ModelRouteLeaseStatus.INVALIDATED,
                            self.clock.monotonic_ms(),
                            reason,
                        )
                    invalidated.append(record.lease)
            for key, entry in tuple(self._affinity.items()):
                if (
                    entry.instance_id == instance_id
                    and entry.instance_generation == generation
                ):
                    del self._affinity[key]
        return tuple(invalidated)

    def expire_reserved(self, *, now_ms: int | None = None) -> tuple[ModelRouteLease, ...]:
        now = self.clock.monotonic_ms() if now_ms is None else now_ms
        expired: list[ModelRouteLease] = []
        with self._lock:
            for record in self._routes.values():
                if (
                    record.status is ModelRouteLeaseStatus.RESERVED
                    and record.lease.dispatch_deadline_ms <= now
                ):
                    self._finish(
                        record,
                        ModelRouteLeaseStatus.EXPIRED,
                        now,
                        "dispatch_deadline",
                    )
                    expired.append(record.lease)
        return tuple(expired)

    def snapshot(self, route_lease_id: str) -> ModelRouteLeaseSnapshot:
        with self._lock:
            record = self._require(route_lease_id)
            return ModelRouteLeaseSnapshot(
                lease=record.lease,
                status=record.status,
                activated_at_ms=record.activated_at_ms,
                finished_at_ms=record.finished_at_ms,
                finish_reason=record.finish_reason,
            )

    def route_for_task(self, run_id: str, task_id: str) -> ModelRouteLease | None:
        with self._lock:
            candidates = [
                record.lease
                for record in self._routes.values()
                if record.lease.run_id == run_id
                and record.lease.task_id == task_id
                and record.status
                in {
                    ModelRouteLeaseStatus.RESERVED,
                    ModelRouteLeaseStatus.ACTIVE,
                }
            ]
            return max(candidates, key=lambda item: item.attempt, default=None)

    def active_count(self) -> int:
        with self._lock:
            return sum(
                record.status
                in {
                    ModelRouteLeaseStatus.RESERVED,
                    ModelRouteLeaseStatus.ACTIVE,
                }
                for record in self._routes.values()
            )

    def active_leases(self) -> tuple[ModelRouteLease, ...]:
        with self._lock:
            return tuple(
                record.lease
                for _, record in sorted(self._routes.items())
                if record.status
                in {
                    ModelRouteLeaseStatus.RESERVED,
                    ModelRouteLeaseStatus.ACTIVE,
                }
            )

    def destroy_run(self, run_id: str) -> int:
        with self._lock:
            active = [
                record
                for record in self._routes.values()
                if record.lease.run_id == run_id
                and record.status
                in {
                    ModelRouteLeaseStatus.RESERVED,
                    ModelRouteLeaseStatus.ACTIVE,
                }
            ]
            if active:
                raise StateTransitionError("cannot destroy Run with active routes")
            route_ids = {
                route_id
                for route_id, record in self._routes.items()
                if record.lease.run_id == run_id
            }
            affinity_keys = {
                self._routes[route_id].lease.affinity_key_hash
                for route_id in route_ids
            }
            for route_id in route_ids:
                del self._routes[route_id]
            for affinity_key in affinity_keys:
                self._affinity.pop(affinity_key, None)
            for key in [key for key in self._attempt_routes if key[0] == run_id]:
                del self._attempt_routes[key]
            return len(route_ids)

    def _finish(
        self,
        record: _RouteRecord,
        status: ModelRouteLeaseStatus,
        now_ms: int,
        reason: str,
    ) -> None:
        self.instances.release_route(
            record.lease.instance_id, record.lease.instance_generation
        )
        record.status = status
        record.finished_at_ms = now_ms
        record.finish_reason = reason
        self._emit(
            record.lease,
            f"model_route_{status.value}",
            {"reason": reason},
        )

    def _emit(
        self,
        lease: ModelRouteLease,
        event_type: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        if self.event_sink is None:
            return
        self.event_sink(
            ModelControlEvent(
                event_type=event_type,
                occurred_at_ms=self.clock.monotonic_ms(),
                model_id=lease.model_id,
                instance_id=lease.instance_id,
                instance_generation=lease.instance_generation,
                route_lease_id=lease.route_lease_id,
                run_id=lease.run_id,
                task_id=lease.task_id,
                attempt=lease.attempt,
                payload=self._event_payload(payload),
            )
        )

    def _emit_rejection(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        model_id: str,
        reason: str,
        started_ns: int,
    ) -> None:
        if self.event_sink is None:
            return
        self.event_sink(
            ModelControlEvent(
                event_type="model_route_rejected",
                occurred_at_ms=self.clock.monotonic_ms(),
                model_id=model_id,
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                payload=self._event_payload(
                    {
                        "reason": reason,
                        "acquire_duration_ms": (
                            perf_counter_ns() - started_ns
                        )
                        // 1_000_000,
                    }
                ),
            )
        )

    @staticmethod
    def _event_payload(payload: dict[str, object] | None) -> FrozenMap:
        frozen = freeze_canonical(payload or {})
        assert isinstance(frozen, FrozenMap)
        return frozen

    def _require(self, route_lease_id: str) -> _RouteRecord:
        try:
            return self._routes[route_lease_id]
        except KeyError as exc:
            raise KeyError(f"unknown ModelRouteLease: {route_lease_id}") from exc
