"""Idempotent retry/fail decisions after explicit resource cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock

from ascend_maze.compiler.ir import TaskDefinition
from ascend_maze.contracts.errors import ErrorInfo
from ascend_maze.core.errors import StateTransitionError
from ascend_maze.core.identifiers import stable_id


class RecoveryAction(str, Enum):
    RETRY = "retry"
    FAIL_TASK = "fail_task"
    CANCEL_RUN = "cancel_run"
    INTERRUPT_RUN = "interrupt_run"


@dataclass(frozen=True, slots=True)
class CleanupBarrier:
    dispatch_invalidated: bool
    worker_released: bool
    unpublished_data_released: bool
    route_released: bool
    placement_released: bool
    node_or_device_quarantined: bool = False

    @property
    def confirmed(self) -> bool:
        return (
            self.dispatch_invalidated
            and self.worker_released
            and self.unpublished_data_released
            and self.route_released
            and self.placement_released
        )

    @property
    def quarantined(self) -> bool:
        return (
            self.dispatch_invalidated
            and self.unpublished_data_released
            and self.route_released
            and self.node_or_device_quarantined
        )

    @property
    def satisfied(self) -> bool:
        return self.confirmed or self.quarantined

    @classmethod
    def confirmed_cleanup(cls) -> "CleanupBarrier":
        return cls(True, True, True, True, True)


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    decision_id: str
    run_id: str
    task_id: str
    attempt: int
    error: ErrorInfo
    action: RecoveryAction
    reason: str
    next_attempt: int | None
    eligible_at_ms: int | None
    reanchor_required: bool
    avoid_node_id: str | None
    avoid_device_id: str | None
    cleanup_requirements: tuple[str, ...]
    retry_budget_before: int
    retry_budget_after: int


_PERMANENT_ERRORS = frozenset(
    {
        "data_binding_failed",
        "data_handle_invalid",
        "environment_mismatch",
        "invalid_task_output",
        "model_catalog_invalid",
        "model_protocol_failed",
        "resource_request_unsatisfiable",
        "serialization_failed",
        "task_definition_invalid",
        "unknown_error",
        "user_code_failed",
    }
)


class RecoveryPolicy:
    """Return one stable decision for each failed run/task/attempt identity."""

    def __init__(self) -> None:
        self._decisions: dict[tuple[str, str, int], RecoveryDecision] = {}
        self._lock = RLock()

    @staticmethod
    def retry_precondition_reason(
        *,
        definition: TaskDefinition,
        error: ErrorInfo,
        attempt_count: int,
        cleanup: CleanupBarrier,
        now_ms: int,
        run_deadline_at_ms: int | None,
    ) -> str | None:
        retries_used = max(0, attempt_count - 1)
        if not cleanup.satisfied:
            return "cleanup_barrier_incomplete"
        if error.error_code in _PERMANENT_ERRORS:
            return "permanent_error"
        if error.error_code not in definition.retry_on:
            return "error_not_in_retry_on"
        if definition.max_retries - retries_used <= 0:
            return "retry_budget_exhausted"
        if run_deadline_at_ms is not None and now_ms >= run_deadline_at_ms:
            return "run_deadline_exhausted"
        return None

    def decide(
        self,
        *,
        definition: TaskDefinition,
        error: ErrorInfo,
        attempt_count: int,
        cleanup: CleanupBarrier,
        now_ms: int,
        run_deadline_at_ms: int | None,
        retry_block_reason: str | None = None,
    ) -> RecoveryDecision:
        key = (error.run_id, error.task_id, error.attempt)
        with self._lock:
            existing = self._decisions.get(key)
            if existing is not None:
                if existing.error != error:
                    raise StateTransitionError(
                        "one attempt produced conflicting recovery errors"
                    )
                return existing

            retries_used = max(0, attempt_count - 1)
            budget_before = max(0, definition.max_retries - retries_used)
            cleanup_requirements = (
                "dispatch_invalidated",
                "worker_released_or_quarantined",
                "unpublished_data_released",
                "route_released",
                "placement_released_or_invalidated",
            )
            action = RecoveryAction.RETRY
            reason = "retry_eligible"
            precondition_reason: str | None = None
            if retry_block_reason is not None:
                action = RecoveryAction.FAIL_TASK
                reason = retry_block_reason
            else:
                precondition_reason = self.retry_precondition_reason(
                    definition=definition,
                    error=error,
                    attempt_count=attempt_count,
                    cleanup=cleanup,
                    now_ms=now_ms,
                    run_deadline_at_ms=run_deadline_at_ms,
                )
            if retry_block_reason is None and precondition_reason is not None:
                action = RecoveryAction.FAIL_TASK
                reason = precondition_reason

            retry = action is RecoveryAction.RETRY
            eligible_at = now_ms + definition.retry_backoff_ms if retry else None
            decision = RecoveryDecision(
                decision_id=stable_id(
                    "decision",
                    error.run_id,
                    error.task_id,
                    str(error.attempt),
                    error.error_code,
                ),
                run_id=error.run_id,
                task_id=error.task_id,
                attempt=error.attempt,
                error=error,
                action=action,
                reason=reason,
                next_attempt=attempt_count + 1 if retry else None,
                eligible_at_ms=eligible_at,
                reanchor_required=retry and error.error_code == "npu_oom",
                avoid_node_id=(
                    error.node_id
                    if retry
                    and error.error_code
                    in {
                        "device_unhealthy",
                        "node_offline",
                        "npu_async_error",
                        "runtime_node_unavailable",
                        "worker_lost",
                    }
                    else None
                ),
                avoid_device_id=(
                    error.device_id
                    if retry
                    and error.error_code
                    in {"device_unhealthy", "npu_async_error"}
                    else None
                ),
                cleanup_requirements=cleanup_requirements,
                retry_budget_before=budget_before,
                retry_budget_after=budget_before - 1 if retry else budget_before,
            )
            self._decisions[key] = decision
            return decision

    def decision(
        self,
        run_id: str,
        task_id: str,
        attempt: int,
    ) -> RecoveryDecision | None:
        with self._lock:
            return self._decisions.get((run_id, task_id, attempt))

    def count_for_run(self, run_id: str) -> int:
        with self._lock:
            return sum(key[0] == run_id for key in self._decisions)

    def destroy_run(self, run_id: str) -> int:
        with self._lock:
            keys = [key for key in self._decisions if key[0] == run_id]
            for key in keys:
                del self._decisions[key]
            return len(keys)
