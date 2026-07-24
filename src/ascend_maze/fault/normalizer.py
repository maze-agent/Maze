"""Stable, platform-independent normalization of producer error facts."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Mapping

from ascend_maze.contracts.errors import ErrorInfo, STABLE_ERROR_CODES
from ascend_maze.core.canonical import FrozenMap


@dataclass(frozen=True, slots=True)
class FaultIdentity:
    run_id: str
    task_id: str
    attempt: int
    dispatch_id: str
    lease_id: str
    route_lease_id: str | None = None


@dataclass(frozen=True, slots=True)
class ErrorClassification:
    error_code: str
    category: str
    confidence: str
    source: str


_DEFAULT_EXCEPTION_TYPES: Mapping[str, tuple[str, str]] = {
    "MemoryError": ("host_oom", "resource"),
    "OutOfMemoryError": ("npu_oom", "resource"),
    "NPUOutOfMemoryError": ("npu_oom", "resource"),
}

_FALLBACK_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"^NPU out of memory(?:\.|$)", re.IGNORECASE),
        "npu_oom",
        "resource",
    ),
)

_SENSITIVE_FIELD = re.compile(
    r"(?i)\b(authorization|api[_-]?key|password|secret|token)\b"
    r"\s*[:=]\s*[^\s,;]+(?:\s+[^\s,;]+)?"
)


class ErrorNormalizer:
    """Prefer structured type/code mappings and expose string fallback use."""

    def __init__(
        self,
        *,
        exception_types: Mapping[str, tuple[str, str]] | None = None,
        platform_error_codes: Mapping[str, tuple[str, str]] | None = None,
        max_message_chars: int = 2_048,
    ) -> None:
        if max_message_chars < 64:
            raise ValueError("max_message_chars must be at least 64")
        self._exception_types = dict(_DEFAULT_EXCEPTION_TYPES)
        if exception_types is not None:
            self._exception_types.update(exception_types)
        self._platform_error_codes = dict(platform_error_codes or {})
        for code, _ in tuple(self._exception_types.items()) + tuple(
            self._platform_error_codes.items()
        ):
            if not isinstance(code, str) or not code:
                raise ValueError("normalizer mapping keys must be non-empty strings")
        for error_code, category in tuple(self._exception_types.values()) + tuple(
            self._platform_error_codes.values()
        ):
            if error_code not in STABLE_ERROR_CODES or not category:
                raise ValueError("normalizer mappings require a stable error code/category")
        self.max_message_chars = max_message_chars

    def classify(
        self,
        *,
        producer_error_code: str | None,
        producer_category: str | None,
        exception_type: str | None,
        platform_error_code: str | None,
        message: str,
    ) -> ErrorClassification:
        if exception_type is not None and exception_type in self._exception_types:
            code, category = self._exception_types[exception_type]
            return ErrorClassification(code, category, "mapped", "exception_type")
        if (
            platform_error_code is not None
            and platform_error_code in self._platform_error_codes
        ):
            code, category = self._platform_error_codes[platform_error_code]
            return ErrorClassification(code, category, "mapped", "platform_error_code")
        if producer_error_code in STABLE_ERROR_CODES and producer_error_code != "unknown_error":
            return ErrorClassification(
                producer_error_code,
                producer_category or "control",
                "exact",
                "producer",
            )
        for pattern, code, category in _FALLBACK_RULES:
            if pattern.search(message):
                return ErrorClassification(code, category, "fallback", "message")
        return ErrorClassification("unknown_error", "control", "exact", "unknown")

    def normalize(self, error: ErrorInfo, *, identity: FaultIdentity) -> ErrorInfo:
        mismatches = self._identity_mismatches(error, identity)
        message = self._sanitize_message(error.message)
        if mismatches:
            return ErrorInfo(
                schema_version=1,
                error_code="backend_internal_error",
                category="control",
                origin="control",
                message="runtime error identity did not match the active Attempt",
                retryable_hint=False,
                classification_confidence="exact",
                execution_phase="cleanup",
                run_id=identity.run_id,
                task_id=identity.task_id,
                attempt=identity.attempt,
                dispatch_id=identity.dispatch_id,
                lease_id=identity.lease_id,
                route_lease_id=identity.route_lease_id,
                occurred_at_ms=error.occurred_at_ms,
                details=FrozenMap(
                    (
                        ("identity_mismatches", tuple(mismatches)),
                        ("producer_error_code", error.error_code),
                    )
                ),
            )
        classification = self.classify(
            producer_error_code=error.error_code,
            producer_category=error.category,
            exception_type=error.exception_type,
            platform_error_code=error.platform_error_code,
            message=message,
        )
        details = dict(error.details.items_tuple())
        details["classification_source"] = classification.source
        return replace(
            error,
            error_code=classification.error_code,
            category=classification.category,
            message=message,
            retryable_hint=(
                error.retryable_hint
                and classification.error_code not in {"unknown_error", "user_code_failed"}
            ),
            classification_confidence=(
                error.classification_confidence
                if classification.source == "producer"
                else classification.confidence
            ),
            details=FrozenMap(tuple(sorted(details.items()))),
        )

    def _sanitize_message(self, message: str) -> str:
        single_line = " ".join(message.replace("\x00", "").splitlines())
        redacted = _SENSITIVE_FIELD.sub(
            lambda match: f"{match.group(1)}=<redacted>",
            single_line,
        )
        return redacted[: self.max_message_chars]

    @staticmethod
    def _identity_mismatches(
        error: ErrorInfo,
        identity: FaultIdentity,
    ) -> tuple[str, ...]:
        mismatches: list[str] = []
        for name in ("run_id", "task_id", "attempt"):
            if getattr(error, name) != getattr(identity, name):
                mismatches.append(name)
        for name in ("dispatch_id", "lease_id", "route_lease_id"):
            producer = getattr(error, name)
            expected = getattr(identity, name)
            if producer is not None and producer != expected:
                mismatches.append(name)
        return tuple(mismatches)
