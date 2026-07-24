#!/usr/bin/env python3
"""Run real-Qwen Workflow acceptance checks through the live C13 Controller."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
from pathlib import Path
import sys
import time
from typing import Any

TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import qwen_benchmark_smoke as smoke  # noqa: E402

from ascend_maze.control.local_rpc import UdsRuntimeClient  # noqa: E402


DEFAULT_SOCKET = Path("/workspace/state/control-plane/control.sock")
DEFAULT_OUTPUT = Path("/workspace/state/output/logical-cluster-e2e")
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
FORBIDDEN_TASK_PARAMETERS = {
    "dag_context",
    "data_handle",
    "device_binding",
    "runtime_node_binding",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate text and vision Workflows through the live 8-node Controller."
    )
    parser.add_argument("--control-socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--data-root", type=Path, default=smoke.DEFAULT_DATA_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--family",
        choices=("all", "text", "vision"),
        default="all",
    )
    parser.add_argument("--run-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--recovery-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.25)
    parser.add_argument("--hbm-recovery-tolerance-mb", type=int, default=1024)
    parser.add_argument(
        "--require-cross-node-text",
        action="store_true",
        help="Fail a text acceptance Run unless its Tasks execute on at least two nodes.",
    )
    args = parser.parse_args()
    if args.run_timeout_seconds <= 0 or args.recovery_timeout_seconds <= 0:
        parser.error("timeouts must be positive")
    if args.poll_interval_seconds <= 0:
        parser.error("poll interval must be positive")
    if args.hbm_recovery_tolerance_mb < 0:
        parser.error("HBM recovery tolerance must be non-negative")
    return args


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(smoke._jsonable(value), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _discover_sample(data_root: Path, family: str) -> Any:
    dataset = "tbench" if family == "text" else "gaia"
    workflow = "retail_cancel" if family == "text" else "vision"
    samples, failures = smoke.discover_samples(
        data_root=data_root,
        datasets={dataset},
        workflows={workflow},
        families={family},
        samples_per_workflow=1,
        sample_offset=0,
        max_inline_file_bytes=64 * 1024 * 1024,
    )
    if failures:
        raise RuntimeError(f"sample discovery failed: {failures}")
    if len(samples) != 1:
        raise RuntimeError(f"expected one {family} sample, found {len(samples)}")
    return samples[0]


def _task_contracts(workflow: object) -> tuple[list[dict[str, object]], bool]:
    contracts: list[dict[str, object]] = []
    clean = True
    for draft in getattr(workflow, "_draft_tasks"):
        function = draft.template.func
        parameters = tuple(inspect.signature(function).parameters)
        forbidden = tuple(sorted(set(parameters) & FORBIDDEN_TASK_PARAMETERS))
        clean = clean and not forbidden
        contracts.append(
            {
                "task_name": draft.task_name,
                "function": function.__qualname__,
                "parameters": parameters,
                "forbidden_parameters": forbidden,
            }
        )
    return contracts, clean


def _hbm_free_by_device(cluster: dict[str, object]) -> dict[str, int]:
    result: dict[str, int] = {}
    payload = cluster.get("cluster")
    if not isinstance(payload, dict):
        return result
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return result
    for node in nodes:
        if not isinstance(node, dict):
            continue
        capacity = node.get("capacity")
        if not isinstance(capacity, dict):
            continue
        npus = capacity.get("npus")
        if not isinstance(npus, list):
            continue
        for npu in npus:
            if not isinstance(npu, dict):
                continue
            device_id = npu.get("device_id")
            free_hbm = npu.get("observed_free_hbm_mb")
            if isinstance(device_id, str) and isinstance(free_hbm, int):
                result[device_id] = free_hbm
    return result


def _task_timings(
    terminal_run: dict[str, object],
    task_names: dict[str, str],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    states = terminal_run.get("task_states")
    if not isinstance(states, list):
        return records
    for state in states:
        if not isinstance(state, dict):
            continue
        task_id = state.get("task_id")
        attempts = state.get("attempts")
        if not isinstance(task_id, str) or not isinstance(attempts, list):
            continue
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            dispatched = attempt.get("dispatched_at_ms")
            worker_started = attempt.get("worker_started_at_ms")
            finished = attempt.get("finished_at_ms")
            records.append(
                {
                    "task_id": task_id,
                    "task_name": task_names.get(task_id, task_id),
                    "attempt": attempt.get("attempt"),
                    "status": attempt.get("status"),
                    "node_id": attempt.get("node_id"),
                    "device_ids": attempt.get("device_ids", []),
                    "dispatch_to_worker_ms": (
                        worker_started - dispatched
                        if isinstance(dispatched, int)
                        and isinstance(worker_started, int)
                        else None
                    ),
                    "worker_execution_ms": (
                        finished - worker_started
                        if isinstance(worker_started, int) and isinstance(finished, int)
                        else None
                    ),
                    "attempt_total_ms": (
                        finished - dispatched
                        if isinstance(dispatched, int) and isinstance(finished, int)
                        else None
                    ),
                }
            )
    return records


def _model_device_evidence(
    task_timings: list[dict[str, object]],
    model_task_ids: set[str],
    model_snapshots: list[dict[str, object]],
    target_model_id: str,
) -> list[dict[str, object]]:
    evidence: dict[tuple[str, ...], dict[str, object]] = {}
    for timing in task_timings:
        task_id = timing.get("task_id")
        if task_id not in model_task_ids:
            continue
        devices = timing.get("device_ids")
        if not isinstance(devices, list):
            continue
        for device_id in devices:
            if not isinstance(device_id, str) or not device_id:
                continue
            key = ("local_worker", str(task_id), device_id)
            evidence[key] = {
                "source": "local_worker_task",
                "task_id": task_id,
                "task_name": timing.get("task_name"),
                "node_id": timing.get("node_id"),
                "physical_device_id": device_id,
            }

    for snapshot in model_snapshots:
        instances = snapshot.get("instances")
        if not isinstance(instances, list):
            continue
        for instance in instances:
            if not isinstance(instance, dict):
                continue
            if instance.get("model_id") != target_model_id:
                continue
            instance_id = instance.get("instance_id")
            device_id = instance.get("npu_device_id")
            if not isinstance(instance_id, str) or not instance_id:
                continue
            if not isinstance(device_id, str) or not device_id:
                continue
            generation = instance.get("generation")
            key = ("service", instance_id, str(generation), device_id)
            item = evidence.setdefault(
                key,
                {
                    "source": "service_model_instance",
                    "instance_id": instance_id,
                    "instance_generation": generation,
                    "model_id": target_model_id,
                    "node_id": instance.get("node_id"),
                    "physical_device_id": device_id,
                    "route_occupancy_observed": False,
                    "request_inflight_observed": False,
                },
            )
            item["route_occupancy_observed"] = bool(
                item["route_occupancy_observed"]
                or (
                    isinstance(instance.get("route_occupancy"), int)
                    and instance["route_occupancy"] > 0
                )
            )
            item["request_inflight_observed"] = bool(
                item["request_inflight_observed"]
                or (
                    isinstance(instance.get("actual_request_inflight"), int)
                    and instance["actual_request_inflight"] > 0
                )
            )
    return [evidence[key] for key in sorted(evidence)]


def _task_node_ids(task_timings: list[dict[str, object]]) -> list[str]:
    return sorted(
        {
            node_id
            for timing in task_timings
            if isinstance((node_id := timing.get("node_id")), str) and node_id
        }
    )


def _require_cross_node_text(
    family: str,
    task_node_ids: list[str],
    *,
    required: bool,
) -> None:
    if required and family == "text" and len(task_node_ids) < 2:
        raise RuntimeError(
            "text Workflow did not cross logical nodes: "
            + (", ".join(task_node_ids) or "no Task placement evidence")
        )


async def _capture_run(
    client: UdsRuntimeClient,
    run_id: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    run_snapshots: list[dict[str, object]] = []
    worker_snapshots: list[dict[str, object]] = []
    model_snapshots: list[dict[str, object]] = []
    previous: tuple[str, str, str] | None = None
    while True:
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Run {run_id} did not terminate")
        shown, workers, models = await asyncio.gather(
            client.query("GetRun", resource_id=run_id, timeout_seconds=10.0),
            client.query("GetWorkerPools", timeout_seconds=10.0),
            client.query("GetModelInstances", timeout_seconds=10.0),
        )
        run = shown.get("run")
        if not isinstance(run, dict):
            raise RuntimeError("GetRun returned no Run snapshot")
        signature = (
            str(run.get("status")),
            json.dumps(shown.get("placements", []), sort_keys=True),
            json.dumps(workers.get("worker_pool", {}), sort_keys=True),
        )
        if signature != previous:
            run_snapshots.append(shown)
            worker_snapshots.append(workers)
            model_snapshots.append(models)
            previous = signature
        if run.get("status") in TERMINAL_STATES:
            return run, run_snapshots, worker_snapshots, model_snapshots
        await asyncio.sleep(poll_interval_seconds)


async def _capture_watch(
    client: UdsRuntimeClient,
    run_id: str,
    timeout_seconds: float,
) -> list[dict[str, object]]:
    return [
        batch
        async for batch in client.watch_run(
            run_id,
            timeout_seconds=timeout_seconds,
        )
    ]


def _run_owned_leases(cluster: dict[str, object], run_id: str) -> list[object]:
    payload = cluster.get("cluster")
    if not isinstance(payload, dict):
        return []
    leases = payload.get("active_leases")
    if not isinstance(leases, list):
        return []
    return [
        lease
        for lease in leases
        if isinstance(lease, dict)
        and isinstance(lease.get("lease"), dict)
        and lease["lease"].get("run_id") == run_id
    ]


def _active_worker_leases(pool: dict[str, object]) -> list[object]:
    leases = pool.get("worker_leases")
    if not isinstance(leases, list):
        return []
    return [
        lease
        for lease in leases
        if isinstance(lease, dict) and not lease.get("released", False)
    ]


async def _wait_recovered(
    client: UdsRuntimeClient,
    run_id: str,
    baseline_hbm: dict[str, int],
    used_devices: set[str],
    *,
    timeout_seconds: float,
    tolerance_mb: int,
) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last: dict[str, object] = {}
    while True:
        system, cluster, workers = await asyncio.gather(
            client.query("GetSystemSnapshot", timeout_seconds=10.0),
            client.query(
                "GetClusterSnapshot", filter="resources", timeout_seconds=10.0
            ),
            client.query("GetWorkerPools", timeout_seconds=10.0),
        )
        pool = workers.get("worker_pool")
        if not isinstance(pool, dict):
            pool = {}
        active_worker_leases = _active_worker_leases(pool)
        final_hbm = _hbm_free_by_device(cluster)
        hbm_recovered = all(
            final_hbm.get(device_id, -1)
            >= baseline_hbm.get(device_id, 0) - tolerance_mb
            for device_id in used_devices
        )
        last = {
            "system": system,
            "cluster": cluster,
            "worker_pools": workers,
            "baseline_hbm_free_mb": baseline_hbm,
            "final_hbm_free_mb": final_hbm,
            "used_devices": sorted(used_devices),
            "run_owned_leases": _run_owned_leases(cluster, run_id),
            "active_worker_leases": active_worker_leases,
            "hbm_recovered": hbm_recovered,
        }
        if (
            system.get("nonterminal_run_count") == 0
            and pool.get("active_worker_lease_count") == 0
            and not active_worker_leases
            and not last["run_owned_leases"]
            and hbm_recovered
        ):
            last["recovered"] = True
            return last
        if asyncio.get_running_loop().time() >= deadline:
            last["recovered"] = False
            return last
        await asyncio.sleep(1.0)


async def _run_family(
    client: UdsRuntimeClient,
    family: str,
    args: argparse.Namespace,
    output_dir: Path,
) -> dict[str, object]:
    sample = _discover_sample(args.data_root, family)
    target_model_id = "qwen3-4b-e2e" if family == "text" else "qwen2_5-vl-3b-e2e"
    workflow, aliases = smoke._build_workflow(
        sample.dataset,
        sample.workflow,
        target_model_id,
    )
    contracts, contracts_clean = _task_contracts(workflow)
    if not contracts_clean:
        raise RuntimeError("Workflow exposes an internal runtime parameter")
    compiled = workflow.compile()
    task_names = {
        task_id: task.task_name for task_id, task in compiled.tasks.items_tuple()
    }
    submission_id = (
        "logical-e2e-"
        + hashlib.sha256(
            f"{sample.sample_id}:{target_model_id}:{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:24]
    )
    record: dict[str, object] = {
        "schema_version": 1,
        "family": family,
        "sample": sample.manifest(),
        "target_model_id": target_model_id,
        "model_aliases": aliases,
        "workflow_fingerprint": compiled.workflow_fingerprint,
        "task_names": task_names,
        "task_contracts": contracts,
        "task_contracts_clean": contracts_clean,
        "submission_id": submission_id,
        "status": "not_started",
    }
    run_id: str | None = None
    destroyed = False
    started = time.perf_counter()
    baseline_cluster = await client.query(
        "GetClusterSnapshot", filter="resources", timeout_seconds=10.0
    )
    baseline_hbm = _hbm_free_by_device(baseline_cluster)
    try:
        prepare_started = time.perf_counter()
        prepared = await client.prepare_submission(
            workflow,
            inputs=sample.inputs,
            submission_id=submission_id,
            session_key=f"{submission_id}-session",
            run_deadline_ms=int(args.run_timeout_seconds * 1_000),
        )
        record["prepare_submission_ms"] = int(
            (time.perf_counter() - prepare_started) * 1_000
        )
        submit_started = time.perf_counter()
        outcome = await client.submit_prepared(prepared, timeout_seconds=60.0)
        record["submit_roundtrip_ms"] = int(
            (time.perf_counter() - submit_started) * 1_000
        )
        record["submission"] = outcome
        value = outcome.get("run_id")
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"submission did not commit: {outcome}")
        run_id = value
        watch_task = asyncio.create_task(
            _capture_watch(client, run_id, args.run_timeout_seconds)
        )
        terminal, runs, workers, models = await _capture_run(
            client,
            run_id,
            timeout_seconds=args.run_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
        record["terminal_run"] = terminal
        record["run_snapshots"] = runs
        record["worker_snapshots"] = workers
        record["model_instance_snapshots"] = models
        record["watch_batches"] = await watch_task
        record["task_timings"] = _task_timings(terminal, task_names)
        record["task_node_ids"] = _task_node_ids(record["task_timings"])
        if terminal.get("status") != "succeeded":
            raise RuntimeError(f"Run terminated as {terminal.get('status')}")
        results: dict[str, object] = {}
        for task_id in compiled.exit_tasks:
            results[task_names[task_id]] = await client.materialize_task_result(
                run_id, task_id
            )
        record["exit_task_results"] = results
        model_task_ids = {
            task_id
            for task_id, task in compiled.tasks.items_tuple()
            if task.model_anchor is not None
        }
        device_evidence = _model_device_evidence(
            record["task_timings"],
            model_task_ids,
            models,
            target_model_id,
        )
        used_devices = {str(item["physical_device_id"]) for item in device_evidence}
        record["model_task_ids"] = sorted(model_task_ids)
        record["model_device_evidence"] = device_evidence
        record["used_physical_devices"] = sorted(used_devices)
        if not used_devices:
            raise RuntimeError("model Task has no verified physical NPU evidence")
        _require_cross_node_text(
            family,
            record["task_node_ids"],
            required=args.require_cross_node_text,
        )
        destroy_started = time.perf_counter()
        record["destroy_result"] = await client.run_action(
            "DestroyRun",
            run_id,
            force=True,
            timeout_seconds=120.0,
        )
        record["destroy_ms"] = int((time.perf_counter() - destroy_started) * 1_000)
        destroyed = True
        recovery = await _wait_recovered(
            client,
            run_id,
            baseline_hbm,
            used_devices,
            timeout_seconds=args.recovery_timeout_seconds,
            tolerance_mb=args.hbm_recovery_tolerance_mb,
        )
        record["recovery"] = recovery
        if not recovery["recovered"]:
            raise RuntimeError("Run resources or NPU HBM did not recover")
        record["status"] = "succeeded"
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        if run_id is not None and not destroyed:
            try:
                record["cancel_result"] = await client.run_action(
                    "CancelRun", run_id, reason="logical_e2e_cleanup", force=True
                )
            except Exception as cleanup_exc:
                record.setdefault("cleanup_errors", []).append(
                    f"cancel:{type(cleanup_exc).__name__}:{cleanup_exc}"
                )
            try:
                record["destroy_result"] = await client.run_action(
                    "DestroyRun", run_id, force=True, timeout_seconds=120.0
                )
            except Exception as cleanup_exc:
                record.setdefault("cleanup_errors", []).append(
                    f"destroy:{type(cleanup_exc).__name__}:{cleanup_exc}"
                )
    finally:
        record["client_e2e_ms"] = int((time.perf_counter() - started) * 1_000)
        _write_json(output_dir / f"{family}.json", record)
    return record


async def run(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    client = UdsRuntimeClient(args.control_socket.expanduser().resolve())
    try:
        status = await client.get_controller_status(timeout_seconds=10.0)
        system = await client.query("GetSystemSnapshot", timeout_seconds=10.0)
        catalog = await client.query("GetModelCatalog", timeout_seconds=10.0)
        model_ids = {
            model.get("model_id")
            for model in catalog.get("models", [])
            if isinstance(model, dict)
        }
        selected = ("text", "vision") if args.family == "all" else (str(args.family),)
        required = {
            "qwen3-4b-e2e" if family == "text" else "qwen2_5-vl-3b-e2e"
            for family in selected
        }
        if status.healthy_node_count != 8:
            raise RuntimeError(
                f"expected 8 healthy logical nodes, found {status.healthy_node_count}"
            )
        if not required <= model_ids:
            raise RuntimeError(
                f"model catalog is missing {sorted(required - model_ids)}"
            )
        records = [
            await _run_family(client, family, args, output_dir) for family in selected
        ]
        summary = {
            "schema_version": 1,
            "controller_status": status,
            "system_before": system,
            "model_catalog": catalog,
            "records": [
                {
                    "family": record["family"],
                    "status": record["status"],
                    "sample": record["sample"],
                    "client_e2e_ms": record.get("client_e2e_ms"),
                    "used_physical_devices": record.get("used_physical_devices", []),
                }
                for record in records
            ],
            "succeeded": all(record["status"] == "succeeded" for record in records),
        }
        _write_json(output_dir / "summary.json", summary)
        print(json.dumps(smoke._jsonable(summary), sort_keys=True))
        return 0 if summary["succeeded"] else 1
    finally:
        client.close()


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
