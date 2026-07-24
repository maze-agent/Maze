"""Local Transformers inference adapter used for cold-load benchmark runs.

This adapter deliberately keeps the C11 model-service state machine intact while
avoiding a long-lived vLLM process.  A model instance becomes READY after a
no-op warmup, and every ``chat()`` call loads the model with Transformers,
generates once, then releases the Python references again.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import gc
import os
from threading import RLock
import time
from typing import Any, cast

from ascend_maze.contracts.resources import PlacementLease
from ascend_maze.core.canonical import FrozenMap
from ascend_maze.core.errors import ContractValidationError
from ascend_maze.core.identifiers import new_id
from ascend_maze.inference.contracts import (
    ChatRequest,
    ChatResponse,
    EngineMetrics,
    EngineProbe,
    InferenceCallError,
    InferenceWorkerConfig,
    ModelRouteContext,
    ModelSpec,
    PortLease,
    ServiceHandle,
    ServiceLaunchRequest,
    ServiceProcessProbe,
    ServiceStopResult,
    WarmupResult,
)


@dataclass(frozen=True, slots=True)
class TransformersLocalGenerationConfig:
    model_path: str
    tokenizer_path: str
    dtype: str
    max_model_len: int
    device_id: str
    trust_remote_code: bool = False
    enable_thinking: bool = False
    runtime_library_paths: tuple[str, ...] = ()
    generation_method: str = "generate"
    model_kind: str = "text"
    qwen2_5_vl_cpu_unique_consecutive_workaround: bool = False

    def __post_init__(self) -> None:
        if not self.model_path:
            raise ContractValidationError("model_path is required")
        if not self.tokenizer_path:
            raise ContractValidationError("tokenizer_path is required")
        if self.dtype not in {"bfloat16", "float16"}:
            raise ContractValidationError(
                "transformers_local only supports bfloat16/float16"
            )
        if (
            isinstance(self.max_model_len, bool)
            or not isinstance(self.max_model_len, int)
            or self.max_model_len < 1
        ):
            raise ContractValidationError("max_model_len must be positive")
        if not isinstance(self.device_id, str) or not self.device_id:
            raise ContractValidationError("device_id is required")
        if not isinstance(self.trust_remote_code, bool) or not isinstance(
            self.enable_thinking, bool
        ):
            raise ContractValidationError("boolean generation flags are invalid")
        if not isinstance(self.runtime_library_paths, tuple) or any(
            not isinstance(item, str) or not item for item in self.runtime_library_paths
        ):
            raise ContractValidationError(
                "runtime_library_paths must be a tuple of strings"
            )
        if not isinstance(self.generation_method, str) or self.generation_method not in {
            "generate",
            "manual_greedy",
        }:
            raise ContractValidationError(
                "generation_method must be generate or manual_greedy"
            )
        if not isinstance(self.model_kind, str) or self.model_kind not in {
            "text",
            "vision_language",
        }:
            raise ContractValidationError(
                "model_kind must be text or vision_language"
            )
        if not isinstance(
            self.qwen2_5_vl_cpu_unique_consecutive_workaround, bool
        ):
            raise ContractValidationError(
                "qwen2_5_vl_cpu_unique_consecutive_workaround must be a boolean"
            )


class TransformersLocalInferenceEngineAdapter:
    """C11 adapter whose actual model load happens inside each chat call."""

    name = "transformers_local"

    def __init__(self) -> None:
        self._handles: dict[str, ServiceHandle] = {}
        self._handles_by_endpoint: dict[str, ServiceHandle] = {}
        self._specs_by_endpoint: dict[str, ModelSpec] = {}
        self._inflight_by_endpoint: dict[str, int] = {}
        self._invocation_records: list[dict[str, object]] = []
        self._lock = RLock()

    def validate_model_spec(self, spec: ModelSpec) -> None:
        if spec.backend != self.name:
            raise ContractValidationError(
                "TransformersLocal adapter only accepts backend='transformers_local'"
            )
        if spec.dtype not in {"bfloat16", "float16"}:
            raise ContractValidationError(
                "transformers_local only supports bfloat16/float16"
            )
        if spec.quantization is not None:
            raise ContractValidationError(
                "transformers_local benchmark backend does not support quantization"
            )
        if spec.tensor_parallel_size != 1:
            raise ContractValidationError(
                "transformers_local requires tensor_parallel_size=1"
            )
        if spec.request_capacity != 1:
            raise ContractValidationError(
                "transformers_local cold-load benchmark requires request_capacity=1"
            )
        allowed = {
            "device_id",
            "enable_thinking",
            "generation_method",
            "model_kind",
            "qwen2_5_vl_cpu_unique_consecutive_workaround",
            "request_timeout_ms",
            "runtime_library_paths",
            "trust_remote_code",
        }
        unknown = {str(key) for key in spec.launch_options} - allowed
        if unknown:
            raise ContractValidationError(
                "unsupported transformers_local launch options: "
                + ", ".join(sorted(unknown))
            )
        for name in ("trust_remote_code", "enable_thinking"):
            value = spec.launch_options.get(name)
            if value is not None and not isinstance(value, bool):
                raise ContractValidationError(f"{name} must be a boolean")
        request_timeout_ms = spec.launch_options.get("request_timeout_ms")
        if request_timeout_ms is not None and (
            isinstance(request_timeout_ms, bool)
            or not isinstance(request_timeout_ms, int)
            or request_timeout_ms < 1
        ):
            raise ContractValidationError("request_timeout_ms must be positive")
        device_id = spec.launch_options.get("device_id")
        if device_id is not None and not isinstance(device_id, str):
            raise ContractValidationError("device_id must be a string")
        runtime_library_paths = spec.launch_options.get("runtime_library_paths")
        if runtime_library_paths is not None and (
            not isinstance(runtime_library_paths, tuple)
            or any(
                not isinstance(item, str) or not item
                for item in runtime_library_paths
            )
        ):
            raise ContractValidationError(
                "runtime_library_paths must be a tuple of strings"
            )
        generation_method = spec.launch_options.get("generation_method")
        if generation_method is not None and generation_method not in {
            "generate",
            "manual_greedy",
        }:
            raise ContractValidationError(
                "generation_method must be generate or manual_greedy"
            )
        model_kind = spec.launch_options.get("model_kind", "text")
        if not isinstance(model_kind, str) or model_kind not in {
            "text",
            "vision_language",
        }:
            raise ContractValidationError(
                "model_kind must be text or vision_language"
            )
        workaround = spec.launch_options.get(
            "qwen2_5_vl_cpu_unique_consecutive_workaround", False
        )
        if not isinstance(workaround, bool):
            raise ContractValidationError(
                "qwen2_5_vl_cpu_unique_consecutive_workaround must be a boolean"
            )

    def worker_config(
        self,
        spec: ModelSpec,
        *,
        instance_placement_lease_id: str,
        npu_device_id: str,
    ) -> InferenceWorkerConfig:
        return InferenceWorkerConfig(
            adapter_name=self.name,
            instance_placement_lease_id=instance_placement_lease_id,
            request_timeout_ms=cast(
                int,
                spec.launch_options.get("request_timeout_ms", 180_000),
            ),
            adapter_options=FrozenMap(
                (
                    ("model_path", spec.artifact_path),
                    ("tokenizer_path", spec.tokenizer_path or spec.artifact_path),
                    ("dtype", spec.dtype),
                    ("max_model_len", spec.max_model_len),
                    ("device_id", npu_device_id),
                    ("trust_remote_code", bool(spec.launch_options.get("trust_remote_code", False))),
                    ("enable_thinking", bool(spec.launch_options.get("enable_thinking", False))),
                    (
                        "generation_method",
                        str(spec.launch_options.get("generation_method", "generate")),
                    ),
                    ("model_kind", str(spec.launch_options.get("model_kind", "text"))),
                    (
                        "qwen2_5_vl_cpu_unique_consecutive_workaround",
                        bool(
                            spec.launch_options.get(
                                "qwen2_5_vl_cpu_unique_consecutive_workaround",
                                False,
                            )
                        ),
                    ),
                    (
                        "runtime_library_paths",
                        cast(
                            tuple[str, ...],
                            spec.launch_options.get("runtime_library_paths", ()),
                        ),
                    ),
                )
            ),
        )

    def build_launch_request(
        self,
        spec: ModelSpec,
        lease: PlacementLease,
        port_lease: PortLease,
    ) -> ServiceLaunchRequest:
        if lease.npu_device_id is None:
            raise ContractValidationError(
                "transformers_local instance lease requires a physical NPU"
            )
        endpoint_id = (
            "transformers-local://"
            f"{lease.node_id}/{port_lease.owner_instance_id}/{port_lease.generation}"
        )
        return ServiceLaunchRequest(
            instance_id=port_lease.owner_instance_id,
            generation=port_lease.generation,
            model_id=spec.model_id,
            artifact_revision=spec.artifact_revision,
            endpoint_id=endpoint_id,
            port_lease_id=port_lease.port_lease_id,
            port=port_lease.port,
            argv=("transformers-local-inprocess", spec.model_id),
            working_directory=None,
            environment=FrozenMap(
                (("ASCEND_RT_VISIBLE_DEVICES", lease.npu_device_id),)
            ),
        )

    async def launch(
        self,
        request: ServiceLaunchRequest,
        lease: PlacementLease,
    ) -> ServiceHandle:
        if lease.npu_device_id is None:
            raise ContractValidationError(
                "transformers_local launch requires a physical NPU"
            )
        handle = ServiceHandle(
            service_handle_id=new_id("service"),
            instance_id=request.instance_id,
            generation=request.generation,
            endpoint_id=request.endpoint_id,
            node_id=lease.node_id,
            boot_id=lease.boot_id,
            npu_device_id=lease.npu_device_id,
            process_id=os.getpid(),
            port_lease_id=request.port_lease_id,
            port=request.port,
        )
        with self._lock:
            self._handles[handle.service_handle_id] = handle
            self._handles_by_endpoint[handle.endpoint_id] = handle
            self._inflight_by_endpoint.setdefault(handle.endpoint_id, 0)
        return handle

    def attach_spec(self, handle: ServiceHandle, spec: ModelSpec) -> None:
        with self._lock:
            if handle.service_handle_id not in self._handles:
                raise RuntimeError("transformers_local service handle is not active")
            self._specs_by_endpoint[handle.endpoint_id] = spec

    async def probe(self, handle: ServiceHandle, spec: ModelSpec) -> EngineProbe:
        with self._lock:
            alive = handle.service_handle_id in self._handles
        return EngineProbe(
            process_alive=alive,
            model_id=spec.model_id,
            artifact_revision=spec.artifact_revision,
            environment_fingerprint=spec.environment_fingerprint,
            dtype=spec.dtype,
            quantization=spec.quantization,
            physical_device_id=handle.npu_device_id,
            process_hbm_mb=spec.weight_hbm_mb,
            request_capacity=spec.request_capacity,
        )

    async def warmup(self, handle: ServiceHandle, spec: ModelSpec) -> WarmupResult:
        del handle, spec
        return WarmupResult(
            succeeded=True,
            duration_ms=0,
            response_digest="transformers-local-no-warmup",
        )

    async def invoke_chat(
        self,
        context: ModelRouteContext,
        request: ChatRequest,
    ) -> ChatResponse:
        with self._lock:
            spec = self._specs_by_endpoint.get(context.endpoint_id)
            handle = self._handles_by_endpoint.get(context.endpoint_id)
            if spec is None or handle is None:
                raise RuntimeError("transformers_local endpoint is not active")
            self._inflight_by_endpoint[context.endpoint_id] += 1
        config = _generation_config_from_spec(spec, handle.npu_device_id)
        try:
            response, metrics = await asyncio.to_thread(
                generate_chat_once,
                config,
                request,
            )
            with self._lock:
                self._invocation_records.append(
                    {
                        "adapter": self.name,
                        "route_lease_id": context.route_lease_id,
                        "model_id": context.model_id,
                        "instance_id": context.instance_id,
                        "instance_generation": context.instance_generation,
                        **metrics,
                    }
                )
            return response
        finally:
            with self._lock:
                self._inflight_by_endpoint[context.endpoint_id] -= 1

    async def read_metrics(self, handle: ServiceHandle) -> EngineMetrics:
        with self._lock:
            inflight = self._inflight_by_endpoint.get(handle.endpoint_id, 0)
        return EngineMetrics(queue_depth=0, actual_request_inflight=inflight)

    async def probe_process(
        self,
        handle: ServiceHandle,
        *,
        timeout_ms: int,
    ) -> ServiceProcessProbe:
        del timeout_ms
        with self._lock:
            alive = handle.service_handle_id in self._handles
        return ServiceProcessProbe(
            process_alive=alive,
            port_open=alive,
            binding_verified=alive,
            physical_device_id=handle.npu_device_id,
            process_hbm_mb=0 if alive else None,
            exit_code=None if alive else 1,
        )

    async def stop(
        self,
        handle: ServiceHandle,
        *,
        timeout_ms: int,
    ) -> ServiceStopResult:
        del timeout_ms
        with self._lock:
            self._handles.pop(handle.service_handle_id, None)
            self._handles_by_endpoint.pop(handle.endpoint_id, None)
            self._specs_by_endpoint.pop(handle.endpoint_id, None)
            self._inflight_by_endpoint.pop(handle.endpoint_id, None)
        return ServiceStopResult(
            process_exited=True,
            port_released=True,
            hbm_recovered=True,
            exit_code=0,
            final_hbm_mb=0,
        )

    def invocation_records(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(dict(item) for item in self._invocation_records)


class TransformersLocalGenerationSession:
    """Load once for one Task and serve that Task's sequential chat calls."""

    def __init__(self, config: TransformersLocalGenerationConfig) -> None:
        self.config = config
        self._torch: Any | None = None
        self._device: Any | None = None
        self._model: Any | None = None
        self._tokenizer: Any | None = None
        self._processor: Any | None = None
        self._logical_device_id: str | None = None
        self._loaded = False
        self._closed = False

    def generate(
        self,
        request: ChatRequest,
    ) -> tuple[ChatResponse, dict[str, object]]:
        if self._closed:
            raise InferenceCallError(
                "transformers_local_session_closed",
                "transformers_local Task session is already closed",
            )
        started = time.perf_counter()
        metrics: dict[str, object] = {
            "model_path": self.config.model_path,
            "model_kind": self.config.model_kind,
            "generation_method": self.config.generation_method,
            "max_tokens": int(request.max_tokens),
            "temperature": float(request.temperature),
            "qwen2_5_vl_cpu_unique_consecutive_workaround": (
                self.config.qwen2_5_vl_cpu_unique_consecutive_workaround
            ),
            "device_id": self.config.device_id,
            "worker_pid": os.getpid(),
            "model_reused": self._loaded,
            "model_load_ms": 0,
            "cleanup_ms": 0,
        }
        if self.config.model_kind == "vision_language":
            metrics["processor_load_ms"] = 0
        if not self._loaded:
            self._load(metrics)
        assert self._torch is not None
        assert self._device is not None
        assert self._model is not None
        metrics["logical_device_id"] = self._logical_device_id

        stage_started = time.perf_counter()
        if self.config.model_kind == "vision_language":
            assert self._processor is not None
            multimodal_messages, image_count = _multimodal_messages(request)
            metrics["image_count"] = image_count
            tokenized = _apply_multimodal_chat_template(
                self._processor,
                multimodal_messages,
                enable_thinking=self.config.enable_thinking,
            )
            metrics["multimodal_preprocess_ms"] = _elapsed_ms(stage_started)
        else:
            assert self._tokenizer is not None
            text_messages = _plain_text_messages(request)
            prompt = _apply_chat_template(
                self._tokenizer,
                text_messages,
                enable_thinking=self.config.enable_thinking,
            )
            tokenized = dict(self._tokenizer([prompt], return_tensors="pt"))
        input_tokens = int(tokenized["input_ids"].shape[-1])
        if input_tokens + request.max_tokens > self.config.max_model_len:
            raise InferenceCallError(
                "model_context_length_exceeded",
                "transformers_local prompt plus max_tokens exceeds max_model_len: "
                f"input_tokens={input_tokens} max_tokens={request.max_tokens} "
                f"max_model_len={self.config.max_model_len}",
            )
        tokenized = {
            key: value.to(self._device) if hasattr(value, "to") else value
            for key, value in tokenized.items()
        }
        if self.config.model_kind == "text":
            metrics["tokenize_ms"] = _elapsed_ms(stage_started)

        stage_started = time.perf_counter()
        with self._torch.no_grad():
            if self.config.generation_method == "manual_greedy":
                if self.config.model_kind == "vision_language":
                    assert self._processor is not None
                    generated = _manual_greedy_multimodal_generate(
                        torch=self._torch,
                        model=self._model,
                        model_inputs=tokenized,
                        max_new_tokens=int(request.max_tokens),
                        eos_token_id=getattr(
                            getattr(self._processor, "tokenizer", None),
                            "eos_token_id",
                            None,
                        ),
                        device=self._device,
                    )
                else:
                    assert self._tokenizer is not None
                    generated = _manual_greedy_generate(
                        torch=self._torch,
                        model=self._model,
                        input_ids=tokenized["input_ids"],
                        max_new_tokens=int(request.max_tokens),
                        eos_token_id=getattr(
                            self._tokenizer,
                            "eos_token_id",
                            None,
                        ),
                        device=self._device,
                    )
            else:
                generation_kwargs: dict[str, object] = {
                    "max_new_tokens": int(request.max_tokens),
                    "do_sample": bool(float(request.temperature) > 0),
                }
                if float(request.temperature) > 0:
                    generation_kwargs["temperature"] = float(request.temperature)
                generation_tokenizer = (
                    self._tokenizer
                    if self.config.model_kind == "text"
                    else getattr(self._processor, "tokenizer", None)
                )
                eos_token_id = getattr(generation_tokenizer, "eos_token_id", None)
                pad_token_id = getattr(generation_tokenizer, "pad_token_id", None)
                if eos_token_id is not None:
                    generation_kwargs["eos_token_id"] = eos_token_id
                if pad_token_id is not None:
                    generation_kwargs["pad_token_id"] = pad_token_id
                elif eos_token_id is not None:
                    generation_kwargs["pad_token_id"] = eos_token_id
                generated = self._model.generate(**tokenized, **generation_kwargs)
        _synchronize(self._torch)
        metrics["generate_ms"] = _elapsed_ms(stage_started)

        stage_started = time.perf_counter()
        if self.config.model_kind == "vision_language":
            assert self._processor is not None
            new_tokens = generated[:, input_tokens:]
            output_tokens = int(new_tokens.shape[-1])
            decoded = self._processor.batch_decode(
                new_tokens,
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False,
            )
            text = str(decoded[0])
        else:
            assert self._tokenizer is not None
            new_tokens = generated[0][input_tokens:]
            output_tokens = int(new_tokens.shape[-1])
            text = self._tokenizer.decode(new_tokens, skip_special_tokens=True)
        metrics["decode_ms"] = _elapsed_ms(stage_started)
        total_duration_ms = _elapsed_ms(started)
        metrics["total_duration_ms"] = total_duration_ms
        metrics["input_tokens"] = input_tokens
        metrics["output_tokens"] = output_tokens
        return (
            ChatResponse(
                text=text,
                finish_reason=(
                    "length" if output_tokens >= int(request.max_tokens) else "stop"
                ),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                engine_queue_depth=0,
                prefix_cache_hit=False,
                ttft_ms=None,
                total_duration_ms=total_duration_ms,
            ),
            metrics,
        )

    def close(self) -> int:
        if self._closed:
            return 0
        cleanup_started = time.perf_counter()
        torch = self._torch
        self._model = None
        self._tokenizer = None
        self._processor = None
        self._device = None
        self._torch = None
        self._loaded = False
        self._closed = True
        gc.collect()
        if torch is not None:
            try:
                _empty_npu_cache(torch)
            except Exception:
                pass
        return _elapsed_ms(cleanup_started)

    def _load(self, metrics: dict[str, object]) -> None:
        load_started = time.perf_counter()
        logical_device_id = _configure_process_npu_visibility(self.config.device_id)
        _prepend_env_paths("LD_LIBRARY_PATH", self.config.runtime_library_paths)
        import torch

        _set_npu_device(torch, logical_device_id)
        if self.config.qwen2_5_vl_cpu_unique_consecutive_workaround:
            _install_qwen25vl_cpu_unique_consecutive_workaround(torch)
        torch_dtype = _torch_dtype(torch, self.config.dtype)
        device = _torch_device(torch, logical_device_id)
        self._torch = torch
        self._device = device
        self._logical_device_id = logical_device_id

        if self.config.model_kind == "vision_language":
            from transformers import AutoModelForImageTextToText, AutoProcessor

            processor_started = time.perf_counter()
            self._processor = AutoProcessor.from_pretrained(
                self.config.model_path,
                trust_remote_code=self.config.trust_remote_code,
            )
            metrics["processor_load_ms"] = _elapsed_ms(processor_started)
            model_started = time.perf_counter()
            self._model = AutoModelForImageTextToText.from_pretrained(
                self.config.model_path,
                torch_dtype=torch_dtype,
                trust_remote_code=self.config.trust_remote_code,
            )
            self._model.to(device)
            self._model.eval()
            _synchronize(torch)
            metrics["model_load_ms"] = _elapsed_ms(model_started)
        else:
            from transformers import AutoModelForCausalLM, AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(
                self.config.tokenizer_path,
                trust_remote_code=self.config.trust_remote_code,
            )
            self._model = AutoModelForCausalLM.from_pretrained(
                self.config.model_path,
                torch_dtype=torch_dtype,
                trust_remote_code=self.config.trust_remote_code,
            )
            self._model.to(device)
            self._model.eval()
            _synchronize(torch)
            metrics["model_load_ms"] = _elapsed_ms(load_started)
        self._loaded = True


