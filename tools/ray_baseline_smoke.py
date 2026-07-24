#!/usr/bin/env python3
"""Ray correctness baseline for migrated GAIA/OpenAGI/tau-bench workflows.

This is an experimental comparison runner, intentionally kept outside
``src/ascend_maze``.  It reuses the migrated workflow definitions and sampling
logic from ``qwen_benchmark_smoke.py``, but executes the compiled DAG through
ordinary Ray tasks and either a Ray actor-owned vLLM-Ascend service or a
text-only per-task Transformers cold-load path.

The baseline does not use the Ascend-Maze scheduler, controller, placement
manager, runtime client or C11 model router.  It is meant to answer a narrower
question first: can the same migrated workflows run end-to-end with local Qwen
models under a plain Ray task/actor execution path?

Typical plan-only usage from the repository root:

    PYTHONPATH="$PWD/src:$PWD:${PYTHONPATH:-}" \
      python tools/ray_baseline_smoke.py --plan-only --samples-per-workflow 1

Typical hardware usage:

    PYTHONPATH="$PWD/src:$PWD:${PYTHONPATH:-}" \
      /home/user2/workplace/miniconda3/envs/ascend-maze/bin/python \
      tools/ray_baseline_smoke.py \
        --family text \
        --samples-per-workflow 1 \
        --output-dir experiments/ray_baseline_smoke/text_first
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from enum import Enum
import hashlib
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
TOOLS_ROOT = REPO_ROOT / "tools"
for _path in (str(TOOLS_ROOT), str(SRC_ROOT), str(REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import qwen_benchmark_smoke as qwen_smoke  # noqa: E402


RAY_BASELINE_OBJECTIVE = "ray_correctness_baseline"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "experiments" / "ray_baseline_smoke"
RAY_TASK_MAX_CALLS = 1


class RayBaselineError(RuntimeError):
    """Expected operational failure in the Ray baseline runner."""


def _jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return {
            "__bytes__": {
                "size": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
            }
        }
    if isinstance(value, bytearray):
        return _jsonable(bytes(value))
    if isinstance(value, memoryview):
        return _jsonable(value.tobytes())
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "items_tuple"):
        return {
            str(key): _jsonable(item)
            for key, item in value.items_tuple()  # type: ignore[attr-defined]
        }
    if hasattr(value, "__dataclass_fields__"):
        return {key: _jsonable(item) for key, item in asdict(value).items()}  # type: ignore[arg-type]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def emit(name: str, value: object) -> None:
    if isinstance(value, str):
        print(f"{name} {value}", flush=True)
    else:
        print(
            f"{name} "
            + json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True),
            flush=True,
        )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _append_jsonl(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def _persist_sample_record(
    *,
    records_path: Path,
    failures_path: Path,
    record: Mapping[str, object],
) -> bool:
    """Persist one sample record and mirror non-success records to failures."""
    _append_jsonl(records_path, record)
    succeeded = record.get("status") == "succeeded"
    if not succeeded:
        _append_jsonl(failures_path, record)
    return succeeded


def _plain(value: object) -> object:
    """Convert Ascend-Maze canonical containers to ordinary Python objects."""
    if hasattr(value, "items_tuple"):
        return {
            _plain(key): _plain(item)
            for key, item in value.items_tuple()  # type: ignore[attr-defined]
        }
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _install_repo_path() -> None:
    for path in (str(TOOLS_ROOT), str(SRC_ROOT), str(REPO_ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(SRC_ROOT), str(REPO_ROOT)]
    if existing:
        parts.append(existing)
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)


def _load_callable(module_name: str, qualname: str) -> Any:
    if "<locals>" in qualname:
        raise RayBaselineError(f"cannot import local callable: {module_name}:{qualname}")
    module = importlib.import_module(module_name)
    value: Any = module
    for part in qualname.split("."):
        value = getattr(value, part)
    return value


def _conservative_sampling_options() -> dict[str, object]:
    return {
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
    }


def _launch_options_for_family(family: str) -> dict[str, object]:
    """Return fixed vLLM launch options that are part of the baseline contract."""
    if family != "vision":
        return {}
    return {
        "generation_config": "vllm",
        "qwen2_5_vl_cpu_unique_consecutive_workaround": True,
    }


def _runtime_patch_dir() -> Path:
    return SRC_ROOT / "ascend_maze" / "inference" / "adapters" / "vllm_runtime_patches"


def _build_vllm_argv(
    *,
    python_executable: Path,
    host: str,
    port: int,
    model_path: Path,
    served_model_name: str,
    dtype: str,
    max_model_len: int,
    gpu_memory_utilization: float,
    max_num_seqs: int,
    max_num_batched_tokens: int | None,
    trust_remote_code: bool,
    generation_config: str | None = None,
) -> list[str]:
    argv = [
        str(python_executable),
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--host",
        host,
        "--port",
        str(port),
        "--model",
        str(model_path),
        "--served-model-name",
        served_model_name,
        "--dtype",
        dtype,
        "--max-model-len",
        str(max_model_len),
        "--tensor-parallel-size",
        "1",
        "--gpu-memory-utilization",
        str(gpu_memory_utilization),
        "--block-size",
        "128",
        "--enforce-eager",
        "--no-enable-prefix-caching",
        "--max-num-seqs",
        str(max_num_seqs),
    ]
    if max_num_batched_tokens is not None:
        argv.extend(("--max-num-batched-tokens", str(max_num_batched_tokens)))
    if trust_remote_code:
        argv.append("--trust-remote-code")
    if generation_config is not None:
        argv.extend(("--generation-config", generation_config))
    return argv


def _service_environment(
    *,
    base_env: Mapping[str, str],
    device_id: str,
    log_level: str,
    runtime_preloads: Mapping[str, str],
    runtime_library_paths: tuple[str, ...],
    qwen2_5_vl_cpu_unique_consecutive_workaround: bool = False,
) -> dict[str, str]:
    env = dict(base_env)
    env["ASCEND_RT_VISIBLE_DEVICES"] = str(device_id)
    env["PYTHONUNBUFFERED"] = "1"
    env["VLLM_LOGGING_LEVEL"] = log_level
    if runtime_preloads:
        env["LD_PRELOAD"] = " ".join(path for path in runtime_preloads)
    if runtime_library_paths:
        inherited = tuple(
            item for item in env.get("LD_LIBRARY_PATH", "").split(os.pathsep) if item
        )
        env["LD_LIBRARY_PATH"] = os.pathsep.join(
            (*runtime_library_paths, *inherited)
        )
    if qwen2_5_vl_cpu_unique_consecutive_workaround:
        inherited = tuple(
            item for item in env.get("PYTHONPATH", "").split(os.pathsep) if item
        )
        env["ASCEND_MAZE_QWEN25VL_CPU_UNIQUE_CONSECUTIVE"] = "1"
        env["PYTHONPATH"] = os.pathsep.join((str(_runtime_patch_dir()), *inherited))
    return env


class _VllmServiceActor:
    """Ray actor that owns one local vLLM-Ascend OpenAI-compatible server."""

    def __init__(self, config: Mapping[str, object]) -> None:
        self.config = dict(config)
        self.process: subprocess.Popen[str] | None = None
        self.log_handle: Any | None = None
        self.endpoint = f"http://127.0.0.1:{int(self.config['port'])}"
        self.model_id = str(self.config["model_id"])
        self.log_path = str(self.config["log_path"])

    def start(self) -> dict[str, object]:
        if self.process is not None and self.process.poll() is None:
            return self.status()
        log_path = Path(self.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = log_path.open("a", encoding="utf-8", buffering=1)
        argv = _build_vllm_argv(
            python_executable=Path(str(self.config["python_executable"])),
            host="127.0.0.1",
            port=int(self.config["port"]),
            model_path=Path(str(self.config["model_path"])),
            served_model_name=self.model_id,
            dtype=str(self.config["dtype"]),
            max_model_len=int(self.config["max_model_len"]),
            gpu_memory_utilization=float(self.config["gpu_memory_utilization"]),
            max_num_seqs=int(self.config["max_num_seqs"]),
            max_num_batched_tokens=self.config["max_num_batched_tokens"],  # type: ignore[arg-type]
            trust_remote_code=bool(self.config["trust_remote_code"]),
            generation_config=self.config.get("generation_config"),  # type: ignore[arg-type]
        )
        env = _service_environment(
            base_env=os.environ,
            device_id=str(self.config["device_id"]),
            log_level=str(self.config["log_level"]),
            runtime_preloads=dict(self.config.get("runtime_preloads", {})),  # type: ignore[arg-type]
            runtime_library_paths=tuple(self.config.get("runtime_library_paths", ())),  # type: ignore[arg-type]
            qwen2_5_vl_cpu_unique_consecutive_workaround=bool(
                self.config.get("qwen2_5_vl_cpu_unique_consecutive_workaround", False)
            ),
        )
        self.log_handle.write("RAY_BASELINE_VLLM_ARGV " + json.dumps(argv) + "\n")
        self.log_handle.write(
            "RAY_BASELINE_VLLM_ENV "
            + json.dumps(
                {
                    "ASCEND_MAZE_QWEN25VL_CPU_UNIQUE_CONSECUTIVE": env.get(
                        "ASCEND_MAZE_QWEN25VL_CPU_UNIQUE_CONSECUTIVE"
                    ),
                    "ASCEND_RT_VISIBLE_DEVICES": env.get("ASCEND_RT_VISIBLE_DEVICES"),
                    "PYTHONPATH_PREFIX": env.get("PYTHONPATH", "").split(os.pathsep)[:3],
                },
                sort_keys=True,
            )
            + "\n"
        )
        self.process = subprocess.Popen(
            argv,
            cwd=str(Path(str(self.config["model_path"])).parent),
            env=env,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self._wait_ready()
        return self.status()

    async def chat(self, payload: Mapping[str, object]) -> dict[str, object]:
        return await asyncio.to_thread(self._chat_sync, payload)

    def _chat_sync(self, payload: Mapping[str, object]) -> dict[str, object]:
        started = time.monotonic()
        request_payload = {
            "model": self.model_id,
            **dict(payload),
            **_conservative_sampling_options(),
        }
        request_payload["model"] = self.model_id
        response = self._http_request(
            "POST",
            f"{self.endpoint}/v1/chat/completions",
            json_body=request_payload,
            timeout_ms=int(self.config["request_timeout_ms"]),
        )
        if response["status_code"] != 200:
            raise RayBaselineError(
                f"vLLM chat returned HTTP {response['status_code']}: "
                f"{str(response['text'])[:500]}"
            )
        body = json.loads(str(response["text"]))
        try:
            choices = body["choices"]
            usage = body["usage"]
            choice = choices[0]
            message = choice["message"]
            text = message["content"]
            finish_reason = choice["finish_reason"]
            input_tokens = usage["prompt_tokens"]
            output_tokens = usage["completion_tokens"]
            if not isinstance(text, str) or not isinstance(finish_reason, str):
                raise TypeError
            if isinstance(input_tokens, bool) or not isinstance(input_tokens, int):
                raise TypeError
            if isinstance(output_tokens, bool) or not isinstance(output_tokens, int):
                raise TypeError
        except (KeyError, IndexError, TypeError) as exc:
            raise RayBaselineError("vLLM chat response schema is invalid") from exc
        return {
            "text": text,
            "finish_reason": finish_reason,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "engine_queue_depth": None,
            "prefix_cache_hit": None,
            "ttft_ms": None,
            "total_duration_ms": max(0, int((time.monotonic() - started) * 1_000)),
        }

    def status(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "endpoint": self.endpoint,
            "pid": None if self.process is None else self.process.pid,
            "returncode": None if self.process is None else self.process.poll(),
            "log_path": self.log_path,
            "argv": _build_vllm_argv(
                python_executable=Path(str(self.config["python_executable"])),
                host="127.0.0.1",
                port=int(self.config["port"]),
                model_path=Path(str(self.config["model_path"])),
                served_model_name=self.model_id,
                dtype=str(self.config["dtype"]),
                max_model_len=int(self.config["max_model_len"]),
                gpu_memory_utilization=float(self.config["gpu_memory_utilization"]),
                max_num_seqs=int(self.config["max_num_seqs"]),
                max_num_batched_tokens=self.config["max_num_batched_tokens"],  # type: ignore[arg-type]
                trust_remote_code=bool(self.config["trust_remote_code"]),
                generation_config=self.config.get("generation_config"),  # type: ignore[arg-type]
            ),
        }

    def stop(self) -> dict[str, object]:
        errors: list[str] = []
        process = self.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as exc:  # pragma: no cover - defensive cleanup path.
                errors.append(f"sigterm:{type(exc).__name__}:{exc}")
            deadline = time.monotonic() + 30.0
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception as exc:  # pragma: no cover
                    errors.append(f"sigkill:{type(exc).__name__}:{exc}")
                try:
                    process.wait(timeout=10)
                except Exception as exc:  # pragma: no cover
                    errors.append(f"wait:{type(exc).__name__}:{exc}")
        if self.log_handle is not None:
            try:
                self.log_handle.close()
            finally:
                self.log_handle = None
        return {
            "model_id": self.model_id,
            "endpoint": self.endpoint,
            "pid": None if process is None else process.pid,
            "returncode": None if process is None else process.poll(),
            "errors": errors,
            "log_path": self.log_path,
        }

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + int(self.config["startup_timeout_ms"]) / 1_000
        last_error = ""
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RayBaselineError(
                    f"vLLM process exited during startup: returncode={self.process.returncode}"
                )
            try:
                health = self._http_request(
                    "GET",
                    f"{self.endpoint}/health",
                    json_body=None,
                    timeout_ms=5_000,
                )
                if health["status_code"] == 200:
                    models = self._http_request(
                        "GET",
                        f"{self.endpoint}/v1/models",
                        json_body=None,
                        timeout_ms=5_000,
                    )
                    if models["status_code"] == 200 and self.model_id in str(
                        models["text"]
                    ):
                        self._warmup()
                        return
                    last_error = f"models={models['status_code']}"
                else:
                    last_error = f"health={health['status_code']}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(1.0)
        raise RayBaselineError(
            f"vLLM did not become ready before timeout: {last_error}"
        )

    def _warmup(self) -> None:
        response = self._chat_sync(
            {
                "messages": [{"role": "user", "content": "Reply with exactly: ready"}],
                "max_tokens": 8,
                "temperature": 0.0,
            }
        )
        if not str(response.get("text", "")).strip():
            raise RayBaselineError("vLLM warmup returned empty text")

    @staticmethod
    def _http_request(
        method: str,
        url: str,
        *,
        json_body: object | None,
        timeout_ms: int,
    ) -> dict[str, object]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - caught by preflight.
            raise RayBaselineError("httpx is required for Ray baseline vLLM HTTP") from exc
        with httpx.Client(timeout=timeout_ms / 1_000) as client:
            response = client.request(method, url, json=json_body)
        return {
            "status_code": int(response.status_code),
            "text": response.text,
            "headers": dict(response.headers),
        }


class _BaselineRouteRouter:
    def __init__(self) -> None:
        self.inflight = 0
        self.max_inflight = 0

    def request_started(self, route_lease_id: str) -> object:
        del route_lease_id
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        return None

    def request_finished(self, route_lease_id: str) -> None:
        del route_lease_id
        self.inflight = max(0, self.inflight - 1)


class _RayActorChatAdapter:
    def __init__(self, service_actor: Any) -> None:
        self.service_actor = service_actor

    async def invoke_chat(self, context: Any, request: Any) -> Any:
        del context
        from ascend_maze.inference.contracts import ChatResponse, InferenceCallError

        payload = {
            "messages": [_plain(message) for message in request.messages],
            "max_tokens": request.max_tokens,
            "temperature": float(request.temperature),
        }
        try:
            import ray

            response = await asyncio.to_thread(
                ray.get,
                self.service_actor.chat.remote(payload),
            )
        except InferenceCallError:
            raise
        except Exception as exc:
            raise InferenceCallError(
                "model_inference_failed",
                f"Ray baseline model actor call failed: {type(exc).__name__}: {exc}",
            ) from exc
        return ChatResponse(**response)


class _RayTransformersChatAdapter:
    def __init__(self, config: Mapping[str, object]) -> None:
        from ascend_maze.inference.adapters.transformers_local import (
            TransformersLocalGenerationConfig,
            TransformersLocalGenerationSession,
        )

        generation_config = TransformersLocalGenerationConfig(
            model_path=str(config["model_path"]),
            tokenizer_path=str(config.get("tokenizer_path", config["model_path"])),
            dtype=str(config["dtype"]),
            max_model_len=int(config["max_model_len"]),
            device_id=str(config["device_id"]),
            trust_remote_code=bool(config.get("trust_remote_code", False)),
            enable_thinking=bool(config.get("enable_thinking", False)),
            generation_method=str(config.get("generation_method", "generate")),
            model_kind=str(config.get("model_kind", "text")),
            qwen2_5_vl_cpu_unique_consecutive_workaround=bool(
                config.get(
                    "qwen2_5_vl_cpu_unique_consecutive_workaround",
                    False,
                )
            ),
            runtime_library_paths=tuple(config.get("runtime_library_paths", ())),
        )
        self.session = TransformersLocalGenerationSession(generation_config)
        self.invocation_records: list[dict[str, object]] = []

    async def invoke_chat(self, context: Any, request: Any) -> Any:
        from ascend_maze.inference.contracts import InferenceCallError

        try:
            response, metrics = await asyncio.to_thread(
                self.session.generate,
                request,
            )
        except InferenceCallError:
            raise
        except Exception as exc:
            raise InferenceCallError(
                "model_inference_failed",
                f"Ray baseline transformers call failed: {type(exc).__name__}: {exc}",
            ) from exc
        self.invocation_records.append(
            {
                "adapter": "ray_baseline_transformers_local",
                "route_lease_id": context.route_lease_id,
                "model_id": context.model_id,
                "instance_id": context.instance_id,
                "instance_generation": context.instance_generation,
                "call_index": len(self.invocation_records) + 1,
                **metrics,
            }
        )
        return response

    async def close(self) -> None:
        cleanup_ms = await asyncio.to_thread(self.session.close)
        if self.invocation_records:
            self.invocation_records[-1]["cleanup_ms"] = cleanup_ms


def _execute_workflow_task_remote(
    *,
    task_payload: Mapping[str, object],
    kwargs: Mapping[str, object],
    route_payload: Mapping[str, object] | None,
    service_actor: Any | None,
    inference_backend: str,
    transformers_config: Mapping[str, object] | None,
    dispatch_started_wall_ns: int,
) -> dict[str, object]:
    """Run one compiled task in a normal Ray worker process."""
    import ray

    worker_entry_wall_ns = time.time_ns()
    task_started_perf = time.perf_counter()
    worker_startup_ms = max(
        0, int((worker_entry_wall_ns - dispatch_started_wall_ns) / 1_000_000)
    )
    input_started_perf = time.perf_counter()
    bound_kwargs = dict(kwargs)
    input_fetch_ms = qwen_smoke._elapsed_ms(input_started_perf)  # noqa: SLF001
    _install_repo_path()
    callable_started_perf: float | None = None
    callable_execute_ms = 0
    inference_records: list[dict[str, object]] = []
    transformers_local_records: list[dict[str, object]] = []
    transformers_adapter: _RayTransformersChatAdapter | None = None
    runtime_context = ray.get_runtime_context()
    worker_node_id = runtime_context.get_node_id()
    worker_node_ip = ray.util.get_node_ip_address()

    def task_timing(status: str, error_code: str | None) -> dict[str, object]:
        chat_request_ms = sum(
            int(item.get("duration_ms", 0))
            for item in inference_records
            if isinstance(item.get("duration_ms"), int)
            and not isinstance(item.get("duration_ms"), bool)
        )
        task_total_ms = qwen_smoke._elapsed_ms(task_started_perf)  # noqa: SLF001
        return {
            "started_at_ms": worker_entry_wall_ns // 1_000_000,
            "status": status,
            "error_code": error_code,
            "dispatch_prepare_ms": 0,
            "worker_startup_ms": worker_startup_ms,
            "worker_startup_scope": "ray_schedule_and_input_materialization",
            "dispatch_wait_ms": worker_startup_ms,
            "input_fetch_ms": input_fetch_ms,
            "input_fetch_scope": "ray_materialized_argument_binding",
            "callable_execute_ms": callable_execute_ms,
            "chat_request_ms": chat_request_ms,
            "callable_minus_chat_ms": max(
                0, callable_execute_ms - chat_request_ms
            ),
            "output_put_ms": 0,
            "output_put_scope": "computed_by_driver_after_ray_get",
            "task_total_ms": task_total_ms,
            "task_runtime_overhead_ms": max(
                0, task_total_ms - input_fetch_ms - callable_execute_ms
            ),
        }

    try:
        from ascend_maze.contracts.runtime import ModelRouteLease
        from ascend_maze.inference.context import (
            AttemptInferenceSession,
            install_route_session,
        )

        func = _load_callable(
            str(task_payload["module"]),
            str(task_payload["qualname"]),
        )
        if route_payload is not None:
            if inference_backend == "vllm" and service_actor is None:
                raise RayBaselineError("model task has no Ray service actor")
            if inference_backend == "transformers" and transformers_config is None:
                raise RayBaselineError("model task has no transformers config")
            now_ms = int(time.time() * 1000)
            lease = ModelRouteLease(
                route_lease_id=str(route_payload["route_lease_id"]),
                run_id=str(route_payload["run_id"]),
                task_id=str(task_payload["task_id"]),
                attempt=1,
                model_id=str(route_payload["model_id"]),
                catalog_revision="ray-baseline",
                instance_id=str(route_payload["instance_id"]),
                instance_generation=1,
                adapter_name=(
                    "ray_baseline_transformers_local"
                    if inference_backend == "transformers"
                    else "ray_baseline_vllm_openai"
                ),
                endpoint_id=str(route_payload["endpoint_id"]),
                instance_node_id="ray-local",
                instance_boot_id="ray-baseline",
                affinity_key_hash=str(route_payload["model_id"]),
                created_at_ms=now_ms,
                dispatch_deadline_ms=now_ms + int(route_payload["deadline_ms"]),
            )
            router = _BaselineRouteRouter()
            adapter = (
                _RayTransformersChatAdapter(transformers_config or {})
                if inference_backend == "transformers"
                else _RayActorChatAdapter(service_actor)
            )
            if isinstance(adapter, _RayTransformersChatAdapter):
                transformers_adapter = adapter
            session = AttemptInferenceSession(
                lease=lease,
                router=router,
                adapter=adapter,
                instance_placement_lease_id=f"ray-baseline-placement-{lease.instance_id}",
                record_sink=lambda record: inference_records.append(asdict(record)),
            )
            callable_started_perf = time.perf_counter()
            with install_route_session(session):
                result = func(**bound_kwargs)
            callable_execute_ms = qwen_smoke._elapsed_ms(  # noqa: SLF001
                callable_started_perf
            )
            inference_summary = asdict(session.summary())
            inference_summary["max_inflight"] = router.max_inflight
            if transformers_adapter is not None:
                asyncio.run(transformers_adapter.close())
                transformers_local_records.extend(
                    transformers_adapter.invocation_records
                )
                transformers_adapter = None
        else:
            callable_started_perf = time.perf_counter()
            result = func(**bound_kwargs)
            callable_execute_ms = qwen_smoke._elapsed_ms(  # noqa: SLF001
                callable_started_perf
            )
            inference_summary = None
        expected_outputs = tuple(str(item) for item in task_payload["expected_outputs"])  # type: ignore[index]
        if not isinstance(result, dict):
            raise RayBaselineError("task returned a non-dict result")
        if tuple(sorted(result)) != tuple(sorted(expected_outputs)):
            raise RayBaselineError(
                "task output names mismatch: "
                f"expected={sorted(expected_outputs)} actual={sorted(result)}"
            )
        timing = task_timing("succeeded", None)
        return {
            "status": "succeeded",
            "outputs": result,
            "inference_records": inference_records,
            "transformers_local_records": transformers_local_records,
            "inference_summary": inference_summary,
            "duration_ms": timing["task_total_ms"],
            "worker_pid": os.getpid(),
            "worker_node_id": worker_node_id,
            "worker_node_ip": worker_node_ip,
            "task_timing": timing,
        }
    except Exception as exc:
        if transformers_adapter is not None:
            try:
                asyncio.run(transformers_adapter.close())
            except Exception:
                pass
            finally:
                transformers_local_records.extend(
                    transformers_adapter.invocation_records
                )
        if callable_started_perf is not None and callable_execute_ms == 0:
            callable_execute_ms = qwen_smoke._elapsed_ms(  # noqa: SLF001
                callable_started_perf
            )
        error_code = (
            exc.error_code
            if hasattr(exc, "error_code") and isinstance(exc.error_code, str)
            else "ray_baseline_task_failed"
        )
        timing = task_timing("failed", error_code)
        return {
            "status": "failed",
            "error_code": error_code,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "inference_records": inference_records,
            "transformers_local_records": transformers_local_records,
            "duration_ms": timing["task_total_ms"],
            "worker_pid": os.getpid(),
            "worker_node_id": worker_node_id,
            "worker_node_ip": worker_node_ip,
            "task_timing": timing,
        }


def _resolve_task_kwargs(
    *,
    compiled: Any,
    node: Any,
    workflow_inputs: Mapping[str, object],
    task_outputs: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    from ascend_maze.compiler.ir import (
        DefaultBinding,
        LiteralBinding,
        OutputBinding,
        WorkflowInputBinding,
    )

    kwargs: dict[str, object] = {}
    for binding in node.inputs:
        if isinstance(binding, LiteralBinding):
            kwargs[binding.input_name] = _plain(binding.value)
        elif isinstance(binding, WorkflowInputBinding):
            kwargs[binding.input_name] = workflow_inputs[binding.workflow_input_name]
        elif isinstance(binding, OutputBinding):
            kwargs[binding.input_name] = task_outputs[binding.source_task_id][
                binding.source_output
            ]
        elif isinstance(binding, DefaultBinding):
            continue
        else:  # pragma: no cover - future IR guard.
            raise RayBaselineError(f"unsupported input binding: {type(binding).__name__}")
    definition = compiled.definitions[node.definition_id]
    missing_required = [
        name
        for name in definition.input_names
        if name not in kwargs and name not in definition.default_inputs
    ]
    if missing_required:
        raise RayBaselineError(
            f"task {node.task_name} is missing required inputs: {missing_required}"
        )
    return kwargs


def _task_payload(compiled: Any, node: Any) -> dict[str, object]:
    definition = compiled.definitions[node.definition_id]
    return {
        "task_id": node.task_id,
        "task_name": node.task_name,
        "definition_id": node.definition_id,
        "module": definition.module,
        "qualname": definition.qualname,
        "expected_outputs": definition.output_names,
        "task_kind": definition.task_kind,
    }


def _run_one_sample_ray(
    *,
    ray_task: Any,
    service_actor: Any,
    inference_backend: str,
    transformers_config: Mapping[str, object] | None,
    sample: Any,
    target_model_id: str,
    run_timeout_seconds: float,
    run_salt: str | None = None,
) -> dict[str, object]:
    import ray

    sample_started = time.perf_counter()
    started_ms = int(time.time() * 1000)
    run_id = (
        "ray-baseline-"
        + hashlib.sha256(
            f"{sample.sample_id}:{target_model_id}:{started_ms}:{run_salt or ''}".encode()
        ).hexdigest()[:20]
    )
    record: dict[str, object] = {
        "schema_version": 1,
        "sample": sample.manifest(),
        "target_model_id": target_model_id,
        "run_id": run_id,
        "started_at_ms": started_ms,
        "status": "not_started",
        "executor": "ray_task_actor_sequential_dag",
        "worker_max_calls": RAY_TASK_MAX_CALLS,
    }
    latency_metrics: dict[str, object] = {}
    client_e2e_started: float | None = None
    client_e2e_finished: float | None = None
    if run_salt is not None:
        record["run_salt"] = run_salt
    outstanding_ref: Any | None = None
    try:
        stage_started = time.perf_counter()
        workflow, model_aliases = qwen_smoke._build_workflow(  # noqa: SLF001
            sample.dataset,
            sample.workflow,
            target_model_id,
        )
        compiled = workflow.compile()
        task_id_by_name = {
            task.task_name: task_id
            for task_id, task in compiled.tasks.items_tuple()
        }
        record["workflow_fingerprint"] = compiled.workflow_fingerprint
        record["model_aliases"] = model_aliases
        record["task_id_by_name"] = task_id_by_name
        record["topological_order"] = list(compiled.topological_order)
        latency_metrics["prepare_ms"] = qwen_smoke._elapsed_ms(  # noqa: SLF001
            stage_started
        )

        task_outputs: dict[str, dict[str, object]] = {}
        task_records: list[dict[str, object]] = []
        task_timing_records: list[dict[str, object]] = []
        inference_records: list[dict[str, object]] = []
        transformers_local_records: list[dict[str, object]] = []
        deadline = time.monotonic() + run_timeout_seconds
        emit(
            "RAY_SAMPLE_START_JSON",
            {
                "sample_id": sample.sample_id,
                "family": sample.family,
                "target_model_id": target_model_id,
                "run_id": run_id,
            },
        )
        record["client_e2e_started_at_ms"] = int(time.time() * 1_000)
        client_e2e_started = time.perf_counter()
        for task_id in compiled.topological_order:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RayBaselineError("sample timed out before next task dispatch")
            node = compiled.tasks[task_id]
            kwargs = _resolve_task_kwargs(
                compiled=compiled,
                node=node,
                workflow_inputs=sample.inputs,
                task_outputs=task_outputs,
            )
            route_payload = None
            if node.model_anchor is not None:
                route_payload = {
                    "route_lease_id": f"{run_id}-{task_id}-route",
                    "run_id": run_id,
                    "model_id": node.model_anchor.model,
                    "instance_id": f"ray-baseline-{sample.family}-instance",
                    "endpoint_id": (
                        "ray-task://transformers-local"
                        if inference_backend == "transformers"
                        else "ray-actor://vllm"
                    ),
                    "deadline_ms": max(1, int(remaining * 1_000)),
                }
            payload = _task_payload(compiled, node)
            dispatch_started_wall_ns = time.time_ns()
            dispatch_started_perf = time.perf_counter()
            outstanding_ref = ray_task.remote(
                task_payload=payload,
                kwargs=kwargs,
                route_payload=route_payload,
                service_actor=service_actor if route_payload is not None else None,
                inference_backend=inference_backend,
                transformers_config=(
                    transformers_config if route_payload is not None else None
                ),
                dispatch_started_wall_ns=dispatch_started_wall_ns,
            )
            dispatch_prepare_ms = qwen_smoke._elapsed_ms(  # noqa: SLF001
                dispatch_started_perf
            )
            result = ray.get(outstanding_ref, timeout=remaining)
            ray_roundtrip_ms = qwen_smoke._elapsed_ms(  # noqa: SLF001
                dispatch_started_perf
            )
            outstanding_ref = None
            task_timing = dict(result.get("task_timing", {}))
            task_timing.update(
                {
                    "dispatch_id": f"{run_id}-{task_id}",
                    "run_id": run_id,
                    "task_id": task_id,
                    "task_name": node.task_name,
                    "attempt": 1,
                    "task_kind": payload["task_kind"],
                    "execution_target": (
                        "model_service"
                        if node.model_anchor is not None
                        else "local_worker"
                    ),
                    "route_lease_id": (
                        None
                        if route_payload is None
                        else route_payload["route_lease_id"]
                    ),
                    "worker_node_id": result.get("worker_node_id"),
                    "worker_node_ip": result.get("worker_node_ip"),
                    "dispatch_prepare_ms": dispatch_prepare_ms,
                    "ray_roundtrip_ms": ray_roundtrip_ms,
                }
            )
            worker_startup_ms = int(task_timing.get("worker_startup_ms", 0))
            task_total_ms = int(task_timing.get("task_total_ms", 0))
            task_timing["output_put_ms"] = max(
                0,
                ray_roundtrip_ms - worker_startup_ms - task_total_ms,
            )
            task_timing["output_put_scope"] = (
                "ray_result_serialization_and_transfer_upper_bound"
            )
            task_timing_records.append(task_timing)
            task_record = {
                "task_id": task_id,
                "task_name": node.task_name,
                "definition_id": node.definition_id,
                "model_anchor": None
                if node.model_anchor is None
                else {
                    "model": node.model_anchor.model,
                    "mode": node.model_anchor.mode,
                },
                "status": result["status"],
                "duration_ms": result.get("duration_ms"),
                "worker_pid": result.get("worker_pid"),
                "worker_node_id": result.get("worker_node_id"),
                "worker_node_ip": result.get("worker_node_ip"),
                "inference_summary": result.get("inference_summary"),
            }
            if result["status"] != "succeeded":
                task_record["error_code"] = result.get("error_code")
                task_record["error"] = result.get("error")
                task_record["traceback"] = result.get("traceback")
                task_records.append(task_record)
                record["status"] = "failed:task"
                record["failure"] = task_record
                break
            outputs = dict(result["outputs"])
            task_outputs[task_id] = outputs
            task_record["output_names"] = sorted(outputs)
            task_transformers_records = result.get("transformers_local_records", [])
            if task_transformers_records:
                task_record["transformers_local_records"] = task_transformers_records
            task_records.append(task_record)
            inference_records.extend(result.get("inference_records", []))
            transformers_local_records.extend(task_transformers_records)
        else:
            record["status"] = "succeeded"
        client_e2e_finished = time.perf_counter()
        record["client_e2e_finished_at_ms"] = int(time.time() * 1_000)

        record["tasks"] = task_records
        record["task_timing_records"] = task_timing_records
        record["task_timing_summary"] = qwen_smoke._task_timing_summary(  # noqa: SLF001
            task_timing_records
        )
        record["inference_records"] = inference_records
        record["transformers_local_records"] = transformers_local_records
        latency_metrics["model_request_ms"] = qwen_smoke._model_request_ms(  # noqa: SLF001
            inference_records
        )
        record["task_results"] = {
            compiled.tasks[task_id].task_name: outputs
            for task_id, outputs in sorted(
                task_outputs.items(),
                key=lambda item: compiled.tasks[item[0]].task_name,
            )
        }
    except Exception as exc:
        if outstanding_ref is not None:
            try:
                ray.cancel(outstanding_ref, force=True)
            except Exception:
                pass
        record["status"] = "unexpected_exception"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["traceback"] = traceback.format_exc()
        emit("RAY_SAMPLE_EXCEPTION_TRACEBACK", record["traceback"])
    finally:
        record["finished_at_ms"] = int(time.time() * 1000)
        record["duration_ms"] = int(record["finished_at_ms"]) - started_ms
        latency_metrics["total_sample_ms"] = qwen_smoke._elapsed_ms(  # noqa: SLF001
            sample_started
        )
        if client_e2e_started is not None:
            end = client_e2e_finished or time.perf_counter()
            record.setdefault("client_e2e_finished_at_ms", int(time.time() * 1_000))
            client_e2e_ms = max(0, int((end - client_e2e_started) * 1_000))
            latency_metrics["client_e2e_ms"] = client_e2e_ms
            model_ms = latency_metrics.get("model_request_ms")
            if isinstance(model_ms, int):
                latency_metrics["client_e2e_minus_model_ms"] = (
                    client_e2e_ms - model_ms
                )
        record["latency_metrics"] = latency_metrics
        emit(
            "RAY_SAMPLE_RESULT_JSON",
            {
                "sample_id": sample.sample_id,
                "status": record["status"],
                "duration_ms": record["duration_ms"],
            },
        )
    return record


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run migrated GAIA/OpenAGI/tau-bench workflows through a plain Ray "
            "task/actor correctness baseline."
        )
    )
    parser.add_argument("--data-root", type=Path, default=qwen_smoke.DEFAULT_DATA_ROOT)
    parser.add_argument(
        "--text-model-path",
        type=Path,
        default=qwen_smoke.DEFAULT_TEXT_MODEL_PATH,
    )
    parser.add_argument(
        "--vision-model-path",
        type=Path,
        default=qwen_smoke.DEFAULT_VISION_MODEL_PATH,
    )
    parser.add_argument("--python-executable", type=Path, default=qwen_smoke._default_python())  # noqa: SLF001
    parser.add_argument("--device-id", default="0")
    parser.add_argument(
        "--inference-backend",
        choices=("vllm", "transformers"),
        default="vllm",
        help=(
            "Inference backend for model tasks. 'transformers' is a cold-load "
            "text/vision path that loads the model inside every Ray task chat() call."
        ),
    )
    parser.add_argument(
        "--dataset",
        action="append",
        choices=("gaia", "openagi", "tbench"),
        default=[],
    )
    parser.add_argument(
        "--workflow",
        action="append",
        default=[],
        help="Workflow selector, e.g. gaia.reason, document_qa, or tbench.",
    )
    parser.add_argument(
        "--family",
        action="append",
        choices=("text", "vision"),
        default=[],
    )
    parser.add_argument("--samples-per-workflow", type=int, default=1)
    parser.add_argument("--sample-offset", type=int, default=0)
    parser.add_argument("--max-inline-file-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--text-max-model-len", type=int, default=10240)
    parser.add_argument("--vision-max-model-len", type=int, default=12288)
    parser.add_argument(
        "--text-dtype",
        choices=("bfloat16", "float16"),
        default="bfloat16",
    )
    parser.add_argument(
        "--vision-dtype",
        choices=("bfloat16", "float16"),
        default="bfloat16",
    )
    parser.add_argument("--text-gpu-memory-utilization", type=float, default=0.5)
    parser.add_argument("--vision-gpu-memory-utilization", type=float, default=0.7)
    parser.add_argument("--max-num-seqs", type=int, default=1)
    parser.add_argument("--text-max-num-batched-tokens", type=int, default=None)
    parser.add_argument("--vision-max-num-batched-tokens", type=int, default=4096)
    parser.add_argument("--startup-timeout-ms", type=int, default=600_000)
    parser.add_argument("--request-timeout-ms", type=int, default=180_000)
    parser.add_argument("--run-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--first-port", type=int, default=31440)
    parser.add_argument("--last-port", type=int, default=31520)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--vision-trust-remote-code",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--text-trust-remote-code",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--ray-address", default=None)
    parser.add_argument("--ray-task-num-cpus", type=float, default=1.0)
    parser.add_argument("--ray-namespace", default="ascend-maze-ray-baseline")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Default: experiments/ray_baseline_smoke/run-<timestamp>",
    )
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--allow-sample-failures", action="store_true")
    parser.add_argument(
        "--tbench-smoke-overrides",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--gaia-file-smoke-summary",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser.parse_args(argv)


def _validate_args(args: argparse.Namespace) -> None:
    if args.samples_per_workflow < 1:
        raise SystemExit("--samples-per-workflow must be positive")
    if args.sample_offset < 0:
        raise SystemExit("--sample-offset must be non-negative")
    if args.max_inline_file_bytes < 1:
        raise SystemExit("--max-inline-file-bytes must be positive")
    for name in ("text_max_model_len", "vision_max_model_len", "max_num_seqs"):
        if getattr(args, name) < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    for name in ("text_gpu_memory_utilization", "vision_gpu_memory_utilization"):
        value = getattr(args, name)
        if not 0 < value <= 0.9:
            raise SystemExit(f"--{name.replace('_', '-')} must be within (0, 0.9]")
    for name in ("text_max_num_batched_tokens", "vision_max_num_batched_tokens"):
        value = getattr(args, name)
        if value is not None and value < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    if args.first_port > args.last_port:
        raise SystemExit("--first-port cannot exceed --last-port")
    if args.run_timeout_seconds <= 0:
        raise SystemExit("--run-timeout-seconds must be positive")
    if args.ray_task_num_cpus < 0:
        raise SystemExit("--ray-task-num-cpus must be non-negative")


def _discover(args: argparse.Namespace) -> tuple[list[Any], list[Any]]:
    return qwen_smoke.discover_samples(
        data_root=args.data_root,
        datasets=set(args.dataset),
        workflows=set(args.workflow),
        families=set(args.family),
        samples_per_workflow=int(args.samples_per_workflow),
        sample_offset=int(args.sample_offset),
        max_inline_file_bytes=int(args.max_inline_file_bytes),
        tbench_smoke_overrides=bool(args.tbench_smoke_overrides),
        gaia_file_smoke_summary=bool(args.gaia_file_smoke_summary),
    )


def _build_plan(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    samples: list[Any],
    discovery_failures: list[Any],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "objective": RAY_BASELINE_OBJECTIVE,
        "executor": {
            "kind": "plain_ray_task_actor",
            "dag_policy": "sequential_topological_order",
            "worker_max_calls": RAY_TASK_MAX_CALLS,
            "uses_ascend_maze_controller": False,
            "uses_ascend_maze_scheduler": False,
            "uses_ascend_maze_runtime_client": False,
        },
        "data_root": str(args.data_root),
        "output_dir": str(output_dir),
        "inference_backend": str(args.inference_backend),
        "samples_per_workflow": int(args.samples_per_workflow),
        "sample_offset": int(args.sample_offset),
        "samples": [sample.manifest() for sample in samples],
        "discovery_failures": discovery_failures,
        "text_model": {
            "model_id": qwen_smoke.TEXT_MODEL_ID,
            "path": str(args.text_model_path),
            "dtype": str(args.text_dtype),
            "max_model_len": int(args.text_max_model_len),
            "max_num_batched_tokens": args.text_max_num_batched_tokens,
            "launch_options": _launch_options_for_family("text"),
        },
        "vision_model": {
            "model_id": qwen_smoke.VISION_MODEL_ID,
            "path": str(args.vision_model_path),
            "dtype": str(args.vision_dtype),
            "max_model_len": int(args.vision_max_model_len),
            "max_num_batched_tokens": args.vision_max_num_batched_tokens,
            "vision_mode": "true_multimodal",
            "launch_options": _launch_options_for_family("vision"),
        },
        "ray": {
            "address": args.ray_address,
            "task_num_cpus": float(args.ray_task_num_cpus),
            "namespace": str(args.ray_namespace),
        },
        "tbench_smoke_overrides": bool(args.tbench_smoke_overrides),
        "gaia_file_smoke_summary": bool(args.gaia_file_smoke_summary),
    }


def _preflight_failed(
    *,
    output_dir: Path,
    samples: list[Any],
    discovery_failures: list[Any],
    message: str,
    extra: Mapping[str, object] | None = None,
) -> int:
    payload: dict[str, object] = {
        "schema_version": 1,
        "result": "preflight_failed",
        "message": message,
        "sample_count": len(samples),
        "discovery_failure_count": len(discovery_failures),
        "output_dir": str(output_dir),
    }
    if extra:
        payload.update(dict(extra))
    _write_json(output_dir / "summary.json", payload)
    _append_jsonl(
        output_dir / "preflight_failures.jsonl",
        {
            "event": "preflight_failed",
            "message": message,
            "sample_count": len(samples),
            "discovery_failure_count": len(discovery_failures),
        },
    )
    emit("RAY_BASELINE_PREFLIGHT_FAILED", message)
    return 2


def _run_preflight(
    *,
    args: argparse.Namespace,
    families_present: set[str],
) -> dict[str, object]:
    if not args.python_executable.is_file():
        raise qwen_smoke.SmokePreflightError(
            f"python executable does not exist: {args.python_executable}"
        )
    model_artifacts: list[dict[str, object]] = []
    if "text" in families_present:
        model_artifacts.append(qwen_smoke.validate_model_artifact(args.text_model_path))
    if "vision" in families_present:
        model_artifacts.append(qwen_smoke.validate_model_artifact(args.vision_model_path))

    module_set = (
        tuple((*qwen_smoke.TRANSFORMERS_LOCAL_MODULES, "ray"))
        if args.inference_backend == "transformers"
        else qwen_smoke.VLLM_MODULES
    )
    current_modules = qwen_smoke.check_current_python_modules(module_set)
    service_modules = qwen_smoke.check_service_python_modules(
        args.python_executable,
        module_set,
    )

    from ascend_maze.ascend.discovery import (
        discover_aicpu_runtime_library_paths,
        discover_ascend_environment,
        discover_atb_runtime_library_preloads,
    )
    from ascend_maze.ascend.dcmi import DcmiDeviceAdapter

    device_adapter = DcmiDeviceAdapter()
    devices = qwen_smoke._device_summary(device_adapter)  # noqa: SLF001
    environment = discover_ascend_environment(device_adapter)
    preloads = dict(discover_atb_runtime_library_preloads())
    runtime_paths = discover_aicpu_runtime_library_paths()
    if args.inference_backend == "vllm" and not preloads:
        raise qwen_smoke.SmokePreflightError(
            "ATB runtime preload libmki.so was not found"
        )
    if args.inference_backend == "vllm" and not runtime_paths:
        raise qwen_smoke.SmokePreflightError(
            "AICPU runtime library paths were not found"
        )
    return {
        "model_artifacts": model_artifacts,
        "current_python_modules": current_modules,
        "service_python_modules": service_modules,
        "initial_devices": devices,
        "environment_fingerprint": environment.environment_fingerprint,
        "environment_versions": dict(environment.versions.items_tuple()),
        "runtime_preloads": preloads,
        "runtime_library_paths": runtime_paths,
    }


def _family_service_config(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    family: str,
    port: int,
    preflight: Mapping[str, object],
) -> dict[str, object]:
    is_vision = family == "vision"
    model_path = args.vision_model_path if is_vision else args.text_model_path
    model_id = qwen_smoke.VISION_MODEL_ID if is_vision else qwen_smoke.TEXT_MODEL_ID
    launch_options = _launch_options_for_family(family)
    return {
        "family": family,
        "model_id": model_id,
        "model_path": str(model_path),
        "python_executable": str(args.python_executable),
        "device_id": str(args.device_id),
        "port": int(port),
        "dtype": str(args.vision_dtype if is_vision else args.text_dtype),
        "max_model_len": int(
            args.vision_max_model_len if is_vision else args.text_max_model_len
        ),
        "gpu_memory_utilization": float(
            args.vision_gpu_memory_utilization
            if is_vision
            else args.text_gpu_memory_utilization
        ),
        "max_num_seqs": int(args.max_num_seqs),
        "max_num_batched_tokens": (
            args.vision_max_num_batched_tokens
            if is_vision
            else args.text_max_num_batched_tokens
        ),
        "trust_remote_code": bool(
            args.vision_trust_remote_code if is_vision else args.text_trust_remote_code
        ),
        "startup_timeout_ms": int(args.startup_timeout_ms),
        "request_timeout_ms": int(args.request_timeout_ms),
        "log_level": str(args.log_level),
        "runtime_preloads": dict(preflight["runtime_preloads"]),  # type: ignore[arg-type]
        "runtime_library_paths": tuple(preflight["runtime_library_paths"]),  # type: ignore[arg-type]
        "log_path": str(output_dir / "logs" / f"{family}_vllm" / "service.log"),
        **launch_options,
    }


def _family_transformers_config(
    *,
    args: argparse.Namespace,
    family: str,
    preflight: Mapping[str, object],
) -> dict[str, object]:
    is_vision = family == "vision"
    model_id = qwen_smoke.VISION_MODEL_ID if is_vision else qwen_smoke.TEXT_MODEL_ID
    model_path = args.vision_model_path if is_vision else args.text_model_path
    config: dict[str, object] = {
        "family": family,
        "model_id": model_id,
        "model_path": str(model_path),
        "tokenizer_path": str(model_path),
        "device_id": str(args.device_id),
        "dtype": str(args.vision_dtype if is_vision else args.text_dtype),
        "generation_method": "manual_greedy",
        "model_kind": "vision_language" if is_vision else "text",
        "max_model_len": int(
            args.vision_max_model_len if is_vision else args.text_max_model_len
        ),
        "trust_remote_code": bool(
            args.vision_trust_remote_code if is_vision else args.text_trust_remote_code
        ),
        "enable_thinking": False,
        "request_timeout_ms": int(args.request_timeout_ms),
        "runtime_library_paths": tuple(preflight["runtime_library_paths"]),  # type: ignore[arg-type]
    }
    if is_vision:
        config["qwen2_5_vl_cpu_unique_consecutive_workaround"] = True
    return config


def _run_family_ray(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    family: str,
    samples: list[Any],
    service_actor_cls: Any,
    ray_task: Any,
    port: int,
    preflight: Mapping[str, object],
) -> dict[str, object]:
    import ray

    target_model_id = qwen_smoke.VISION_MODEL_ID if family == "vision" else qwen_smoke.TEXT_MODEL_ID
    records_path = output_dir / f"{family}_records.jsonl"
    failures_path = output_dir / f"{family}_failures.jsonl"
    service_config = (
        _family_service_config(
            args=args,
            output_dir=output_dir,
            family=family,
            port=port,
            preflight=preflight,
        )
        if args.inference_backend == "vllm"
        else None
    )
    transformers_config = (
        _family_transformers_config(args=args, family=family, preflight=preflight)
        if args.inference_backend == "transformers"
        else None
    )
    summary: dict[str, object] = {
        "family": family,
        "inference_backend": str(args.inference_backend),
        "target_model_id": target_model_id,
        "sample_count": len(samples),
        "records_path": str(records_path),
        "failures_path": str(failures_path),
        "service_config": None
        if service_config is None
        else {
            key: value
            for key, value in service_config.items()
            if key not in {"runtime_preloads", "runtime_library_paths"}
        },
        "transformers_config": transformers_config,
        "status": "not_started",
    }
    service_actor = None
    succeeded = 0
    failed = 0
    cleanup_errors: list[str] = []
    try:
        if args.inference_backend == "vllm":
            assert service_config is not None
            service_actor = service_actor_cls.remote(service_config)
            start_info = ray.get(
                service_actor.start.remote(),
                timeout=int(args.startup_timeout_ms / 1_000) + 60,
            )
            summary["service_start"] = start_info
            emit("RAY_SERVICE_START_JSON", start_info)
        for sample in samples:
            record = _run_one_sample_ray(
                ray_task=ray_task,
                service_actor=service_actor,
                inference_backend=str(args.inference_backend),
                transformers_config=transformers_config,
                sample=sample,
                target_model_id=target_model_id,
                run_timeout_seconds=float(args.run_timeout_seconds),
            )
            if _persist_sample_record(
                records_path=records_path,
                failures_path=failures_path,
                record=record,
            ):
                succeeded += 1
            else:
                failed += 1
    finally:
        if service_actor is not None:
            try:
                stop_info = ray.get(service_actor.stop.remote(), timeout=120)
                summary["service_stop"] = stop_info
                emit("RAY_SERVICE_STOP_JSON", stop_info)
            except Exception as exc:
                cleanup_errors.append(f"service_stop:{type(exc).__name__}:{exc}")
                emit("RAY_SERVICE_STOP_ERROR", traceback.format_exc())
            try:
                ray.kill(service_actor, no_restart=True)
            except Exception:
                pass
    summary.update(
        {
            "status": "completed",
            "succeeded": succeeded,
            "failed": failed,
            "cleanup_errors": cleanup_errors,
            "service_log_tails": (
                qwen_smoke._tail_logs(output_dir / "logs" / f"{family}_vllm")  # noqa: SLF001
                if failed or cleanup_errors
                else {}
            ),
        }
    )
    _write_json(output_dir / f"{family}_summary.json", summary)
    return summary


def run_baseline(args: argparse.Namespace) -> int:
    _install_repo_path()
    output_dir = (
        args.output_dir.expanduser().resolve(strict=False)
        if args.output_dir is not None
        else DEFAULT_OUTPUT_ROOT / f"run-{int(time.time())}"
    )
    args.data_root = args.data_root.expanduser().resolve(strict=False)
    args.text_model_path = args.text_model_path.expanduser().resolve(strict=False)
    args.vision_model_path = args.vision_model_path.expanduser().resolve(strict=False)
    args.python_executable = args.python_executable.expanduser().resolve(strict=False)

    samples, discovery_failures = _discover(args)
    plan = _build_plan(
        args=args,
        output_dir=output_dir,
        samples=samples,
        discovery_failures=discovery_failures,
    )
    _write_json(output_dir / "plan.json", plan)
    emit("RAY_BASELINE_PLAN_PATH", str(output_dir / "plan.json"))
    emit(
        "RAY_BASELINE_PLAN_JSON",
        {
            "sample_count": len(samples),
            "discovery_failure_count": len(discovery_failures),
            "families": sorted({sample.family for sample in samples}),
        },
    )
    for failure in discovery_failures:
        _append_jsonl(output_dir / "discovery_failures.jsonl", failure)

    if args.plan_only:
        _write_json(
            output_dir / "summary.json",
            {
                "schema_version": 1,
                "result": "plan_only_succeeded",
                "sample_count": len(samples),
                "discovery_failure_count": len(discovery_failures),
                "output_dir": str(output_dir),
            },
        )
        emit("RAY_BASELINE_RESULT", "plan_only_succeeded")
        return 0

    if not samples:
        return _preflight_failed(
            output_dir=output_dir,
            samples=samples,
            discovery_failures=discovery_failures,
            message="sample discovery produced no runnable samples",
        )

    families_present = {sample.family for sample in samples}
    try:
        preflight = _run_preflight(args=args, families_present=families_present)
    except qwen_smoke.SmokePreflightError as exc:
        return _preflight_failed(
            output_dir=output_dir,
            samples=samples,
            discovery_failures=discovery_failures,
            message=str(exc),
        )
    except Exception as exc:
        return _preflight_failed(
            output_dir=output_dir,
            samples=samples,
            discovery_failures=discovery_failures,
            message=f"{type(exc).__name__}: {exc}",
            extra={"traceback": traceback.format_exc()},
        )

    if args.check_only:
        _write_json(
            output_dir / "summary.json",
            {
                "schema_version": 1,
                "result": "check_only_succeeded",
                "sample_count": len(samples),
                "discovery_failure_count": len(discovery_failures),
                "output_dir": str(output_dir),
                **preflight,
            },
        )
        emit("RAY_BASELINE_RESULT", "check_only_succeeded")
        return 0

    if args.inference_backend == "transformers":
        qwen_smoke._prepend_env_paths(  # noqa: SLF001
            "LD_LIBRARY_PATH",
            tuple(preflight["runtime_library_paths"]),  # type: ignore[arg-type]
        )

    import ray

    summaries: list[dict[str, object]] = []
    result_code = 0
    cleanup_errors: list[str] = []
    required_model_paths: list[Path] = []
    if "text" in families_present:
        required_model_paths.append(args.text_model_path)
    if "vision" in families_present:
        required_model_paths.append(args.vision_model_path)

    try:
        ray.init(
            address=args.ray_address,
            ignore_reinit_error=True,
            include_dashboard=False,
            namespace=str(args.ray_namespace),
            runtime_env={
                "env_vars": {
                    "PYTHONPATH": os.environ["PYTHONPATH"],
                    "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH", ""),
                }
            },
        )
        service_actor_cls = (
            ray.remote(num_cpus=0)(_VllmServiceActor)
            if args.inference_backend == "vllm"
            else None
        )
        ray_task = ray.remote(
            num_cpus=float(args.ray_task_num_cpus),
            max_calls=RAY_TASK_MAX_CALLS,
        )(
            _execute_workflow_task_remote
        )
        port_by_family = {"text": int(args.first_port), "vision": int(args.first_port) + 1}
        if "vision" in families_present and port_by_family["vision"] > int(args.last_port):
            raise RayBaselineError("port range needs at least two ports for text+vision")
        for family in ("text", "vision"):
            family_samples = [sample for sample in samples if sample.family == family]
            if not family_samples:
                continue
            emit(
                "RAY_FAMILY_START_JSON",
                {
                    "family": family,
                    "inference_backend": str(args.inference_backend),
                    "sample_count": len(family_samples),
                    "model_path": str(
                        args.vision_model_path
                        if family == "vision"
                        else args.text_model_path
                    ),
                },
            )
            summaries.append(
                _run_family_ray(
                    args=args,
                    output_dir=output_dir,
                    family=family,
                    samples=family_samples,
                    service_actor_cls=service_actor_cls,
                    ray_task=ray_task,
                    port=port_by_family[family],
                    preflight=preflight,
                )
            )
    except Exception:
        emit("RAY_BASELINE_EXCEPTION_TRACEBACK", traceback.format_exc())
        result_code = 99
    finally:
        try:
            ray.shutdown()
        except Exception as exc:
            cleanup_errors.append(f"ray_shutdown:{type(exc).__name__}:{exc}")

    owned_process_group_ids = tuple(
        int(service_start["pid"])
        for summary in summaries
        if isinstance((service_start := summary.get("service_start")), dict)
        and service_start.get("pid") is not None
    )
    residual = qwen_smoke._residual_vllm_processes(  # noqa: SLF001
        required_model_paths,
        tuple(range(int(args.first_port), int(args.last_port) + 1)),
        owned_process_group_ids=owned_process_group_ids,
    )
    emit("RAY_FINAL_RESIDUAL_VLLM_PROCESSES_JSON", residual)
    total_failed = sum(int(summary.get("failed", 0)) for summary in summaries)
    total_succeeded = sum(int(summary.get("succeeded", 0)) for summary in summaries)
    if result_code == 0 and total_failed and not args.allow_sample_failures:
        result_code = 20
    if result_code == 0 and (residual or cleanup_errors):
        result_code = 11
    summary_payload = {
        "schema_version": 1,
        "result": (
            "succeeded"
            if result_code == 0
            else (
                "sample_failures"
                if result_code == 20
                else "cleanup_failed"
                if result_code == 11
                else f"failed:{result_code}"
            )
        ),
        "exit_code": result_code,
        "sample_count": len(samples),
        "succeeded": total_succeeded,
        "failed": total_failed,
        "discovery_failure_count": len(discovery_failures),
        "families": summaries,
        "residual_vllm_processes": residual,
        "cleanup_errors": cleanup_errors,
        "output_dir": str(output_dir),
    }
    _write_json(output_dir / "summary.json", summary_payload)
    emit("RAY_BASELINE_SUMMARY_PATH", str(output_dir / "summary.json"))
    emit("RAY_BASELINE_SUMMARY_JSON", summary_payload)
    emit("RAY_BASELINE_EXIT_CODE", result_code)
    return result_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _validate_args(args)
    return run_baseline(args)


if __name__ == "__main__":
    raise SystemExit(main())
