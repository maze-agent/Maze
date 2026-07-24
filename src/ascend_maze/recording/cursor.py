"""Signed opaque cursor for committed historical event pages."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
import hashlib
import hmac
import re

from ascend_maze.core.canonical import (
    FrozenMap,
    canonical_bytes,
    decode_canonical_bytes,
)
from ascend_maze.core.errors import ContractValidationError

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class CursorPosition:
    run_id: str
    manifest_digest: str
    file_index: int
    row_index: int

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ContractValidationError("cursor run_id is required")
        if not _SHA256_RE.fullmatch(self.manifest_digest):
            raise ContractValidationError("cursor manifest digest is invalid")
        for name in ("file_index", "row_index"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"cursor {name} must be non-negative")


class CursorCodec:
    def __init__(self, signing_key: bytes) -> None:
        if not isinstance(signing_key, bytes) or len(signing_key) < 16:
            raise ValueError("cursor signing key must contain at least 16 bytes")
        self._signing_key = signing_key

    def encode(self, position: CursorPosition) -> str:
        payload = canonical_bytes(
            {
                "schema_version": 1,
                "run_id": position.run_id,
                "manifest_digest": position.manifest_digest,
                "file_index": position.file_index,
                "row_index": position.row_index,
            }
        )
        signature = hmac.new(self._signing_key, payload, hashlib.sha256).digest()
        return f"{self._b64(payload)}.{self._b64(signature)}"

    def decode(self, token: str) -> CursorPosition:
        if not isinstance(token, str) or not token:
            raise ContractValidationError("cursor token is required")
        try:
            payload_text, signature_text = token.split(".", 1)
            payload = self._unb64(payload_text)
            signature = self._unb64(signature_text)
        except (TypeError, ValueError) as exc:
            raise ContractValidationError("invalid cursor token") from exc
        expected = hmac.new(self._signing_key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ContractValidationError("cursor signature mismatch")
        value = decode_canonical_bytes(payload)
        if not isinstance(value, FrozenMap):
            raise ContractValidationError("cursor payload must be a mapping")
        try:
            schema_version = value["schema_version"]
            run_id = value["run_id"]
            manifest_digest = value["manifest_digest"]
            file_index = value["file_index"]
            row_index = value["row_index"]
            if not isinstance(run_id, str) or not isinstance(manifest_digest, str):
                raise TypeError("cursor identities must be strings")
            if (
                isinstance(file_index, bool)
                or not isinstance(file_index, int)
                or isinstance(row_index, bool)
                or not isinstance(row_index, int)
            ):
                raise TypeError("cursor positions must be integers")
            position = CursorPosition(
                run_id=run_id,
                manifest_digest=manifest_digest,
                file_index=file_index,
                row_index=row_index,
            )
        except (KeyError, TypeError) as exc:
            raise ContractValidationError("cursor payload is incomplete") from exc
        if schema_version != 1:
            raise ContractValidationError("unsupported cursor schema version")
        return position

    @staticmethod
    def _b64(value: bytes) -> str:
        return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _unb64(value: str) -> bytes:
        return urlsafe_b64decode(value + "=" * (-len(value) % 4))