def generate_chat_once(
    config: TransformersLocalGenerationConfig,
    request: ChatRequest,
) -> tuple[ChatResponse, dict[str, object]]:
    """Load a local Transformers model, run one chat completion, then cleanup."""

    session = TransformersLocalGenerationSession(config)
    metrics: dict[str, object] | None = None
    try:
        response, metrics = session.generate(request)
        return response, metrics
    finally:
        cleanup_ms = session.close()
        if metrics is not None:
            metrics["cleanup_ms"] = cleanup_ms


def _generation_config_from_spec(
    spec: ModelSpec,
    device_id: str,
) -> TransformersLocalGenerationConfig:
    return TransformersLocalGenerationConfig(
        model_path=spec.artifact_path,
        tokenizer_path=spec.tokenizer_path or spec.artifact_path,
        dtype=spec.dtype,
        max_model_len=spec.max_model_len,
        device_id=str(spec.launch_options.get("device_id", device_id)),
        trust_remote_code=bool(spec.launch_options.get("trust_remote_code", False)),
        enable_thinking=bool(spec.launch_options.get("enable_thinking", False)),
        runtime_library_paths=cast(
            tuple[str, ...],
            spec.launch_options.get("runtime_library_paths", ()),
        ),
        generation_method=str(spec.launch_options.get("generation_method", "generate")),
        model_kind=str(spec.launch_options.get("model_kind", "text")),
        qwen2_5_vl_cpu_unique_consecutive_workaround=bool(
            spec.launch_options.get(
                "qwen2_5_vl_cpu_unique_consecutive_workaround", False
            )
        ),
    )


