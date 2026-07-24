"""Controller lifecycle and structured shutdown results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from ascend_maze.contracts.recording import FlushResult
from ascend_maze.core.canonical import CanonicalValue, FrozenMap, freeze_canonical
from ascend_maze.core.errors import ContractValidationError


class ControllerLifecycleState(str, Enum):
    CREATED = "created"
    READY = "ready"
    DRAINING = "draining"
    STOPPED = "stopped"


class ShutdownMode(str, Enum):
    GRACEFUL = "graceful"
    FORCE = "force"


class NodeAction(str, Enum):
    DRAIN = "drain"
    RESUME = "resume"


@dataclass(frozen=True, slots=True)
class ShutdownResource:
    kind: str
    resource_id: str
    state: str
    node_id: str | None = None
    details: Mapping[CanonicalValue, CanonicalValue] = field(
        default_factory=FrozenMap
    )

    def __post_init__(self) -> None:
        for name in ("kind", "resource_id", "state"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractValidationError(f"{name} is required")
        frozen = freeze_canonical(self.details)
        if not isinstance(frozen, FrozenMap):
            raise ContractValidationError("shutdown resource details must be a mapping")
        object.__setattr__(self, "details", frozen)


@dataclass(frozen=True, slots=True)
class NodeActionResult:
    action: NodeAction
    node_id: str
    boot_id: str
    status: str
    started_at_ms: int
    finished_at_ms: int
    forced: bool
    timed_out: bool
    cleanup_confirmed: bool
    cancelled_run_ids: tuple[str, ...]
    incomplete_resources: tuple[ShutdownResource, ...]
    errors: tuple[str, ...]
    exit_code: int

    def __post_init__(self) -> None:
        for name in ("node_id", "boot_id", "status"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractValidationError(f"{name} is required")
        if self.finished_at_ms < self.started_at_ms:
            raise ContractValidationError("node action finish time precedes start time")
        expected_cleanup = (
            not self.incomplete_resources and not self.timed_out and not self.errors
        )
        if self.cleanup_confirmed != expected_cleanup:
            raise ContractValidationError(
                "cleanup_confirmed does not match node action evidence"
            )
        expected_exit = 0 if expected_cleanup else 1
        if self.exit_code != expected_exit:
            raise ContractValidationError("node action exit_code does not match evidence")


@dataclass(frozen=True, slots=True)
class ShutdownResult:
    mode: ShutdownMode
    lifecycle_state: ControllerLifecycleState
    started_at_ms: int
    finished_at_ms: int
    active_run_ids_at_start: tuple[str, ...]
    drained_run_ids: tuple[str, ...]
    terminated_run_ids: tuple[str, ...]
    recording_run_ids: tuple[str, ...]
    flush_results: tuple[FlushResult, ...]
    incomplete_resources: tuple[ShutdownResource, ...]
    steps: tuple[str, ...]
    errors: tuple[str, ...]
    recording_complete: bool
    cleanup_confirmed: bool
    exit_code: int

    def __post_init__(self) -> None:
        if self.finished_at_ms < self.started_at_ms:
            raise ContractValidationError("shutdown finish time precedes start time")
        expected_cleanup = not self.incomplete_resources and not self.errors
        if self.cleanup_confirmed != expected_cleanup:
            raise ContractValidationError("cleanup_confirmed does not match shutdown evidence")
        expected_recording = (
            {item.run_id for item in self.flush_results} == set(self.recording_run_ids)
            and all(item.recording_complete for item in self.flush_results)
            and not any(error.startswith("recorder_") for error in self.errors)
        )
        if self.recording_complete != expected_recording:
            raise ContractValidationError("recording_complete does not match FlushResults")
        expected_exit = 0 if expected_cleanup and expected_recording else 1
        if self.exit_code != expected_exit:
            raise ContractValidationError("shutdown exit_code does not match cleanup evidence")
