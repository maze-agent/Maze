"""Default-deny scan for sensitive values in persisted C8 payloads."""

from __future__ import annotations

import re
from typing import Iterable

from ascend_maze.core.canonical import FrozenMap

_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "auth_token",
        "authorization",
        "bearer_token",
        "completion",
        "completion_text",
        "content",
        "file_content",
        "generated_text",
        "messages",
        "password",
        "private_key",
        "prompt",
        "response_text",
        "return_value",
        "secret",
        "task_input",
        "task_output",
        "tensor_content",
    }
)
_FORBIDDEN_KEY_SUFFIXES = (
    "_api_key",
    "_password",
    "_private_key",
    "_secret",
    "_token",
    "_token_value",
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def privacy_violations(value: object) -> tuple[str, ...]:
    violations: list[str] = []
    _scan(value, "payload", violations)
    return tuple(sorted(set(violations)))


def any_privacy_violations(values: Iterable[object]) -> bool:
    return any(privacy_violations(value) for value in values)


def _scan(value: object, path: str, output: list[str]) -> None:
    if isinstance(value, FrozenMap):
        pairs = value.items_tuple()
    elif isinstance(value, dict):
        pairs = tuple(value.items())
    else:
        pairs = ()
    if pairs:
        for key, item in pairs:
            if not isinstance(key, str):
                output.append(f"{path}.<non_string_key>")
                continue
            key_text = str(key)
            normalized = key_text.strip().lower().replace("-", "_")
            child = f"{path}.{key_text}"
            if normalized in _FORBIDDEN_KEYS or normalized.endswith(
                _FORBIDDEN_KEY_SUFFIXES
            ):
                output.append(child)
                continue
            _scan(item, child, output)
        return
    if isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _scan(item, f"{path}[{index}]", output)
        return
    if isinstance(value, str) and any(
        pattern.search(value) is not None for pattern in _SECRET_PATTERNS
    ):
        output.append(path)
    if isinstance(value, bytes) and value:
        output.append(path)
