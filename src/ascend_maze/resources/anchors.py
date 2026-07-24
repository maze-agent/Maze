"""C5 resource anchors used by the stage-two correctness path."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from threading import RLock
from typing import Protocol, runtime_checkable

from ascend_maze.compiler.ir import CompiledWorkflow, TaskDefinition, TaskNode
from ascend_maze.contracts.resources import ExecutionTarget, ResourceSpec
from ascend_maze.core.canonical import canonical_digest


_ZERO_RESOURCES = ResourceSpec(cpu_num=0, mem_mb=0, npu_mem_mb=0, io_num=0)


@dataclass(frozen=True, slots=True)
class ResourceAnchor:
    definition_id: str
    task_kind: str
    execution_target: ExecutionTarget
    declared: ResourceSpec
    static_inferred: ResourceSpec
    learned: ResourceSpec | None
    effective: ResourceSpec
    model_id: str | None
    profile_key: str
    revision: int
    strategy: str


@dataclass(frozen=True, slots=True)
class OomReanchorResult:
    anchor: ResourceAnchor
    created: bool
    reason: str
    previous_npu_mem_mb: int
    observed_peak_npu_mem_mb: int | None


@runtime_checkable
class ResourceAnchorProvider(Protocol):
    strategy: str

    def resolve(
        self,
        *,
        run_id: str,
        compiled: CompiledWorkflow,
        task_id: str,
    ) -> ResourceAnchor: ...

    def reanchor_after_oom(
        self,
        *,
        run_id: str,
        compiled: CompiledWorkflow,
        task_id: str,
        observed_peak_npu_mem_mb: int | None,
    ) -> OomReanchorResult: ...

    def destroy_run(self, run_id: str) -> int: ...

    def count_for_run(self, run_id: str) -> int: ...


def _elementwise_max(left: ResourceSpec, right: ResourceSpec) -> ResourceSpec:
    return ResourceSpec(
        cpu_num=max(left.cpu_num, right.cpu_num),
        mem_mb=max(left.mem_mb, right.mem_mb),
        npu_mem_mb=max(left.npu_mem_mb, right.npu_mem_mb),
        io_num=max(left.io_num, right.io_num),
    )


class DeclaredOnlyAnchorProvider:
    """Resolve immutable per-run anchors from compiled declarations only."""

    strategy = "declared_only"

    def __init__(
        self,
        *,
        environment_fingerprint: str,
        oom_growth_factor: float = 1.25,
        oom_safety_margin: float = 1.10,
    ) -> None:
        if not isinstance(environment_fingerprint, str) or not environment_fingerprint:
            raise ValueError("environment_fingerprint is required")
        self.environment_fingerprint = environment_fingerprint
        if oom_growth_factor <= 1 or oom_safety_margin < 1:
            raise ValueError("OOM growth and safety factors are invalid")
        self.oom_growth_factor = oom_growth_factor
        self.oom_safety_margin = oom_safety_margin
        self._anchors: dict[tuple[str, str], ResourceAnchor] = {}
        self._oom_reanchored: set[tuple[str, str]] = set()
        self._lock = RLock()

    def resolve(
        self,
        *,
        run_id: str,
        compiled: CompiledWorkflow,
        task_id: str,
    ) -> ResourceAnchor:
        key = (run_id, task_id)
        with self._lock:
            cached = self._anchors.get(key)
            if cached is not None:
                return cached
            node: TaskNode = compiled.tasks[task_id]
            definition: TaskDefinition = compiled.definitions[node.definition_id]
            target = (
                ExecutionTarget.MODEL_SERVICE
                if node.model_anchor is not None
                and node.model_anchor.mode == "service"
                else ExecutionTarget.LOCAL_WORKER
            )
            model_id = None if node.model_anchor is None else node.model_anchor.model
            profile_key = canonical_digest(
                {
                    "definition_id": definition.definition_id,
                    "code_hash": definition.code_hash,
                    "environment_fingerprint": self.environment_fingerprint,
                    "execution_target": target.value,
                    "model_id": model_id,
                    "strategy": self.strategy,
                }
            )
            anchor = ResourceAnchor(
                definition_id=definition.definition_id,
                task_kind=definition.task_kind,
                execution_target=target,
                declared=definition.resources,
                static_inferred=_ZERO_RESOURCES,
                learned=None,
                effective=definition.resources,
                model_id=model_id,
                profile_key=profile_key,
                revision=1,
                strategy=self.strategy,
            )
            self._anchors[key] = anchor
            return anchor

    def reanchor_after_oom(
        self,
        *,
        run_id: str,
        compiled: CompiledWorkflow,
        task_id: str,
        observed_peak_npu_mem_mb: int | None,
    ) -> OomReanchorResult:
        if observed_peak_npu_mem_mb is not None and observed_peak_npu_mem_mb < 0:
            raise ValueError("observed peak NPU HBM must be non-negative")
        key = (run_id, task_id)
        with self._lock:
            current = self.resolve(run_id=run_id, compiled=compiled, task_id=task_id)
            previous = current.effective.npu_mem_mb
            if key in self._oom_reanchored:
                return OomReanchorResult(
                    current,
                    False,
                    "oom_reanchor_limit_exhausted",
                    previous,
                    observed_peak_npu_mem_mb,
                )
            candidates = [math.ceil(previous * self.oom_growth_factor)]
            if observed_peak_npu_mem_mb is not None:
                candidates.append(
                    math.ceil(observed_peak_npu_mem_mb * self.oom_safety_margin)
                )
            next_hbm = max(1, *candidates)
            learned = ResourceSpec(0, 0, next_hbm, 0)
            updated = replace(
                current,
                learned=learned,
                effective=replace(current.effective, npu_mem_mb=next_hbm),
                revision=current.revision + 1,
            )
            self._anchors[key] = updated
            self._oom_reanchored.add(key)
            return OomReanchorResult(
                updated,
                True,
                "oom_reanchored",
                previous,
                observed_peak_npu_mem_mb,
            )

    def destroy_run(self, run_id: str) -> int:
        with self._lock:
            keys = [key for key in self._anchors if key[0] == run_id]
            for key in keys:
                del self._anchors[key]
                self._oom_reanchored.discard(key)
            return len(keys)

    def count_for_run(self, run_id: str) -> int:
        with self._lock:
            return sum(key[0] == run_id for key in self._anchors)


class StaticAnchorProvider(DeclaredOnlyAnchorProvider):
    """Merge deterministic compile-time hints with user-declared lower bounds."""

    strategy = "static"

    def resolve(
        self,
        *,
        run_id: str,
        compiled: CompiledWorkflow,
        task_id: str,
    ) -> ResourceAnchor:
        anchor = super().resolve(run_id=run_id, compiled=compiled, task_id=task_id)
        key = (run_id, task_id)
        with self._lock:
            definition = compiled.definitions[compiled.tasks[task_id].definition_id]
            effective = _elementwise_max(
                definition.resources,
                definition.static_inferred,
            )
            current = self._anchors[key]
            if (
                current.static_inferred == definition.static_inferred
                and current.effective == effective
            ):
                return current
            updated = replace(
                anchor,
                static_inferred=definition.static_inferred,
                effective=effective,
                strategy=self.strategy,
                profile_key=canonical_digest(
                    {
                        "definition_id": definition.definition_id,
                        "code_hash": definition.code_hash,
                        "environment_fingerprint": self.environment_fingerprint,
                        "execution_target": anchor.execution_target.value,
                        "model_id": anchor.model_id,
                        "strategy": self.strategy,
                        "static_inferred": {
                            "cpu_num": definition.static_inferred.cpu_num,
                            "mem_mb": definition.static_inferred.mem_mb,
                            "npu_mem_mb": definition.static_inferred.npu_mem_mb,
                            "io_num": definition.static_inferred.io_num,
                        },
                    }
                ),
            )
            self._anchors[key] = updated
            return updated
