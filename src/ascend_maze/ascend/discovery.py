"""Build homogeneous environment fingerprints and C6 node capacity."""

from __future__ import annotations

import hashlib
from importlib import metadata
import os
from pathlib import Path
import platform
import re
import sys

from ascend_maze.ascend.contracts import (
    AscendColocationConfig,
    AscendCorrectnessConfig,
    AscendDeviceSnapshot,
    AscendEnvironmentSnapshot,
)
from ascend_maze.ascend.dcmi import DcmiDeviceAdapter
from ascend_maze.core.canonical import FrozenMap
from ascend_maze.placement import (
    NodeCapacity,
    NodeObservation,
    NpuCapacity,
    NpuObservation,
)


def _distribution_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "absent"


def _version_file(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "absent"
    values: list[str] = []
    for line in content.splitlines():
        match = re.match(
            r"(?:Version|version|package_version)\s*=\s*[\"']?([^\"']+)", line
        )
        if match:
            values.append(match.group(1).strip())
    return values[0] if values else "unknown"


def _cann_version() -> str:
    candidates: list[Path] = []
    ascend_home = os.environ.get("ASCEND_HOME_PATH")
    if ascend_home:
        candidates.append(Path(ascend_home) / "version.info")
        candidates.append(Path(ascend_home) / "opp" / "version.info")
    candidates.extend(
        sorted(Path("/usr/local/Ascend").glob("cann-*/opp/version.info"), reverse=True)
    )
    for candidate in candidates:
        version = _version_file(candidate)
        if version != "absent":
            return version
    return "absent"


def _atb_version() -> str:
    candidates: list[Path] = []
    atb_home = os.environ.get("ATB_HOME_PATH")
    if atb_home:
        home = Path(atb_home).expanduser().resolve(strict=False)
        candidates.extend((home / "version.info", home.parents[1] / "version.info"))
    candidates.append(Path("/usr/local/Ascend/nnal/atb/latest/version.info"))
    for candidate in candidates:
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r"^\s*Ascend-cann-atb\s*:\s*(\S+)\s*$", content, re.MULTILINE)
        if match:
            return match.group(1)
    return "absent"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atb_library_directory() -> Path | None:
    candidates: list[Path] = []
    atb_home = os.environ.get("ATB_HOME_PATH")
    if atb_home:
        candidates.append(Path(atb_home).expanduser() / "lib")
    candidates.extend(
        Path(item)
        for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if item
    )
    candidates.append(Path("/usr/local/Ascend/nnal/atb/latest/atb/cxx_abi_1/lib"))
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        identity = str(resolved)
        if identity in seen:
            continue
        seen.add(identity)
        if (resolved / "libmki.so").is_file() and (
            resolved / "libtbe_adapter.so"
        ).is_file():
            return resolved
    return None


def discover_atb_runtime_library_preloads() -> FrozenMap[str, str]:
    """Return the exact ATB preload identity required by torch_npu ATB ops."""

    directory = _atb_library_directory()
    if directory is None:
        return FrozenMap()
    library = (directory / "libmki.so").resolve(strict=True)
    return FrozenMap(((str(library), _file_sha256(library)),))


def discover_aicpu_runtime_library_paths() -> tuple[str, ...]:
    """Return CANN AICPU kernel directories needed by torch_npu CPU kernels."""

    candidates: list[Path] = []
    ascend_home = os.environ.get("ASCEND_HOME_PATH")
    if ascend_home:
        home = Path(ascend_home).expanduser().resolve(strict=False)
        candidates.extend(
            (
                home / "opp" / "built-in" / "op_impl" / "host_aicpu",
                home
                / "opp"
                / "built-in"
                / "op_impl"
                / "aicpu"
                / "aicpu_kernel"
                / "lib"
                / "Ascend",
            )
        )
    for cann_home in sorted(Path("/usr/local/Ascend").glob("cann-*"), reverse=True):
        candidates.extend(
            (
                cann_home / "opp" / "built-in" / "op_impl" / "host_aicpu",
                cann_home
                / "opp"
                / "built-in"
                / "op_impl"
                / "aicpu"
                / "aicpu_kernel"
                / "lib"
                / "Ascend",
            )
        )

    paths: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        identity = str(resolved)
        if identity in seen or not resolved.is_dir():
            continue
        if not any(resolved.glob("libcpu_kernels*.so")):
            continue
        seen.add(identity)
        paths.append(identity)
    return tuple(paths)


