"""Frozen resolved configuration snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping

from ascend_maze.core.canonical import (
    CanonicalValue,
    FrozenMap,
    canonical_digest,
    freeze_canonical,
)
from ascend_maze.core.errors import ContractValidationError
from ascend_maze.core.time import wall_time_ms

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    schema_version: int
    project_version: str
    source_path: str
    resolved: FrozenMap[CanonicalValue, CanonicalValue]
    model_catalog_revision: str
    config_fingerprint: str
    created_at_ms: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version < 1
        ):
            raise ContractValidationError("schema_version must be a positive integer")
        if any(
            not isinstance(value, str) or not value
            for value in (self.project_version, self.model_catalog_revision)
        ):
            raise ContractValidationError(
                "project_version and model_catalog_revision are required"
            )
        if not isinstance(self.source_path, str) or not self.source_path:
            raise ContractValidationError("source_path must be a non-empty string")
        if (
            not isinstance(self.config_fingerprint, str)
            or not _SHA256_RE.fullmatch(self.config_fingerprint)
        ):
            raise ContractValidationError(
                "config_fingerprint must be a lowercase SHA-256 hex digest"
            )
        if (
            isinstance(self.created_at_ms, bool)
            or not isinstance(self.created_at_ms, int)
            or self.created_at_ms < 0
        ):
            raise ContractValidationError("created_at_ms must be non-negative")
        frozen = freeze_canonical(self.resolved)
        if not isinstance(frozen, FrozenMap):
            raise ContractValidationError("resolved config must be a mapping")
        object.__setattr__(self, "resolved", frozen)
        object.__setattr__(
            self,
            "source_path",
            str(Path(self.source_path).expanduser().resolve(strict=False)),
        )

    @classmethod
    def create(
        cls,
        *,
        schema_version: int,
        project_version: str,
        source_path: str,
        resolved: Mapping[str, object],
        model_catalog_revision: str,
        build_revision: str,
        runtime_versions: Mapping[str, str] | None = None,
        created_at_ms: int | None = None,
    ) -> "ConfigSnapshot":
        if not isinstance(build_revision, str) or not build_revision:
            raise ContractValidationError("build_revision is required")
        if runtime_versions is not None and (
            not isinstance(runtime_versions, Mapping)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in runtime_versions.items()
            )
        ):
            raise ContractValidationError(
                "runtime_versions must map strings to strings"
            )
        frozen = freeze_canonical(resolved)
        if not isinstance(frozen, FrozenMap):
            raise ContractValidationError("resolved config must be a mapping")
        fingerprint = canonical_digest(
            {
                "schema_version": schema_version,
                "project_version": project_version,
                "resolved": frozen,
                "model_catalog_revision": model_catalog_revision,
                "build_revision": build_revision,
                "runtime_versions": dict(runtime_versions or {}),
            }
        )
        return cls(
            schema_version=schema_version,
            project_version=project_version,
            source_path=source_path,
            resolved=frozen,
            model_catalog_revision=model_catalog_revision,
            config_fingerprint=fingerprint,
            created_at_ms=(
                wall_time_ms() if created_at_ms is None else created_at_ms
            ),
        )
