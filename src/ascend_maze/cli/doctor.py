"""Read-only C13 environment diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata
import os
from pathlib import Path
import shutil
import stat
import sys

from ascend_maze import __version__
from ascend_maze.config import LoadedConfig


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: str
    message: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    schema_version: int
    project: str
    config_fingerprint: str
    checks: tuple[DoctorCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.status != "fail" for check in self.checks)


def run_doctor(loaded: LoadedConfig) -> DoctorReport:
    config = loaded.config
    checks: list[DoctorCheck] = [
        DoctorCheck(
            "project",
            "pass",
            f"Ascend-Maze {__version__} on Python {sys.version_info.major}.{sys.version_info.minor}",
        ),
        _maze_executable_check(),
        _runtime_directory_check(Path(config.control.runtime_directory)),
        _token_file_check(Path(config.control.cluster_token_file)),
        _recording_directory_check(Path(config.recording.root_directory)),
    ]
    for package in ("cloudpickle", "pyarrow", "ray", "torch", "torch-npu"):
        checks.append(_package_check(package, required=package in {"cloudpickle", "pyarrow"}))
    if config.inference.model_catalog_path is not None:
        path = Path(config.inference.model_catalog_path)
        checks.append(
            DoctorCheck(
                "model_catalog",
                "pass" if path.is_file() else "fail",
                str(path),
            )
        )
    checks.append(_npu_smi_check())
    return DoctorReport(
        schema_version=1,
        project="Ascend-Maze",
        config_fingerprint=loaded.snapshot.config_fingerprint,
        checks=tuple(checks),
    )


def _maze_executable_check() -> DoctorCheck:
    executable = shutil.which("maze")
    entries = tuple(
        importlib.metadata.entry_points(group="console_scripts", name="maze")
    )
    expected_entry = "ascend_maze.cli.main:main"
    conflicting = tuple(
        sorted(entry.value for entry in entries if entry.value != expected_entry)
    )
    has_expected = any(entry.value == expected_entry for entry in entries)
    if executable is None:
        return DoctorCheck(
            "maze_executable",
            "fail" if conflicting else "warn",
            (
                f"conflicting maze console entries: {', '.join(conflicting)}"
                if conflicting
                else "maze is not installed on PATH; use python -m ascend_maze.cli.main"
            ),
        )
    resolved = Path(executable).resolve(strict=False)
    environment = Path(sys.executable).resolve(strict=False).parent
    valid = resolved.parent == environment and has_expected and not conflicting
    details = [f"{resolved} (expected environment {environment})"]
    if not has_expected:
        details.append(f"missing console entry {expected_entry}")
    if conflicting:
        details.append(f"conflicting entries: {', '.join(conflicting)}")
    return DoctorCheck(
        "maze_executable",
        "pass" if valid else "fail",
        "; ".join(details),
    )


def _runtime_directory_check(path: Path) -> DoctorCheck:
    parent = path if path.exists() else path.parent
    writable = parent.exists() and os.access(parent, os.W_OK | os.X_OK)
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        private = mode & 0o077 == 0
    else:
        private = True
    return DoctorCheck(
        "control_runtime_directory",
        "pass" if writable and private else "fail",
        f"{path} writable={writable} private={private}",
    )


def _token_file_check(path: Path) -> DoctorCheck:
    if not path.exists():
        return DoctorCheck("cluster_token_file", "fail", f"missing: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    valid = path.is_file() and mode & 0o077 == 0 and path.stat().st_size > 0
    return DoctorCheck(
        "cluster_token_file",
        "pass" if valid else "fail",
        f"{path} mode={mode:04o}",
    )


def _recording_directory_check(path: Path) -> DoctorCheck:
    parent = path if path.exists() else path.parent
    usage = shutil.disk_usage(parent) if parent.exists() else None
    writable = parent.exists() and os.access(parent, os.W_OK | os.X_OK)
    message = f"{path} writable={writable}"
    if usage is not None:
        message += f" free_bytes={usage.free}"
    return DoctorCheck(
        "recording_directory",
        "pass" if writable else "fail",
        message,
    )


def _package_check(package: str, *, required: bool) -> DoctorCheck:
    try:
        version = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return DoctorCheck(
            f"package:{package}",
            "fail" if required else "unknown",
            "not installed",
        )
    return DoctorCheck(f"package:{package}", "pass", version)


def _npu_smi_check() -> DoctorCheck:
    executable = shutil.which("npu-smi")
    if executable is None:
        return DoctorCheck("npu_smi", "unknown", "npu-smi is not on PATH")
    return DoctorCheck(
        "npu_smi",
        "pass",
        f"available at {Path(executable).resolve(strict=False)}; no NPU context created",
    )
