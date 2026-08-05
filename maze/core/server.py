from ast import arg
import asyncio
import math
import struct
import uuid
import signal
import copy
import contextlib
import importlib.util
import json
import os
from pathlib import Path
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any,List
from urllib.parse import urlsplit, urlunsplit
from maze.core.path.path import (
    MaPath,
    SchedulerUnavailableError,
    WorkflowIdempotencyConflictError,
    WorkflowIdempotencyStateError,
    WorkflowInitializationError,
    WorkflowNotFoundError,
    validate_run_workflow_file_context,
)
from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from maze.core.workflow.task import CodeTask
from maze.core.files.artifact_store import LocalCASArtifactStore, sha256_bytes
from maze.core.application.spec import AppSpecError, app_file_context, app_spec_from_payload
from maze.core.workflow.dag_spec import DagSpecError, dag_file_context, dag_spec_from_payload
from maze.core.workflow.resources import apply_model_anchor_estimate, model_anchor_gpu_mem_mb, normalize_resources
from maze.core.local_models import DEFAULT_MODEL_DIR, RUNTIME_CONFIG_PATH, model_dir
from maze.core.scheduler.llm_instance import (
    validate_model_backend,
    validate_transformers_model,
)


app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   
    allow_credentials=True,
    allow_methods=["*"],  
    allow_headers=["*"],    
)

mapath = MaPath()
artifact_store = LocalCASArtifactStore()

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_TEST_TASK_TIMEOUT_SECONDS = 180
MODEL_TEST_WAIT_TIMEOUT_SECONDS = 240
MODEL_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".pt", ".pth")
DTYPE_BYTES = {
    "F64": 8,
    "FLOAT64": 8,
    "F32": 4,
    "FLOAT32": 4,
    "FP32": 4,
    "F16": 2,
    "FLOAT16": 2,
    "FP16": 2,
    "BF16": 2,
    "BFloat16": 2,
    "I64": 8,
    "U64": 8,
    "I32": 4,
    "U32": 4,
    "I16": 2,
    "U16": 2,
    "I8": 1,
    "U8": 1,
    "BOOL": 1,
}


LOCAL_MODEL_TEST_TASK_CODE = r'''
def maze_local_model_test(model_dir: str):
    import os
    import time
    from pathlib import Path

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"

    model = None
    tokenizer = None
    try:
        model_path = Path(model_dir)
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        started = time.time()
        tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=True)
        tokenizer_seconds = time.time() - started

        started = time.time()
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            local_files_only=True,
            trust_remote_code=True,
            dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
            device_map="auto" if torch.cuda.is_available() else None,
        )
        load_seconds = time.time() - started
        device = getattr(model, "device", None)
        if device is None:
            device = next(model.parameters()).device

        messages = [{"role": "user", "content": "Reply with exactly: OK"}]
        if hasattr(tokenizer, "apply_chat_template"):
            prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        else:
            prompt = "Reply with exactly: OK"
        inputs = tokenizer([prompt], return_tensors="pt")
        inputs = {key: value.to(device) for key, value in inputs.items()}

        started = time.time()
        with torch.inference_mode():
            output = model.generate(**inputs, max_new_tokens=8, do_sample=False)
        generate_seconds = time.time() - started
        new_tokens = output[:, inputs["input_ids"].shape[-1]:]
        generated_text = tokenizer.decode(new_tokens[0], skip_special_tokens=True).strip()
        peak_allocated = torch.cuda.max_memory_allocated() if torch.cuda.is_available() else 0
        peak_reserved = torch.cuda.max_memory_reserved() if torch.cuda.is_available() else 0

        return {
            "tokenizer_seconds": round(tokenizer_seconds, 3),
            "load_seconds": round(load_seconds, 3),
            "generate_seconds": round(generate_seconds, 3),
            "device": str(device),
            "generated_text": generated_text,
            "cuda": torch.cuda.is_available(),
            "peak_cuda_allocated_bytes": int(peak_allocated),
            "peak_cuda_reserved_bytes": int(peak_reserved),
            "__maze_metrics__": {
                "model_load_seconds": round(load_seconds, 6),
                "gpu_memory_peak_allocated_bytes": int(peak_allocated),
                "gpu_memory_peak_reserved_bytes": int(peak_reserved),
            },
        }
    finally:
        del model
        del tokenizer
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
'''


def _load_runtime_config() -> Dict[str, Any]:
    with contextlib.suppress(Exception):
        return json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def _save_runtime_config(config: Dict[str, Any]) -> None:
    RUNTIME_CONFIG_PATH.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")


def _model_dir() -> Path:
    return model_dir()


def _module_available(name: str) -> bool:
    with contextlib.suppress(Exception):
        return importlib.util.find_spec(name) is not None
    return False


def _format_bytes(num_bytes: int | float | None) -> str | None:
    if num_bytes is None:
        return None
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


def _format_params(count: int | float | None) -> str | None:
    if count is None:
        return None
    value = float(count)
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(int(value))


def _dtype_nbytes(dtype: Any) -> float | None:
    raw = str(dtype or "").replace("torch.", "").replace("_", "").replace("-", "").upper()
    aliases = {
        "FLOAT64": 8,
        "DOUBLE": 8,
        "FLOAT32": 4,
        "FP32": 4,
        "FLOAT": 4,
        "FLOAT16": 2,
        "FP16": 2,
        "HALF": 2,
        "BFLOAT16": 2,
        "BF16": 2,
        "INT8": 1,
        "UINT8": 1,
        "FP8": 1,
        "INT4": 0.5,
        "UINT4": 0.5,
        "NF4": 0.5,
    }
    if raw in DTYPE_BYTES:
        return DTYPE_BYTES[raw]
    if raw in aliases:
        return aliases[raw]
    if "4BIT" in raw:
        return 0.55
    if "8BIT" in raw:
        return 1
    return None


def _config_weight_nbytes(config: Dict[str, Any]) -> float:
    quant = config.get("quantization_config") or {}
    if isinstance(quant, dict):
        bits = quant.get("bits") or quant.get("weight_bits")
        if quant.get("load_in_4bit") or bits == 4 or "4bit" in str(quant).lower():
            return 0.55
        if quant.get("load_in_8bit") or bits == 8 or "8bit" in str(quant).lower():
            return 1
    return _dtype_nbytes(config.get("torch_dtype") or config.get("dtype")) or 2