def _plain_text_messages(request: ChatRequest) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for message in request.messages:
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str):
            raise InferenceCallError(
                "transformers_local_invalid_message",
                "chat message role must be a string",
            )
        messages.append({"role": role, "content": _text_content(content)})
    return messages


def _multimodal_messages(
    request: ChatRequest,
) -> tuple[list[dict[str, object]], int]:
    """Translate the public OpenAI content schema to Transformers' schema."""

    messages: list[dict[str, object]] = []
    image_count = 0
    for message in request.messages:
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str):
            raise InferenceCallError(
                "transformers_local_invalid_message",
                "chat message role must be a string",
            )
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, tuple):
            raise InferenceCallError(
                "transformers_local_invalid_content",
                "multimodal chat content must be a string or content parts",
            )
        parts: list[dict[str, object]] = []
        for part in content:
            if not isinstance(part, FrozenMap):
                raise InferenceCallError(
                    "transformers_local_invalid_content",
                    "chat content part must be a mapping",
                )
            part_type = part.get("type")
            if part_type == "text":
                text = part.get("text")
                if not isinstance(text, str):
                    raise InferenceCallError(
                        "transformers_local_invalid_content",
                        "text content part must contain string text",
                    )
                parts.append({"type": "text", "text": text})
                continue
            if part_type != "image_url":
                raise InferenceCallError(
                    "transformers_local_invalid_content",
                    "unsupported multimodal content part",
                )
            image_url = part.get("image_url")
            if not isinstance(image_url, FrozenMap):
                raise InferenceCallError(
                    "transformers_local_invalid_content",
                    "image_url content part must contain a mapping",
                )
            url = image_url.get("url")
            if not isinstance(url, str) or not url:
                raise InferenceCallError(
                    "transformers_local_invalid_content",
                    "image_url content part must contain a non-empty URL",
                )
            converted: dict[str, object] = {"type": "image", "url": url}
            detail = image_url.get("detail")
            if isinstance(detail, str):
                converted["detail"] = detail
            parts.append(converted)
            image_count += 1
        messages.append({"role": role, "content": parts})
    return messages, image_count


