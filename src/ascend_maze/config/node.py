"""Strict worker-node bootstrap configuration, separate from global policy."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from ascend_maze.contracts.runtime import RuntimeDeviceMapping
from ascend_maze.core.errors import ContractValidationError


@dataclass(frozen=True, slots=True)
class NodeBootstrapConfig:
    schema_version: int
    source_path: str
    cluster_id: str
    node_id: str
    node_ip: str
    controller_endpoint: str
    authorization_token_file: str
    runtime_directory: str
    worker_rpc_bind_address: str
    worker_advertised_host: str | None
    ray_temp_directory: str
    ray_num_cpus: int
    recording_root_directory: str
    device_mappings: tuple[RuntimeDeviceMapping, ...] = ()


_ALLOWED = frozenset(
    {
        "schema_version",
        "cluster_id",
        "node_id",
        "node_ip",
        "controller_endpoint",
        "authorization_token_file",
        "runtime_directory",
        "worker_rpc_bind_address",
        "worker_advertised_host",
        "ray_temp_directory",
        "ray_num_cpus",
        "recording_root_directory",
        "device_mappings",
    }
)


def load_node_bootstrap(path: str | Path) -> NodeBootstrapConfig:
    source = Path(path).expanduser().resolve(strict=False)
    if not source.is_file():
        raise ContractValidationError(f"node bootstrap file does not exist: {source}")
    try:
        document = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ContractValidationError(f"invalid node bootstrap TOML: {exc}") from exc
    unknown = sorted(set(document) - _ALLOWED)
    if unknown:
        raise ContractValidationError(f"node.{unknown[0]}: unknown field")
    schema_version = document.get("schema_version", 1)
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise ContractValidationError("node.schema_version: unsupported value")
    base = source.parent
    runtime_value = document.get("runtime_directory")
    if runtime_value is None:
        runtime_value = _default_runtime_directory()
    runtime = _path(
        base,
        runtime_value,
        "runtime_directory",
    )
    values = {
        "schema_version": 1,
        "source_path": str(source),
        "cluster_id": _string(document, "cluster_id"),
        "node_id": _string(document, "node_id"),
        "node_ip": _string(document, "node_ip"),
        "controller_endpoint": _string(document, "controller_endpoint"),
        "authorization_token_file": _path(
            base,
            document.get("authorization_token_file"),
            "authorization_token_file",
        ),
        "runtime_directory": runtime,
        "worker_rpc_bind_address": _optional_string(
            document,
            "worker_rpc_bind_address",
            default="0.0.0.0:0",
        ),
        "worker_advertised_host": _optional_string(
            document,
            "worker_advertised_host",
            default=None,
        ),
        "ray_temp_directory": _path(
            base,
            document.get("ray_temp_directory", str(Path(runtime) / "ray")),
            "ray_temp_directory",
        ),
        "ray_num_cpus": document.get("ray_num_cpus", os.cpu_count() or 1),
        "recording_root_directory": _path(
            base,
            document.get(
                "recording_root_directory", str(Path(runtime) / "records")
            ),
            "recording_root_directory",
        ),
        "device_mappings": _device_mappings(document.get("device_mappings", [])),
    }
    for name in (
        "ray_num_cpus",
    ):
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ContractValidationError(f"node.{name}: must be positive")
    return NodeBootstrapConfig(**values)  # type: ignore[arg-type]


def _device_mappings(value: object) -> tuple[RuntimeDeviceMapping, ...]:
    if not isinstance(value, list):
        raise ContractValidationError("node.device_mappings: must be an array")
    mappings: list[RuntimeDeviceMapping] = []
    allowed = {
        "physical_device_id",
        "runtime_visible_device_id",
        "visible_device_index",
    }
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ContractValidationError(
                f"node.device_mappings[{index}]: must be a table"
            )
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise ContractValidationError(
                f"node.device_mappings[{index}].{unknown[0]}: unknown field"
            )
        try:
            mappings.append(
                RuntimeDeviceMapping(
                    physical_device_id=item.get("physical_device_id"),  # type: ignore[arg-type]
                    runtime_visible_device_id=item.get(  # type: ignore[arg-type]
                        "runtime_visible_device_id"
                    ),
                    visible_device_index=item.get(  # type: ignore[arg-type]
                        "visible_device_index", 0
                    ),
                )
            )
        except ContractValidationError as exc:
            raise ContractValidationError(
                f"node.device_mappings[{index}]: {exc}"
            ) from exc
    physical_ids = tuple(item.physical_device_id for item in mappings)
    if len(physical_ids) != len(set(physical_ids)):
        raise ContractValidationError(
            "node.device_mappings: physical_device_id values must be unique"
        )
    return tuple(sorted(mappings))


def _string(document: dict[str, object], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise ContractValidationError(f"node.{name}: value is required")
    return value


def _optional_string(
    document: dict[str, object],
    name: str,
    *,
    default: str | None,
) -> str | None:
    value = document.get(name, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ContractValidationError(f"node.{name}: must be a non-empty string")
    return value


def _path(base: Path, value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractValidationError(f"node.{name}: path is required")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return str(candidate.resolve(strict=False))


def _default_runtime_directory() -> str:
    root = os.environ.get("XDG_RUNTIME_DIR")
    if not root:
        raise ContractValidationError(
            "node.runtime_directory is required without XDG_RUNTIME_DIR"
        )
    return str(Path(root) / "ascend-maze-node")
