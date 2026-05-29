"""
Task decorators for defining task metadata and configuration
"""

import ast
import inspect
import cloudpickle
import base64
import textwrap
from functools import wraps
from typing import Dict, List, Any, Callable, Optional, get_type_hints
from dataclasses import dataclass
from maze.utils.resource_infer import infer_gpu_resources_from_function


@dataclass
class TaskMetadata:
    """Task metadata"""
    func: Callable
    func_name: str
    code_str: str
    code_ser: str  # Serialized function (using cloudpickle)
    inputs: List[str]
    outputs: List[str]
    resources: Dict[str, Any]
    data_types: Dict[str, str]  # Parameter data types
    max_retries: int | None = None
    retry_backoff_seconds: float = 0
    retry_on: List[str] | None = None
    timeout_seconds: float | None = None


class TaskOutputInferenceError(ValueError):
    """Raised when Maze cannot infer output keys from a task return dict."""


def _normalize_resources(resources: Dict[str, Any] = None, func: Callable | None = None) -> Dict[str, Any]:
    """
    Normalize and validate resource configuration
    
    Rules:
    1. Default: cpu=1, cpu_mem=0, gpu=0, gpu_mem=0
    2. Fill missing fields with defaults
    3. If cpu is specified, ensure it's at least 1
    4. If gpu_mem is specified, ensure gpu is at least 1
    
    Args:
        resources: User-provided resource configuration
        
    Returns:
        Dict: Normalized resource configuration
    """
    # Default resource configuration
    default_resources = {
        "cpu": 1,
        "cpu_mem": 0,
        "gpu": 0,
        "gpu_mem": 0
    }
    
    # Start with defaults
    normalized = default_resources.copy()
    explicit_gpu = False
    explicit_gpu_mem = False

    if resources is None:
        resources = {}
    else:
        explicit_gpu = "gpu" in resources
        explicit_gpu_mem = "gpu_mem" in resources
    
    # Update with user-provided values
    for key in ["cpu", "cpu_mem", "gpu", "gpu_mem"]:
        if key in resources:
            normalized[key] = resources[key]

    if func is not None and not explicit_gpu_mem:
        should_infer_gpu = (not explicit_gpu) or normalized["gpu"] > 0
        if should_infer_gpu:
            inferred_gpu = infer_gpu_resources_from_function(func)
            normalized["gpu_mem"] = max(normalized["gpu_mem"], inferred_gpu["gpu_mem"])
            if not explicit_gpu:
                normalized["gpu"] = max(normalized["gpu"], inferred_gpu["gpu"])
    
    # Ensure cpu is at least 1 if specified
    if normalized["cpu"] < 1:
        normalized["cpu"] = 1
    
    # If gpu_mem is specified and > 0, ensure gpu is at least 1
    if normalized["gpu_mem"] > 0 and normalized["gpu"] < 1:
        normalized["gpu"] = 1
    
    return normalized


def _annotation_to_data_type(annotation: Any) -> str:
    if annotation is inspect.Signature.empty:
        return "str"
    if annotation is None:
        return "None"
    if isinstance(annotation, str):
        return annotation

    name = getattr(annotation, "__name__", None)
    if name:
        return name

    return str(annotation).replace("typing.", "")


def _safe_type_hints(func: Callable) -> Dict[str, Any]:
    try:
        return get_type_hints(func)
    except Exception:
        return dict(getattr(func, "__annotations__", {}) or {})


def _get_function_source(func: Callable) -> str:
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):
        return ""

    return textwrap.dedent(source)


def _get_code_str(func: Callable) -> str:
    source = _get_function_source(func)
    if not source:
        return ""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func.__name__:
            lines = source.splitlines(keepends=True)
            return "".join(lines[node.lineno - 1:node.end_lineno])

    return source


def _infer_inputs(func: Callable) -> List[str]:
    signature = inspect.signature(func)
    inputs = []

    for parameter in signature.parameters.values():
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            inputs.append(parameter.name)
        elif parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            raise ValueError(
                f"Task function {func.__name__} cannot use *args or **kwargs. "
                "Use explicit named parameters instead."
            )
        else:
            raise ValueError(
                f"Task function {func.__name__} has positional-only parameter "
                f"{parameter.name!r}, which Maze cannot pass by name."
            )

    return inputs


def _literal_dict_key(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Str):  # pragma: no cover
        return node.s
    return None


def _return_dict_keys(node: ast.AST) -> Optional[List[str]]:
    if not isinstance(node, ast.Dict):
        return None

    keys = []
    for key_node in node.keys:
        if key_node is None:
            return None
        key = _literal_dict_key(key_node)
        if key is None:
            return None
        keys.append(key)

    return keys


