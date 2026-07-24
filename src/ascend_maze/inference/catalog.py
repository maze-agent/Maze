"""Immutable, offline-validated model catalog."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ascend_maze.compiler.ir import CompiledWorkflow
from ascend_maze.core.errors import ContractValidationError
from ascend_maze.inference.contracts import (
    InferenceEngineAdapter,
    ModelSpec,
    model_catalog_digest,
)


class ModelCatalog:
    def __init__(
        self,
        specs: tuple[ModelSpec, ...],
        *,
        adapters: Mapping[str, InferenceEngineAdapter],
        environment_capabilities: tuple[str, ...] = (),
        max_single_npu_hbm_mb: int | None = None,
    ) -> None:
        if not specs:
            raise ContractValidationError("ModelCatalog requires at least one model")
        model_ids = [spec.model_id for spec in specs]
        if len(model_ids) != len(set(model_ids)):
            raise ContractValidationError("ModelCatalog model IDs must be unique")
        revisions = {spec.catalog_revision for spec in specs}
        if len(revisions) != 1:
            raise ContractValidationError(
                "all ModelCatalog entries must share one catalog revision"
            )
        if any(not isinstance(name, str) or not name for name in adapters):
            raise ContractValidationError("Adapter registry names must be non-empty")
        capabilities = frozenset(environment_capabilities)
        ordered = tuple(sorted(specs, key=lambda item: item.model_id))
        for spec in ordered:
            adapter = adapters.get(spec.backend)
            if adapter is None:
                raise ContractValidationError(
                    f"unsupported model backend: {spec.backend}"
                )
            artifact = Path(spec.artifact_path)
            if not artifact.is_dir():
                raise ContractValidationError(
                    f"model artifact directory does not exist: {artifact}"
                )
            if spec.tokenizer_path is not None and not Path(
                spec.tokenizer_path
            ).exists():
                raise ContractValidationError(
                    f"tokenizer path does not exist: {spec.tokenizer_path}"
                )
            missing = set(spec.required_capabilities) - capabilities
            if missing:
                raise ContractValidationError(
                    f"model {spec.model_id} requires unavailable capabilities: "
                    + ", ".join(sorted(missing))
                )
            if (
                max_single_npu_hbm_mb is not None
                and spec.instance_hbm_mb > max_single_npu_hbm_mb
            ):
                raise ContractValidationError(
                    f"model {spec.model_id} exceeds single-NPU HBM capacity"
                )
            adapter.validate_model_spec(spec)
        self._specs = ordered
        self._by_id = {spec.model_id: spec for spec in ordered}
        self._adapters = dict(adapters)
        self.catalog_revision = next(iter(revisions))
        self.content_digest = model_catalog_digest(ordered)

    @property
    def specs(self) -> tuple[ModelSpec, ...]:
        return self._specs

    def get(self, model_id: str) -> ModelSpec:
        try:
            return self._by_id[model_id]
        except KeyError as exc:
            raise ContractValidationError(
                f"model is not registered in ModelCatalog: {model_id}"
            ) from exc

    def adapter(self, model_id: str) -> InferenceEngineAdapter:
        return self._adapters[self.get(model_id).backend]

    def adapters(self) -> tuple[InferenceEngineAdapter, ...]:
        return tuple(
            self._adapters[name]
            for name in sorted(self._adapters)
        )

    def validate_workflow(self, compiled: CompiledWorkflow) -> None:
        for node in compiled.tasks.values():
            anchor = node.model_anchor
            if anchor is None:
                continue
            spec = self.get(anchor.model)
            if spec.catalog_revision != self.catalog_revision:
                raise ContractValidationError("model catalog revision mismatch")
            if anchor.mode == "service" and spec.backend not in self._adapters:
                raise ContractValidationError(
                    f"model {spec.model_id} has no service adapter"
                )
