"""Shared CodePackage validation used by local and distributed Workers."""

from __future__ import annotations

from collections.abc import Callable
import importlib

from ascend_maze.compiler.analyzer import analyse_callable
from ascend_maze.contracts.runtime import CodePackage
from ascend_maze.core.errors import ContractValidationError
from ascend_maze.runtime.serialization import deserialize_callable


def load_code_package(package: CodePackage) -> Callable[..., object]:
    if "<locals>" not in package.qualname:
        try:
            module = importlib.import_module(package.module)
            value: object = module
            for part in package.qualname.split("."):
                value = getattr(value, part)
            if callable(value):
                validate_loaded_callable(value, package)
                return value
        except (ImportError, AttributeError, ContractValidationError):
            pass
    if package.serialized_fallback is None:
        raise ContractValidationError(
            "callable is not importable and CodePackage has no fallback"
        )
    value = deserialize_callable(package.serialized_fallback)
    if not callable(value):
        raise ContractValidationError("serialized fallback is not callable")
    validate_loaded_callable(value, package)
    return value


def validate_loaded_callable(
    func: Callable[..., object], package: CodePackage
) -> None:
    analysis = analyse_callable(func)
    if analysis.code_hash != package.code_hash:
        raise ContractValidationError("loaded callable code hash mismatch")
