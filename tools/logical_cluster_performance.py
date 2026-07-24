#!/usr/bin/env python3
"""Paired Ascend-Maze/plain-Ray performance pilot on the logical 8-node cluster."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import statistics
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Mapping, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility.
    import tomli as tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
TOOLS_ROOT = REPO_ROOT / "tools"
for _path in (str(TOOLS_ROOT), str(SRC_ROOT), str(REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import logical_cluster_e2e as logical_e2e  # noqa: E402
import logical_cluster_figures as logical_figures  # noqa: E402
import qwen_benchmark_smoke as qwen_smoke  # noqa: E402
import ray_baseline_smoke as ray_smoke  # noqa: E402


SCHEMA_VERSION = 1
OBJECTIVE = "logical_cluster_maze_ray_performance_pilot"
CONTAINER_NAME = "ascend-maze-logical-node-0"
CONTAINER_ENV = REPO_ROOT / "deploy" / "logical_cluster" / "container_env.sh"
DEFAULT_STATE_ROOT = (
    Path.home() / ".local" / "state" / "ascend-maze" / "logical-cluster"
)
DEFAULT_CONTROL_SOCKET = Path("/workspace/state/control-plane/control.sock")
DEFAULT_OUTPUT_ROOT = (
    DEFAULT_STATE_ROOT / "node-0" / "output" / "logical-cluster-performance"
)
TEXT_MODEL_ID = "qwen3-4b-e2e"
TEXT_MODEL_PATH = Path("/home/user2/workplace/model_weight/model_from_hf/Qwen3-4B")
VISION_MODEL_ID = "qwen2_5-vl-3b-e2e"
VISION_MODEL_PATH = Path(
    "/home/user2/workplace/model_weight/model_from_hf/Qwen2.5-VL-3B-Instruct"
)
FIXED_BATCH_SIZE = 20
REQUEST_CLEANUP_GRACE_SECONDS = 180.0
FIXED_TEXT_EXTRA_WORKFLOWS = {
    "gaia": "file",
    "openagi": "document_qa",
    "tbench": "airline_book",
}
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}


class PerformancePilotError(RuntimeError):
    """Expected pilot setup or execution failure."""


@dataclass(frozen=True, slots=True)
class WorkloadCase:
    case_id: str
    mode: str
    request_count: int
    launch_offsets_ms: tuple[int, ...]
    batch_size: int | None = None
    arrival_ratio: float | None = None
    arrival_rate_per_second: float | None = None
    average_workflow_seconds: float | None = None
    admission_window_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"batch", "arrival"}:
            raise ValueError("workload mode must be batch or arrival")
        if self.request_count < 1 or len(self.launch_offsets_ms) != self.request_count:
            raise ValueError("request_count must match launch offsets")
        if tuple(sorted(self.launch_offsets_ms)) != self.launch_offsets_ms:
            raise ValueError("launch offsets must be sorted")
        if any(item < 0 for item in self.launch_offsets_ms):
            raise ValueError("launch offsets must be non-negative")

    def payload(self) -> dict[str, object]:
        return _jsonable(asdict(self))


def _jsonable(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_jsonable(value), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PerformancePilotError(f"JSON document is not an object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _warm_model_file_cache(model_paths: Sequence[Path]) -> dict[str, object]:
    started = time.perf_counter()
    files = sorted(
        path
        for model_path in model_paths
        for path in model_path.rglob("*")
        if path.is_file()
    )
    buffer = bytearray(16 * 1024 * 1024)
    total_bytes = 0
    for path in files:
        with path.open("rb", buffering=0) as handle:
            advise = getattr(os, "posix_fadvise", None)
            will_need = getattr(os, "POSIX_FADV_WILLNEED", None)
            if callable(advise) and isinstance(will_need, int):
                advise(handle.fileno(), 0, 0, will_need)
            while True:
                size = handle.readinto(buffer)
                if not size:
                    break
                total_bytes += size
    return {
        "model_paths": [str(path) for path in model_paths],
        "file_count": len(files),
        "bytes_read": total_bytes,
        "duration_ms": round((time.perf_counter() - started) * 1_000),
        "included_in_request_e2e": False,
    }


def _stats(values: Sequence[float]) -> dict[str, float | int | None]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "p50": None,
            "p95": None,
            "p99": None,
        }

    def percentile(fraction: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        rank = (len(ordered) - 1) * fraction
        lower = math.floor(rank)
        upper = math.ceil(rank)
        if lower == upper:
            return ordered[lower]
        weight = rank - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    return {
        "count": len(ordered),
        "min": ordered[0],
        "max": ordered[-1],
        "mean": statistics.fmean(ordered),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
    }


def _arrival_offsets_ms(
    *,
    arrival_ratio: float,
    average_workflow_seconds: float,
    admission_window_seconds: float,
) -> tuple[int, ...]:
    if arrival_ratio <= 0:
        raise ValueError("arrival_ratio must be positive")
    if average_workflow_seconds <= 0 or admission_window_seconds <= 0:
        raise ValueError("arrival timing values must be positive")
    rate = arrival_ratio / average_workflow_seconds
    interval = 1.0 / rate
    offsets: list[int] = []
    offset = 0.0
    while offset < admission_window_seconds:
        offsets.append(round(offset * 1_000))
        offset += interval
    return tuple(offsets)


def _build_cases(args: argparse.Namespace) -> tuple[WorkloadCase, ...]:
    cases: list[WorkloadCase] = []
    modes = {str(item) for item in args.mode}
    if "batch" in modes:
        for size in args.batch_size:
            cases.append(
                WorkloadCase(
                    case_id=f"batch-{size}",
                    mode="batch",
                    request_count=int(size),
                    launch_offsets_ms=(0,) * int(size),
                    batch_size=int(size),
                )
            )
    if "arrival" in modes:
        for ratio in args.arrival_ratio:
            offsets = _arrival_offsets_ms(
                arrival_ratio=float(ratio),
                average_workflow_seconds=float(args.average_workflow_seconds),
                admission_window_seconds=float(args.arrival_window_seconds),
            )
            ratio_id = str(float(ratio)).replace(".", "p")
            cases.append(
                WorkloadCase(
                    case_id=f"arrival-ratio-{ratio_id}",
                    mode="arrival",
                    request_count=len(offsets),
                    launch_offsets_ms=offsets,
                    arrival_ratio=float(ratio),
                    arrival_rate_per_second=(
                        float(ratio) / float(args.average_workflow_seconds)
                    ),
                    average_workflow_seconds=float(args.average_workflow_seconds),
                    admission_window_seconds=float(args.arrival_window_seconds),
                )
            )
    if not cases:
        raise ValueError("at least one workload case is required")
    return tuple(cases)


def _execution_order(
    cases: Sequence[WorkloadCase], executor: str
) -> tuple[tuple[WorkloadCase, str, int], ...]:
    if executor in {"maze", "ray"}:
        return tuple((case, executor, 1) for case in cases)
    ordered: list[tuple[WorkloadCase, str, int]] = []
    for index, case in enumerate(cases):
        pair = ("maze", "ray") if index % 2 == 0 else ("ray", "maze")
        ordered.extend((case, name, position + 1) for position, name in enumerate(pair))
    return tuple(ordered)


def _aggregate_requests(
    records: Sequence[Mapping[str, object]],
    *,
    mode: str,
    admission_window_seconds: float | None,
) -> dict[str, object]:
    succeeded = [item for item in records if item.get("status") == "succeeded"]
    latencies = [
        float(item["client_e2e_ms"])
        for item in succeeded
        if isinstance(item.get("client_e2e_ms"), (int, float))
        and not isinstance(item.get("client_e2e_ms"), bool)
    ]
    starts = [
        int(item["client_e2e_started_at_ms"])
        for item in records
        if isinstance(item.get("client_e2e_started_at_ms"), int)
    ]
    finishes = [
        int(item["client_e2e_finished_at_ms"])
        for item in records
        if isinstance(item.get("client_e2e_finished_at_ms"), int)
    ]
    makespan_ms = max(finishes) - min(starts) if starts and finishes else 0
    completed_in_window = None
    window_throughput = None
    if mode == "arrival" and starts and admission_window_seconds is not None:
        deadline = min(starts) + round(admission_window_seconds * 1_000)
        completed_in_window = sum(
            item.get("status") == "succeeded"
            and isinstance(item.get("client_e2e_finished_at_ms"), int)
            and int(item["client_e2e_finished_at_ms"]) <= deadline
            for item in records
        )
        window_throughput = completed_in_window / admission_window_seconds
    failure_reasons: dict[str, int] = {}
    for item in records:
        if item.get("status") == "succeeded":
            continue
        reason = str(item.get("error") or item.get("status") or "unknown")
        failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
    return {
        "request_count": len(records),
        "succeeded": len(succeeded),
        "failed": len(records) - len(succeeded),
        "success_rate": len(succeeded) / len(records) if records else 0.0,
        "e2e_latency_ms": _stats(latencies),
        "p95_e2e_ms": _stats(latencies)["p95"],
        "makespan_ms": makespan_ms,
        "throughput_requests_per_second": (
            len(succeeded) / (makespan_ms / 1_000) if makespan_ms > 0 else 0.0
        ),
        "completed_in_admission_window": completed_in_window,
        "admission_window_throughput_requests_per_second": window_throughput,
        "failure_reasons": failure_reasons,
    }


TIMING_FIELDS = (
    "model_load_ms",
    "generate_ms",
    "total_duration_ms",
    "worker_startup_ms",
    "input_fetch_ms",
    "callable_ms",
    "output_put_ms",
    "task_total_ms",
    "dispatch_prepare_ms",
    "ray_roundtrip_ms",
    "queue_to_dispatch_ms",
    "dispatch_to_prepared_ms",
    "prepared_to_running_ms",
    "dispatch_to_running_ms",
)


def _aggregate_timings(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    values: dict[str, list[float]] = {name: [] for name in TIMING_FIELDS}
    for record in records:
        groups = (
            record.get("transformers_local_records", []),
            record.get("task_timings", []),
            record.get("dispatch_lifecycle", []),
        )
        for group in groups:
            if not isinstance(group, list):
                continue
            for item in group:
                if not isinstance(item, Mapping):
                    continue
                for name in TIMING_FIELDS:
                    value = item.get(name)
                    if (
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and math.isfinite(float(value))
                    ):
                        values[name].append(float(value))
    return {name: _stats(items) for name, items in values.items() if items}


def _aggregate_breakdowns(
    records: Sequence[Mapping[str, object]],
    *,
    mode: str,
    admission_window_seconds: float | None,
) -> dict[str, object]:
    def aggregate(items: Sequence[Mapping[str, object]]) -> dict[str, object]:
        return {
            "requests": _aggregate_requests(
                items,
                mode=mode,
                admission_window_seconds=admission_window_seconds,
            ),
            "timings": _aggregate_timings(items),
        }

    families: dict[str, list[Mapping[str, object]]] = {}
    workflows: dict[str, list[Mapping[str, object]]] = {}
    for record in records:
        family = str(record.get("family", "unknown"))
        workflow = f"{record.get('dataset', 'unknown')}.{record.get('workflow', 'unknown')}"
        families.setdefault(family, []).append(record)
        workflows.setdefault(workflow, []).append(record)
    return {
        "overall": aggregate(records),
        "families": {
            key: aggregate(items) for key, items in sorted(families.items())
        },
        "workflows": {
            key: aggregate(items) for key, items in sorted(workflows.items())
        },
    }


class HostResourceMonitor:
    """Sample logical-node cgroups and all physical NPUs outside the containers."""

    def __init__(
        self,
        *,
        output_path: Path,
        interval_seconds: float,
        container_prefix: str = "ascend-maze-logical-node-",
    ) -> None:
        self.output_path = output_path
        self.interval_seconds = interval_seconds
        self.container_prefix = container_prefix
        self.samples: list[dict[str, object]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._containers = self._discover_containers()
        self._previous_cpu: dict[str, tuple[int, int]] = {}
        from ascend_maze.ascend.dcmi import DcmiDeviceAdapter

        self._dcmi = DcmiDeviceAdapter()

    @staticmethod
    def _cpu_count(cpuset: str) -> int:
        total = 0
        for part in cpuset.split(","):
            if "-" in part:
                start, finish = part.split("-", 1)
                total += int(finish) - int(start) + 1
            elif part:
                total += 1
        return total

    def _discover_containers(self) -> tuple[dict[str, object], ...]:
        command = [
            "docker",
            "ps",
            "--filter",
            "label=com.ascend-maze.logical-cluster=true",
            "--format",
            "{{.Names}}",
        ]
        names = sorted(
            item.strip()
            for item in subprocess.check_output(command, text=True).splitlines()
            if item.strip().startswith(self.container_prefix)
        )
        if len(names) != 8:
            raise PerformancePilotError(
                f"expected 8 running logical containers, found {len(names)}"
            )
        containers: list[dict[str, object]] = []
        for name in names:
            payload = json.loads(
                subprocess.check_output(["docker", "inspect", name], text=True)
            )[0]
            container_id = str(payload["Id"])
            cpuset = str(payload["HostConfig"]["CpusetCpus"])
            cpu_path = (
                Path("/sys/fs/cgroup/cpu,cpuacct/docker")
                / container_id
                / "cpuacct.usage"
            )
            memory_path = (
                Path("/sys/fs/cgroup/memory/docker")
                / container_id
                / "memory.usage_in_bytes"
            )
            if not cpu_path.is_file() or not memory_path.is_file():
                raise PerformancePilotError(
                    f"logical container cgroup files are missing: {name}"
                )
            containers.append(
                {
                    "name": name,
                    "node_id": name.removeprefix(self.container_prefix),
                    "container_id": container_id,
                    "cpuset": cpuset,
                    "cpu_count": self._cpu_count(cpuset),
                    "cpu_path": cpu_path,
                    "memory_path": memory_path,
                }
            )
        return tuple(containers)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("resource monitor is already started")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text("", encoding="utf-8")
        self._sample()
        self._thread = threading.Thread(
            target=self._run,
            name="logical-cluster-resource-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> tuple[dict[str, object], ...]:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(5.0, self.interval_seconds * 2))
        self._sample()
        return tuple(self.samples)

    def _record_sample(self, sample: dict[str, object]) -> None:
        self.samples.append(sample)
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_jsonable(sample), sort_keys=True) + "\n")

    def wait_for_hbm_recovery(
        self,
        *,
        baseline_hbm_mb: int,
        timeout_seconds: float,
        tolerance_mb: int,
    ) -> dict[str, object]:
        started = time.monotonic()
        deadline = started + timeout_seconds
        stable_samples = 0
        final_hbm_mb: int | None = None
        while time.monotonic() < deadline:
            sample = self.samples[-1] if self.samples else None
            if isinstance(sample, Mapping):
                npus = sample.get("npus")
                errors = sample.get("errors")
                value = sample.get("cluster_hbm_used_mb")
                dcmi_error = isinstance(errors, list) and any(
                    str(item).startswith("dcmi:") for item in errors
                )
                if (
                    isinstance(npus, list)
                    and len(npus) == 8
                    and isinstance(value, int)
                    and not dcmi_error
                ):
                    final_hbm_mb = value
                    if value <= baseline_hbm_mb + tolerance_mb:
                        stable_samples += 1
                        if stable_samples >= 2:
                            return {
                                "recovered": True,
                                "baseline_hbm_mb": baseline_hbm_mb,
                                "final_hbm_mb": final_hbm_mb,
                                "tolerance_mb": tolerance_mb,
                                "wait_ms": round((time.monotonic() - started) * 1_000),
                            }
                    else:
                        stable_samples = 0
            time.sleep(min(1.0, self.interval_seconds))
        return {
            "recovered": False,
            "baseline_hbm_mb": baseline_hbm_mb,
            "final_hbm_mb": final_hbm_mb,
            "tolerance_mb": tolerance_mb,
            "wait_ms": round((time.monotonic() - started) * 1_000),
        }

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def _sample(self) -> None:
        timestamp_ms = int(time.time() * 1_000)
        monotonic_ns = time.monotonic_ns()
        container_samples: list[dict[str, object]] = []
        errors: list[str] = []
        for container in self._containers:
            name = str(container["name"])
            try:
                cpu_usage_ns = int(Path(container["cpu_path"]).read_text().strip())
                memory_bytes = int(Path(container["memory_path"]).read_text().strip())
                previous = self._previous_cpu.get(name)
                cpu_percent = None
                if previous is not None:
                    previous_time_ns, previous_usage_ns = previous
                    elapsed = monotonic_ns - previous_time_ns
                    if elapsed > 0:
                        cpu_percent = max(
                            0.0,
                            (cpu_usage_ns - previous_usage_ns)
                            / elapsed
                            / int(container["cpu_count"])
                            * 100,
                        )
                self._previous_cpu[name] = (monotonic_ns, cpu_usage_ns)
                container_samples.append(
                    {
                        "name": name,
                        "node_id": container["node_id"],
                        "cpu_count": container["cpu_count"],
                        "cpu_usage_ns": cpu_usage_ns,
                        "cpu_utilization_pct": cpu_percent,
                        "memory_used_bytes": memory_bytes,
                    }
                )
            except Exception as exc:
                errors.append(f"cgroup:{name}:{type(exc).__name__}:{exc}")
        npu_samples: list[dict[str, object]] = []
        try:
            for device in self._dcmi.devices():
                npu_samples.append(
                    {
                        "physical_device_id": device.physical_device_id,
                        "utilization_pct": device.utilization,
                        "used_hbm_mb": device.used_hbm_mb,
                        "total_hbm_mb": device.total_hbm_mb,
                        "processes": [asdict(item) for item in device.processes],
                    }
                )
        except Exception as exc:
            errors.append(f"dcmi:{type(exc).__name__}:{exc}")
        cpu_values = [
            float(item["cpu_utilization_pct"])
            for item in container_samples
            if isinstance(item.get("cpu_utilization_pct"), (int, float))
        ]
        npu_values = [
            float(item["utilization_pct"])
            for item in npu_samples
            if isinstance(item.get("utilization_pct"), (int, float))
        ]
        self._record_sample(
            {
                "timestamp_ms": timestamp_ms,
                "monotonic_ns": monotonic_ns,
                "containers": container_samples,
                "npus": npu_samples,
                "cluster_cpu_utilization_pct": (
                    statistics.fmean(cpu_values) if cpu_values else None
                ),
                "cluster_npu_utilization_pct": (
                    statistics.fmean(npu_values) if npu_values else None
                ),
                "max_device_npu_utilization_pct": max(npu_values, default=None),
                "cluster_hbm_used_mb": sum(
                    int(item["used_hbm_mb"]) for item in npu_samples
                ),
                "errors": errors,
            }
        )


def _aggregate_resources(
    samples: Sequence[Mapping[str, object]],
    *,
    started_at_ms: int,
    finished_at_ms: int,
) -> dict[str, object]:
    selected = [
        item
        for item in samples
        if isinstance(item.get("timestamp_ms"), int)
        and started_at_ms <= int(item["timestamp_ms"]) <= finished_at_ms
    ]
    if not selected and samples:
        selected = list(samples)
    cpu_values = [
        float(item["cluster_cpu_utilization_pct"])
        for item in selected
        if isinstance(item.get("cluster_cpu_utilization_pct"), (int, float))
    ]
    npu_values = [
        float(item["cluster_npu_utilization_pct"])
        for item in selected
        if isinstance(item.get("cluster_npu_utilization_pct"), (int, float))
    ]
    max_npu_values = [
        float(item["max_device_npu_utilization_pct"])
        for item in selected
        if isinstance(item.get("max_device_npu_utilization_pct"), (int, float))
    ]
    hbm_values = [
        int(item["cluster_hbm_used_mb"])
        for item in selected
        if isinstance(item.get("cluster_hbm_used_mb"), int)
    ]
    baseline_hbm = None
    baseline_samples = [
        item
        for item in samples
        if isinstance(item.get("timestamp_ms"), int)
        and int(item["timestamp_ms"]) < started_at_ms
    ]
    baseline_cpu_values = [
        float(item["cluster_cpu_utilization_pct"])
        for item in baseline_samples
        if isinstance(item.get("cluster_cpu_utilization_pct"), (int, float))
    ]
    baseline_npu_values = [
        float(item["cluster_npu_utilization_pct"])
        for item in baseline_samples
        if isinstance(item.get("cluster_npu_utilization_pct"), (int, float))
    ]
    for item in reversed(samples):
        if (
            isinstance(item.get("timestamp_ms"), int)
            and int(item["timestamp_ms"]) <= started_at_ms
            and isinstance(item.get("cluster_hbm_used_mb"), int)
        ):
            baseline_hbm = int(item["cluster_hbm_used_mb"])
            break
    per_device: dict[str, dict[str, list[float]]] = {}
    per_node_cpu: dict[str, list[float]] = {}
    monitor_errors: list[str] = []
    for sample in selected:
        for error in sample.get("errors", []):  # type: ignore[union-attr]
            monitor_errors.append(str(error))
        for item in sample.get("containers", []):  # type: ignore[union-attr]
            if not isinstance(item, Mapping):
                continue
            value = item.get("cpu_utilization_pct")
            if isinstance(value, (int, float)):
                per_node_cpu.setdefault(str(item.get("node_id")), []).append(
                    float(value)
                )
        for item in sample.get("npus", []):  # type: ignore[union-attr]
            if not isinstance(item, Mapping):
                continue
            device_id = str(item.get("physical_device_id"))
            target = per_device.setdefault(
                device_id,
                {"utilization": [], "hbm": [], "process_count": []},
            )
            utilization = item.get("utilization_pct")
            hbm = item.get("used_hbm_mb")
            if isinstance(utilization, (int, float)):
                target["utilization"].append(float(utilization))
            if isinstance(hbm, int):
                target["hbm"].append(float(hbm))
            processes = item.get("processes")
            if isinstance(processes, list):
                target["process_count"].append(float(len(processes)))
    return {
        "sample_count": len(selected),
        "window_started_at_ms": started_at_ms,
        "window_finished_at_ms": finished_at_ms,
        "cluster_cpu_utilization_pct": _stats(cpu_values),
        "baseline_cluster_cpu_utilization_pct": _stats(baseline_cpu_values),
        "incremental_cluster_cpu_utilization_pct": (
            None
            if not cpu_values or not baseline_cpu_values
            else statistics.fmean(cpu_values) - statistics.fmean(baseline_cpu_values)
        ),
        "cluster_npu_utilization_pct": _stats(npu_values),
        "baseline_cluster_npu_utilization_pct": _stats(baseline_npu_values),
        "incremental_cluster_npu_utilization_pct": (
            None
            if not npu_values or not baseline_npu_values
            else statistics.fmean(npu_values) - statistics.fmean(baseline_npu_values)
        ),
        "max_device_npu_utilization_pct": _stats(max_npu_values),
        "cluster_hbm_used_mb": _stats(hbm_values),
        "baseline_cluster_hbm_used_mb": baseline_hbm,
        "peak_incremental_hbm_mb": (
            None
            if baseline_hbm is None or not hbm_values
            else max(hbm_values) - baseline_hbm
        ),
        "per_node_cpu_utilization_pct": {
            key: _stats(values) for key, values in sorted(per_node_cpu.items())
        },
        "per_device": {
            key: {
                "utilization_pct": _stats(value["utilization"]),
                "hbm_used_mb": _stats(value["hbm"]),
                "npu_process_count": _stats(value["process_count"]),
            }
            for key, value in sorted(per_device.items(), key=lambda item: int(item[0]))
        },
        "monitor_errors": monitor_errors,
    }


def _latest_valid_cluster_hbm(
    samples: Sequence[Mapping[str, object]],
) -> int | None:
    for sample in reversed(samples):
        npus = sample.get("npus")
        errors = sample.get("errors")
        value = sample.get("cluster_hbm_used_mb")
        dcmi_error = isinstance(errors, list) and any(
            str(item).startswith("dcmi:") for item in errors
        )
        if (
            isinstance(npus, list)
            and len(npus) == 8
            and isinstance(value, int)
            and not dcmi_error
        ):
            return value
    return None


def _discover_text_sample(data_root: Path) -> Any:
    samples, failures = qwen_smoke.discover_samples(
        data_root=data_root,
        datasets={"tbench"},
        workflows={"retail_cancel"},
        families={"text"},
        samples_per_workflow=1,
        sample_offset=0,
        max_inline_file_bytes=64 * 1024 * 1024,
        tbench_smoke_overrides=True,
        gaia_file_smoke_summary=True,
    )
    if failures:
        raise PerformancePilotError(f"sample discovery failed: {failures}")
    if len(samples) != 1:
        raise PerformancePilotError(
            f"expected one retail_cancel sample, found {len(samples)}"
        )
    return samples[0]


def _discover_mixed_candidates(data_root: Path) -> tuple[Any, ...]:
    samples, failures = qwen_smoke.discover_samples(
        data_root=data_root,
        datasets=set(),
        workflows=set(),
        families=set(),
        samples_per_workflow=2,
        sample_offset=0,
        max_inline_file_bytes=64 * 1024 * 1024,
        tbench_smoke_overrides=True,
        gaia_file_smoke_summary=True,
    )
    if failures:
        raise PerformancePilotError(f"mixed sample discovery failed: {failures}")
    expected = set(qwen_smoke.WORKFLOW_MODULES)
    discovered = {(sample.dataset, sample.workflow) for sample in samples}
    if discovered != expected:
        raise PerformancePilotError(
            "mixed sample discovery did not cover the 14 workflows; "
            f"missing={sorted(expected - discovered)}, "
            f"extra={sorted(discovered - expected)}"
        )
    return tuple(samples)


def _fixed_batch20_selection(samples: Sequence[Any]) -> tuple[tuple[Any, str], ...]:
    grouped: dict[tuple[str, str], list[Any]] = {}
    for sample in samples:
        grouped.setdefault((sample.dataset, sample.workflow), []).append(sample)
    for items in grouped.values():
        items.sort(key=lambda item: (int(item.query_index), item.sample_id))

    workflow_order = tuple(qwen_smoke.WORKFLOW_MODULES)
    if set(grouped) != set(workflow_order):
        raise PerformancePilotError("fixed Batch=20 candidates do not cover all workflows")
    if any(len(grouped[key]) < 2 for key in workflow_order):
        short = sorted(key for key in workflow_order if len(grouped[key]) < 2)
        raise PerformancePilotError(f"fixed Batch=20 needs two samples per workflow: {short}")

    selected: list[tuple[Any, str]] = [
        (grouped[key][0], "first_sample_per_workflow") for key in workflow_order
    ]
    for key in sorted(qwen_smoke.VISION_WORKFLOWS):
        selected.append((grouped[key][1], "second_sample_visual_workflow"))
    for dataset, workflow in FIXED_TEXT_EXTRA_WORKFLOWS.items():
        selected.append(
            (grouped[(dataset, workflow)][1], "second_sample_dataset_text_representative")
        )
    if len(selected) != FIXED_BATCH_SIZE:
        raise AssertionError(f"fixed Batch selection produced {len(selected)} requests")
    sample_ids = [sample.sample_id for sample, _ in selected]
    if len(set(sample_ids)) != len(sample_ids):
        raise AssertionError("fixed Batch selection contains duplicate samples")
    return tuple(selected)


def _target_model_id(family: str) -> str:
    if family == "text":
        return TEXT_MODEL_ID
    if family == "vision":
        return VISION_MODEL_ID
    raise PerformancePilotError(f"unsupported sample family: {family}")


def _build_fixed_batch20_manifest(samples: Sequence[Any]) -> dict[str, object]:
    selected = _fixed_batch20_selection(samples)
    entries = []
    for request_index, (sample, reason) in enumerate(selected, start=1):
        entries.append(
            {
                "request_index": request_index,
                "selection_reason": reason,
                "target_model_id": _target_model_id(sample.family),
                **sample.manifest(),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "objective": "fixed_mixed_batch20_manifest",
        "request_count": FIXED_BATCH_SIZE,
        "launch_offsets_ms": [0] * FIXED_BATCH_SIZE,
        "selection_policy": (
            "first sample of every migrated workflow; second sample of every "
            "vision workflow; second sample of one stable text workflow per dataset"
        ),
        "entries": entries,
    }


def _samples_from_manifest(
    data_root: Path, manifest_path: Path
) -> tuple[tuple[Any, str], ...]:
    manifest = _read_json(manifest_path)
    entries = manifest.get("entries")
    if manifest.get("request_count") != FIXED_BATCH_SIZE or not isinstance(entries, list):
        raise PerformancePilotError("workload manifest is not a fixed Batch=20 manifest")
    candidates = _discover_mixed_candidates(data_root)
    by_id = {sample.sample_id: sample for sample in candidates}
    resolved: list[tuple[Any, str]] = []
    for expected_index, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            raise PerformancePilotError("workload manifest entry is not an object")
        if entry.get("request_index") != expected_index:
            raise PerformancePilotError("workload manifest request indexes are unstable")
        sample_id = entry.get("sample_id")
        sample = by_id.get(str(sample_id))
        if sample is None:
            raise PerformancePilotError(f"manifest sample is unavailable: {sample_id}")
        for name in ("dataset", "workflow", "family"):
            if entry.get(name) != getattr(sample, name):
                raise PerformancePilotError(
                    f"manifest sample metadata changed for {sample_id}: {name}"
                )
        target_model_id = str(entry.get("target_model_id", ""))
        if target_model_id != _target_model_id(sample.family):
            raise PerformancePilotError(
                f"manifest model mapping is invalid for {sample_id}"
            )
        resolved.append((sample, target_model_id))
    if len(resolved) != FIXED_BATCH_SIZE:
        raise PerformancePilotError("workload manifest does not contain 20 entries")
    return tuple(resolved)


def _case_samples(
    args: argparse.Namespace, case: WorkloadCase
) -> tuple[tuple[Any, str], ...]:
    if args.workload_manifest is None:
        sample = _discover_text_sample(args.data_root)
        return tuple((sample, TEXT_MODEL_ID) for _ in range(case.request_count))
    if case.mode != "batch" or case.request_count != FIXED_BATCH_SIZE:
        raise PerformancePilotError(
            "fixed workload manifest requires exactly one batch-20 case"
        )
    return _samples_from_manifest(args.data_root, args.workload_manifest)


def _active_model_instances(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    instances = payload.get("instances")
    if not isinstance(instances, list):
        return []
    return [
        item
        for item in instances
        if isinstance(item, Mapping) and item.get("state") != "stopped"
    ]


def _placement_lease_counts(cluster: Mapping[str, object]) -> dict[str, int]:
    payload = cluster.get("cluster")
    if not isinstance(payload, Mapping):
        return {}
    leases = payload.get("active_leases")
    if not isinstance(leases, list):
        return {}
    counts: dict[str, int] = {}
    for item in leases:
        if not isinstance(item, Mapping):
            continue
        lease = item.get("lease")
        if not isinstance(lease, Mapping):
            continue
        kind = str(lease.get("reservation_kind", "unknown"))
        counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


async def _wait_maze_control_recovery(
    client: Any,
    run_ids: set[str],
    *,
    timeout_seconds: float,
) -> dict[str, object]:
    started = asyncio.get_running_loop().time()
    deadline = started + timeout_seconds
    last: dict[str, object] = {}
    while True:
        system, cluster, workers, models = await asyncio.gather(
            client.query("GetSystemSnapshot", timeout_seconds=10.0),
            client.query(
                "GetClusterSnapshot", filter="resources", timeout_seconds=10.0
            ),
            client.query("GetWorkerPools", timeout_seconds=10.0),
            client.query("GetModelInstances", timeout_seconds=10.0),
        )
        pool = workers.get("worker_pool")
        if not isinstance(pool, dict):
            pool = {}
        active_workers = logical_e2e._active_worker_leases(pool)  # noqa: SLF001
        active_models = _active_model_instances(models)
        run_owned = {
            run_id: logical_e2e._run_owned_leases(cluster, run_id)  # noqa: SLF001
            for run_id in sorted(run_ids)
        }
        route_occupancy = sum(
            int(item.get("route_occupancy", 0))
            for item in active_models
            if isinstance(item.get("route_occupancy", 0), int)
        )
        actual_request_inflight = sum(
            int(item.get("actual_request_inflight", 0))
            for item in active_models
            if isinstance(item.get("actual_request_inflight", 0), int)
        )
        recovered = (
            system.get("nonterminal_run_count") == 0
            and pool.get("active_worker_lease_count") == 0
            and not active_workers
            and not any(run_owned.values())
            and not active_models
            and route_occupancy == 0
            and actual_request_inflight == 0
        )
        last = {
            "recovered": recovered,
            "wait_ms": round((asyncio.get_running_loop().time() - started) * 1_000),
            "run_ids": sorted(run_ids),
            "nonterminal_run_count": system.get("nonterminal_run_count"),
            "active_worker_lease_count": pool.get("active_worker_lease_count"),
            "active_worker_leases": active_workers,
            "run_owned_placement_leases": run_owned,
            "active_model_instances": active_models,
            "route_occupancy": route_occupancy,
            "actual_request_inflight": actual_request_inflight,
            "active_placement_lease_counts": _placement_lease_counts(cluster),
            "system": system,
            "cluster": cluster,
            "worker_pools": workers,
            "model_instances": models,
        }
        if recovered or asyncio.get_running_loop().time() >= deadline:
            return last
        await asyncio.sleep(1.0)


def _task_timings(
    run: Mapping[str, object], task_names: Mapping[str, str]
) -> list[dict[str, object]]:
    return logical_e2e._task_timings(dict(run), dict(task_names))  # noqa: SLF001


def _dispatch_lifecycle(
    watch_batches: Sequence[Mapping[str, object]],
    task_names: Mapping[str, str],
) -> list[dict[str, object]]:
    lifecycle_types = {
        "task_queued",
        "task_dispatched",
        "dispatch_prepared",
        "worker_started",
    }
    attempts: dict[tuple[str, int], dict[str, Mapping[str, object]]] = {}
    queued_by_task: dict[str, list[Mapping[str, object]]] = {}
    for batch in watch_batches:
        events = batch.get("events")
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, Mapping):
                continue
            event_type = event.get("event_type")
            task_id = event.get("task_id")
            attempt = event.get("attempt")
            if event_type not in lifecycle_types or not isinstance(task_id, str):
                continue
            if event_type == "task_queued":
                queued_by_task.setdefault(task_id, []).append(event)
                continue
            if not isinstance(attempt, int) or isinstance(attempt, bool):
                continue
            attempts.setdefault((task_id, attempt), {})[str(event_type)] = event

    def timestamp(event: Mapping[str, object] | None) -> int | None:
        if event is None:
            return None
        value = event.get("monotonic_time_ms")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def elapsed(start: int | None, finish: int | None) -> int | None:
        if start is None or finish is None:
            return None
        return max(0, finish - start)

    records: list[dict[str, object]] = []
    queue_index_by_task: dict[str, int] = {}
    for (task_id, attempt), events in sorted(attempts.items()):
        queue_index = queue_index_by_task.get(task_id, 0)
        queued_events = queued_by_task.get(task_id, [])
        queued = queued_events[queue_index] if queue_index < len(queued_events) else None
        queue_index_by_task[task_id] = queue_index + 1
        dispatched = events.get("task_dispatched")
        prepared = events.get("dispatch_prepared")
        running = events.get("worker_started")
        queued_at = timestamp(queued)
        dispatched_at = timestamp(dispatched)
        prepared_at = timestamp(prepared)
        running_at = timestamp(running)
        dispatch_payload = (
            dispatched.get("payload")
            if isinstance(dispatched, Mapping)
            and isinstance(dispatched.get("payload"), Mapping)
            else {}
        )
        prepared_payload = (
            prepared.get("payload")
            if isinstance(prepared, Mapping)
            and isinstance(prepared.get("payload"), Mapping)
            else {}
        )
        running_payload = (
            running.get("payload")
            if isinstance(running, Mapping)
            and isinstance(running.get("payload"), Mapping)
            else {}
        )
        records.append(
            {
                "task_id": task_id,
                "task_name": task_names.get(task_id, task_id),
                "attempt": attempt,
                "dispatch_id": dispatch_payload.get("dispatch_id"),
                "node_id": dispatch_payload.get("node_id"),
                "worker_pid": running_payload.get("worker_pid"),
                "task_queued_sequence": (
                    None if queued is None else queued.get("sequence")
                ),
                "task_dispatched_sequence": (
                    None if dispatched is None else dispatched.get("sequence")
                ),
                "dispatch_prepared_sequence": (
                    None if prepared is None else prepared.get("sequence")
                ),
                "running_sequence": (
                    None if running is None else running.get("sequence")
                ),
                "task_queued_at_ms": queued_at,
                "task_dispatched_at_ms": dispatched_at,
                "dispatch_prepared_at_ms": prepared_at,
                "running_at_ms": running_at,
                "dispatch_prepare_ms": prepared_payload.get("dispatch_prepare_ms"),
                "queue_to_dispatch_ms": elapsed(queued_at, dispatched_at),
                "dispatch_to_prepared_ms": elapsed(dispatched_at, prepared_at),
                "prepared_to_running_ms": elapsed(prepared_at, running_at),
                "dispatch_to_running_ms": elapsed(dispatched_at, running_at),
            }
        )
    return records


async def _wait_maze_terminal(
    client: Any, run_id: str, timeout_seconds: float
) -> tuple[
    dict[str, object], list[dict[str, object]], list[dict[str, object]]
]:
    watch_batches: list[dict[str, object]] = []
    async for batch in client.watch_run(run_id, timeout_seconds=timeout_seconds):
        watch_batches.append(batch)
    shown = await client.query(
        "GetRun", resource_id=run_id, timeout_seconds=min(30.0, timeout_seconds)
    )
    run = shown.get("run")
    if not isinstance(run, dict):
        raise PerformancePilotError("GetRun returned no terminal Run")
    if str(run.get("status")) not in TERMINAL_STATES:
        raise PerformancePilotError("WatchRun ended before a terminal state")
    raw_timings = shown.get("runtime_task_timings")
    runtime_task_timings = (
        [dict(item) for item in raw_timings if isinstance(item, Mapping)]
        if isinstance(raw_timings, list)
        else []
    )
    return run, watch_batches, runtime_task_timings


async def _run_maze_request(
    *,
    client: Any,
    workflow: Any,
    compiled: Any,
    task_names: Mapping[str, str],
    sample: Any,
    target_model_id: str,
    case_id: str,
    request_index: int,
    timeout_seconds: float,
) -> dict[str, object]:
    unique = f"{case_id}:{request_index}:{time.time_ns()}"
    submission_id = "perf-" + hashlib.sha256(unique.encode()).hexdigest()[:28]
    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "executor": "maze",
        "case_id": case_id,
        "request_index": request_index,
        "sample_id": sample.sample_id,
        "dataset": sample.dataset,
        "workflow": sample.workflow,
        "family": sample.family,
        "target_model_id": target_model_id,
        "submission_id": submission_id,
        "status": "not_started",
    }
    run_id: str | None = None
    destroyed = False
    e2e_started_perf = time.perf_counter()
    record["client_e2e_started_at_ms"] = int(time.time() * 1_000)
    try:
        stage = time.perf_counter()
        prepared = await asyncio.wait_for(
            client.prepare_submission(
                workflow,
                inputs=sample.inputs,
                submission_id=submission_id,
                session_key=f"{submission_id}-session",
                run_deadline_ms=round(timeout_seconds * 1_000),
            ),
            timeout=min(120.0, max(30.0, timeout_seconds)),
        )
        record["prepare_submission_ms"] = round((time.perf_counter() - stage) * 1_000)
        stage = time.perf_counter()
        outcome = await client.submit_prepared(prepared, timeout_seconds=60.0)
        record["submit_roundtrip_ms"] = round((time.perf_counter() - stage) * 1_000)
        value = outcome.get("run_id")
        if not isinstance(value, str) or not value:
            raise PerformancePilotError(f"submission did not commit: {outcome}")
        run_id = value
        record["run_id"] = run_id
        terminal, watch_batches, runtime_task_timings = await _wait_maze_terminal(
            client, run_id, timeout_seconds
        )
        record["terminal_status"] = terminal.get("status")
        record["watch_batch_count"] = len(watch_batches)
        record["runtime_task_timings"] = runtime_task_timings
        record["transformers_local_records"] = [
            dict(item)
            for timing in runtime_task_timings
            for item in (
                timing.get("inference_metrics")
                if isinstance(timing.get("inference_metrics"), list)
                else []
            )
            if isinstance(item, Mapping)
        ]
        record["dispatch_lifecycle"] = _dispatch_lifecycle(
            watch_batches, task_names
        )
        if terminal.get("status") != "succeeded":
            raise PerformancePilotError(f"Run terminated as {terminal.get('status')}")
        results = {}
        for task_id in compiled.exit_tasks:
            results[task_names[task_id]] = await asyncio.wait_for(
                client.materialize_task_result(run_id, task_id),
                timeout=min(120.0, max(30.0, timeout_seconds)),
            )
        record["exit_task_results"] = results
        record["status"] = "succeeded"
        record["client_e2e_finished_at_ms"] = int(time.time() * 1_000)
        record["client_e2e_ms"] = round(
            (time.perf_counter() - e2e_started_perf) * 1_000
        )
        record["task_timings"] = _task_timings(terminal, task_names)
        stage = time.perf_counter()
        record["destroy_result"] = await client.run_action(
            "DestroyRun", run_id, force=True, timeout_seconds=120.0
        )
        record["destroy_ms"] = round((time.perf_counter() - stage) * 1_000)
        destroyed = True
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["traceback"] = traceback.format_exc()
    finally:
        record.setdefault("client_e2e_finished_at_ms", int(time.time() * 1_000))
        record.setdefault(
            "client_e2e_ms", round((time.perf_counter() - e2e_started_perf) * 1_000)
        )
        if run_id is not None and not destroyed:
            try:
                await client.run_action(
                    "CancelRun",
                    run_id,
                    reason="performance_pilot_cleanup",
                    force=True,
                    timeout_seconds=30.0,
                )
            except Exception as exc:
                record.setdefault("cleanup_errors", []).append(
                    f"cancel:{type(exc).__name__}:{exc}"
                )
            try:
                await client.run_action(
                    "DestroyRun", run_id, force=True, timeout_seconds=120.0
                )
            except Exception as exc:
                record.setdefault("cleanup_errors", []).append(
                    f"destroy:{type(exc).__name__}:{exc}"
                )
    return record


async def _run_maze_request_bounded(
    *,
    client: Any,
    workflow: Any,
    compiled: Any,
    task_names: Mapping[str, str],
    sample: Any,
    target_model_id: str,
    case_id: str,
    request_index: int,
    timeout_seconds: float,
    hard_timeout_seconds: float | None = None,
) -> dict[str, object]:
    started_at_ms = int(time.time() * 1_000)
    hard_timeout = (
        timeout_seconds + REQUEST_CLEANUP_GRACE_SECONDS
        if hard_timeout_seconds is None
        else hard_timeout_seconds
    )
    try:
        return await asyncio.wait_for(
            _run_maze_request(
                client=client,
                workflow=workflow,
                compiled=compiled,
                task_names=task_names,
                sample=sample,
                target_model_id=target_model_id,
                case_id=case_id,
                request_index=request_index,
                timeout_seconds=timeout_seconds,
            ),
            timeout=hard_timeout,
        )
    except asyncio.TimeoutError:
        finished_at_ms = int(time.time() * 1_000)
        return {
            "schema_version": SCHEMA_VERSION,
            "executor": "maze",
            "case_id": case_id,
            "request_index": request_index,
            "sample_id": sample.sample_id,
            "dataset": sample.dataset,
            "workflow": sample.workflow,
            "family": sample.family,
            "target_model_id": target_model_id,
            "status": "failed",
            "error": (
                "TimeoutError: request exceeded hard deadline "
                f"of {hard_timeout:.3f} seconds"
            ),
            "client_e2e_started_at_ms": started_at_ms,
            "client_e2e_finished_at_ms": finished_at_ms,
            "client_e2e_ms": max(0, finished_at_ms - started_at_ms),
            "hard_timeout_seconds": hard_timeout,
        }


async def _run_scheduled(
    launch_offsets_ms: Sequence[int], run_one: Any
) -> tuple[list[dict[str, object]], int, int]:
    loop = asyncio.get_running_loop()
    workload_started_at_ms = int(time.time() * 1_000)
    started = loop.time()
    tasks: list[asyncio.Task[dict[str, object]]] = []
    for request_index, offset_ms in enumerate(launch_offsets_ms, start=1):
        delay = started + offset_ms / 1_000 - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        actual_offset_ms = round((loop.time() - started) * 1_000)

        async def invoke(index: int, planned: int, actual: int) -> dict[str, object]:
            result = await run_one(index)
            result["planned_launch_offset_ms"] = planned
            result["actual_launch_offset_ms"] = actual
            return result

        tasks.append(
            asyncio.create_task(invoke(request_index, int(offset_ms), actual_offset_ms))
        )
    records = list(await asyncio.gather(*tasks))
    workload_finished_at_ms = int(time.time() * 1_000)
    return records, workload_started_at_ms, workload_finished_at_ms


async def _run_maze_worker(
    args: argparse.Namespace, case: WorkloadCase
) -> dict[str, object]:
    from ascend_maze.control.local_rpc import UdsRuntimeClient

    selected = _case_samples(args, case)
    requests: list[dict[str, object]] = []
    for sample, target_model_id in selected:
        workflow, aliases = qwen_smoke._build_workflow(  # noqa: SLF001
            sample.dataset, sample.workflow, target_model_id
        )
        compiled = workflow.compile()
        requests.append(
            {
                "sample": sample,
                "target_model_id": target_model_id,
                "workflow": workflow,
                "compiled": compiled,
                "aliases": aliases,
                "task_names": {
                    task_id: task.task_name
                    for task_id, task in compiled.tasks.items_tuple()
                },
            }
        )
    client = UdsRuntimeClient(args.control_socket)
    try:
        controller_status = await client.get_controller_status(timeout_seconds=10.0)
        if controller_status.healthy_node_count != 8:
            raise PerformancePilotError(
                f"expected 8 healthy nodes, found {controller_status.healthy_node_count}"
            )
        await client._ensure_data_store()  # noqa: SLF001

        async def run_one(index: int) -> dict[str, object]:
            request = requests[index - 1]
            return await _run_maze_request_bounded(
                client=client,
                workflow=request["workflow"],
                compiled=request["compiled"],
                task_names=request["task_names"],
                sample=request["sample"],
                target_model_id=str(request["target_model_id"]),
                case_id=case.case_id,
                request_index=index,
                timeout_seconds=float(args.request_timeout_seconds),
            )

        records, started_at_ms, finished_at_ms = await _run_scheduled(
            case.launch_offsets_ms, run_one
        )
        run_ids = {
            str(record["run_id"])
            for record in records
            if isinstance(record.get("run_id"), str)
        }
        control_recovery = await _wait_maze_control_recovery(
            client,
            run_ids,
            timeout_seconds=float(args.resource_recovery_timeout_seconds),
        )
        system_after = await client.query("GetSystemSnapshot", timeout_seconds=10.0)
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": OBJECTIVE,
            "executor": "maze",
            "case": case.payload(),
            "samples": [sample.manifest() for sample, _ in selected],
            "workload_manifest_path": (
                None
                if args.workload_manifest is None
                else str(args.workload_manifest)
            ),
            "workflows": [
                {
                    "sample_id": request["sample"].sample_id,
                    "target_model_id": request["target_model_id"],
                    "model_aliases": request["aliases"],
                    "workflow_fingerprint": request["compiled"].workflow_fingerprint,
                }
                for request in requests
            ],
            "controller_status": controller_status,
            "system_after": system_after,
            "control_recovery": control_recovery,
            "workload_started_at_ms": started_at_ms,
            "workload_finished_at_ms": finished_at_ms,
            "records": records,
            "aggregate": _aggregate_requests(
                records,
                mode=case.mode,
                admission_window_seconds=case.admission_window_seconds,
            ),
            "breakdowns": _aggregate_breakdowns(
                records,
                mode=case.mode,
                admission_window_seconds=case.admission_window_seconds,
            ),
        }
    finally:
        client.close()


def _transformers_config(
    args: argparse.Namespace, family: str
) -> dict[str, object]:
    from ascend_maze.ascend.discovery import discover_aicpu_runtime_library_paths

    is_vision = family == "vision"
    if family not in {"text", "vision"}:
        raise PerformancePilotError(f"unsupported Transformers family: {family}")
    model_id = VISION_MODEL_ID if is_vision else TEXT_MODEL_ID
    model_path = args.vision_model_path if is_vision else args.text_model_path
    return {
        "family": family,
        "model_id": model_id,
        "model_path": str(model_path),
        "tokenizer_path": str(model_path),
        "device_id": "0",
        "dtype": "bfloat16",
        "generation_method": "manual_greedy",
        "model_kind": "vision_language" if is_vision else "text",
        "max_model_len": 12288 if is_vision else 10240,
        "trust_remote_code": not is_vision,
        "enable_thinking": False,
        "qwen2_5_vl_cpu_unique_consecutive_workaround": is_vision,
        "request_timeout_ms": round(float(args.request_timeout_seconds) * 1_000),
        "runtime_library_paths": tuple(discover_aicpu_runtime_library_paths()),
    }


async def _run_ray_worker(
    args: argparse.Namespace, case: WorkloadCase
) -> dict[str, object]:
    import ray

    selected = _case_samples(args, case)
    ray.init(
        address="auto",
        namespace=f"ascend-maze-performance-{case.case_id}",
        ignore_reinit_error=True,
        include_dashboard=False,
        runtime_env={"env_vars": {"PYTHONPATH": os.environ["PYTHONPATH"]}},
    )
    ray_task = ray.remote(
        num_cpus=float(args.ray_task_num_cpus),
        max_calls=ray_smoke.RAY_TASK_MAX_CALLS,
    )(ray_smoke._execute_workflow_task_remote)  # noqa: SLF001
    transformers_configs = {
        family: _transformers_config(args, family) for family in ("text", "vision")
    }
    try:
        alive_nodes = [node for node in ray.nodes() if node.get("Alive")]
        if len(alive_nodes) != 8:
            raise PerformancePilotError(
                f"expected 8 healthy Ray nodes, found {len(alive_nodes)}"
            )

        async def run_one(index: int) -> dict[str, object]:
            sample, target_model_id = selected[index - 1]
            record = await asyncio.to_thread(
                ray_smoke._run_one_sample_ray,  # noqa: SLF001
                ray_task=ray_task,
                service_actor=None,
                inference_backend="transformers",
                transformers_config=transformers_configs[sample.family],
                sample=sample,
                target_model_id=target_model_id,
                run_timeout_seconds=float(args.request_timeout_seconds),
                run_salt=f"{case.case_id}-{index}",
            )
            latency = record.get("latency_metrics")
            if not isinstance(latency, Mapping):
                latency = {}
            return {
                "schema_version": SCHEMA_VERSION,
                "executor": "ray",
                "case_id": case.case_id,
                "request_index": index,
                "sample_id": sample.sample_id,
                "dataset": sample.dataset,
                "workflow": sample.workflow,
                "family": sample.family,
                "target_model_id": target_model_id,
                "run_id": record.get("run_id"),
                "status": record.get("status"),
                "client_e2e_started_at_ms": record.get("client_e2e_started_at_ms"),
                "client_e2e_finished_at_ms": record.get("client_e2e_finished_at_ms"),
                "client_e2e_ms": latency.get("client_e2e_ms"),
                "error": record.get("error"),
                "task_timings": record.get("task_timing_records", []),
                "tasks": record.get("tasks", []),
                "transformers_local_records": record.get(
                    "transformers_local_records", []
                ),
                "raw_record": record,
            }

        records, started_at_ms, finished_at_ms = await _run_scheduled(
            case.launch_offsets_ms, run_one
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": OBJECTIVE,
            "executor": "ray",
            "case": case.payload(),
            "samples": [sample.manifest() for sample, _ in selected],
            "workload_manifest_path": (
                None
                if args.workload_manifest is None
                else str(args.workload_manifest)
            ),
            "worker_max_calls": ray_smoke.RAY_TASK_MAX_CALLS,
            "ray_task_num_cpus": float(args.ray_task_num_cpus),
            "ray_nodes": [
                {
                    "node_id": node.get("NodeID"),
                    "node_ip": node.get("NodeManagerAddress"),
                    "cpu": node.get("Resources", {}).get("CPU"),
                }
                for node in alive_nodes
            ],
            "transformers_configs": transformers_configs,
            "workload_started_at_ms": started_at_ms,
            "workload_finished_at_ms": finished_at_ms,
            "records": records,
            "aggregate": _aggregate_requests(
                records,
                mode=case.mode,
                admission_window_seconds=case.admission_window_seconds,
            ),
            "breakdowns": _aggregate_breakdowns(
                records,
                mode=case.mode,
                admission_window_seconds=case.admission_window_seconds,
            ),
        }
    finally:
        ray.shutdown()


def _case_from_payload(payload: Mapping[str, object]) -> WorkloadCase:
    return WorkloadCase(
        case_id=str(payload["case_id"]),
        mode=str(payload["mode"]),
        request_count=int(payload["request_count"]),
        launch_offsets_ms=tuple(int(item) for item in payload["launch_offsets_ms"]),  # type: ignore[index]
        batch_size=(
            None if payload.get("batch_size") is None else int(payload["batch_size"])
        ),
        arrival_ratio=(
            None
            if payload.get("arrival_ratio") is None
            else float(payload["arrival_ratio"])
        ),
        arrival_rate_per_second=(
            None
            if payload.get("arrival_rate_per_second") is None
            else float(payload["arrival_rate_per_second"])
        ),
        average_workflow_seconds=(
            None
            if payload.get("average_workflow_seconds") is None
            else float(payload["average_workflow_seconds"])
        ),
        admission_window_seconds=(
            None
            if payload.get("admission_window_seconds") is None
            else float(payload["admission_window_seconds"])
        ),
    )


def _case_from_file(path: Path) -> WorkloadCase:
    return _case_from_payload(_read_json(path))


def _run_internal_worker(args: argparse.Namespace) -> int:
    if args.case_file is None or args.result_file is None:
        raise SystemExit(
            "--case-file and --result-file are required for internal worker"
        )
    case = _case_from_file(args.case_file)
    started_at_ms = int(time.time() * 1_000)
    try:
        result = asyncio.run(
            _run_maze_worker(args, case)
            if args.internal_worker == "maze"
            else _run_ray_worker(args, case)
        )
        result["worker_process_started_at_ms"] = started_at_ms
        result["worker_process_finished_at_ms"] = int(time.time() * 1_000)
        result["worker_environment"] = {
            "python": sys.version,
            "executable": sys.executable,
            "pid": os.getpid(),
        }
        _write_json(args.result_file, result)
        print(json.dumps({"status": "succeeded", "result_file": str(args.result_file)}))
        requests_succeeded = result["aggregate"]["failed"] == 0  # type: ignore[index]
        control_recovered = (
            args.internal_worker != "maze"
            or result.get("control_recovery", {}).get("recovered") is True  # type: ignore[union-attr]
        )
        return 0 if requests_succeeded and control_recovered else 20
    except Exception as exc:
        failure = {
            "schema_version": SCHEMA_VERSION,
            "objective": OBJECTIVE,
            "executor": args.internal_worker,
            "case": case.payload(),
            "status": "worker_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "worker_process_started_at_ms": started_at_ms,
            "worker_process_finished_at_ms": int(time.time() * 1_000),
        }
        _write_json(args.result_file, failure)
        print(json.dumps({"status": "failed", "error": failure["error"]}))
        return 99


def _container_output_path(host_path: Path, state_root: Path) -> Path:
    node_root = (state_root / "node-0").resolve()
    try:
        relative = host_path.resolve().relative_to(node_root)
    except ValueError as exc:
        raise PerformancePilotError(
            f"output directory must be below the node-0 state mount: {node_root}"
        ) from exc
    return Path("/workspace/state") / relative


def _git_environment() -> dict[str, object]:
    def run(*argv: str) -> str:
        return subprocess.check_output(argv, cwd=REPO_ROOT, text=True).strip()

    try:
        revision = run("git", "rev-parse", "HEAD")
        status = run("git", "status", "--short")
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"revision": revision, "dirty": bool(status), "status": status.splitlines()}


def _control_environment(state_root: Path) -> dict[str, object]:
    config_path = state_root / "node-0" / "control-plane" / "controller.toml"
    catalog_path = state_root / "node-0" / "control-plane" / "model_catalog.toml"
    if not config_path.is_file() or not catalog_path.is_file():
        raise PerformancePilotError("logical-cluster control configuration is missing")
    config = tomllib.loads(config_path.read_text(encoding="ascii"))
    catalog = tomllib.loads(catalog_path.read_text(encoding="ascii"))
    return {
        "profile": config.get("profile"),
        "controller_config_path": str(config_path),
        "controller_config_sha256": _sha256(config_path),
        "config": config,
        "model_catalog_path": str(catalog_path),
        "model_catalog_sha256": _sha256(catalog_path),
        "model_catalog": catalog,
    }


def _load_resume_state(
    args: argparse.Namespace,
    output_dir: Path,
) -> tuple[
    dict[str, object],
    tuple[WorkloadCase, ...],
    tuple[tuple[WorkloadCase, str, int], ...],
]:
    plan_path = output_dir / "plan.json"
    manifest_path = output_dir / "workload_manifest.json"
    if not plan_path.is_file():
        raise PerformancePilotError(f"resume plan is missing: {plan_path}")
    plan = _read_json(plan_path)
    if plan.get("objective") != OBJECTIVE or plan.get("schema_version") != SCHEMA_VERSION:
        raise PerformancePilotError("resume plan has an incompatible objective or schema")

    raw_cases = plan.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise PerformancePilotError("resume plan contains no workload cases")
    cases = tuple(
        _case_from_payload(item)
        for item in raw_cases
        if isinstance(item, Mapping)
    )
    if len(cases) != len(raw_cases):
        raise PerformancePilotError("resume plan contains an invalid workload case")

    executor = str(plan.get("executor", ""))
    if executor not in {"paired", "maze", "ray"}:
        raise PerformancePilotError(f"resume plan has an invalid executor: {executor}")
    args.executor = executor
    order = _execution_order(cases, executor)
    expected_order = [
        (case.case_id, item_executor, pair_position)
        for case, item_executor, pair_position in order
    ]
    raw_order = plan.get("execution_order")
    if not isinstance(raw_order, list):
        raise PerformancePilotError("resume plan contains no execution order")
    frozen_order = [
        (
            str(item.get("case_id")),
            str(item.get("executor")),
            int(item.get("pair_position", 0)),
        )
        for item in raw_order
        if isinstance(item, Mapping)
    ]
    if frozen_order != expected_order:
        raise PerformancePilotError("resume execution order does not match frozen cases")

    contract = plan.get("contract")
    if not isinstance(contract, Mapping):
        raise PerformancePilotError("resume plan contains no experiment contract")
    frozen_manifest = contract.get("workload_manifest")
    if frozen_manifest is not None:
        if not manifest_path.is_file():
            raise PerformancePilotError(f"resume manifest is missing: {manifest_path}")
        manifest = _read_json(manifest_path)
        if manifest != frozen_manifest:
            raise PerformancePilotError("resume manifest differs from the frozen plan")
        args.workload_manifest = manifest_path
    else:
        args.workload_manifest = None

    for argument, contract_key in (
        ("text_model_path", "text_model_path"),
        ("vision_model_path", "vision_model_path"),
    ):
        value = contract.get(contract_key)
        if not isinstance(value, str) or not value:
            raise PerformancePilotError(
                f"resume contract is missing {contract_key}"
            )
        setattr(args, argument, Path(value).expanduser().resolve())
    for argument, contract_key in (
        ("request_timeout_seconds", "request_timeout_seconds"),
        ("case_timeout_seconds", "case_timeout_seconds"),
        ("ray_task_num_cpus", "ray_task_num_cpus"),
    ):
        value = contract.get(contract_key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PerformancePilotError(
                f"resume contract is missing {contract_key}"
            )
        setattr(args, argument, float(value))

    frozen_control = plan.get("control_environment")
    current_control = _control_environment(args.state_root)
    if not isinstance(frozen_control, Mapping):
        raise PerformancePilotError("resume plan contains no control environment")
    for key in ("profile", "controller_config_sha256", "model_catalog_sha256"):
        if frozen_control.get(key) != current_control.get(key):
            raise PerformancePilotError(
                f"resume control environment changed: {key}"
            )
    if current_control.get("profile") != "performance":
        raise PerformancePilotError(
            "resume requires the frozen performance control profile"
        )
    return plan, cases, order


def _completed_case_result(
    output_dir: Path,
    case: WorkloadCase,
    executor: str,
) -> dict[str, object] | None:
    result_path = output_dir / "cases" / case.case_id / executor / "result.json"
    if not result_path.is_file():
        return None
    result = _read_json(result_path)
    frozen_case = result.get("case")
    process = result.get("process")
    aggregate = result.get("aggregate")
    resources = result.get("resources")
    physical = result.get("physical_hbm_recovery")
    resource_path = result.get("resource_samples_path")
    complete = (
        result.get("executor") == executor
        and isinstance(frozen_case, Mapping)
        and frozen_case.get("case_id") == case.case_id
        and isinstance(process, Mapping)
        and process.get("exit_code") == 0
        and isinstance(aggregate, Mapping)
        and isinstance(resources, Mapping)
        and isinstance(resources.get("sample_count"), int)
        and int(resources["sample_count"]) > 0
        and isinstance(physical, Mapping)
        and isinstance(physical.get("recovered"), bool)
        and isinstance(resource_path, str)
        and Path(resource_path).is_file()
    )
    if executor == "maze":
        control = result.get("control_recovery")
        complete = (
            complete
            and isinstance(control, Mapping)
            and isinstance(control.get("recovered"), bool)
        )
    return result if complete else None


def _archive_incomplete_case(
    output_dir: Path,
    case: WorkloadCase,
    executor: str,
) -> Path | None:
    case_dir = output_dir / "cases" / case.case_id / executor
    if not case_dir.is_dir():
        return None
    candidates = [
        path
        for path in case_dir.iterdir()
        if path.name != "incomplete_attempts"
    ]
    if not candidates:
        return None
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    archive = case_dir / "incomplete_attempts" / timestamp
    suffix = 1
    while archive.exists():
        archive = case_dir / "incomplete_attempts" / f"{timestamp}-{suffix}"
        suffix += 1
    archive.mkdir(parents=True)
    for path in candidates:
        path.replace(archive / path.name)
    return archive


def _worker_command(
    *,
    args: argparse.Namespace,
    executor: str,
    container_case_path: Path,
    container_result_path: Path,
    container_manifest_path: Path | None,
) -> list[str]:
    command = [
        sys.executable,
        str(REPO_ROOT / "tools" / "logical_cluster_performance.py"),
        "--internal-worker",
        executor,
        "--case-file",
        str(container_case_path),
        "--result-file",
        str(container_result_path),
        "--control-socket",
        str(DEFAULT_CONTROL_SOCKET),
        "--data-root",
        str(args.data_root),
        "--text-model-path",
        str(args.text_model_path),
        "--vision-model-path",
        str(args.vision_model_path),
        "--request-timeout-seconds",
        str(args.request_timeout_seconds),
        "--resource-recovery-timeout-seconds",
        str(args.resource_recovery_timeout_seconds),
        "--ray-task-num-cpus",
        str(args.ray_task_num_cpus),
    ]
    if container_manifest_path is not None:
        command.extend(("--workload-manifest", str(container_manifest_path)))
    return command


def _run_container_worker(
    *,
    args: argparse.Namespace,
    executor: str,
    case: WorkloadCase,
    output_dir: Path,
    container_output_dir: Path,
) -> dict[str, object]:
    case_dir = output_dir / "cases" / case.case_id / executor
    container_case_dir = container_output_dir / "cases" / case.case_id / executor
    case_file = case_dir / "case.json"
    result_file = case_dir / "runner.json"
    resource_path = case_dir / "resource_samples.jsonl"
    _write_json(case_file, case.payload())
    worker_argv = _worker_command(
        args=args,
        executor=executor,
        container_case_path=container_case_dir / "case.json",
        container_result_path=container_case_dir / "runner.json",
        container_manifest_path=(
            None
            if args.workload_manifest is None
            else container_output_dir / "workload_manifest.json"
        ),
    )
    shell_command = (
        f"source {shlex.quote(str(CONTAINER_ENV))}; exec {shlex.join(worker_argv)}"
    )
    docker_command = ["docker", "exec", CONTAINER_NAME, "bash", "-lc", shell_command]
    cache_warm = _warm_model_file_cache(
        (args.text_model_path, args.vision_model_path)
    )
    monitor = HostResourceMonitor(
        output_path=resource_path,
        interval_seconds=float(args.resource_sample_interval_seconds),
    )
    monitor.start()
    time.sleep(float(args.resource_baseline_seconds))
    baseline_hbm_mb = _latest_valid_cluster_hbm(monitor.samples)
    started_at_ms = int(time.time() * 1_000)
    case_dir.mkdir(parents=True, exist_ok=True)
    with (
        (case_dir / "stdout.log").open("w", encoding="utf-8") as stdout_handle,
        (case_dir / "stderr.log").open("w", encoding="utf-8") as stderr_handle,
    ):
        try:
            completed = subprocess.run(
                docker_command,
                stdout=stdout_handle,
                stderr=stderr_handle,
                timeout=float(args.case_timeout_seconds),
                check=False,
            )
            exit_code = completed.returncode
            timeout_error = None
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            timeout_error = str(exc)
    hbm_recovery = (
        {
            "recovered": False,
            "error": "no valid pre-workload DCMI baseline",
        }
        if baseline_hbm_mb is None
        else monitor.wait_for_hbm_recovery(
            baseline_hbm_mb=baseline_hbm_mb,
            timeout_seconds=float(args.resource_recovery_timeout_seconds),
            tolerance_mb=int(args.hbm_recovery_tolerance_mb),
        )
    )
    samples = monitor.stop()
    finished_at_ms = int(time.time() * 1_000)
    result = (
        _read_json(result_file)
        if result_file.is_file()
        else {
            "schema_version": SCHEMA_VERSION,
            "executor": executor,
            "status": "missing_runner_result",
            "error": timeout_error or f"worker exited with code {exit_code}",
        }
    )
    workload_start = result.get("workload_started_at_ms")
    workload_finish = result.get("workload_finished_at_ms")
    if not isinstance(workload_start, int):
        workload_start = started_at_ms
    if not isinstance(workload_finish, int):
        workload_finish = finished_at_ms
    result["process"] = {
        "docker_command": docker_command,
        "exit_code": exit_code,
        "timeout_error": timeout_error,
        "started_at_ms": started_at_ms,
        "finished_at_ms": finished_at_ms,
        "stdout_path": str(case_dir / "stdout.log"),
        "stderr_path": str(case_dir / "stderr.log"),
    }
    result["model_file_cache_warmup"] = cache_warm
    result["physical_hbm_recovery"] = hbm_recovery
    result["resource_samples_path"] = str(resource_path)
    result["resources"] = _aggregate_resources(
        samples,
        started_at_ms=workload_start,
        finished_at_ms=workload_finish,
    )
    _write_json(case_dir / "result.json", result)
    return result


def _fmt(value: object, digits: int = 2) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "n/a"
    return f"{float(value):.{digits}f}"


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _render_report(summary: Mapping[str, object]) -> str:
    contract = summary.get("contract")
    contract = contract if isinstance(contract, Mapping) else {}
    lines = [
        "# Ascend-Maze / Ray 八逻辑节点性能 Pilot",
        "",
        "> 本报告是单台 8 卡主机上的八容器逻辑集群结果，不代表真实跨机网络性能。",
        "",
        "## 实验契约",
        "",
        "- Workload：固定 Batch=20；14 种 workflow 各一个样本，另按确定性规则补 6 个样本",
        "- 文本模型：`Qwen3-4B`；视觉模型：`Qwen2.5-VL-3B-Instruct`；均为 Transformers `manual_greedy`",
        "- 生成参数：`max_tokens=4096`、`temperature=0`；文本/视觉 `max_model_len=10240/12288`",
        "- 模型加载：计入每个请求 E2E；模型 Task 进程一次性使用",
        "- Ray：每个 Task 请求逻辑节点全部 20 CPU，保证每节点同时至多一个 Task；`max_calls=1`",
        "- Maze：按 CPU、I/O、NPU slot 和实测 HBM 预算共卡；文本/视觉实例预算为 13824/11776 MB",
        "- 页缓存：两个执行器测量前均读取两套模型文件；每个 Task 仍创建新进程并把权重搬到 NPU",
        "- E2E：客户端开始准备并提交请求，到终态结果返回；`DestroyRun` 不计入",
        "- 硬截止：请求 "
        f"{_fmt(contract.get('request_timeout_seconds'), 0)} 秒；Case "
        f"{_fmt(contract.get('case_timeout_seconds'), 0)} 秒",
        "",
        "## 汇总",
        "",
        "| Case | 执行器 | 成功/总数 | E2E P95 (ms) | 吞吐 (req/s) | CPU 均值 (%) | CPU 增量 (%) | NPU 八卡均值 (%) | 单卡 NPU 峰值 (%) | HBM 增量峰值 (MB) | 单卡最大 NPU 进程数 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    partial_evidence = summary.get("partial_evidence")
    if isinstance(partial_evidence, Mapping):
        evidence_labels = {
            "latency_timing": "延迟、吞吐和阶段时间",
            "host_cpu_npu_hbm": "宿主侧 CPU/NPU/HBM 时序",
            "ray_physical_recovery_timeline": "Ray 物理资源回落时序",
        }
        evidence_lines = ["## 当前证据范围", ""]
        for key, label in evidence_labels.items():
            if key in partial_evidence:
                evidence_lines.append(f"- {label}：{partial_evidence[key]}")
        evidence_lines.extend(
            (
                "",
                "> 资源图不对缺失的 Ray 采样做推断或补齐；空白即表示证据不可用。",
                "",
            )
        )
        summary_index = lines.index("## 汇总")
        lines[summary_index:summary_index] = evidence_lines
    results = summary.get("results", [])
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, Mapping):
                continue
            aggregate = result.get("aggregate")
            resources = result.get("resources")
            if not isinstance(aggregate, Mapping):
                aggregate = {}
            if not isinstance(resources, Mapping):
                resources = {}
            cpu = resources.get("cluster_cpu_utilization_pct")
            npu = resources.get("cluster_npu_utilization_pct")
            max_npu = resources.get("max_device_npu_utilization_pct")
            cpu_mean = cpu.get("mean") if isinstance(cpu, Mapping) else None
            npu_mean = npu.get("mean") if isinstance(npu, Mapping) else None
            max_npu_peak = max_npu.get("max") if isinstance(max_npu, Mapping) else None
            case = result.get("case")
            case_id = case.get("case_id") if isinstance(case, Mapping) else "unknown"
            per_device = resources.get("per_device")
            process_peaks = [
                item.get("npu_process_count", {}).get("max")
                for item in per_device.values()
                if isinstance(per_device, Mapping) and isinstance(item, Mapping)
            ] if isinstance(per_device, Mapping) else []
            max_processes = max(
                (float(value) for value in process_peaks if isinstance(value, (int, float))),
                default=None,
            )
            lines.append(
                "| "
                + " | ".join(
                    (
                        str(case_id),
                        str(result.get("executor")),
                        f"{aggregate.get('succeeded', 0)}/{aggregate.get('request_count', 0)}",
                        _fmt(aggregate.get("p95_e2e_ms")),
                        _fmt(aggregate.get("throughput_requests_per_second"), 4),
                        _fmt(cpu_mean),
                        _fmt(resources.get("incremental_cluster_cpu_utilization_pct")),
                        _fmt(npu_mean),
                        _fmt(max_npu_peak),
                        _fmt(resources.get("peak_incremental_hbm_mb"), 0),
                        _fmt(max_processes, 0),
                    )
                )
                + " |"
            )
    figures = summary.get("figures")
    if isinstance(figures, list) and figures:
        lines.extend(("", "## 图表", ""))
        for figure in figures:
            if not isinstance(figure, Mapping):
                continue
            title = str(figure.get("title", figure.get("id", "figure")))
            description = str(figure.get("description", ""))
            path = str(figure.get("path", ""))
            if not path:
                continue
            lines.extend(
                (
                    f"### {title}",
                    "",
                    description,
                    "",
                    f"![{title}]({path})",
                    "",
                )
            )
    lines.extend(
        (
            "",
            "## 文本、视觉与 Workflow",
            "",
            "| 执行器 | 分组 | 成功/总数 | E2E 均值 (ms) | E2E P95 (ms) | 吞吐 (req/s) |",
            "|---|---|---:|---:|---:|---:|",
        )
    )
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, Mapping):
                continue
            breakdowns = result.get("breakdowns")
            if not isinstance(breakdowns, Mapping):
                continue
            groups: list[tuple[str, object]] = [("overall", breakdowns.get("overall"))]
            for section in ("families", "workflows"):
                values = breakdowns.get(section)
                if isinstance(values, Mapping):
                    groups.extend((str(key), value) for key, value in values.items())
            for name, value in groups:
                if not isinstance(value, Mapping):
                    continue
                requests = value.get("requests")
                if not isinstance(requests, Mapping):
                    continue
                latency = requests.get("e2e_latency_ms")
                mean = latency.get("mean") if isinstance(latency, Mapping) else None
                lines.append(
                    "| "
                    + " | ".join(
                        (
                            str(result.get("executor")),
                            name,
                            f"{requests.get('succeeded', 0)}/{requests.get('request_count', 0)}",
                            _fmt(mean),
                            _fmt(requests.get("p95_e2e_ms")),
                            _fmt(requests.get("throughput_requests_per_second"), 4),
                        )
                    )
                    + " |"
                )
    lines.extend(
        (
            "",
            "## 阶段时间",
            "",
            "> 各行是独立统计，部分阶段存在包含关系，不能把表中所有均值直接相加。",
            "",
            "| 执行器 | 分组 | 阶段 | 样本数 | 均值 (ms) | P95 (ms) | 最大值 (ms) |",
            "|---|---|---|---:|---:|---:|---:|",
        )
    )
    timing_labels = {
        "queue_to_dispatch_ms": "queue -> dispatch",
        "dispatch_prepare_ms": "dispatch prepare",
        "dispatch_to_running_ms": "dispatch -> running",
        "worker_startup_ms": "worker startup",
        "model_load_ms": "model load",
        "generate_ms": "generation",
        "output_put_ms": "output put",
        "total_duration_ms": "inference total",
        "ray_roundtrip_ms": "Ray task roundtrip",
    }
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, Mapping):
                continue
            breakdowns = _mapping_or_empty(result.get("breakdowns"))
            timing_groups = {
                "overall": breakdowns.get("overall"),
                "text": _mapping_or_empty(breakdowns.get("families")).get("text"),
                "vision": _mapping_or_empty(breakdowns.get("families")).get(
                    "vision"
                ),
            }
            for group_name, group in timing_groups.items():
                timings = _mapping_or_empty(_mapping_or_empty(group).get("timings"))
                for metric, label in timing_labels.items():
                    stats = _mapping_or_empty(timings.get(metric))
                    if not stats:
                        continue
                    lines.append(
                        "| "
                        + " | ".join(
                            (
                                str(result.get("executor")),
                                group_name,
                                label,
                                str(stats.get("count", 0)),
                                _fmt(stats.get("mean")),
                                _fmt(stats.get("p95")),
                                _fmt(stats.get("max")),
                            )
                        )
                        + " |"
                    )
    lines.extend(
        (
            "",
            "## 回收审计",
            "",
            "| 执行器 | 控制面回收 | 非终态 Run | Active Worker Lease | Run-owned Placement | Standby Placement | Active Model | Route 占用 | 推理中请求 | 物理 HBM 回落 | HBM 等待 (ms) |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        )
    )
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, Mapping):
                continue
            control = result.get("control_recovery")
            physical = result.get("physical_hbm_recovery")
            control_map = _mapping_or_empty(control)
            run_owned = control_map.get("run_owned_placement_leases")
            run_owned_count = (
                sum(len(value) for value in run_owned.values() if isinstance(value, list))
                if isinstance(run_owned, Mapping)
                else None
            )
            active_models = control_map.get("active_model_instances")
            active_model_count = (
                len(active_models) if isinstance(active_models, list) else None
            )
            placement_counts = _mapping_or_empty(
                control_map.get("active_placement_lease_counts")
            )
            lines.append(
                "| "
                + " | ".join(
                    (
                        str(result.get("executor")),
                        (
                            str(control.get("recovered"))
                            if isinstance(control, Mapping)
                            else "n/a"
                        ),
                        _fmt(control_map.get("nonterminal_run_count"), 0),
                        _fmt(control_map.get("active_worker_lease_count"), 0),
                        _fmt(run_owned_count, 0),
                        _fmt(placement_counts.get("standby_worker"), 0),
                        _fmt(active_model_count, 0),
                        _fmt(control_map.get("route_occupancy"), 0),
                        _fmt(control_map.get("actual_request_inflight"), 0),
                        (
                            str(physical.get("recovered"))
                            if isinstance(physical, Mapping)
                            else "n/a"
                        ),
                        _fmt(
                            physical.get("wait_ms")
                            if isinstance(physical, Mapping)
                            else None,
                            0,
                        ),
                    )
                )
                + " |"
            )
    lines.extend(
        (
            "",
            "Maze 的全局 Standby Placement 在 Run 结束后应恢复到 24，而不是清零；"
            "Run/Attempt 拥有的 PlacementLease、WorkerLease、RouteLease 和模型实例必须清零。",
            "",
            "本次仅运行一轮，P95 和吞吐量用于 Pilot 对比，不宣称统计显著性。",
            "",
            "## 到达负载",
            "",
            "Arrival ratio 定义为 `arrival_rate × average_workflow_seconds`。报告同时保留到达率、准入窗口内完成量以及排空后的完整请求 E2E。",
            "",
            "## 可审计文件",
            "",
            "- `plan.json`：实验顺序、负载计划和冻结配置",
            "- `summary.json`：全部请求、聚合指标、环境和资源统计",
            "- `cases/*/*/runner.json`：容器内原始执行记录",
            "- `cases/*/*/resource_samples.jsonl`：宿主机 CPU/NPU/HBM 时间序列",
            "- `cases/*/*/stdout.log`、`stderr.log`：每个执行器的控制台证据",
            "",
            "## 解释边界",
            "",
            "该 Pilot 验证统一口径和执行链路，不用于宣称最终性能优劣。文件系统页缓存、运行顺序和单机容器网络均已保留在证据中，正式实验需用分块交替重复控制这些因素。",
            "",
        )
    )
    return "\n".join(lines)


def _run_orchestrator(args: argparse.Namespace) -> int:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else (DEFAULT_OUTPUT_ROOT / f"pilot-{timestamp}").resolve()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    resume: dict[str, object] | None = None
    if args.resume:
        plan, cases, order = _load_resume_state(args, output_dir)
        contract = plan.get("contract")
        assert isinstance(contract, Mapping)
        frozen_manifest = contract.get("workload_manifest")
        manifest = dict(frozen_manifest) if isinstance(frozen_manifest, Mapping) else None
        control_environment = _control_environment(args.state_root)
        resume = {
            "resumed_at_ms": int(time.time() * 1_000),
            "command": sys.argv,
            "git": _git_environment(),
            "reused_results": [],
            "rerun_results": [],
            "archived_incomplete_attempts": [],
        }
    else:
        if args.mixed_batch20:
            manifest = _build_fixed_batch20_manifest(
                _discover_mixed_candidates(args.data_root)
            )
            args.workload_manifest = output_dir / "workload_manifest.json"
            _write_json(args.workload_manifest, manifest)
        elif args.workload_manifest is not None:
            manifest = _read_json(args.workload_manifest)
            copied_manifest_path = output_dir / "workload_manifest.json"
            _write_json(copied_manifest_path, manifest)
            args.workload_manifest = copied_manifest_path
        else:
            manifest = None
        cases = _build_cases(args)
        order = _execution_order(cases, str(args.executor))
        control_environment = _control_environment(args.state_root)
        plan = {
            "schema_version": SCHEMA_VERSION,
            "objective": OBJECTIVE,
            "created_at_ms": int(time.time() * 1_000),
            "executor": args.executor,
            "cases": [case.payload() for case in cases],
            "execution_order": [
                {
                    "ordinal": ordinal,
                    "case_id": case.case_id,
                    "executor": executor,
                    "pair_position": pair_position,
                }
                for ordinal, (case, executor, pair_position) in enumerate(
                    order, start=1
                )
            ],
            "contract": {
                "workload_manifest": manifest,
                "text_model_id": TEXT_MODEL_ID,
                "text_model_path": str(args.text_model_path),
                "vision_model_id": VISION_MODEL_ID,
                "vision_model_path": str(args.vision_model_path),
                "inference_backend": "transformers",
                "generation_method": "manual_greedy",
                "max_tokens": 4096,
                "temperature": 0.0,
                "max_model_len_by_family": {"text": 10240, "vision": 12288},
                "request_timeout_seconds": float(args.request_timeout_seconds),
                "case_timeout_seconds": float(args.case_timeout_seconds),
                "resource_sample_interval_seconds": float(
                    args.resource_sample_interval_seconds
                ),
                "resource_baseline_seconds": float(args.resource_baseline_seconds),
                "resource_recovery_timeout_seconds": float(
                    args.resource_recovery_timeout_seconds
                ),
                "hbm_recovery_tolerance_mb": int(
                    args.hbm_recovery_tolerance_mb
                ),
                "model_load_in_request_e2e": True,
                "destroy_in_request_e2e": False,
                "ray_worker_max_calls": ray_smoke.RAY_TASK_MAX_CALLS,
                "ray_task_num_cpus": float(args.ray_task_num_cpus),
            },
            "control_environment": control_environment,
            "git": _git_environment(),
            "command": sys.argv,
            "output_dir": str(output_dir),
        }
        _write_json(output_dir / "plan.json", plan)
    if args.plan_only:
        summary = {
            **plan,
            "result": "plan_only_succeeded",
            "results": [],
        }
        _write_json(output_dir / "summary.json", summary)
        (output_dir / "report.md").write_text(_render_report(summary), encoding="utf-8")
        print(
            json.dumps({"result": "plan_only_succeeded", "output_dir": str(output_dir)})
        )
        return 0
    if control_environment.get("profile") != "performance":
        raise PerformancePilotError(
            "logical Controller is not using the performance profile; restart with "
            "deploy/logical_cluster/logical_cluster.sh control-up performance"
        )
    if not args.text_model_path.is_dir():
        raise PerformancePilotError(
            f"Qwen3-4B model path is missing: {args.text_model_path}"
        )
    if manifest is not None and not args.vision_model_path.is_dir():
        raise PerformancePilotError(
            f"Qwen2.5-VL-3B model path is missing: {args.vision_model_path}"
        )
    container_output_dir = _container_output_path(output_dir, args.state_root)
    results: list[dict[str, object]] = []
    for ordinal, (case, executor, pair_position) in enumerate(order, start=1):
        reused = (
            _completed_case_result(output_dir, case, executor)
            if args.resume
            else None
        )
        if reused is not None:
            assert resume is not None
            resume["reused_results"].append(  # type: ignore[union-attr]
                {"case_id": case.case_id, "executor": executor}
            )
            result = reused
            print(
                json.dumps(
                    {
                        "event": "case_reused",
                        "ordinal": ordinal,
                        "case_id": case.case_id,
                        "executor": executor,
                    }
                ),
                flush=True,
            )
        else:
            if args.resume:
                assert resume is not None
                archive = _archive_incomplete_case(output_dir, case, executor)
                if archive is not None:
                    resume["archived_incomplete_attempts"].append(  # type: ignore[union-attr]
                        {
                            "case_id": case.case_id,
                            "executor": executor,
                            "path": str(archive),
                        }
                    )
                resume["rerun_results"].append(  # type: ignore[union-attr]
                    {"case_id": case.case_id, "executor": executor}
                )
            print(
                json.dumps(
                    {
                        "event": "case_start",
                        "ordinal": ordinal,
                        "case_id": case.case_id,
                        "executor": executor,
                    }
                ),
                flush=True,
            )
            result = _run_container_worker(
                args=args,
                executor=executor,
                case=case,
                output_dir=output_dir,
                container_output_dir=container_output_dir,
            )
        print(
            json.dumps(
                {
                    "event": "case_finish",
                    "ordinal": ordinal,
                    "case_id": case.case_id,
                    "executor": executor,
                    "aggregate": result.get("aggregate"),
                }
            ),
            flush=True,
        )
        result["execution_ordinal"] = ordinal
        result["pair_position"] = pair_position
        results.append(result)
        _write_json(
            output_dir / "partial_summary.json",
            {**plan, "resume": resume, "results": results},
        )
    failed = [
        result
        for result in results
        if not isinstance(result.get("aggregate"), Mapping)
        or result["aggregate"].get("failed") != 0  # type: ignore[index]
        or result.get("process", {}).get("exit_code") != 0  # type: ignore[union-attr]
        or result.get("physical_hbm_recovery", {}).get("recovered") is not True  # type: ignore[union-attr]
        or (
            result.get("executor") == "maze"
            and result.get("control_recovery", {}).get("recovered") is not True  # type: ignore[union-attr]
        )
    ]
    summary = {
        **plan,
        "completed_at_ms": int(time.time() * 1_000),
        "result": "succeeded" if not failed else "failed",
        "result_count": len(results),
        "failed_result_count": len(failed),
        "resume": resume,
        "results": results,
    }
    summary["figures"] = logical_figures.write_figures(summary, output_dir)
    _write_json(output_dir / "summary.json", summary)
    (output_dir / "report.md").write_text(_render_report(summary), encoding="utf-8")
    print(
        json.dumps(
            {
                "result": summary["result"],
                "output_dir": str(output_dir),
                "report": str(output_dir / "report.md"),
            }
        )
    )
    return 0 if not failed else 20


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run paired Maze/Ray performance cases on the logical cluster."
    )
    parser.add_argument(
        "--executor", choices=("paired", "maze", "ray"), default="paired"
    )
    parser.add_argument(
        "--mode",
        action="append",
        choices=("batch", "arrival"),
        default=None,
    )
    parser.add_argument("--batch-size", action="append", type=int, default=None)
    parser.add_argument("--arrival-ratio", action="append", type=float, default=None)
    parser.add_argument("--average-workflow-seconds", type=float, default=30.0)
    parser.add_argument("--arrival-window-seconds", type=float, default=130.0)
    parser.add_argument("--resource-sample-interval-seconds", type=float, default=1.0)
    parser.add_argument("--resource-baseline-seconds", type=float, default=3.0)
    parser.add_argument("--resource-recovery-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--hbm-recovery-tolerance-mb", type=int, default=1024)
    parser.add_argument("--request-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--case-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--ray-task-num-cpus", type=float, default=20.0)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--data-root", type=Path, default=qwen_smoke.DEFAULT_DATA_ROOT)
    parser.add_argument("--text-model-path", type=Path, default=TEXT_MODEL_PATH)
    parser.add_argument("--vision-model-path", type=Path, default=VISION_MODEL_PATH)
    parser.add_argument(
        "--mixed-batch20",
        action="store_true",
        help="run the fixed 14-workflow mixed text/vision Batch=20 pilot",
    )
    parser.add_argument(
        "--workload-manifest",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "resume a frozen output directory, reuse complete case results, and "
            "rerun only cases missing host resource evidence"
        ),
    )
    parser.add_argument(
        "--internal-worker",
        choices=("maze", "ray"),
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--case-file", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--result-file", type=Path, default=None, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--control-socket",
        type=Path,
        default=DEFAULT_CONTROL_SOCKET,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    if args.resume and args.output_dir is None:
        parser.error("--resume requires --output-dir")
    if args.resume and args.plan_only:
        parser.error("--resume cannot be combined with --plan-only")
    if args.resume and args.internal_worker is not None:
        parser.error("--resume is only valid for the host orchestrator")
    if args.mode is None:
        args.mode = ["batch", "arrival"]
    if args.batch_size is None:
        args.batch_size = [1, 2]
    if args.arrival_ratio is None:
        args.arrival_ratio = [0.25]
    if args.mixed_batch20:
        args.mode = ["batch"]
        args.batch_size = [FIXED_BATCH_SIZE]
        args.arrival_ratio = []
    if any(item < 1 for item in args.batch_size):
        parser.error("--batch-size must be positive")
    if any(item <= 0 for item in args.arrival_ratio):
        parser.error("--arrival-ratio must be positive")
    for name in (
        "average_workflow_seconds",
        "arrival_window_seconds",
        "resource_sample_interval_seconds",
        "resource_baseline_seconds",
        "resource_recovery_timeout_seconds",
        "request_timeout_seconds",
        "case_timeout_seconds",
        "ray_task_num_cpus",
    ):
        if float(getattr(args, name)) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.hbm_recovery_tolerance_mb < 0:
        parser.error("--hbm-recovery-tolerance-mb must be non-negative")
    args.state_root = args.state_root.expanduser().resolve()
    args.data_root = args.data_root.expanduser().resolve()
    args.text_model_path = args.text_model_path.expanduser().resolve()
    args.vision_model_path = args.vision_model_path.expanduser().resolve()
    if args.workload_manifest is not None:
        args.workload_manifest = args.workload_manifest.expanduser().resolve()
    if args.case_file is not None:
        args.case_file = args.case_file.expanduser().resolve()
    if args.result_file is not None:
        args.result_file = args.result_file.expanduser().resolve()
    args.control_socket = args.control_socket.expanduser().resolve()
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.internal_worker is not None:
        return _run_internal_worker(args)
    try:
        return _run_orchestrator(args)
    except PerformancePilotError as exc:
        print(f"logical-cluster performance preflight failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
