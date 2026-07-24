"""Deterministic workflow compiler with cycle-safe lazy public exports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ascend_maze.compiler.compiler import CompileOptions
    from ascend_maze.compiler.ir import CompiledWorkflow

__all__ = ["CompileOptions", "CompiledWorkflow", "compile_workflow"]


def __getattr__(name: str) -> Any:
    if name in {"CompileOptions", "compile_workflow"}:
        from ascend_maze.compiler.compiler import CompileOptions, compile_workflow

        return {
            "CompileOptions": CompileOptions,
            "compile_workflow": compile_workflow,
        }[name]
    if name == "CompiledWorkflow":
        from ascend_maze.compiler.ir import CompiledWorkflow

        return CompiledWorkflow
    raise AttributeError(name)
