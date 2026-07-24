"""Strict versioned config override documents for managed Controller startup."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from ascend_maze.core.errors import ContractValidationError

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class ConfigOverrideDocument:
    build_revision: str
    expected_config_fingerprint: str
    overrides: tuple[tuple[str, object], ...]


def load_config_override_document(path: str | Path) -> ConfigOverrideDocument:
    try:
        source = Path(path).expanduser().resolve(strict=True)
        value = json.loads(source.read_bytes(), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            f"cannot read config override document: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ContractValidationError("config override document must be an object")
    allowed = {
        "schema_version",
        "schema",
        "build_revision",
        "expected_config_fingerprint",
        "overrides",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractValidationError(
            f"config override document field is unknown: {unknown[0]}"
        )
    if value.get("schema_version") != 1 or value.get("schema") != (
        "ascend-maze.controller-config-overrides.v1"
    ):
        raise ContractValidationError("config override document schema is invalid")
    revision = value.get("build_revision")
    fingerprint = value.get("expected_config_fingerprint")
    if not isinstance(revision, str) or not _GIT_REVISION_RE.fullmatch(revision):
        raise ContractValidationError("config override build_revision is invalid")
    if not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(fingerprint):
        raise ContractValidationError(
            "config override expected_config_fingerprint is invalid"
        )
    raw = value.get("overrides")
    if not isinstance(raw, list):
        raise ContractValidationError("config override entries must be an array")
    overrides: list[tuple[str, object]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"path", "value"}:
            raise ContractValidationError(
                f"config override entry {index} must contain path and value"
            )
        config_path = item.get("path")
        if not isinstance(config_path, str) or not config_path:
            raise ContractValidationError(
                f"config override entry {index} path is invalid"
            )
        overrides.append((config_path, item.get("value")))
    if len({name for name, _ in overrides}) != len(overrides):
        raise ContractValidationError("config override paths are duplicated")
    return ConfigOverrideDocument(
        build_revision=revision,
        expected_config_fingerprint=fingerprint,
        overrides=tuple(overrides),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError(
                f"config override document contains duplicate field: {key}"
            )
        result[key] = value
    return result
