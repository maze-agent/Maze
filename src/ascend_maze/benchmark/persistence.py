"""Durable canonical JSON persistence for C14 state machines."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import os
from pathlib import Path
import secrets
from typing import cast

from ascend_maze.benchmark.canonical import canonical_json_bytes
from ascend_maze.core.errors import ExperimentValidationError

AtomicWriteFailpoint = Callable[[str, Path], None]


def atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    failpoint: AtomicWriteFailpoint | None = None,
) -> None:
    """Commit one file with file and parent-directory durability barriers."""

    path = path.resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if failpoint is not None:
            failpoint("after_file_fsync", path)
        os.replace(temporary, path)
        if failpoint is not None:
            failpoint("after_replace", path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if failpoint is not None:
            failpoint("after_directory_fsync", path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(
    path: Path,
    payload: object,
    *,
    failpoint: AtomicWriteFailpoint | None = None,
) -> None:
    atomic_write_bytes(
        path,
        canonical_json_bytes(payload) + b"\n",
        failpoint=failpoint,
    )


def load_json_object(path: Path, *, description: str) -> Mapping[str, object]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentValidationError(f"cannot read {description}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExperimentValidationError(f"{description} must be a JSON object")
    return cast(Mapping[str, object], value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExperimentValidationError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result
