"""Versioned stage-6A Fake inference profile and ConfigSnapshot assembly."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ascend_maze.contracts.config import ConfigSnapshot
from ascend_maze.core.errors import ContractValidationError
from ascend_maze.inference import ModelCatalog


@dataclass(frozen=True, slots=True)
class Stage6AConfig:
    reconcile_interval_ms: int = 100
    affinity_ttl_ms: int = 300_000
    affinity_capacity: int = 10_000
    dispatch_timeout_ms: int = 5_000
    scheduler_policy: str = "fcfs"
    anchor_strategy: str = "declared_only"
    runtime_backend: str = "fake"
    standby_enabled: bool = False
    allow_colocation: bool = False
    max_tasks_per_worker: int = 1

    def __post_init__(self) -> None:
        for name in (
            "reconcile_interval_ms",
            "affinity_ttl_ms",
            "affinity_capacity",
            "dispatch_timeout_ms",
            "max_tasks_per_worker",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ContractValidationError(f"{name} must be positive")
        if self.scheduler_policy != "fcfs":
            raise ContractValidationError("stage 6A requires FCFS")
        if self.anchor_strategy != "declared_only":
            raise ContractValidationError("stage 6A requires declared_only anchors")
        if self.runtime_backend != "fake":
            raise ContractValidationError("stage 6A requires FakeRuntime")
        if self.standby_enabled:
            raise ContractValidationError("stage 6A correctness disables Standby")
        if self.allow_colocation:
            raise ContractValidationError("stage 6A correctness disables colocation")
        if self.max_tasks_per_worker != 1:
            raise ContractValidationError(
                "stage 6A correctness requires one Task per Worker"
            )


def create_stage6a_config_snapshot(
    config: Stage6AConfig,
    catalog: ModelCatalog,
    *,
    environment_fingerprint: str,
    source_path: str,
    build_revision: str,
    runtime_versions: Mapping[str, str] | None = None,
    project_version: str = "0.1.0",
    created_at_ms: int | None = None,
) -> ConfigSnapshot:
    if not environment_fingerprint:
        raise ContractValidationError("environment_fingerprint is required")
    mismatched = tuple(
        spec.model_id
        for spec in catalog.specs
        if spec.environment_fingerprint != environment_fingerprint
    )
    if mismatched:
        raise ContractValidationError(
            "ModelCatalog environment mismatch: " + ", ".join(mismatched)
        )
    if not config.allow_colocation and any(
        spec.allow_colocation for spec in catalog.specs
    ):
        raise ContractValidationError(
            "stage 6A correctness catalog cannot enable model colocation"
        )
    resolved = {
        "profile": "stage6a-correctness",
        "environment_fingerprint": environment_fingerprint,
        "scheduler": {"policy": config.scheduler_policy},
        "anchor": {"strategy": config.anchor_strategy},
        "runtime": {"backend": config.runtime_backend},
        "worker": {
            "standby_enabled": config.standby_enabled,
            "allow_colocation": config.allow_colocation,
            "max_tasks_per_worker": config.max_tasks_per_worker,
            "dispatch_timeout_ms": config.dispatch_timeout_ms,
        },
        "inference": {
            "adapter": "fake",
            "catalog_revision": catalog.catalog_revision,
            "catalog_content_digest": catalog.content_digest,
            "models": tuple(spec.canonical_payload() for spec in catalog.specs),
            "affinity_ttl_ms": config.affinity_ttl_ms,
            "affinity_capacity": config.affinity_capacity,
            "reconcile_interval_ms": config.reconcile_interval_ms,
        },
    }
    return ConfigSnapshot.create(
        schema_version=1,
        project_version=project_version,
        source_path=source_path,
        resolved=resolved,
        model_catalog_revision=catalog.catalog_revision,
        build_revision=build_revision,
        runtime_versions=runtime_versions,
        created_at_ms=created_at_ms,
    )
