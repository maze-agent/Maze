"""Static validation and output inference for phase-one task callables."""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
import hashlib
import inspect
import textwrap
from types import FunctionType
from typing import Protocol

from ascend_maze.core.canonical import canonical_bytes
from ascend_maze.core.errors import TaskDefinitionError, TaskOutputInferenceError


@dataclass(frozen=True, slots=True)
class AnalyzedCallable:
    module: str
    qualname: str
    input_names: tuple[str, ...]
    output_names: tuple[str, ...]
    source: str
    normalized_ast: str
    code_hash: str
    static_task_kind: str | None
    static_cpu_num: int
    static_io_num: int
    static_signals: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Flow:
    return_key_sets: tuple[tuple[str, ...], ...]
    may_fallthrough: bool


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return None if prefix is None else f"{prefix}.{node.attr}"
    return None


def _static_resource_hints(node: ast.FunctionDef) -> tuple[str | None, int, int, tuple[str, ...]]:
    names: set[str] = set()
    cpu_num = 0
    io_num = 0
    for item in ast.walk(node):
        if isinstance(item, ast.Import):
            names.update(alias.name for alias in item.names)
        elif isinstance(item, ast.ImportFrom) and item.module:
            names.add(item.module)
        elif isinstance(item, ast.Call):
            dotted = _dotted_name(item.func)
            if dotted is not None:
                names.add(dotted)
            for keyword in item.keywords:
                if (
                    keyword.arg in {"max_workers", "n_jobs", "num_workers"}
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, int)
                    and not isinstance(keyword.value.value, bool)
                    and keyword.value.value > 0
                ):
                    cpu_num = max(cpu_num, keyword.value.value)

    npu_prefixes = (
        "acl",
        "torch.npu",
        "torch_npu",
        "mindspore.runtime",
        "mindspore.set_context",
    )
    io_prefixes = (
        "aiofiles",
        "httpx",
        "requests",
        "socket",
        "urllib",
    )
    npu_signals = sorted(
        name for name in names if any(name.startswith(prefix) for prefix in npu_prefixes)
    )
    io_signals = sorted(
        name for name in names if any(name.startswith(prefix) for prefix in io_prefixes)
    )
    if npu_signals:
        kind = "npu"
    elif io_signals:
        kind = "io"
        io_num = max(io_num, 1)
    else:
        kind = None
    signals = tuple(f"npu:{name}" for name in npu_signals) + tuple(
        f"io:{name}" for name in io_signals
    )
    if cpu_num:
        signals += (f"cpu_workers:{cpu_num}",)
    return kind, cpu_num, io_num, signals


class _TryStatement(Protocol):
    body: list[ast.stmt]
    handlers: list[ast.ExceptHandler]
    orelse: list[ast.stmt]
    finalbody: list[ast.stmt]


def _find_function_node(source: str, name: str) -> ast.FunctionDef:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise TaskDefinitionError(f"cannot parse task source for {name}: {exc}") from exc
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            raise TaskDefinitionError("async task functions are not supported")
    raise TaskDefinitionError(f"cannot find function body for task {name}")


def _dict_keys(node: ast.AST, task_name: str) -> tuple[str, ...]:
    if not isinstance(node, ast.Dict):
        raise TaskOutputInferenceError(
            f"task {task_name} must directly return a dict literal"
        )
    keys: list[str] = []
    for key_node in node.keys:
        if key_node is None:
            raise TaskOutputInferenceError(
                f"task {task_name} cannot use dict unpacking in its return value"
            )
        if not (
            isinstance(key_node, ast.Constant)
            and isinstance(key_node.value, str)
        ):
            raise TaskOutputInferenceError(
                f"task {task_name} return keys must be static strings"
            )
        keys.append(key_node.value)
    if len(keys) != len(set(keys)):
        raise TaskOutputInferenceError(
            f"task {task_name} return dict contains duplicate keys"
        )
    return tuple(sorted(keys))


def _analyse_block(statements: list[ast.stmt], task_name: str) -> _Flow:
    returns: list[tuple[str, ...]] = []
    active = True
    for statement in statements:
        if not active:
            break
        flow = _analyse_statement(statement, task_name)
        returns.extend(flow.return_key_sets)
        active = flow.may_fallthrough
    return _Flow(tuple(returns), active)


def _analyse_try(statement: _TryStatement, task_name: str) -> _Flow:
    body = _analyse_block(statement.body, task_name)
    returns = list(body.return_key_sets)

    if body.may_fallthrough:
        otherwise = _analyse_block(statement.orelse, task_name)
        returns.extend(otherwise.return_key_sets)
        may_fallthrough = otherwise.may_fallthrough
    else:
        may_fallthrough = False

    for handler in statement.handlers:
        handled = _analyse_block(handler.body, task_name)
        returns.extend(handled.return_key_sets)
        may_fallthrough = may_fallthrough or handled.may_fallthrough

    if statement.finalbody:
        final = _analyse_block(statement.finalbody, task_name)
        returns.extend(final.return_key_sets)
        if not final.may_fallthrough:
            return _Flow(tuple(final.return_key_sets), False)

    return _Flow(tuple(returns), may_fallthrough)


def _is_unconditional_match_case(case: ast.match_case) -> bool:
    return (
        case.guard is None
        and isinstance(case.pattern, ast.MatchAs)
        and case.pattern.pattern is None
        and case.pattern.name is None
    )


