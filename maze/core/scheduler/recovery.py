"""Attempt cleanup and bounded recovery, independent of resource estimation."""

from __future__ import annotations

import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import ray
from maze.core.resource_policy import ResourceContext, recommend_resources


def plan_model_load_retry(
    message: dict, failure: dict | None, *, cleaned: bool, cancelled: bool
) -> dict | None:
    if (
        not cleaned
        or cancelled
        or (failure or {}).get("device_type") != "cuda"
        or message.get("_oom_retries", 0) >= 1
    ):
        return None
    context = ResourceContext.from_dict(message["resource_context"])
    request = {
        "cpu_num": message.get("cpu_nums", 1),
        "gpu_mem": message.get("gpu_mem", 0),
    }
    limit = (message.get("model_anchor") or {}).get("gpu_mem_limit")
    if limit:
        request["gpu_mem_limit"] = limit
    decision = recommend_resources(context, request, failure=failure)
    return {
        **message,
        "gpu_mem": decision.effective["gpu_mem"],
        "_oom_retries": 1,
        "resource_decision": decision.to_dict(),
    }


def prepare_cleanup(task, repair: dict, *, instances, stop_instance, shutting_down):
    if repair["scope"] == "task":
        identity = dict(getattr(task, "worker_identity", {}) or {})
        return lambda: confirm_worker_cleanup(identity)
    route = task.failed_model_route or {}
    identifier = route.get("instance_id")
    if not identifier or identifier != task.last_error.get("memory_failure", {}).get(
        "instance_id"
    ):
        raise RuntimeError("OOM instance identity does not match the failed route")
    detail = instances.get_instance_resource_detail(identifier)
    if route.get("generation_id") and route["generation_id"] != detail.get(
        "generation_id"
    ):
        raise RuntimeError("Ignoring OOM from an earlier model generation")
    if instances.prepare_memory_recovery(identifier):
        repair.update(
            type="instance_backpressure", adjusted_resources=dict(task.resources)
        )
        repair["resource_decision"]["effective"] = dict(task.resources)
        repair["resource_decision"]["sources"] = ["admission_backpressure"]
        return lambda: instances.wait_for_admission(identifier, cancelled=shutting_down)
    return lambda: stop_instance(identifier)


def process_start_ticks(pid: int) -> str | None:
    try:
        tail = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if tail[0] == "Z" else tail[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


@ray.remote(num_cpus=0)
def wait_worker_exit(identity: dict, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_start_ticks(int(identity["pid"])) != identity["start_ticks"]:
            return True
        time.sleep(0.05)
    raise RuntimeError(
        "Failed attempt worker is still alive; its GPU lease remains reserved"
    )


def confirm_worker_cleanup(identity: dict) -> None:
    if not identity.get("node_id") or not identity.get("start_ticks"):
        raise RuntimeError("Cannot verify failed GPU worker identity")
    ref = wait_worker_exit.options(
        scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
            identity["node_id"], soft=False
        )
    ).remote(identity)
    ray.get(ref, timeout=20)


@dataclass
class PendingRecovery:
    task: Any
    identity: tuple
    error: dict
    repair: dict
    future: Future


class RecoveryCoordinator:
    """Runs cleanup off the scheduler lock and returns results to its owner thread."""

    def __init__(self):
        self.executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="maze-resource-recovery"
        )
        self.pending: list[PendingRecovery] = []

    def submit(
        self, task: Any, error: dict, repair: dict, cleanup: Callable[[], None]
    ) -> None:
        if len(self.pending) >= 16:
            raise RuntimeError("Resource recovery queue is full")
        task.status = "recovering"
        identity = (task.attempt, task.dispatch_id, task.lease_id)
        self.pending.append(
            PendingRecovery(
                task, identity, error, repair, self.executor.submit(cleanup)
            )
        )

    def completed(self) -> list[PendingRecovery]:
        done, waiting = [], []
        for item in self.pending:
            (done if item.future.done() else waiting).append(item)
        self.pending = waiting
        return done

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)