def _apply_multimodal_chat_template(
    processor: Any,
    messages: list[dict[str, object]],
    *,
    enable_thinking: bool,
) -> dict[str, Any]:
    kwargs: dict[str, object] = {
        "tokenize": True,
        "add_generation_prompt": True,
        "return_dict": True,
        "return_tensors": "pt",
    }
    if enable_thinking:
        kwargs["enable_thinking"] = True
    try:
        encoded = processor.apply_chat_template(messages, **kwargs)
    except TypeError:
        kwargs.pop("enable_thinking", None)
        encoded = processor.apply_chat_template(messages, **kwargs)
    if not hasattr(encoded, "items"):
        raise InferenceCallError(
            "transformers_local_invalid_processor_output",
            "multimodal processor must return a mapping",
        )
    result = dict(encoded.items())
    if "input_ids" not in result:
        raise InferenceCallError(
            "transformers_local_invalid_processor_output",
            "multimodal processor output is missing input_ids",
        )
    return result


def _text_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, tuple):
        chunks: list[str] = []
        for part in content:
            if not isinstance(part, FrozenMap):
                raise InferenceCallError(
                    "transformers_local_invalid_content",
                    "chat content part must be a mapping",
                )
            part_type = part.get("type")
            if part_type != "text":
                raise InferenceCallError(
                    "transformers_local_text_only",
                    "transformers_local benchmark backend supports text content only",
                )
            text = part.get("text")
            if not isinstance(text, str):
                raise InferenceCallError(
                    "transformers_local_invalid_content",
                    "text content part must contain string text",
                )
            chunks.append(text)
        return "\n".join(chunks)
    raise InferenceCallError(
        "transformers_local_invalid_content",
        "chat content must be a string or text-only content parts",
    )


