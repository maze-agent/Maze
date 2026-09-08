"""Small sequential baseline; explicitly not a throughput or GPU benchmark."""

import argparse
import json
import platform
import statistics
import time
from collections.abc import Callable

import ray

from examples.arithmetic import dag
from maze import Runtime
from maze_examples.arithmetic import double, offset


@ray.remote
def merge(a: dict[str, float], b: dict[str, float]) -> float:
    return a["value"] + b["value"]


def measure(call: Callable[[int], float], count: int) -> dict[str, float]:
    for _ in range(3):
        assert call(3) == 10
    elapsed = []
    for x in range(count):
        started = time.perf_counter()
        assert call(x) == 3 * x + 1
        elapsed.append((time.perf_counter() - started) * 1000)
    ordered = sorted(elapsed)
    return {
        "samples": count,
        "mean_ms": statistics.mean(elapsed),
        "p50_ms": statistics.median(elapsed),
        "p95_ms": ordered[min(count - 1, int(count * 0.95))],
        "p99_ms": ordered[min(count - 1, int(count * 0.99))],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    if args.samples < 10:
        parser.error("Use at least 10 samples")
    ray.init(
        address="local",
        num_cpus=2,
        object_store_memory=256 * 1024**2,
        include_dashboard=False,
    )
    try:
        runtime = Runtime(dag)
        d, o = ray.remote(double), ray.remote(offset)

        def direct(x: int) -> float:
            return ray.get(merge.remote(d.remote(x), o.remote(x)))

        result = {
            "environment": {
                "python": platform.python_version(),
                "ray": ray.__version__,
                "platform": platform.platform(),
                "logical_cpus": 2,
            },
            "warmup": 3,
            "concurrency": 1,
            "direct_ray": measure(direct, args.samples),
            "maze": measure(lambda x: runtime.invoke({"x": x})["result"], args.samples),
        }
        print(json.dumps(result, indent=2))
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()
