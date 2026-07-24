"""Node-local long-lived service process and port authority."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
import inspect
import os
from pathlib import Path
import signal
import socket
from typing import IO, Any, Protocol

import grpc

from ascend_maze.contracts.resources import PlacementLease, ReservationVector
from ascend_maze.contracts.runtime import (
    RuntimeDeviceMapping,
    RuntimeNodeBinding,
)
from ascend_maze.core.canonical import FrozenMap
from ascend_maze.core.identifiers import new_id
from ascend_maze.inference.contracts import (
    PortLease,
    ServiceHandle,
    ServiceLaunchRequest,
    ServiceProcessExit,
    ServiceProcessProbe,
    ServiceStopResult,
)
from ascend_maze.runtime.ray_node_registry import RayNodeRegistry, RuntimeNodeStatus

from ascend_maze.control.proto import control_pb2 as _control_pb2
from ascend_maze.control.proto import control_pb2_grpc

control_pb2: Any = _control_pb2


class ServiceDeviceSnapshot(Protocol):
    used_hbm_mb: int


class ServiceDeviceMonitor(Protocol):
    def devices(self) -> tuple[ServiceDeviceSnapshot, ...]: ...

    def device(self, physical_device_id: str) -> ServiceDeviceSnapshot: ...

    def process_hbm_mb(self, physical_device_id: str, pid: int) -> int | None: ...

    def verify_process_device(
        self,
        pid: int,
        physical_device_id: str,
        *,
        deadline_seconds: float = 2.0,
        poll_interval_seconds: float = 0.05,
    ) -> bool: ...


@dataclass(slots=True)
class _ServiceRecord:
    request: ServiceLaunchRequest
    lease: PlacementLease
    handle: ServiceHandle
    process: asyncio.subprocess.Process
    log_file: IO[bytes]
    baseline_hbm_mb: int
    monitor_task: asyncio.Task[None] | None = None
    stopping: bool = False


class NodeServiceProcessManager:
    """The only node-local authority allowed to spawn model services."""

    def __init__(
        self,
        *,
        node_id: str,
        boot_id: str,
        device_monitor: ServiceDeviceMonitor,
        allowed_executables: Sequence[str],
        log_directory: str | Path,
        first_port: int = 25_000,
        last_port: int = 65_535,
        port_bind_host: str = "127.0.0.1",
        hbm_recovery_tolerance_mb: int = 64,
        poll_interval_ms: int = 100,
        device_mappings: tuple[RuntimeDeviceMapping, ...] = (),
        on_unexpected_exit: (
            Callable[[ServiceProcessExit], Awaitable[None] | None] | None
        ) = None,
    ) -> None:
        if not node_id or not boot_id or not port_bind_host:
            raise ValueError("node, boot and port bind host are required")
        if (
            isinstance(first_port, bool)
            or isinstance(last_port, bool)
            or not isinstance(first_port, int)
            or not isinstance(last_port, int)
            or first_port < 1
            or last_port > 65_535
            or first_port > last_port
        ):
            raise ValueError("service port range must be within 1..65535")
        for name, value in (
            ("hbm_recovery_tolerance_mb", hbm_recovery_tolerance_mb),
            ("poll_interval_ms", poll_interval_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be non-negative")
        if poll_interval_ms == 0:
            raise ValueError("poll_interval_ms must be positive")
        executables = tuple(
            str(Path(item).expanduser().resolve(strict=False))
            for item in allowed_executables
        )
        if not executables or any(not Path(item).is_file() for item in executables):
            raise ValueError("allowed service executables must be existing files")
        logs = Path(log_directory).expanduser().resolve(strict=False)
        logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(logs, 0o700)
        self.node_id = node_id
        self.boot_id = boot_id
        self.device_monitor = device_monitor
        self.allowed_executables = frozenset(executables)
        self.log_directory = logs
        self.first_port = first_port
        self.last_port = last_port
        self.port_bind_host = port_bind_host
        self.hbm_recovery_tolerance_mb = hbm_recovery_tolerance_mb
        self.poll_interval_ms = poll_interval_ms
        self.on_unexpected_exit = on_unexpected_exit
        self._device_mappings = {
            item.physical_device_id: item for item in device_mappings
        }
        if len(self._device_mappings) != len(device_mappings):
            raise ValueError("service physical device mappings must be unique")
        self._next_port = first_port
        self._ports: dict[tuple[str, str, int], PortLease] = {}
        self._ports_by_owner: dict[tuple[str, int], PortLease] = {}
        self._services: dict[str, _ServiceRecord] = {}
        self._stopped: dict[str, ServiceStopResult] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def set_unexpected_exit_sink(
        self,
        sink: Callable[[ServiceProcessExit], Awaitable[None] | None] | None,
    ) -> None:
        self.on_unexpected_exit = sink

    async def active_handles(self) -> tuple[ServiceHandle, ...]:
        async with self._lock:
            return tuple(
                record.handle
                for _, record in sorted(self._services.items())
                if record.process.returncode is None
            )

    async def acquire_port(
        self,
        *,
        node_id: str,
        boot_id: str,
        owner_instance_id: str,
        generation: int,
    ) -> PortLease:
        self._validate_node(node_id, boot_id)
        if not owner_instance_id or generation < 1:
            raise ValueError("service port owner identity is invalid")
        owner = (owner_instance_id, generation)
        async with self._lock:
            self._require_open()
            existing = self._ports_by_owner.get(owner)
            if existing is not None:
                return existing
            capacity = self.last_port - self.first_port + 1
            for offset in range(capacity):
                port = self.first_port + (
                    (self._next_port - self.first_port + offset) % capacity
                )
                key = (node_id, boot_id, port)
                if key in self._ports or not self._port_available(port):
                    continue
                lease = PortLease(
                    port_lease_id=new_id("port"),
                    node_id=node_id,
                    boot_id=boot_id,
                    port=port,
                    owner_instance_id=owner_instance_id,
                    generation=generation,
                )
                self._ports[key] = lease
                self._ports_by_owner[owner] = lease
                self._next_port = self.first_port if port == self.last_port else port + 1
                return lease
        raise RuntimeError("no node-local model service port is available")

    async def release_port(self, lease: PortLease) -> bool:
        self._validate_node(lease.node_id, lease.boot_id)
        key = (lease.node_id, lease.boot_id, lease.port)
        owner = (lease.owner_instance_id, lease.generation)
        async with self._lock:
            current = self._ports.get(key)
            if current is None:
                return False
            if current != lease or self._ports_by_owner.get(owner) != lease:
                raise RuntimeError("stale node-local PortLease release")
            if any(
                record.request.port_lease_id == lease.port_lease_id
                for record in self._services.values()
            ):
                raise RuntimeError("cannot release a port owned by a service process")
            del self._ports[key]
            del self._ports_by_owner[owner]
            return True

    async def launch(
        self,
        request: ServiceLaunchRequest,
        lease: PlacementLease,
    ) -> ServiceHandle:
        self._validate_launch(request, lease)
        executable = str(Path(request.argv[0]).resolve(strict=False))
        if executable not in self.allowed_executables:
            raise RuntimeError("service executable is not allowed by NodeAgent")
        working_directory = request.working_directory
        if working_directory is not None and not Path(working_directory).is_dir():
            raise RuntimeError("service working directory does not exist")
        async with self._lock:
            self._require_open()
            port = self._ports_by_owner.get((request.instance_id, request.generation))
            if (
                port is None
                or port.port_lease_id != request.port_lease_id
                or port.port != request.port
            ):
                raise RuntimeError("service launch has no matching NodeAgent PortLease")
            existing = next(
                (
                    item
                    for item in self._services.values()
                    if item.request.instance_id == request.instance_id
                    and item.request.generation == request.generation
                ),
                None,
            )
            if existing is not None:
                if existing.request != request or existing.lease != lease:
                    raise RuntimeError("service launch identity conflicts with an existing process")
                return existing.handle
            if not self._port_available(request.port):
                raise RuntimeError("leased service port is already occupied")
            baseline_hbm_mb = int(
                self.device_monitor.device(lease.npu_device_id or "").used_hbm_mb
            )
            log_path = self.log_directory / (
                f"{request.instance_id}.{request.generation}.log"
            )
            log_file = log_path.open("ab", buffering=0)
            os.chmod(log_path, 0o600)
            environment = os.environ.copy()
            environment.update(request.environment)
            try:
                process = await asyncio.create_subprocess_exec(
                    executable,
                    *request.argv[1:],
                    cwd=working_directory,
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            except Exception:
                log_file.close()
                raise
            handle = ServiceHandle(
                service_handle_id=new_id("service"),
                instance_id=request.instance_id,
                generation=request.generation,
                endpoint_id=request.endpoint_id,
                node_id=lease.node_id,
                boot_id=lease.boot_id,
                npu_device_id=lease.npu_device_id or "",
                process_id=process.pid,
                port_lease_id=request.port_lease_id,
                port=request.port,
            )
            record = _ServiceRecord(
                request=request,
                lease=lease,
                handle=handle,
                process=process,
                log_file=log_file,
                baseline_hbm_mb=baseline_hbm_mb,
            )
            self._services[handle.service_handle_id] = record
            record.monitor_task = asyncio.create_task(self._monitor(record))
            return handle

    async def probe_process(
        self,
        handle: ServiceHandle,
        *,
        timeout_ms: int,
    ) -> ServiceProcessProbe:
        if timeout_ms <= 0:
            raise ValueError("service probe timeout must be positive")
        async with self._lock:
            record = self._services.get(handle.service_handle_id)
            if record is None:
                stopped = self._stopped.get(handle.service_handle_id)
                return ServiceProcessProbe(
                    process_alive=False,
                    port_open=self._port_open(handle.port),
                    binding_verified=False,
                    physical_device_id=handle.npu_device_id,
                    process_hbm_mb=None,
                    exit_code=None if stopped is None else stopped.exit_code,
                )
            self._validate_handle(record.handle, handle)
            process = record.process
        alive = process.returncode is None
        hbm = self._service_process_hbm(handle)
        binding = False
        if alive and hbm is not None:
            binding = self._verify_service_group(handle, timeout_ms)
        return ServiceProcessProbe(
            process_alive=alive,
            port_open=self._port_open(handle.port),
            binding_verified=binding,
            physical_device_id=handle.npu_device_id,
            process_hbm_mb=hbm,
            exit_code=process.returncode,
        )

    async def stop(
        self,
        handle: ServiceHandle,
        *,
        timeout_ms: int,
    ) -> ServiceStopResult:
        if timeout_ms <= 0:
            raise ValueError("service stop timeout must be positive")
        async with self._lock:
            prior = self._stopped.get(handle.service_handle_id)
            if prior is not None:
                return prior
            record = self._services.get(handle.service_handle_id)
            if record is None:
                raise KeyError("unknown service handle")
            self._validate_handle(record.handle, handle)
            record.stopping = True
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1_000
        force_deadline = asyncio.get_running_loop().time() + timeout_ms / 2_000
        forced = False
        process = record.process
        self._signal_group(process.pid, signal.SIGTERM)
        if process.returncode is None:
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=self._remaining(force_deadline)
                )
            except asyncio.TimeoutError:
                forced = True
                self._signal_group(process.pid, signal.SIGKILL)
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=self._remaining(deadline)
                    )
                except asyncio.TimeoutError:
                    pass
        final_hbm = int(
            self.device_monitor.device(handle.npu_device_id).used_hbm_mb
        )
        port_released = not self._port_open(handle.port)
        hbm_recovered = False
        while self._remaining(deadline) > 0:
            process_hbm = self._service_process_hbm(handle)
            final_hbm = int(
                self.device_monitor.device(handle.npu_device_id).used_hbm_mb
            )
            port_released = not self._port_open(handle.port)
            hbm_recovered = (
                process_hbm is None
                and final_hbm
                <= record.baseline_hbm_mb + self.hbm_recovery_tolerance_mb
            )
            if process.returncode is not None and port_released and hbm_recovered:
                break
            if not forced and asyncio.get_running_loop().time() >= force_deadline:
                forced = True
                self._signal_group(process.pid, signal.SIGKILL)
            await asyncio.sleep(self.poll_interval_ms / 1_000)
        result = ServiceStopResult(
            process_exited=process.returncode is not None,
            port_released=port_released,
            hbm_recovered=hbm_recovered,
            exit_code=process.returncode,
            forced_termination=forced,
            final_hbm_mb=final_hbm,
        )
        if result.process_exited and result.port_released and result.hbm_recovered:
            async with self._lock:
                self._services.pop(handle.service_handle_id, None)
                self._stopped[handle.service_handle_id] = result
            record.log_file.close()
        return result

    async def close(self, timeout_ms: int = 30_000) -> None:
        async with self._lock:
            if self._closed:
                return
            handles = tuple(record.handle for record in self._services.values())
        for handle in handles:
            try:
                await self.stop(handle, timeout_ms=timeout_ms)
            except Exception:
                continue
        async with self._lock:
            remaining = tuple(self._services.values())
            self._closed = True
            self._ports.clear()
            self._ports_by_owner.clear()
        for record in remaining:
            if record.process.returncode is None:
                self._signal_group(record.process.pid, signal.SIGKILL)
                await record.process.wait()
            record.log_file.close()

    async def _monitor(self, record: _ServiceRecord) -> None:
        exit_code = await record.process.wait()
        record.log_file.close()
        if record.stopping:
            return
        event = ServiceProcessExit(
            service_handle_id=record.handle.service_handle_id,
            instance_id=record.handle.instance_id,
            generation=record.handle.generation,
            process_id=record.handle.process_id,
            exit_code=exit_code,
        )
        sink = self.on_unexpected_exit
        if sink is not None:
            result = sink(event)
            if inspect.isawaitable(result):
                await result

    def _validate_launch(
        self, request: ServiceLaunchRequest, lease: PlacementLease
    ) -> None:
        self._validate_node(lease.node_id, lease.boot_id)
        if lease.reservation_kind != "model_instance":
            raise RuntimeError("service process requires a model_instance Lease")
        if lease.model_instance_id != request.instance_id:
            raise RuntimeError("service request and PlacementLease instance differ")
        if lease.npu_device_id is None:
            raise RuntimeError("service PlacementLease has no physical NPU")
        expected_runtime_device = self._runtime_device_mapping(
            lease.npu_device_id
        ).runtime_visible_device_id
        if (
            request.environment.get("ASCEND_RT_VISIBLE_DEVICES")
            != expected_runtime_device
        ):
            raise RuntimeError("service device visibility differs from PlacementLease")

    def _runtime_device_mapping(
        self, physical_device_id: str
    ) -> RuntimeDeviceMapping:
        if not self._device_mappings:
            return RuntimeDeviceMapping.identity(physical_device_id)
        try:
            return self._device_mappings[physical_device_id]
        except KeyError as exc:
            raise RuntimeError(
                "service PlacementLease device is absent from node topology"
            ) from exc

    def _validate_node(self, node_id: str, boot_id: str) -> None:
        if (node_id, boot_id) != (self.node_id, self.boot_id):
            raise RuntimeError("service operation targets a stale node generation")

    @staticmethod
    def _validate_handle(expected: ServiceHandle, received: ServiceHandle) -> None:
        if expected != received:
            raise RuntimeError("service handle identity is stale")

    def _port_available(self, port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((self.port_bind_host, port))
        except OSError:
            return False
        return True

    def _port_open(self, port: int) -> bool:
        try:
            with socket.create_connection(
                (self.port_bind_host, port), timeout=0.1
            ):
                return True
        except OSError:
            return False

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("NodeAgent service process manager is closed")

    def _service_process_hbm(self, handle: ServiceHandle) -> int | None:
        snapshot = self.device_monitor.device(handle.npu_device_id)
        members = [
            process
            for process in getattr(snapshot, "processes", ())
            if self._same_process_group(int(process.pid), handle.process_id)
        ]
        if members:
            return sum(int(process.hbm_mb) for process in members)
        return self.device_monitor.process_hbm_mb(
            handle.npu_device_id, handle.process_id
        )

    def _verify_service_group(self, handle: ServiceHandle, timeout_ms: int) -> bool:
        snapshots = getattr(self.device_monitor, "devices", None)
        if callable(snapshots):
            matching_devices = {
                str(getattr(device, "physical_device_id"))
                for device in snapshots()
                for process in getattr(device, "processes", ())
                if self._same_process_group(int(process.pid), handle.process_id)
            }
            if matching_devices:
                return matching_devices == {handle.npu_device_id}
        return self.device_monitor.verify_process_device(
            handle.process_id,
            handle.npu_device_id,
            deadline_seconds=min(timeout_ms / 1_000, 0.5),
            poll_interval_seconds=min(self.poll_interval_ms / 1_000, 0.05),
        )

    @staticmethod
    def _same_process_group(candidate_pid: int, leader_pid: int) -> bool:
        try:
            return os.getpgid(candidate_pid) == leader_pid
        except (ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.001, deadline - asyncio.get_running_loop().time())

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass


def encode_port_lease(lease: PortLease) -> Any:
    return control_pb2.PortLeaseMessage(
        port_lease_id=lease.port_lease_id,
        node_id=lease.node_id,
        boot_id=lease.boot_id,
        port=lease.port,
        owner_instance_id=lease.owner_instance_id,
        generation=lease.generation,
    )


def decode_port_lease(message: Any) -> PortLease:
    return PortLease(
        port_lease_id=str(message.port_lease_id),
        node_id=str(message.node_id),
        boot_id=str(message.boot_id),
        port=int(message.port),
        owner_instance_id=str(message.owner_instance_id),
        generation=int(message.generation),
    )


def encode_service_handle(handle: ServiceHandle) -> Any:
    return control_pb2.ServiceHandleMessage(
        service_handle_id=handle.service_handle_id,
        instance_id=handle.instance_id,
        generation=handle.generation,
        endpoint_id=handle.endpoint_id,
        node_id=handle.node_id,
        boot_id=handle.boot_id,
        npu_device_id=handle.npu_device_id,
        process_id=handle.process_id,
        port_lease_id=handle.port_lease_id,
        port=handle.port,
    )


def decode_service_handle(message: Any) -> ServiceHandle:
    return ServiceHandle(
        service_handle_id=str(message.service_handle_id),
        instance_id=str(message.instance_id),
        generation=int(message.generation),
        endpoint_id=str(message.endpoint_id),
        node_id=str(message.node_id),
        boot_id=str(message.boot_id),
        npu_device_id=str(message.npu_device_id),
        process_id=int(message.process_id),
        port_lease_id=str(message.port_lease_id),
        port=int(message.port),
    )


def decode_service_launch(message: Any) -> ServiceLaunchRequest:
    return ServiceLaunchRequest(
        instance_id=str(message.instance_id),
        generation=int(message.generation),
        model_id=str(message.model_id),
        artifact_revision=str(message.artifact_revision),
        endpoint_id=str(message.endpoint_id),
        port_lease_id=str(message.port_lease_id),
        port=int(message.port),
        argv=tuple(str(item) for item in message.argv),
        working_directory=(
            str(message.working_directory) if message.has_working_directory else None
        ),
        environment=FrozenMap(
            tuple(sorted((str(item.name), str(item.value)) for item in message.environment))
        ),
    )


def decode_model_placement(message: Any) -> PlacementLease:
    return PlacementLease(
        lease_id=str(message.lease_id),
        reservation_kind="model_instance",
        run_id=None,
        task_id=None,
        attempt=None,
        node_id=str(message.node_id),
        boot_id=str(message.boot_id),
        npu_device_id=str(message.npu_device_id),
        resources=ReservationVector(
            cpu_num=0,
            host_mem_mb=0,
            io_slots=0,
            npu_hbm_mb=int(message.npu_hbm_mb),
            npu_slots=int(message.npu_slots),
        ),
        snapshot_version=0,
        created_at_ms=int(message.created_at_ms),
        dispatch_deadline_ms=int(message.dispatch_deadline_ms),
        model_instance_id=str(message.model_instance_id),
    )


class NodeAgentServiceProcessBackend:
    """Controller-side C9 client for one or more registered NodeAgents."""

    def __init__(
        self,
        *,
        cluster_id: str,
        authorization_token: bytes,
        controller_generation: str,
        node_registry: RayNodeRegistry,
        rpc_timeout_ms: int = 30_000,
    ) -> None:
        if not cluster_id or not authorization_token or not controller_generation:
            raise ValueError("service RPC controller identity is required")
        if rpc_timeout_ms <= 0:
            raise ValueError("service RPC timeout must be positive")
        self.cluster_id = cluster_id
        self.authorization_token = authorization_token
        self.controller_generation = controller_generation
        self.node_registry = node_registry
        self.rpc_timeout_ms = rpc_timeout_ms
        self._channels: dict[str, grpc.aio.Channel] = {}
        self._stubs: dict[str, object] = {}
        self._port_leases: dict[str, PortLease] = {}
        self._released_port_leases: dict[str, PortLease] = {}

    async def acquire(
        self,
        *,
        node_id: str,
        boot_id: str,
        owner_instance_id: str,
        generation: int,
    ) -> PortLease:
        binding = self._binding(node_id, boot_id)
        response = await self._stub(binding.agent_endpoint).AcquirePort(
            control_pb2.AcquireServicePortRequest(
                meta=self._meta(binding),
                owner_instance_id=owner_instance_id,
                generation=generation,
            ),
            timeout=self.rpc_timeout_ms / 1_000,
        )
        self._require_accepted(response, "service_port_acquire_failed")
        if not response.has_lease:
            raise RuntimeError("NodeAgent accepted port acquisition without a lease")
        lease = decode_port_lease(response.lease)
        self._port_leases[lease.port_lease_id] = lease
        self._released_port_leases.pop(lease.port_lease_id, None)
        return lease

    async def release(self, lease: PortLease) -> bool:
        tracked = self._port_leases.get(lease.port_lease_id)
        released = self._released_port_leases.get(lease.port_lease_id)
        if tracked is not None and tracked != lease:
            raise RuntimeError("controller PortLease release identity is stale")
        if released is not None:
            if released != lease:
                raise RuntimeError("controller PortLease release identity is stale")
            return True
        binding = self._binding(lease.node_id, lease.boot_id)
        response = await self._stub(binding.agent_endpoint).ReleasePort(
            control_pb2.ReleaseServicePortRequest(
                meta=self._meta(binding),
                lease=encode_port_lease(lease),
            ),
            timeout=self.rpc_timeout_ms / 1_000,
        )
        if response.accepted:
            self._port_leases.pop(lease.port_lease_id, None)
            self._released_port_leases[lease.port_lease_id] = lease
            return True
        if response.error_code == "port_lease_not_found":
            if tracked != lease:
                return False
            self._port_leases.pop(lease.port_lease_id, None)
            self._released_port_leases[lease.port_lease_id] = lease
            return True
        self._require_accepted(response, "service_port_release_failed")
        return True

    def active_count(self) -> int:
        return len(self._port_leases)

    async def launch(
        self,
        request: ServiceLaunchRequest,
        lease: PlacementLease,
    ) -> ServiceHandle:
        binding = self.node_registry.resolve_lease(lease)
        if lease.npu_device_id is None:
            raise RuntimeError("service PlacementLease has no physical NPU")
        mapping = binding.device_mapping(lease.npu_device_id)
        environment = dict(request.environment.items_tuple())
        environment["ASCEND_RT_VISIBLE_DEVICES"] = (
            mapping.runtime_visible_device_id
        )
        request = replace(
            request,
            environment=FrozenMap(tuple(sorted(environment.items()))),
        )
        launch = control_pb2.ServiceLaunchSpecMessage(
            instance_id=request.instance_id,
            generation=request.generation,
            model_id=request.model_id,
            artifact_revision=request.artifact_revision,
            endpoint_id=request.endpoint_id,
            port_lease_id=request.port_lease_id,
            port=request.port,
            argv=request.argv,
            working_directory=request.working_directory or "",
            has_working_directory=request.working_directory is not None,
        )
        for key, value in request.environment.items_tuple():
            launch.environment.add(name=key, value=value)
        response = await self._stub(binding.agent_endpoint).Launch(
            control_pb2.LaunchServiceRequestMessage(
                meta=self._meta(binding),
                request=launch,
                lease=control_pb2.ModelPlacementLeaseMessage(
                    lease_id=lease.lease_id,
                    node_id=lease.node_id,
                    boot_id=lease.boot_id,
                    npu_device_id=lease.npu_device_id or "",
                    npu_hbm_mb=lease.resources.npu_hbm_mb,
                    npu_slots=lease.resources.npu_slots,
                    created_at_ms=lease.created_at_ms,
                    dispatch_deadline_ms=lease.dispatch_deadline_ms,
                    model_instance_id=lease.model_instance_id or "",
                ),
            ),
            timeout=self.rpc_timeout_ms / 1_000,
        )
        self._require_accepted(response, "service_launch_failed")
        if not response.has_handle:
            raise RuntimeError("NodeAgent accepted service launch without a handle")
        return decode_service_handle(response.handle)

    async def probe_process(
        self,
        handle: ServiceHandle,
        *,
        timeout_ms: int,
    ) -> ServiceProcessProbe:
        binding = self._binding(handle.node_id, handle.boot_id)
        response = await self._stub(binding.agent_endpoint).Probe(
            control_pb2.ProbeServiceRequest(
                meta=self._meta(binding),
                handle=encode_service_handle(handle),
                timeout_ms=timeout_ms,
            ),
            timeout=min(timeout_ms, self.rpc_timeout_ms) / 1_000,
        )
        self._require_accepted(response, "service_probe_failed")
        return ServiceProcessProbe(
            process_alive=bool(response.process_alive),
            port_open=bool(response.port_open),
            binding_verified=bool(response.binding_verified),
            physical_device_id=str(response.physical_device_id),
            process_hbm_mb=(
                int(response.process_hbm_mb) if response.has_process_hbm_mb else None
            ),
            exit_code=int(response.exit_code) if response.has_exit_code else None,
        )

    async def stop(
        self,
        handle: ServiceHandle,
        *,
        timeout_ms: int,
    ) -> ServiceStopResult:
        binding = self._binding(handle.node_id, handle.boot_id)
        response = await self._stub(binding.agent_endpoint).Stop(
            control_pb2.StopServiceRequest(
                meta=self._meta(binding),
                handle=encode_service_handle(handle),
                timeout_ms=timeout_ms,
            ),
            timeout=max(timeout_ms, self.rpc_timeout_ms) / 1_000,
        )
        self._require_accepted(response, "service_stop_failed")
        return ServiceStopResult(
            process_exited=bool(response.process_exited),
            port_released=bool(response.port_released),
            hbm_recovered=bool(response.hbm_recovered),
            exit_code=int(response.exit_code) if response.has_exit_code else None,
            forced_termination=bool(response.forced_termination),
            final_hbm_mb=(
                int(response.final_hbm_mb) if response.has_final_hbm_mb else None
            ),
        )

    async def close(self) -> None:
        channels = tuple(self._channels.values())
        self._channels.clear()
        self._stubs.clear()
        self._port_leases.clear()
        self._released_port_leases.clear()
        for channel in channels:
            await channel.close()

    def endpoint_host(self, lease: PlacementLease) -> str:
        binding = self.node_registry.resolve_lease(lease)
        host = binding.agent_endpoint.rsplit(":", 1)[0]
        return host.strip("[]")

    def _binding(self, node_id: str, boot_id: str) -> RuntimeNodeBinding:
        binding = self.node_registry.binding(node_id)
        if binding.boot_id != boot_id:
            raise RuntimeError("NodeAgent service target boot generation is stale")
        if self.node_registry.status(node_id) is not RuntimeNodeStatus.HEALTHY:
            raise RuntimeError("NodeAgent service target is not healthy")
        return binding

    def _meta(self, binding: RuntimeNodeBinding) -> Any:
        return control_pb2.ServiceControlMeta(
            schema_version=1,
            cluster_id=self.cluster_id,
            node_id=binding.node_id,
            boot_id=binding.boot_id,
            agent_generation=binding.agent_generation,
            controller_generation=self.controller_generation,
            runtime_generation=binding.runtime_generation,
            authorization_token=self.authorization_token,
        )

    def _stub(self, endpoint: str) -> Any:
        stub = self._stubs.get(endpoint)
        if stub is not None:
            return stub
        channel = grpc.aio.insecure_channel(endpoint)
        stub = control_pb2_grpc.ServiceProcessControlStub(channel)
        self._channels[endpoint] = channel
        self._stubs[endpoint] = stub
        return stub

    @staticmethod
    def _require_accepted(response: Any, fallback_code: str) -> None:
        if response.accepted:
            return
        code = str(response.error_code) or fallback_code
        raise RuntimeError(f"{code}: {response.message}")
