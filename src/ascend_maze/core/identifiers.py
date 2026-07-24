"""Stable and runtime identifier helpers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import uuid

from ascend_maze.core.errors import ContractValidationError

_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_prefix(prefix: str) -> None:
    if not isinstance(prefix, str) or not _PREFIX_RE.fullmatch(prefix):
        raise ContractValidationError(
            "identifier prefix must match [a-z][a-z0-9_]*"
        )


def new_id(prefix: str) -> str:
    """Create an opaque runtime ID. Never use this for compiled identities."""

    _validate_prefix(prefix)
    return f"{prefix}_{uuid.uuid4().hex}"


def stable_id(prefix: str, *components: str, digest_length: int = 24) -> str:
    """Create a deterministic ID from explicitly ordered UTF-8 components."""

    _validate_prefix(prefix)
    if not 8 <= digest_length <= 64:
        raise ContractValidationError("digest_length must be between 8 and 64")
    digest = hashlib.sha256()
    for component in components:
        if not isinstance(component, str):
            raise ContractValidationError("stable ID components must be strings")
        encoded = component.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"{prefix}_{digest.hexdigest()[:digest_length]}"


@dataclass(frozen=True, slots=True)
class GenerationRef:
    """Identity of state owned by one controller and local generation."""

    controller_generation: str
    generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.controller_generation, str) or not self.controller_generation:
            raise ContractValidationError("controller_generation is required")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ContractValidationError("generation must be a non-negative integer")
