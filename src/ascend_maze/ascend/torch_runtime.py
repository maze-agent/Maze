"""Lazy torch_npu binding used only inside a leased one-shot NPU Worker."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
import re
import resource
import sys
from typing import Any

from ascend_maze.ascend.dcmi import DcmiDeviceAdapter
from ascend_maze.contracts.runtime import DeviceBinding


class AscendBindingError(RuntimeError):
    pass


@dataclass(slots=True)
class BoundTorchNpuRuntime:
    binding: DeviceBinding
    torch: Any
    torch_npu: Any
    dcmi: DcmiDeviceAdapter
    worker_pid: int
    initial_process_hbm_mb: int

    def synchronize(self) -> None:
        self.torch.npu.synchronize(self.binding.visible_device_index)

    def peak_allocated_mb(self) -> int:
        return int(
            self.torch.npu.max_memory_allocated(self.binding.visible_device_index)
            // (1024 * 1024)
        )

    def peak_reserved_mb(self) -> int:
        return int(
            self.torch.npu.max_memory_reserved(self.binding.visible_device_index)
            // (1024 * 1024)
        )

    def process_hbm_mb(self) -> int | None:
        return self.dcmi.process_hbm_mb(
            self.binding.physical_device_id,
            self.worker_pid,
        )

    def reset_peak_stats(self) -> None:
        self.torch.npu.reset_peak_memory_stats(self.binding.visible_device_index)

    def oom_classification_confidence(self, exc: BaseException) -> str | None:
        if isinstance(exc, self.torch.OutOfMemoryError):
            return "exact"
        if (
            getattr(self.torch_npu, "__version__", None) == "2.7.1.post2"
            and isinstance(exc, RuntimeError)
            and str(exc).startswith("NPU out of memory.")
        ):
            return "fallback"
        return None


def bind_torch_npu_device(
    binding: DeviceBinding,
    *,
    dcmi: DcmiDeviceAdapter | None = None,
) -> BoundTorchNpuRuntime:
    imported = sorted(
        name
        for name in sys.modules
        if name == "torch_npu" or name.startswith("torch_npu.") or name == "acl"
    )
    if imported:
        raise AscendBindingError(
            "Ascend runtime was imported before DeviceBinding: " + ", ".join(imported)
        )
    for name, value in binding.environment_variables.items_tuple():
        existing = os.environ.get(name)
        if existing is not None and existing != value:
            raise AscendBindingError(
                f"conflicting pre-existing device environment variable: {name}"
            )
        os.environ[name] = value
    try:
        torch = importlib.import_module("torch")
        torch_npu = importlib.import_module("torch_npu")
        if torch.npu.device_count() != 1:
            raise AscendBindingError(
                "DeviceBinding must expose exactly one logical NPU"
            )
        torch.npu.set_device(binding.visible_device_index)
        probe = torch.empty(
            (1024 * 1024,),
            dtype=torch.uint8,
            device=f"npu:{binding.visible_device_index}",
        )
        torch.npu.synchronize(binding.visible_device_index)
        del probe
    except AscendBindingError:
        raise
    except Exception as exc:
        raise AscendBindingError(f"failed to initialize leased NPU: {exc}") from exc
    if torch.npu.current_device() != binding.visible_device_index:
        raise AscendBindingError("torch_npu current device does not match DeviceBinding")
    monitor = dcmi or DcmiDeviceAdapter()
    worker_pid = os.getpid()
    if not monitor.verify_process_device(
        worker_pid,
        binding.physical_device_id,
    ):
        raise AscendBindingError(
            "Worker PID is not mapped exclusively to the leased physical NPU"
        )
    process_hbm = monitor.process_hbm_mb(binding.physical_device_id, worker_pid)
    if process_hbm is None:
        raise AscendBindingError("DCMI did not report the bound Worker process")
    runtime = BoundTorchNpuRuntime(
        binding=binding,
        torch=torch,
        torch_npu=torch_npu,
        dcmi=monitor,
        worker_pid=worker_pid,
        initial_process_hbm_mb=process_hbm,
    )
    runtime.reset_peak_stats()
    return runtime


def host_peak_rss_mb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)


def contains_npu_tensor(value: object, *, _seen: set[int] | None = None) -> bool:
    seen = set() if _seen is None else _seen
    identity = id(value)
    if identity in seen:
        return False
    device = getattr(value, "device", None)
    if getattr(device, "type", None) == "npu":
        return True
    if isinstance(value, dict):
        seen.add(identity)
        return any(
            contains_npu_tensor(key, _seen=seen)
            or contains_npu_tensor(item, _seen=seen)
            for key, item in value.items()
        )
    if isinstance(value, (tuple, list, set, frozenset)):
        seen.add(identity)
        return any(contains_npu_tensor(item, _seen=seen) for item in value)
    return False


def platform_error_code(exc: BaseException) -> str | None:
    message = str(exc)
    match = re.search(
        r"\berror code(?:\s+is)?\s*[:=]?\s*(\d{5,})\b",
        message,
        re.IGNORECASE,
    )
    if match is None:
        match = re.search(r"\bERR(\d{5,})\b", message, re.IGNORECASE)
    return None if match is None else match.group(1)
