"""Strict TOML loader producing one immutable C13 configuration snapshot."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib.metadata
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence, TypeVar, cast

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the supported Python 3.10 environment
    import tomli as tomllib

from ascend_maze import __version__
from ascend_maze.config.schema import (
    ClusterConfig,
    ControlConfig,
    DataConfig,
    FaultConfig,
    InferenceConfig,
    MainConfig,
    PlacementConfig,
    RayRuntimeConfig,
    RecordingConfig,
    SchedulerConfig,
    WorkerConfig,
    WorkflowConfig,
)
from ascend_maze.config.model_catalog import load_model_catalog
from ascend_maze.contracts.config import ConfigSnapshot
from ascend_maze.core.canonical import CanonicalValue, FrozenMap
from ascend_maze.core.errors import ContractValidationError

DEFAULT_CONFIG_NAME = "ascend-maze.toml"
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: MainConfig
    snapshot: ConfigSnapshot
    source_bytes_digest: str

    def rendered(self) -> FrozenMap[CanonicalValue, CanonicalValue]:
        return self.snapshot.resolved


_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "profile",
        "control",
        "workflow",
        "data",
        "cluster",
        "runtime",
        "scheduler",
        "placement",
        "worker",
        "inference",
        "recording",
        "fault",
    }
)


def resolve_config_path(path: str | Path | None = None) -> Path:
    selected = path
    if selected is None:
        selected = os.environ.get("ASCEND_MAZE_CONFIG", DEFAULT_CONFIG_NAME)
    candidate = Path(selected).expanduser().resolve(strict=False)
    if not candidate.is_file():
        raise ContractValidationError(f"config: file does not exist: {candidate}")
    return candidate


def load_config(
    path: str | Path | None = None,
    *,
    build_revision: str = "uncommitted",
    created_at_ms: int | None = None,
    config_overrides: Sequence[tuple[str, object]] = (),
) -> LoadedConfig:
    source = resolve_config_path(path)
    raw_bytes = source.read_bytes()
    try:
        document = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ContractValidationError(f"config: invalid TOML: {exc}") from exc
    _apply_config_overrides(document, config_overrides)
    _reject_unknown(document, _ROOT_KEYS, "")
    schema_version = _integer(document.get("schema_version", 1), "schema_version")
    profile = _string(document.get("profile", "correctness"), "profile")
    base = source.parent

    control_raw = _table(document, "control")
    workflow_raw = _table(document, "workflow")
    data_raw = _table(document, "data")
    cluster_raw = _table(document, "cluster")
    runtime_raw = _table(document, "runtime")
    _reject_unknown(runtime_raw, frozenset({"ray"}), "runtime")
    ray_raw = _table(runtime_raw, "ray", prefix="runtime")
    scheduler_raw = _table(document, "scheduler")
    placement_raw = _table(document, "placement")
    worker_raw = _table(document, "worker")
    inference_raw = _table(document, "inference")
    recording_raw = _table(document, "recording")
    fault_raw = _table(document, "fault")

    raw_runtime_directory = control_raw.get("runtime_directory")
    if raw_runtime_directory is None:
        raw_runtime_directory = _default_runtime_directory()
    runtime_directory = _path(
        base,
        raw_runtime_directory,
        "control.runtime_directory",
    )
    control = _construct(
        ControlConfig,
        control_raw,
        "control",
        defaults={
            "socket_path": str(Path(runtime_directory) / "control.sock"),
            "runtime_directory": runtime_directory,
            "pid_file": str(Path(runtime_directory) / "controller.pid"),
            "cluster_token_file": str(Path(runtime_directory) / "cluster.token"),
            "recovery_path": str(Path(runtime_directory) / "controller.sqlite3"),
        },
        path_fields={
            "socket_path",
            "runtime_directory",
            "pid_file",
            "cluster_token_file",
            "recovery_path",
        },
        base=base,
    )
    workflow = _construct(WorkflowConfig, workflow_raw, "workflow")
    shared_roots = data_raw.get("shared_filesystem_roots", [])
    if not isinstance(shared_roots, list) or any(
        not isinstance(item, str) for item in shared_roots
    ):
        raise ContractValidationError(
            "data.shared_filesystem_roots: must be an array of paths"
        )
    _reject_unknown(data_raw, frozenset({"shared_filesystem_roots"}), "data")
    data = DataConfig(
        tuple(
            sorted(
                _path(base, item, "data.shared_filesystem_roots")
                for item in shared_roots
            )
        )
    )
    cluster = _construct(ClusterConfig, cluster_raw, "cluster")
    ray = _construct(
        RayRuntimeConfig,
        ray_raw,
        "runtime.ray",
        defaults={"temp_directory": str(Path(runtime_directory) / "ray")},
        path_fields={"temp_directory"},
        base=base,
    )
    scheduler = _construct(SchedulerConfig, scheduler_raw, "scheduler")
    placement = _construct(PlacementConfig, placement_raw, "placement")
    worker = _construct(WorkerConfig, worker_raw, "worker")
    inference = _construct(
        InferenceConfig,
        inference_raw,
        "inference",
        path_fields={"model_catalog_path"},
        base=base,
    )
    recording = _construct(
        RecordingConfig,
        recording_raw,
        "recording",
        defaults={"root_directory": str(Path(runtime_directory) / "records")},
        path_fields={"root_directory", "cursor_signing_key_file"},
        base=base,
    )
    fault = _construct(FaultConfig, fault_raw, "fault")
    config = MainConfig(
        schema_version=schema_version,
        profile=profile,
        source_path=str(source),
        control=control,
        workflow=workflow,
        data=data,
        cluster=cluster,
        ray=ray,
        scheduler=scheduler,
        placement=placement,
        worker=worker,
        inference=inference,
        recording=recording,
        fault=fault,
    )
    catalog_revision, catalog_digest = _catalog_identity(
        inference.model_catalog_path,
        environment_fingerprint=cluster.environment_fingerprint,
    )
    resolved = _resolved_payload(config)
    resolved_inference = resolved["inference"]
    assert isinstance(resolved_inference, dict)
    resolved_inference["model_catalog_content_digest"] = catalog_digest
    snapshot = ConfigSnapshot.create(
        schema_version=schema_version,
        project_version=__version__,
        source_path=str(source),
        resolved=resolved,
        model_catalog_revision=catalog_revision,
        build_revision=build_revision,
        runtime_versions=_runtime_versions(),
        created_at_ms=created_at_ms,
    )
    from hashlib import sha256

    return LoadedConfig(config, snapshot, sha256(raw_bytes).hexdigest())


def _apply_config_overrides(
    document: dict[str, Any], overrides: Sequence[tuple[str, object]]
) -> None:
    seen: set[str] = set()
    for path, value in overrides:
        if not isinstance(path, str) or not path:
            raise ContractValidationError(
                "config override path must be a non-empty dotted string"
            )
        if path in seen:
            raise ContractValidationError(f"config override is duplicated: {path}")
        seen.add(path)
        parts = path.split(".")
        if any(not part or not part.replace("_", "a").isalnum() for part in parts):
            raise ContractValidationError(f"invalid config override path: {path}")
        target: dict[str, Any] = document
        for index, part in enumerate(parts[:-1]):
            existing = target.get(part)
            if existing is None:
                nested: dict[str, Any] = {}
                target[part] = nested
                target = nested
                continue
            if not isinstance(existing, dict):
                prefix = ".".join(parts[: index + 1])
                raise ContractValidationError(
                    f"config override traverses non-table field: {prefix}"
                )
            target = existing
        target[parts[-1]] = value


def _construct(
    target: type[_T],
    raw: Mapping[str, object],
    prefix: str,
    *,
    defaults: Mapping[str, object] | None = None,
    path_fields: set[str] | None = None,
    base: Path | None = None,
) -> _T:
    fields = frozenset(cast(Any, target).__dataclass_fields__)
    _reject_unknown(raw, fields, prefix)
    values = dict(defaults or {})
    values.update(raw)
    for name in path_fields or set():
        if name in values and values[name] is not None:
            assert base is not None
            values[name] = _path(base, values[name], f"{prefix}.{name}")
    try:
        return target(**values)
    except TypeError as exc:
        raise ContractValidationError(f"{prefix}: invalid field type: {exc}") from exc


def _resolved_payload(config: MainConfig) -> dict[str, object]:
    payload = asdict(config)
    payload.pop("source_path")
    ray = payload.pop("ray")
    payload["runtime"] = {"ray": ray}
    payload["control"]["cluster_token"] = "<redacted>"  # type: ignore[index]
    return payload


def _catalog_identity(
    path: str | None,
    *,
    environment_fingerprint: str,
) -> tuple[str, str | None]:
    if path is None:
        return "no-model-catalog", None
    document = load_model_catalog(
        path,
        environment_fingerprint=environment_fingerprint,
    )
    return document.catalog_revision, document.content_digest


def _runtime_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for package in ("cloudpickle", "pyarrow", "ray", "torch", "torch-npu"):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = "not-installed"
    return result


def _default_runtime_directory() -> str:
    value = os.environ.get("XDG_RUNTIME_DIR")
    if not value:
        raise ContractValidationError(
            "control.runtime_directory: required when XDG_RUNTIME_DIR is unavailable"
        )
    return str(Path(value) / "ascend-maze")


def _path(base: Path, value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractValidationError(f"{field}: must be a non-empty path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return str(candidate.resolve(strict=False))


def _table(
    document: Mapping[str, object],
    name: str,
    *,
    prefix: str = "",
) -> Mapping[str, object]:
    value = document.get(name, {})
    field = f"{prefix}.{name}" if prefix else name
    if not isinstance(value, dict):
        raise ContractValidationError(f"{field}: must be a TOML table")
    return value


def _reject_unknown(
    document: Mapping[str, object], allowed: frozenset[str], prefix: str
) -> None:
    unknown = sorted(set(document) - allowed)
    if unknown:
        field = f"{prefix}.{unknown[0]}" if prefix else unknown[0]
        raise ContractValidationError(f"{field}: unknown configuration field")


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractValidationError(f"{field}: must be an integer")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractValidationError(f"{field}: must be a non-empty string")
    return value
