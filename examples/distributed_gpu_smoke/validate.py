#!/usr/bin/env python
"""Run a distributed Maze GPU placement smoke test."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from maze import MaClient

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from register_ray_workers import register_ray_workers  # noqa: E402


GPU_PROBE_CODE = r'''
def gpu_probe(task_name: str, sleep_seconds: int = 1):
    import os
    import socket
    import subprocess
    import time

    import ray

    time.sleep(int(sleep_seconds))

    gpu_name = ""
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=5,
        ).strip().splitlines()
        gpu_name = output[0] if output else ""
    except Exception as exc:
        gpu_name = f"nvidia-smi failed: {type(exc).__name__}: {exc}"

    return {
        "task_name": task_name,
        "hostname": socket.gethostname(),
        "node_ip": ray.util.get_node_ip_address(),
        "node_id": ray.get_runtime_context().get_node_id(),
        "pid": os.getpid(),
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "gpu_name": gpu_name,
    }
'''


def _save_probe_task(workflow, task_name: str, sleep_seconds: int):
    task = workflow.add_task(task_type="code", task_name=task_name)
    task.save(
        code_str=GPU_PROBE_CODE,
        task_input={
            "input_params": {
                "1": {
                    "key": "task_name",
                    "input_schema": "from_user",
                    "data_type": "str",
                    "value": task_name,
                    "has_value": True,
                },
                "2": {
                    "key": "sleep_seconds",
                    "input_schema": "from_user",
                    "data_type": "int",
                    "value": sleep_seconds,
                    "has_value": True,
                },
            }
        },
        task_output={
            "output_params": {
                "1": {"key": "task_name", "data_type": "str"},
                "2": {"key": "hostname", "data_type": "str"},
                "3": {"key": "node_ip", "data_type": "str"},
                "4": {"key": "node_id", "data_type": "str"},
                "5": {"key": "pid", "data_type": "int"},
                "6": {"key": "cuda_visible_devices", "data_type": "str"},
                "7": {"key": "gpu_name", "data_type": "str"},
            }
        },
        resources={"cpu": 1, "cpu_mem": 0, "gpu": 1, "gpu_mem": 0},
    )
    return task


def _print_table(rows: list[dict]) -> None:
    headers = [
        "task_name",
        "maze_task_id",
        "node_id",
        "node_ip",
        "cuda_visible_devices",
        "gpu_name",
    ]
    widths = {header: len(header) for header in headers}
    for row in rows:
        for header in headers:
            widths[header] = max(widths[header], len(str(row.get(header, ""))))

    print("  ".join(header.ljust(widths[header]) for header in headers))
    print("  ".join("-" * widths[header] for header in headers))
    for row in rows:
        print("  ".join(str(row.get(header, "")).ljust(widths[header]) for header in headers))


def run_smoke(server_url: str, num_tasks: int, sleep_seconds: int) -> list[dict]:
    client = MaClient(server_url)
    workflow = client.create_workflow()
    for index in range(num_tasks):
        _save_probe_task(workflow, f"gpu_probe_{index}", sleep_seconds)

    run_id = workflow.run()
    messages = workflow.get_results(run_id, verbose=False)
    starts = {
        message["data"]["task_id"]: message["data"]
        for message in messages
        if message.get("type") == "start_task"
    }
    exceptions = [message for message in messages if message.get("type") == "task_exception"]
    if exceptions:
        print(json.dumps(exceptions, indent=2))
        raise RuntimeError("Distributed GPU smoke failed with task_exception")

    rows = []
    for message in messages:
        if message.get("type") != "finish_task":
            continue
        data = message["data"]
        result = data.get("result") or {}
        start = starts.get(data["task_id"], {})
        rows.append({
            "task_name": result.get("task_name"),
            "maze_task_id": data["task_id"][:8],
            "node_id": (result.get("node_id") or start.get("node_id") or "")[:10],
            "node_ip": result.get("node_ip") or start.get("node_ip"),
            "cuda_visible_devices": result.get("cuda_visible_devices"),
            "gpu_name": result.get("gpu_name"),
        })

    print(f"run_id = {run_id}")
    _print_table(rows)
    unique_nodes = sorted({row["node_ip"] for row in rows if row.get("node_ip")})
    print(f"unique_node_ips = {', '.join(unique_nodes)}")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate distributed Maze GPU placement")
    parser.add_argument("--server-url", default="http://127.0.0.1:8000", help="Maze core URL")
    parser.add_argument("--num-tasks", type=int, default=4, help="Number of independent GPU tasks")
    parser.add_argument("--sleep-seconds", type=int, default=1, help="Seconds each GPU task sleeps")
    parser.add_argument(
        "--register-workers",
        action="store_true",
        help="Register all live Ray worker nodes with Maze before running the smoke",
    )
    args = parser.parse_args()

    if args.register_workers:
        registered = register_ray_workers(args.server_url)
        print("registered_workers")
        print(json.dumps(registered, indent=2))

    rows = run_smoke(args.server_url, args.num_tasks, args.sleep_seconds)
    if len({row.get("node_ip") for row in rows}) < 2 and args.num_tasks > 1:
        raise SystemExit("Smoke completed, but all tasks ran on one node")


if __name__ == "__main__":
    main()