def _apply_chat_template(
    tokenizer: Any,
    messages: list[dict[str, str]],
    *,
    enable_thinking: bool,
) -> str:
    if hasattr(tokenizer, "apply_chat_template"):
        try:
            return tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=enable_thinking,
            )
        except TypeError:
            return tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
    return "\n".join(
        f"{message['role']}: {message['content']}" for message in messages
    ) + "\nassistant:"


def _manual_greedy_generate(
    *,
    torch: Any,
    model: Any,
    input_ids: Any,
    max_new_tokens: int,
    eos_token_id: int | None,
    device: Any,
) -> Any:
    if max_new_tokens < 1:
        return input_ids

    generated_parts = [input_ids]
    step_input_ids = input_ids
    past_key_values = None
    # Creating masks directly on NPU can trigger an AICPU OnesLike path on the
    # current torch_npu/CANN builds, so grow a CPU-origin mask one column at a
    # time and transfer only that column after the initial prompt.
    attention_mask = torch.ones(
        tuple(input_ids.shape),
        dtype=torch.long,
    ).to(device)
    for _ in range(max_new_tokens):
        outputs = model(
            input_ids=step_input_ids,
            attention_mask=attention_mask,
            past_key_values=past_key_values,
            use_cache=True,
            return_dict=True,
        )
        past_key_values = outputs.past_key_values
        next_id = torch.argmax(outputs.logits[:, -1, :], dim=-1, keepdim=True)
        generated_parts.append(next_id)
        if eos_token_id is not None and int(next_id.cpu().item()) == int(eos_token_id):
            break
        step_input_ids = next_id
        next_attention = torch.ones(
            (int(attention_mask.shape[0]), 1),
            dtype=torch.long,
        ).to(device)
        attention_mask = torch.cat((attention_mask, next_attention), dim=-1)
    return torch.cat(tuple(generated_parts), dim=-1)


