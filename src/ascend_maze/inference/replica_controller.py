"""Demand-driven deterministic replica reconciliation without prediction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
from threading import RLock
from typing import Callable

from ascend_maze.core.clock import Clock, SystemClock
from ascend_maze.core.canonical import FrozenMap, freeze_canonical
from ascend_maze.core.errors import StateTransitionError
from ascend_maze.core.identifiers import stable_id
from ascend_maze.inference.catalog import ModelCatalog
from ascend_maze.inference.contracts import (
    ModelControlEvent,
    ModelDemand,
    ModelInstanceState,
)
from ascend_maze.inference.instance_manager import ModelInstanceManager
from ascend_maze.inference.router import InferenceRouter
from ascend_maze.placement import NodeStatus


@dataclass(slots=True)
class _ScaleState:
    pressure_since_ms: int | None = None
    last_scale_at_ms: int | None = None


class ReplicaController:
    def __init__(
        self,
        *,
        catalog: ModelCatalog,
        instances: ModelInstanceManager,
        router: InferenceRouter,
        event_sink: Callable[[ModelControlEvent], None] | None = None,
        clock: Clock | None = None,
        reconcile_interval_ms: int = 100,
    ) -> None:
        if (
            isinstance(reconcile_interval_ms, bool)
            or not isinstance(reconcile_interval_ms, int)
            or reconcile_interval_ms < 1
        ):
            raise ValueError("reconcile_interval_ms must be positive")
        self.catalog = catalog
        self.instances = instances
        self.router = router
        self.event_sink = event_sink
        self.clock = clock or SystemClock()
        self.reconcile_interval_ms = reconcile_interval_ms
        self._demands: dict[str, ModelDemand] = {}
        self._demand_keys: dict[tuple[str, str, str, str], str] = {}
        self._scale = {spec.model_id: _ScaleState() for spec in catalog.specs}
        self._start_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_tasks: dict[str, asyncio.Task[None]] = {}
        self._reconcile_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._closing = False
        self._draining_nodes: set[tuple[str, str]] = set()
        self._lock = RLock()

    async def start(self) -> None:
        if self._runner is not None:
            return
        self._closing = False
        await self.reconcile()
        self._runner = asyncio.create_task(self._run_loop())

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        runner = self._runner
        self._runner = None
        if runner is not None:
            await runner
        await self.wait_for_background()

    async def abandon(self) -> None:
        """Stop reconciliation without draining model instances."""

        self._closing = True
        self._wake.set()
        runner = self._runner
        self._runner = None
        if runner is not None:
            await runner
        with self._lock:
            tasks = tuple(self._start_tasks.values()) + tuple(
                self._stop_tasks.values()
            )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def register_demand(
        self,
        *,
        run_id: str,
        task_id: str,
        model_id: str,
        registered_at_ms: int | None = None,
    ) -> ModelDemand:
        spec = self.catalog.get(model_id)
        key = (run_id, task_id, model_id, spec.catalog_revision)
        with self._lock:
            existing_id = self._demand_keys.get(key)
            if existing_id is not None:
                return self._demands[existing_id]
            now = (
                self.clock.monotonic_ms()
                if registered_at_ms is None
                else registered_at_ms
            )
            demand = ModelDemand(
                demand_id=stable_id("model_demand", *key),
                run_id=run_id,
                task_id=task_id,
                model_id=model_id,
                catalog_revision=spec.catalog_revision,
                registered_at_ms=now,
            )
            self._demands[demand.demand_id] = demand
            self._demand_keys[key] = demand.demand_id
        self._emit(
            "model_demand_registered",
            model_id,
            run_id=run_id,
            task_id=task_id,
        )
        self._wake.set()
        return demand

    def remove_demand(
        self,
        *,
        run_id: str,
        task_id: str,
        model_id: str,
    ) -> bool:
        spec = self.catalog.get(model_id)
        key = (run_id, task_id, model_id, spec.catalog_revision)
        with self._lock:
            demand_id = self._demand_keys.pop(key, None)
            if demand_id is None:
                return False
            del self._demands[demand_id]
        self._emit(
            "model_demand_removed",
            model_id,
            run_id=run_id,
            task_id=task_id,
        )
        self._wake.set()
        return True

    def remove_run(self, run_id: str) -> int:
        with self._lock:
            demands = tuple(
                demand for demand in self._demands.values() if demand.run_id == run_id
            )
        removed = 0
        for demand in demands:
            removed += self.remove_demand(
                run_id=demand.run_id,
                task_id=demand.task_id,
                model_id=demand.model_id,
            )
        return removed

    def demands(self, model_id: str | None = None) -> tuple[ModelDemand, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        demand
                        for demand in self._demands.values()
                        if model_id is None or demand.model_id == model_id
                    ),
                    key=lambda item: item.demand_id,
                )
            )

    def pending_count(self, model_id: str) -> int:
        return sum(
            self.router.route_for_task(demand.run_id, demand.task_id) is None
            for demand in self.demands(model_id)
        )

    async def reconcile(self) -> None:
        async with self._reconcile_lock:
            for spec in self.catalog.specs:
                await self._reconcile_model(spec.model_id)

    def wake(self) -> None:
        self._wake.set()

    def begin_node_drain(self, node_id: str, boot_id: str) -> None:
        with self._lock:
            self._draining_nodes.add((node_id, boot_id))
        self._wake.set()

    def end_node_drain(self, node_id: str, boot_id: str) -> None:
        with self._lock:
            self._draining_nodes.discard((node_id, boot_id))
        self._wake.set()

    def node_is_draining(self, node_id: str | None, boot_id: str | None) -> bool:
        if node_id is None or boot_id is None:
            return False
        with self._lock:
            return (node_id, boot_id) in self._draining_nodes

    async def wait_for_background(self) -> None:
        while True:
            with self._lock:
                tasks = tuple(self._start_tasks.values()) + tuple(
                    self._stop_tasks.values()
                )
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_loop(self) -> None:
        interval = self.reconcile_interval_ms / 1_000
        while not self._closing:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if not self._closing:
                try:
                    await self.reconcile()
                except Exception as exc:
                    for spec in self.catalog.specs:
                        self._emit(
                            "model_reconcile_failed",
                            spec.model_id,
                            payload={
                                "exception_type": type(exc).__name__,
                                "message": str(exc),
                            },
                        )

    async def _reconcile_model(self, model_id: str) -> None:
        spec = self.catalog.get(model_id)
        now = self.clock.monotonic_ms()
        instances = self.instances.instances(model_id=model_id)
        available_start_slots = max(
            0,
            spec.max_parallel_starts - self._active_start_count(model_id),
        )
        waiting_to_start = [
            instance
            for instance in instances
            if instance.state
            in {
                ModelInstanceState.REQUESTED,
                ModelInstanceState.RESERVING,
            }
        ]
        for instance in waiting_to_start[:available_start_slots]:
            self._schedule_start(instance.instance_id)
        instances = self.instances.instances(model_id=model_id)
        pending = self.pending_count(model_id)
        route_occupancy = sum(item.route_occupancy for item in instances)
        effective_requests = pending + route_occupancy
        desired_for_load = math.ceil(
            effective_requests
            / max(1, spec.request_capacity * spec.target_route_utilization)
        )
        desired = min(
            spec.max_replicas,
            max(spec.min_replicas, desired_for_load),
        )
        live_states = {
            ModelInstanceState.REQUESTED,
            ModelInstanceState.RESERVING,
            ModelInstanceState.STARTING,
            ModelInstanceState.WARMING,
            ModelInstanceState.READY,
            ModelInstanceState.DRAINING,
        }
        live = [item for item in instances if item.state in live_states]
        ready = [item for item in instances if item.state is ModelInstanceState.READY]
        draining = [
            item for item in instances if item.state is ModelInstanceState.DRAINING
        ]
        if effective_requests and draining:
            for instance in draining:
                if len(ready) >= desired:
                    break
                if self.node_is_draining(instance.node_id, instance.boot_id):
                    continue
                if self.instances.cancel_drain(
                    instance.instance_id, instance.generation
                ):
                    ready.append(self.instances.snapshot(instance.instance_id))

        scale = self._scale[model_id]
        nodes = self.instances.placement.snapshot().nodes
        scale_up_allowed = not nodes or any(
            node.status is NodeStatus.HEALTHY for node in nodes
        )
        needs_scale_up = scale_up_allowed and desired > len(live) and (
            pending >= spec.scale_up_pending_threshold or not ready
        )
        if needs_scale_up:
            if scale.pressure_since_ms is None:
                scale.pressure_since_ms = now
            cooldown_done = (
                scale.last_scale_at_ms is None
                or now - scale.last_scale_at_ms >= spec.scale_cooldown_ms
            )
            sustained = now - scale.pressure_since_ms >= spec.scale_up_sustain_ms
            if cooldown_done and sustained:
                active_starts = self._active_start_count(model_id)
                starts = min(
                    desired - len(live),
                    max(0, spec.max_parallel_starts - active_starts),
                    spec.max_replicas - len(live),
                )
                stopped = iter(
                    sorted(
                        (
                            item
                            for item in instances
                            if item.state is ModelInstanceState.STOPPED
                        ),
                        key=lambda item: item.instance_id,
                    )
                )
                for _ in range(max(0, starts)):
                    reusable = next(stopped, None)
                    requested = (
                        self.instances.create_requested(model_id)
                        if reusable is None
                        else self.instances.restart_stopped(reusable.instance_id)
                    )
                    self._schedule_start(requested.instance_id)
                if starts:
                    scale.last_scale_at_ms = now
                    self._emit(
                        "model_scale_up",
                        model_id,
                        payload={
                            "reason": "pending_or_utilization",
                            "target_replicas": desired,
                            "actual_replicas": len(live),
                            "started": starts,
                            "pending_demand": pending,
                            "route_occupancy": route_occupancy,
                            "cooldown_until_ms": now + spec.scale_cooldown_ms,
                        },
                    )
        else:
            scale.pressure_since_ms = None

        instances = self.instances.instances(model_id=model_id)
        ready = [item for item in instances if item.state is ModelInstanceState.READY]
        excess = len(ready) - desired
        cooldown_done = (
            scale.last_scale_at_ms is None
            or now - scale.last_scale_at_ms >= spec.scale_cooldown_ms
        )
        if excess > 0 and cooldown_done:
            candidates = sorted(
                (
                    item
                    for item in ready
                    if item.route_occupancy == 0
                    and item.actual_request_inflight == 0
                    and now - item.last_used_at_ms >= spec.scale_down_idle_ms
                ),
                key=lambda item: (
                    self.router.affinity_count(
                        item.instance_id, item.generation
                    )
                    > 0,
                    item.last_used_at_ms,
                    item.instance_id,
                ),
            )
            selected = candidates[:excess]
            for instance in selected:
                self.router.forget_instance_affinity(
                    instance.instance_id, instance.generation
                )
                self.instances.begin_drain(instance.instance_id, instance.generation)
            for instance in selected:
                self._schedule_stop(instance.instance_id, instance.generation)
            if selected:
                scale.last_scale_at_ms = now
                self._emit(
                    "model_scale_down",
                    model_id,
                    payload={
                        "reason": "idle_capacity",
                        "target_replicas": desired,
                        "actual_replicas": len(ready),
                        "drained": len(selected),
                        "cooldown_until_ms": now + spec.scale_cooldown_ms,
                    },
                )

        for instance in self.instances.instances(
            model_id=model_id,
            states=frozenset(
                {ModelInstanceState.DRAINING, ModelInstanceState.FAILED}
            ),
        ):
            self.instances.check_cleanup_timeout(
                instance.instance_id, instance.generation
            )
            self._schedule_stop(instance.instance_id, instance.generation)

    def _schedule_start(self, instance_id: str) -> bool:
        with self._lock:
            existing = self._start_tasks.get(instance_id)
            if existing is not None and not existing.done():
                return False
            self._start_tasks[instance_id] = asyncio.create_task(
                self._start_instance(instance_id)
            )
            return True

    def _active_start_count(self, model_id: str) -> int:
        with self._lock:
            return sum(
                not task.done()
                and self.instances.spec_for_instance(instance_id).model_id
                == model_id
                for instance_id, task in self._start_tasks.items()
            )

    async def _start_instance(self, instance_id: str) -> None:
        try:
            await self.instances.start_instance(instance_id)
        finally:
            with self._lock:
                current = self._start_tasks.get(instance_id)
                if current is asyncio.current_task():
                    del self._start_tasks[instance_id]
            if self.instances.snapshot(instance_id).state is ModelInstanceState.READY:
                self._wake.set()

    def _schedule_stop(self, instance_id: str, generation: int) -> bool:
        with self._lock:
            existing = self._stop_tasks.get(instance_id)
            if existing is not None and not existing.done():
                return False
            self._stop_tasks[instance_id] = asyncio.create_task(
                self._stop_instance(instance_id, generation)
            )
            return True

    async def _stop_instance(self, instance_id: str, generation: int) -> None:
        try:
            await self.instances.stop_if_drained(instance_id, generation)
        except StateTransitionError:
            if self.instances.snapshot(instance_id).generation == generation:
                raise
        finally:
            with self._lock:
                current = self._stop_tasks.get(instance_id)
                if current is asyncio.current_task():
                    del self._stop_tasks[instance_id]
            if (
                self.instances.snapshot(instance_id).state
                is ModelInstanceState.STOPPED
            ):
                self._wake.set()

    def _emit(
        self,
        event_type: str,
        model_id: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        if self.event_sink is None:
            return
        self.event_sink(
            ModelControlEvent(
                event_type=event_type,
                occurred_at_ms=self.clock.monotonic_ms(),
                model_id=model_id,
                run_id=run_id,
                task_id=task_id,
                payload=self._event_payload(payload),
            )
        )

    @staticmethod
    def _event_payload(payload: dict[str, object] | None) -> FrozenMap:
        frozen = freeze_canonical(payload or {})
        assert isinstance(frozen, FrozenMap)
        return frozen
