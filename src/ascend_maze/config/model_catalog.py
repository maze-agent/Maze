"""Strict TOML ModelCatalog parsing without loading model weights."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Mapping

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from ascend_maze.core.canonical import canonical_digest
from ascend_maze.core.errors import ContractValidationError, ModelValidationError
from ascend_maze.inference.contracts import ModelSpec


@dataclass(frozen=True, slots=True)
class ModelCatalogDocument:
    catalog_revision: str
    specs: tuple[ModelSpec, ...]
    content_digest: str


def load_model_catalog(
    path: str | Path,
    *,
    environment_fingerprint: str,
) -> ModelCatalogDocument:
    try:
        return _load_model_catalog(
            path,
            environment_fingerprint=environment_fingerprint,
        )
    except ModelValidationError:
        raise
    except ContractValidationError as exc:
        raise ModelValidationError(str(exc)) from exc


def _load_model_catalog(
    path: str | Path,
    *,
    environment_fingerprint: str,
) -> ModelCatalogDocument:
    source = Path(path).expanduser().resolve(strict=False)
    if not source.is_file():
        raise ContractValidationError(f"model catalog file does not exist: {source}")
    try:
        document = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ContractValidationError(f"invalid ModelCatalog TOML: {exc}") from exc
    if set(document) - {"schema_version", "catalog_revision", "models"}:
        unknown = sorted(set(document) - {"schema_version", "catalog_revision", "models"})
        raise ContractValidationError(f"ModelCatalog.{unknown[0]}: unknown field")
    schema_version = document.get("schema_version", 1)
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise ContractValidationError("ModelCatalog.schema_version: unsupported value")
    revision = document.get("catalog_revision")
    if not isinstance(revision, str) or not revision:
        raise ContractValidationError("ModelCatalog.catalog_revision: value is required")
    raw_models = document.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ContractValidationError("ModelCatalog.models: at least one model is required")
    specs = tuple(
        sorted(
            (
                _parse_model(
                    raw,
                    index=index,
                    catalog_revision=revision,
                    base=source.parent,
                    environment_fingerprint=environment_fingerprint,
                )
                for index, raw in enumerate(raw_models)
            ),
            key=lambda item: item.model_id,
        )
    )
    if len({item.model_id for item in specs}) != len(specs):
        raise ContractValidationError("ModelCatalog.models: duplicate model_id")
    return ModelCatalogDocument(
        catalog_revision=revision,
        specs=specs,
        content_digest=canonical_digest(tuple(item.canonical_payload() for item in specs)),
    )


def _parse_model(
    raw: object,
    *,
    index: int,
    catalog_revision: str,
    base: Path,
    environment_fingerprint: str,
) -> ModelSpec:
    prefix = f"ModelCatalog.models[{index}]"
    if not isinstance(raw, dict):
        raise ContractValidationError(f"{prefix}: must be a TOML table")
    allowed = set(ModelSpec.__dataclass_fields__) - {
        "catalog_revision",
        "environment_fingerprint",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ContractValidationError(f"{prefix}.{unknown[0]}: unknown field")
    values = dict(raw)
    for name in ("artifact_path", "tokenizer_path"):
        if values.get(name) is None:
            continue
        value = values[name]
        if not isinstance(value, str) or not value:
            raise ContractValidationError(f"{prefix}.{name}: path is invalid")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        values[name] = str(candidate.resolve(strict=False))
    capabilities = values.get("required_capabilities", [])
    if not isinstance(capabilities, list) or any(
        not isinstance(item, str) or not item for item in capabilities
    ):
        raise ContractValidationError(
            f"{prefix}.required_capabilities: must be an array of strings"
        )
    values["required_capabilities"] = tuple(sorted(set(capabilities)))
    for name in ("launch_options", "warmup_request"):
        value = values.get(name, {})
        if not isinstance(value, Mapping):
            raise ContractValidationError(f"{prefix}.{name}: must be a table")
        values[name] = dict(value)
    values.setdefault("tokenizer_path", None)
    values.setdefault("quantization", None)
    values["catalog_revision"] = catalog_revision
    values["environment_fingerprint"] = environment_fingerprint
    try:
        spec = ModelSpec(**values)
    except TypeError as exc:
        raise ContractValidationError(f"{prefix}: missing or invalid fields: {exc}") from exc
    _validate_artifact(spec, prefix)
    _validate_backend(spec, prefix)
    return spec


def _validate_artifact(spec: ModelSpec, prefix: str) -> None:
    artifact = Path(spec.artifact_path)
    if not artifact.is_dir():
        raise ContractValidationError(
            f"{prefix}.artifact_path: directory does not exist: {artifact}"
        )
    if spec.tokenizer_path is not None and not Path(spec.tokenizer_path).exists():
        raise ContractValidationError(
            f"{prefix}.tokenizer_path: path does not exist: {spec.tokenizer_path}"
        )


def _validate_backend(spec: ModelSpec, prefix: str) -> None:
    if spec.backend == "fake":
        allowed = {"response_prefix"}
    elif spec.backend == "vllm_ascend":
        allowed = {
            "block_size",
            "enable_prefix_caching",
            "enforce_eager",
            "gpu_memory_utilization",
            "log_level",
            "max_num_batched_tokens",
            "max_num_seqs",
            "trust_remote_code",
        }
        if spec.dtype not in {"bfloat16", "float16"}:
            raise ContractValidationError(
                f"{prefix}.dtype: vllm_ascend requires bfloat16 or float16"
            )
        if spec.npu_slots != 1:
            raise ContractValidationError(
                f"{prefix}.npu_slots: vllm_ascend requires one NPU slot"
            )
    elif spec.backend == "transformers_local":
        allowed = {
            "device_id",
            "enable_thinking",
            "generation_method",
            "model_kind",
            "qwen2_5_vl_cpu_unique_consecutive_workaround",
            "request_timeout_ms",
            "runtime_library_paths",
            "trust_remote_code",
        }
        if spec.dtype not in {"bfloat16", "float16"}:
            raise ContractValidationError(
                f"{prefix}.dtype: transformers_local requires bfloat16 or float16"
            )
        if spec.tensor_parallel_size != 1:
            raise ContractValidationError(
                f"{prefix}.tensor_parallel_size: transformers_local requires one NPU"
            )
        if spec.npu_slots != 1:
            raise ContractValidationError(
                f"{prefix}.npu_slots: transformers_local requires one NPU slot"
            )
        if spec.request_capacity != 1:
            raise ContractValidationError(
                f"{prefix}.request_capacity: transformers_local requires capacity one"
            )
    else:
        raise ContractValidationError(f"{prefix}.backend: unsupported value {spec.backend!r}")
    unknown = sorted(str(key) for key in spec.launch_options if str(key) not in allowed)
    if unknown:
        raise ContractValidationError(
            f"{prefix}.launch_options.{unknown[0]}: unsupported for {spec.backend}"
        )
