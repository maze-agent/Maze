"""Ray endpoint adapter for the backend-neutral C10 Worker Pool."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from typing import Any

import ray
from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

from ascend_maze.contracts.runtime import RuntimeNodeBinding
from ascend_maze.contracts.worker import (
    StandbyWarmupReport,
    WorkerPoolProfileConfig,
    WorkerProfile,
)
from ascend_maze.core.time import monotonic_time_ms
from ascend_maze.runtime.ray_worker import (
    RAY_STANDBY_WORKER,
)


@dataclass(frozen=True, slots=True)
class _RayWorkerEndpoint:
    actor: Any
    ray_node_id: str
    worker_pid: int


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_RAY_PROCESS_PROBE: Any = ray.remote(
    num_cpus=0,
    max_retries=0,
    max_calls=1,
)(_process_exists)


async def _thread_call_before(
    deadline_ms: int,
    function: Any,
    *args: object,
    max_wait_seconds: float | None = None,
) -> Any:
    remaining = (deadline_ms - monotonic_time_ms()) / 1_000
    if remaining <= 0:
        raise asyncio.TimeoutError
    if max_wait_seconds is not None:
        remaining = min(remaining, max_wait_seconds)
    return await asyncio.wait_for(
        asyncio.to_thread(function, *args),
        timeout=remaining,
    )


class RayWorkerEndpointFactory:
    async def start(
        self,
        *,
        worker_id: str,
        worker_generation: int,
        binding: RuntimeNodeBinding,
        config: WorkerPoolProfileConfig,
        deadline_ms: int,
    ) -> tuple[Any, StandbyWarmupReport]:
        remaining = deadline_ms - monotonic_time_ms()
        if remaining <= 0:
            raise TimeoutError("worker acquire deadline expired before spawn")
        actor = RAY_STANDBY_WORKER.options(
            scheduling_strategy=NodeAffinitySchedulingStrategy(
                binding.ray_node_id, soft=False
            ),
            name=f"maze:standby:{worker_id}",
        ).remote(
            worker_id=worker_id,
            worker_generation=worker_generation,
            profile=config.profile,
            warmup_manifest=config.warmup_manifest,
            max_tasks_per_worker=config.max_tasks_per_worker,
            max_worker_lifetime_ms=config.max_worker_lifetime_ms,
            max_rss_growth_mb=config.max_rss_growth_mb,
        )
        try:
            report = await asyncio.wait_for(
                asyncio.to_thread(ray.get, actor.ready.remote()),
                timeout=remaining / 1000,
            )
            if not isinstance(report, StandbyWarmupReport):
                raise TypeError("Standby Worker returned an invalid warmup report")
            if (
                report.worker_id != worker_id
                or report.worker_generation != worker_generation
                or report.ray_node_id != binding.ray_node_id
            ):
                raise RuntimeError("Standby Worker identity does not match its node binding")
            if report.forbidden_device_modules:
                raise RuntimeError(
                    "Standby warmup created an Ascend runtime context: "
                    + ", ".join(report.forbidden_device_modules)
                )
            if config.profile is WorkerProfile.NPU_HOST and (
                not report.zero_hbm_verified or report.npu_context_device_ids
            ):
                raise RuntimeError(
                    "Standby Worker failed zero-HBM validation: "
                    + (report.zero_hbm_error or repr(report.npu_context_device_ids))
                )
            return _RayWorkerEndpoint(actor, binding.ray_node_id, report.worker_pid), report
        except BaseException:
            ray.kill(actor, no_restart=True)
            raise

    def submit(self, endpoint: Any, kwargs: dict[str, object]) -> Any:
        if not isinstance(endpoint, _RayWorkerEndpoint):
            raise TypeError("invalid Ray Worker endpoint")
        return endpoint.actor.execute.remote(**kwargs)

    async def terminate(
        self,
        endpoint: Any,
        *,
        force: bool = False,
        timeout_ms: int = 10_000,
    ) -> None:
        if not isinstance(endpoint, _RayWorkerEndpoint):
            raise TypeError("invalid Ray Worker endpoint")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
            raise ValueError("timeout_ms must be a positive integer")
        deadline = monotonic_time_ms() + timeout_ms
        if force:
            ray.kill(endpoint.actor, no_restart=True)
        else:
            shutdown_ref: Any | None = None
            try:
                shutdown_ref = await _thread_call_before(
                    deadline,
                    endpoint.actor.shutdown.remote,
                    max_wait_seconds=5.0,
                )
                await _thread_call_before(
                    deadline,
                    ray.get,
                    shutdown_ref,
                    max_wait_seconds=5.0,
                )
            except asyncio.TimeoutError:
                if shutdown_ref is not None:
                    ray.cancel(shutdown_ref, force=True)
                ray.kill(endpoint.actor, no_restart=True)
            except Exception:
                # A graceful exit is reported to the caller as ActorDiedError.
                pass
        while monotonic_time_ms() < deadline:
            process_probe = _RAY_PROCESS_PROBE.options(
                scheduling_strategy=NodeAffinitySchedulingStrategy(
                    endpoint.ray_node_id, soft=False
                )
            )
            probe_ref: Any | None = None
            try:
                probe_ref = await _thread_call_before(
                    deadline,
                    process_probe.remote,
                    endpoint.worker_pid,
                )
                alive = await _thread_call_before(
                    deadline,
                    ray.get,
                    probe_ref,
                )
            except asyncio.TimeoutError:
                if probe_ref is not None:
                    ray.cancel(probe_ref, force=True)
                break
            if not alive:
                return
            await asyncio.sleep(0.05)
        ray.kill(endpoint.actor, no_restart=True)
        raise TimeoutError(
            f"Ray Worker PID {endpoint.worker_pid} did not exit within "
            f"{timeout_ms} ms cleanup deadline"
        )
