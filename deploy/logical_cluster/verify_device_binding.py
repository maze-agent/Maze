#!/usr/bin/env python3
"""Exercise DeviceBinding in a child Worker and verify physical NPU cleanup."""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

from ascend_maze.ascend.dcmi import DcmiDeviceAdapter
from ascend_maze.ascend.torch_runtime import bind_torch_npu_device
from ascend_maze.contracts.resources import PlacementLease, ReservationVector
from ascend_maze.contracts.runtime import (
    DeviceBinding,
    RuntimeDeviceMapping,
    RuntimeNodeBinding,
)


def _worker(args: argparse.Namespace) -> int:
    faulthandler.dump_traceback_later(30, repeat=True)
    mapping = RuntimeDeviceMapping(
        physical_device_id=args.physical_device_id,
        runtime_visible_device_id=args.runtime_visible_device_id,
        visible_device_index=args.visible_device_index,
    )
    node_binding = RuntimeNodeBinding(
        node_id=args.node_id,
        boot_id="binding-probe-boot",
        ray_node_id="binding-probe-ray-node",
        runtime_generation=1,
        agent_generation="binding-probe-agent",
        agent_endpoint="127.0.0.1:1",
        producer_id="binding-probe-producer",
        device_mappings=(mapping,),
    )
    lease = PlacementLease(
        lease_id="binding-probe-lease",
        reservation_kind="task",
        run_id="binding-probe-run",
        task_id="binding-probe-task",
        attempt=1,
        node_id=args.node_id,
        boot_id=node_binding.boot_id,
        npu_device_id=args.physical_device_id,
        resources=ReservationVector(1, 1_024, 0, args.allocation_mb, 1),
        snapshot_version=1,
        created_at_ms=1,
        dispatch_deadline_ms=60_000,
        allow_npu_colocation=False,
    )
    binding = DeviceBinding.from_lease(lease, node_binding)
    runtime = bind_torch_npu_device(binding)
    allocation = runtime.torch.empty(
        (args.allocation_mb * 1024 * 1024,),
        dtype=runtime.torch.uint8,
        device=f"npu:{binding.visible_device_index}",
    )
    payload = {
        "binding_verified": True,
        "environment_variables": dict(binding.environment_variables.items_tuple()),
        "physical_device_id": binding.physical_device_id,
        "process_hbm_mb": runtime.process_hbm_mb(),
        "runtime_visible_device_id": binding.runtime_visible_device_id,
        "visible_device_index": binding.visible_device_index,
        "worker_pid": runtime.worker_pid,
    }
    Path(args.ready_file).write_text(
        json.dumps(payload, sort_keys=True), encoding="ascii"
    )
    faulthandler.cancel_dump_traceback_later()
    if args.release_file:
        release_file = Path(args.release_file)
        while not release_file.exists():
            time.sleep(0.1)
    else:
        sys.stdin.readline()
    del allocation
    runtime.torch.npu.empty_cache()
    return 0


def _wait_for_ready(
    ready_file: Path,
    process: subprocess.Popen[str],
    timeout_seconds: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if ready_file.is_file():
            return json.loads(ready_file.read_text(encoding="ascii"))
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(
                "binding Worker exited before readiness: "
                f"code={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
            )
        time.sleep(0.1)
    process.kill()
    stdout, stderr = process.communicate()
    raise TimeoutError(
        "binding Worker readiness timed out: "
        f"stdout={stdout!r}, stderr={stderr!r}"
    )


def _ready_worker_pid(ready: dict[str, object]) -> int:
    value = ready.get("worker_pid")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f"binding Worker returned an invalid PID: {value!r}")
    return value


