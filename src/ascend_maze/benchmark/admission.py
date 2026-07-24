"""Read-only Ascend admission and host resource evidence for C14E."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Mapping, Sequence, cast

from ascend_maze.ascend import DcmiDeviceAdapter, discover_ascend_environment
from ascend_maze.benchmark.canonical import canonical_json_bytes, canonical_json_digest
from ascend_maze.benchmark.contracts import ExperimentSpec
from ascend_maze.benchmark.persistence import atomic_write_json
from ascend_maze.config import load_config, load_model_catalog
from ascend_maze.core.errors import ExperimentValidationError

ADMISSION_SCHEMA = "ascend-maze.c14e-admission.v1"
HOST_AUDIT_SCHEMA = "ascend-maze.host-resource-audit.v1"

_RELATED_EXECUTABLES = frozenset(
    {
        "dashboard",
        "dashboard_agent",
        "gcs_server",
        "log_monitor",
        "plasma_store",
        "raylet",
        "runtime_env_agent",
    }
)
_PID_RE = re.compile(r"pid=(\d+)")
_MODEL_REHASH_LIMIT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AdmissionEvidence:
    software: Mapping[str, object]
    hardware: Mapping[str, object]
    model_artifacts: Mapping[str, object]
    host_baseline: Mapping[str, object]
    evidence_digest: str

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "schema": ADMISSION_SCHEMA,
            "software": dict(self.software),
            "hardware": dict(self.hardware),
            "model_artifacts": dict(self.model_artifacts),
            "host_baseline": dict(self.host_baseline),
            "evidence_digest": self.evidence_digest,
        }


class AscendAdmissionGate:
    """Freeze one Study environment and reject drift before every Trial."""

    def __init__(self, *, required_device_count: int = 8) -> None:
        if required_device_count < 1:
            raise ValueError("required_device_count must be positive")
        self.required_device_count = required_device_count
        self._evidence_by_study: dict[str, AdmissionEvidence] = {}

    def admit(
        self,
        spec: ExperimentSpec,
        *,
        study_directory: str | Path | None = None,
    ) -> AdmissionEvidence:
        cached = self._evidence_by_study.get(spec.study_id)
        evidence = collect_ascend_admission(
            spec,
            required_device_count=self.required_device_count,
            include_model_file_hashes=cached is None,
            frozen_model_artifacts=(
                None if cached is None else cached.model_artifacts
            ),
        )
        if cached is None:
            self._evidence_by_study[spec.study_id] = evidence
            cached = evidence
        else:
            _validate_against_frozen_evidence(cached, evidence)
        if study_directory is not None:
            _write_environment_manifests(Path(study_directory), cached)
        return cached


def collect_ascend_admission(
    spec: ExperimentSpec,
    *,
    required_device_count: int = 8,
    include_model_file_hashes: bool = True,
    frozen_model_artifacts: Mapping[str, object] | None = None,
) -> AdmissionEvidence:
    repository = _repository_root(Path(spec.base_config_path).parent)
    source = _git_source_identity(repository)
    if source["commit"] != spec.build_revision:
        raise ExperimentValidationError(
            "current source commit does not match ExperimentSpec.build_revision"
        )
    if source["tracked_worktree_clean"] is not True:
        raise ExperimentValidationError(
            "tracked source changes are forbidden during C14E admission"
        )

    loaded = load_config(
        spec.base_config_path,
        build_revision=spec.build_revision,
        created_at_ms=0,
    )
    if (
        loaded.snapshot.config_fingerprint
        != spec.base_config_snapshot.config_fingerprint
    ):
        raise ExperimentValidationError(
            "current ConfigSnapshot does not match the frozen ExperimentSpec"
        )

    adapter = DcmiDeviceAdapter()
    devices = adapter.devices()
    environment = discover_ascend_environment(adapter, devices)
    if environment.environment_fingerprint != (
        spec.workload.required_environment_fingerprint
    ):
        raise ExperimentValidationError(
            "current Ascend environment fingerprint does not match the Study"
        )
    _validate_devices(devices, required_device_count=required_device_count)
    topology = _npu_topology(required_device_count)
    host = capture_host_resources(adapter=adapter)
    if cast(Sequence[object], host["relevant_processes"]):
        raise ExperimentValidationError(
            "C14E admission found an existing Ray/Maze/vLLM process"
        )
    if cast(Sequence[object], host["relevant_listeners"]):
        raise ExperimentValidationError(
            "C14E admission found an existing Ray/Maze/vLLM listener"
        )

    catalog_path = loaded.config.inference.model_catalog_path
    if catalog_path is None:
        raise ExperimentValidationError("C14E requires a ModelCatalog")
    catalog = load_model_catalog(
        catalog_path,
        environment_fingerprint=environment.environment_fingerprint,
    )
    if catalog.catalog_revision != spec.workload.model_catalog_revision:
        raise ExperimentValidationError(
            "ModelCatalog revision does not match the ExperimentSpec"
        )
    if len(catalog.specs) != 1:
        raise ExperimentValidationError(
            "C14E Qwen workload requires exactly one model artifact"
        )
    model = catalog.specs[0]
    if model.model_id != "qwen3-4b" or model.backend != "vllm_ascend":
        raise ExperimentValidationError(
            "C14E requires the qwen3-4b vllm_ascend model"
        )
    if include_model_file_hashes:
        model_artifacts = model_artifact_manifest(Path(model.artifact_path))
    elif frozen_model_artifacts is not None:
        _verify_frozen_model_manifest(Path(model.artifact_path), frozen_model_artifacts)
        model_artifacts = dict(frozen_model_artifacts)
    else:
        raise ValueError("frozen_model_artifacts is required when hashes are skipped")
    if model_artifacts["content_digest"] != spec.workload.model_artifact_digest:
        raise ExperimentValidationError(
            "Qwen3-4B content digest does not match the ExperimentSpec"
        )
    if model_artifacts["artifact_revision"] != model.artifact_revision:
        raise ExperimentValidationError(
            "Qwen3-4B revision does not match the ModelCatalog"
        )

    software = {
        "schema_version": 1,
        "source_commit": source["commit"],
        "tracked_worktree_clean": source["tracked_worktree_clean"],
        "config_fingerprint": loaded.snapshot.config_fingerprint,
        "environment_fingerprint": environment.environment_fingerprint,
        "python_executable": str(Path(sys.executable).resolve(strict=False)),
        "machine": environment.machine,
        "versions": dict(environment.versions.items_tuple()),
    }
    hardware = {
        "schema_version": 1,
        "device_count": len(devices),
        "chip_types": environment.chip_types,
        "devices": [_device_payload(item) for item in devices],
        "topology": topology,
    }
    identity = {
        "software": software,
        "hardware": hardware,
        "model_artifacts": model_artifacts,
        "host_baseline": host,
    }
    return AdmissionEvidence(
        software=software,
        hardware=hardware,
        model_artifacts=model_artifacts,
        host_baseline=host,
        evidence_digest=canonical_json_digest(identity),
    )


def model_artifact_manifest(path: Path) -> dict[str, object]:
    root = path.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ExperimentValidationError("model artifact path is not a directory")
    files: list[dict[str, object]] = []
    file_stats: list[dict[str, object]] = []
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if candidate.is_symlink():
            raise ExperimentValidationError(
                f"model artifact contains a symbolic link: {candidate.name}"
            )
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root).as_posix()
        info = candidate.stat()
        digest = _file_sha256(candidate)
        verified = candidate.stat()
        if (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ) != (
            verified.st_dev,
            verified.st_ino,
            verified.st_size,
            verified.st_mtime_ns,
            verified.st_ctime_ns,
        ):
            raise ExperimentValidationError(
                f"model artifact changed while hashing: {relative}"
            )
        files.append(
            {
                "logical_name": relative,
                "size_bytes": info.st_size,
                "sha256": digest,
            }
        )
        file_stats.append(
            {
                "logical_name": relative,
                "size_bytes": info.st_size,
                "device": info.st_dev,
                "inode": info.st_ino,
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
            }
        )
    if not files:
        raise ExperimentValidationError("model artifact directory is empty")
    index = next(
        (
            item
            for item in files
            if item["logical_name"] == "model.safetensors.index.json"
        ),
        None,
    )
    if index is None:
        raise ExperimentValidationError("model artifact index is missing")
    body = {
        "schema_version": 1,
        "logical_model": "qwen3-4b",
        "artifact_revision": index["sha256"],
        "file_count": len(files),
        "total_size_bytes": sum(cast(int, item["size_bytes"]) for item in files),
        "files": files,
        "file_stats": file_stats,
    }
    return {**body, "content_digest": canonical_json_digest(files)}


def capture_host_resources(
    *, adapter: DcmiDeviceAdapter | None = None
) -> dict[str, object]:
    device_adapter = adapter or DcmiDeviceAdapter()
    processes = _related_processes()
    listeners = _related_listeners({cast(int, item["pid"]) for item in processes})
    devices = device_adapter.devices()
    body = {
        "schema_version": 1,
        "schema": HOST_AUDIT_SCHEMA,
        "devices": [_device_payload(item) for item in devices],
        "relevant_processes": processes,
        "relevant_listeners": listeners,
    }
    return {**body, "snapshot_digest": canonical_json_digest(body)}


def host_recovery_issues(
    before: Mapping[str, object],
    after: Mapping[str, object],
    *,
    hbm_tolerance_mb: int,
) -> tuple[str, ...]:
    issues: list[str] = []
    if after.get("schema") != HOST_AUDIT_SCHEMA:
        return ("host_audit_invalid",)
    if cast(Sequence[object], after.get("relevant_processes", ())):
        issues.append("related_process_residual")
    if cast(Sequence[object], after.get("relevant_listeners", ())):
        issues.append("related_listener_residual")
    baseline = {
        cast(str, item["physical_device_id"]): item
        for item in cast(Sequence[Mapping[str, object]], before.get("devices", ()))
    }
    observed = {
        cast(str, item["physical_device_id"]): item
        for item in cast(Sequence[Mapping[str, object]], after.get("devices", ()))
    }
    if set(baseline) != set(observed):
        issues.append("npu_inventory_changed")
    for device_id in sorted(set(baseline).intersection(observed)):
        prior = baseline[device_id]
        current = observed[device_id]
        if current.get("health") != "healthy":
            issues.append(f"npu_unhealthy:{device_id}")
        if cast(Sequence[object], current.get("processes", ())):
            issues.append(f"npu_process_residual:{device_id}")
        used = current.get("used_hbm_mb")
        baseline_used = prior.get("used_hbm_mb")
        if (
            isinstance(used, int)
            and isinstance(baseline_used, int)
            and used > baseline_used + hbm_tolerance_mb
        ):
            issues.append(f"npu_hbm_not_recovered:{device_id}")
    return tuple(issues)


def _write_environment_manifests(root: Path, evidence: AdmissionEvidence) -> None:
    directory = root / "environment"
    expected = {
        "software.json": dict(evidence.software),
        "hardware.json": dict(evidence.hardware),
        "model_artifacts.json": dict(evidence.model_artifacts),
        "admission.json": evidence.canonical_payload(),
    }
    for name, payload in expected.items():
        path = directory / name
        if path.exists():
            from ascend_maze.benchmark.persistence import load_json_object

            existing = load_json_object(path, description=name)
            if canonical_json_bytes(existing) != canonical_json_bytes(payload):
                raise ExperimentValidationError(
                    f"frozen C14E environment manifest changed: {name}"
                )
        else:
            atomic_write_json(path, payload)


def _validate_against_frozen_evidence(
    frozen: AdmissionEvidence, observed: AdmissionEvidence
) -> None:
    if dict(observed.software) != dict(frozen.software):
        raise ExperimentValidationError(
            "C14E software admission changed between Trials"
        )
    if dict(observed.model_artifacts) != dict(frozen.model_artifacts):
        raise ExperimentValidationError(
            "C14E model admission changed between Trials"
        )
    frozen_hardware = dict(frozen.hardware)
    observed_hardware = dict(observed.hardware)
    for key in ("device_count", "chip_types", "topology"):
        if observed_hardware.get(key) != frozen_hardware.get(key):
            raise ExperimentValidationError(
                f"C14E hardware admission changed between Trials: {key}"
            )
    issues = host_recovery_issues(
        frozen.host_baseline,
        observed.host_baseline,
        hbm_tolerance_mb=64,
    )
    if issues:
        raise ExperimentValidationError(
            "C14E host baseline did not recover before the next Trial: "
            + ", ".join(issues)
        )


def _verify_frozen_model_manifest(
    path: Path, manifest: Mapping[str, object]
) -> None:
    root = path.expanduser().resolve(strict=True)
    raw_files = manifest.get("files")
    raw_stats = manifest.get("file_stats")
    if not isinstance(raw_files, Sequence) or not isinstance(raw_stats, Sequence):
        raise ExperimentValidationError("frozen model artifact manifest is invalid")
    stats_by_name: dict[str, Mapping[str, object]] = {}
    for raw in raw_stats:
        if not isinstance(raw, Mapping):
            raise ExperimentValidationError("frozen model file stat is invalid")
        logical_name = raw.get("logical_name")
        if not isinstance(logical_name, str) or logical_name in stats_by_name:
            raise ExperimentValidationError("frozen model file stat identity is invalid")
        stats_by_name[logical_name] = raw
    expected_names: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, Mapping):
            raise ExperimentValidationError("frozen model file record is invalid")
        relative = raw.get("logical_name")
        size = raw.get("size_bytes")
        digest = raw.get("sha256")
        if (
            not isinstance(relative, str)
            or not isinstance(size, int)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ExperimentValidationError("frozen model file identity is invalid")
        candidate = root / relative
        if not candidate.is_file() or candidate.stat().st_size != size:
            raise ExperimentValidationError(
                f"frozen model artifact changed: {relative}"
            )
        expected_stat = stats_by_name.get(relative)
        if expected_stat is None:
            raise ExperimentValidationError("frozen model file stat is missing")
        info = candidate.stat()
        observed_stat = {
            "logical_name": relative,
            "size_bytes": info.st_size,
            "device": info.st_dev,
            "inode": info.st_ino,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
        }
        if dict(expected_stat) != observed_stat:
            raise ExperimentValidationError(
                f"frozen model artifact metadata changed: {relative}"
            )
        if size <= _MODEL_REHASH_LIMIT_BYTES and _file_sha256(candidate) != digest:
            raise ExperimentValidationError(
                f"frozen model artifact content changed: {relative}"
            )
        expected_names.add(relative)
    if set(stats_by_name) != expected_names:
        raise ExperimentValidationError("frozen model file stat set changed")
    current_names = {
        candidate.relative_to(root).as_posix()
        for candidate in root.rglob("*")
        if candidate.is_file()
    }
    if current_names != expected_names:
        raise ExperimentValidationError("frozen model artifact file set changed")


def _repository_root(start: Path) -> Path:
    result = _run_checked(("git", "-C", str(start), "rev-parse", "--show-toplevel"))
    return Path(result.strip()).resolve(strict=True)


def _git_source_identity(repository: Path) -> dict[str, object]:
    commit = _run_checked(("git", "-C", str(repository), "rev-parse", "HEAD")).strip()
    status = _run_checked(
        (
            "git",
            "-C",
            str(repository),
            "status",
            "--porcelain",
            "--untracked-files=no",
        )
    )
    return {"commit": commit, "tracked_worktree_clean": not status.strip()}


def _npu_topology(required_device_count: int) -> dict[str, object]:
    executable = shutil.which("npu-smi")
    if executable is None:
        raise ExperimentValidationError("npu-smi is required for topology admission")
    output = _run_checked((executable, "info", "-t", "topo"))
    rows: list[dict[str, object]] = []
    device_labels = tuple(f"NPU{index}" for index in range(required_device_count))
    for line in output.splitlines():
        fields = line.split()
        if tuple(fields[:required_device_count]) == device_labels:
            continue
        if not fields or not re.fullmatch(r"NPU\d+", fields[0]):
            continue
        if len(fields) < required_device_count + 2:
            raise ExperimentValidationError("npu-smi topology row is incomplete")
        links = fields[1 : required_device_count + 1]
        index = int(fields[0][3:])
        if index >= required_device_count:
            continue
        for peer, link in enumerate(links):
            expected = "X" if peer == index else "HCCS"
            if link != expected:
                raise ExperimentValidationError(
                    f"C14E requires all-HCCS topology: NPU{index}/NPU{peer}={link}"
                )
        rows.append(
            {
                "device_id": str(index),
                "links": links,
                "cpu_affinity": fields[required_device_count + 1],
            }
        )
    if len(rows) != required_device_count:
        raise ExperimentValidationError("npu-smi topology device count mismatch")
    return {
        "kind": "all_hccs",
        "rows": sorted(rows, key=lambda item: cast(str, item["device_id"])),
        "raw_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def _validate_devices(devices: Sequence[object], *, required_device_count: int) -> None:
    if len(devices) != required_device_count:
        raise ExperimentValidationError(
            f"C14E requires {required_device_count} NPUs, found {len(devices)}"
        )
    for item in devices:
        chip_type = getattr(item, "chip_type", None)
        health = getattr(item, "health", None)
        total_hbm_mb = getattr(item, "total_hbm_mb", None)
        processes = getattr(item, "processes", ())
        if (
            chip_type != "910B3"
            or health != "healthy"
            or not isinstance(total_hbm_mb, int)
            or total_hbm_mb < 65_000
        ):
            raise ExperimentValidationError("C14E requires healthy 64-GiB 910B3 devices")
        if processes:
            raise ExperimentValidationError("C14E admission requires idle NPU devices")


def _device_payload(item: object) -> dict[str, object]:
    processes = getattr(item, "processes")
    return {
        "physical_device_id": getattr(item, "physical_device_id"),
        "card_id": getattr(item, "card_id"),
        "card_device_id": getattr(item, "card_device_id"),
        "chip_type": getattr(item, "chip_type"),
        "chip_version": getattr(item, "chip_version"),
        "total_hbm_mb": getattr(item, "total_hbm_mb"),
        "used_hbm_mb": getattr(item, "used_hbm_mb"),
        "health": getattr(item, "health"),
        "utilization": getattr(item, "utilization"),
        "processes": [
            {"pid": process.pid, "hbm_mb": process.hbm_mb}
            for process in processes
        ],
    }


def _related_processes() -> list[dict[str, object]]:
    ignored = _process_ancestry()
    uid = os.getuid()
    result: list[dict[str, object]] = []
    for directory in sorted(Path("/proc").iterdir(), key=lambda item: item.name):
        if not directory.name.isdigit():
            continue
        pid = int(directory.name)
        if pid in ignored:
            continue
        try:
            if directory.stat().st_uid != uid:
                continue
            cmdline = (directory / "cmdline").read_bytes().split(b"\0")
            argv = [item.decode("utf-8", errors="replace") for item in cmdline if item]
            comm = (directory / "comm").read_text(encoding="utf-8").strip()
            stat_fields = (directory / "stat").read_text(encoding="utf-8").split()
        except (OSError, UnicodeDecodeError):
            continue
        category = _process_category(argv, comm)
        if category is None:
            continue
        result.append(
            {
                "pid": pid,
                "category": category,
                "executable": Path(argv[0]).name if argv else comm,
                "start_ticks": int(stat_fields[21]),
            }
        )
    return result


def _process_category(argv: Sequence[str], comm: str) -> str | None:
    executable = Path(argv[0]).name if argv else comm
    joined = " ".join(argv)
    if executable in _RELATED_EXECUTABLES or comm.startswith("ray::"):
        return "ray"
    if (
        executable.startswith("vllm")
        or comm.startswith("VLLM::")
        or "vllm.entrypoints" in joined
        or "vllm.v1.engine" in joined
    ):
        return "vllm"
    if "ascend_maze.cli.main" in joined and re.search(
        r"\b(controller|node)\s+(start|run)\b", joined
    ):
        return "maze"
    if executable == "maze" and re.search(r"\b(controller|node)\b", joined):
        return "maze"
    return None


def _process_ancestry() -> set[int]:
    result: set[int] = set()
    pid = os.getpid()
    while pid > 1 and pid not in result:
        result.add(pid)
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            pid = int(fields[3])
        except (OSError, ValueError, IndexError):
            break
    return result


def _related_listeners(pids: set[int]) -> list[dict[str, object]]:
    if not pids:
        return []
    executable = shutil.which("ss")
    if executable is None:
        raise ExperimentValidationError("ss is required for listener auditing")
    output = _run_checked((executable, "-H", "-lntp"))
    result: list[dict[str, object]] = []
    for line in output.splitlines():
        matched = {int(value) for value in _PID_RE.findall(line)}.intersection(pids)
        if not matched:
            continue
        fields = line.split()
        local_address = fields[3] if len(fields) > 3 else "unknown"
        for pid in sorted(matched):
            result.append({"pid": pid, "local_address": local_address})
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _run_checked(argv: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            tuple(argv),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExperimentValidationError(
            f"read-only admission command failed: {argv[0]}: {exc}"
        ) from exc
    return completed.stdout