def discover_ascend_environment(
    adapter: DcmiDeviceAdapter,
    devices: tuple[AscendDeviceSnapshot, ...] | None = None,
) -> AscendEnvironmentSnapshot:
    inventory = adapter.devices() if devices is None else devices
    atb_directory = _atb_library_directory()
    aicpu_paths = discover_aicpu_runtime_library_paths()
    versions = {
        "python": platform.python_version(),
        "torch": _distribution_version("torch"),
        "torch_npu": _distribution_version("torch-npu"),
        "vllm": _distribution_version("vllm"),
        "vllm_ascend": _distribution_version("vllm-ascend"),
        "ray": _distribution_version("ray"),
        "cloudpickle": _distribution_version("cloudpickle"),
        "driver": _version_file(Path("/usr/local/Ascend/driver/version.info")),
        "firmware": _version_file(Path("/usr/local/Ascend/firmware/version.info")),
        "cann": _cann_version(),
        "atb": _atb_version(),
        "atb_library_path": "absent" if atb_directory is None else str(atb_directory),
        "atb_libmki_sha256": (
            "absent"
            if atb_directory is None
            else _file_sha256(atb_directory / "libmki.so")
        ),
        "atb_libtbe_adapter_sha256": (
            "absent"
            if atb_directory is None
            else _file_sha256(atb_directory / "libtbe_adapter.so")
        ),
        "aicpu_runtime_library_paths": os.pathsep.join(aicpu_paths)
        if aicpu_paths
        else "absent",
        "executable_abi": f"{sys.version_info.major}.{sys.version_info.minor}",
    }
    return AscendEnvironmentSnapshot.create(
        machine=platform.machine(),
        chip_types=tuple(item.chip_type for item in inventory),
        versions=versions,
    )


def _physical_host_memory_bytes() -> int:
    return int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))


def _cgroup_memory_bytes(*names: str) -> int | None:
    for name in names:
        path = Path(name)
        try:
            raw = path.read_text(encoding="ascii").strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return None


def _host_memory_mb() -> int:
    physical = _physical_host_memory_bytes()
    cgroup_limit = _cgroup_memory_bytes(
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        "/sys/fs/cgroup/memory.max",
    )
    effective = physical
    if cgroup_limit is not None and cgroup_limit < physical:
        effective = cgroup_limit
    return effective // (1024 * 1024)


def _host_available_memory_mb() -> int:
    available_bytes: int | None = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                available_bytes = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    if available_bytes is None:
        raise RuntimeError("cannot read host available memory")
    cgroup_limit = _cgroup_memory_bytes(
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        "/sys/fs/cgroup/memory.max",
    )
    cgroup_used = _cgroup_memory_bytes(
        "/sys/fs/cgroup/memory/memory.usage_in_bytes",
        "/sys/fs/cgroup/memory.current",
    )
    if cgroup_limit is not None and cgroup_used is not None:
        available_bytes = min(
            available_bytes,
            max(0, cgroup_limit - cgroup_used),
        )
    return available_bytes // (1024 * 1024)


def _available_cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:  # pragma: no cover - non-Linux fallback
        return os.cpu_count() or 1


def build_ascend_node_observation(
    *,
    node_id: str,
    boot_id: str,
    sequence: int,
    received_at_ms: int,
    adapter: DcmiDeviceAdapter,
) -> NodeObservation:
    devices = adapter.devices()
    return NodeObservation(
        node_id=node_id,
        boot_id=boot_id,
        sequence=sequence,
        received_at_ms=received_at_ms,
        observed_free_mem_mb=_host_available_memory_mb(),
        npus=tuple(
            NpuObservation(
                device_id=item.physical_device_id,
                health=item.health,
                observed_free_hbm_mb=item.free_hbm_mb,
                utilization=item.utilization,
            )
            for item in devices
        ),
    )


def build_ascend_node_capacity(
    *,
    node_id: str,
    boot_id: str,
    node_ip: str,
    adapter: DcmiDeviceAdapter,
    environment: AscendEnvironmentSnapshot,
    config: AscendCorrectnessConfig | AscendColocationConfig,
    cpu_system_reserved: int = 1,
    mem_system_reserved_mb: int = 2_048,
) -> NodeCapacity:
    devices = adapter.devices()
    if tuple(sorted(set(item.chip_type for item in devices))) != environment.chip_types:
        raise ValueError("Ascend inventory changed after environment fingerprinting")
    npus = tuple(
        NpuCapacity(
            device_id=item.physical_device_id,
            chip_type=item.chip_type,
            total_hbm_mb=item.total_hbm_mb,
            system_reserved_hbm_mb=config.npu_system_reserved_hbm_mb,
            task_slots_total=config.task_slots_total,
            observed_free_hbm_mb=item.free_hbm_mb,
            healthy=item.health == "healthy",
        )
        for item in devices
    )
    return NodeCapacity(
        node_id=node_id,
        boot_id=boot_id,
        node_ip=node_ip,
        cpu_total=_available_cpu_count(),
        mem_total_mb=_host_memory_mb(),
        cpu_system_reserved=cpu_system_reserved,
        mem_system_reserved_mb=mem_system_reserved_mb,
        io_slots_total=config.io_slots_total,
        npus=npus,
        observed_free_mem_mb=None,
        capabilities=FrozenMap(
            (
                ("platform", "ascend"),
                ("chip_family", ",".join(environment.chip_types)),
                (
                    "environment_fingerprint",
                    environment.environment_fingerprint,
                ),
                ("driver_version", environment.versions["driver"]),
                ("cann_version", environment.versions["cann"]),
                ("torch_npu_version", environment.versions["torch_npu"]),
            )
        ),
    )
