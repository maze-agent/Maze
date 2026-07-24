"""Create transport-neutral code packages from deterministic definitions."""

from __future__ import annotations

import importlib
from typing import Callable, Mapping

from ascend_maze.compiler.ir import CompiledWorkflow
from ascend_maze.contracts.runtime import CodePackage
from ascend_maze.runtime.serialization import serialize_callable


def build_code_packages(
    compiled: CompiledWorkflow,
    *,
    environment_fingerprint: str,
    callables_by_definition: Mapping[str, Callable[..., object]] | None = None,
) -> tuple[CodePackage, ...]:
    callables = callables_by_definition or {}
    return tuple(
        CodePackage.create(
            definition_id=definition.definition_id,
            code_hash=definition.code_hash,
            module=definition.module,
            qualname=definition.qualname,
            serialized_fallback=_serialized_fallback(
                definition.module,
                definition.qualname,
                callables.get(definition.definition_id),
            ),
            environment_fingerprint=environment_fingerprint,
        )
        for _, definition in compiled.definitions.items_tuple()
    )


def _serialized_fallback(
    module_name: str,
    qualname: str,
    func: Callable[..., object] | None,
) -> bytes | None:
    if func is None:
        return None
    importable: object | None = None
    if "<locals>" not in qualname:
        try:
            importable = importlib.import_module(module_name)
            for part in qualname.split("."):
                importable = getattr(importable, part)
        except (ImportError, AttributeError):
            importable = None
    if importable is func:
        return None
    return serialize_callable(func)
