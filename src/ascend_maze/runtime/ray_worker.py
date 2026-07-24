"""Hard-placed cold one-shot Ray Worker for Host tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any

import ray

from ascend_maze.contracts.data import DataHandle
from ascend_maze.contracts.errors import ErrorInfo
from ascend_maze.contracts.resources import (
    ExecutionTarget,
    PlacementLease,
    ResourceObservation,
    ResourceSpec,
)
from ascend_maze.contracts.runtime import (
    CodePackage,
    DeviceBinding,
    ExecutionRequest,
    RuntimeNodeBinding,
)
from ascend_maze.contracts.worker import (
    StandbyWarmupReport,
    WarmupManifest,
    WorkerLease,
    WorkerProfile,
)
from ascend_maze.core.canonical import FrozenMap
from ascend_maze.core.time import monotonic_time_ms
from ascend_maze.data.ray_store import RayDataStore, RayDataStoreDescriptor
from ascend_maze.inference.context import AttemptInferenceSession, install_route_session
from ascend_maze.inference.contracts import (
    AttemptInferenceSummary,
    InferenceCallError,
    InferenceRequestRecord,
    InferenceWorkerConfig,
)
from ascend_maze.inference.worker_client import create_worker_inference_client
from ascend_maze.runtime.code_loader import load_code_package
from ascend_maze.runtime.events import RuntimeEvent, RuntimeEventKind
from ascend_maze.ascend.torch_runtime import (
    BoundTorchNpuRuntime,
    bind_torch_npu_device,
    contains_npu_tensor,
    host_peak_rss_mb,
    platform_error_code,
)
from ascend_maze.ascend.dcmi import DcmiDeviceAdapter

from ascend_maze.control.node_rpc import (
    NodeAgentIdentity,
    report_worker_event,
)


@dataclass(frozen=True, slots=True)
class RayWorkerOutcome:
    dispatch_id: str
    ray_node_id: str
    worker_pid: int
    worker_started_delivered: bool
    terminal_event: RuntimeEvent
    terminal_event_delivered: bool
    physical_device_id: str | None = None
    binding_verified: bool = False
    reuse_safe: bool = False
    cleanup_reason: str | None = None
    task_timing: "RayTaskTimingRecord | None" = None


@dataclass(frozen=True, slots=True)
class RayTaskTimingRecord:
    dispatch_id: str
    run_id: str
    task_id: str
    attempt: int
    task_kind: str
    execution_target: str
    route_lease_id: str | None
    started_at_ms: int
    status: str
    error_code: str | None
    input_fetch_ms: int
    callable_execute_ms: int
    chat_request_ms: int
    output_put_ms: int
    task_total_ms: int
    input_handle_count: int
    output_count: int
    inference_metrics: tuple[dict[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "dispatch_id": self.dispatch_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "task_kind": self.task_kind,
            "execution_target": self.execution_target,
            "route_lease_id": self.route_lease_id,
            "started_at_ms": self.started_at_ms,
            "status": self.status,
            "error_code": self.error_code,
            "input_fetch_ms": self.input_fetch_ms,
            "input_fetch_scope": "ray_materialized_argument_binding",
            "callable_execute_ms": self.callable_execute_ms,
            "chat_request_ms": self.chat_request_ms,
            "output_put_ms": self.output_put_ms,
            "output_put_scope": "ray_data_store_put_staged",
            "task_total_ms": self.task_total_ms,
            "input_handle_count": self.input_handle_count,
            "output_count": self.output_count,
            "inference_metrics": [dict(item) for item in self.inference_metrics],
            "task_runtime_overhead_ms": max(
                0,
                self.task_total_ms
                - self.input_fetch_ms
                - self.callable_execute_ms
                - self.output_put_ms,
            ),
            "callable_minus_chat_ms": max(
                0,
                self.callable_execute_ms - self.chat_request_ms,
            ),
        }


@dataclass(frozen=True, slots=True)
class _RayUserCodeOutcome:
    terminal_event: RuntimeEvent
    task_timing: RayTaskTimingRecord


_RAY_DATA_ARGUMENT_PREFIX = "_ascend_maze_data_argument_"


def ray_data_argument_keyword(argument_index: int) -> str:
    if (
        isinstance(argument_index, bool)
        or not isinstance(argument_index, int)
        or argument_index < 0
    ):
        raise ValueError("argument_index must be a non-negative integer")
    return f"{_RAY_DATA_ARGUMENT_PREFIX}{argument_index}"


class _WorkerRouteReporter:
    def __init__(
        self,
        *,
        request: ExecutionRequest,
        placement_lease: PlacementLease,
        binding: RuntimeNodeBinding,
        agent_identity: NodeAgentIdentity,
        controller_generation: str,
        event_timeout_seconds: float,
    ) -> None:
        self.request = request
        self.placement_lease = placement_lease
        self.binding = binding
        self.agent_identity = agent_identity
        self.controller_generation = controller_generation
        self.event_timeout_seconds = event_timeout_seconds
        self._call_index = 0
        self._chat_request_ms = 0

    @property
    def chat_request_ms(self) -> int:
        return self._chat_request_ms

    def request_started(self, route_lease_id: str) -> object:
        self._validate_route(route_lease_id)
        self._call_index += 1
        self._report(
            RuntimeEvent.create(
                kind=RuntimeEventKind.INFERENCE_REQUEST_STARTED,
                dispatch_id=self.request.dispatch_id,
                run_id=self.request.run_id,
                task_id=self.request.task_id,
                attempt=self.request.attempt,
                lease_id=self.placement_lease.lease_id,
                route_lease_id=route_lease_id,
                occurred_at_ms=monotonic_time_ms(),
                worker_pid=os.getpid(),
                inference_call_index=self._call_index,
            )
        )
        assert self.request.model_route is not None
        return self.request.model_route

    def request_finished(self, route_lease_id: str) -> None:
        self._validate_route(route_lease_id)

    def record(self, record: InferenceRequestRecord) -> None:
        self._validate_route(record.route_lease_id)
        if record.call_index != self._call_index:
            raise RuntimeError("Worker inference record call_index mismatch")
        self._chat_request_ms += record.duration_ms
        self._report(
            RuntimeEvent.create(
                kind=RuntimeEventKind.INFERENCE_REQUEST_FINISHED,
                dispatch_id=self.request.dispatch_id,
                run_id=self.request.run_id,
                task_id=self.request.task_id,
                attempt=self.request.attempt,
                lease_id=self.placement_lease.lease_id,
                route_lease_id=record.route_lease_id,
                occurred_at_ms=monotonic_time_ms(),
                worker_pid=os.getpid(),
                inference_request=record,
            )
        )

    def _validate_route(self, route_lease_id: str) -> None:
        route = self.request.model_route
        if route is None or route.route_lease_id != route_lease_id:
            raise RuntimeError("Worker inference route identity mismatch")

    def _report(self, event: RuntimeEvent) -> None:
        try:
            report_worker_event(
                endpoint=self.binding.agent_endpoint,
                identity=self.agent_identity,
                controller_generation=self.controller_generation,
                runtime_generation=self.binding.runtime_generation,
                event=event,
                timeout_seconds=self.event_timeout_seconds,
            )
        except Exception as exc:
            raise InferenceCallError(
                "model_route_reporting_failed",
                f"NodeAgent rejected inference lifecycle event: {type(exc).__name__}: {exc}",
            ) from exc


def _current_rss_mb() -> int:
    try:
        fields = Path("/proc/self/statm").read_text(encoding="ascii").split()
        return int(int(fields[1]) * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024))
    except (OSError, ValueError, IndexError):
        return host_peak_rss_mb()


def _child_process_ids() -> frozenset[int]:
    try:
        text = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").read_text(
            encoding="ascii"
        )
        return frozenset(int(value) for value in text.split())
    except (OSError, ValueError):
        return frozenset()


def _open_file_descriptors() -> frozenset[int]:
    try:
        critical: set[int] = set()
        for item in Path("/proc/self/fd").iterdir():
            if not item.name.isdigit():
                continue
            try:
                target = os.readlink(item)
            except OSError:
                continue
            if target.startswith(("socket:[", "pipe:[", "anon_inode:")):
                continue
            if target == "/dev/null" or target.startswith("/proc/"):
                continue
            critical.add(int(item.name))
        return frozenset(critical)
    except OSError:
        return frozenset()


def _terminate_child_processes(process_ids: frozenset[int]) -> None:
    for process_id in process_ids:
        try:
            os.kill(process_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 0.2
    remaining = set(process_ids)
    while remaining and time.monotonic() < deadline:
        remaining = {
            process_id
            for process_id in remaining
            if Path(f"/proc/{process_id}").exists()
        }
        if remaining:
            time.sleep(0.01)
    for process_id in remaining:
        try:
            os.kill(process_id, signal.SIGKILL)
        except ProcessLookupError:
            pass


class _RayStandbyWorker:
    """Host-warmed Actor that delegates every Attempt to the common runner."""

    def __init__(
        self,
        *,
        worker_id: str,
        worker_generation: int,
        profile: WorkerProfile,
        warmup_manifest: WarmupManifest,
        max_tasks_per_worker: int,
        max_worker_lifetime_ms: int,
        max_rss_growth_mb: int,
    ) -> None:
        started = monotonic_time_ms()
        self.worker_id = worker_id
        self.worker_generation = worker_generation
        self.profile = profile
        self.max_tasks_per_worker = max_tasks_per_worker
        self.max_worker_lifetime_ms = max_worker_lifetime_ms
        self.max_rss_growth_mb = max_rss_growth_mb
        self._busy = False
        self._tasks_completed = 0
        for module in warmup_manifest.modules:
            importlib.import_module(module)
        self._created_at_ms = monotonic_time_ms()
        self._baseline_environment = dict(os.environ)
        self._baseline_cwd = os.getcwd()
        self._baseline_threads = frozenset(
            thread.ident for thread in threading.enumerate() if thread.ident is not None
        )
        self._baseline_children = _child_process_ids()
        self._baseline_fds = _open_file_descriptors()
        self._baseline_rss_mb = _current_rss_mb()
        zero_hbm_verified = profile is not WorkerProfile.NPU_HOST
        zero_hbm_error: str | None = None
        context_device_ids: tuple[str, ...] = ()
        npu_used_hbm_mb: tuple[tuple[str, int], ...] = ()
        if profile is WorkerProfile.NPU_HOST:
            try:
                devices = DcmiDeviceAdapter().devices()
                context_device_ids = tuple(
                    sorted(
                        device.physical_device_id
                        for device in devices
                        if any(
                            process.pid == os.getpid() for process in device.processes
                        )
                    )
                )
                npu_used_hbm_mb = tuple(
                    sorted(
                        (device.physical_device_id, device.used_hbm_mb)
                        for device in devices
                    )
                )
                zero_hbm_verified = not context_device_ids
            except Exception as exc:
                zero_hbm_error = f"{type(exc).__name__}: {exc}"
        self._warmup_report = StandbyWarmupReport(
            worker_id=worker_id,
            worker_generation=worker_generation,
            ray_node_id=ray.get_runtime_context().get_node_id(),
            worker_pid=os.getpid(),
            imported_modules=warmup_manifest.modules,
            forbidden_device_modules=tuple(
                name
                for name in sorted(sys.modules)
                if name in {"acl", "torch_npu"}
                or name.startswith(("acl.", "torch_npu."))
            ),
            host_rss_mb=self._baseline_rss_mb,
            host_warmup_ms=max(0, monotonic_time_ms() - started),
            zero_hbm_verified=zero_hbm_verified,
            zero_hbm_error=zero_hbm_error,
            npu_context_device_ids=context_device_ids,
            npu_used_hbm_mb=npu_used_hbm_mb,
        )

    def ready(self) -> StandbyWarmupReport:
        return self._warmup_report

    def execute(self, **kwargs: Any) -> RayWorkerOutcome:
        worker_lease = kwargs.get("worker_lease")
        if not isinstance(worker_lease, WorkerLease):
            raise TypeError("Standby Worker requires a WorkerLease")
        if (
            worker_lease.worker_id != self.worker_id
            or worker_lease.worker_generation != self.worker_generation
            or worker_lease.profile is not self.profile
        ):
            raise RuntimeError("worker_generation_mismatch")
        if self._busy:
            raise RuntimeError("Standby Worker already has an active Attempt")
        self._busy = True
        try:
            outcome = _execute_one_shot(**kwargs)
            self._tasks_completed += 1
            if self.profile is WorkerProfile.NPU_HOST:
                return outcome
            cleanup_reason = self._sanitize()
            return RayWorkerOutcome(
                dispatch_id=outcome.dispatch_id,
                ray_node_id=outcome.ray_node_id,
                worker_pid=outcome.worker_pid,
                worker_started_delivered=outcome.worker_started_delivered,
                terminal_event=outcome.terminal_event,
                terminal_event_delivered=outcome.terminal_event_delivered,
                physical_device_id=outcome.physical_device_id,
                binding_verified=outcome.binding_verified,
                reuse_safe=cleanup_reason is None,
                cleanup_reason=cleanup_reason,
                task_timing=outcome.task_timing,
            )
        finally:
            self._busy = False

    def shutdown(self) -> None:
        ray.actor.exit_actor()

    def _sanitize(self) -> str | None:
        leaked_children = _child_process_ids() - self._baseline_children
        if leaked_children:
            _terminate_child_processes(leaked_children)
            return "child_process_leaked"
        # A retiring actor releases its threads, descriptors, RSS and environment
        # with the process.  Only run reuse checks when the actor can return idle.
        if self._tasks_completed >= self.max_tasks_per_worker:
            return "task_limit_reached"
        if monotonic_time_ms() - self._created_at_ms >= self.max_worker_lifetime_ms:
            return "worker_lifetime_exceeded"
        try:
            os.chdir(self._baseline_cwd)
            for name in tuple(os.environ):
                if name not in self._baseline_environment:
                    del os.environ[name]
            os.environ.update(self._baseline_environment)
        except OSError as exc:
            return f"environment_restore_failed:{type(exc).__name__}"
        threads = frozenset(
            thread.ident for thread in threading.enumerate() if thread.ident is not None
        )
        if threads - self._baseline_threads:
            return "background_thread_leaked"
        if _open_file_descriptors() - self._baseline_fds:
            return "file_descriptor_leaked"
        if _current_rss_mb() - self._baseline_rss_mb > self.max_rss_growth_mb:
            return "rss_limit_exceeded"
        return None


def _execute_one_shot(
    *,
    request: ExecutionRequest,
    placement_lease: PlacementLease,
    worker_lease: WorkerLease,
    binding: RuntimeNodeBinding,
    agent_identity: NodeAgentIdentity,
    controller_generation: str,
    data_store_descriptor: RayDataStoreDescriptor,
    code_package: CodePackage,
    event_timeout_seconds: float,
    device_binding: DeviceBinding | None,
    inference_config: InferenceWorkerConfig | None,
    **resolved_data_arguments: object,
) -> RayWorkerOutcome:
    worker_pid = os.getpid()
    ray_node_id = ray.get_runtime_context().get_node_id()
    store = RayDataStore.connect(data_store_descriptor)
    started_delivered = False
    if (
        ray_node_id != binding.ray_node_id
        or placement_lease.node_id != binding.node_id
        or placement_lease.boot_id != binding.boot_id
        or worker_lease.node_id != binding.node_id
        or worker_lease.boot_id != binding.boot_id
    ):
        terminal = _failure_event(
            request=request,
            lease=placement_lease,
            worker_lease=worker_lease,
            binding=binding,
            kind=RuntimeEventKind.DISPATCH_FAILED,
            error_code="runtime_node_unavailable",
            category="runtime",
            phase="dispatched",
            message="Ray Worker did not start on the leased node generation",
        )
        delivered = _try_report(
            binding.agent_endpoint,
            agent_identity,
            controller_generation,
            binding.runtime_generation,
            terminal,
            event_timeout_seconds,
        )
        return RayWorkerOutcome(
            request.dispatch_id,
            ray_node_id,
            worker_pid,
            False,
            terminal,
            delivered,
            None,
            False,
        )

    npu_runtime: BoundTorchNpuRuntime | None = None
    local_npu = (
        (
            request.execution_target is ExecutionTarget.LOCAL_WORKER
            and request.task_kind == "npu"
        )
        or (
            request.execution_target is ExecutionTarget.MODEL_SERVICE
            and inference_config is not None
            and inference_config.adapter_name == "transformers_local"
        )
    )
    if local_npu:
        if device_binding is None:
            terminal = _failure_event(
                request=request,
                lease=placement_lease,
                worker_lease=worker_lease,
                binding=binding,
                kind=RuntimeEventKind.DISPATCH_FAILED,
                error_code="device_bind_failed",
                category="node_device",
                phase="dispatched",
                message="NPU Worker did not receive a DeviceBinding",
                device_binding=None,
                npu_runtime=None,
            )
            delivered = _try_report(
                binding.agent_endpoint,
                agent_identity,
                controller_generation,
                binding.runtime_generation,
                terminal,
                event_timeout_seconds,
            )
            return RayWorkerOutcome(
                request.dispatch_id,
                ray_node_id,
                worker_pid,
                False,
                terminal,
                delivered,
            )
        try:
            npu_runtime = bind_torch_npu_device(device_binding)
        except Exception as exc:
            terminal = _failure_event(
                request=request,
                lease=placement_lease,
                worker_lease=worker_lease,
                binding=binding,
                kind=RuntimeEventKind.DISPATCH_FAILED,
                error_code="device_bind_failed",
                category="node_device",
                phase="dispatched",
                message=f"{type(exc).__name__}: {exc}",
                exception=exc,
                device_binding=device_binding,
                npu_runtime=None,
            )
            delivered = _try_report(
                binding.agent_endpoint,
                agent_identity,
                controller_generation,
                binding.runtime_generation,
                terminal,
                event_timeout_seconds,
            )
            return RayWorkerOutcome(
                request.dispatch_id,
                ray_node_id,
                worker_pid,
                False,
                terminal,
                delivered,
                device_binding.physical_device_id,
                False,
            )
    elif device_binding is not None:
        terminal = _failure_event(
            request=request,
            lease=placement_lease,
            worker_lease=worker_lease,
            binding=binding,
            kind=RuntimeEventKind.DISPATCH_FAILED,
            error_code="device_bind_failed",
            category="node_device",
            phase="dispatched",
            message="CPU/I/O Worker unexpectedly received a DeviceBinding",
            device_binding=device_binding,
            npu_runtime=None,
        )
        delivered = _try_report(
            binding.agent_endpoint,
            agent_identity,
            controller_generation,
            binding.runtime_generation,
            terminal,
            event_timeout_seconds,
        )
        return RayWorkerOutcome(
            request.dispatch_id,
            ray_node_id,
            worker_pid,
            False,
            terminal,
            delivered,
            device_binding.physical_device_id,
            False,
        )

    if request.execution_target is ExecutionTarget.MODEL_SERVICE and (
        request.model_route is None
        or inference_config is None
        or request.model_route.adapter_name != inference_config.adapter_name
    ):
        terminal = _failure_event(
            request=request,
            lease=placement_lease,
            worker_lease=worker_lease,
            binding=binding,
            kind=RuntimeEventKind.DISPATCH_FAILED,
            error_code="model_route_invalidated",
            category="model",
            phase="dispatched",
            message="service Worker did not receive a matching inference config",
        )
        delivered = _try_report(
            binding.agent_endpoint,
            agent_identity,
            controller_generation,
            binding.runtime_generation,
            terminal,
            event_timeout_seconds,
        )
        return RayWorkerOutcome(
            request.dispatch_id,
            ray_node_id,
            worker_pid,
            False,
            terminal,
            delivered,
        )
    if (
        request.execution_target is ExecutionTarget.LOCAL_WORKER
        and inference_config is not None
    ):
        terminal = _failure_event(
            request=request,
            lease=placement_lease,
            worker_lease=worker_lease,
            binding=binding,
            kind=RuntimeEventKind.DISPATCH_FAILED,
            error_code="model_protocol_failed",
            category="model",
            phase="dispatched",
            message="local Worker unexpectedly received an inference config",
            device_binding=device_binding,
            npu_runtime=npu_runtime,
        )
        delivered = _try_report(
            binding.agent_endpoint,
            agent_identity,
            controller_generation,
            binding.runtime_generation,
            terminal,
            event_timeout_seconds,
        )
        return RayWorkerOutcome(
            request.dispatch_id,
            ray_node_id,
            worker_pid,
            False,
            terminal,
            delivered,
            None if device_binding is None else device_binding.physical_device_id,
            npu_runtime is not None,
        )
    started = RuntimeEvent.create(
        kind=RuntimeEventKind.WORKER_STARTED,
        dispatch_id=request.dispatch_id,
        run_id=request.run_id,
        task_id=request.task_id,
        attempt=request.attempt,
        lease_id=placement_lease.lease_id,
        route_lease_id=_route_lease_id(request),
        occurred_at_ms=monotonic_time_ms(),
        worker_pid=worker_pid,
        device_id=(
            None if device_binding is None else device_binding.physical_device_id
        ),
        binding_verified=npu_runtime is not None,
    )
    started_delivered = _try_report(
        binding.agent_endpoint,
        agent_identity,
        controller_generation,
        binding.runtime_generation,
        started,
        event_timeout_seconds,
    )
    if not started_delivered:
        terminal = _failure_event(
            request=request,
            lease=placement_lease,
            worker_lease=worker_lease,
            binding=binding,
            kind=RuntimeEventKind.DISPATCH_FAILED,
            error_code="worker_start_failed",
            category="worker",
            phase="dispatched",
            message="WorkerStarted could not be delivered to NodeAgent",
            device_binding=device_binding,
            npu_runtime=npu_runtime,
        )
        return RayWorkerOutcome(
            request.dispatch_id,
            ray_node_id,
            worker_pid,
            False,
            terminal,
            False,
            None if device_binding is None else device_binding.physical_device_id,
            npu_runtime is not None,
        )

    user_code_outcome = _run_user_code(
        request=request,
        placement_lease=placement_lease,
        worker_lease=worker_lease,
        binding=binding,
        store=store,
        code_package=code_package,
        resolved_data_arguments=resolved_data_arguments,
        device_binding=device_binding,
        npu_runtime=npu_runtime,
        inference_config=inference_config,
        agent_identity=agent_identity,
        controller_generation=controller_generation,
        event_timeout_seconds=event_timeout_seconds,
        started_at_ms=started.occurred_at_ms,
    )
    terminal = user_code_outcome.terminal_event
    delivered = _try_report(
        binding.agent_endpoint,
        agent_identity,
        controller_generation,
        binding.runtime_generation,
        terminal,
        event_timeout_seconds,
    )
    return RayWorkerOutcome(
        request.dispatch_id,
        ray_node_id,
        worker_pid,
        started_delivered,
        terminal,
        delivered,
        None if device_binding is None else device_binding.physical_device_id,
        npu_runtime is not None,
        task_timing=user_code_outcome.task_timing,
    )


def _run_user_code(
    *,
    request: ExecutionRequest,
    placement_lease: PlacementLease,
    worker_lease: WorkerLease,
    binding: RuntimeNodeBinding,
    store: RayDataStore,
    code_package: CodePackage,
    resolved_data_arguments: dict[str, object],
    device_binding: DeviceBinding | None,
    npu_runtime: BoundTorchNpuRuntime | None,
    inference_config: InferenceWorkerConfig | None,
    agent_identity: NodeAgentIdentity,
    controller_generation: str,
    event_timeout_seconds: float,
    started_at_ms: int,
) -> _RayUserCodeOutcome:
    task_started_perf = time.perf_counter()
    input_started_perf = time.perf_counter()
    input_fetch_ms = 0
    callable_execute_ms = 0
    output_put_ms = 0
    output_count = 0
    reporter: _WorkerRouteReporter | None = None
    inference_client: Any | None = None

    def complete(
        event: RuntimeEvent,
        *,
        status: str,
        error_code: str | None,
    ) -> _RayUserCodeOutcome:
        metrics_snapshot = getattr(inference_client, "invocation_records", None)
        inference_metrics = (
            tuple(dict(item) for item in metrics_snapshot())
            if callable(metrics_snapshot)
            else ()
        )
        return _RayUserCodeOutcome(
            terminal_event=event,
            task_timing=RayTaskTimingRecord(
                dispatch_id=request.dispatch_id,
                run_id=request.run_id,
                task_id=request.task_id,
                attempt=request.attempt,
                task_kind=request.task_kind,
                execution_target=request.execution_target.value,
                route_lease_id=_route_lease_id(request),
                started_at_ms=started_at_ms,
                status=status,
                error_code=error_code,
                input_fetch_ms=input_fetch_ms,
                callable_execute_ms=callable_execute_ms,
                chat_request_ms=(
                    0 if reporter is None else reporter.chat_request_ms
                ),
                output_put_ms=output_put_ms,
                task_total_ms=max(
                    0, int((time.perf_counter() - task_started_perf) * 1_000)
                ),
                input_handle_count=sum(
                    argument.kind == "data_handle" for argument in request.arguments
                ),
                output_count=output_count,
                inference_metrics=inference_metrics,
            ),
        )

    try:
        if not isinstance(code_package, CodePackage):
            raise TypeError("code registry value is not CodePackage")
        if (
            code_package.definition_id != request.code_handle.definition_id
            or code_package.code_hash != request.code_handle.code_hash
            or code_package.environment_fingerprint != request.environment_fingerprint
        ):
            raise ValueError("CodePackage identity does not match ExecutionRequest")
        func = load_code_package(code_package)
        expected_argument_keys = {
            ray_data_argument_keyword(index)
            for index, argument in enumerate(request.arguments)
            if argument.kind == "data_handle"
        }
        if set(resolved_data_arguments) != expected_argument_keys:
            raise ValueError("resolved Ray arguments do not match ExecutionRequest")
        kwargs: dict[str, object] = {}
        for index, argument in enumerate(request.arguments):
            if argument.kind == "literal":
                kwargs[argument.name] = argument.literal
            elif argument.kind == "data_handle":
                kwargs[argument.name] = resolved_data_arguments[
                    ray_data_argument_keyword(index)
                ]
        input_fetch_ms = max(
            0, int((time.perf_counter() - input_started_perf) * 1_000)
        )
    except Exception as exc:
        input_fetch_ms = max(
            0, int((time.perf_counter() - input_started_perf) * 1_000)
        )
        return complete(
            _failure_event(
                request=request,
                lease=placement_lease,
                worker_lease=worker_lease,
                binding=binding,
                kind=RuntimeEventKind.TASK_FAILED,
                error_code="data_binding_failed",
                category="data",
                phase="binding",
                message=f"{type(exc).__name__}: {exc}",
                exception=exc,
                device_binding=device_binding,
                npu_runtime=npu_runtime,
            ),
            status="failed",
            error_code="data_binding_failed",
        )
    inference_summary: AttemptInferenceSummary | None = None
    session: AttemptInferenceSession | None = None
    callable_started_perf = time.perf_counter()
    try:
        if request.execution_target is ExecutionTarget.MODEL_SERVICE:
            assert request.model_route is not None
            assert inference_config is not None
            reporter = _WorkerRouteReporter(
                request=request,
                placement_lease=placement_lease,
                binding=binding,
                agent_identity=agent_identity,
                controller_generation=controller_generation,
                event_timeout_seconds=event_timeout_seconds,
            )
            inference_client = create_worker_inference_client(inference_config)
            session = AttemptInferenceSession(
                lease=request.model_route,
                router=reporter,
                adapter=inference_client,
                instance_placement_lease_id=(
                    inference_config.instance_placement_lease_id
                ),
                record_sink=reporter.record,
            )
            with install_route_session(session):
                result = func(**kwargs)
            inference_summary = session.summary()
        else:
            result = func(**kwargs)
        callable_execute_ms = max(
            0, int((time.perf_counter() - callable_started_perf) * 1_000)
        )
    except Exception as exc:
        callable_execute_ms = max(
            0, int((time.perf_counter() - callable_started_perf) * 1_000)
        )
        if session is not None:
            inference_summary = session.summary()
        if inference_client is not None:
            try:
                asyncio.run(inference_client.close())
            except Exception:
                pass
        oom_confidence = (
            None
            if npu_runtime is None
            else npu_runtime.oom_classification_confidence(exc)
        )
        if isinstance(exc, InferenceCallError):
            error_code = exc.error_code
            category = "model"
            confidence = "exact"
        elif oom_confidence is not None:
            error_code = "npu_oom"
            category = "resource"
            confidence = oom_confidence
        elif npu_runtime is not None and platform_error_code(exc) is not None:
            error_code = "npu_async_error"
            category = "node_device"
            confidence = "mapped"
        else:
            error_code = "user_code_failed"
            category = "user"
            confidence = "exact"
        return complete(
            _failure_event(
                request=request,
                lease=placement_lease,
                worker_lease=worker_lease,
                binding=binding,
                kind=RuntimeEventKind.TASK_FAILED,
                error_code=error_code,
                category=category,
                phase="user_code",
                message=f"{type(exc).__name__}: {exc}",
                exception=exc,
                classification_confidence=confidence,
                device_binding=device_binding,
                npu_runtime=npu_runtime,
                inference_summary=inference_summary,
            ),
            status="failed",
            error_code=error_code,
        )
    if inference_client is not None:
        try:
            asyncio.run(inference_client.close())
        except Exception as exc:
            return complete(
                _failure_event(
                    request=request,
                    lease=placement_lease,
                    worker_lease=worker_lease,
                    binding=binding,
                    kind=RuntimeEventKind.TASK_FAILED,
                    error_code="model_client_cleanup_failed",
                    category="model",
                    phase="cleanup",
                    message=f"{type(exc).__name__}: {exc}",
                    exception=exc,
                    device_binding=device_binding,
                    npu_runtime=npu_runtime,
                    inference_summary=inference_summary,
                ),
                status="failed",
                error_code="model_client_cleanup_failed",
            )
    if npu_runtime is not None:
        try:
            npu_runtime.synchronize()
        except Exception as exc:
            return complete(
                _failure_event(
                    request=request,
                    lease=placement_lease,
                    worker_lease=worker_lease,
                    binding=binding,
                    kind=RuntimeEventKind.TASK_FAILED,
                    error_code="npu_async_error",
                    category="node_device",
                    phase="npu_synchronize",
                    message=f"{type(exc).__name__}: {exc}",
                    exception=exc,
                    classification_confidence="mapped",
                    device_binding=device_binding,
                    npu_runtime=npu_runtime,
                    inference_summary=inference_summary,
                ),
                status="failed",
                error_code="npu_async_error",
            )
    if not isinstance(result, dict) or tuple(sorted(result)) != tuple(
        sorted(request.expected_outputs)
    ):
        return complete(
            _failure_event(
                request=request,
                lease=placement_lease,
                worker_lease=worker_lease,
                binding=binding,
                kind=RuntimeEventKind.TASK_FAILED,
                error_code="invalid_task_output",
                category="data",
                phase="publishing",
                message="Task returned keys that do not match its output contract",
                device_binding=device_binding,
                npu_runtime=npu_runtime,
                inference_summary=inference_summary,
            ),
            status="failed",
            error_code="invalid_task_output",
        )
    if npu_runtime is not None and contains_npu_tensor(result):
        return complete(
            _failure_event(
                request=request,
                lease=placement_lease,
                worker_lease=worker_lease,
                binding=binding,
                kind=RuntimeEventKind.TASK_FAILED,
                error_code="invalid_task_output",
                category="data",
                phase="publishing",
                message="NPU Tensor outputs must be moved to host memory before return",
                device_binding=device_binding,
                npu_runtime=npu_runtime,
                inference_summary=inference_summary,
            ),
            status="failed",
            error_code="invalid_task_output",
        )
    output_handles: list[tuple[str, DataHandle]] = []
    output_started_perf = time.perf_counter()
    try:
        for output_name in request.expected_outputs:
            output_handles.append(
                (
                    output_name,
                    store.put_staged_for_runtime_node(
                        result[output_name],
                        data_store_descriptor_generation(store),
                        node_id=binding.node_id,
                        boot_id=binding.boot_id,
                        runtime_generation=binding.runtime_generation,
                    ),
                )
            )
        output_put_ms = max(
            0, int((time.perf_counter() - output_started_perf) * 1_000)
        )
        output_count = len(output_handles)
    except Exception as exc:
        output_put_ms = max(
            0, int((time.perf_counter() - output_started_perf) * 1_000)
        )
        output_count = len(output_handles)
        store.release_many(tuple(handle for _, handle in output_handles))
        return complete(
            _failure_event(
                request=request,
                lease=placement_lease,
                worker_lease=worker_lease,
                binding=binding,
                kind=RuntimeEventKind.TASK_FAILED,
                error_code="result_publish_failed",
                category="data",
                phase="publishing",
                message=f"{type(exc).__name__}: {exc}",
                exception=exc,
                device_binding=device_binding,
                npu_runtime=npu_runtime,
                inference_summary=inference_summary,
            ),
            status="failed",
            error_code="result_publish_failed",
        )
    observation = _resource_observation(
        request=request,
        lease=placement_lease,
        status="succeeded",
        error_type=None,
        device_binding=device_binding,
        npu_runtime=npu_runtime,
    )
    return complete(
        RuntimeEvent.create(
            kind=RuntimeEventKind.TASK_RESULT,
            dispatch_id=request.dispatch_id,
            run_id=request.run_id,
            task_id=request.task_id,
            attempt=request.attempt,
            lease_id=placement_lease.lease_id,
            route_lease_id=_route_lease_id(request),
            occurred_at_ms=monotonic_time_ms(),
            output_handles=tuple(output_handles),
            worker_pid=os.getpid(),
            device_id=(
                None if device_binding is None else device_binding.physical_device_id
            ),
            binding_verified=npu_runtime is not None,
            resource_observation=observation,
            inference_summary=inference_summary,
        ),
        status="succeeded",
        error_code=None,
    )


def data_store_descriptor_generation(store: RayDataStore) -> str:
    return store.descriptor.owner_generation


def _failure_event(
    *,
    request: ExecutionRequest,
    lease: PlacementLease,
    worker_lease: WorkerLease,
    binding: RuntimeNodeBinding,
    kind: RuntimeEventKind,
    error_code: str,
    category: str,
    phase: str,
    message: str,
    exception: BaseException | None = None,
    classification_confidence: str = "exact",
    device_binding: DeviceBinding | None = None,
    npu_runtime: BoundTorchNpuRuntime | None = None,
    inference_summary: AttemptInferenceSummary | None = None,
) -> RuntimeEvent:
    observation = _resource_observation(
        request=request,
        lease=lease,
        status="failed",
        error_type=None if exception is None else type(exception).__name__,
        device_binding=device_binding,
        npu_runtime=npu_runtime,
    )
    error = ErrorInfo(
        schema_version=1,
        error_code=error_code,
        category=category,
        origin="worker" if kind is RuntimeEventKind.TASK_FAILED else "runtime",
        message=message,
        retryable_hint=error_code
        in {"runtime_node_unavailable", "worker_start_failed"},
        classification_confidence=classification_confidence,
        execution_phase=phase,
        run_id=request.run_id,
        task_id=request.task_id,
        attempt=request.attempt,
        dispatch_id=request.dispatch_id,
        lease_id=lease.lease_id,
        route_lease_id=_route_lease_id(request),
        node_id=binding.node_id,
        boot_id=binding.boot_id,
        worker_id=worker_lease.worker_id,
        model_instance_id=(
            None if request.model_route is None else request.model_route.instance_id
        ),
        device_id=(
            None if device_binding is None else device_binding.physical_device_id
        ),
        exception_type=None if exception is None else type(exception).__name__,
        platform_error_code=(
            None if exception is None else platform_error_code(exception)
        ),
        occurred_at_ms=monotonic_time_ms(),
        details=FrozenMap(
            (
                ("binding_verified", npu_runtime is not None),
                ("worker_pid", os.getpid()),
            )
        ),
    )
    return RuntimeEvent.create(
        kind=kind,
        dispatch_id=request.dispatch_id,
        run_id=request.run_id,
        task_id=request.task_id,
        attempt=request.attempt,
        lease_id=lease.lease_id,
        route_lease_id=_route_lease_id(request),
        occurred_at_ms=error.occurred_at_ms,
        error=error,
        worker_pid=os.getpid(),
        device_id=(
            None if device_binding is None else device_binding.physical_device_id
        ),
        binding_verified=npu_runtime is not None,
        resource_observation=observation,
        inference_summary=inference_summary,
    )


def _route_lease_id(request: ExecutionRequest) -> str | None:
    return None if request.model_route is None else request.model_route.route_lease_id


def _resource_observation(
    *,
    request: ExecutionRequest,
    lease: PlacementLease,
    status: str,
    error_type: str | None,
    device_binding: DeviceBinding | None,
    npu_runtime: BoundTorchNpuRuntime | None,
) -> ResourceObservation:
    peak_allocated: int | None = None
    peak_reserved: int | None = None
    process_hbm: int | None = None
    metric_quality: str | None = None
    if npu_runtime is not None:
        try:
            peak_allocated = npu_runtime.peak_allocated_mb()
            peak_reserved = npu_runtime.peak_reserved_mb()
        except Exception:
            pass
        try:
            sampled = npu_runtime.process_hbm_mb()
            process_hbm = max(
                npu_runtime.initial_process_hbm_mb,
                sampled if sampled is not None else 0,
            )
            metric_quality = "sampled"
        except Exception:
            process_hbm = None
    return ResourceObservation(
        run_id=request.run_id,
        task_id=request.task_id,
        definition_id=request.code_handle.definition_id,
        attempt=request.attempt,
        code_hash=request.code_handle.code_hash,
        environment_fingerprint=request.environment_fingerprint,
        requested=ResourceSpec(
            cpu_num=lease.resources.cpu_num,
            mem_mb=lease.resources.host_mem_mb,
            npu_mem_mb=lease.resources.npu_hbm_mb,
            io_num=lease.resources.io_slots,
        ),
        status=status,
        peak_host_rss_mb=host_peak_rss_mb(),
        peak_npu_allocated_mb=peak_allocated,
        peak_npu_reserved_mb=peak_reserved,
        peak_npu_process_hbm_mb=process_hbm,
        npu_metric_source=(
            "torch_npu_allocator+dcmi_process" if npu_runtime is not None else None
        ),
        npu_metric_quality=metric_quality,
        error_type=error_type,
        device_id=(
            None if device_binding is None else device_binding.physical_device_id
        ),
        worker_pid=os.getpid(),
        binding_verified=npu_runtime is not None,
    )


def _try_report(
    endpoint: str,
    identity: NodeAgentIdentity,
    controller_generation: str,
    runtime_generation: int,
    event: RuntimeEvent,
    timeout_seconds: float,
) -> bool:
    try:
        report_worker_event(
            endpoint=endpoint,
            identity=identity,
            controller_generation=controller_generation,
            runtime_generation=runtime_generation,
            event=event,
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        return False
    return True


_RAY_ONE_SHOT_MAX_CALLS = 1
_RAY_ONE_SHOT_MAX_RETRIES = 0
_RAY_ONE_SHOT_NUM_CPUS = 0
RAY_ONE_SHOT_FAULT_OPTIONS = FrozenMap(
    (
        ("max_calls", _RAY_ONE_SHOT_MAX_CALLS),
        ("max_retries", _RAY_ONE_SHOT_MAX_RETRIES),
        ("num_cpus", _RAY_ONE_SHOT_NUM_CPUS),
    )
)
_RAY_REMOTE: Any = ray.remote(
    max_calls=_RAY_ONE_SHOT_MAX_CALLS,
    max_retries=_RAY_ONE_SHOT_MAX_RETRIES,
    num_cpus=_RAY_ONE_SHOT_NUM_CPUS,
)
RAY_ONE_SHOT_WORKER: Any = _RAY_REMOTE(_execute_one_shot)

_RAY_STANDBY_MAX_RESTARTS = 0
_RAY_STANDBY_MAX_TASK_RETRIES = 0
_RAY_STANDBY_NUM_CPUS = 0
RAY_STANDBY_FAULT_OPTIONS = FrozenMap(
    (
        ("max_restarts", _RAY_STANDBY_MAX_RESTARTS),
        ("max_task_retries", _RAY_STANDBY_MAX_TASK_RETRIES),
        ("num_cpus", _RAY_STANDBY_NUM_CPUS),
    )
)
_RAY_STANDBY_REMOTE: Any = ray.remote(
    max_restarts=_RAY_STANDBY_MAX_RESTARTS,
    max_task_retries=_RAY_STANDBY_MAX_TASK_RETRIES,
    num_cpus=_RAY_STANDBY_NUM_CPUS,
)
RAY_STANDBY_WORKER: Any = _RAY_STANDBY_REMOTE(_RayStandbyWorker)
