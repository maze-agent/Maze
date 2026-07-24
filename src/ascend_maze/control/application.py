"""C13 managed Head process composition and foreground lifecycle."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import stat
import sys
from typing import cast

from ascend_maze.ascend import (
    AscendColocationConfig,
    AscendCorrectnessConfig,
    DcmiDeviceAdapter,
    build_ascend_node_capacity,
    build_ascend_node_observation,
    discover_aicpu_runtime_library_paths,
    discover_ascend_environment,
    discover_atb_runtime_library_preloads,
)
from ascend_maze.config import LoadedConfig, load_model_catalog
from ascend_maze.config.schema import MainConfig
from ascend_maze.contracts.recording import ParquetRecorderConfig
from ascend_maze.contracts.resources import ReservationVector
from ascend_maze.contracts.runtime import RuntimeDeviceMapping
from ascend_maze.contracts.worker import (
    WarmupManifest,
    WorkerPoolConfig,
    WorkerPoolProfileConfig,
    WorkerProfile,
)
from ascend_maze.control.node_rpc import NodeAgent, NodeAgentIdentity
from ascend_maze.control.contracts import NodeRuntimePolicy
from ascend_maze.control.ray_host import ManagedRayHost
from ascend_maze.control.service_process import (
    NodeAgentServiceProcessBackend,
    NodeServiceProcessManager,
    ServiceDeviceMonitor,
)
from ascend_maze.core.errors import (
    ContractValidationError,
    EnvironmentValidationError,
)
from ascend_maze.core.identifiers import new_id
from ascend_maze.inference import (
    InferenceCoordinator,
    InMemoryPortLeaseManager,
    ModelCatalog,
)
from ascend_maze.inference.adapters.fake import FakeInferenceEngineAdapter
from ascend_maze.inference.adapters.transformers_local import (
    TransformersLocalInferenceEngineAdapter,
)
from ascend_maze.inference.adapters.vllm_ascend import (
    VllmAscendInferenceEngineAdapter,
)
from ascend_maze.placement import PlacementManager
from ascend_maze.recording import NoopRecorder, ParquetRecorder
from ascend_maze.resources import DeclaredOnlyAnchorProvider, StaticAnchorProvider
from ascend_maze.runtime.ray_cluster import RayClusterConfig, current_ray_node_id
from ascend_maze.runtime.ray_node_registry import RayNodeRegistry
from ascend_maze.scheduler import (
    FcfsPolicy,
    HacsNoTpStaticPolicy,
    HeterogeneousPartitioner,
    UnifiedPartitioner,
)


class ControllerApplication:
    def __init__(
        self,
        loaded: LoadedConfig,
        *,
        device_adapter: DcmiDeviceAdapter | None = None,
    ) -> None:
        self.loaded = loaded
        self.config = loaded.config
        self.device_adapter = device_adapter or DcmiDeviceAdapter()
        self.host: ManagedRayHost | None = None

    def build(self) -> ManagedRayHost:
        if self.host is not None:
            return self.host
        config = self.config
        devices = self.device_adapter.devices()
        if not devices:
            raise EnvironmentValidationError("Doctor: no Ascend NPU was discovered")
        if any(item.health != "healthy" for item in devices):
            raise EnvironmentValidationError(
                "Doctor: one or more Ascend NPUs are unhealthy"
            )
        environment = discover_ascend_environment(self.device_adapter, devices)
        configured_environment = config.cluster.environment_fingerprint
        if configured_environment not in {"auto", "local-unverified"} and (
            configured_environment != environment.environment_fingerprint
        ):
            raise EnvironmentValidationError(
                "cluster.environment_fingerprint: current environment does not match"
            )
        boot_id = _boot_id()
        platform_config: AscendCorrectnessConfig | AscendColocationConfig
        if config.profile == "correctness":
            platform_config = AscendCorrectnessConfig(
                anchor_strategy=config.placement.anchor_strategy,
                task_slots_total=config.placement.task_slots_total,
                allow_colocation=config.placement.allow_colocation,
                max_tasks_per_worker=config.worker.max_tasks_per_worker,
                standby_min_idle=config.worker.standby_min_idle,
                npu_system_reserved_hbm_mb=config.placement.npu_system_reserved_hbm_mb,
                npu_hbm_headroom_mb=config.placement.npu_hbm_headroom_mb,
                host_mem_headroom_mb=config.placement.host_mem_headroom_mb,
                io_slots_total=config.placement.io_slots_total,
                worker_binding_deadline_ms=config.worker.binding_deadline_ms,
                hbm_recovery_deadline_ms=config.worker.hbm_recovery_deadline_ms,
                hbm_recovery_tolerance_mb=config.worker.hbm_recovery_tolerance_mb,
            )
        else:
            platform_config = AscendColocationConfig(
                anchor_strategy=config.placement.anchor_strategy,
                scheduler_policy=config.scheduler.policy,
                task_slots_total=config.placement.task_slots_total,
                allow_colocation=config.placement.allow_colocation,
                max_tasks_per_worker=config.worker.max_tasks_per_worker,
                standby_min_idle=config.worker.standby_min_idle,
                npu_system_reserved_hbm_mb=config.placement.npu_system_reserved_hbm_mb,
                npu_hbm_headroom_mb=config.placement.npu_hbm_headroom_mb,
                host_mem_headroom_mb=config.placement.host_mem_headroom_mb,
                io_slots_total=config.placement.io_slots_total,
                worker_binding_deadline_ms=config.worker.binding_deadline_ms,
                hbm_recovery_deadline_ms=config.worker.hbm_recovery_deadline_ms,
                hbm_recovery_tolerance_mb=config.worker.hbm_recovery_tolerance_mb,
            )
        capacity = build_ascend_node_capacity(
            node_id=config.cluster.head_node_id,
            boot_id=boot_id,
            node_ip=config.cluster.head_node_ip,
            adapter=self.device_adapter,
            environment=environment,
            config=platform_config,
        )
        device_mappings = tuple(
            RuntimeDeviceMapping.identity(item.device_id) for item in capacity.npus
        )
        token = _read_secret(Path(config.control.cluster_token_file), "cluster token")
        cursor_key = _load_or_create_cursor_key(
            Path(config.recording.cursor_signing_key_file)
            if config.recording.cursor_signing_key_file is not None
            else Path(config.control.runtime_directory) / "recording.cursor.key"
        )
        controller_generation = new_id("controller")
        node_registry = RayNodeRegistry()
        placement = PlacementManager(
            host_mem_headroom_mb=config.placement.host_mem_headroom_mb,
            npu_hbm_headroom_mb=config.placement.npu_hbm_headroom_mb,
            required_environment_fingerprint=environment.environment_fingerprint,
        )
        placement.register_node(capacity)
        recorder = _recorder(config, cursor_key)
        service_backend = NodeAgentServiceProcessBackend(
            cluster_id=config.cluster.cluster_id,
            authorization_token=token,
            controller_generation=controller_generation,
            node_registry=node_registry,
            rpc_timeout_ms=config.control.shutdown_cleanup_timeout_ms,
        )
        inference = _inference(
            loaded=self.loaded,
            placement=placement,
            node_registry=node_registry,
            service_backend=service_backend,
            environment_fingerprint=environment.environment_fingerprint,
        )

        def head_agent_factory() -> NodeAgent:
            ray_node_id = current_ray_node_id()
            agent_generation = new_id("node_agent")
            producer_id = (
                f"node_agent:{config.cluster.head_node_id}:{boot_id}:{agent_generation}"
            )
            node_recorder = _recorder(config, cursor_key)
            service_manager = NodeServiceProcessManager(
                node_id=config.cluster.head_node_id,
                boot_id=boot_id,
                device_monitor=cast(ServiceDeviceMonitor, self.device_adapter),
                allowed_executables=(sys.executable,),
                log_directory=Path(config.control.runtime_directory) / "model-logs",
                hbm_recovery_tolerance_mb=config.worker.hbm_recovery_tolerance_mb,
                device_mappings=device_mappings,
            )
            return NodeAgent(
                identity=NodeAgentIdentity(
                    cluster_id=config.cluster.cluster_id,
                    node_id=config.cluster.head_node_id,
                    boot_id=boot_id,
                    ray_node_id=ray_node_id,
                    agent_generation=agent_generation,
                    environment_fingerprint=environment.environment_fingerprint,
                    producer_id=producer_id,
                    device_mappings=device_mappings,
                ),
                authorization_token=token,
                recorder=node_recorder,
                worker_device_verifier=lambda pid, device_id: self.device_adapter.verify_process_device(
                    pid, device_id
                ),
                node_observation_provider=lambda sequence, received_at_ms: build_ascend_node_observation(
                    node_id=config.cluster.head_node_id,
                    boot_id=boot_id,
                    sequence=sequence,
                    received_at_ms=received_at_ms,
                    adapter=self.device_adapter,
                ),
                service_process_manager=service_manager,
                node_capacity=capacity,
            )

        policy = (
            HacsNoTpStaticPolicy()
            if config.scheduler.policy == "hacs_no_tp"
            else FcfsPolicy()
        )
        partitioner = (
            UnifiedPartitioner()
            if config.scheduler.partitioner == "unified"
            else HeterogeneousPartitioner()
        )
        anchors = (
            StaticAnchorProvider(
                environment_fingerprint=environment.environment_fingerprint
            )
            if config.placement.anchor_strategy == "static"
            else DeclaredOnlyAnchorProvider(
                environment_fingerprint=environment.environment_fingerprint
            )
        )
        worker_pool_config = _worker_pool_config(config)
        ray_config = RayClusterConfig(
            namespace=config.ray.namespace,
            temp_directory=config.ray.temp_directory,
            include_dashboard=config.ray.include_dashboard,
            local_num_cpus=config.ray.local_num_cpus,
            local_object_store_memory=config.ray.object_store_memory_bytes,
            disable_ray_npu_resource=config.ray.disable_ray_npu_resource,
        )
        self.host = ManagedRayHost(
            ray_config=ray_config,
            cluster_id=config.cluster.cluster_id,
            authorization_token=token,
            config_fingerprint=self.loaded.snapshot.config_fingerprint,
            environment_fingerprint=environment.environment_fingerprint,
            build_revision=os.environ.get("ASCEND_MAZE_BUILD_REVISION", "uncommitted"),
            node_capacities=(capacity,),
            node_runtime_policy=NodeRuntimePolicy(
                task_slots_total=config.placement.task_slots_total,
                allow_colocation=config.placement.allow_colocation,
                npu_system_reserved_hbm_mb=(
                    config.placement.npu_system_reserved_hbm_mb
                ),
                npu_hbm_headroom_mb=config.placement.npu_hbm_headroom_mb,
                host_mem_headroom_mb=config.placement.host_mem_headroom_mb,
                io_slots_total=config.placement.io_slots_total,
                hbm_recovery_tolerance_mb=config.worker.hbm_recovery_tolerance_mb,
                recording_backend=config.recording.backend,
                recording_control_queue_capacity=(
                    config.recording.control_queue_capacity
                ),
                recording_telemetry_queue_capacity=(
                    config.recording.telemetry_queue_capacity
                ),
                recording_batch_size=config.recording.batch_size,
                recording_flush_interval_ms=config.recording.flush_interval_ms,
                recording_compression=config.recording.compression,
                recording_max_page_size=config.recording.max_page_size,
            ),
            control_socket_path=Path(config.control.socket_path),
            controller_generation=controller_generation,
            node_rpc_bind_address=config.control.node_rpc_bind_address,
            node_rpc_advertised_host=config.control.node_rpc_advertised_host,
            anchors=anchors,
            placement=placement,
            policy=policy,
            partitioner=partitioner,
            placement_lookahead=config.scheduler.placement_lookahead,
            max_bypass_count=config.scheduler.max_bypass_count,
            dispatch_timeout_ms=config.scheduler.dispatch_timeout_ms,
            recorder_flush_timeout_ms=config.recording.flush_timeout_ms,
            recorder=recorder,
            worker_pool_config=worker_pool_config,
            inference=inference,
            node_registry=node_registry,
            recovery_path=Path(config.control.recovery_path),
            pid_lock_path=Path(config.control.pid_file),
            head_node_agent_factory=head_agent_factory,
            shutdown_drain_timeout_ms=config.control.shutdown_drain_timeout_ms,
            shutdown_cleanup_timeout_ms=config.control.shutdown_cleanup_timeout_ms,
            max_inline_control_bytes=config.control.max_inline_control_bytes,
        )
        return self.host

    async def run(self) -> int:
        host = self.build()
        controller = await host.start()
        loop = asyncio.get_running_loop()
        signal_tasks: set[asyncio.Task[object]] = set()

        def request_shutdown() -> None:
            if controller.lifecycle_state.value in {"draining", "stopped"}:
                return
            task = asyncio.create_task(controller.close())
            signal_tasks.add(task)
            task.add_done_callback(signal_tasks.discard)

        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, request_shutdown)
            except NotImplementedError:  # pragma: no cover
                pass
        try:
            await controller.wait_stopped()
            result = await controller.close()
            return result.exit_code
        finally:
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.remove_signal_handler(signum)
                except NotImplementedError:  # pragma: no cover
                    pass
            await host.close()


def _worker_pool_config(config: MainConfig) -> WorkerPoolConfig:
    """Translate the frozen C13 worker watermarks into the C10 pool contract."""

    worker = config.worker
    min_idle = worker.standby_min_idle
    max_idle = worker.standby_max_idle
    mode = "zero_hbm_standby" if max_idle > 0 else "cold_start"
    profiles = tuple(
        WorkerPoolProfileConfig(
            profile=profile,
            min_idle=min_idle,
            max_idle=max_idle,
            max_total=worker.max_total,
            replenish_concurrency=1,
            idle_ttl_ms=60_000,
            acquire_timeout_ms=worker.binding_deadline_ms,
            max_tasks_per_worker=worker.max_tasks_per_worker,
            max_worker_lifetime_ms=120_000,
            max_rss_growth_mb=256,
            standby_resources=ReservationVector(
                cpu_num=1,
                host_mem_mb=256,
                io_slots=1 if profile is WorkerProfile.IO else 0,
                npu_hbm_mb=0,
                npu_slots=0,
            ),
            termination_timeout_ms=worker.binding_deadline_ms,
            warmup_manifest=WarmupManifest(("json",)),
        )
        for profile in (WorkerProfile.CPU, WorkerProfile.IO, WorkerProfile.NPU_HOST)
    )
    return WorkerPoolConfig(
        mode=mode,
        profiles=profiles,
        reconcile_interval_ms=250,
        config_generation=1,
    )


def _recorder(config: object, cursor_key: bytes) -> ParquetRecorder | NoopRecorder:
    from ascend_maze.config.schema import MainConfig

    if not isinstance(config, MainConfig):
        raise TypeError("config must be MainConfig")
    if config.recording.backend == "noop":
        return NoopRecorder()
    return ParquetRecorder(
        ParquetRecorderConfig(
            root_directory=config.recording.root_directory,
            control_queue_capacity=config.recording.control_queue_capacity,
            telemetry_queue_capacity=config.recording.telemetry_queue_capacity,
            batch_size=config.recording.batch_size,
            flush_interval_ms=config.recording.flush_interval_ms,
            compression=config.recording.compression,
            max_page_size=config.recording.max_page_size,
        ),
        cursor_signing_key=cursor_key,
    )


def _inference(
    *,
    loaded: LoadedConfig,
    placement: PlacementManager,
    node_registry: RayNodeRegistry,
    service_backend: NodeAgentServiceProcessBackend,
    environment_fingerprint: str,
) -> InferenceCoordinator | None:
    path = loaded.config.inference.model_catalog_path
    if path is None:
        return None
    document = load_model_catalog(path, environment_fingerprint=environment_fingerprint)
    backends = {item.backend for item in document.specs}
    if backends == {"fake"}:
        fake = FakeInferenceEngineAdapter()
        catalog = ModelCatalog(document.specs, adapters={"fake": fake})
        return InferenceCoordinator(
            catalog=catalog,
            placement=placement,
            service_backend=fake,
            reconcile_interval_ms=loaded.config.inference.reconcile_interval_ms,
        )
    if backends == {"transformers_local"}:
        transformers_adapter = TransformersLocalInferenceEngineAdapter()
        catalog = ModelCatalog(
            document.specs,
            adapters={"transformers_local": transformers_adapter},
            environment_capabilities=("ascend", "transformers_local"),
            max_single_npu_hbm_mb=placement.max_single_npu_allocatable_hbm_mb(),
        )
        return InferenceCoordinator(
            catalog=catalog,
            placement=placement,
            service_backend=transformers_adapter,
            port_leases=InMemoryPortLeaseManager(),
            reconcile_interval_ms=loaded.config.inference.reconcile_interval_ms,
        )
    if backends != {"vllm_ascend"}:
        raise ContractValidationError(
            "one Controller generation cannot mix inference service backends"
        )
    vllm_adapter = VllmAscendInferenceEngineAdapter(
        process_backend=service_backend,
        python_executable=sys.executable,
        endpoint_host_resolver=service_backend.endpoint_host,
        runtime_library_preloads=discover_atb_runtime_library_preloads(),
        runtime_library_paths=discover_aicpu_runtime_library_paths(),
    )
    catalog = ModelCatalog(
        document.specs,
        adapters={"vllm_ascend": vllm_adapter},
        environment_capabilities=("ascend", "vllm_ascend"),
        max_single_npu_hbm_mb=placement.max_single_npu_allocatable_hbm_mb(),
    )
    return InferenceCoordinator(
        catalog=catalog,
        placement=placement,
        service_backend=service_backend,
        port_leases=service_backend,
        reconcile_interval_ms=loaded.config.inference.reconcile_interval_ms,
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


def _load_or_create_cursor_key(path: Path) -> bytes:
    if path.exists():
        key = _read_secret(path, "recording cursor signing key")
        if len(key) < 32:
            raise ContractValidationError(
                "recording cursor signing key must contain at least 32 bytes"
            )
        return key
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    key = os.urandom(32)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(key)
        stream.flush()
        os.fsync(stream.fileno())
    return key


def _boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
    except OSError as exc:
        raise ContractValidationError("cannot read node boot_id") from exc
    if not value:
        raise ContractValidationError("node boot_id is empty")
    return value
