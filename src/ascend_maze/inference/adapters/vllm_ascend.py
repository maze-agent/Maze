"""vLLM-Ascend adapter using its OpenAI-compatible HTTP surface."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Protocol

from ascend_maze.contracts.resources import PlacementLease
from ascend_maze.core.canonical import CanonicalValue, FrozenMap, canonical_digest
from ascend_maze.core.errors import ContractValidationError
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
    ServiceProcessBackend,
    WarmupResult,
)


@dataclass(frozen=True, slots=True)
class VllmHttpResponse:
    status_code: int
    content: bytes
    headers: Mapping[str, str]

    def json(self) -> object:
        try:
            return json.loads(self.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InferenceCallError(
                "model_protocol_failed",
                "vLLM returned an invalid JSON response",
            ) from exc


class VllmHttpTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: object | None,
        timeout_ms: int,
    ) -> VllmHttpResponse: ...

    async def close(self) -> None: ...


class HttpxVllmTransport:
    """Lazy pooled transport safe across Task-owned asyncio event loops."""

    def __init__(self) -> None:
        self._client: Any = None
        self._lock = Lock()
        self._closed = False

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: object | None,
        timeout_ms: int,
    ) -> VllmHttpResponse:
        try:
            response = await asyncio.to_thread(
                self._request_sync,
                method,
                url,
                json_body,
                timeout_ms,
            )
        except Exception as exc:
            try:
                import httpx
            except ImportError:
                raise RuntimeError(
                    "vLLM-Ascend HTTP support requires the inference-vllm extra"
                ) from exc
            if isinstance(exc, httpx.TimeoutException):
                raise InferenceCallError(
                    "model_inference_timeout", f"vLLM request timed out: {url}"
                ) from exc
            if isinstance(exc, httpx.RequestError):
                raise InferenceCallError(
                    "model_service_unavailable",
                    f"vLLM endpoint is unavailable: {type(exc).__name__}",
                ) from exc
            raise
        return VllmHttpResponse(
            status_code=int(response.status_code),
            content=bytes(response.content),
            headers=dict(response.headers),
        )

    async def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
            self._closed = True
        if client is not None:
            await asyncio.to_thread(client.close)

    def _request_sync(
        self,
        method: str,
        url: str,
        json_body: object | None,
        timeout_ms: int,
    ) -> Any:
        return self._get_client().request(
            method,
            url,
            json=json_body,
            timeout=timeout_ms / 1_000,
        )

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._lock:
            if self._closed:
                raise RuntimeError("vLLM HTTP transport is closed")
            if self._client is None:
                try:
                    import httpx
                except ImportError as exc:
                    raise RuntimeError(
                        "vLLM-Ascend HTTP support requires the inference-vllm extra"
                    ) from exc
                limits = httpx.Limits(
                    max_connections=64,
                    max_keepalive_connections=16,
                    keepalive_expiry=30.0,
                )
                self._client = httpx.Client(limits=limits)
        return self._client


def _plain(value: CanonicalValue) -> object:
    if isinstance(value, FrozenMap):
        return {str(key): _plain(item) for key, item in value.items_tuple()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


class VllmAscendInferenceEngineAdapter:
    name = "vllm_ascend"

    _ALLOWED_OPTIONS = frozenset(
        {
            "block_size",
            "enable_prefix_caching",
            "enforce_eager",
            "generation_config",
            "gpu_memory_utilization",
            "log_level",
            "max_num_batched_tokens",
            "max_num_seqs",
            "qwen2_5_vl_cpu_unique_consecutive_workaround",
            "trust_remote_code",
        }
    )

    def __init__(
        self,
        *,
        process_backend: ServiceProcessBackend,
        python_executable: str,
        endpoint_host_resolver: Callable[[PlacementLease], str],
        transport: VllmHttpTransport | None = None,
        bind_host: str = "0.0.0.0",
        api_server_entrypoint: tuple[str, ...] = (
            "-m",
            "vllm.entrypoints.openai.api_server",
        ),
        runtime_library_preloads: Mapping[str, str] | None = None,
        runtime_library_paths: tuple[str, ...] | None = None,
        request_timeout_ms: int = 30_000,
        probe_timeout_ms: int = 300_000,
        probe_interval_ms: int = 250,
    ) -> None:
        executable = Path(python_executable).expanduser().resolve(strict=False)
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("python_executable must be an existing absolute file")
        if not bind_host:
            raise ValueError("bind_host is required")
        if (
            not isinstance(api_server_entrypoint, tuple)
            or not api_server_entrypoint
            or any(
                not isinstance(item, str) or not item for item in api_server_entrypoint
            )
        ):
            raise ValueError("api_server_entrypoint must contain non-empty strings")
        for name, value in (
            ("request_timeout_ms", request_timeout_ms),
            ("probe_timeout_ms", probe_timeout_ms),
            ("probe_interval_ms", probe_interval_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be positive")
        self.process_backend = process_backend
        self.python_executable = str(executable)
        self.endpoint_host_resolver = endpoint_host_resolver
        self.transport = transport or HttpxVllmTransport()
        self.bind_host = bind_host
        self.api_server_entrypoint = api_server_entrypoint
        self.runtime_library_preloads = self._validate_runtime_library_preloads(
            runtime_library_preloads
        )
        self.runtime_library_paths = self._validate_runtime_library_paths(
            runtime_library_paths
        )
        self.request_timeout_ms = request_timeout_ms
        self.probe_timeout_ms = probe_timeout_ms
        self.probe_interval_ms = probe_interval_ms

    def validate_model_spec(self, spec: ModelSpec) -> None:
        if spec.backend != self.name:
            raise ContractValidationError(
                "vLLM-Ascend adapter requires backend='vllm_ascend'"
            )
        if spec.tensor_parallel_size != 1 or spec.npu_slots != 1:
            raise ContractValidationError(
                "stage 6B vLLM-Ascend requires one NPU per model instance"
            )
        if spec.dtype not in {"bfloat16", "float16"}:
            raise ContractValidationError(
                "stage 6B vLLM-Ascend supports bfloat16 or float16"
            )
        if spec.quantization is not None:
            raise ContractValidationError(
                "stage 6B admission has not validated quantized Qwen3"
            )
        if spec.allow_colocation:
            raise ContractValidationError(
                "vLLM 0.11 admission does not provide a hard total-HBM limit; "
                "allow_colocation must be false"
            )
        options = self._options(spec)
        unknown = set(options) - self._ALLOWED_OPTIONS
        if unknown:
            raise ContractValidationError(
                "unsupported vLLM-Ascend launch options: " + ", ".join(sorted(unknown))
            )
        utilization = options.get("gpu_memory_utilization")
        if (
            isinstance(utilization, bool)
            or not isinstance(utilization, (int, float))
            or not 0 < float(utilization) <= 0.9
        ):
            raise ContractValidationError(
                "gpu_memory_utilization must be numeric within (0, 0.9]"
            )
        block_size = options.get("block_size", 128)
        if block_size not in {1, 8, 16, 32, 64, 128}:
            raise ContractValidationError("unsupported vLLM block_size")
        max_num_batched_tokens = options.get("max_num_batched_tokens")
        if max_num_batched_tokens is not None and (
            isinstance(max_num_batched_tokens, bool)
            or not isinstance(max_num_batched_tokens, int)
            or max_num_batched_tokens < 1
        ):
            raise ContractValidationError(
                "max_num_batched_tokens must be positive"
            )
        max_num_seqs = options.get("max_num_seqs")
        if max_num_seqs is not None and (
            isinstance(max_num_seqs, bool)
            or not isinstance(max_num_seqs, int)
            or max_num_seqs < 1
        ):
            raise ContractValidationError("max_num_seqs must be positive")
        for name in ("enforce_eager", "enable_prefix_caching", "trust_remote_code"):
            value = options.get(name, name != "trust_remote_code")
            if not isinstance(value, bool):
                raise ContractValidationError(f"{name} must be a boolean")
        workaround = options.get("qwen2_5_vl_cpu_unique_consecutive_workaround", False)
        if not isinstance(workaround, bool):
            raise ContractValidationError(
                "qwen2_5_vl_cpu_unique_consecutive_workaround must be a boolean"
            )
        generation_config = options.get("generation_config")
        if generation_config is not None and generation_config not in {"auto", "vllm"}:
            raise ContractValidationError("generation_config must be auto or vllm")
        log_level = options.get("log_level", "INFO")
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ContractValidationError("log_level is invalid")
        self._warmup_payload(spec)

    def worker_config(
        self,
        spec: ModelSpec,
        *,
        instance_placement_lease_id: str,
        npu_device_id: str,
    ) -> InferenceWorkerConfig:
        del npu_device_id
        self.validate_model_spec(spec)
        return InferenceWorkerConfig(
            adapter_name=self.name,
            instance_placement_lease_id=instance_placement_lease_id,
            request_timeout_ms=self.request_timeout_ms,
        )

    def build_launch_request(
        self,
        spec: ModelSpec,
        lease: PlacementLease,
        port_lease: PortLease,
    ) -> ServiceLaunchRequest:
        self.validate_model_spec(spec)
        if lease.npu_device_id is None:
            raise ContractValidationError("model instance requires a physical NPU")
        if (
            port_lease.node_id != lease.node_id
            or port_lease.boot_id != lease.boot_id
            or port_lease.owner_instance_id != lease.model_instance_id
        ):
            raise ContractValidationError(
                "PortLease does not match model PlacementLease"
            )
        host = self.endpoint_host_resolver(lease)
        if not host or "://" in host or "/" in host:
            raise ContractValidationError("resolved model service host is invalid")
        endpoint_host = (
            f"[{host}]" if ":" in host and not host.startswith("[") else host
        )
        endpoint = f"http://{endpoint_host}:{port_lease.port}"
        options = self._options(spec)
        argv = [
            self.python_executable,
            *self.api_server_entrypoint,
            "--host",
            self.bind_host,
            "--port",
            str(port_lease.port),
            "--model",
            spec.artifact_path,
            "--served-model-name",
            spec.model_id,
            "--dtype",
            spec.dtype,
            "--max-model-len",
            str(spec.max_model_len),
            "--tensor-parallel-size",
            "1",
            "--gpu-memory-utilization",
            str(self._gpu_memory_utilization(options)),
            "--block-size",
            str(options.get("block_size", 128)),
        ]
        if bool(options.get("enforce_eager", True)):
            argv.append("--enforce-eager")
        if bool(options.get("enable_prefix_caching", True)):
            argv.append("--enable-prefix-caching")
        else:
            argv.append("--no-enable-prefix-caching")
        if bool(options.get("trust_remote_code", False)):
            argv.append("--trust-remote-code")
        max_num_seqs = options.get("max_num_seqs")
        if max_num_seqs is not None:
            argv.extend(("--max-num-seqs", str(max_num_seqs)))
        max_num_batched_tokens = options.get("max_num_batched_tokens")
        if max_num_batched_tokens is not None:
            argv.extend(("--max-num-batched-tokens", str(max_num_batched_tokens)))
        generation_config = options.get("generation_config")
        if generation_config is not None:
            argv.extend(("--generation-config", str(generation_config)))
        environment_values = [
            ("ASCEND_MAZE_ARTIFACT_REVISION", spec.artifact_revision),
            ("ASCEND_MAZE_ENVIRONMENT_FINGERPRINT", spec.environment_fingerprint),
            ("ASCEND_MAZE_INSTANCE_GENERATION", str(port_lease.generation)),
            ("ASCEND_MAZE_INSTANCE_ID", port_lease.owner_instance_id),
            ("ASCEND_RT_VISIBLE_DEVICES", lease.npu_device_id),
            ("PYTHONUNBUFFERED", "1"),
            ("VLLM_LOGGING_LEVEL", str(options.get("log_level", "INFO"))),
        ]
        if self.runtime_library_preloads:
            environment_values.extend(
                (
                    (
                        "ASCEND_MAZE_RUNTIME_LIBRARY_PRELOAD_DIGEST",
                        canonical_digest(self.runtime_library_preloads),
                    ),
                    (
                        "LD_PRELOAD",
                        " ".join(path for path, _ in self.runtime_library_preloads),
                    ),
                )
            )
        if self.runtime_library_paths:
            current = os.environ.get("LD_LIBRARY_PATH", "")
            inherited = tuple(item for item in current.split(os.pathsep) if item)
            environment_values.append(
                (
                    "LD_LIBRARY_PATH",
                    os.pathsep.join((*self.runtime_library_paths, *inherited)),
                )
            )
        if bool(options.get("qwen2_5_vl_cpu_unique_consecutive_workaround", False)):
            current = os.environ.get("PYTHONPATH", "")
            inherited = tuple(item for item in current.split(os.pathsep) if item)
            patch_dir = str(Path(__file__).resolve().with_name("vllm_runtime_patches"))
            environment_values.extend(
                (
                    ("ASCEND_MAZE_QWEN25VL_CPU_UNIQUE_CONSECUTIVE", "1"),
                    ("PYTHONPATH", os.pathsep.join((patch_dir, *inherited))),
                )
            )
        environment = FrozenMap(environment_values)
        return ServiceLaunchRequest(
            instance_id=port_lease.owner_instance_id,
            generation=port_lease.generation,
            model_id=spec.model_id,
            artifact_revision=spec.artifact_revision,
            endpoint_id=endpoint,
            port_lease_id=port_lease.port_lease_id,
            port=port_lease.port,
            argv=tuple(argv),
            working_directory=str(Path(spec.artifact_path).parent),
            environment=environment,
        )

    @staticmethod
    def _validate_runtime_library_preloads(
        configured: Mapping[str, str] | None,
    ) -> tuple[tuple[str, str], ...]:
        if configured is None:
            return ()
        if not isinstance(configured, Mapping):
            raise ContractValidationError(
                "runtime_library_preloads must map absolute paths to SHA-256 digests"
            )
        resolved: dict[str, str] = {}
        for configured_path, expected_digest in configured.items():
            if not isinstance(configured_path, str) or not configured_path:
                raise ContractValidationError(
                    "runtime library preload paths must be non-empty strings"
                )
            if (
                not isinstance(expected_digest, str)
                or len(expected_digest) != 64
                or any(
                    character not in "0123456789abcdef" for character in expected_digest
                )
            ):
                raise ContractValidationError(
                    "runtime library preload digests must be lowercase SHA-256 values"
                )
            path = Path(configured_path).expanduser()
            if not path.is_absolute():
                raise ContractValidationError(
                    "runtime library preload paths must be absolute"
                )
            try:
                path = path.resolve(strict=True)
            except OSError as exc:
                raise ContractValidationError(
                    f"runtime library preload does not exist: {configured_path}"
                ) from exc
            if not path.is_file():
                raise ContractValidationError(
                    f"runtime library preload is not a file: {path}"
                )
            normalized = str(path)
            if os.pathsep in normalized or any(
                character.isspace() for character in normalized
            ):
                raise ContractValidationError(
                    "runtime library preload paths cannot contain whitespace or path separators"
                )
            actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual_digest != expected_digest:
                raise ContractValidationError(
                    f"runtime library preload digest mismatch: {path}"
                )
            prior = resolved.get(normalized)
            if prior is not None and prior != expected_digest:
                raise ContractValidationError(
                    f"conflicting runtime library preload identity: {path}"
                )
            resolved[normalized] = expected_digest
        return tuple(sorted(resolved.items()))

    @staticmethod
    def _validate_runtime_library_paths(
        configured: tuple[str, ...] | None,
    ) -> tuple[str, ...]:
        if configured is None:
            return ()
        if not isinstance(configured, tuple):
            raise ContractValidationError("runtime_library_paths must be a tuple")
        resolved: list[str] = []
        seen: set[str] = set()
        for configured_path in configured:
            if not isinstance(configured_path, str) or not configured_path:
                raise ContractValidationError(
                    "runtime library paths must be non-empty strings"
                )
            path = Path(configured_path).expanduser().resolve(strict=False)
            if not path.is_absolute() or not path.is_dir():
                raise ContractValidationError(
                    f"runtime library path does not exist: {configured_path}"
                )
            identity = str(path)
            if identity in seen:
                continue
            seen.add(identity)
            resolved.append(identity)
        return tuple(resolved)

    async def probe(self, handle: ServiceHandle, spec: ModelSpec) -> EngineProbe:
        deadline = monotonic() + self.probe_timeout_ms / 1_000
        last_error: Exception | None = None
        while monotonic() < deadline:
            process = await self.process_backend.probe_process(
                handle,
                timeout_ms=min(self.request_timeout_ms, self.probe_timeout_ms),
            )
            if not process.process_alive:
                raise InferenceCallError(
                    "model_process_exited",
                    f"vLLM process exited with code {process.exit_code}",
                )
            try:
                health = await self._request("GET", f"{handle.endpoint_id}/health")
                if health.status_code == 200:
                    if not process.binding_verified:
                        raise InferenceCallError(
                            "model_device_binding_mismatch",
                            "NodeAgent could not verify vLLM on its leased physical NPU",
                        )
                    model = await self._model_descriptor(
                        handle.endpoint_id, spec.model_id
                    )
                    self._validate_model_descriptor(model, spec)
                    if process.process_hbm_mb is None:
                        raise InferenceCallError(
                            "model_hbm_unavailable",
                            "NodeAgent has no process HBM sample for ready vLLM",
                        )
                    return EngineProbe(
                        process_alive=True,
                        model_id=spec.model_id,
                        artifact_revision=spec.artifact_revision,
                        environment_fingerprint=spec.environment_fingerprint,
                        dtype=spec.dtype,
                        quantization=spec.quantization,
                        physical_device_id=process.physical_device_id,
                        process_hbm_mb=process.process_hbm_mb,
                        request_capacity=spec.request_capacity,
                    )
            except InferenceCallError as exc:
                if exc.error_code not in {
                    "model_service_unavailable",
                    "model_inference_timeout",
                }:
                    raise
                last_error = exc
            await asyncio.sleep(self.probe_interval_ms / 1_000)
        raise InferenceCallError(
            "model_startup_timeout",
            "vLLM did not become ready before the probe deadline"
            + ("" if last_error is None else f": {last_error}"),
        )

    async def warmup(self, handle: ServiceHandle, spec: ModelSpec) -> WarmupResult:
        started = monotonic()
        response = await self._invoke(handle.endpoint_id, self._warmup_payload(spec))
        text = response.text
        if not text:
            raise InferenceCallError(
                "model_warmup_failed", "vLLM warmup returned empty content"
            )
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return WarmupResult(
            succeeded=True,
            duration_ms=max(0, int((monotonic() - started) * 1_000)),
            response_digest=digest,
        )

    async def invoke_chat(
        self,
        context: ModelRouteContext,
        request: ChatRequest,
    ) -> ChatResponse:
        payload = {
            "model": context.model_id,
            "messages": [_plain(message) for message in request.messages],
            "max_tokens": request.max_tokens,
            "temperature": float(request.temperature),
            **self._conservative_sampling_options(),
        }
        return await self._invoke(context.endpoint_id, payload)

    async def read_metrics(self, handle: ServiceHandle) -> EngineMetrics:
        response = await self._request("GET", f"{handle.endpoint_id}/metrics")
        if response.status_code != 200:
            self._raise_http_error(response, "metrics")
        try:
            from prometheus_client.parser import text_string_to_metric_families
        except ImportError as exc:
            raise RuntimeError(
                "vLLM metrics support requires the inference-vllm extra"
            ) from exc
        try:
            text = response.content.decode("utf-8")
            values: dict[str, float] = {}
            for family in text_string_to_metric_families(text):
                for sample in family.samples:
                    values[sample.name] = values.get(sample.name, 0.0) + float(
                        sample.value
                    )
        except (UnicodeDecodeError, ValueError) as exc:
            raise InferenceCallError(
                "model_protocol_failed", "vLLM metrics are malformed"
            ) from exc
        waiting = self._metric(
            values, "vllm:num_requests_waiting", "vllm_num_requests_waiting"
        )
        running = self._metric(
            values, "vllm:num_requests_running", "vllm_num_requests_running"
        )
        if waiting is None or running is None:
            raise InferenceCallError(
                "model_metrics_unavailable",
                "vLLM did not expose request waiting/running gauges",
            )
        return EngineMetrics(
            queue_depth=max(0, int(waiting)),
            actual_request_inflight=max(0, int(running)),
        )

    async def close(self) -> None:
        await self.transport.close()

    async def _invoke(
        self, endpoint: str, payload: Mapping[str, object]
    ) -> ChatResponse:
        started = monotonic()
        response = await self._request(
            "POST",
            f"{endpoint}/v1/chat/completions",
            json_body=dict(payload),
        )
        if response.status_code != 200:
            self._raise_http_error(response, "chat")
        body = response.json()
        try:
            if not isinstance(body, dict):
                raise TypeError
            choices = body["choices"]
            usage = body["usage"]
            if not isinstance(choices, list) or not choices:
                raise TypeError
            if not isinstance(usage, dict):
                raise TypeError
            choice = choices[0]
            if not isinstance(choice, dict):
                raise TypeError
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError
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
        except (AssertionError, IndexError, KeyError, TypeError) as exc:
            raise InferenceCallError(
                "model_protocol_failed", "vLLM chat response schema is invalid"
            ) from exc
        return ChatResponse(
            text=text,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            engine_queue_depth=None,
            prefix_cache_hit=None,
            ttft_ms=None,
            total_duration_ms=max(0, int((monotonic() - started) * 1_000)),
        )

    async def _model_descriptor(
        self, endpoint: str, model_id: str
    ) -> Mapping[str, object]:
        response = await self._request("GET", f"{endpoint}/v1/models")
        if response.status_code != 200:
            self._raise_http_error(response, "model list")
        body = response.json()
        try:
            if not isinstance(body, dict):
                raise TypeError
            models = body["data"]
            if not isinstance(models, list):
                raise TypeError
            descriptor = next(
                item
                for item in models
                if isinstance(item, dict) and item.get("id") == model_id
            )
        except (KeyError, StopIteration, TypeError) as exc:
            raise InferenceCallError(
                "model_identity_mismatch",
                f"vLLM did not report served model {model_id}",
            ) from exc
        return descriptor

    @staticmethod
    def _validate_model_descriptor(
        descriptor: Mapping[str, object], spec: ModelSpec
    ) -> None:
        root = descriptor.get("root")
        max_len = descriptor.get("max_model_len")
        if not isinstance(root, str) or Path(root).resolve(strict=False) != Path(
            spec.artifact_path
        ).resolve(strict=False):
            raise InferenceCallError(
                "model_identity_mismatch", "vLLM reported a different model artifact"
            )
        if max_len != spec.max_model_len:
            raise InferenceCallError(
                "model_config_mismatch", "vLLM max_model_len differs from ModelSpec"
            )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: object | None = None,
    ) -> VllmHttpResponse:
        return await self.transport.request(
            method,
            url,
            json_body=json_body,
            timeout_ms=self.request_timeout_ms,
        )

    @staticmethod
    def _raise_http_error(response: VllmHttpResponse, operation: str) -> None:
        code = (
            "model_service_unavailable"
            if response.status_code >= 500
            else "model_protocol_failed"
        )
        raise InferenceCallError(
            code,
            f"vLLM {operation} returned HTTP {response.status_code}",
        )

    @staticmethod
    def _metric(values: Mapping[str, float], *names: str) -> float | None:
        for name in names:
            if name in values:
                return values[name]
        return None

    @staticmethod
    def _options(spec: ModelSpec) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in spec.launch_options.items_tuple():
            if not isinstance(key, str):
                raise ContractValidationError(
                    "vLLM launch option names must be strings"
                )
            result[key] = _plain(value)
        return result

    @staticmethod
    def _gpu_memory_utilization(options: Mapping[str, object]) -> float:
        value = options["gpu_memory_utilization"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractValidationError("gpu_memory_utilization must be numeric")
        return float(value)

    @staticmethod
    def _warmup_payload(spec: ModelSpec) -> dict[str, object]:
        payload = {
            str(key): _plain(value) for key, value in spec.warmup_request.items_tuple()
        }
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ContractValidationError("vLLM warmup_request requires messages")
        for message in messages:
            if (
                not isinstance(message, dict)
                or not isinstance(message.get("role"), str)
                or not isinstance(message.get("content"), str)
            ):
                raise ContractValidationError("vLLM warmup messages are invalid")
        max_tokens = payload.get("max_tokens", 8)
        temperature = payload.get("temperature", 0.0)
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens < 1
        ):
            raise ContractValidationError("warmup max_tokens must be positive")
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or float(temperature) < 0
        ):
            raise ContractValidationError("warmup temperature must be non-negative")
        return {
            "model": spec.model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": float(temperature),
            **VllmAscendInferenceEngineAdapter._conservative_sampling_options(),
        }

    @staticmethod
    def _conservative_sampling_options() -> dict[str, object]:
        return {
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
            "repetition_penalty": 1.0,
        }
