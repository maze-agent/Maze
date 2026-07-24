"""C13 foreground worker-node bootstrap and owned Ray worker lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hmac
import os
from pathlib import Path
import signal
import stat
import sys
from typing import cast

import grpc

from ascend_maze.ascend import (
    AscendColocationConfig,
    AscendCorrectnessConfig,
    DcmiDeviceAdapter,
    build_ascend_node_capacity,
    build_ascend_node_observation,
    discover_ascend_environment,
)
from ascend_maze.config import NodeBootstrapConfig
from ascend_maze.contracts.recording import ExecutionRecorder, ParquetRecorderConfig
from ascend_maze.contracts.runtime import RuntimeDeviceMapping
from ascend_maze.control.contracts import NodeRuntimePolicy
from ascend_maze.control.node_rpc import NodeAgent, NodeAgentIdentity
from ascend_maze.control.process_lock import NodeProcessLock
from ascend_maze.control.proto import control_pb2 as _control_pb2
from ascend_maze.control.proto import control_pb2_grpc
from ascend_maze.control.service_process import (
    NodeServiceProcessManager,
    ServiceDeviceMonitor,
)
from ascend_maze.core.errors import (
    ContractValidationError,
    EnvironmentValidationError,
)
from ascend_maze.core.identifiers import new_id
from ascend_maze.placement import NodeCapacity
from ascend_maze.recording import NoopRecorder, ParquetRecorder
from ascend_maze.runtime.ray_cluster import ManagedRayWorkerNode

control_pb2 = _control_pb2


@dataclass(frozen=True, slots=True)
class NodeBootstrapResponse:
    cluster_id: str
    controller_generation: str
    config_fingerprint: str
    environment_fingerprint: str
    ray_address: str
    ray_namespace: str
    node_runtime_policy: NodeRuntimePolicy


class NodeApplication:
    def __init__(
        self,
        config: NodeBootstrapConfig,
        *,
        device_adapter: DcmiDeviceAdapter | None = None,
    ) -> None:
        self.config = config
        self.device_adapter = device_adapter or DcmiDeviceAdapter()
        self.worker: ManagedRayWorkerNode | None = None
        self.agent: NodeAgent | None = None

    async def run(self) -> int:
        config = self.config
        agent_generation = new_id("node_agent")
        process_lock = NodeProcessLock(
            Path(config.runtime_directory) / "node.pid",
            node_generation=agent_generation,
        )
        process_lock.acquire()
        worker: ManagedRayWorkerNode | None = None
        agent: NodeAgent | None = None
        recorder: ExecutionRecorder | None = None
        service_manager: NodeServiceProcessManager | None = None
        installed_signals: list[signal.Signals] = []
        try:
            devices = self.device_adapter.devices()
            if not devices:
                raise EnvironmentValidationError("Doctor: no Ascend NPU was discovered")
            if any(item.health != "healthy" for item in devices):
                raise EnvironmentValidationError(
                    "Doctor: one or more Ascend NPUs are unhealthy"
                )
            environment = discover_ascend_environment(self.device_adapter, devices)
            token = _read_secret(Path(config.authorization_token_file), "cluster token")
            bootstrap = await fetch_node_bootstrap(config, token)
            if not hmac.compare_digest(
                environment.environment_fingerprint,
                bootstrap.environment_fingerprint,
            ):
                raise EnvironmentValidationError(
                    "worker node environment fingerprint does not match Controller"
                )
            boot_id = _boot_id()
            policy = bootstrap.node_runtime_policy
            if not policy.allow_colocation:
                platform_config: AscendCorrectnessConfig | AscendColocationConfig = (
                    AscendCorrectnessConfig(
                        task_slots_total=1,
                        allow_colocation=False,
                        npu_system_reserved_hbm_mb=(
                            policy.npu_system_reserved_hbm_mb
                        ),
                        npu_hbm_headroom_mb=policy.npu_hbm_headroom_mb,
                        host_mem_headroom_mb=policy.host_mem_headroom_mb,
                        io_slots_total=policy.io_slots_total,
                    )
                )
            else:
                platform_config = AscendColocationConfig(
                    task_slots_total=policy.task_slots_total,
                    allow_colocation=True,
                    npu_system_reserved_hbm_mb=policy.npu_system_reserved_hbm_mb,
                    npu_hbm_headroom_mb=policy.npu_hbm_headroom_mb,
                    host_mem_headroom_mb=policy.host_mem_headroom_mb,
                    io_slots_total=policy.io_slots_total,
                )
            capacity = build_ascend_node_capacity(
                node_id=config.node_id,
                boot_id=boot_id,
                node_ip=config.node_ip,
                adapter=self.device_adapter,
                environment=environment,
                config=platform_config,
            )
            device_mappings = _resolve_device_mappings(
                capacity=capacity,
                configured=config.device_mappings,
            )
            worker = ManagedRayWorkerNode(
                address=bootstrap.ray_address,
                namespace=bootstrap.ray_namespace,
                node_ip=config.node_ip,
                temp_directory=config.ray_temp_directory,
                num_cpus=config.ray_num_cpus,
                log_path=Path(config.runtime_directory) / "ray-worker.log",
            )
            self.worker = worker
            ray_node_id = await asyncio.to_thread(worker.start)
            if policy.recording_backend == "parquet":
                cursor_key = _load_or_create_key(
                    Path(config.runtime_directory) / "recording.cursor.key"
                )
                recorder = ParquetRecorder(
                    ParquetRecorderConfig(
                        root_directory=config.recording_root_directory,
                        control_queue_capacity=(
                            policy.recording_control_queue_capacity
                        ),
                        telemetry_queue_capacity=(
                            policy.recording_telemetry_queue_capacity
                        ),
                        batch_size=policy.recording_batch_size,
                        flush_interval_ms=policy.recording_flush_interval_ms,
                        compression=policy.recording_compression,
                        max_page_size=policy.recording_max_page_size,
                    ),
                    cursor_signing_key=cursor_key,
                )
            else:
                recorder = NoopRecorder()
            service_manager = NodeServiceProcessManager(
                node_id=config.node_id,
                boot_id=boot_id,
                device_monitor=cast(ServiceDeviceMonitor, self.device_adapter),
                allowed_executables=(sys.executable,),
                log_directory=Path(config.runtime_directory) / "model-logs",
                hbm_recovery_tolerance_mb=policy.hbm_recovery_tolerance_mb,
                device_mappings=device_mappings,
            )
            agent = NodeAgent(
                identity=NodeAgentIdentity(
                    cluster_id=config.cluster_id,
                    node_id=config.node_id,
                    boot_id=boot_id,
                    ray_node_id=ray_node_id,
                    agent_generation=agent_generation,
                    environment_fingerprint=environment.environment_fingerprint,
                    producer_id=(
                        f"node_agent:{config.node_id}:{boot_id}:{agent_generation}"
                    ),
                    device_mappings=device_mappings,
                ),
                authorization_token=token,
                recorder=recorder,
                worker_device_verifier=lambda pid, device_id: self.device_adapter.verify_process_device(
                    pid, device_id
                ),
                node_observation_provider=lambda sequence, received_at_ms: build_ascend_node_observation(
                    node_id=config.node_id,
                    boot_id=boot_id,
                    sequence=sequence,
                    received_at_ms=received_at_ms,
                    adapter=self.device_adapter,
                ),
                service_process_manager=service_manager,
                node_capacity=capacity,
            )
            self.agent = agent
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signum, stop.set)
                    installed_signals.append(signum)
                except NotImplementedError:  # pragma: no cover
                    pass
            await agent.start(
                controller_endpoint=config.controller_endpoint,
                worker_bind_address=config.worker_rpc_bind_address,
                worker_advertised_host=config.worker_advertised_host,
            )
            await stop.wait()
            return 0
        finally:
            loop = asyncio.get_running_loop()
            for signum in installed_signals:
                try:
                    loop.remove_signal_handler(signum)
                except NotImplementedError:  # pragma: no cover
                    pass
            try:
                if agent is not None:
                    await agent.close(grace_seconds=0)
                else:
                    if service_manager is not None:
                        await service_manager.close(1_000)
                    if recorder is not None:
                        await recorder.close(1_000)
            finally:
                try:
                    if worker is not None:
                        await asyncio.to_thread(worker.close)
                finally:
                    process_lock.close()


def _resolve_device_mappings(
    *,
    capacity: NodeCapacity,
    configured: tuple[RuntimeDeviceMapping, ...],
) -> tuple[RuntimeDeviceMapping, ...]:
    physical_ids = tuple(sorted(item.device_id for item in capacity.npus))
    if not configured:
        return tuple(RuntimeDeviceMapping.identity(item) for item in physical_ids)
    configured_ids = tuple(
        sorted(item.physical_device_id for item in configured)
    )
    if configured_ids != physical_ids:
        raise EnvironmentValidationError(
            "node device_mappings must exactly match discovered physical NPUs"
        )
    return tuple(sorted(configured))


async def fetch_node_bootstrap(
    config: NodeBootstrapConfig,
    token: bytes,
    *,
    timeout_seconds: float = 5.0,
) -> NodeBootstrapResponse:
    request_id = new_id("node_bootstrap")
    async with grpc.aio.insecure_channel(config.controller_endpoint) as channel:
        response = await control_pb2_grpc.NodeControlStub(channel).GetBootstrap(
            control_pb2.NodeBootstrapRequest(  # type: ignore[attr-defined]
                schema_version=1,
                request_id=request_id,
                cluster_id=config.cluster_id,
                node_id=config.node_id,
                authorization_token=token,
            ),
            timeout=timeout_seconds,
        )
    if response.request_id != request_id or response.status_code != "ok":
        raise RuntimeError(response.message or "node bootstrap failed")
    if str(response.cluster_id) != config.cluster_id:
        raise ContractValidationError("node bootstrap cluster_id mismatch")
    required = {
        "controller_generation": str(response.controller_generation),
        "config_fingerprint": str(response.config_fingerprint),
        "environment_fingerprint": str(response.environment_fingerprint),
        "ray_address": str(response.ray_address),
        "ray_namespace": str(response.ray_namespace),
    }
    missing = sorted(name for name, value in required.items() if not value)
    if missing:
        raise ContractValidationError(
            f"node bootstrap response is missing {missing[0]}"
        )
    return NodeBootstrapResponse(
        cluster_id=str(response.cluster_id),
        controller_generation=required["controller_generation"],
        config_fingerprint=required["config_fingerprint"],
        environment_fingerprint=required["environment_fingerprint"],
        ray_address=required["ray_address"],
        ray_namespace=required["ray_namespace"],
        node_runtime_policy=NodeRuntimePolicy(
            task_slots_total=int(response.task_slots_total),
            allow_colocation=bool(response.allow_colocation),
            npu_system_reserved_hbm_mb=int(
                response.npu_system_reserved_hbm_mb
            ),
            npu_hbm_headroom_mb=int(response.npu_hbm_headroom_mb),
            host_mem_headroom_mb=int(response.host_mem_headroom_mb),
            io_slots_total=int(response.io_slots_total),
            hbm_recovery_tolerance_mb=int(response.hbm_recovery_tolerance_mb),
            recording_backend=str(response.recording_backend),
            recording_control_queue_capacity=int(
                response.recording_control_queue_capacity
            ),
            recording_telemetry_queue_capacity=int(
                response.recording_telemetry_queue_capacity
            ),
            recording_batch_size=int(response.recording_batch_size),
            recording_flush_interval_ms=int(response.recording_flush_interval_ms),
            recording_compression=str(response.recording_compression),
            recording_max_page_size=int(response.recording_max_page_size),
        ),
    )


def _read_secret(path: Path, description: str) -> bytes:
    try:
        info = path.stat()
        value = path.read_bytes()
    except OSError as exc:
        raise ContractValidationError(f"{description} is unavailable: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise ContractValidationError(f"{description} must be a regular 0600 file")
    if not value:
        raise ContractValidationError(f"{description} must not be empty")
    return value


def _load_or_create_key(path: Path) -> bytes:
    if path.exists():
        return _read_secret(path, "recording cursor key")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = os.urandom(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(key)
        stream.flush()
        os.fsync(stream.fileno())
    return key


def _boot_id() -> str:
    value = Path("/proc/sys/kernel/random/boot_id").read_text(
        encoding="ascii"
    ).strip()
    if not value:
        raise ContractValidationError("node boot_id is empty")
    return value
