"""Opaque data identity and DataStore protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Protocol, runtime_checkable

from ascend_maze.core.canonical import CanonicalValue, FrozenMap, freeze_canonical
from ascend_maze.core.errors import ContractValidationError

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _metadata(value: object) -> FrozenMap[CanonicalValue, CanonicalValue]:
    frozen = freeze_canonical(value)
    if not isinstance(frozen, FrozenMap):
        raise ContractValidationError("metadata must be a mapping")
    return frozen


@dataclass(frozen=True, slots=True)
class DataOwner:
    owner_kind: str
    owner_id: str
    owner_generation: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in (self.owner_kind, self.owner_id, self.owner_generation)
        ):
            raise ContractValidationError("data owner identity fields are required")


@dataclass(frozen=True, slots=True)
class DataHandle:
    owner_generation: str
    staged_handle_id: str
    stable_digest: str | None = None
    size_bytes: int | None = None
    metadata: FrozenMap[CanonicalValue, CanonicalValue] = field(
        default_factory=FrozenMap
    )

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in (self.owner_generation, self.staged_handle_id)
        ):
            raise ContractValidationError(
                "owner_generation and staged_handle_id are required"
            )
        if self.stable_digest is not None and (
            not isinstance(self.stable_digest, str)
            or not _SHA256_RE.fullmatch(self.stable_digest)
        ):
            raise ContractValidationError(
                "stable_digest must be a lowercase SHA-256 hex digest"
            )
        if self.size_bytes is not None and (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ContractValidationError("size_bytes must be non-negative")
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    def submission_identity(self) -> tuple[str, ...]:
        if self.stable_digest is not None:
            return ("digest", self.stable_digest)
        return ("handle", self.owner_generation, self.staged_handle_id)


@dataclass(frozen=True, slots=True)
class SharedFileRef:
    canonical_path: str
    content_sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_path, str) or not self.canonical_path:
            raise ContractValidationError("SharedFileRef path must be a string")
        raw_path = Path(self.canonical_path).expanduser()
        if not raw_path.is_absolute():
            raise ContractValidationError("SharedFileRef path must be absolute")
        path = raw_path.resolve(strict=False)
        if (
            not isinstance(self.content_sha256, str)
            or not _SHA256_RE.fullmatch(self.content_sha256)
        ):
            raise ContractValidationError(
                "content_sha256 must be a lowercase SHA-256 hex digest"
            )
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ContractValidationError("size_bytes must be non-negative")
        object.__setattr__(self, "canonical_path", str(path))


def shared_file_metadata(
    file_ref: SharedFileRef,
) -> tuple[tuple[str, CanonicalValue], ...]:
    return (
        ("value_kind", "shared_file"),
        ("shared_file_path", file_ref.canonical_path),
        ("shared_file_sha256", file_ref.content_sha256),
        ("shared_file_size_bytes", file_ref.size_bytes),
    )


def shared_file_from_handle(handle: DataHandle) -> SharedFileRef | None:
    if handle.metadata.get("value_kind") != "shared_file":
        return None
    path = handle.metadata.get("shared_file_path")
    digest = handle.metadata.get("shared_file_sha256")
    size = handle.metadata.get("shared_file_size_bytes")
    return SharedFileRef(path, digest, size)  # type: ignore[arg-type]


@runtime_checkable
class DataStore(Protocol):
    def put_staged(self, value: Any, owner_generation: str) -> DataHandle: ...

    def put_staged_for_submission_input(
        self, value: Any, owner_generation: str
    ) -> DataHandle: ...

    def get(self, handle: DataHandle) -> Any: ...

    def adopt(self, handles: tuple[DataHandle, ...], owner: DataOwner) -> None: ...

    def release(self, handle: DataHandle) -> None: ...

    def release_many(self, handles: tuple[DataHandle, ...]) -> None: ...

    def state_of(self, handle: DataHandle) -> str: ...