def _manual_greedy_multimodal_generate(
    *,
    torch: Any,
    model: Any,
    model_inputs: dict[str, Any],
    max_new_tokens: int,
    eos_token_id: int | None,
    device: Any,
) -> Any:
    """Greedy Qwen2.5-VL decode without Transformers' NPU OnesLike path."""

    input_ids = model_inputs["input_ids"]
    if max_new_tokens < 1:
        return input_ids
    get_rope_index = getattr(getattr(model, "model", None), "get_rope_index", None)
    if not callable(get_rope_index):
        raise InferenceCallError(
            "transformers_local_manual_multimodal_unsupported",
            "manual multimodal generation requires Qwen2.5-VL RoPE support",
        )

    cpu_input_ids = input_ids.detach().cpu()
    cpu_attention_mask = model_inputs["attention_mask"].detach().cpu()
    cpu_image_grid: Any = model_inputs.get("image_grid_thw")
    if hasattr(cpu_image_grid, "detach"):
        cpu_image_grid = cpu_image_grid.detach().cpu()
    position_ids, rope_deltas = get_rope_index(
        cpu_input_ids,
        image_grid_thw=cpu_image_grid,
        attention_mask=cpu_attention_mask,
    )

    first_inputs = dict(model_inputs)
    first_inputs["position_ids"] = position_ids.to(device)
    generated_parts = [input_ids]
    attention_mask = model_inputs["attention_mask"]
    step_input_ids: Any | None = None
    past_key_values: Any | None = None
    prompt_length = int(input_ids.shape[-1])
    batch_size = int(input_ids.shape[0])
    for step in range(max_new_tokens):
        if step == 0:
            call_inputs = first_inputs
        else:
            cache_position = prompt_length + step - 1
            next_position_ids = torch.full(
                (3, batch_size, 1),
                cache_position,
                dtype=cpu_input_ids.dtype,
            ) + rope_deltas.reshape(1, batch_size, 1)
            call_inputs = {
                "input_ids": step_input_ids,
                "attention_mask": attention_mask,
                "position_ids": next_position_ids.to(device),
                "past_key_values": past_key_values,
            }
        outputs = model(
            **call_inputs,
            use_cache=True,
            return_dict=True,
            logits_to_keep=1,
        )
        past_key_values = outputs.past_key_values
        next_id = torch.argmax(outputs.logits[:, -1, :], dim=-1, keepdim=True)
        generated_parts.append(next_id)
        if eos_token_id is not None and int(next_id.cpu().item()) == int(eos_token_id):
            break
        step_input_ids = next_id
        next_attention = torch.ones(
            (batch_size, 1),
            dtype=torch.long,
        ).to(device)
        attention_mask = torch.cat((attention_mask, next_attention), dim=-1)
    return torch.cat(tuple(generated_parts), dim=-1)


