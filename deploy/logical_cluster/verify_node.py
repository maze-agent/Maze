#!/usr/bin/env python3
"""Verify logical-node isolation and execute a small operation on its NPU."""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
import socket


def _memory_limit_bytes() -> int | None:
    candidates = (
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
        Path("/sys/fs/cgroup/memory.max"),
    )
    for path in candidates:
        try:
            value = path.read_text(encoding="ascii").strip()
        except OSError:
            continue
        if value != "max":
            return int(value)
    return None


def main() -> None:
    import ascend_maze
    import ray
    import torch
    import torch_npu  # noqa: F401

    model_paths = (
        Path("/home/user2/workplace/model_weight/model_from_hf/Qwen3-4B"),
        Path(
            "/home/user2/workplace/model_weight/model_from_hf/"
            "Qwen2.5-VL-3B-Instruct"
        ),
    )
    missing_models = [str(path) for path in model_paths if not path.is_dir()]
    if missing_models:
        raise RuntimeError(f"model mounts are missing: {missing_models}")

    visible_devices = sorted(glob.glob("/dev/davinci[0-9]*"))
    device_count = torch.npu.device_count()
    if visible_devices != ["/dev/davinci0"]:
        raise RuntimeError(f"unexpected visible device files: {visible_devices}")
    if device_count != 1:
        raise RuntimeError(f"expected one logical NPU, found {device_count}")

    torch.npu.set_device(0)
    value = torch.arange(8, dtype=torch.int32, device="npu")
    observed = value.cpu().tolist()
    if observed != list(range(8)):
        raise RuntimeError(f"NPU tensor verification failed: {observed}")

    payload = {
        "hostname": socket.gethostname(),
        "logical_device_count": device_count,
        "logical_device_id": torch.npu.current_device(),
        "physical_device_id": int(os.environ["ASCEND_PHYSICAL_DEVICE_ID"]),
        "visible_device_files": visible_devices,
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "cpu_count": len(os.sched_getaffinity(0)),
        "memory_limit_bytes": _memory_limit_bytes(),
        "model_paths": [str(path) for path in model_paths],
        "ray_version": ray.__version__,
        "ascend_maze_path": str(Path(ascend_maze.__file__).resolve()),
        "tensor_result": observed,
        "torch_version": torch.__version__,
        "torch_npu_version": torch_npu.__version__,
    }
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
