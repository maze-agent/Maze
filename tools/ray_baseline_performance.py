#!/usr/bin/env python3
"""Ray performance baseline for migrated benchmark workflows.

This tool builds on ``ray_baseline_smoke.py``.  The smoke runner proves the
plain-Ray correctness path; this runner adds a repeatable workload plan,
warmup, a measurement window, concurrency/load controls and performance
aggregation.  It is still an experimental baseline tool, not Ascend-Maze core.
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import random
import sys
import time
import traceback
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
TOOLS_ROOT = REPO_ROOT / "tools"
for _path in (str(TOOLS_ROOT), str(SRC_ROOT), str(REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import qwen_benchmark_smoke as qwen_smoke  # noqa: E402
import ray_baseline_smoke as smoke  # noqa: E402


RAY_PERFORMANCE_OBJECTIVE = "ray_performance_baseline"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "experiments" / "ray_baseline_performance"


class RayPerformanceError(RuntimeError):
    """Expected operational failure in the Ray performance runner."""


@dataclass(frozen=True, slots=True)
class WorkloadItem:
    item_id: str
    family: str
    stage: str
    repeat: int
    iteration: int
    sequence: int
    sample_index: int
    sample_id: str
    planned_launch_offset_ms: int = 0

    def manifest(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "family": self.family,
            "stage": self.stage,
            "repeat": self.repeat,
            "iteration": self.iteration,
            "sequence": self.sequence,
            "sample_index": self.sample_index,
            "sample_id": self.sample_id,
            "planned_launch_offset_ms": self.planned_launch_offset_ms,
        }


def _stable_item_id(
    *,
    family: str,
    stage: str,
    repeat: int,
    iteration: int,
    sequence: int,
    sample_id: str,
) -> str:
    digest = hashlib.sha256(
        f"{family}:{stage}:{repeat}:{iteration}:{sequence}:{sample_id}".encode()
    ).hexdigest()[:16]
    return f"{family}-{stage}-{repeat}-{iteration}-{sequence}-{digest}"


def _arrival_config_from_args(args: argparse.Namespace) -> dict[str, object]:
    """Return the Maze-style arrival contract used by the plan and runner."""

    arrival_ratio = getattr(args, "arrival_ratio", None)
    avg_workflow_time_seconds = float(getattr(args, "avg_workflow_time_seconds", 45.0))
    target_qps = float(getattr(args, "target_qps", 0.0))
    if arrival_ratio is not None:
        effective_arrival_rate = float(arrival_ratio) / avg_workflow_time_seconds
        source = "arrival_ratio"
    elif target_qps > 0:
        effective_arrival_rate = target_qps
        source = "target_qps"
    else:
        effective_arrival_rate = 0.0
        source = "unpaced"
    return {
        "arrival_mode": str(getattr(args, "arrival_mode", "fixed")),
        "batch_size": int(getattr(args, "batch_size", 1)),
        "arrival_ratio": None if arrival_ratio is None else float(arrival_ratio),
        "avg_workflow_time_seconds": avg_workflow_time_seconds,
        "effective_arrival_rate": effective_arrival_rate,
        "arrival_rate_source": source,
        "target_qps": target_qps,
        "seed": int(getattr(args, "seed", 42)),
    }


def _stats(values: list[float]) -> dict[str, object]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "p50": None,
            "p95": None,
            "p99": None,
        }
    ordered = sorted(float(value) for value in values)

    def percentile(p: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        rank = (len(ordered) - 1) * p
        lower = int(rank)
        upper = min(len(ordered) - 1, lower + 1)
        fraction = rank - lower
        return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction

    return {
        "count": len(ordered),
        "min": ordered[0],
        "max": ordered[-1],
        "mean": sum(ordered) / len(ordered),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
    }


def _sum_int(records: list[Mapping[str, object]], key: str) -> int:
    total = 0
    for record in records:
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


def _aggregate_records(
    records: list[Mapping[str, object]],
    *,
    measurement_started_at_ms: int,
    measurement_finished_at_ms: int,
) -> dict[str, object]:
    measurement_records = [
        record
        for record in records
        if isinstance(record.get("performance"), Mapping)
        and record["performance"].get("stage") == "measurement"  # type: ignore[index]
    ]
    succeeded = [
        record for record in measurement_records if record.get("status") == "succeeded"
    ]
    failed = [
        record for record in measurement_records if record.get("status") != "succeeded"
    ]
    workflow_latencies = [
        float(record["duration_ms"])
        for record in succeeded
        if isinstance(record.get("duration_ms"), int)
    ]
    task_latencies: list[float] = []
    task_latency_by_name: dict[str, list[float]] = {}
    inference_records: list[Mapping[str, object]] = []
    for record in succeeded:
        for task in record.get("tasks", []):
            if not isinstance(task, Mapping):
                continue
            duration = task.get("duration_ms")
            if isinstance(duration, int) and not isinstance(duration, bool):
                task_latencies.append(float(duration))
                task_name = str(task.get("task_name", "unknown"))
                task_latency_by_name.setdefault(task_name, []).append(float(duration))
        for inference in record.get("inference_records", []):
            if isinstance(inference, Mapping):
                inference_records.append(inference)

    chat_latencies = [
        float(record["duration_ms"])
        for record in inference_records
        if isinstance(record.get("duration_ms"), int)
    ]
    input_tokens = _sum_int(inference_records, "input_tokens")
    output_tokens = _sum_int(inference_records, "output_tokens")
    wall_seconds = max(
        0.001,
        (measurement_finished_at_ms - measurement_started_at_ms) / 1_000,
    )
    chat_seconds = max(0.001, sum(chat_latencies) / 1_000)
    failure_reasons: dict[str, int] = {}
    for record in failed:
        reason = str(record.get("status", "unknown"))
        failure = record.get("failure")
        if isinstance(failure, Mapping):
            reason = str(failure.get("error_code") or reason)
        elif isinstance(record.get("error"), str):
            reason = str(record["error"]).split(":", 1)[0]
        failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

    return {
        "measurement_started_at_ms": measurement_started_at_ms,
        "measurement_finished_at_ms": measurement_finished_at_ms,
        "measurement_wall_seconds": wall_seconds,
        "total": len(measurement_records),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "success_rate": 0.0
        if not measurement_records
        else len(succeeded) / len(measurement_records),
        "workflow_latency_ms": _stats(workflow_latencies),
        "task_latency_ms": _stats(task_latencies),
        "task_latency_ms_by_name": {
            name: _stats(values) for name, values in sorted(task_latency_by_name.items())
        },
        "chat_latency_ms": _stats(chat_latencies),
        "chat_request_count": len(inference_records),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "output_tokens_per_wall_second": output_tokens / wall_seconds,
        "output_tokens_per_chat_second": output_tokens / chat_seconds,
        "failure_reasons": failure_reasons,
    }


def _build_workload_items(
    *,
    family: str,
    samples: list[Any],
    warmup_iterations: int,
    measurement_iterations: int,
    repeats: int,
    arrival_mode: str = "fixed",
    batch_size: int = 1,
    measurement_window_seconds: float = 0.0,
    effective_arrival_rate: float = 0.0,
    seed: int = 42,
) -> tuple[list[WorkloadItem], list[WorkloadItem]]:
    ordered = sorted(samples, key=lambda sample: sample.sample_id)
    warmup: list[WorkloadItem] = []
    measurement: list[WorkloadItem] = []

    def make_item(
        *,
        stage: str,
        repeat: int,
        iteration: int,
        sequence: int,
        sample_index: int,
        planned_launch_offset_ms: int = 0,
    ) -> WorkloadItem:
        sample = ordered[sample_index]
        return WorkloadItem(
            item_id=_stable_item_id(
                family=family,
                stage=stage,
                repeat=repeat,
                iteration=iteration,
                sequence=sequence,
                sample_id=sample.sample_id,
            ),
            family=family,
            stage=stage,
            repeat=repeat,
            iteration=iteration,
            sequence=sequence,
            sample_index=sample_index,
            sample_id=sample.sample_id,
            planned_launch_offset_ms=planned_launch_offset_ms,
        )

    if not ordered:
        return warmup, measurement

    sequence = 0
    for iteration in range(1, warmup_iterations + 1):
        for sample_index, sample in enumerate(ordered):
            sequence += 1
            warmup.append(
                make_item(
                    stage="warmup",
                    repeat=0,
                    iteration=iteration,
                    sequence=sequence,
                    sample_index=sample_index,
                )
            )
    sequence = 0

    def planned_offset_ms(item_index: int) -> int:
        if effective_arrival_rate <= 0:
            return 0
        return int(round((item_index - 1) * 1_000 / effective_arrival_rate))

    if arrival_mode == "batch":
        for repeat in range(1, repeats + 1):
            for batch_position in range(1, batch_size + 1):
                sequence += 1
                measurement.append(
                    make_item(
                        stage="measurement",
                        repeat=repeat,
                        iteration=1,
                        sequence=sequence,
                        sample_index=(batch_position - 1) % len(ordered),
                        planned_launch_offset_ms=0,
                    )
                )
        return warmup, measurement

    if arrival_mode in {"paced", "poisson"} and measurement_window_seconds > 0:
        rng = random.Random(seed)
        offset_seconds = 0.0
        while offset_seconds < measurement_window_seconds:
            sequence += 1
            sample_index = (sequence - 1) % len(ordered)
            measurement.append(
                make_item(
                    stage="measurement",
                    repeat=1,
                    iteration=sequence,
                    sequence=sequence,
                    sample_index=sample_index,
                    planned_launch_offset_ms=int(round(offset_seconds * 1_000)),
                )
            )
            if effective_arrival_rate <= 0:
                break
            if arrival_mode == "poisson":
                offset_seconds += rng.expovariate(effective_arrival_rate)
            else:
                offset_seconds += 1.0 / effective_arrival_rate
        return warmup, measurement

    for repeat in range(1, repeats + 1):
        for iteration in range(1, measurement_iterations + 1):
            for sample_index, _sample in enumerate(ordered):
                sequence += 1
                measurement.append(
                    make_item(
                        stage="measurement",
                        repeat=repeat,
                        iteration=iteration,
                        sequence=sequence,
                        sample_index=sample_index,
                        planned_launch_offset_ms=planned_offset_ms(sequence),
                    )
                )
    return warmup, measurement


def _item_sample(item: WorkloadItem, samples: list[Any]) -> Any:
    try:
        sample = samples[item.sample_index]
    except IndexError as exc:
        raise RayPerformanceError(f"invalid workload sample index: {item}") from exc
    if sample.sample_id != item.sample_id:
        by_id = {candidate.sample_id: candidate for candidate in samples}
        try:
            return by_id[item.sample_id]
        except KeyError as exc:
            raise RayPerformanceError(f"missing workload sample: {item.sample_id}") from exc
    return sample


def _run_workload_items(
    *,
    ray_task: Any,
    service_actor: Any,
    samples: list[Any],
    items: list[WorkloadItem],
    target_model_id: str,
    run_timeout_seconds: float,
    concurrency: int,
    measurement_window_seconds: float,
    hold_until_window_deadline: bool,
    records_path: Path,
    failures_path: Path,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    records: list[dict[str, object]] = []
    launched = 0
    completed = 0
    stopped_by_window = False
    stage = "measurement" if items and items[0].stage == "measurement" else "warmup"
    started_at_ms = int(time.time() * 1_000)
    window_deadline = (
        None
        if measurement_window_seconds <= 0 or stage != "measurement"
        else time.monotonic() + measurement_window_seconds
    )
    stage_started_monotonic = time.monotonic()

    def run_item(item: WorkloadItem) -> dict[str, object]:
        sample = _item_sample(item, samples)
        launched_at_ms = int(time.time() * 1_000)
        record = smoke._run_one_sample_ray(  # noqa: SLF001
            ray_task=ray_task,
            service_actor=service_actor,
            inference_backend="vllm",
            transformers_config=None,
            sample=sample,
            target_model_id=target_model_id,
            run_timeout_seconds=run_timeout_seconds,
            run_salt=item.item_id,
        )
        record["performance"] = {
            **item.manifest(),
            "launched_at_ms": launched_at_ms,
            "recorded_at_ms": int(time.time() * 1_000),
        }
        return record

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        in_flight: dict[Any, WorkloadItem] = {}
        next_index = 0
        while in_flight or next_index < len(items):
            while len(in_flight) < concurrency and next_index < len(items):
                now = time.monotonic()
                if window_deadline is not None and now >= window_deadline:
                    stopped_by_window = True
                    next_index = len(items)
                    break
                item = items[next_index]
                planned_launch_at = (
                    stage_started_monotonic + item.planned_launch_offset_ms / 1_000
                )
                delay = planned_launch_at - now
                if delay > 0:
                    if in_flight:
                        break
                    sleep_seconds = delay
                    if window_deadline is not None:
                        sleep_seconds = min(sleep_seconds, max(0.0, window_deadline - now))
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                    continue
                future = executor.submit(run_item, item)
                in_flight[future] = item
                launched += 1
                next_index += 1
            if not in_flight:
                if next_index < len(items):
                    continue
                continue
            done, _pending = wait(tuple(in_flight), timeout=1.0, return_when=FIRST_COMPLETED)
            for future in done:
                item = in_flight.pop(future)
                try:
                    record = future.result()
                except Exception as exc:
                    record = {
                        "schema_version": 1,
                        "status": "unexpected_exception",
                        "error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(),
                        "sample": {"sample_id": item.sample_id},
                        "performance": item.manifest(),
                    }
                smoke._persist_sample_record(  # noqa: SLF001
                    records_path=records_path,
                    failures_path=failures_path,
                    record=record,
                )
                records.append(record)
                completed += 1
        if (
            hold_until_window_deadline
            and window_deadline is not None
            and time.monotonic() < window_deadline
        ):
            time.sleep(max(0.0, window_deadline - time.monotonic()))
    finished_at_ms = int(time.time() * 1_000)
    return records, {
        "stage": stage,
        "planned": len(items),
        "launched": launched,
        "completed": completed,
        "stopped_by_window": stopped_by_window,
        "held_until_window_deadline": bool(hold_until_window_deadline and window_deadline is not None),
        "started_at_ms": started_at_ms,
        "finished_at_ms": finished_at_ms,
        "duration_ms": finished_at_ms - started_at_ms,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a plain-Ray performance baseline for migrated workflows."
    )
    parser.add_argument("--data-root", type=Path, default=qwen_smoke.DEFAULT_DATA_ROOT)
    parser.add_argument("--text-model-path", type=Path, default=qwen_smoke.DEFAULT_TEXT_MODEL_PATH)
    parser.add_argument("--vision-model-path", type=Path, default=qwen_smoke.DEFAULT_VISION_MODEL_PATH)
    parser.add_argument("--python-executable", type=Path, default=qwen_smoke._default_python())  # noqa: SLF001
    parser.add_argument("--device-id", default="0")
    parser.add_argument(
        "--dataset",
        action="append",
        choices=("gaia", "openagi", "tbench"),
        default=[],
    )
    parser.add_argument("--workflow", action="append", default=[])
    parser.add_argument(
        "--family",
        action="append",
        choices=("text", "vision"),
        default=[],
        help="Default is text for first Qwen3-4B performance baseline.",
    )
    parser.add_argument("--samples-per-workflow", type=int, default=1)
    parser.add_argument("--sample-offset", type=int, default=0)
    parser.add_argument("--max-inline-file-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--text-max-model-len", type=int, default=10240)
    parser.add_argument("--vision-max-model-len", type=int, default=12288)
    parser.add_argument("--text-dtype", choices=("bfloat16", "float16"), default="bfloat16")
    parser.add_argument("--vision-dtype", choices=("bfloat16", "float16"), default="bfloat16")
    parser.add_argument("--text-gpu-memory-utilization", type=float, default=0.5)
    parser.add_argument("--vision-gpu-memory-utilization", type=float, default=0.7)
    parser.add_argument("--max-num-seqs", type=int, default=1)
    parser.add_argument("--text-max-num-batched-tokens", type=int, default=None)
    parser.add_argument("--vision-max-num-batched-tokens", type=int, default=4096)
    parser.add_argument("--startup-timeout-ms", type=int, default=600_000)
    parser.add_argument("--request-timeout-ms", type=int, default=180_000)
    parser.add_argument("--run-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--first-port", type=int, default=31640)
    parser.add_argument("--last-port", type=int, default=31720)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--vision-trust-remote-code", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--text-trust-remote-code", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--ray-address", default=None)
    parser.add_argument("--ray-task-num-cpus", type=float, default=1.0)
    parser.add_argument("--ray-namespace", default="ascend-maze-ray-performance")
    parser.add_argument("--model-actor-concurrency", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument(
        "--arrival-mode",
        choices=("fixed", "batch", "paced", "poisson"),
        default="fixed",
        help=(
            "fixed keeps the legacy finite workload semantics; batch launches one "
            "batch of --batch-size requests per repeat; paced/poisson use the "
            "effective arrival rate."
        ),
    )
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument(
        "--arrival-ratio",
        type=float,
        default=None,
        help="Maze-style load ratio. Effective arrival rate = ratio / avg workflow seconds.",
    )
    parser.add_argument(
        "--avg-workflow-time-seconds",
        type=float,
        default=45.0,
        help="Denominator for --arrival-ratio; Maze continuous-arrival examples use 45s.",
    )
    parser.add_argument(
        "--target-qps",
        type=float,
        default=0.0,
        help="Backward-compatible direct arrival rate. Do not combine with --arrival-ratio.",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--warmup-iterations", type=int, default=1)
    parser.add_argument("--measurement-iterations", type=int, default=3)
    parser.add_argument("--measurement-window-seconds", type=float, default=0.0)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--allow-sample-failures", action="store_true")
    parser.add_argument("--tbench-smoke-overrides", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--gaia-file-smoke-summary", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args(argv)
    if not args.family:
        args.family = ["text"]
    return args


def _validate_args(args: argparse.Namespace) -> None:
    smoke._validate_args(args)  # noqa: SLF001
    for name in (
        "concurrency",
        "model_actor_concurrency",
        "measurement_iterations",
        "repeats",
    ):
        value = getattr(args, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    if args.warmup_iterations < 0:
        raise SystemExit("--warmup-iterations must be non-negative")
    if args.target_qps < 0:
        raise SystemExit("--target-qps must be non-negative")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be positive")
    if args.avg_workflow_time_seconds <= 0:
        raise SystemExit("--avg-workflow-time-seconds must be positive")
    if args.arrival_ratio is not None and args.arrival_ratio < 0:
        raise SystemExit("--arrival-ratio must be non-negative")
    if args.arrival_ratio is not None and args.target_qps > 0:
        raise SystemExit("--arrival-ratio and --target-qps cannot be combined")
    if args.arrival_mode in {"paced", "poisson"}:
        arrival_config = _arrival_config_from_args(args)
        if float(arrival_config["effective_arrival_rate"]) <= 0:
            raise SystemExit(
                "--arrival-mode paced/poisson requires --arrival-ratio or --target-qps"
            )
    if args.measurement_window_seconds < 0:
        raise SystemExit("--measurement-window-seconds must be non-negative")


def _discover(args: argparse.Namespace) -> tuple[list[Any], list[Any]]:
    return qwen_smoke.discover_samples(
        data_root=args.data_root,
        datasets=set(args.dataset),
        workflows=set(args.workflow),
        families=set(args.family),
        samples_per_workflow=int(args.samples_per_workflow),
        sample_offset=int(args.sample_offset),
        max_inline_file_bytes=int(args.max_inline_file_bytes),
        tbench_smoke_overrides=bool(args.tbench_smoke_overrides),
        gaia_file_smoke_summary=bool(args.gaia_file_smoke_summary),
    )


def _build_plan(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    samples: list[Any],
    discovery_failures: list[Any],
) -> dict[str, object]:
    arrival_config = _arrival_config_from_args(args)
    by_family: dict[str, list[Any]] = {}
    for sample in samples:
        by_family.setdefault(sample.family, []).append(sample)
    family_plans: dict[str, object] = {}
    for family, family_samples in sorted(by_family.items()):
        warmup, measurement = _build_workload_items(
            family=family,
            samples=family_samples,
            warmup_iterations=int(args.warmup_iterations),
            measurement_iterations=int(args.measurement_iterations),
            repeats=int(args.repeats),
            arrival_mode=str(arrival_config["arrival_mode"]),
            batch_size=int(arrival_config["batch_size"]),
            measurement_window_seconds=float(args.measurement_window_seconds),
            effective_arrival_rate=float(arrival_config["effective_arrival_rate"]),
            seed=int(arrival_config["seed"]),
        )
        family_plans[family] = {
            "samples": [sample.manifest() for sample in sorted(family_samples, key=lambda item: item.sample_id)],
            "warmup": [item.manifest() for item in warmup],
            "measurement": [item.manifest() for item in measurement],
        }
    return {
        "schema_version": 1,
        "objective": RAY_PERFORMANCE_OBJECTIVE,
        "executor": {
            "kind": "plain_ray_task_actor",
            "dag_policy": "per_workflow_sequential_topological_order",
            "worker_max_calls": smoke.RAY_TASK_MAX_CALLS,
            "workflow_concurrency": int(args.concurrency),
            "model_actor_concurrency": int(args.model_actor_concurrency),
            "target_qps": float(args.target_qps),
            "effective_arrival_rate": float(arrival_config["effective_arrival_rate"]),
            "uses_ascend_maze_controller": False,
            "uses_ascend_maze_scheduler": False,
            "uses_ascend_maze_runtime_client": False,
        },
        "workload": {
            **arrival_config,
            "warmup_iterations": int(args.warmup_iterations),
            "measurement_iterations": int(args.measurement_iterations),
            "measurement_window_seconds": float(args.measurement_window_seconds),
            "repeats": int(args.repeats),
            "families": family_plans,
        },
        "models": {
            "text": {
                "model_id": qwen_smoke.TEXT_MODEL_ID,
                "path": str(args.text_model_path),
                "dtype": str(args.text_dtype),
                "max_model_len": int(args.text_max_model_len),
                "launch_options": smoke._launch_options_for_family("text"),  # noqa: SLF001
            },
            "vision": {
                "model_id": qwen_smoke.VISION_MODEL_ID,
                "path": str(args.vision_model_path),
                "dtype": str(args.vision_dtype),
                "max_model_len": int(args.vision_max_model_len),
                "vision_mode": "true_multimodal",
                "launch_options": smoke._launch_options_for_family("vision"),  # noqa: SLF001
            },
        },
        "data_root": str(args.data_root),
        "output_dir": str(output_dir),
        "samples": [sample.manifest() for sample in samples],
        "discovery_failures": discovery_failures,
    }


def _preflight_failed(
    *,
    output_dir: Path,
    samples: list[Any],
    discovery_failures: list[Any],
    message: str,
    extra: Mapping[str, object] | None = None,
) -> int:
    payload: dict[str, object] = {
        "schema_version": 1,
        "result": "preflight_failed",
        "message": message,
        "sample_count": len(samples),
        "discovery_failure_count": len(discovery_failures),
        "output_dir": str(output_dir),
    }
    if extra:
        payload.update(dict(extra))
    smoke._write_json(output_dir / "summary.json", payload)  # noqa: SLF001
    smoke._append_jsonl(  # noqa: SLF001
        output_dir / "preflight_failures.jsonl",
        {"event": "preflight_failed", "message": message},
    )
    smoke.emit("RAY_PERF_PREFLIGHT_FAILED", message)
    return 2


def _run_family_performance(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    family: str,
    samples: list[Any],
    service_actor_cls: Any,
    ray_task: Any,
    port: int,
    preflight: Mapping[str, object],
) -> dict[str, object]:
    import ray

    arrival_config = _arrival_config_from_args(args)
    target_model_id = qwen_smoke.VISION_MODEL_ID if family == "vision" else qwen_smoke.TEXT_MODEL_ID
    records_path = output_dir / f"{family}_performance_records.jsonl"
    failures_path = output_dir / f"{family}_performance_failures.jsonl"
    warmup, measurement = _build_workload_items(
        family=family,
        samples=samples,
        warmup_iterations=int(args.warmup_iterations),
        measurement_iterations=int(args.measurement_iterations),
        repeats=int(args.repeats),
        arrival_mode=str(arrival_config["arrival_mode"]),
        batch_size=int(arrival_config["batch_size"]),
        measurement_window_seconds=float(args.measurement_window_seconds),
        effective_arrival_rate=float(arrival_config["effective_arrival_rate"]),
        seed=int(arrival_config["seed"]),
    )
    service_config = smoke._family_service_config(  # noqa: SLF001
        args=args,
        output_dir=output_dir,
        family=family,
        port=port,
        preflight=preflight,
    )
    summary: dict[str, object] = {
        "family": family,
        "target_model_id": target_model_id,
        "sample_count": len(samples),
        "records_path": str(records_path),
        "failures_path": str(failures_path),
        "warmup_planned": len(warmup),
        "measurement_planned": len(measurement),
        "workload": arrival_config,
        "status": "not_started",
    }
    service_actor = None
    cleanup_errors: list[str] = []
    all_records: list[dict[str, object]] = []
    try:
        service_actor = service_actor_cls.remote(service_config)
        start_info = ray.get(
            service_actor.start.remote(),
            timeout=int(args.startup_timeout_ms / 1_000) + 60,
        )
        summary["service_start"] = start_info
        smoke.emit("RAY_PERF_SERVICE_START_JSON", start_info)
        if warmup:
            warmup_records, warmup_stage = _run_workload_items(
                ray_task=ray_task,
                service_actor=service_actor,
                samples=sorted(samples, key=lambda item: item.sample_id),
                items=warmup,
                target_model_id=target_model_id,
                run_timeout_seconds=float(args.run_timeout_seconds),
                concurrency=int(args.concurrency),
                measurement_window_seconds=0.0,
                hold_until_window_deadline=False,
                records_path=records_path,
                failures_path=failures_path,
            )
        else:
            warmup_records, warmup_stage = [], {
                "stage": "warmup",
                "planned": 0,
                "launched": 0,
                "completed": 0,
                "stopped_by_window": False,
                "held_until_window_deadline": False,
                "started_at_ms": int(time.time() * 1_000),
                "finished_at_ms": int(time.time() * 1_000),
                "duration_ms": 0,
            }
        all_records.extend(warmup_records)
        measurement_records, measurement_stage = _run_workload_items(
            ray_task=ray_task,
            service_actor=service_actor,
            samples=sorted(samples, key=lambda item: item.sample_id),
            items=measurement,
            target_model_id=target_model_id,
            run_timeout_seconds=float(args.run_timeout_seconds),
            concurrency=int(args.concurrency),
            measurement_window_seconds=float(args.measurement_window_seconds),
            hold_until_window_deadline=bool(
                arrival_config["arrival_mode"] in {"paced", "poisson"}
                and float(args.measurement_window_seconds) > 0
            ),
            records_path=records_path,
            failures_path=failures_path,
        )
        all_records.extend(measurement_records)
        aggregate = _aggregate_records(
            all_records,
            measurement_started_at_ms=int(measurement_stage["started_at_ms"]),
            measurement_finished_at_ms=int(measurement_stage["finished_at_ms"]),
        )
        summary.update(
            {
                "status": "completed",
                "warmup": warmup_stage,
                "measurement": measurement_stage,
                "aggregate": aggregate,
                "succeeded": aggregate["succeeded"],
                "failed": aggregate["failed"],
            }
        )
    finally:
        if service_actor is not None:
            try:
                stop_info = ray.get(service_actor.stop.remote(), timeout=120)
                summary["service_stop"] = stop_info
                smoke.emit("RAY_PERF_SERVICE_STOP_JSON", stop_info)
            except Exception as exc:
                cleanup_errors.append(f"service_stop:{type(exc).__name__}:{exc}")
                smoke.emit("RAY_PERF_SERVICE_STOP_ERROR", traceback.format_exc())
            try:
                ray.kill(service_actor, no_restart=True)
            except Exception:
                pass
    summary["cleanup_errors"] = cleanup_errors
    if cleanup_errors:
        summary["service_log_tails"] = qwen_smoke._tail_logs(  # noqa: SLF001
            output_dir / "logs" / f"{family}_vllm"
        )
    smoke._write_json(output_dir / f"{family}_performance_summary.json", summary)  # noqa: SLF001
    return summary


def run_performance(args: argparse.Namespace) -> int:
    smoke._install_repo_path()  # noqa: SLF001
    output_dir = (
        args.output_dir.expanduser().resolve(strict=False)
        if args.output_dir is not None
        else DEFAULT_OUTPUT_ROOT / f"run-{int(time.time())}"
    )
    args.data_root = args.data_root.expanduser().resolve(strict=False)
    args.text_model_path = args.text_model_path.expanduser().resolve(strict=False)
    args.vision_model_path = args.vision_model_path.expanduser().resolve(strict=False)
    args.python_executable = args.python_executable.expanduser().resolve(strict=False)

    samples, discovery_failures = _discover(args)
    arrival_config = _arrival_config_from_args(args)
    plan = _build_plan(
        args=args,
        output_dir=output_dir,
        samples=samples,
        discovery_failures=discovery_failures,
    )
    smoke._write_json(output_dir / "performance_plan.json", plan)  # noqa: SLF001
    smoke.emit("RAY_PERF_PLAN_PATH", str(output_dir / "performance_plan.json"))
    smoke.emit(
        "RAY_PERF_PLAN_JSON",
        {
            "sample_count": len(samples),
            "discovery_failure_count": len(discovery_failures),
            "families": sorted({sample.family for sample in samples}),
            "measurement_iterations": int(args.measurement_iterations),
            "concurrency": int(args.concurrency),
            "arrival_mode": str(arrival_config["arrival_mode"]),
            "batch_size": int(arrival_config["batch_size"]),
            "arrival_ratio": arrival_config["arrival_ratio"],
            "effective_arrival_rate": arrival_config["effective_arrival_rate"],
        },
    )
    if args.plan_only:
        smoke._write_json(  # noqa: SLF001
            output_dir / "summary.json",
            {
                "schema_version": 1,
                "result": "plan_only_succeeded",
                "sample_count": len(samples),
                "discovery_failure_count": len(discovery_failures),
                "output_dir": str(output_dir),
                "workload": arrival_config,
            },
        )
        smoke.emit("RAY_PERF_RESULT", "plan_only_succeeded")
        return 0
    if not samples:
        return _preflight_failed(
            output_dir=output_dir,
            samples=samples,
            discovery_failures=discovery_failures,
            message="sample discovery produced no runnable samples",
        )
    families_present = {sample.family for sample in samples}
    try:
        preflight = smoke._run_preflight(args=args, families_present=families_present)  # noqa: SLF001
    except qwen_smoke.SmokePreflightError as exc:
        return _preflight_failed(
            output_dir=output_dir,
            samples=samples,
            discovery_failures=discovery_failures,
            message=str(exc),
        )
    except Exception as exc:
        return _preflight_failed(
            output_dir=output_dir,
            samples=samples,
            discovery_failures=discovery_failures,
            message=f"{type(exc).__name__}: {exc}",
            extra={"traceback": traceback.format_exc()},
        )
    if args.check_only:
        smoke._write_json(  # noqa: SLF001
            output_dir / "summary.json",
            {
                "schema_version": 1,
                "result": "check_only_succeeded",
                "sample_count": len(samples),
                "discovery_failure_count": len(discovery_failures),
                "output_dir": str(output_dir),
                **preflight,
            },
        )
        smoke.emit("RAY_PERF_RESULT", "check_only_succeeded")
        return 0

    import ray

    summaries: list[dict[str, object]] = []
    result_code = 0
    cleanup_errors: list[str] = []
    required_model_paths: list[Path] = []
    if "text" in families_present:
        required_model_paths.append(args.text_model_path)
    if "vision" in families_present:
        required_model_paths.append(args.vision_model_path)
    try:
        ray.init(
            address=args.ray_address,
            ignore_reinit_error=True,
            include_dashboard=False,
            namespace=str(args.ray_namespace),
            runtime_env={"env_vars": {"PYTHONPATH": os.environ["PYTHONPATH"]}},
        )
        service_actor_cls = ray.remote(
            num_cpus=0,
            max_concurrency=int(args.model_actor_concurrency),
        )(smoke._VllmServiceActor)  # noqa: SLF001
        ray_task = ray.remote(
            num_cpus=float(args.ray_task_num_cpus),
            max_calls=smoke.RAY_TASK_MAX_CALLS,
        )(
            smoke._execute_workflow_task_remote  # noqa: SLF001
        )
        port_by_family = {"text": int(args.first_port), "vision": int(args.first_port) + 1}
        if "vision" in families_present and port_by_family["vision"] > int(args.last_port):
            raise RayPerformanceError("port range needs at least two ports for text+vision")
        for family in ("text", "vision"):
            family_samples = [sample for sample in samples if sample.family == family]
            if not family_samples:
                continue
            smoke.emit(
                "RAY_PERF_FAMILY_START_JSON",
                {"family": family, "sample_count": len(family_samples)},
            )
            summaries.append(
                _run_family_performance(
                    args=args,
                    output_dir=output_dir,
                    family=family,
                    samples=family_samples,
                    service_actor_cls=service_actor_cls,
                    ray_task=ray_task,
                    port=port_by_family[family],
                    preflight=preflight,
                )
            )
    except Exception:
        smoke.emit("RAY_PERF_EXCEPTION_TRACEBACK", traceback.format_exc())
        result_code = 99
    finally:
        try:
            ray.shutdown()
        except Exception as exc:
            cleanup_errors.append(f"ray_shutdown:{type(exc).__name__}:{exc}")

    residual = qwen_smoke._residual_vllm_processes(  # noqa: SLF001
        required_model_paths,
        tuple(range(int(args.first_port), int(args.last_port) + 1)),
    )
    total_failed = sum(int(summary.get("failed", 0)) for summary in summaries)
    total_succeeded = sum(int(summary.get("succeeded", 0)) for summary in summaries)
    if result_code == 0 and total_failed and not args.allow_sample_failures:
        result_code = 20
    if result_code == 0 and (residual or cleanup_errors):
        result_code = 11
    summary_payload = {
        "schema_version": 1,
        "result": (
            "succeeded"
            if result_code == 0
            else (
                "sample_failures"
                if result_code == 20
                else "cleanup_failed"
                if result_code == 11
                else f"failed:{result_code}"
            )
        ),
        "exit_code": result_code,
        "sample_count": len(samples),
        "succeeded": total_succeeded,
        "failed": total_failed,
        "discovery_failure_count": len(discovery_failures),
        "families": summaries,
        "workload": arrival_config,
        "residual_vllm_processes": residual,
        "cleanup_errors": cleanup_errors,
        "output_dir": str(output_dir),
    }
    smoke._write_json(output_dir / "summary.json", summary_payload)  # noqa: SLF001
    smoke.emit("RAY_PERF_SUMMARY_PATH", str(output_dir / "summary.json"))
    smoke.emit("RAY_PERF_SUMMARY_JSON", summary_payload)
    smoke.emit("RAY_PERF_EXIT_CODE", result_code)
    return result_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _validate_args(args)
    return run_performance(args)


if __name__ == "__main__":
    raise SystemExit(main())
