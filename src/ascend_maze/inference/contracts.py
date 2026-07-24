"""Immutable C11 contracts shared by routing, workers and engine adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from pathlib import Path
from typing import Protocol, runtime_checkable

from ascend_maze.contracts.resources import PlacementLease, ReservationVector
from ascend_maze.contracts.runtime import ModelRouteLease
from ascend_maze.core.canonical import (
    CanonicalValue,
    FrozenMap,
    canonical_digest,
    freeze_canonical,
)
from ascend_maze.core.errors import ContractValidationError


def _required_string(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ContractValidationError(f"{name} is required")
    return value


def _non_negative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError(f"{name} must be a non-negative integer")
    return value


def _positive_int(name: str, value: object) -> int:
    result = _non_negative_int(name, value)
    if result < 1:
        raise ContractValidationError(f"{name} must be positive")
    return result


@dataclass(frozen=True, slots=True)
class ModelSpec:
    model_id: str
    catalog_revision: str
    artifact_path: str
    tokenizer_path: str | None
    artifact_revision: str
    backend: str
    dtype: str
    quantization: str | None
    tensor_parallel_size: int
    max_model_len: int
    instance_cpu_num: int
    instance_host_mem_mb: int
    weight_hbm_mb: int
    runtime_hbm_mb: int
    kv_cache_hbm_mb: int
    instance_hbm_mb: int
    npu_slots: int
    allow_colocation: bool
    request_capacity: int
    required_capabilities: tuple[str, ...]
    environment_fingerprint: str
    launch_options: FrozenMap[CanonicalValue, CanonicalValue] = field(
        default_factory=FrozenMap
    )
    warmup_request: FrozenMap[CanonicalValue, CanonicalValue] = field(
        default_factory=FrozenMap
    )
    min_replicas: int = 0
    max_replicas: int = 1
    target_route_utilization: float = 0.8
    scale_up_pending_threshold: int = 1
    scale_up_sustain_ms: int = 0
    scale_down_idle_ms: int = 60_000
    scale_cooldown_ms: int = 10_000
    max_parallel_starts: int = 1
    startup_timeout_ms: int = 300_000
    drain_timeout_ms: int = 60_000

    def __post_init__(self) -> None:
        for name in (
            "model_id",
            "catalog_revision",
            "artifact_path",
            "artifact_revision",
            "backend",
            "dtype",
            "environment_fingerprint",
        ):
            _required_string(name, getattr(self, name))
        for name in ("tokenizer_path", "quantization"):
            value = getattr(self, name)
            if value is not None:
                _required_string(name, value)
        object.__setattr__(
            self,
            "artifact_path",
            str(Path(self.artifact_path).expanduser().resolve(strict=False)),
        )
        if self.tokenizer_path is not None:
            object.__setattr__(
                self,
                "tokenizer_path",
                str(Path(self.tokenizer_path).expanduser().resolve(strict=False)),
            )
        if self.tensor_parallel_size != 1:
            raise ContractValidationError(
                "stage-six inference requires tensor_parallel_size=1"
            )
        for name in (
            "max_model_len",
            "instance_cpu_num",
            "instance_host_mem_mb",
            "instance_hbm_mb",
            "npu_slots",
            "request_capacity",
            "max_replicas",
            "scale_up_pending_threshold",
            "max_parallel_starts",
            "startup_timeout_ms",
            "drain_timeout_ms",
        ):
            _positive_int(name, getattr(self, name))
        for name in (
            "weight_hbm_mb",
            "runtime_hbm_mb",
            "kv_cache_hbm_mb",
            "min_replicas",
            "scale_up_sustain_ms",
            "scale_down_idle_ms",
            "scale_cooldown_ms",
        ):
            _non_negative_int(name, getattr(self, name))
        if self.instance_hbm_mb < (
            self.weight_hbm_mb + self.runtime_hbm_mb + self.kv_cache_hbm_mb
        ):
            raise ContractValidationError(
                "instance_hbm_mb must cover weight, runtime and KV cache budgets"
            )
        if self.min_replicas > self.max_replicas:
            raise ContractValidationError("min_replicas cannot exceed max_replicas")
        if self.max_parallel_starts > self.max_replicas:
            raise ContractValidationError(
                "max_parallel_starts cannot exceed max_replicas"
            )
        if (
            isinstance(self.target_route_utilization, bool)
            or not isinstance(self.target_route_utilization, (int, float))
            or not math.isfinite(float(self.target_route_utilization))
            or not 0 < float(self.target_route_utilization) <= 1
        ):
            raise ContractValidationError(
                "target_route_utilization must be within (0, 1]"
            )
        if not isinstance(self.allow_colocation, bool):
            raise ContractValidationError("allow_colocation must be a boolean")
        if (
            not isinstance(self.required_capabilities, tuple)
            or tuple(sorted(set(self.required_capabilities)))
            != self.required_capabilities
            or any(
                not isinstance(item, str) or not item
                for item in self.required_capabilities
            )
        ):
            raise ContractValidationError(
                "required_capabilities must be sorted unique strings"
            )
        for name in ("launch_options", "warmup_request"):
            frozen = freeze_canonical(getattr(self, name))
            if not isinstance(frozen, FrozenMap):
                raise ContractValidationError(f"{name} must be a mapping")
            object.__setattr__(self, name, frozen)

    @property
    def reservation(self) -> ReservationVector:
        return ReservationVector(
            cpu_num=self.instance_cpu_num,
            host_mem_mb=self.instance_host_mem_mb,
            io_slots=0,
            npu_hbm_mb=self.instance_hbm_mb,
            npu_slots=self.npu_slots,
        )

    def canonical_payload(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


class ModelInstanceState(str, Enum):
    REQUESTED = "requested"
    RESERVING = "reserving"
    STARTING = "starting"
    WARMING = "warming"
    READY = "ready"
    DRAINING = "draining"
    STOPPING = "stopping"
    FAILED = "failed"
    STOPPED = "stopped"


class ModelRouteLeaseStatus(str, Enum):
    RESERVED = "reserved"
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


@dataclass(frozen=True, slots=True)
class ModelDemand:
    demand_id: str
    run_id: str
    task_id: str
    model_id: str
    catalog_revision: str
    registered_at_ms: int

    def __post_init__(self) -> None:
        for name in ("demand_id", "run_id", "task_id", "model_id", "catalog_revision"):
            _required_string(name, getattr(self, name))
        _non_negative_int("registered_at_ms", self.registered_at_ms)


@dataclass(frozen=True, slots=True)
class ModelInstance:
    instance_id: str
    model_id: str
    catalog_revision: str
    state: ModelInstanceState
    placement_lease_id: str | None
    service_handle_id: str | None
    node_id: str | None
    boot_id: str | None
    npu_device_id: str | None
    endpoint_id: str | None
    generation: int
    created_at_ms: int
    ready_at_ms: int | None
    state_changed_at_ms: int
    route_capacity: int
    route_occupancy: int
    actual_request_inflight: int
    last_used_at_ms: int
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("instance_id", "model_id", "catalog_revision"):
            _required_string(name, getattr(self, name))
        if not isinstance(self.state, ModelInstanceState):
            raise ContractValidationError("state must be ModelInstanceState")
        for name in (
            "placement_lease_id",
            "service_handle_id",
            "node_id",
            "boot_id",
            "npu_device_id",
            "endpoint_id",
            "failure_reason",
        ):
            value = getattr(self, name)
            if value is not None:
                _required_string(name, value)
        _positive_int("generation", self.generation)
        _positive_int("route_capacity", self.route_capacity)
        for name in (
            "created_at_ms",
            "state_changed_at_ms",
            "route_occupancy",
            "actual_request_inflight",
            "last_used_at_ms",
        ):
            _non_negative_int(name, getattr(self, name))
        if self.ready_at_ms is not None:
            _non_negative_int("ready_at_ms", self.ready_at_ms)


@dataclass(frozen=True, slots=True)
class ModelRouteLeaseSnapshot:
    lease: ModelRouteLease
    status: ModelRouteLeaseStatus
    activated_at_ms: int | None
    finished_at_ms: int | None
    finish_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.lease, ModelRouteLease):
            raise ContractValidationError("lease must be ModelRouteLease")
        if not isinstance(self.status, ModelRouteLeaseStatus):
            raise ContractValidationError("status must be ModelRouteLeaseStatus")
        for name in ("activated_at_ms", "finished_at_ms"):
            value = getattr(self, name)
            if value is not None:
                _non_negative_int(name, value)
        if self.finish_reason is not None:
            _required_string("finish_reason", self.finish_reason)


@dataclass(frozen=True, slots=True)
class ModelRouteAcquireResult:
    lease: ModelRouteLease | None
    rejection_reason: str | None
    affinity_hit: bool

    def __post_init__(self) -> None:
        if (self.lease is None) == (self.rejection_reason is None):
            raise ContractValidationError(
                "route result requires exactly one lease or rejection reason"
            )
        if self.rejection_reason is not None:
            _required_string("rejection_reason", self.rejection_reason)
        if not isinstance(self.affinity_hit, bool):
            raise ContractValidationError("affinity_hit must be a boolean")
        if self.lease is None and self.affinity_hit:
            raise ContractValidationError("rejected route cannot be an affinity hit")


@dataclass(frozen=True, slots=True)
class ModelRouteContext:
    route_lease_id: str
    model_id: str
    adapter_name: str
    endpoint_id: str
    instance_id: str
    instance_generation: int

    def __post_init__(self) -> None:
        for name in (
            "route_lease_id",
            "model_id",
            "adapter_name",
            "endpoint_id",
            "instance_id",
        ):
            _required_string(name, getattr(self, name))
        _positive_int("instance_generation", self.instance_generation)


@dataclass(frozen=True, slots=True)
class InferenceWorkerConfig:
    adapter_name: str
    instance_placement_lease_id: str
    request_timeout_ms: int
    adapter_options: FrozenMap[CanonicalValue, CanonicalValue] = FrozenMap(())

    def __post_init__(self) -> None:
        _required_string("adapter_name", self.adapter_name)
        _required_string(
            "instance_placement_lease_id", self.instance_placement_lease_id
        )
        _positive_int("request_timeout_ms", self.request_timeout_ms)
        frozen = freeze_canonical(self.adapter_options)
        if not isinstance(frozen, FrozenMap):
            raise ContractValidationError("adapter_options must be a mapping")
        object.__setattr__(self, "adapter_options", frozen)


@dataclass(frozen=True, slots=True)
class ChatRequest:
    messages: tuple[FrozenMap[CanonicalValue, CanonicalValue], ...]
    max_tokens: int = 128
    temperature: float = 0.0

    def __post_init__(self) -> None:
        _positive_int("max_tokens", self.max_tokens)
        if (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not math.isfinite(float(self.temperature))
            or self.temperature < 0
        ):
            raise ContractValidationError("temperature must be finite and non-negative")
        frozen_messages: list[FrozenMap[CanonicalValue, CanonicalValue]] = []
        for message in self.messages:
            frozen = freeze_canonical(message)
            if not isinstance(frozen, FrozenMap):
                raise ContractValidationError("chat messages must be mappings")
            role = frozen.get("role")
            content = frozen.get("content")
            if not isinstance(role, str) or not role:
                raise ContractValidationError(
                    "chat messages require a non-empty string role"
                )
            _validate_chat_content(content)
            frozen_messages.append(frozen)
        if not frozen_messages:
            raise ContractValidationError("chat requires at least one message")
        object.__setattr__(self, "messages", tuple(frozen_messages))

    @classmethod
    def create(
        cls,
        messages: tuple[dict[str, object], ...] | list[dict[str, object]],
        *,
        max_tokens: int = 128,
        temperature: float = 0.0,
    ) -> "ChatRequest":
        return cls(tuple(messages), max_tokens=max_tokens, temperature=temperature)  # type: ignore[arg-type]


def _validate_chat_content(content: CanonicalValue | None) -> None:
    if isinstance(content, str):
        return
    if not isinstance(content, tuple) or not content:
        raise ContractValidationError(
            "chat message content must be a string or non-empty content parts"
        )
    for part in content:
        if not isinstance(part, FrozenMap):
            raise ContractValidationError("chat content parts must be mappings")
        part_type = part.get("type")
        if part_type == "text":
            _validate_text_content_part(part)
        elif part_type == "image_url":
            _validate_image_url_content_part(part)
        else:
            raise ContractValidationError(
                "chat content part type must be 'text' or 'image_url'"
            )


def _validate_text_content_part(
    part: FrozenMap[CanonicalValue, CanonicalValue],
) -> None:
    if set(part) != {"type", "text"}:
        raise ContractValidationError("text chat content parts require only type/text")
    if not isinstance(part.get("text"), str):
        raise ContractValidationError("text chat content part requires string text")


def _validate_image_url_content_part(
    part: FrozenMap[CanonicalValue, CanonicalValue],
) -> None:
    if set(part) != {"type", "image_url"}:
        raise ContractValidationError(
            "image_url chat content parts require only type/image_url"
        )
    image_url = part.get("image_url")
    if not isinstance(image_url, FrozenMap):
        raise ContractValidationError("image_url chat content part requires a mapping")
    allowed_keys = {"url", "detail"}
    unknown = set(image_url) - allowed_keys
    if unknown:
        raise ContractValidationError(
            "image_url mapping contains unsupported keys: "
            + ", ".join(str(key) for key in sorted(unknown, key=str))
        )
    url = image_url.get("url")
    if not isinstance(url, str) or not url:
        raise ContractValidationError("image_url mapping requires a non-empty url")
    detail = image_url.get("detail")
    if detail is not None and detail not in {"auto", "low", "high"}:
        raise ContractValidationError(
            "image_url detail must be one of auto/low/high"
        )
    if not (
        url.startswith("data:image/")
        or url.startswith("http://")
        or url.startswith("https://")
    ):
        raise ContractValidationError(
            "image_url url must be a data:image URI or an HTTP(S) URL"
        )


@dataclass(frozen=True, slots=True)
class ChatResponse:
    text: str
    finish_reason: str
    input_tokens: int
    output_tokens: int
    engine_queue_depth: int | None
    prefix_cache_hit: bool | None
    ttft_ms: int | None
    total_duration_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ContractValidationError("text must be a string")
        _required_string("finish_reason", self.finish_reason)
        for name in ("input_tokens", "output_tokens", "total_duration_ms"):
            _non_negative_int(name, getattr(self, name))
        for name in ("engine_queue_depth", "ttft_ms"):
            value = getattr(self, name)
            if value is not None:
                _non_negative_int(name, value)
        if self.prefix_cache_hit is not None and not isinstance(
            self.prefix_cache_hit, bool
        ):
            raise ContractValidationError("prefix_cache_hit must be boolean or None")


@dataclass(frozen=True, slots=True)
class InferenceRequestRecord:
    route_lease_id: str
    call_index: int
    run_id: str
    task_id: str
    attempt: int
    model_id: str
    instance_id: str
    instance_generation: int
    instance_placement_lease_id: str
    started_at_ms: int
    duration_ms: int
    status: str
    input_tokens: int | None
    output_tokens: int | None
    engine_queue_depth: int | None
    prefix_cache_hit: bool | None
    ttft_ms: int | None
    error_code: str | None

    def __post_init__(self) -> None:
        for name in (
            "route_lease_id",
            "run_id",
            "task_id",
            "model_id",
            "instance_id",
            "instance_placement_lease_id",
        ):
            _required_string(name, getattr(self, name))
        for name in ("call_index", "attempt", "instance_generation"):
            _positive_int(name, getattr(self, name))
        for name in ("started_at_ms", "duration_ms"):
            _non_negative_int(name, getattr(self, name))
        for name in (
            "input_tokens",
            "output_tokens",
            "engine_queue_depth",
            "ttft_ms",
        ):
            value = getattr(self, name)
            if value is not None:
                _non_negative_int(name, value)
        if self.status not in {"succeeded", "failed"}:
            raise ContractValidationError("inference request status is invalid")
        if self.status == "succeeded" and self.error_code is not None:
            raise ContractValidationError("succeeded request cannot carry error_code")
        if self.status == "failed":
            _required_string("error_code", self.error_code)
        if self.prefix_cache_hit is not None and not isinstance(
            self.prefix_cache_hit, bool
        ):
            raise ContractValidationError("prefix_cache_hit must be boolean or None")


@dataclass(frozen=True, slots=True)
class AttemptInferenceSummary:
    route_lease_id: str
    request_count: int
    request_inflight: bool
    context_cleared: bool

    def __post_init__(self) -> None:
        _required_string("route_lease_id", self.route_lease_id)
        _non_negative_int("request_count", self.request_count)
        if not isinstance(self.request_inflight, bool) or not isinstance(
            self.context_cleared, bool
        ):
            raise ContractValidationError("summary flags must be booleans")


@dataclass(frozen=True, slots=True)
class PortLease:
    port_lease_id: str
    node_id: str
    boot_id: str
    port: int
    owner_instance_id: str
    generation: int

    def __post_init__(self) -> None:
        for name in (
            "port_lease_id",
            "node_id",
            "boot_id",
            "owner_instance_id",
        ):
            _required_string(name, getattr(self, name))
        _positive_int("port", self.port)
        if self.port > 65_535:
            raise ContractValidationError("port must be within 1..65535")
        _positive_int("generation", self.generation)


@dataclass(frozen=True, slots=True)
class ServiceLaunchRequest:
    instance_id: str
    generation: int
    model_id: str
    artifact_revision: str
    endpoint_id: str
    port_lease_id: str
    port: int
    argv: tuple[str, ...]
    working_directory: str | None
    environment: FrozenMap[str, str]

    def __post_init__(self) -> None:
        for name in (
            "instance_id",
            "model_id",
            "artifact_revision",
            "endpoint_id",
            "port_lease_id",
        ):
            _required_string(name, getattr(self, name))
        _positive_int("generation", self.generation)
        _positive_int("port", self.port)
        if self.port > 65_535:
            raise ContractValidationError("service port must be within 1..65535")
        if (
            not isinstance(self.argv, tuple)
            or not self.argv
            or any(
                not isinstance(item, str) or not item or "\0" in item
                for item in self.argv
            )
        ):
            raise ContractValidationError("service argv must contain non-empty strings")
        if self.working_directory is not None:
            _required_string("working_directory", self.working_directory)
        if not isinstance(self.environment, FrozenMap) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.environment.items()
        ):
            raise ContractValidationError("service environment must map strings")


@dataclass(frozen=True, slots=True)
class ServiceHandle:
    service_handle_id: str
    instance_id: str
    generation: int
    endpoint_id: str
    node_id: str
    boot_id: str
    npu_device_id: str
    process_id: int
    port_lease_id: str
    port: int

    def __post_init__(self) -> None:
        for name in (
            "service_handle_id",
            "instance_id",
            "endpoint_id",
            "node_id",
            "boot_id",
            "npu_device_id",
            "port_lease_id",
        ):
            _required_string(name, getattr(self, name))
        _positive_int("generation", self.generation)
        _positive_int("process_id", self.process_id)
        _positive_int("port", self.port)
        if self.port > 65_535:
            raise ContractValidationError("service port must be within 1..65535")


@dataclass(frozen=True, slots=True)
class ServiceProcessProbe:
    process_alive: bool
    port_open: bool
    binding_verified: bool
    physical_device_id: str
    process_hbm_mb: int | None
    exit_code: int | None = None

    def __post_init__(self) -> None:
        for name in ("process_alive", "port_open", "binding_verified"):
            if not isinstance(getattr(self, name), bool):
                raise ContractValidationError(f"{name} must be a boolean")
        _required_string("physical_device_id", self.physical_device_id)
        if self.process_hbm_mb is not None:
            _non_negative_int("process_hbm_mb", self.process_hbm_mb)
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise ContractValidationError("exit_code must be an integer or None")


@dataclass(frozen=True, slots=True)
class ServiceProcessExit:
    service_handle_id: str
    instance_id: str
    generation: int
    process_id: int
    exit_code: int

    def __post_init__(self) -> None:
        for name in ("service_handle_id", "instance_id"):
            _required_string(name, getattr(self, name))
        _positive_int("generation", self.generation)
        _positive_int("process_id", self.process_id)
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise ContractValidationError("exit_code must be an integer")


@dataclass(frozen=True, slots=True)
class EngineProbe:
    process_alive: bool
    model_id: str
    artifact_revision: str
    environment_fingerprint: str
    dtype: str
    quantization: str | None
    physical_device_id: str
    process_hbm_mb: int
    request_capacity: int

    def __post_init__(self) -> None:
        if not isinstance(self.process_alive, bool):
            raise ContractValidationError("process_alive must be a boolean")
        for name in (
            "model_id",
            "artifact_revision",
            "environment_fingerprint",
            "dtype",
            "physical_device_id",
        ):
            _required_string(name, getattr(self, name))
        if self.quantization is not None:
            _required_string("quantization", self.quantization)
        _non_negative_int("process_hbm_mb", self.process_hbm_mb)
        _positive_int("request_capacity", self.request_capacity)


@dataclass(frozen=True, slots=True)
class WarmupResult:
    succeeded: bool
    duration_ms: int
    response_digest: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.succeeded, bool):
            raise ContractValidationError("succeeded must be a boolean")
        _non_negative_int("duration_ms", self.duration_ms)
        if self.response_digest is not None:
            _required_string("response_digest", self.response_digest)


@dataclass(frozen=True, slots=True)
class EngineMetrics:
    queue_depth: int
    actual_request_inflight: int

    def __post_init__(self) -> None:
        _non_negative_int("queue_depth", self.queue_depth)
        _non_negative_int("actual_request_inflight", self.actual_request_inflight)


@dataclass(frozen=True, slots=True)
class ServiceStopResult:
    process_exited: bool
    port_released: bool
    hbm_recovered: bool
    exit_code: int | None = None
    forced_termination: bool = False
    final_hbm_mb: int | None = None

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, bool)
            for value in (
                self.process_exited,
                self.port_released,
                self.hbm_recovered,
                self.forced_termination,
            )
        ):
            raise ContractValidationError("service stop confirmations must be booleans")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise ContractValidationError("exit_code must be an integer or None")
        if self.final_hbm_mb is not None:
            _non_negative_int("final_hbm_mb", self.final_hbm_mb)


@dataclass(frozen=True, slots=True)
class ModelControlEvent:
    event_type: str
    occurred_at_ms: int
    model_id: str
    instance_id: str | None = None
    instance_generation: int | None = None
    route_lease_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    attempt: int | None = None
    payload: FrozenMap[CanonicalValue, CanonicalValue] = field(
        default_factory=FrozenMap
    )

    def __post_init__(self) -> None:
        _required_string("event_type", self.event_type)
        _required_string("model_id", self.model_id)
        _non_negative_int("occurred_at_ms", self.occurred_at_ms)
        frozen = freeze_canonical(self.payload)
        if not isinstance(frozen, FrozenMap):
            raise ContractValidationError("model control payload must be a mapping")
        object.__setattr__(self, "payload", frozen)


class InferenceCallError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = _required_string("error_code", error_code)
        super().__init__(message)


@runtime_checkable
class InferenceEngineAdapter(Protocol):
    name: str

    def validate_model_spec(self, spec: ModelSpec) -> None: ...

    def worker_config(
        self,
        spec: ModelSpec,
        *,
        instance_placement_lease_id: str,
        npu_device_id: str,
    ) -> InferenceWorkerConfig: ...

    def build_launch_request(
        self,
        spec: ModelSpec,
        lease: PlacementLease,
        port_lease: PortLease,
    ) -> ServiceLaunchRequest: ...

    async def probe(self, handle: ServiceHandle, spec: ModelSpec) -> EngineProbe: ...

    async def warmup(self, handle: ServiceHandle, spec: ModelSpec) -> WarmupResult: ...

    async def invoke_chat(
        self,
        context: ModelRouteContext,
        request: ChatRequest,
    ) -> ChatResponse: ...

    async def read_metrics(self, handle: ServiceHandle) -> EngineMetrics: ...


@runtime_checkable
class PortLeaseManager(Protocol):
    async def acquire(
        self,
        *,
        node_id: str,
        boot_id: str,
        owner_instance_id: str,
        generation: int,
    ) -> PortLease: ...

    async def release(self, lease: PortLease) -> bool: ...

    def active_count(self) -> int: ...


@runtime_checkable
class ServiceProcessBackend(Protocol):
    async def launch(
        self,
        request: ServiceLaunchRequest,
        lease: PlacementLease,
    ) -> ServiceHandle: ...

    async def probe_process(
        self,
        handle: ServiceHandle,
        *,
        timeout_ms: int,
    ) -> ServiceProcessProbe: ...

    async def stop(
        self,
        handle: ServiceHandle,
        *,
        timeout_ms: int,
    ) -> ServiceStopResult: ...


def model_catalog_digest(specs: tuple[ModelSpec, ...]) -> str:
    return canonical_digest(tuple(spec.canonical_payload() for spec in specs))
