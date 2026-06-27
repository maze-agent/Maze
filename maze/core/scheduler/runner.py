import ray
import ast
import time
import binascii
import base64
import cloudpickle
from maze.core.files.lineage import ArtifactError, TASK_RESULT_ENVELOPE, run_task_with_file_context
from maze.core.scheduler.error import (
    exception_to_error_envelope,
    make_error_envelope,
    is_task_error_result,
    looks_like_oom,
    task_error_result,
)
from maze.metrics.collector import _drain, _metrics_scope

# Special key used by users in their `return {...}` to attach metrics
# without calling metrics.report(). The framework strips this key before
# the result reaches downstream tasks.
USER_METRICS_RETURN_KEY = "__maze_metrics__"
INVOCATION_ERROR_REASONS = {
    "json_parse_failed",
    "schema_mismatch",
    "max_tokens_insufficient",
    "tool_invocation_args_missing",
}


def _invocation_error_result(reason: str, message: str, *, details: dict | None = None):
    return task_error_result(
        make_error_envelope(
            "invocation_error",
            message,
            retryable=True,
            origin="invocation",
        )
        | {
            "invocation_reason": reason,
            "details": details or {},
        }
    )


def _merge_metrics(*dicts):
    merged = {}
    for d in dicts:
        if not d:
            continue
        for key, value in d.items():
            if key == "by_model" and isinstance(value, dict):
                bucket = merged.setdefault("by_model", {})
                for model_name, model_metrics in value.items():
                    if not isinstance(model_metrics, dict):
                        continue
                    target = bucket.setdefault(model_name, {})
                    for sub_k, sub_v in model_metrics.items():
                        if isinstance(sub_v, (int, float)) and isinstance(target.get(sub_k), (int, float)):
                            target[sub_k] = target[sub_k] + sub_v
                        elif isinstance(sub_v, (int, float)) and sub_k not in target:
                            target[sub_k] = sub_v
                        else:
                            target[sub_k] = sub_v
                continue
            if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
                merged[key] = merged[key] + value
            elif isinstance(value, (int, float)) and key not in merged:
                merged[key] = value
            else:
                merged[key] = value
    return merged


def _wrap_with_envelope(raw_output, started_at, finished_at, reported_metrics):
    """Wrap the raw task output into a unified envelope.

    Compatible with the existing file lineage envelope: if ``raw_output``
    already carries ``TASK_RESULT_ENVELOPE``, we reuse it instead of
    nesting envelopes.
    """
    if is_task_error_result(raw_output):
        return raw_output

    user_metrics = {}
    file_manifest = None

    if isinstance(raw_output, dict) and raw_output.get(TASK_RESULT_ENVELOPE):
        result_inner = raw_output.get("result")
        file_manifest = raw_output.get("file_manifest")
    else:
        result_inner = raw_output

    if isinstance(result_inner, dict) and USER_METRICS_RETURN_KEY in result_inner:
        user_metrics = result_inner.pop(USER_METRICS_RETURN_KEY) or {}

    merged_metrics = _merge_metrics(reported_metrics, user_metrics)

    envelope = {
        TASK_RESULT_ENVELOPE: True,
        "result": result_inner,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": int((finished_at - started_at) * 1000),
    }
    if file_manifest is not None:
        envelope["file_manifest"] = file_manifest
    if merged_metrics:
        envelope["metrics"] = merged_metrics
    return envelope


def _ensure_result_can_cross_worker_boundary(result):
    try:
        cloudpickle.dumps(result)
    except Exception as exc:
        raise TypeError(
            "Task result cannot be serialized for transport from worker to scheduler. "
            "Return JSON-safe values, numpy arrays, bytes, pathlib paths, or objects "
            "that cloudpickle can serialize; write large model/binary outputs to files "
            "so Maze can return artifact references."
        ) from exc
    return result


def _maybe_invocation_error_output(output):
    if not isinstance(output, dict):
        return output
    if not output.get("__maze_invocation_task__"):
        return output
    cleaned_output = dict(output)
    cleaned_output.pop("__maze_invocation_task__", None)

    action = cleaned_output.get("action")
    parse_error = cleaned_output.get("parse_error")
    finish_reason = cleaned_output.get("finish_reason") or cleaned_output.get("stop_reason")

    if parse_error and isinstance(action, dict) and "error" in action:
        return _invocation_error_result(
            "json_parse_failed",
            str(action.get("error") or parse_error),
            details={
                "parse_error": parse_error,
                "raw": cleaned_output.get("raw"),
            },
        )

    if str(finish_reason or "").lower() == "length":
        return _invocation_error_result(
            "max_tokens_insufficient",
            "LLM response stopped because max_tokens was insufficient.",
            details={
                "finish_reason": finish_reason,
                "max_tokens": cleaned_output.get("max_tokens"),
            },
        )

    if isinstance(action, dict) and "error" in action:
        message = str(action.get("error") or "")
        reason = _invocation_reason(message)
        if reason in INVOCATION_ERROR_REASONS:
            return _invocation_error_result(
                reason,
                message,
                details={
                    "raw": action.get("raw") or cleaned_output.get("raw"),
                    "action": action,
                },
            )

    return cleaned_output


