"""
Static resource inference from task source (AST + light heuristics).
Merged with user _task_decorator values in query_loader (max on numerics; type rules).
"""
from __future__ import annotations

import ast
import inspect
import math
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# --- Parameter scale from text (e.g. "32B", "7.5B") ---
_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([Bb])\b")
# Model id / path like ...-32B-... or ..._70b...
_NAME_PARAM_RE = re.compile(r"[-_/](\d+(?:\.\d+)?)\s*([Bb])(?:[-_.\s]|$)", re.IGNORECASE)

VRAM_OVERHEAD = 1.1
GPU_REF_MB = 8000.0
CPU_REF = 4.0


def _literal_str(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Str):  # pragma: no cover
        return node.s
    return None


def _collect_string_literals(tree: ast.AST) -> List[str]:
    out: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.Str):  # pragma: no cover
            out.append(node.s)
    return out


def _max_param_count_from_text(texts: Iterable[str]) -> float:
    best = 0.0
    for s in texts:
        for m in _PARAM_RE.finditer(s):
            val = float(m.group(1))
            suf = m.group(2).lower()
            if suf == "b":
                n = val * 1e9
                best = max(best, n)
        for m in _NAME_PARAM_RE.finditer(s):
            val = float(m.group(1))
            suf = m.group(2).lower()
            if suf == "b":
                n = val * 1e9
                best = max(best, n)
    return best


def _from_pretrained_strings(tree: ast.AST) -> List[str]:
    found: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = None
        if isinstance(fn, ast.Attribute) and fn.attr == "from_pretrained":
            name = "from_pretrained"
        elif isinstance(fn, ast.Name) and fn.id == "from_pretrained":
            name = "from_pretrained"
        if name != "from_pretrained" or not node.args:
            continue
        s = _literal_str(node.args[0])
        if s:
            found.append(s)
    return found


def _infer_bytes_per_param(joined_lower: str) -> int:
    if "float64" in joined_lower or "fp64" in joined_lower or "torch.float64" in joined_lower:
        return 8
    if any(
        x in joined_lower
        for x in ("float32", "fp32", "torch.float32", "bfloat32")
    ):
        return 4
    return 2


def _infer_cpu_num_from_ast(tree: ast.AST) -> int:
    best = 1
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg in ("max_workers", "processes", "num_workers") and kw.value:
                v = _const_int(kw.value)
                if v is not None:
                    best = max(best, v)
            if kw.arg == "n_jobs" and kw.value:
                v = _const_int(kw.value)
                if v is not None and v > 0:
                    best = max(best, v)
    return best


def _const_int(node: ast.AST) -> Optional[int]:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return int(node.value)
    if isinstance(node, ast.Num):  # pragma: no cover
        return int(node.n)
    return None


def _infer_mem_mb_heuristic(source: str) -> int:
    base = 1024
    hits = 0
    for needle in ("read_csv", "read_parquet", "read_json", "read_feather", "open(", "download", "requests.get", "httpx."):
        hits += source.count(needle)
    return min(32768, base + hits * 512)


def _parse_function_ast(func: Callable[..., Any]) -> Optional[ast.AST]:
    try:
        src = inspect.getsource(func)
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func.__name__:
            return node
    return tree


def infer_resources_from_function(func: Callable[..., Any]) -> Dict[str, Any]:
    """
    Returns inferred: cpu_num, mem (MB), gpu_mem (MB), model_name (optional str).
    gpu_mem is 0 if no parameter scale was found.
    """
    tree = _parse_function_ast(func)
    if tree is None:
        return {"cpu_num": 1, "mem": 1024, "gpu_mem": 0, "model_name": None}

    strings = _collect_string_literals(tree)
    strings.extend(_from_pretrained_strings(tree))
    joined = " ".join(strings).lower()

    bpp = _infer_bytes_per_param(joined)
    n_params = _max_param_count_from_text(strings)
    if n_params <= 0:
        gpu_mem_mb = 0
    else:
        bytes_est = n_params * float(bpp) * VRAM_OVERHEAD
        gpu_mem_mb = int(math.ceil(bytes_est / (1024 * 1024)))

    cpu_num = _infer_cpu_num_from_ast(tree)
    try:
        full_src = inspect.getsource(func)
    except (OSError, TypeError):
        full_src = ""
    mem_mb = _infer_mem_mb_heuristic(full_src)

    model_name = None
    fps = _from_pretrained_strings(tree)
    if fps:
        model_name = fps[0]

    return {
        "cpu_num": int(cpu_num),
        "mem": int(mem_mb),
        "gpu_mem": int(gpu_mem_mb),
        "model_name": model_name,
    }


def _user_type_explicit(decorator_info: Dict[str, Any]) -> bool:
    if "type" not in decorator_info:
        return False
    t = decorator_info.get("type")
    return t is not None and t != ""


def _infer_type_cpu_gpu(gpu_mem: float, cpu_num: float) -> str:
    gw = float(gpu_mem) / GPU_REF_MB
    cw = float(cpu_num) / CPU_REF
    if gw >= cw:
        return "gpu"
    return "cpu"


def merge_resource_node_attrs(func: Callable[..., Any], decorator_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build nx node attributes: max(user decorator numerics, inferred), type rule.
    """
    inf = infer_resources_from_function(func)
    merged: Dict[str, Any] = {}

    for key in ("cpu_num", "mem", "gpu_mem"):
        iv = int(inf.get(key, 0))
        if key in decorator_info:
            try:
                uv = int(decorator_info[key])
            except (TypeError, ValueError):
                uv = 0
            merged[key] = max(uv, iv)
        else:
            merged[key] = max(0, iv) if iv > 0 else iv

    merged["backend"] = decorator_info.get("backend", "huggingface")
    merged["model_name"] = decorator_info.get("model_name")
    if merged["model_name"] is None and inf.get("model_name"):
        merged["model_name"] = inf["model_name"]

    if _user_type_explicit(decorator_info):
        merged["type"] = decorator_info["type"]
    else:
        merged["type"] = _infer_type_cpu_gpu(float(merged["gpu_mem"]), float(merged["cpu_num"]))

    return merged
