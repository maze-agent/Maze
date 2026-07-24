"""Structured error envelope shared by runtime-independent components."""

from __future__ import annotations

from dataclasses import dataclass, field

from ascend_maze.core.canonical import CanonicalValue, FrozenMap, freeze_canonical
from ascend_maze.core.errors import ContractValidationError

STABLE_ERROR_CODES = frozenset(
    {
        "backend_internal_error",
        "code_delivery_failed",
        "data_binding_failed",
        "data_handle_invalid",
        "device_bind_failed",
        "device_unhealthy",
        "environment_mismatch",
        "host_oom",
        "invalid_task_output",
        "model_adapter_config_invalid",
        "model_adapter_unsupported",
        "model_catalog_invalid",
        "model_chat_async_context_unsupported",
        "model_client_cleanup_failed",
        "model_config_mismatch",
        "model_device_binding_mismatch",
        "model_dispatch_failed",
        "model_hbm_unavailable",
        "model_identity_mismatch",
        "model_instance_failed",
        "model_inference_failed",
        "model_inference_timeout",
        "model_metrics_unavailable",
        "model_process_exited",
        "model_protocol_failed",
        "model_route_concurrent_call_forbidden",
        "model_route_context_leaked",
        "model_route_context_missing",
        "model_route_invalidated",
        "model_route_reporting_failed",
        "model_service_timeout",
        "model_service_unavailable",
        "model_startup_timeout",
        "model_warmup_failed",
        "node_offline",
        "npu_async_error",
        "npu_oom",
        "resource_observation_inconsistent",
        "resource_request_unsatisfiable",
        "result_publish_failed",
        "run_cancelled",
        "run_deadline_exceeded",
        "runtime_node_unavailable",
        "runtime_cancel_failed",
        "scheduler_interrupted",
        "serialization_failed",
        "task_cancelled",
        "task_definition_invalid",
        "task_timeout",
        "unknown_error",
        "upstream_failed",
        "user_code_failed",
        "worker_acquire_failed",
        "worker_cleanup_failed",
        "worker_lost",
        "worker_start_failed",
    }
)

DEFAULT_RETRY_ON = (
    "npu_oom",
    "runtime_node_unavailable",
    "worker_acquire_failed",
    "worker_start_failed",
)


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    schema_version: int
    error_code: str
    category: str
    origin: str
    message: str
    retryable_hint: bool
    classification_confidence: str
    execution_phase: str
    run_id: str
    task_id: str
    attempt: int
    dispatch_id: str | None = None
    lease_id: str | None = None
    route_lease_id: str | None = None
    model_instance_id: str | None = None
    node_id: str | None = None
    boot_id: str | None = None
    device_id: str | None = None
    worker_id: str | None = None
    exception_type: str | None = None
    platform_error_code: str | None = None
    occurred_at_ms: int = 0
    details: FrozenMap[CanonicalValue, CanonicalValue] = field(
        default_factory=FrozenMap
    )
    traceback_ref: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version < 1
        ):
            raise ContractValidationError("schema_version must be a positive integer")
        required = (
            "error_code",
            "category",
            "origin",
            "message",
            "classification_confidence",
            "execution_phase",
            "run_id",
            "task_id",
        )
        for name in required:
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractValidationError(f"{name} is required")
        if self.error_code not in STABLE_ERROR_CODES:
            raise ContractValidationError(f"unsupported stable error_code: {self.error_code}")
        if not isinstance(self.retryable_hint, bool):
            raise ContractValidationError("retryable_hint must be a boolean")
        if self.classification_confidence not in {"exact", "mapped", "fallback"}:
            raise ContractValidationError(
                "classification_confidence must be exact, mapped or fallback"
            )
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 0
        ):
            raise ContractValidationError("attempt must be non-negative")
        if (
            isinstance(self.occurred_at_ms, bool)
            or not isinstance(self.occurred_at_ms, int)
            or self.occurred_at_ms < 0
        ):
            raise ContractValidationError("occurred_at_ms must be non-negative")
        frozen = freeze_canonical(self.details)
        if not isinstance(frozen, FrozenMap):
            raise ContractValidationError("details must be a mapping")
        object.__setattr__(self, "details", frozen)
