"""Deterministic engine and service-process fake for C11 control tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import RLock

from ascend_maze.contracts.resources import PlacementLease
from ascend_maze.core.canonical import FrozenMap
from ascend_maze.core.errors import ContractValidationError
from ascend_maze.core.identifiers import new_id
from ascend_maze.core.time import monotonic_time_ms
from ascend_maze.inference.contracts import (
    ChatRequest,
    ChatResponse,
    EngineMetrics,
    EngineProbe,
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
class FakeAdapterPlan:
    launch_delay_ms: int = 0
    probe_delay_ms: int = 0
    warmup_delay_ms: int = 0
    invoke_delay_ms: int = 0
    stop_delay_ms: int = 0
    fail_build_launch: str | None = None
    fail_launch: str | None = None
    fail_attach: str | None = None
    fail_probe: str | None = None
    fail_warmup: str | None = None
    fail_invoke: str | None = None
    process_hbm_mb: int | None = None
    wrong_model_id: str | None = None
    wrong_device_id: str | None = None
    wrong_service_handle_field: str | None = None
    stop_process_exited: bool = True
    stop_port_released: bool = True
    stop_hbm_recovered: bool = True

    def __post_init__(self) -> None:
        for name in (
            "launch_delay_ms",
            "probe_delay_ms",
            "warmup_delay_ms",
            "invoke_delay_ms",
            "stop_delay_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"{name} must be non-negative")
        if self.wrong_service_handle_field not in {
            None,
            "instance_id",
            "generation",
            "endpoint_id",
            "node_id",
            "boot_id",
            "npu_device_id",
        }:
            raise ContractValidationError("unsupported ServiceHandle fault field")


class FakeInferenceEngineAdapter:
    name = "fake"

    def __init__(self) -> None:
        self._plans: dict[str, FakeAdapterPlan] = {}
        self._specs_by_endpoint: dict[str, ModelSpec] = {}
        self._handles: dict[str, ServiceHandle] = {}
        self._inflight_by_endpoint: dict[str, int] = {}
        self._next_pid = 10_000
        self._launch_count = 0
        self._stop_count = 0
        self._invoke_count = 0
        self._lock = RLock()

    def set_plan(self, model_id: str, plan: FakeAdapterPlan) -> None:
        if not model_id:
            raise ValueError("model_id is required")
        self._plans[model_id] = plan

    def validate_model_spec(self, spec: ModelSpec) -> None:
        if spec.backend != self.name:
            raise ContractValidationError("Fake adapter only accepts backend='fake'")
        allowed = {"response_prefix"}
        unknown = {str(key) for key in spec.launch_options} - allowed
        if unknown:
            raise ContractValidationError(
                "unsupported Fake adapter launch options: " + ", ".join(sorted(unknown))
            )
        prefix = spec.launch_options.get("response_prefix")
        if prefix is not None and not isinstance(prefix, str):
            raise ContractValidationError("response_prefix must be a string")

    def worker_config(
        self,
        spec: ModelSpec,
        *,
        instance_placement_lease_id: str,
        npu_device_id: str,
    ) -> InferenceWorkerConfig:
        del npu_device_id
        plan = self._plan(spec.model_id)
        prefix = spec.launch_options.get("response_prefix", spec.model_id)
        assert isinstance(prefix, str)
        return InferenceWorkerConfig(
            adapter_name=self.name,
            instance_placement_lease_id=instance_placement_lease_id,
            request_timeout_ms=30_000,
            adapter_options=FrozenMap(
                (
                    ("response_prefix", prefix),
                    ("invoke_delay_ms", plan.invoke_delay_ms),
                    ("fail_invoke", plan.fail_invoke),
                )
            ),
        )

    def build_launch_request(
        self,
        spec: ModelSpec,
        lease: PlacementLease,
        port_lease: PortLease,
    ) -> ServiceLaunchRequest:
        plan = self._plan(spec.model_id)
        if plan.fail_build_launch is not None:
            raise RuntimeError(plan.fail_build_launch)
        if lease.npu_device_id is None:
            raise ContractValidationError(
                "model instance lease requires a physical NPU"
            )
        return ServiceLaunchRequest(
            instance_id=port_lease.owner_instance_id,
            generation=port_lease.generation,
            model_id=spec.model_id,
            artifact_revision=spec.artifact_revision,
            endpoint_id=f"fake://{lease.node_id}:{port_lease.port}/{port_lease.owner_instance_id}/{port_lease.generation}",
            port_lease_id=port_lease.port_lease_id,
            port=port_lease.port,
            argv=("fake-engine", spec.model_id),
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
        plan = self._plan(request.model_id)
        await self._delay(plan.launch_delay_ms)
        if plan.fail_launch is not None:
            raise RuntimeError(plan.fail_launch)
        with self._lock:
            self._next_pid += 1
            wrong = plan.wrong_service_handle_field
            handle = ServiceHandle(
                service_handle_id=new_id("service"),
                instance_id=(
                    "wrong_instance" if wrong == "instance_id" else request.instance_id
                ),
                generation=(
                    request.generation + 1
                    if wrong == "generation"
                    else request.generation
                ),
                endpoint_id=(
                    f"{request.endpoint_id}/wrong"
                    if wrong == "endpoint_id"
                    else request.endpoint_id
                ),
                node_id="wrong_node" if wrong == "node_id" else lease.node_id,
                boot_id="wrong_boot" if wrong == "boot_id" else lease.boot_id,
                npu_device_id=(
                    "wrong_device"
                    if wrong == "npu_device_id"
                    else lease.npu_device_id or ""
                ),
                process_id=self._next_pid,
                port_lease_id=request.port_lease_id,
                port=request.port,
            )
            self._handles[handle.service_handle_id] = handle
            self._launch_count += 1
            return handle

    def attach_spec(self, handle: ServiceHandle, spec: ModelSpec) -> None:
        plan = self._plan(spec.model_id)
        if plan.fail_attach is not None:
            raise RuntimeError(plan.fail_attach)
        with self._lock:
            self._specs_by_endpoint[handle.endpoint_id] = spec
            self._inflight_by_endpoint.setdefault(handle.endpoint_id, 0)

    async def probe(self, handle: ServiceHandle, spec: ModelSpec) -> EngineProbe:
        plan = self._plan(spec.model_id)
        await self._delay(plan.probe_delay_ms)
        if plan.fail_probe is not None:
            raise RuntimeError(plan.fail_probe)
        return EngineProbe(
            process_alive=handle.service_handle_id in self._handles,
            model_id=plan.wrong_model_id or spec.model_id,
            artifact_revision=spec.artifact_revision,
            environment_fingerprint=spec.environment_fingerprint,
            dtype=spec.dtype,
            quantization=spec.quantization,
            physical_device_id=plan.wrong_device_id or handle.npu_device_id,
            process_hbm_mb=(
                spec.weight_hbm_mb
                if plan.process_hbm_mb is None
                else plan.process_hbm_mb
            ),
            request_capacity=spec.request_capacity,
        )

    async def warmup(self, handle: ServiceHandle, spec: ModelSpec) -> WarmupResult:
        del handle
        plan = self._plan(spec.model_id)
        await self._delay(plan.warmup_delay_ms)
        if plan.fail_warmup is not None:
            raise RuntimeError(plan.fail_warmup)
        return WarmupResult(
            succeeded=True,
            duration_ms=plan.warmup_delay_ms,
            response_digest="fake-warmup-response",
        )

    async def invoke_chat(
        self,
        context: ModelRouteContext,
        request: ChatRequest,
    ) -> ChatResponse:
        with self._lock:
            spec = self._specs_by_endpoint.get(context.endpoint_id)
            if spec is None:
                raise RuntimeError("fake endpoint is not active")
            self._inflight_by_endpoint[context.endpoint_id] += 1
            self._invoke_count += 1
        plan = self._plan(spec.model_id)
        started = monotonic_time_ms()
        try:
            await self._delay(plan.invoke_delay_ms)
            if plan.fail_invoke is not None:
                raise RuntimeError(plan.fail_invoke)
            content = _content_text_preview(request.messages[-1]["content"])
            prefix = spec.launch_options.get("response_prefix", spec.model_id)
            assert isinstance(prefix, str)
            text = f"{prefix}:{content}"
            return ChatResponse(
                text=text,
                finish_reason="stop",
                input_tokens=max(1, len(content.split())),
                output_tokens=max(1, len(text.split())),
                engine_queue_depth=0,
                prefix_cache_hit=False,
                ttft_ms=plan.invoke_delay_ms,
                total_duration_ms=max(0, monotonic_time_ms() - started),
            )
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
        spec = self._specs_by_endpoint.get(handle.endpoint_id)
        plan = FakeAdapterPlan() if spec is None else self._plan(spec.model_id)
        alive = handle.service_handle_id in self._handles
        return ServiceProcessProbe(
            process_alive=alive,
            port_open=alive,
            binding_verified=alive and plan.wrong_device_id is None,
            physical_device_id=plan.wrong_device_id or handle.npu_device_id,
            process_hbm_mb=(
                None
                if spec is None
                else (
                    spec.weight_hbm_mb
                    if plan.process_hbm_mb is None
                    else plan.process_hbm_mb
                )
            ),
            exit_code=None if alive else 1,
        )

    async def stop(
        self,
        handle: ServiceHandle,
        *,
        timeout_ms: int,
    ) -> ServiceStopResult:
        del timeout_ms
        spec = self._specs_by_endpoint.get(handle.endpoint_id)
        plan = FakeAdapterPlan() if spec is None else self._plan(spec.model_id)
        await self._delay(plan.stop_delay_ms)
        result = ServiceStopResult(
            process_exited=plan.stop_process_exited,
            port_released=plan.stop_port_released,
            hbm_recovered=plan.stop_hbm_recovered,
        )
        if all((result.process_exited, result.port_released, result.hbm_recovered)):
            with self._lock:
                self._handles.pop(handle.service_handle_id, None)
                self._specs_by_endpoint.pop(handle.endpoint_id, None)
                self._inflight_by_endpoint.pop(handle.endpoint_id, None)
                self._stop_count += 1
        return result

    def crash_instance(self, instance_id: str, generation: int) -> ServiceHandle:
        with self._lock:
            handle = next(
                (
                    item
                    for item in self._handles.values()
                    if item.instance_id == instance_id and item.generation == generation
                ),
                None,
            )
            if handle is None:
                raise KeyError("fake service instance is not running")
            del self._handles[handle.service_handle_id]
            self._specs_by_endpoint.pop(handle.endpoint_id, None)
            self._inflight_by_endpoint.pop(handle.endpoint_id, None)
            return handle

    def is_process_alive(self, instance_id: str, generation: int) -> bool:
        with self._lock:
            return any(
                handle.instance_id == instance_id and handle.generation == generation
                for handle in self._handles.values()
            )

    @property
    def launch_count(self) -> int:
        return self._launch_count

    @property
    def stop_count(self) -> int:
        return self._stop_count

    @property
    def invoke_count(self) -> int:
        return self._invoke_count

    def _plan(self, model_id: str) -> FakeAdapterPlan:
        return self._plans.get(model_id, FakeAdapterPlan())

    @staticmethod
    async def _delay(milliseconds: int) -> None:
        if milliseconds:
            await asyncio.sleep(milliseconds / 1_000)


def _content_text_preview(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, tuple):
        fragments: list[str] = []
        image_count = 0
        for part in value:
            if not isinstance(part, FrozenMap):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                fragments.append(str(part["text"]))
            elif part.get("type") == "image_url":
                image_count += 1
        if image_count:
            fragments.append(f"[{image_count} image(s)]")
        return " ".join(fragment for fragment in fragments if fragment)
    return str(value)