def _install_qwen25vl_cpu_unique_consecutive_workaround(torch: Any) -> None:
    """Offload the one small integer UniqueConsecutive used by Qwen2.5-VL."""

    original = torch.unique_consecutive
    if getattr(original, "_ascend_maze_qwen25vl_cpu_workaround", False):
        return

    def patched(input_tensor: Any, *args: Any, **kwargs: Any) -> Any:
        device_type = getattr(getattr(input_tensor, "device", None), "type", None)
        should_offload = (
            device_type == "npu"
            and getattr(input_tensor, "dtype", None)
            in {torch.int16, torch.int32, torch.int64}
            and getattr(input_tensor, "ndim", 0) <= 1
            and input_tensor.numel() <= 131_072
            and kwargs.get("dim") is None
        )
        if should_offload:
            return original(input_tensor.detach().cpu(), *args, **kwargs)
        return original(input_tensor, *args, **kwargs)

    patched._ascend_maze_qwen25vl_cpu_workaround = True  # type: ignore[attr-defined]
    torch.unique_consecutive = patched


def _torch_dtype(torch: Any, dtype: str) -> Any:
    if dtype == "bfloat16":
        return torch.bfloat16
    if dtype == "float16":
        return torch.float16
    raise ContractValidationError(f"unsupported dtype: {dtype}")


