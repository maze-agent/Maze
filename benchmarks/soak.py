"""Opt-in mixed CPU/async soak; writes results only to an explicit output path."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any

import ray

from examples.arithmetic import spec as arithmetic
from maze.engine import Engine, EngineError
from maze.spec import DagSpec, EngineConfig, InvokeRequest, Limits


def delay_spec() -> DagSpec:
    return DagSpec.model_validate(
        {
            "schema": "maze.dag/v1",
            "name": "soak_delay",
            "inputs": {"x": {"type": "number"}, "seconds": {"type": "number"}},
            "nodes": [
                {
                    "id": "delay",
                    "callable": "maze_examples.arithmetic:delay",
                    "execution": "async_actor",
                    "resources": {"num_cpus": 0},
                    "inputs": {"x": {"type": "number"}, "seconds": {"type": "number"}},
                    "outputs": {"value": {"type": "number"}},
                }
            ],
            "edges": [
                {"from": {"input": name}, "to": {"node": "delay", "port": name}}
                for name in ("x", "seconds")
            ],
            "outputs": {
                "value": {"from": {"node": "delay", "port": "value"}, "type": "number"}
            },
        }
    )


def memory_sample() -> dict[str, int]:
    """RSS includes shared pages; descendants belong to this benchmark process."""
    rows = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss=,comm="], text=True)
    processes = {
        int(pid): (int(parent), int(rss), Path(command).name)
        for pid, parent, rss, command in (
            line.split(None, 3) for line in rows.splitlines() if line.strip()
        )
    }
    owned = {os.getpid()}
    while (
        children := {
            pid for pid, (parent, _, _) in processes.items() if parent in owned
        }
        - owned
    ):
        owned.update(children)
    return {
        "driver_rss_kib": processes[os.getpid()][1],
        "descendant_rss_kib": sum(
            processes[pid][1] for pid in owned if pid != os.getpid()
        ),
        "descendant_processes": len(owned) - 1,
        "gcs_rss_kib": sum(
            processes[pid][1] for pid in owned if processes[pid][2] == "gcs_server"
        ),
    }


def latency(values: deque[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    return (
        {
            "retained_samples": len(ordered),
            **{
                f"p{p}_ms": ordered[
                    min(len(ordered) - 1, int((len(ordered) - 1) * p / 100))
                ]
                for p in (50, 95, 99)
            },
        }
        if ordered
        else {"retained_samples": 0}
    )


async def run(
    seconds: float, concurrency: int, sample_s: float, warmup: int
) -> dict[str, Any]:
    engine = Engine(
        EngineConfig(
            limits=Limits(
                max_active_invocations=concurrency, max_waiting_invocations=concurrency
            )
        )
    )
    counts: Counter[str] = Counter()
    times = {kind: deque(maxlen=2048) for kind in ("success", "cancel", "timeout")}
    samples: list[dict[str, Any]] = []
    requests = 0
    redeployments = 0
    try:
        cpu = await engine.deploy(arithmetic)
        io = await engine.deploy(delay_spec())
    except BaseException:
        await engine.close()
        raise

    async def call(index: int, measured: bool) -> None:
        nonlocal requests
        requests += 1
        kind = (
            "cancel" if index % 8 == 0 else "timeout" if index % 8 == 1 else "success"
        )
        key = io if kind != "success" or index % 2 else cpu
        inputs = {
            "x": index,
            **({"seconds": 10 if kind != "success" else 0.005} if key == io else {}),
        }
        body = InvokeRequest(
            input=inputs,
            invocation_id=f"soak_{requests}",
            timeout_s=0.04 if kind == "timeout" else 30,
            metrics="none",
        )
        started = time.perf_counter()
        task = asyncio.create_task(engine.invoke(key, body))
        if kind == "cancel":
            await asyncio.sleep(0.01)
            engine.cancel(body.invocation_id)
        try:
            result = await task
            assert kind == "success", "Expected cancellation/deadline"
            assert result["output"] == (
                {"result": 3 * index + 1} if key == cpu else {"value": index}
            )
            outcome = "succeeded"
        except EngineError as exc:
            expected = {
                "cancel": "INVOCATION_CANCELLED",
                "timeout": "INVOCATION_TIMEOUT",
            }
            assert exc.code == expected.get(kind), f"Unexpected {exc.code} for {kind}"
            assert exc.cleanup == "complete", "Remote cleanup incomplete"
            outcome = exc.code
        if measured:
            counts[outcome] += 1
            times[kind].append((time.perf_counter() - started) * 1000)

    def quiescent() -> None:
        assert not engine.requests and not engine.waiting
        assert engine.active == engine.inflight == 0
        assert len(engine.deployments) == 2

    try:
        for batch in range(warmup):
            await asyncio.gather(
                *(call(batch * concurrency + n, False) for n in range(concurrency))
            )
            quiescent()
        started = time.monotonic()
        next_sample, next_deploy = started, started + 10
        batch = 0
        while time.monotonic() - started < seconds:
            await asyncio.gather(
                *(call(batch * concurrency + n, True) for n in range(concurrency))
            )
            batch += 1
            quiescent()
            now = time.monotonic()
            if now >= next_deploy:
                await engine.undeploy(io)
                io = await engine.deploy(delay_spec())
                redeployments += 1
                quiescent()
                next_deploy = time.monotonic() + 10
            if now >= next_sample:
                samples.append(
                    {
                        "elapsed_s": time.monotonic() - started,
                        **memory_sample(),
                        "active": engine.active,
                        "waiting": len(engine.waiting),
                        "inflight": engine.inflight,
                        "requests": len(engine.requests),
                        "deployments": len(engine.deployments),
                    }
                )
                print(
                    json.dumps({"progress": samples[-1], "outcomes": dict(counts)}),
                    flush=True,
                )
                next_sample = now + sample_s
        elapsed = time.monotonic() - started
        samples.append({"elapsed_s": elapsed, **memory_sample()})
        trends = {}
        for field in ("driver_rss_kib", "descendant_rss_kib", "gcs_rss_kib"):
            slope = statistics.linear_regression(
                [s["elapsed_s"] for s in samples], [s[field] for s in samples]
            ).slope
            trends[field] = {
                "first": samples[0][field],
                "last": samples[-1][field],
                "slope_kib_per_minute": slope * 60,
            }
        return {
            "elapsed_s": elapsed,
            "outcomes": dict(counts),
            "attempts_per_s": sum(counts.values()) / elapsed,
            "successful_per_s": counts["succeeded"] / elapsed,
            "unexpected_errors": 0,
            "cleanup_checks_passed": batch,
            "actor_redeployments": redeployments,
            "latency": {k: latency(v) for k, v in times.items()},
            "memory": trends,
            "samples": samples,
            "limitations": [
                "RSS sums shared pages and includes benchmark bookkeeping",
                "Ray ObjectRef totals and object-store usage not measured",
                "No HTTP/LLM/GPU traffic; CPU tasks and asyncio delay only",
                "A short soak does not establish long-term leak freedom",
            ],
        }
    finally:
        await engine.close()
        assert not engine.deployments and not engine.requests


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=300)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--sample-s", type=float, default=10)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument(
        "--task-event-capacity",
        type=int,
        help="Diagnostic GCS event retention limit for pinned Ray 2.58.0",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        not math.isfinite(args.seconds)
        or not math.isfinite(args.sample_s)
        or args.seconds < 1
        or args.concurrency < 2
        or args.concurrency > 32
        or args.sample_s <= 0
        or args.warmup < 1
    ):
        parser.error(
            "Use seconds>=1, concurrency 2..32, positive sample interval and warmup>=1"
        )
    if ray.is_initialized() or os.environ.get("RAY_ADDRESS"):
        parser.error("Requires an unattached benchmark process")
    if (
        args.task_event_capacity is not None
        and not 1 <= args.task_event_capacity <= 1000000
    ):
        parser.error("task-event-capacity must be between 1 and 1000000")
    if args.task_event_capacity is not None and os.environ.get(
        "RAY_task_events_max_num_task_in_gcs", str(args.task_event_capacity)
    ) != str(args.task_event_capacity):
        parser.error("Conflicting task event capacity in environment and CLI")
    overrides = (
        {
            "_system_config": {
                "task_events_max_num_task_in_gcs": args.task_event_capacity
            }
        }
        if args.task_event_capacity is not None
        else {}
    )
    try:
        ray.init(
            address="local",
            num_cpus=2,
            object_store_memory=256 * 1024**2,
            include_dashboard=False,
            **overrides,
        )
        result = asyncio.run(
            run(args.seconds, args.concurrency, args.sample_s, args.warmup)
        )
    finally:
        ray.shutdown()
    result["environment"] = {
        "python": platform.python_version(),
        "ray": ray.__version__,
        "platform": platform.platform(),
        "logical_cpus": 2,
        "object_store_bytes": 256 * 1024**2,
    }
    result["configuration"] = {
        "seconds": args.seconds,
        "concurrency": args.concurrency,
        "sample_s": args.sample_s,
        "warmup_batches": args.warmup,
        "task_events_report_interval_ms": os.environ.get(
            "RAY_task_events_report_interval_ms", "Ray default"
        ),
        "task_events_max_num_task_in_gcs": (
            args.task_event_capacity
            if args.task_event_capacity is not None
            else os.environ.get("RAY_task_events_max_num_task_in_gcs", "Ray default")
        ),
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Report: {args.output}", flush=True)


if __name__ == "__main__":
    main()
