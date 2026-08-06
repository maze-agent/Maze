from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict

import ray

from maze.core.scheduler.runner import execute_code_task_in_worker


STANDBY_WORKER_RESOURCE_OPTIONS = {
    "num_cpus": 0.05,
    "num_gpus": 0,
}
DEFAULT_STANDBY_POOL_SIZES = {
    "gpu": 1,
    "cpu": 2,
    "io": 1,
}


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class StandbyWorkerLease:
    node_id: str
    worker_type: str
    actor: Any


@ray.remote(**STANDBY_WORKER_RESOURCE_OPTIONS)
class StandbyWorker:
    def __init__(self, node_id: str, worker_type: str):
        self.node_id = node_id
        self.worker_type = worker_type
        self.created_time = time.time()
        self.ready = False
        self.warmed_modules = []
        self.warmup_errors = []

        self._warm_common_modules()
        if worker_type == "gpu":
            self._warm_gpu_modules_without_vram()
        self.ready = True

    def _warm_common_modules(self):
        for module_name in ("json", "math"):
            try:
                __import__(module_name)
                self.warmed_modules.append(module_name)
            except Exception as exc:
                self.warmup_errors.append({"module": module_name, "error": str(exc)})

    def _warm_gpu_modules_without_vram(self):
        for module_name in ("torch", "transformers"):
            try:
                __import__(module_name)
                self.warmed_modules.append(module_name)
            except Exception as exc:
                self.warmup_errors.append({"module": module_name, "error": str(exc)})

    def ping(self):
        return {
            "node_id": self.node_id,
            "worker_type": self.worker_type,
            "ready": self.ready,
            "created_time": self.created_time,
            "warmed_modules": list(self.warmed_modules),
            "warmup_errors": list(self.warmup_errors),
            "zero_vram": True,
        }

    def execute_code_task(
        self,
        code_str: str = None,
        code_ser: str = None,
        task_input_data: dict = None,
        cuda_visible_devices: str | None = None,
        file_context: dict | None = None,
        model_route: dict | None = None,
    ):
        return execute_code_task_in_worker(
            code_str=code_str,
            code_ser=code_ser,
            task_input_data=task_input_data,
            cuda_visible_devices=cuda_visible_devices,
            file_context=file_context,
            model_route=model_route,
        )

class StandbyWorkerPoolManager:
    def __init__(
        self,
        pool_sizes: Dict[str, int] | None = None,
        enabled: bool = True,
        actor_factory: Callable[[str, str], Any] | None = None,
        actor_killer: Callable[[Any], None] | None = None,
    ):
        self.pool_sizes = {
            worker_type: max(0, int(count))
            for worker_type, count in (pool_sizes or DEFAULT_STANDBY_POOL_SIZES).items()
        }
        self.enabled = enabled
        self.actor_factory = actor_factory or self._start_actor
        self.actor_killer = actor_killer or ray.kill
        self.node_workers: Dict[str, Dict[str, list[Any]]] = {}
        self.busy_worker_ids: set[int] = set()

    @classmethod
    def from_env(cls):
        enabled = _env_flag("MAZE_STANDBY_WORKERS_ENABLED", True)
        sizes = dict(DEFAULT_STANDBY_POOL_SIZES)
        for worker_type in list(sizes):
            value = os.environ.get(f"MAZE_STANDBY_{worker_type.upper()}_WORKERS")
            if value is not None:
                sizes[worker_type] = max(0, int(value))
        return cls(pool_sizes=sizes, enabled=enabled)

    def _start_actor(self, node_id: str, worker_type: str):
        return StandbyWorker.options(
            scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                node_id=node_id,
                soft=True,
            ),
        ).remote(node_id=node_id, worker_type=worker_type)

    def ensure_for_nodes(self, nodes: Dict[str, Any]):
        if not self.enabled:
            return

        live_node_ids = set(nodes)
        for stale_node_id in set(self.node_workers) - live_node_ids:
            self.remove_node(stale_node_id)

        for node_id in sorted(live_node_ids):
            worker_groups = self.node_workers.setdefault(node_id, {})
            for worker_type, target_count in self.pool_sizes.items():
                workers = worker_groups.setdefault(worker_type, [])
                while len(workers) < target_count:
                    workers.append(self.actor_factory(node_id, worker_type))
                while len(workers) > target_count:
                    removable = next(
                        (actor for actor in reversed(workers) if id(actor) not in self.busy_worker_ids),
                        None,
                    )
                    if removable is None:
                        break
                    workers.remove(removable)
                    try:
                        self.actor_killer(removable)
                    except Exception:
                        pass

    def remove_node(self, node_id: str):
        worker_groups = self.node_workers.pop(node_id, {})
        for workers in worker_groups.values():
            for actor in workers:
                self.busy_worker_ids.discard(id(actor))
                try:
                    self.actor_killer(actor)
                except Exception:
                    pass

    def acquire(self, node_id: str, task_kind: str) -> StandbyWorkerLease | None:
        if not self.enabled:
            return None

        worker_type = (task_kind or "cpu").strip().lower()
        workers = self.node_workers.get(node_id, {}).get(worker_type, [])
        for actor in workers:
            actor_id = id(actor)
            if actor_id in self.busy_worker_ids:
                continue
            self.busy_worker_ids.add(actor_id)
            return StandbyWorkerLease(node_id=node_id, worker_type=worker_type, actor=actor)
        return None

    def release(self, lease: StandbyWorkerLease | None, *, discard: bool = False):
        if lease is None:
            return
        actor = lease.actor
        self.busy_worker_ids.discard(id(actor))

        if not discard and lease.worker_type != "gpu":
            return

        workers = self.node_workers.get(lease.node_id, {}).get(lease.worker_type)
        if workers is None:
            return
        try:
            workers.remove(actor)
        except ValueError:
            return

        try:
            self.actor_killer(actor)
        except Exception:
            pass

        target_count = self.pool_sizes.get(lease.worker_type, 0)
        while self.enabled and len(workers) < target_count:
            try:
                workers.append(self.actor_factory(lease.node_id, lease.worker_type))
            except Exception:
                break

    def _execution_snapshot(self):
        nodes = {}
        for node_id, worker_groups in self.node_workers.items():
            nodes[node_id] = {}
            for worker_type, workers in worker_groups.items():
                busy = len([actor for actor in workers if id(actor) in self.busy_worker_ids])
                total = len(workers)
                nodes[node_id][worker_type] = {
                    "total": total,
                    "busy": busy,
                    "idle": max(0, total - busy),
                }
        return {
            "enabled": self.enabled,
            "nodes": nodes,
        }

    def snapshot(self):
        nodes = {}
        for node_id, worker_groups in self.node_workers.items():
            nodes[node_id] = {
                worker_type: len(workers)
                for worker_type, workers in worker_groups.items()
            }
        return {
            "enabled": self.enabled,
            "resource_options": dict(STANDBY_WORKER_RESOURCE_OPTIONS),
            "target_pool_sizes": dict(self.pool_sizes),
            "nodes": nodes,
            "execution": self._execution_snapshot(),
        }