def _invocation_reason(message: str) -> str:
    text = str(message or "").lower()
    if "max_tokens" in text or "finish_reason" in text or ("maximum" in text and "token" in text):
        return "max_tokens_insufficient"
    if "json" in text or "expecting value" in text or "malformed" in text:
        return "json_parse_failed"
    if "schema" in text or "validation" in text or "required field" in text:
        return "schema_mismatch"
    if "missing" in text and ("arg" in text or "tool" in text):
        return "tool_invocation_args_missing"
    return "invocation_error"


def run_code_task(
    code_str: str = None,
    code_ser: str = None,
    task_input_data: dict = None,
    file_context: dict | None = None,
):
    try:
        if code_ser is not None:
            func = cloudpickle.loads(base64.b64decode(code_ser))
            return _ensure_result_can_cross_worker_boundary(
                _maybe_invocation_error_output(run_task_with_file_context(func, task_input_data, file_context))
            )
        if code_str is not None:
            runner = Runner(code_str, task_input_data)
            return _ensure_result_can_cross_worker_boundary(
                _maybe_invocation_error_output(run_task_with_file_context(lambda data: runner.run(data), task_input_data, file_context))
            )
        raise ValueError("Missing code_str or code_ser")
    except ArtifactError as exc:
        return task_error_result(
            exception_to_error_envelope(
                "artifact_error",
                exc,
                origin="artifact",
            )
        )
    except TypeError as exc:
        if "Task result cannot be serialized" not in str(exc):
            return task_error_result(
                exception_to_error_envelope(
                    "user_code",
                    exc,
                    origin="runner",
                )
            )
        return task_error_result(
            exception_to_error_envelope(
                "serialization_error",
                exc,
                origin="runner",
            )
        )
    except Exception as exc:
        return task_error_result(
            exception_to_error_envelope(
                "resource_insufficient" if looks_like_oom(exc) else "user_code",
                exc,
                origin="runner",
            )
        )


def run_langgraph_task(code_ser: str, args: str, kwargs: str):
    try:
        func = cloudpickle.loads(base64.b64decode(code_ser))
        loaded_args = cloudpickle.loads(base64.b64decode(args))
        loaded_kwargs = cloudpickle.loads(base64.b64decode(kwargs))
        return func(*loaded_args, **loaded_kwargs)
    except Exception as exc:
        return task_error_result(
            exception_to_error_envelope(
                "user_code",
                exc,
                origin="runner",
            )
        )


@ray.remote(max_retries=0)
def remote_task_runner(
    code_str: str = None,
    code_ser: str = None,
    task_input_data: dict = None,
    cuda_visible_devices: str | None = None,
    file_context: dict | None = None,
):
    if cuda_visible_devices:
        import os
        os.environ["CUDA_VISIBLE_DEVICES"] = cuda_visible_devices

    started_at = time.time()
    with _metrics_scope() as collector:
        output = run_code_task(
            code_str=code_str,
            code_ser=code_ser,
            task_input_data=task_input_data,
            file_context=file_context,
        )
        reported = _drain(collector)
    finished_at = time.time()
    return _wrap_with_envelope(output, started_at, finished_at, reported)


@ray.remote(max_retries=0)
def remote_lgraph_task_runner(code_ser: str, args: str, kwargs: str, cuda_visible_devices: str | None = None):
    if cuda_visible_devices:
        import os
        os.environ["CUDA_VISIBLE_DEVICES"] = cuda_visible_devices

    return run_langgraph_task(code_ser, args, kwargs)


class Runner():
    def __init__(self, code_str, task_input_data):
        self.code_str = code_str
        self.task_input_data = task_input_data

    def _extract_imports(self):
        tree = ast.parse(self.code_str)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports.append(node)
        return imports

    def _extract_function(self):
        func = None
        tree = ast.parse(self.code_str)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                func = node
                break
        return func

    def run(self, task_input_data=None):
        func_node = self._extract_function()
        import_nodes = self._extract_imports()
        namespace = {}

        for imp in import_nodes:
            module = ast.Module(body=[imp], type_ignores=[])
            code = compile(module, '<string>', 'exec')
            exec(code, namespace)

        module = ast.Module(body=[func_node], type_ignores=[])
        code = compile(module, '<string>', 'exec')
        exec(code, namespace)

        func_name = func_node.name
        if func_name in namespace:
            result = namespace[func_name](**(task_input_data if task_input_data is not None else (self.task_input_data or {})))
            if not isinstance(result, dict):
                raise TypeError(
                    f"Task {func_name} must return a dict. "
                    f"Got {type(result).__name__} instead."
                )
            return result
        else:
            raise NameError(f"Function {func_name} not found in namespace")