def _weight_files(path: Path) -> List[Path]:
    files: List[Path] = []
    if not path.is_dir():
        return files
    for root, dirs, names in os.walk(path):
        dirs[:] = [
            name for name in dirs
            if not name.startswith(".") and name != "__pycache__"
        ]
        for name in names:
            if name.endswith(MODEL_WEIGHT_SUFFIXES):
                files.append(Path(root) / name)
    return sorted(files)


def _safetensors_header_stats(file_path: Path) -> Dict[str, Any] | None:
    try:
        with file_path.open("rb") as handle:
            raw_len = handle.read(8)
            if len(raw_len) != 8:
                return None
            header_len = struct.unpack("<Q", raw_len)[0]
            if header_len <= 0 or header_len > 100 * 1024 * 1024:
                return None
            header = json.loads(handle.read(header_len).decode("utf-8"))
    except Exception:
        return None

    params = 0
    weight_bytes = 0
    dtypes: Dict[str, int] = {}
    for name, info in header.items():
        if name == "__metadata__" or not isinstance(info, dict):
            continue
        shape = info.get("shape") or []
        dtype = str(info.get("dtype") or "")
        dtypes[dtype] = dtypes.get(dtype, 0) + 1
        tensor_params = 1
        for dim in shape:
            tensor_params *= int(dim)
        params += tensor_params

        offsets = info.get("data_offsets")
        if isinstance(offsets, list) and len(offsets) == 2:
            weight_bytes += max(0, int(offsets[1]) - int(offsets[0]))
        else:
            nbytes = _dtype_nbytes(dtype)
            if nbytes is not None:
                weight_bytes += int(tensor_params * nbytes)

    if not params and not weight_bytes:
        return None
    return {
        "params": params or None,
        "weight_bytes": weight_bytes or None,
        "dtypes": dtypes,
    }


