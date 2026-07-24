"""Canonical JSON and deterministic identities for the offline benchmark kernel."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import unicodedata

from ascend_maze.core.canonical import FrozenMap
from ascend_maze.core.errors import ExperimentValidationError


def json_value(value: object) -> object:
    """Convert supported immutable values to normalized JSON-compatible values."""

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExperimentValidationError("canonical JSON rejects non-finite floats")
        return value
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        try:
            normalized.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ExperimentValidationError(
                "canonical JSON strings must be valid UTF-8"
            ) from exc
        return normalized
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExperimentValidationError(
                    "canonical JSON object keys must be strings"
                )
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in result:
                raise ExperimentValidationError(
                    f"duplicate normalized JSON key: {normalized_key}"
                )
            result[normalized_key] = json_value(item)
        return result
    raise ExperimentValidationError(
        f"unsupported canonical JSON value: {type(value).__name__}"
    )


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise ExperimentValidationError("failed to encode canonical JSON") from exc


def canonical_json_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def stable_payload_id(prefix: str, value: object, *, length: int = 32) -> str:
    if not prefix or not prefix.replace("_", "a").isalnum() or not prefix[0].isalpha():
        raise ExperimentValidationError("identity prefix must be alphanumeric")
    if length < 8 or length > 64:
        raise ExperimentValidationError("identity digest length must be 8..64")
    return f"{prefix}_{canonical_json_digest(value)[:length]}"


def derive_seed(*components: object) -> int:
    """Derive a stable non-negative signed-64-bit seed from named components."""

    digest = hashlib.sha256(canonical_json_bytes(components)).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def thaw(value: object) -> object:
    """Return plain immutable-container content without changing scalar types."""

    if isinstance(value, FrozenMap):
        result: dict[object, object] = {}
        for key, item in value.items_tuple():
            result[thaw(key)] = thaw(item)
        return result
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value