def _wait_for_hbm_recovery(
    *,
    monitor: DcmiDeviceAdapter,
    physical_device_id: str,
    worker_pid: int,
    baseline_hbm_mb: int,
    tolerance_mb: int,
    timeout_seconds: float,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    final_hbm_mb = monitor.device(physical_device_id).used_hbm_mb
    while time.monotonic() < deadline:
        process_hbm = monitor.process_hbm_mb(physical_device_id, worker_pid)
        final_hbm_mb = monitor.device(physical_device_id).used_hbm_mb
        if (
            process_hbm is None
            and final_hbm_mb <= baseline_hbm_mb + tolerance_mb
        ):
            return final_hbm_mb
        time.sleep(0.2)
    raise TimeoutError(
        "Worker HBM did not recover before deadline: "
        f"baseline={baseline_hbm_mb}, final={final_hbm_mb}, "
        f"tolerance={tolerance_mb}, pid={worker_pid}"
    )


def _orchestrator(args: argparse.Namespace) -> int:
    monitor = DcmiDeviceAdapter()
    devices = monitor.devices()
    physical_ids = tuple(item.physical_device_id for item in devices)
    if args.physical_device_id not in physical_ids:
        raise RuntimeError(
            f"physical NPU {args.physical_device_id} is absent from DCMI {physical_ids}"
        )
    baseline_hbm_mb = monitor.device(args.physical_device_id).used_hbm_mb
    temporary_root = Path(os.environ.get("TMPDIR", tempfile.gettempdir()))
    with tempfile.TemporaryDirectory(
        prefix="ascend-maze-binding-", dir=temporary_root
    ) as directory:
        ready_file = Path(directory) / "ready.json"
        command = (
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--node-id",
            args.node_id,
            "--physical-device-id",
            args.physical_device_id,
            "--runtime-visible-device-id",
            args.runtime_visible_device_id,
            "--visible-device-index",
            str(args.visible_device_index),
            "--allocation-mb",
            str(args.allocation_mb),
            "--ready-file",
            str(ready_file),
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready = _wait_for_ready(ready_file, process, args.timeout_seconds)
            worker_pid = _ready_worker_pid(ready)
            matching_devices = {
                device.physical_device_id
                for device in monitor.devices()
                if any(item.pid == worker_pid for item in device.processes)
            }
            if matching_devices != {args.physical_device_id}:
                raise RuntimeError(
                    "Worker PID is not exclusive to the leased physical NPU: "
                    f"pid={worker_pid}, devices={sorted(matching_devices)}"
                )
            if ready["runtime_visible_device_id"] != args.runtime_visible_device_id:
                raise RuntimeError("Worker runtime-visible device mapping changed")
            assert process.stdin is not None
            process.stdin.write("release\n")
            process.stdin.flush()
            stdout, stderr = process.communicate(timeout=args.timeout_seconds)
            if process.returncode != 0:
                raise RuntimeError(
                    "binding Worker failed during cleanup: "
                    f"code={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
                )
            final_hbm_mb = _wait_for_hbm_recovery(
                monitor=monitor,
                physical_device_id=args.physical_device_id,
                worker_pid=worker_pid,
                baseline_hbm_mb=baseline_hbm_mb,
                tolerance_mb=args.hbm_tolerance_mb,
                timeout_seconds=args.timeout_seconds,
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    print(
        json.dumps(
            {
                "baseline_hbm_mb": baseline_hbm_mb,
                "dcmi_device_ids": physical_ids,
                "final_hbm_mb": final_hbm_mb,
                "mapping": ready,
                "matching_physical_devices": sorted(matching_devices),
                "status": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


def _docker_worker_pid(container_name: str, ready_file: str) -> int:
    output = subprocess.check_output(
        ("docker", "top", container_name, "-eo", "pid,args"),
        text=True,
    )
    matches: list[int] = []
    for line in output.splitlines()[1:]:
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2:
            continue
        pid, command = fields
        if "verify_device_binding.py --worker" in command and ready_file in command:
            matches.append(int(pid))
    if len(matches) != 1:
        raise RuntimeError(
            "could not identify one host PID for the container Worker: "
            f"matches={matches}"
        )
    return matches[0]


def _docker_orchestrator(args: argparse.Namespace) -> int:
    if not args.host_state_directory:
        raise ValueError("--host-state-directory is required with --container-name")
    monitor = DcmiDeviceAdapter()
    devices = monitor.devices()
    physical_ids = tuple(item.physical_device_id for item in devices)
    if args.physical_device_id not in physical_ids:
        raise RuntimeError(
            f"physical NPU {args.physical_device_id} is absent from DCMI {physical_ids}"
        )
    baseline_hbm_mb = monitor.device(args.physical_device_id).used_hbm_mb
    host_tmp = Path(args.host_state_directory) / "tmp"
    host_tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="ascend-maze-host-binding-", dir=host_tmp
    ) as directory:
        host_directory = Path(directory)
        container_directory = (
            Path(args.container_state_directory) / "tmp" / host_directory.name
        )
        host_ready_file = host_directory / "ready.json"
        host_release_file = host_directory / "release"
        container_ready_file = container_directory / "ready.json"
        container_release_file = container_directory / "release"
        worker_command = (
            sys.executable,
            args.script_container_path or str(Path(__file__).resolve()),
            "--worker",
            "--node-id",
            args.node_id,
            "--physical-device-id",
            args.physical_device_id,
            "--runtime-visible-device-id",
            args.runtime_visible_device_id,
            "--visible-device-index",
            str(args.visible_device_index),
            "--allocation-mb",
            str(args.allocation_mb),
            "--ready-file",
            str(container_ready_file),
            "--release-file",
            str(container_release_file),
        )
        shell_command = (
            f"source {shlex.quote(args.environment_script)}; "
            f"exec {shlex.join(worker_command)}"
        )
        process = subprocess.Popen(
            ("docker", "exec", args.container_name, "bash", "-lc", shell_command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready = _wait_for_ready(
                host_ready_file, process, args.timeout_seconds
            )
            host_worker_pid = _docker_worker_pid(
                args.container_name, str(container_ready_file)
            )
            matching_devices = {
                device.physical_device_id
                for device in monitor.devices()
                if any(item.pid == host_worker_pid for item in device.processes)
            }
            if matching_devices != {args.physical_device_id}:
                raise RuntimeError(
                    "host DCMI did not place the Worker exclusively on its lease: "
                    f"pid={host_worker_pid}, devices={sorted(matching_devices)}"
                )
            host_release_file.write_text("release\n", encoding="ascii")
            stdout, stderr = process.communicate(timeout=args.timeout_seconds)
            if process.returncode != 0:
                raise RuntimeError(
                    "container binding Worker failed during cleanup: "
                    f"code={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
                )
            final_hbm_mb = _wait_for_hbm_recovery(
                monitor=monitor,
                physical_device_id=args.physical_device_id,
                worker_pid=host_worker_pid,
                baseline_hbm_mb=baseline_hbm_mb,
                tolerance_mb=args.hbm_tolerance_mb,
                timeout_seconds=args.timeout_seconds,
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    print(
        json.dumps(
            {
                "baseline_hbm_mb": baseline_hbm_mb,
                "container_worker_pid": ready["worker_pid"],
                "dcmi_device_ids": physical_ids,
                "final_hbm_mb": final_hbm_mb,
                "host_worker_pid": host_worker_pid,
                "mapping": ready,
                "matching_physical_devices": sorted(matching_devices),
                "status": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--physical-device-id", required=True)
    parser.add_argument("--runtime-visible-device-id", required=True)
    parser.add_argument("--visible-device-index", type=int, default=0)
    parser.add_argument("--allocation-mb", type=int, default=256)
    parser.add_argument("--hbm-tolerance-mb", type=int, default=128)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--ready-file")
    parser.add_argument("--release-file")
    parser.add_argument("--container-name")
    parser.add_argument("--host-state-directory")
    parser.add_argument("--container-state-directory", default="/workspace/state")
    parser.add_argument("--environment-script", default="/dev/null")
    parser.add_argument("--script-container-path")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.worker:
        if not args.ready_file:
            raise ValueError("--ready-file is required in Worker mode")
        return _worker(args)
    if args.container_name:
        return _docker_orchestrator(args)
    return _orchestrator(args)


if __name__ == "__main__":
    raise SystemExit(main())