def _prepend_env_paths(name: str, paths: tuple[str, ...]) -> None:
    if not paths:
        return
    existing = tuple(item for item in os.environ.get(name, "").split(os.pathsep) if item)
    merged: list[str] = []
    for item in (*paths, *existing):
        if item not in merged:
            merged.append(item)
    os.environ[name] = os.pathsep.join(merged)


def _configure_process_npu_visibility(physical_device_id: str) -> str:
    os.environ["ASCEND_RT_VISIBLE_DEVICES"] = physical_device_id
    # A single visible physical NPU is remapped to logical device zero.
    return "0"


def _set_npu_device(torch: Any, device_id: str) -> None:
    try:
        import torch_npu  # noqa: F401
    except Exception:
        pass
    npu = getattr(torch, "npu", None)
    if npu is not None and hasattr(npu, "set_device"):
        npu.set_device(f"npu:{device_id}")


def _torch_device(torch: Any, device_id: str) -> Any:
    npu = getattr(torch, "npu", None)
    if npu is not None and hasattr(npu, "is_available") and npu.is_available():
        return torch.device(f"npu:{device_id}")
    if hasattr(torch, "cuda") and torch.cuda.is_available():
        return torch.device("cuda:0")
    return torch.device("cpu")


def _synchronize(torch: Any) -> None:
    npu = getattr(torch, "npu", None)
    if npu is not None and hasattr(npu, "synchronize"):
        npu.synchronize()
    elif hasattr(torch, "cuda") and torch.cuda.is_available():
        torch.cuda.synchronize()


def _empty_npu_cache(torch: Any) -> None:
    npu = getattr(torch, "npu", None)
    if npu is not None and hasattr(npu, "empty_cache"):
        npu.empty_cache()
        if hasattr(npu, "synchronize"):
            npu.synchronize()
    elif hasattr(torch, "cuda") and torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.synchronize()


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1_000))