def infer_output_keys_from_source(func: Callable) -> List[str]:
    source = _get_function_source(func)
    if not source:
        raise TaskOutputInferenceError(
            f"Cannot inspect source for task {func.__name__}. Return a dict literal with string keys."
        )

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise TaskOutputInferenceError(f"Cannot parse task source for {func.__name__}: {e}") from e

    function_node = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func.__name__:
            function_node = node
            break

    if function_node is None:
        raise TaskOutputInferenceError(f"Cannot find function body for task {func.__name__}")

    output_keys = []
    nodes_to_visit = list(function_node.body)
    while nodes_to_visit:
        node = nodes_to_visit.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue

        if isinstance(node, ast.Return):
            if node.value is None:
                continue
            keys = _return_dict_keys(node.value)
            if keys is None:
                raise TaskOutputInferenceError(
                    f"Task {func.__name__} must return a dict literal with string keys so Maze can infer outputs."
                )
            output_keys.extend(keys)
            continue

        nodes_to_visit.extend(ast.iter_child_nodes(node))

    if not output_keys:
        raise TaskOutputInferenceError(
            f"Task {func.__name__} must return a dict literal with at least one output key."
        )

    deduped = []
    for key in output_keys:
        if key not in deduped:
            deduped.append(key)

    return deduped


def _infer_data_types(func: Callable, inputs: List[str], outputs: List[str], data_types: Dict[str, str] | None = None) -> Dict[str, str]:
    hints = _safe_type_hints(func)
    types_config = {}

    for input_name in inputs:
        types_config[input_name] = _annotation_to_data_type(hints.get(input_name, inspect.Signature.empty))
    for output_name in outputs:
        types_config[output_name] = "any"

    if data_types:
        types_config.update(data_types)

    return types_config


def _make_serialized_task_callable(func: Callable) -> Callable:
    @wraps(func)
    def maze_task_callable(task_input_data: Dict[str, Any] | None = None) -> Dict[str, Any]:
        kwargs = dict(task_input_data or {})
        result = func(**kwargs)
        if not isinstance(result, dict):
            raise TypeError(
                f"Task {func.__name__} must return a dict. "
                f"Got {type(result).__name__} instead."
            )
        return result

    return maze_task_callable


def task(
    func: Callable = None,
    *,
    resources: Dict[str, Any] = None,
    data_types: Dict[str, str] = None,
    max_retries: int | None = None,
    retry_backoff_seconds: float = 0,
    retry_on: List[str] | None = None,
    timeout_seconds: float | None = None,
    **unsupported_options,
):
    """
    Task decorator for marking and configuring task functions.

    Maze tasks are ordinary Python functions. Inputs are inferred from the
    function signature, and outputs are inferred from the string keys in the
    returned dict. The task must return a dict at runtime.
    
    Args:
        resources: Resource requirements configuration, defaults to {"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}
        
    Example:
        @task(resources={"cpu": 1, "cpu_mem": 128})
        def my_task(input_value: str):
            return {"output_value": input_value + " processed"}
    """
    if unsupported_options:
        unsupported = ", ".join(sorted(unsupported_options))
        raise TypeError(
            f"@task no longer accepts {unsupported}. "
            "Define a normal Python function; Maze infers inputs from the "
            "function signature and outputs from the returned dict keys."
        )

    def decorator(func: Callable) -> Callable:
        code_str = _get_code_str(func)
        inputs = _infer_inputs(func)
        outputs = infer_output_keys_from_source(func)

        # Serialize a Maze wrapper so the scheduler can keep passing a dict
        # while user functions receive normal Python keyword arguments.
        task_callable = _make_serialized_task_callable(func)
        code_ser = base64.b64encode(cloudpickle.dumps(task_callable)).decode('utf-8')

        # Normalize and validate resource configuration
        resources_config = _normalize_resources(resources, func)
        types_config = _infer_data_types(func, inputs, outputs, data_types=data_types)

        # Create metadata
        metadata = TaskMetadata(
            func=func,
            func_name=func.__name__,
            code_str=code_str,
            code_ser=code_ser,
            inputs=inputs,
            outputs=outputs,
            resources=resources_config,
            data_types=types_config,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            retry_on=list(retry_on) if retry_on is not None else None,
            timeout_seconds=timeout_seconds,
        )
        
        # Attach metadata to function
        @wraps(func)
        def maze_task_wrapper(*args, **kwargs):
            try:
                from maze.client.maze.workflow_authoring import active_graph_context

                graph_context = active_graph_context()
            except Exception:
                graph_context = None

            if graph_context is not None:
                return graph_context.call_task(maze_task_wrapper, args, kwargs)

            return func(*args, **kwargs)

        maze_task_wrapper._maze_task_metadata = metadata
        
        return maze_task_wrapper
    
    if func is not None:
        if not callable(func):
            raise TypeError(
                "@task no longer accepts inputs/outputs. "
                "Use @task or @task(resources={...}) on a normal Python function."
            )
        return decorator(func)

    return decorator


def get_task_metadata(func: Callable) -> TaskMetadata:
    """
    Get task metadata from function
    
    Args:
        func: Function decorated with @task
        
    Returns:
        TaskMetadata: Task metadata
        
    Raises:
        ValueError: If function is not decorated with @task
    """
    if not hasattr(func, '_maze_task_metadata'):
        raise ValueError(f"Function {func.__name__} is not decorated with @task")
    
    return func._maze_task_metadata