def _config_llm_param_estimate(config: Dict[str, Any]) -> int | None:
    hidden = config.get("hidden_size") or config.get("n_embd") or config.get("d_model")
    layers = config.get("num_hidden_layers") or config.get("n_layer") or config.get("num_layers")
    vocab = config.get("vocab_size")
    if not hidden or not layers or not vocab:
        return None

    hidden = int(hidden)
    layers = int(layers)
    vocab = int(vocab)
    heads = int(config.get("num_attention_heads") or config.get("n_head") or 0)
    kv_heads = int(config.get("num_key_value_heads") or config.get("num_kv_heads") or heads or 0)
    head_dim = int(config.get("head_dim") or (hidden // heads if heads else hidden))
    intermediate = config.get("intermediate_size") or config.get("n_inner") or config.get("ffn_dim")

    kv_width = kv_heads * head_dim if kv_heads else hidden
    attention = hidden * hidden + (2 * hidden * kv_width) + hidden * hidden

    mlp = 0
    if intermediate:
        intermediate = int(intermediate)
        model_type = str(config.get("model_type") or "").lower()
        gated = model_type in {"llama", "mistral", "mixtral", "qwen2", "qwen3", "gemma", "deepseek_v2"}
        mlp = hidden * intermediate * (3 if gated else 2)

    layer_norms = layers * hidden * 2
    embeddings = vocab * hidden
    total = embeddings + layers * (attention + mlp + layer_norms)
    if config.get("tie_word_embeddings") is False:
        total += embeddings
    return int(total)


def _estimate_local_model(path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    files = _weight_files(path)
    file_bytes = sum(file.stat().st_size for file in files if file.is_file())

    safe_stats = [_safetensors_header_stats(file) for file in files if file.suffix == ".safetensors"]
    safe_stats = [item for item in safe_stats if item]
    if safe_stats:
        params = sum(int(item.get("params") or 0) for item in safe_stats) or None
        weight_bytes = sum(int(item.get("weight_bytes") or 0) for item in safe_stats) or file_bytes
        method = "safetensors_header"
    else:
        params = _config_llm_param_estimate(config)
        if params:
            weight_bytes = int(params * _config_weight_nbytes(config))
            method = "config_formula"
        else:
            weight_bytes = file_bytes
            method = "file_size"

    gpu_mem_mb = int(math.ceil((weight_bytes * 1.2) / (1024 * 1024))) if weight_bytes else 0
    return {
        "weight_file_count": len(files),
        "weight_bytes": int(file_bytes),
        "weight_size": _format_bytes(file_bytes),
        "estimated_params": params,
        "estimated_params_label": _format_params(params),
        "estimated_weight_memory_bytes": int(weight_bytes) if weight_bytes else 0,
        "estimated_weight_memory": _format_bytes(weight_bytes),
        "estimated_gpu_mem_mb": gpu_mem_mb,
        "estimate_method": method,
    }


def _is_local_host(host: str | None) -> bool:
    return (host or "").strip().lower().strip("[]") in LOCAL_HOSTS


def _request_host(req: Request) -> str | None:
    header_host = req.headers.get("host", "")
    if not header_host:
        return req.client.host if req.client else None
    return header_host.rsplit(":", 1)[0].strip("[]")


def _worker_reachable_head_host(req: Request, cluster_host: str | None, explicit_host: str | None = None) -> str:
    if explicit_host:
        return explicit_host

    request_host = _request_host(req)
    if cluster_host and not _is_local_host(cluster_host):
        return cluster_host
    if request_host and not _is_local_host(request_host):
        return request_host
    return cluster_host or request_host or "localhost"


def _replace_url_host(base_url: str, host: str, fallback_port: int | None = None) -> str:
    parsed = urlsplit(base_url)
    port = parsed.port or fallback_port
    host_for_netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    netloc = f"{host_for_netloc}:{port}" if port else host_for_netloc
    return urlunsplit((parsed.scheme or "http", netloc, parsed.path, parsed.query, parsed.fragment)).rstrip("/")


def _configured_artifact_advertised_url() -> str | None:
    configured = (
        os.environ.get("MAZE_ARTIFACT_ADVERTISED_URL")
        or os.environ.get("MAZE_ARTIFACT_PUBLIC_URL")
    )
    if not configured:
        return None
    parsed = urlsplit(configured.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "MAZE_ARTIFACT_ADVERTISED_URL must be an absolute http(s) URL"
        )
    return configured.strip().rstrip("/")


def _request_base_url(req: Request) -> str:
    advertised_url = _configured_artifact_advertised_url()
    if advertised_url:
        return advertised_url
    parsed = urlsplit(str(req.base_url))
    return urlunsplit((parsed.scheme or "http", parsed.netloc, "", "", "")).rstrip("/")


def _cluster_has_remote_worker(cluster: Dict[str, Any] | None) -> bool:
    if not isinstance(cluster, dict):
        return False
    head_ip = str(cluster.get("head_node_ip") or "").strip()
    candidates = [
        *(cluster.get("nodes") or []),
        *(cluster.get("unregistered_ray_nodes") or []),
    ]
    return any(
        str(node.get("role") or "worker") != "head"
        and bool(node.get("alive", True))
        and (
            not head_ip
            or str(node.get("node_ip") or "").strip() != head_ip
        )
        for node in candidates
        if isinstance(node, dict)
    )


def _request_artifact_capability(req: Request) -> str | None:
    authorization = str(req.headers.get("authorization") or "")
    if authorization.lower().startswith("bearer "):
        capability = authorization[7:].strip()
        return capability or None
    capability = str(req.headers.get("x-maze-artifact-capability") or "").strip()
    return capability or None


def _redact_artifact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_artifact_secrets(item)
            for key, item in value.items()
            if str(key).lower() not in {
                "capability",
                "artifact_capability",
                "authorization",
                "x-maze-artifact-capability",
            }
        }
    if isinstance(value, list):
        return [_redact_artifact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_artifact_secrets(item) for item in value]
    return value


def _local_models() -> List[Dict[str, Any]]:
    model_dir = _model_dir()
    models = []
    if not model_dir.exists():
        return models
    for path in sorted(item for item in model_dir.iterdir() if item.is_dir()):
        config_path = path / "config.json"
        if not config_path.is_file():
            continue
        config: Dict[str, Any] = {}
        with contextlib.suppress(Exception):
            config = json.loads(config_path.read_text(encoding="utf-8"))
        model_type = str(config.get("model_type") or "")
        estimate = _estimate_local_model(path, config)
        models.append({
            "id": path.name,
            "name": path.name,
            "path": str(path),
            "type": "local",
            "model_type": model_type,
            "backend": "transformers",
            "backends": ["transformers"],
            "model_scope": "head",
            **estimate,
        })
    return models


def _model_file_checks(model_id: str) -> tuple[Dict[str, Any], Path, List[Dict[str, Any]], Dict[str, Any] | None]:
    model = next((item for item in _local_models() if item["id"] == model_id), None)
    if not model:
        raise HTTPException(status_code=404, detail="local model not found in the configured model directory")

    path = Path(model["path"])
    checks = []

    def check(name: str, ok: bool, message: str):
        checks.append({"name": name, "ok": ok, "message": message})

    config_path = path / "config.json"
    config_ok = False
    with contextlib.suppress(Exception):
        json.loads(config_path.read_text(encoding="utf-8"))
        config_ok = True
    check("config", config_ok, "config.json is readable" if config_ok else "config.json is missing or invalid")

    weight_suffixes = (".safetensors", ".bin", ".gguf", ".pt", ".pth")
    weight_files = [
        item.name
        for item in path.iterdir()
        if item.is_file() and (item.name.endswith(weight_suffixes) or item.name.endswith(".index.json"))
    ] if path.is_dir() else []
    check("weights", bool(weight_files), f"{len(weight_files)} weight/index file(s)" if weight_files else "no model weight file found")

    tokenizer_files = [
        name for name in ("tokenizer.json", "tokenizer.model", "vocab.json", "merges.txt")
        if (path / name).is_file()
    ]
    check("tokenizer", bool(tokenizer_files), ", ".join(tokenizer_files) if tokenizer_files else "no tokenizer file found")

    file_ready = config_ok and bool(weight_files) and bool(tokenizer_files)
    if not file_ready:
        return model, path, checks, {
            "status": "success",
            "ok": False,
            "model": model,
            "checks": checks,
            "message": "Model files are incomplete",
        }

    transformers_ok = _module_available("transformers")
    check("transformers", transformers_ok, "installed" if transformers_ok else "not installed in the head environment")
    if not transformers_ok:
        return model, path, checks, {
            "status": "success",
            "ok": False,
            "model": model,
            "checks": checks,
            "message": "Transformers is required for the local load test",
        }

    return model, path, checks, None


def _model_anchor_for_model(model: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "local_model": model["id"],
        "model_scope": model.get("model_scope") or "head",
        "backend": model.get("backend") or "transformers",
        "estimated_weight_memory_bytes": model.get("estimated_weight_memory_bytes") or 0,
        "estimated_gpu_mem_mb": model.get("estimated_gpu_mem_mb") or 0,
        "estimated_params": model.get("estimated_params"),
    }


async def _resources_for_model_anchor(
    resources: Dict[str, Any] | None,
    model_anchor: Dict[str, Any] | None,
) -> Dict[str, Any]:
    next_resources = apply_model_anchor_estimate(resources, model_anchor)

    if not model_anchor:
        return next_resources

    anchor = dict(model_anchor)
    if anchor.get("model_scope") == "head":
        cluster = await mapath.get_cluster_resources()
        head_node_id = cluster.get("head_node_id")
        if head_node_id:
            next_resources["target_node_id"] = head_node_id

    return next_resources


def _model_test_resources(cluster: Dict[str, Any], model: Dict[str, Any] | None = None) -> Dict[str, Any]:
    head_node_id = cluster.get("head_node_id")
    head = next((node for node in cluster.get("nodes", []) if node.get("node_id") == head_node_id), None)
    resources = {"cpu_num": 1, "gpu_mem": 0, "io_num": 0}
    if head_node_id:
        resources["target_node_id"] = head_node_id
    estimated_gpu_mem = model_anchor_gpu_mem_mb(_model_anchor_for_model(model or {})) if model else 0
    for device in (head or {}).get("resources", {}).get("gpu", {}).get("devices", []):
        if device.get("total_count", 0) > 0:
            if estimated_gpu_mem:
                resources["gpu_mem"] = estimated_gpu_mem
            return resources
    return resources


async def _wait_for_model_test_run(run_id: str, timeout_seconds: float) -> Dict[str, Any]:
    queue = mapath.async_que.get(run_id)
    if queue is None:
        raise RuntimeError(f"run queue not found: {run_id}")

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        message = await asyncio.wait_for(queue.get(), timeout=remaining)
        if message.get("type") in {"finish_workflow", "task_exception"}:
            return mapath.get_static_run_snapshot(run_id)


async def _run_model_test_task(model: Dict[str, Any], path: Path, checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    cluster = await mapath.get_cluster_resources()
    resources = _model_test_resources(cluster, model)
    workflow_id = f"model-test-{uuid.uuid4()}"
    task_id = str(uuid.uuid4())
    run_id = None

    mapath.create_workflow(workflow_id)
    try:
        workflow = mapath.get_workflow(workflow_id)
        task = CodeTask(workflow_id, task_id, f"Test {model['name']}")
        workflow.add_task(task_id, task)
        task.save_task(
            task_input={
                "input_params": {
                    "1": {
                        "key": "model_dir",
                        "input_schema": "from_user",
                        "data_type": "str",
                        "value": str(path),
                        "has_value": True,
                    }
                }
            },
            task_output={
                "output_params": {
                    "1": {"key": "generated_text", "data_type": "str"},
                }
            },
            code_str=LOCAL_MODEL_TEST_TASK_CODE,
            code_ser=None,
            resources=resources,
            task_kind="gpu",
            timeout_seconds=MODEL_TEST_TASK_TIMEOUT_SECONDS,
        )

        run_id = mapath.run_workflow(
            workflow_id,
            timeout_seconds=MODEL_TEST_WAIT_TIMEOUT_SECONDS,
            tags=["model-test"],
            metadata={"kind": "local_model_test", "model_id": model["id"], "model_path": str(path)},
        )
        checks.append({"name": "maze_task", "ok": True, "message": f"run {run_id[:8]} task {task_id[:8]}"})
        snapshot = await _wait_for_model_test_run(run_id, MODEL_TEST_WAIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            if run_id:
                await mapath.stop_workflow(run_id)
        checks.append({"name": "load", "ok": False, "message": f"timed out after {MODEL_TEST_WAIT_TIMEOUT_SECONDS}s"})
        return {
            "status": "success",
            "ok": False,
            "model": model,
            "checks": checks,
            "run_id": run_id,
            "workflow_id": workflow_id,
            "task_id": task_id,
            "resources": resources,
            "message": "Local model test task timed out",
        }
    finally:
        mapath.workflows.pop(workflow_id, None)
        if run_id:
            mapath.async_que.pop(run_id, None)

    task_snapshot = (snapshot.get("task_nodes") or {}).get(task_id) or {}
    result = task_snapshot.get("result_summary") or {}
    if snapshot.get("status") != "succeeded" or task_snapshot.get("status") != "succeeded":
        error = task_snapshot.get("error") or snapshot.get("error_summary") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        checks.append({"name": "load", "ok": False, "message": message or "task failed"})
        return {
            "status": "success",
            "ok": False,
            "model": model,
            "checks": checks,
            "run_id": run_id,
            "workflow_id": workflow_id,
            "task_id": task_id,
            "resources": resources,
            "error": error,
            "message": "Local model load test failed",
        }

    checks.append({"name": "load", "ok": True, "message": f"{result.get('load_seconds', '?')}s on {result.get('device', 'unknown')}"})
    checks.append({"name": "generate", "ok": bool(result.get("generated_text")), "message": result.get("generated_text") or "no text generated"})
    return {
        "status": "success",
        "ok": bool(result.get("generated_text")),
        "model": model,
        "checks": checks,
        "runtime": result,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "task_id": task_id,
        "resources": resources,
        "message": "Local model loaded and generated a response" if result.get("generated_text") else "Local model loaded but generated no text",
    }


async def _test_local_model(model_id: str) -> Dict[str, Any]:
    model, path, checks, early_response = _model_file_checks(model_id)
    if early_response is not None:
        return early_response
    return await _run_model_test_task(model, path, checks)


async def _worker_reachable_file_context(req: Request, file_context: Dict[str, Any] | None):
    validate_run_workflow_file_context(file_context)
    if not file_context or not file_context.get("enabled"):
        return file_context

    artifact_store_context = file_context.get("artifact_store") or {}
    advertised_url = _configured_artifact_advertised_url()
    base_url = advertised_url or artifact_store_context.get("base_url")
    if not base_url:
        return file_context

    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Artifact store base_url must be an absolute http(s) URL")

    cluster = None
    try:
        cluster = await mapath.get_cluster_resources(timeout=2.0)
    except Exception:
        pass
    cluster_host = cluster.get("head_node_ip") if isinstance(cluster, dict) else None
    multi_node = _cluster_has_remote_worker(cluster)

    prepared_context = copy.deepcopy(file_context)
    prepared_store = dict(prepared_context.get("artifact_store") or {})
    prepared_store["base_url"] = str(base_url).rstrip("/")

    if not _is_local_host(parsed.hostname):
        prepared_context["artifact_store"] = prepared_store
        return prepared_context

    head_host = _worker_reachable_head_host(req, cluster_host)
    if head_host and not _is_local_host(head_host):
        prepared_store["base_url"] = _replace_url_host(base_url, head_host, req.url.port)

    final_host = urlsplit(prepared_store["base_url"]).hostname
    if multi_node and _is_local_host(final_host):
        raise ValueError(
            "Multi-node artifact transport requires MAZE_ARTIFACT_ADVERTISED_URL "
            "or a reachable non-loopback Head address"
        )

    prepared_context["artifact_store"] = prepared_store
    return prepared_context

def signal_handler(signum, frame):
    mapath.cleanup()
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

@app.post("/apps/validate")
async def validate_app_spec(req: Request):
    try:
        data = await req.json()
        payload = data.get("spec", data)
        spec = app_spec_from_payload(
            payload,
            source_path=data.get("source_path"),
            overrides={
                "workspace": data.get("workspace_dir"),
                "timeout_seconds": data.get("timeout_seconds"),
            },
        )
        return {"status": "success", "spec": spec}
    except AppSpecError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/apps/run")
async def run_app(req: Request):
    try:
        data = await req.json()
        payload = data.get("spec", data)
        spec = app_spec_from_payload(
            payload,
            source_path=data.get("source_path"),
            overrides={
                "workspace": data.get("workspace_dir"),
                "timeout_seconds": data.get("timeout_seconds"),
            },
        )
        artifact_mode = data.get("artifact_mode", True)
        file_context = data.get("file_context")
        if file_context is None:
            file_context = app_file_context(
                spec,
                artifact_base_url=_request_base_url(req),
                artifact_mode=artifact_mode,
            )
        file_context = await _worker_reachable_file_context(req, file_context)
        workflow_id = mapath.create_app_workflow(spec)
        metadata = {
            **dict(spec.get("metadata") or {}),
            **dict(data.get("metadata") or {}),
            "app_name": spec["name"],
            "workflow_name": spec["name"],
            "run_kind": "app",
            "app_spec": spec,
        }
        tags = list(dict.fromkeys([*spec.get("tags", []), *data.get("tags", []), "app"]))
        run_id = mapath.run_workflow(
            workflow_id,
            file_context=file_context,
            timeout_seconds=spec.get("timeout_seconds"),
            tags=tags,
            metadata=metadata,
        )
        return {
            "status": "success",
            "run_id": run_id,
            "workflow_id": workflow_id,
            "spec": spec,
        }
    except AppSpecError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SchedulerUnavailableError as e:
        raise HTTPException(status_code=503, detail=e.detail())
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/workflows/validate")
async def validate_dag_workflow(req: Request):
    try:
        data = await req.json()
        payload = data.get("spec", data)
        spec = dag_spec_from_payload(payload)
        return {"status": "success", "spec": spec}
    except DagSpecError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/workflows/submit")
async def submit_dag_workflow(req: Request):
    try:
        data = await req.json()
        if not isinstance(data, dict):
            raise TypeError("request body must be a JSON object")

        unsupported_fields = sorted(
            field
            for field in (
                "inputs",
                "final_output_refs",
                "idempotency_key",
                "idempotency_fingerprint",
            )
            if field in data
        )
        if unsupported_fields:
            raise ValueError(
                "/workflows/submit does not support fields: "
                + ", ".join(unsupported_fields)
            )

        payload = data.get("spec", data)
        spec = dag_spec_from_payload(payload)
        run_config = spec.get("run") or {}
        raw_run_config = payload.get("run") or {}
        if not isinstance(raw_run_config, dict):
            raise TypeError("spec.run must be a JSON object")

        if "artifact_mode" in raw_run_config:
            artifact_mode = raw_run_config["artifact_mode"]
            artifact_mode_field = "spec.run.artifact_mode"
        else:
            artifact_mode = data.get("artifact_mode", True)
            artifact_mode_field = "artifact_mode"
        if not isinstance(artifact_mode, bool):
            raise TypeError(f"{artifact_mode_field} must be a boolean")

        def validated_tags(value: Any, field_name: str) -> List[str]:
            if value is None:
                return []
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise TypeError(f"{field_name} must be a list of strings")
            return value

        validated_tags(payload.get("tags"), "spec.tags")
        validated_tags(raw_run_config.get("tags"), "spec.run.tags")
        request_tags = validated_tags(data.get("tags"), "tags")

        file_context = data.get("file_context")
        if file_context is None:
            file_context = dag_file_context(
                spec,
                artifact_base_url=_request_base_url(req),
                artifact_mode=artifact_mode,
            )
        file_context = await _worker_reachable_file_context(req, file_context)
        workflow_id = mapath.create_dag_workflow(spec)
        workflow = mapath.get_workflow(workflow_id)
        for task in workflow.tasks.values():
            if getattr(task, "model_anchor", None):
                task.resources = await _resources_for_model_anchor(task.resources, task.model_anchor)

        metadata = {
            **dict(spec.get("metadata") or {}),
            **dict(run_config.get("metadata") or {}),
            **dict(data.get("metadata") or {}),
            "workflow_name": spec["name"],
            "run_kind": "dag",
            "dag_spec": spec,
        }
        tags = list(dict.fromkeys([
            *spec.get("tags", []),
            *run_config.get("tags", []),
            *request_tags,
            "dag",
        ]))
        run_id = mapath.run_workflow(
            workflow_id,
            file_context=file_context,
            timeout_seconds=run_config.get("timeout_seconds"),
            tags=tags,
            metadata=metadata,
            **({"inputs": run_config["inputs"]} if "inputs" in run_config else {}),
            **(
                {"final_output_refs": spec["final_output_refs"]}
                if "final_output_refs" in spec
                else {}
            ),
            **(
                {
                    "idempotency_key": run_config.get("idempotency_key"),
                    "idempotency_fingerprint": run_config.get("idempotency_fingerprint"),
                }
                if "idempotency_key" in run_config
                or "idempotency_fingerprint" in run_config
                else {}
            ),
        )
        response = {
            "status": "success",
            "workflow_id": workflow_id,
            "run_id": run_id,
            "spec": spec,
        }
        if run_config.get("idempotency_key") is not None:
            response["idempotency_key"] = run_config["idempotency_key"]
            response["idempotency_fingerprint"] = run_config["idempotency_fingerprint"]
        return response
    except DagSpecError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except WorkflowIdempotencyConflictError as e:
        raise HTTPException(status_code=409, detail=e.detail())
    except WorkflowNotFoundError as e:
        raise HTTPException(status_code=404, detail=e.detail())
    except WorkflowInitializationError as e:
        raise HTTPException(status_code=500, detail=e.detail())
    except WorkflowIdempotencyStateError as e:
        raise HTTPException(status_code=500, detail=e.detail())
    except SchedulerUnavailableError as e:
        raise HTTPException(status_code=503, detail=e.detail())
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs")
async def create_dynamic_run(req: Request):
    try:
        data = await req.json()
        run_id = await mapath.create_dynamic_run(
            max_tasks=data.get("max_tasks", 100),
            timeout_seconds=data.get("timeout_seconds"),
            file_context=await _worker_reachable_file_context(req, data.get("file_context")),
            metadata=data.get("metadata"),
        )
        return {"status": "success", "run_id": run_id}
    except SchedulerUnavailableError as e:
        raise HTTPException(status_code=503, detail=e.detail())
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs")
async def list_runs(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    limit: Optional[int] = None,
    detail: bool = False,
):
    try:
        return {
            "status": "success",
            "runs": _redact_artifact_secrets(
                await mapath.list_runs(status=status, kind=kind, limit=limit, detail=detail)
            ),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    try:
        return {
            "status": "success",
            "run": _redact_artifact_secrets(await mapath.get_run_snapshot(run_id)),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs/{run_id}/tasks")
async def get_run_tasks(run_id: str):
    try:
        return {
            "status": "success",
            "run_id": run_id,
            "tasks": _redact_artifact_secrets(await mapath.get_run_tasks(run_id)),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs/{run_id}/tasks/{task_id}")
async def get_run_task(run_id: str, task_id: str):
    try:
        return {
            "status": "success",
            "run_id": run_id,
            "task": _redact_artifact_secrets(await mapath.get_run_task(run_id, task_id)),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs/{run_id}/artifacts")
async def get_run_artifacts(run_id: str):
    try:
        return {
            "status": "success",
            "run_id": run_id,
            "artifacts": _redact_artifact_secrets(await mapath.get_run_artifacts(run_id)),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs/{run_id}/tasks/{task_id}/artifacts")
async def get_run_task_artifacts(run_id: str, task_id: str):
    try:
        return {
            "status": "success",
            "run_id": run_id,
            "task_id": task_id,
            "artifacts": _redact_artifact_secrets(
                await mapath.get_run_task_artifacts(run_id, task_id)
            ),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs/{run_id}/events")
async def get_run_events(run_id: str, after: Optional[int] = None):
    try:
        return {
            "status": "success",
            "run_id": run_id,
            "events": _redact_artifact_secrets(await mapath.get_run_events(run_id, after)),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs/{run_id}/logs")
async def get_run_logs(run_id: str, tail: Optional[int] = 500, task_id: Optional[str] = None):
    try:
        return {
            "status": "success",
            **await mapath.get_run_logs(run_id, tail=tail, task_id=task_id),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, req: Request):
    try:
        try:
            data = await req.json()
        except Exception:
            data = {}

        if run_id in mapath.dynamic_runs:
            dynamic_run = await mapath.cancel_dynamic_run(run_id, data.get("reason"))
            return {
                "status": "success",
                "run_id": run_id,
                "run_status": dynamic_run.status,
            }

        await mapath.stop_workflow(run_id)
        snapshot = await mapath.get_run_snapshot(run_id)
        response = {
            "status": "success",
            "run_id": run_id,
            "run_status": snapshot.get("status"),
        }
        initialization = snapshot.get("idempotency_initialization")
        if (
            isinstance(initialization, dict)
            and initialization.get("status") == "cleanup_pending"
        ):
            response["initialization_status"] = "cleanup_pending"
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/runs/{run_id}/retry")
async def retry_run(run_id: str, req: Request):
    try:
        try:
            data = await req.json()
        except Exception:
            data = {}
        snapshot = await mapath.get_run_snapshot(run_id)
        metadata = snapshot.get("metadata") or {}
        spec = metadata.get("app_spec")
        if not spec:
            raise HTTPException(status_code=400, detail="Only AppSpec runs can be retried through this endpoint")

        spec = app_spec_from_payload(
            spec,
            source_path=spec.get("source_path"),
            overrides={
                "workspace": data.get("workspace_dir"),
                "timeout_seconds": data.get("timeout_seconds"),
            },
        )
        file_context = data.get("file_context")
        if file_context is None:
            file_context = app_file_context(
                spec,
                artifact_base_url=_request_base_url(req),
                artifact_mode=data.get("artifact_mode", True),
            )
        file_context = await _worker_reachable_file_context(req, file_context)
        workflow_id = mapath.create_app_workflow(spec)
        retry_metadata = {
            **metadata,
            **dict(data.get("metadata") or {}),
            "app_name": spec["name"],
            "workflow_name": spec["name"],
            "run_kind": "app",
            "app_spec": spec,
            "retried_from_run_id": run_id,
        }
        previous_tags = snapshot.get("tags") or []
        tags = list(dict.fromkeys([*previous_tags, *data.get("tags", []), "app", "retry"]))
        new_run_id = mapath.run_workflow(
            workflow_id,
            file_context=file_context,
            timeout_seconds=spec.get("timeout_seconds"),
            tags=tags,
            metadata=retry_metadata,
        )
        return {
            "status": "success",
            "run_id": new_run_id,
            "workflow_id": workflow_id,
            "retried_from_run_id": run_id,
            "spec": spec,
        }
    except HTTPException:
        raise
    except AppSpecError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SchedulerUnavailableError as e:
        raise HTTPException(status_code=503, detail=e.detail())
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dynamic_runs")
async def list_dynamic_runs(
    status: Optional[str] = None,
    limit: Optional[int] = None,
    detail: bool = False,
):
    try:
        return {
            "status": "success",
            "runs": await mapath.list_dynamic_runs(status=status, limit=limit, detail=detail),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs/cleanup")
async def cleanup_dynamic_runs(req: Request):
    try:
        try:
            data = await req.json()
        except Exception:
            data = {}
        return {
            "status": "success",
            "cleanup": await mapath.cleanup_dynamic_runs(
                statuses=data.get("statuses"),
                older_than_days=data.get("older_than_days"),
                dry_run=data.get("dry_run", True),
            ),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs/{run_id}/task_specs")
async def register_dynamic_task_spec(run_id: str, req: Request):
    try:
        data = await req.json()
        task_spec = await mapath.register_dynamic_task_spec(run_id, data)
        return {
            "status": "success",
            "run_id": run_id,
            "task_spec_id": task_spec.task_spec_id,
            "task_name": task_spec.task_name,
            "inputs": task_spec.inputs,
            "outputs": task_spec.outputs,
            "resources": task_spec.resources,
        }
    except SchedulerUnavailableError as e:
        raise HTTPException(status_code=503, detail=e.detail())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dynamic_runs/{run_id}")
async def get_dynamic_run(run_id: str):
    try:
        return {
            "status": "success",
            "run": await mapath.get_dynamic_run_snapshot(run_id),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/dynamic_runs/{run_id}")
async def delete_dynamic_run(run_id: str):
    try:
        return {
            "status": "success",
            **await mapath.delete_dynamic_run(run_id),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs/{run_id}/append_task")
async def append_dynamic_task(run_id: str, req: Request):
    try:
        data = await req.json()
        task, idempotent = await mapath.append_dynamic_task(
            run_id=run_id,
            task_spec_id=data.get("task_spec_id"),
            task_spec_payload=data.get("task_spec"),
            inputs=data.get("inputs", {}),
            parents=data.get("parents", []),
            request_id=data.get("request_id"),
            resources=data.get("resources") or data.get("resource_override"),
            model_anchor=data.get("model_anchor"),
        )
        outputs = []
        if task.task_output:
            outputs = [
                {
                    "name": output_info.get("key"),
                    "data_type": output_info.get("data_type", "any"),
                }
                for output_info in task.task_output.get("output_params", {}).values()
            ]
        return {
            "status": "success",
            "run_id": run_id,
            "task_id": task.task_id,
            "task_name": task.task_name,
            "outputs": outputs,
            "idempotent": idempotent,
        }
    except SchedulerUnavailableError as e:
        raise HTTPException(status_code=503, detail=e.detail())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs/{run_id}/finalize")
async def finalize_dynamic_run(run_id: str, req: Request):
    try:
        data = await req.json()
        await mapath.finalize_dynamic_run(run_id, data.get("result"))
        return {"status": "success", "run_id": run_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs/{run_id}/cancel")
async def cancel_dynamic_run(run_id: str, req: Request):
    try:
        try:
            data = await req.json()
        except Exception:
            data = {}
        dynamic_run = await mapath.cancel_dynamic_run(run_id, data.get("reason"))
        return {
            "status": "success",
            "run_id": run_id,
            "run_status": dynamic_run.status,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dynamic_runs/{run_id}/events")
async def get_dynamic_run_events(run_id: str, after: Optional[int] = None):
    try:
        return {
            "status": "success",
            "run_id": run_id,
            "events": await mapath.get_dynamic_run_events(run_id, after),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs/{run_id}/events")
async def emit_dynamic_run_event(run_id: str, req: Request):
    try:
        data = await req.json()
        event = await mapath.emit_dynamic_run_event(run_id, data)
        return {
            "status": "success",
            "run_id": run_id,
            "event": event,
        }
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/dynamic_runs/{run_id}/metadata")
async def update_dynamic_run_metadata(run_id: str, req: Request):
    try:
        data = await req.json()
        metadata = data.get("metadata", data)
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be a JSON object")
        updated = await mapath.update_dynamic_run_metadata(run_id, metadata)
        return {
            "status": "success",
            "run_id": run_id,
            "metadata": updated,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs/{run_id}/permission_requests")
async def create_dynamic_permission_request(run_id: str, req: Request):
    try:
        data = await req.json()
        request_payload = data.get("request", data)
        created = await mapath.upsert_dynamic_permission_request(run_id, request_payload)
        return {
            "status": "success",
            "run_id": run_id,
            "request": created,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dynamic_runs/{run_id}/permission_requests/{request_id}")
async def get_dynamic_permission_request(run_id: str, request_id: str):
    try:
        snapshot = await mapath.get_dynamic_run_snapshot(run_id)
        requests_map = (snapshot.get("metadata") or {}).get("permission_requests") or {}
        request_payload = requests_map.get(request_id)
        if not isinstance(request_payload, dict):
            raise ValueError(f"Permission request not found: {request_id}")
        return {
            "status": "success",
            "run_id": run_id,
            "request": request_payload,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs/{run_id}/permission_requests/{request_id}/decision")
async def decide_dynamic_permission_request(run_id: str, request_id: str, req: Request):
    try:
        data = await req.json()
        decision = data.get("decision", data)
        decided = await mapath.decide_dynamic_permission_request(run_id, request_id, decision)
        return {
            "status": "success",
            "run_id": run_id,
            "request": decided,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/dynamic_runs/{run_id}/events")
async def get_dynamic_run_events_ws(websocket: WebSocket, run_id: str):
    try:
        await websocket.accept()
        await mapath.get_dynamic_run_res(run_id, websocket)
        await websocket.close()
    except Exception:
        await websocket.close()

@app.post("/get_head_ray_port")
async def get_head_ray_port():
    try:
        port =  mapath.get_ray_head_port()
        return {"status": "success","port": port}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/models")
async def get_models():
    return {
        "status": "success",
        "model_dir": str(_model_dir()),
        "models": _local_models(),
    }

@app.get("/resource-history")
async def get_resource_history():
    try:
        return {"status": "success", "history": mapath.resource_history.load()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/models/config")
async def set_models_config(req: Request):
    data = await req.json()
    raw_model_dir = str(data.get("model_dir") or "").strip()
    if not raw_model_dir:
        raise HTTPException(status_code=400, detail="model_dir is required")
    model_dir = Path(raw_model_dir).expanduser()
    if not model_dir.is_absolute():
        raise HTTPException(status_code=400, detail="model_dir must be an absolute path on the head server")

    model_dir = model_dir.resolve()
    if not model_dir.exists() or not model_dir.is_dir():
        raise HTTPException(status_code=400, detail="model_dir must be an existing directory on the head server")

    config = _load_runtime_config()
    config["model_dir"] = str(model_dir)
    _save_runtime_config(config)
    return {
        "status": "success",
        "model_dir": str(model_dir),
        "models": _local_models(),
    }

@app.post("/models/test")
async def test_model(req: Request):
    data = await req.json()
    model_id = str(data.get("model_id") or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="model_id is required")
    return await _test_local_model(model_id)

@app.get("/cluster/resources")
async def get_cluster_resources():
    try:
        resources = await mapath.get_cluster_resources()
        return {"status": "success", "cluster": resources}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for scheduler cluster resources")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cluster/queues")
async def get_cluster_queues():
    try:
        queues = await mapath.get_cluster_queues()
        if isinstance(queues, dict) and not queues.get("scheduling_algorithm"):
            queues = dict(queues)
            queues["scheduling_algorithm"] = getattr(mapath, "strategy", None) or "FCFS"
        return {"status": "success", "queues": queues}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for scheduler queue snapshot")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/cluster/nodes/{node_id}/disable")
async def disable_cluster_node(node_id: str):
    try:
        result = await mapath.set_cluster_node_disabled(node_id=node_id, disabled=True)
        return {
            "status": "success",
            "node_id": result.get("node_id", node_id),
            "disabled": result.get("disabled", True),
            "cluster": result.get("cluster"),
        }
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for scheduler node control")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/cluster/nodes/{node_id}/enable")
async def enable_cluster_node(node_id: str):
    try:
        result = await mapath.set_cluster_node_disabled(node_id=node_id, disabled=False)
        return {
            "status": "success",
            "node_id": result.get("node_id", node_id),
            "disabled": result.get("disabled", False),
            "cluster": result.get("cluster"),
        }
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for scheduler node control")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cluster/join_command")
async def get_cluster_join_command(req: Request, host: Optional[str] = None):
    try:
        cluster = await mapath.get_cluster_resources()
        head_host = _worker_reachable_head_host(req, cluster.get("head_node_ip"), host)
        port = req.url.port or 80
        head_url = f"http://{head_host}:{port}"
        command = f"maze start --worker --addr {head_host}:{port}"
        return {
            "status": "success",
            "head_host": head_host,
            "head_url": head_url,
            "ray_head_port": mapath.get_ray_head_port(),
            "command": command,
            "agent_command": f"{command} --agent",
        }
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for scheduler cluster resources")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/cluster/reconcile_workers")
async def reconcile_workers(req: Request):
    try:
        try:
            data = await req.json()
        except Exception:
            data = {}
        cluster = await mapath.get_cluster_resources()
        host = _worker_reachable_head_host(req, cluster.get("head_node_ip"), data.get("host"))
        port = int(data.get("port") or req.url.port or 80)
        ray_head_port = mapath.get_ray_head_port()
        head_url = f"http://{host}:{port}"
        commands = [
            {
                "node_id": node.get("node_id"),
                "node_ip": node.get("node_ip"),
                "command": f"maze start --worker --addr {host}:{port}",
                "agent_command": f"maze start --worker --addr {host}:{port} --agent",
            }
            for node in cluster.get("unregistered_ray_nodes", [])
        ]
        return {
            "status": "success",
            "head_host": host,
            "head_url": head_url,
            "ray_head_port": ray_head_port,
            "unregistered_count": len(commands),
            "unregistered_ray_nodes": cluster.get("unregistered_ray_nodes", []),
            "recommended_commands": commands,
            "executed": False,
        }
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for scheduler cluster resources")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/artifacts/sha256/{sha256}")
async def put_artifact(sha256: str, req: Request):
    try:
        capability = _request_artifact_capability(req)
        if capability:
            artifact_store.require_upload_capability(capability)
        elif artifact_store.is_private(sha256):
            raise HTTPException(status_code=404, detail="Artifact not found")
        data = await req.body()
        if sha256_bytes(data) != sha256:
            raise HTTPException(status_code=400, detail="Artifact checksum mismatch")
        return artifact_store.put_bytes(
            sha256,
            data,
            private=bool(capability),
            capability=capability,
        )
    except PermissionError:
        raise HTTPException(status_code=404, detail="Artifact not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.head("/artifacts/sha256/{sha256}")
async def head_artifact(sha256: str, req: Request):
    try:
        if not artifact_store.exists(sha256):
            raise HTTPException(status_code=404, detail="Artifact not found")
        return artifact_store.metadata(sha256, _request_artifact_capability(req))
    except PermissionError:
        raise HTTPException(status_code=404, detail="Artifact not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/artifacts/sha256/{sha256}/metadata")
async def get_artifact_metadata(sha256: str, req: Request):
    try:
        if not artifact_store.exists(sha256):
            raise HTTPException(status_code=404, detail="Artifact not found")
        return {
            "status": "success",
            "artifact": artifact_store.metadata(
                sha256,
                _request_artifact_capability(req),
            ),
        }
    except PermissionError:
        raise HTTPException(status_code=404, detail="Artifact not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/artifacts/sha256/{sha256}")
async def get_artifact(sha256: str, req: Request):
    try:
        path = artifact_store.blob_path(sha256)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Artifact not found")
        artifact_store.require_read(sha256, _request_artifact_capability(req))
        return FileResponse(path, media_type="application/octet-stream", filename=sha256)
    except PermissionError:
        raise HTTPException(status_code=404, detail="Artifact not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/start_worker")
async def start_worker(req:Request):
    try:
        data = await req.json()
        worker = await mapath.start_worker(
            data["node_ip"],
            data["node_id"],
            data["resources"],
            data.get("capabilities"),
        )
        return {"status": "success", "worker": worker}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for scheduler worker registration")
    except SchedulerUnavailableError as e:
        raise HTTPException(status_code=503, detail=e.detail())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# for multiple llm inference instance
@app.post("/start_llm_instance")
async def start_llm_instance(req:Request):
    try:
        data = await req.json()
        model = str(data.get("model") or "").strip()
        backend = data.get("backend", "vllm")
        cpu_nums = int(data.get("cpu_nums", 5))
        gpu_nums = int(data.get("gpu_nums", 1))
        gpu_mem = int(data.get("gpu_mem", 0))
        if "memory_mib" in data:
            memory = int(data["memory_mib"]) * 1024 * 1024
        elif "memory" in data:
            memory = int(data["memory"])
        else:
            memory = 1024 * 1024 * 1024
        if not model:
            raise ValueError("model is required")
        if cpu_nums < 0 or memory < 0 or gpu_mem < 0:
            raise ValueError("resource reservations must not be negative")
        if gpu_nums != 1:
            raise ValueError("Model instances currently require exactly one GPU")

        backend_args = {}
        if data.get("gpu_memory_utilization") is not None:
            utilization = float(data["gpu_memory_utilization"])
            if not 0 < utilization <= 1:
                raise ValueError("gpu_memory_utilization must be between 0 and 1")
            backend_args["gpu_memory_utilization"] = utilization
        if data.get("max_model_len") is not None:
            max_model_len = int(data["max_model_len"])
            if max_model_len <= 0:
                raise ValueError("max_model_len must be positive")
            backend_args["max_model_len"] = max_model_len
        backend, backend_args = validate_model_backend(backend, backend_args)
        if backend == "transformers":
            validate_transformers_model(model)
        instance_id = str(uuid.uuid4())
        return await mapath.start_llm_instance(
            instance_id,
            model,
            cpu_nums,
            gpu_nums,
            memory,
            gpu_mem,
            backend=backend,
            backend_args=backend_args,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SchedulerUnavailableError as e:
        raise HTTPException(status_code=503, detail=e.detail())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/stop_llm_instance")
async def stop_llm_instance(req:Request):
    try:
        data = await req.json()
        stopped = await mapath.stop_llm_instance(data["instance_id"])
        return {"status": "success", "instance": stopped}
    except SchedulerUnavailableError as e:
        raise HTTPException(status_code=503, detail=e.detail())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# === Phase 1 observability API (static workflows) ===

@app.get("/v1/metrics")
async def get_global_metrics():
    """Cluster-wide aggregate metrics for static workflows."""
    try:
        return mapath.get_global_metrics_snapshot()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/runs")
async def list_runs(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """List static workflow runs (newest first)."""
    try:
        runs = mapath.list_static_runs(status=status, limit=limit + offset)
        offset = max(0, int(offset))
        limit = max(0, int(limit))
        return {
            "runs": runs[offset:offset + limit] if limit else runs[offset:],
            "total": len(runs),
            "offset": offset,
            "limit": limit,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/runs/{run_id}/snapshot")
async def get_run_snapshot(run_id: str):
    """Full snapshot of a static run (in-memory if active, else from store)."""
    try:
        return mapath.get_static_run_snapshot(run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/runs/{run_id}/current-task")
async def get_run_current_task(run_id: str):
    """What is the run currently doing?"""
    try:
        return mapath.get_static_current_task(run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/runs/{run_id}/tasks")
async def list_run_tasks(run_id: str):
    """All tasks of a static run with their states and metrics."""
    try:
        snapshot = mapath.get_static_run_snapshot(run_id)
        tasks = snapshot.get("task_nodes") or {}
        return {
            "run_id": run_id,
            "task_total": (snapshot.get("task_counts") or {}).get("total", len(tasks)),
            "tasks": _redact_artifact_secrets(tasks),
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/runs/{run_id}/timeline")
async def get_run_timeline(run_id: str, after: Optional[int] = None):
    """Event log of a static run (one event per scheduling moment)."""
    try:
        events = mapath._get_static_run_events(run_id, after=after)
        return {"run_id": run_id, "events": events}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