def _analyse_statement(statement: ast.stmt, task_name: str) -> _Flow:
    if isinstance(statement, ast.Return):
        if statement.value is None:
            raise TaskOutputInferenceError(
                f"task {task_name} cannot use a bare return"
            )
        return _Flow((_dict_keys(statement.value, task_name),), False)
    if isinstance(statement, ast.Raise):
        return _Flow((), False)
    if isinstance(statement, ast.If):
        left = _analyse_block(statement.body, task_name)
        right = (
            _analyse_block(statement.orelse, task_name)
            if statement.orelse
            else _Flow((), True)
        )
        return _Flow(
            left.return_key_sets + right.return_key_sets,
            left.may_fallthrough or right.may_fallthrough,
        )
    if isinstance(statement, ast.Match):
        returns: list[tuple[str, ...]] = []
        may_fallthrough = True
        has_unconditional = False
        for case in statement.cases:
            flow = _analyse_block(case.body, task_name)
            returns.extend(flow.return_key_sets)
            if _is_unconditional_match_case(case):
                has_unconditional = True
            may_fallthrough = may_fallthrough or flow.may_fallthrough
        if has_unconditional and all(
            not _analyse_block(case.body, task_name).may_fallthrough
            for case in statement.cases
        ):
            may_fallthrough = False
        return _Flow(tuple(returns), may_fallthrough)
    if isinstance(statement, (ast.For, ast.While)):
        body = _analyse_block(statement.body, task_name)
        otherwise = _analyse_block(statement.orelse, task_name)
        return _Flow(body.return_key_sets + otherwise.return_key_sets, True)
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return _analyse_block(statement.body, task_name)
    if isinstance(statement, ast.Try):
        return _analyse_try(statement, task_name)
    if hasattr(ast, "TryStar") and isinstance(statement, ast.TryStar):
        return _analyse_try(statement, task_name)
    if isinstance(statement, (ast.Break, ast.Continue)):
        return _Flow((), False)
    return _Flow((), True)


def _validate_function_kind(func: object) -> FunctionType:
    if not inspect.isfunction(func):
        raise TaskDefinitionError(
            "phase-one tasks must satisfy inspect.isfunction(); methods, partials "
            "and callable objects are not supported"
        )
    typed = func
    if typed.__name__ == "<lambda>":
        raise TaskDefinitionError("lambda tasks are not supported")
    if inspect.iscoroutinefunction(typed) or inspect.isasyncgenfunction(typed):
        raise TaskDefinitionError("async task functions are not supported")
    if inspect.isgeneratorfunction(typed):
        raise TaskDefinitionError("generator task functions are not supported")
    if typed.__closure__:
        raise TaskDefinitionError("task functions cannot have non-empty closures")
    return typed


def _validate_signature(func: FunctionType) -> tuple[str, ...]:
    names: list[str] = []
    for parameter in inspect.signature(func).parameters.values():
        if parameter.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            raise TaskDefinitionError(
                f"task parameter {parameter.name!r} must be explicitly named"
            )
        names.append(parameter.name)
    return tuple(names)


def _canonical_ast_payload(value: object) -> object:
    if isinstance(value, ast.AST):
        fields: list[tuple[str, object]] = []
        for name in value._fields:
            field_value = getattr(value, name, None)
            if name == "type_params":
                if field_value:
                    raise TaskDefinitionError(
                        "generic task function type parameters are not supported"
                    )
                continue
            fields.append((name, _canonical_ast_payload(field_value)))
        return ("ast", type(value).__name__, tuple(fields))
    if isinstance(value, (list, tuple)):
        return tuple(_canonical_ast_payload(item) for item in value)
    if value is Ellipsis:
        return ("singleton", "Ellipsis")
    if isinstance(value, complex):
        return ("complex", value.real.hex(), value.imag.hex())
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    raise TaskDefinitionError(
        f"task AST contains an unsupported value: {type(value).__name__}"
    )


def analyse_callable(func: object) -> AnalyzedCallable:
    typed = _validate_function_kind(func)
    inputs = _validate_signature(typed)
    try:
        source = textwrap.dedent(inspect.getsource(typed))
    except (OSError, TypeError) as exc:
        raise TaskDefinitionError(
            f"cannot inspect source for task {typed.__qualname__}"
        ) from exc
    node = _find_function_node(source, typed.__name__)
    flow = _analyse_block(node.body, typed.__name__)
    if flow.may_fallthrough:
        raise TaskOutputInferenceError(
            f"task {typed.__name__} has a normal path that can fall through"
        )
    if not flow.return_key_sets:
        raise TaskOutputInferenceError(
            f"task {typed.__name__} has no direct dict return from which to infer outputs"
        )
    expected = flow.return_key_sets[0]
    if any(keys != expected for keys in flow.return_key_sets[1:]):
        raise TaskOutputInferenceError(
            f"task {typed.__name__} returns inconsistent output key sets"
        )

    normalized_node = copy.deepcopy(node)
    normalized_node.decorator_list = []
    normalized_ast = canonical_bytes(
        _canonical_ast_payload(normalized_node)
    ).decode("utf-8")
    module = typed.__module__ or ""
    qualname = typed.__qualname__
    code_hash = hashlib.sha256(
        canonical_bytes(
            {
                "module": module,
                "qualname": qualname,
                "ast": normalized_ast,
            }
        )
    ).hexdigest()
    static_task_kind, static_cpu_num, static_io_num, static_signals = (
        _static_resource_hints(node)
    )
    return AnalyzedCallable(
        module=module,
        qualname=qualname,
        input_names=inputs,
        output_names=expected,
        source=source,
        normalized_ast=normalized_ast,
        code_hash=code_hash,
        static_task_kind=static_task_kind,
        static_cpu_num=static_cpu_num,
        static_io_num=static_io_num,
        static_signals=static_signals,
    )
