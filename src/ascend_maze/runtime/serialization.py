"""Typed boundary around cloudpickle's untyped public module."""

from __future__ import annotations

from importlib import import_module
from typing import Protocol, cast


class _CloudpickleModule(Protocol):
    def dumps(self, value: object) -> bytes: ...

    def loads(self, payload: bytes) -> object: ...


_cloudpickle = cast(_CloudpickleModule, import_module("cloudpickle"))


def serialize_callable(value: object) -> bytes:
    return _cloudpickle.dumps(value)


def deserialize_callable(payload: bytes) -> object:
    return _cloudpickle.loads(payload)
