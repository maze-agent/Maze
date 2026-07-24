"""Phase-one task decorator."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import math
from typing import Callable, Mapping, TypeVar, overload

from ascend_maze.compiler.analyzer import AnalyzedCallable, analyse_callable
from ascend_maze.contracts.errors import DEFAULT_RETRY_ON, STABLE_ERROR_CODES
from ascend_maze.contracts.resources import ResourceDeclaration
from ascend_maze.core.errors import ContractValidationError, TaskDefinitionError

F = TypeVar("F", bound=Callable[..., object])
_TASK_TEMPLATE_ATTRIBUTE = "__ascend_maze_task_template__"


def _milliseconds(
    name: str,
    value: float | int | None,
    *,
    allow_zero: bool,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TaskDefinitionError(f"{name} must be a number")
    if not math.isfinite(float(value)):
        raise TaskDefinitionError(f"{name} must be finite")
    milliseconds = int(round(float(value) * 1000))
    minimum = 0 if allow_zero else 1
    if milliseconds < minimum:
        comparator = "non-negative" if allow_zero else "positive"
        raise TaskDefinitionError(f"{name} must be {comparator}")
    return milliseconds


@dataclass(frozen=True, slots=True)
class TaskTemplate:
    func: Callable[..., object]
    analysis: AnalyzedCallable
    resource_declaration: ResourceDeclaration
    declared_task_kind: str | None
    timeout_ms: int | None
    max_retries: int
    retry_backoff_ms: int
    retry_on: tuple[str, ...]


def _decorate(
    func: F,
    *,
    resources: Mapping[str, object] | None,
    task_kind: str | None,
    timeout_seconds: float | int | None,
    max_retries: int,
    retry_backoff_seconds: float | int,
    retry_on: tuple[str, ...] | list[str] | None,
) -> F:
    try:
        declaration = ResourceDeclaration.from_public(resources)
    except ContractValidationError as exc:
        raise TaskDefinitionError(str(exc)) from exc
    if task_kind is not None and not isinstance(task_kind, str):
        raise TaskDefinitionError("task_kind must be a string or None")
    normalized_kind = None if task_kind is None else task_kind.strip().lower()
    if normalized_kind not in {None, "cpu", "npu", "io"}:
        raise TaskDefinitionError("task_kind must be one of: cpu, npu, io")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise TaskDefinitionError("max_retries must be a non-negative integer")
    if retry_on is not None and not isinstance(retry_on, (tuple, list)):
        raise TaskDefinitionError("retry_on must be a tuple or list of stable error codes")
    requested_retry_on = DEFAULT_RETRY_ON if retry_on is None else tuple(retry_on)
    if any(not isinstance(item, str) or not item for item in requested_retry_on):
        raise TaskDefinitionError("retry_on values must be non-empty strings")
    unknown_retry_codes = sorted(set(requested_retry_on) - STABLE_ERROR_CODES)
    if unknown_retry_codes:
        raise TaskDefinitionError(
            "retry_on contains unknown stable error codes: "
            + ", ".join(unknown_retry_codes)
        )
    normalized_retry_on = tuple(sorted(set(requested_retry_on)))
    timeout_ms = _milliseconds(
        "timeout_seconds", timeout_seconds, allow_zero=False
    )
    backoff_ms = _milliseconds(
        "retry_backoff_seconds", retry_backoff_seconds, allow_zero=True
    )
    assert backoff_ms is not None
    if hasattr(func, _TASK_TEMPLATE_ATTRIBUTE):
        raise TaskDefinitionError("function is already decorated with @task")
    analysis = analyse_callable(func)
    template = TaskTemplate(
        func=func,
        analysis=analysis,
        resource_declaration=declaration,
        declared_task_kind=normalized_kind,
        timeout_ms=timeout_ms,
        max_retries=max_retries,
        retry_backoff_ms=backoff_ms,
        retry_on=normalized_retry_on,
    )
    setattr(func, _TASK_TEMPLATE_ATTRIBUTE, template)
    return func


@overload
def task(func: F) -> F: ...


@overload
def task(
    func: None = None,
    *,
    resources: Mapping[str, object] | None = None,
    task_kind: str | None = None,
    timeout_seconds: float | int | None = None,
    max_retries: int = 1,
    retry_backoff_seconds: float | int = 0,
    retry_on: tuple[str, ...] | list[str] | None = None,
) -> Callable[[F], F]: ...


def task(
    func: F | None = None,
    *,
    resources: Mapping[str, object] | None = None,
    task_kind: str | None = None,
    timeout_seconds: float | int | None = None,
    max_retries: int = 1,
    retry_backoff_seconds: float | int = 0,
    retry_on: tuple[str, ...] | list[str] | None = None,
) -> F | Callable[[F], F]:
    """Validate and mark a synchronous Python function as an Ascend-Maze task."""

    def decorator(inner: F) -> F:
        return _decorate(
            inner,
            resources=resources,
            task_kind=task_kind,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            retry_on=retry_on,
        )

    if func is not None:
        return decorator(func)
    return decorator


def get_task_template(func: object) -> TaskTemplate:
    if not inspect.isfunction(func):
        raise TaskDefinitionError("workflow tasks must be @task-decorated functions")
    template = getattr(func, _TASK_TEMPLATE_ATTRIBUTE, None)
    if not isinstance(template, TaskTemplate):
        raise TaskDefinitionError("function is not decorated with @task")
    return template
