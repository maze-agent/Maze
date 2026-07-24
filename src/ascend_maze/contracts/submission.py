"""Deterministic workflow submission identity contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re

from ascend_maze.contracts.data import DataHandle, SharedFileRef
from ascend_maze.core.canonical import (
    CanonicalValue,
    FrozenMap,
    canonical_digest,
    freeze_canonical,
)
from ascend_maze.core.errors import ContractValidationError

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _require_digest(name: str, value: object) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ContractValidationError(
            f"{name} must be a lowercase SHA-256 hex digest"
        )
    return value


class SubmissionState(str, Enum):
    PREPARING = "preparing"
    COMMITTED = "committed"
    ABORTED = "aborted"


def hash_session_key(session_key: str | None) -> str:
    if session_key is None:
        return canonical_digest(None)
    if not isinstance(session_key, str):
        raise ContractValidationError("session_key must be a string or None")
    return canonical_digest(session_key)


@dataclass(frozen=True, slots=True)
class RunInputIdentity:
    input_name: str
    identity_kind: str
    identity: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.input_name, str)
            or not self.input_name
            or self.identity_kind not in {"literal", "data_handle", "shared_file"}
            or not isinstance(self.identity, tuple)
            or not self.identity
            or any(not isinstance(item, str) or not item for item in self.identity)
        ):
            raise ContractValidationError("input identity fields are required")
        if self.identity_kind == "literal":
            if len(self.identity) != 1:
                raise ContractValidationError("literal identity must contain one digest")
            _require_digest("literal identity", self.identity[0])
        elif self.identity_kind == "data_handle":
            if self.identity[0] == "digest":
                if len(self.identity) != 2:
                    raise ContractValidationError(
                        "digest DataHandle identity has invalid shape"
                    )
                _require_digest("DataHandle identity", self.identity[1])
            elif self.identity[0] == "handle":
                if len(self.identity) != 3:
                    raise ContractValidationError(
                        "staged DataHandle identity has invalid shape"
                    )
            else:
                raise ContractValidationError("unsupported DataHandle identity")
        elif len(self.identity) != 3:
            raise ContractValidationError("SharedFileRef identity has invalid shape")
        else:
            _require_digest("SharedFileRef identity", self.identity[1])
            try:
                size_bytes = int(self.identity[2])
            except ValueError as exc:
                raise ContractValidationError(
                    "SharedFileRef identity size must be non-negative"
                ) from exc
            if size_bytes < 0 or str(size_bytes) != self.identity[2]:
                raise ContractValidationError(
                    "SharedFileRef identity size must be non-negative"
                )

    @classmethod
    def from_small_value(cls, input_name: str, value: object) -> "RunInputIdentity":
        return cls(input_name, "literal", (canonical_digest(value),))

    @classmethod
    def from_data_handle(
        cls, input_name: str, handle: DataHandle
    ) -> "RunInputIdentity":
        return cls(input_name, "data_handle", handle.submission_identity())

    @classmethod
    def from_shared_file(
        cls, input_name: str, file_ref: SharedFileRef
    ) -> "RunInputIdentity":
        return cls(
            input_name,
            "shared_file",
            (
                file_ref.canonical_path,
                file_ref.content_sha256,
                str(file_ref.size_bytes),
            ),
        )


@dataclass(frozen=True, slots=True)
class SubmissionOptions:
    run_deadline_ms: int | None = None
    execution_options: FrozenMap[CanonicalValue, CanonicalValue] = field(
        default_factory=FrozenMap
    )

    def __post_init__(self) -> None:
        if self.run_deadline_ms is not None and (
            isinstance(self.run_deadline_ms, bool)
            or not isinstance(self.run_deadline_ms, int)
            or self.run_deadline_ms <= 0
        ):
            raise ContractValidationError("run_deadline_ms must be positive")
        frozen = freeze_canonical(self.execution_options)
        if not isinstance(frozen, FrozenMap):
            raise ContractValidationError("execution_options must be a mapping")
        object.__setattr__(self, "execution_options", frozen)


def _submission_payload_hash(
    *,
    workflow_fingerprint: str,
    inputs: tuple[RunInputIdentity, ...],
    session_key_hash: str,
    options: SubmissionOptions,
    config_fingerprint: str,
) -> str:
    ordered_inputs = sorted(inputs, key=lambda item: item.input_name)
    return canonical_digest(
        {
            "workflow_fingerprint": workflow_fingerprint,
            "inputs": [
                {
                    "name": item.input_name,
                    "kind": item.identity_kind,
                    "identity": item.identity,
                }
                for item in ordered_inputs
            ],
            "session_key_hash": session_key_hash,
            "run_deadline_ms": options.run_deadline_ms,
            "execution_options": options.execution_options,
            "config_fingerprint": config_fingerprint,
        }
    )


@dataclass(frozen=True, slots=True)
class SubmissionContract:
    submission_id: str
    workflow_fingerprint: str
    input_identities: tuple[RunInputIdentity, ...]
    session_key_hash: str
    options: SubmissionOptions
    config_fingerprint: str
    submission_payload_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.submission_id, str) or not self.submission_id:
            raise ContractValidationError("submission_id is required")
        _require_digest("workflow_fingerprint", self.workflow_fingerprint)
        _require_digest("session_key_hash", self.session_key_hash)
        _require_digest("config_fingerprint", self.config_fingerprint)
        _require_digest("submission_payload_hash", self.submission_payload_hash)
        if not isinstance(self.input_identities, tuple) or any(
            not isinstance(item, RunInputIdentity) for item in self.input_identities
        ):
            raise ContractValidationError(
                "input_identities must be a tuple of RunInputIdentity"
            )
        if not isinstance(self.options, SubmissionOptions):
            raise ContractValidationError("options must be SubmissionOptions")
        names = [item.input_name for item in self.input_identities]
        if len(names) != len(set(names)):
            raise ContractValidationError("run input identities must have unique names")
        ordered = tuple(sorted(self.input_identities, key=lambda item: item.input_name))
        object.__setattr__(self, "input_identities", ordered)
        expected = _submission_payload_hash(
            workflow_fingerprint=self.workflow_fingerprint,
            inputs=ordered,
            session_key_hash=self.session_key_hash,
            options=self.options,
            config_fingerprint=self.config_fingerprint,
        )
        if self.submission_payload_hash != expected:
            raise ContractValidationError("submission_payload_hash does not match payload")

    @classmethod
    def create(
        cls,
        *,
        submission_id: str,
        workflow_fingerprint: str,
        input_identities: tuple[RunInputIdentity, ...],
        session_key_hash: str,
        options: SubmissionOptions,
        config_fingerprint: str,
    ) -> "SubmissionContract":
        if not submission_id or not workflow_fingerprint or not config_fingerprint:
            raise ContractValidationError(
                "submission_id, workflow_fingerprint and config_fingerprint are required"
            )
        names = [item.input_name for item in input_identities]
        if len(names) != len(set(names)):
            raise ContractValidationError("run input identities must have unique names")
        ordered = tuple(sorted(input_identities, key=lambda item: item.input_name))
        payload_hash = _submission_payload_hash(
            workflow_fingerprint=workflow_fingerprint,
            inputs=ordered,
            session_key_hash=session_key_hash,
            options=options,
            config_fingerprint=config_fingerprint,
        )
        return cls(
            submission_id=submission_id,
            workflow_fingerprint=workflow_fingerprint,
            input_identities=ordered,
            session_key_hash=session_key_hash,
            options=options,
            config_fingerprint=config_fingerprint,
            submission_payload_hash=payload_hash,
        )
