"""Stable human and JSON output without terminal control sequences."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
import json
from pathlib import Path
import sys
from typing import Mapping, TextIO


def json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"value is not JSON-compatible: {type(value).__name__}")


def emit_json(value: object, *, stream: TextIO | None = None) -> None:
    target = sys.stdout if stream is None else stream
    json.dump(
        json_value(value),
        target,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    target.write("\n")


def emit_error(message: str, *, stream: TextIO | None = None) -> None:
    target = sys.stderr if stream is None else stream
    target.write(f"Ascend-Maze: {message}\n")
