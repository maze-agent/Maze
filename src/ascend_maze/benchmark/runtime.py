"""Runtime-neutral contracts used by the C14 Trial orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from ascend_maze.benchmark.canonical import canonical_json_digest, thaw
from ascend_maze.benchmark.contracts import CellSpec, ExperimentSpec
from ascend_maze.core.canonical import FrozenMap, freeze_canonical
from ascend_maze.core.errors import ExperimentValidationError

TERMINAL_RUN_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
)


def _frozen_mapping(name: str, value: Mapping[str, object]) -> FrozenMap:
    frozen = freeze_canonical(dict(value))
    if not isinstance(frozen, FrozenMap):
        raise ExperimentValidationError(f"{name} must be a mapping")
    return frozen


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    submission_id: str
    state: str
    run_id: str | None
    replayed: bool = False
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.submission_id:
            raise ExperimentValidationError("submission receipt ID is required")
        if self.state not in {"committed", "aborted"}:
            raise ExperimentValidationError("submission receipt state is invalid")
        if self.state == "committed" and not self.run_id:
            raise ExperimentValidationError("committed submission requires a Run ID")
        if self.state == "aborted" and self.run_id is not None:
            raise ExperimentValidationError("aborted submission cannot have a Run ID")
        if not isinstance(self.replayed, bool):
            raise ExperimentValidationError("submission replayed must be a boolean")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "submission_id": self.submission_id,
            "state": self.state,
            "run_id": self.run_id,
            "replayed": self.replayed,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class TerminalRunResult:
    run_id: str
    status: str
    snapshot: FrozenMap

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ExperimentValidationError("terminal Run ID is required")
        if self.status not in TERMINAL_RUN_STATES:
            raise ExperimentValidationError("Run result is not terminal")
        object.__setattr__(
            self, "snapshot", _frozen_mapping("Run snapshot", self.snapshot)
        )

    @classmethod
    def create(
        cls, run_id: str, status: str, snapshot: Mapping[str, object]
    ) -> "TerminalRunResult":
        return cls(run_id, status, _frozen_mapping("Run snapshot", snapshot))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "snapshot": thaw(self.snapshot),
        }


@dataclass(frozen=True, slots=True)
class RunFlushResult:
    run_id: str
    recording_complete: bool
    committed_files: tuple[str, ...]
    payload: FrozenMap

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ExperimentValidationError("flush Run ID is required")
        if not isinstance(self.recording_complete, bool):
            raise ExperimentValidationError("recording_complete must be a boolean")
        files = tuple(self.committed_files)
        if any(not isinstance(item, str) or not item for item in files):
            raise ExperimentValidationError("committed file paths are invalid")
        if len(files) != len(set(files)):
            raise ExperimentValidationError("committed file paths contain duplicates")
        object.__setattr__(self, "committed_files", files)
        object.__setattr__(
            self, "payload", _frozen_mapping("flush payload", self.payload)
        )

    @classmethod
    def create(
        cls,
        run_id: str,
        recording_complete: bool,
        committed_files: tuple[str, ...],
        payload: Mapping[str, object],
    ) -> "RunFlushResult":
        return cls(
            run_id,
            recording_complete,
            committed_files,
            _frozen_mapping("flush payload", payload),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "recording_complete": self.recording_complete,
            "committed_files": self.committed_files,
            "payload": thaw(self.payload),
        }


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    captured_at_wall_ms: int
    controller_generation: str
    config_fingerprint: str
    payload: FrozenMap
    snapshot_digest: str

    def __post_init__(self) -> None:
        if self.captured_at_wall_ms < 0:
            raise ExperimentValidationError("resource snapshot time is invalid")
        if not self.controller_generation or not self.config_fingerprint:
            raise ExperimentValidationError("resource snapshot identity is incomplete")
        frozen = _frozen_mapping("resource snapshot payload", self.payload)
        object.__setattr__(self, "payload", frozen)
        expected = canonical_json_digest(thaw(frozen))
        if self.snapshot_digest != expected:
            raise ExperimentValidationError("resource snapshot digest is invalid")

    @classmethod
    def create(
        cls,
        *,
        captured_at_wall_ms: int,
        controller_generation: str,
        config_fingerprint: str,
        payload: Mapping[str, object],
    ) -> "ResourceSnapshot":
        frozen = _frozen_mapping("resource snapshot payload", payload)
        return cls(
            captured_at_wall_ms=captured_at_wall_ms,
            controller_generation=controller_generation,
            config_fingerprint=config_fingerprint,
            payload=frozen,
            snapshot_digest=canonical_json_digest(thaw(frozen)),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "captured_at_wall_ms": self.captured_at_wall_ms,
            "controller_generation": self.controller_generation,
            "config_fingerprint": self.config_fingerprint,
            "snapshot_digest": self.snapshot_digest,
            "payload": thaw(self.payload),
        }


@dataclass(frozen=True, slots=True)
class ResourceRecoveryResult:
    recovered: bool
    checked_at_wall_ms: int
    reason_code: str | None
    details: FrozenMap

    def __post_init__(self) -> None:
        if not isinstance(self.recovered, bool) or self.checked_at_wall_ms < 0:
            raise ExperimentValidationError("resource recovery result is invalid")
        if self.recovered and self.reason_code is not None:
            raise ExperimentValidationError("recovered resources cannot have a reason")
        if not self.recovered and not self.reason_code:
            raise ExperimentValidationError("failed recovery requires a reason code")
        object.__setattr__(
            self, "details", _frozen_mapping("recovery details", self.details)
        )

    @classmethod
    def create(
        cls,
        *,
        recovered: bool,
        checked_at_wall_ms: int,
        reason_code: str | None,
        details: Mapping[str, object],
    ) -> "ResourceRecoveryResult":
        return cls(
            recovered,
            checked_at_wall_ms,
            reason_code,
            _frozen_mapping("recovery details", details),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "recovered": self.recovered,
            "checked_at_wall_ms": self.checked_at_wall_ms,
            "reason_code": self.reason_code,
            "details": thaw(self.details),
        }


class BenchmarkRuntimeClient(Protocol):
    """The complete runtime surface visible to the benchmark orchestrator."""

    async def prepare_trial(self) -> Mapping[str, object]: ...

    async def resource_snapshot(self) -> ResourceSnapshot: ...

    async def submit(
        self,
        workflow: object,
        *,
        inputs: dict[str, object],
        submission_id: str,
        run_deadline_ms: int | None,
    ) -> SubmissionReceipt: ...

    async def wait_terminal(
        self, run_id: str, *, deadline_monotonic_ms: int
    ) -> TerminalRunResult: ...

    async def flush_run(self, run_id: str, *, request_id: str) -> RunFlushResult: ...

    async def cancel_run(self, run_id: str, *, request_id: str) -> None: ...

    async def destroy_run(
        self,
        run_id: str,
        *,
        request_id: str,
        force: bool = False,
    ) -> None: ...

    async def wait_for_recovery(
        self,
        before: ResourceSnapshot,
        *,
        run_ids: tuple[str, ...],
        deadline_monotonic_ms: int,
    ) -> tuple[ResourceSnapshot, ResourceRecoveryResult]: ...

    async def shutdown(self, *, request_id: str) -> Mapping[str, object]: ...

    async def finalize_recovery(
        self,
        before: ResourceSnapshot,
        after: ResourceSnapshot,
        recovery: ResourceRecoveryResult,
        *,
        deadline_monotonic_ms: int,
    ) -> tuple[ResourceSnapshot, ResourceRecoveryResult]: ...


class BenchmarkRuntimeFactory(Protocol):
    analysis_after_each_trial: bool

    async def open(
        self,
        *,
        spec: ExperimentSpec,
        cell: CellSpec,
        trial_attempt_id: str,
        trial_directory: str,
        resume: bool,
    ) -> BenchmarkRuntimeClient: ...
