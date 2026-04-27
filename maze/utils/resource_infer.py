from __future__ import annotations

import ast
import inspect
import math
import re
from typing import Any, Callable, Dict, Iterable, List, Optional

_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([Bb])\b")
_NAME_PARAM_RE = re.compile(r"[-_/](\d+(?:\.\d+)?)\s*([Bb])(?:[-_.\s]|$)", re.IGNORECASE)
_VRAM_OVERHEAD = 1.1


def _literal_str(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Str):  # pragma: no cover
        return node.s
    return None


def _collect_string_literals(tree: ast.AST) -> List[str]:
    out: List[str] = []
    for node in ast.walk(tree):
        value = _literal_str(node)
        if value is not None:
            out.append(value)
    return out


def _from_pretrained_strings(tree: ast.AST) -> List[str]:
    found: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "from_pretrained" and node.args:
            value = _literal_str(node.args[0])
            if value is not None:
                found.append(value)
        elif isinstance(fn, ast.Name) and fn.id == "from_pretrained" and node.args:
            value = _literal_str(node.args[0])
            if value is not None:
                found.append(value)
    return found


def _max_param_count_from_text(texts: Iterable[str]) -> float:
    best = 0.0
    for text in texts:
        for match in _PARAM_RE.finditer(text):
            best = max(best, float(match.group(1)) * 1e9)
        for match in _NAME_PARAM_RE.finditer(text):
            best = max(best, float(match.group(1)) * 1e9)
    return best


def _infer_bytes_per_param(joined_lower: str) -> int:
    if "float64" in joined_lower or "fp64" in joined_lower or "torch.float64" in joined_lower:
        return 8
    if any(token in joined_lower for token in ("float32", "fp32", "torch.float32", "bfloat32")):
        return 4
    return 2


def _parse_function_ast(func: Callable[..., Any]) -> Optional[ast.AST]:
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func.__name__:
            return node
    return tree


def infer_gpu_resources_from_function(func: Callable[..., Any]) -> Dict[str, int]:
    tree = _parse_function_ast(func)
    if tree is None:
        return {"gpu": 0, "gpu_mem": 0}

    strings = _collect_string_literals(tree)
    strings.extend(_from_pretrained_strings(tree))
    joined = " ".join(strings).lower()
    bytes_per_param = _infer_bytes_per_param(joined)
    n_params = _max_param_count_from_text(strings)

    if n_params <= 0:
        return {"gpu": 0, "gpu_mem": 0}

    bytes_est = n_params * float(bytes_per_param) * _VRAM_OVERHEAD
    gpu_mem_mb = int(math.ceil(bytes_est / (1024 * 1024)))
    return {"gpu": 1, "gpu_mem": max(gpu_mem_mb, 0)}
