"""Managed Ray connection and Controller lifecycle for a Head process."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable

from ascend_maze.core.clock import Clock
from ascend_maze.contracts.recording import ExecutionRecorder
from ascend_maze.contracts.worker import WorkerPoolConfig
from ascend_maze.placement import NodeCapacity
from ascend_maze.placement import PlacementManager
from ascend_maze.resources import ResourceAnchorProvider
from ascend_maze.runtime.ray_cluster import (
    ManagedRayCluster,
    RayClusterConfig,
    current_ray_address,
)
from ascend_maze.scheduler import QueuePartitioner, SchedulingPolicy

from ascend_maze.control.ray_controller import RayHostController
from ascend_maze.control.contracts import NodeRuntimePolicy
from ascend_maze.control.node_rpc import NodeAgent
from ascend_maze.control.process_lock import ControllerProcessLock
from ascend_maze.control.recovery import ControllerRecoveryStore
from ascend_maze.inference import InferenceCoordinator
from ascend_maze.runtime.ray_node_registry import RayNodeRegistry
from ascend_maze.core.identifiers import new_id


class ManagedRayHost:
    def __init__(
        self,
        *,
        ray_config: RayClusterConfig,
        cluster_id: str,
        authorization_token: bytes,
        config_fingerprint: str,
        environment_fingerprint: str,
        build_revision: str,
        node_capacities: tuple[NodeCapacity, ...],
        node_runtime_policy: NodeRuntimePolicy | None = None,
        control_socket_path: Path | None = None,
        controller_generation: str | None = None,
        node_rpc_bind_address: str = "127.0.0.1:0",
        node_rpc_advertised_host: str | None = None,
        clock: Clock | None = None,
        anchors: ResourceAnchorProvider | None = None,
        placement: PlacementManager | None = None,
        policy: SchedulingPolicy | None = None,
        partitioner: QueuePartitioner | None = None,
        placement_lookahead: int = 8,
        max_bypass_count: int = 8,
        dispatch_timeout_ms: int = 5_000,
        recorder_flush_timeout_ms: int = 1_000,
        worker_pool_config: WorkerPoolConfig | None = None,
        recorder: ExecutionRecorder | None = None,
        inference: InferenceCoordinator | None = None,
        node_registry: RayNodeRegistry | None = None,
        recovery_store: ControllerRecoveryStore | None = None,
        recovery_path: Path | None = None,
        pid_lock_path: Path | None = None,
        head_node_agent_factory: Callable[[], NodeAgent] | None = None,
        shutdown_drain_timeout_ms: int = 5_000,
        shutdown_cleanup_timeout_ms: int = 30_000,
        max_inline_control_bytes: int = 1_048_576,
    ) -> None:
        self.ray_cluster = ManagedRayCluster(ray_config)
        self.cluster_id = cluster_id
        self.authorization_token = authorization_token
        self.config_fingerprint = config_fingerprint
        self.environment_fingerprint = environment_fingerprint
        self.build_revision = build_revision
        self.node_capacities = node_capacities
        self.node_runtime_policy = node_runtime_policy or NodeRuntimePolicy()
        self.control_socket_path = control_socket_path
        self.controller_generation = controller_generation or new_id("controller")
        self.node_rpc_bind_address = node_rpc_bind_address
        self.node_rpc_advertised_host = node_rpc_advertised_host
        self.clock = clock
        self.anchors = anchors
        self.placement = placement
        self.policy = policy
        self.partitioner = partitioner
        self.placement_lookahead = placement_lookahead
        self.max_bypass_count = max_bypass_count
        self.dispatch_timeout_ms = dispatch_timeout_ms
        self.recorder_flush_timeout_ms = recorder_flush_timeout_ms
        self.worker_pool_config = worker_pool_config
        self.recorder = recorder
        self.inference = inference
        self.node_registry = node_registry
        self.recovery_store = recovery_store
        self.recovery_path = recovery_path
        self.pid_lock = (
            None
            if pid_lock_path is None
            else ControllerProcessLock(
                pid_lock_path,
                controller_generation=self.controller_generation,
            )
        )
        self.head_node_agent_factory = head_node_agent_factory
        self.head_node_agent: NodeAgent | None = None
        self.shutdown_drain_timeout_ms = shutdown_drain_timeout_ms
        self.shutdown_cleanup_timeout_ms = shutdown_cleanup_timeout_ms
        self.max_inline_control_bytes = max_inline_control_bytes
        self.controller: RayHostController | None = None

    async def start(self) -> RayHostController:
        if self.controller is not None:
            return self.controller
        if self.pid_lock is not None:
            self.pid_lock.acquire()
        try:
            self.ray_cluster.start()
            self.head_node_agent = (
                None
                if self.head_node_agent_factory is None
                else self.head_node_agent_factory()
            )
            controller = RayHostController(
                cluster_id=self.cluster_id,
                authorization_token=self.authorization_token,
                ray_namespace=self.ray_cluster.config.namespace,
                ray_address=current_ray_address(),
                config_fingerprint=self.config_fingerprint,
                environment_fingerprint=self.environment_fingerprint,
                build_revision=self.build_revision,
                node_capacities=self.node_capacities,
                node_runtime_policy=self.node_runtime_policy,
                control_socket_path=self.control_socket_path,
                controller_generation=self.controller_generation,
                node_rpc_bind_address=self.node_rpc_bind_address,
                node_rpc_advertised_host=self.node_rpc_advertised_host,
                clock=self.clock,
                anchors=self.anchors,
                placement=self.placement,
                policy=self.policy,
                partitioner=self.partitioner,
                placement_lookahead=self.placement_lookahead,
                max_bypass_count=self.max_bypass_count,
                dispatch_timeout_ms=self.dispatch_timeout_ms,
                recorder_flush_timeout_ms=self.recorder_flush_timeout_ms,
                worker_pool_config=self.worker_pool_config,
                recorder=self.recorder,
                inference=self.inference,
                node_registry=self.node_registry,
                recovery_store=self.recovery_store,
                recovery_path=self.recovery_path,
                head_node_agent=self.head_node_agent,
                shutdown_drain_timeout_ms=self.shutdown_drain_timeout_ms,
                shutdown_cleanup_timeout_ms=self.shutdown_cleanup_timeout_ms,
                max_inline_control_bytes=self.max_inline_control_bytes,
            )
            await controller.start()
        except Exception:
            self.ray_cluster.close()
            if self.pid_lock is not None:
                self.pid_lock.close()
            raise
        self.controller = controller
        return controller

    async def close(self) -> None:
        controller = self.controller
        self.controller = None
        try:
            if controller is not None:
                await controller.close()
        finally:
            try:
                if controller is not None and controller.local_rpc is not None:
                    await controller.local_rpc.close(grace_seconds=1.0)
            finally:
                self.ray_cluster.close()
                if self.pid_lock is not None:
                    self.pid_lock.close()
