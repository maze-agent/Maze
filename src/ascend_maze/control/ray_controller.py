"""Stage-three Controller composition for the Ray Host execution path."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from ascend_maze.core.clock import Clock
from ascend_maze.core.errors import StateTransitionError
from ascend_maze.core.identifiers import new_id
from ascend_maze.inference import InferenceCoordinator, ServiceProcessExit
from ascend_maze.contracts.recording import (
    ExecutionRecorder,
    HistoricalEventReader,
    RunEventPage,
)
from ascend_maze.contracts.runtime import RuntimeNodeBinding
from ascend_maze.contracts.worker import WorkerPoolConfig
from ascend_maze.contracts.submission import SubmissionState
from ascend_maze.data.index import RunDataState
from ascend_maze.data.ray_store import (
    RayDataStore,
    RayDataStoreDescriptor,
    RayDataStoreOwnerUnavailableError,
)
from ascend_maze.placement import LeaseStatus, NodeCapacity, NodeObservation, NodeStatus
from ascend_maze.placement import PlacementManager
from ascend_maze.recording import InMemoryRecorder
from ascend_maze.runtime.ray_backend import RayRuntimeBackend
from ascend_maze.runtime.ray_node_registry import (
    RayNodeRegistry,
    RuntimeNodeStatus,
)
from ascend_maze.runtime.worker_broker import ColdWorkerBroker
from ascend_maze.runtime.worker_pool import (
    StandbyWorkerBroker,
    WorkerPoolEvent,
)
from ascend_maze.runtime.ray_worker_pool import RayWorkerEndpointFactory
from ascend_maze.resources import ResourceAnchorProvider
from ascend_maze.scheduler import QueuePartitioner, SchedulingPolicy

from ascend_maze.control.controller import InMemoryController
from ascend_maze.control.contracts import NodeRuntimePolicy
from ascend_maze.control.lifecycle import ShutdownResource, ShutdownResult
from ascend_maze.control.local_rpc import (
    ControllerStatus,
    LocalControlServer,
)
from ascend_maze.control.node_rpc import (
    NodeAgent,
    NodeControlServer,
    NodeRecoveryInventory,
)
from ascend_maze.control.recovery import (
    ControllerCheckpoint,
    ControllerRecoveryStore,
    RecoveryIdentity,
    SqliteControllerRecoveryStore,
)


def _assert_quiescent_data_owner_rotation(checkpoint: ControllerCheckpoint) -> None:
    runs_by_id = {item.run_id: item for item in checkpoint.runs}
    for run in checkpoint.runs:
        tombstone = run.index.tombstone
        if (
            run.destroy_result is None
            or run.index.state is not RunDataState.DESTROYED
            or tombstone is None
            or not tombstone.destroy_succeeded
        ):
            raise StateTransitionError(
                "cannot rotate unavailable DataStoreOwner while checkpoint Run "
                f"is not successfully destroyed: {run.run_id}"
            )
        if run.index.workflow_inputs or run.index.task_outputs:
            raise StateTransitionError(
                "cannot rotate unavailable DataStoreOwner while checkpoint Run "
                f"still contains DataHandles: {run.run_id}"
            )
    for submission in checkpoint.submissions:
        if submission.state is SubmissionState.PREPARING:
            raise StateTransitionError(
                "cannot rotate unavailable DataStoreOwner while a submission is "
                f"preparing: {submission.submission_id}"
            )
        if submission.state is SubmissionState.COMMITTED and (
            submission.run_id is None or submission.run_id not in runs_by_id
        ):
            raise StateTransitionError(
                "cannot rotate unavailable DataStoreOwner because a committed "
                f"submission has no recovered Run: {submission.submission_id}"
            )


def _rotate_quiescent_data_owner(
    checkpoint: ControllerCheckpoint,
    *,
    owner_generation: str,
    descriptor: RayDataStoreDescriptor,
) -> ControllerCheckpoint:
    _assert_quiescent_data_owner_rotation(checkpoint)
    return replace(
        checkpoint,
        data_owner_generation=owner_generation,
        data_store_descriptor=descriptor,
        submissions=tuple(
            replace(item, workflow_inputs=()) for item in checkpoint.submissions
        ),
    )


class RayHostController(InMemoryController):
    """Reuse the serial submission/scheduler authority with Ray boundaries."""

    def __init__(
        self,
        *,
        cluster_id: str,
        authorization_token: bytes,
        ray_namespace: str,
        config_fingerprint: str,
        environment_fingerprint: str,
        build_revision: str,
        node_capacities: tuple[NodeCapacity, ...],
        ray_address: str = "unconfigured",
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
        shutdown_drain_timeout_ms: int = 5_000,
        shutdown_cleanup_timeout_ms: int = 30_000,
        head_node_agent: NodeAgent | None = None,
        max_inline_control_bytes: int = 1_048_576,
    ) -> None:
        if recovery_store is not None and recovery_path is not None:
            raise ValueError("configure recovery_store or recovery_path, not both")
        generation = controller_generation or new_id("controller")
        effective_recovery = recovery_store or (
            None
            if recovery_path is None
            else SqliteControllerRecoveryStore(recovery_path)
        )
        recovery_claim = (
            None
            if effective_recovery is None
            else effective_recovery.claim_generation(
                identity=RecoveryIdentity(
                    cluster_id=cluster_id,
                    config_fingerprint=config_fingerprint,
                    environment_fingerprint=environment_fingerprint,
                    build_revision=build_revision,
                ),
                controller_generation=generation,
            )
        )
        recovered_checkpoint = (
            None if recovery_claim is None else recovery_claim.checkpoint
        )
        descriptor = (
            None
            if recovered_checkpoint is None
            else recovered_checkpoint.data_store_descriptor
        )
        if descriptor is not None and not isinstance(
            descriptor, RayDataStoreDescriptor
        ):
            raise TypeError("recovered DataStore descriptor is not a Ray descriptor")
        if descriptor is not None and descriptor.owner_namespace != ray_namespace:
            raise StateTransitionError(
                "recovered DataStoreOwner namespace does not match configured Ray "
                "namespace"
            )
        data_owner_generation = (
            generation
            if recovered_checkpoint is None
            else recovered_checkpoint.data_owner_generation
        )
        if descriptor is None:
            data_store = RayDataStore.start(
                owner_generation=data_owner_generation,
                namespace=ray_namespace,
            )
        else:
            try:
                data_store = RayDataStore.connect(descriptor)
            except RayDataStoreOwnerUnavailableError:
                assert recovered_checkpoint is not None
                _assert_quiescent_data_owner_rotation(recovered_checkpoint)
                data_owner_generation = new_id("data_owner")
                data_store = RayDataStore.start(
                    owner_generation=data_owner_generation,
                    namespace=ray_namespace,
                )
                recovered_checkpoint = _rotate_quiescent_data_owner(
                    recovered_checkpoint,
                    owner_generation=data_owner_generation,
                    descriptor=data_store.descriptor,
                )
                assert recovery_claim is not None
                recovery_claim = replace(
                    recovery_claim,
                    checkpoint=recovered_checkpoint,
                )
        effective_recorder = recorder or InMemoryRecorder()
        node_registry = node_registry or RayNodeRegistry()
        effective_placement = placement or PlacementManager()
        pool_events: list[WorkerPoolEvent] = []

        def capture_pool_event(event: WorkerPoolEvent) -> None:
            pool_events.append(event)
            if event.run_id is None or not hasattr(self, "core"):
                return
            self.core.record_control_event(
                event.run_id,
                event.event_type,
                task_id=event.task_id,
                attempt=event.attempt,
                lease_id=event.placement_lease_id,
                payload={
                    "boot_id": event.boot_id,
                    "worker_id": event.worker_id,
                    "worker_lease_id": event.worker_lease_id,
                    "worker_profile": event.profile.value,
                    "reason": event.reason,
                    "worker_acquire_ms": event.worker_acquire_ms,
                    "cold_start_ms": event.cold_start_ms,
                    "host_warmup_ms": event.host_warmup_ms,
                },
            )

        worker_broker: ColdWorkerBroker | StandbyWorkerBroker
        if worker_pool_config is None:
            worker_broker = ColdWorkerBroker(
                node_registry=node_registry,
                environment_fingerprint=environment_fingerprint,
            )
        else:
            worker_broker = StandbyWorkerBroker(
                node_registry=node_registry,
                placement=effective_placement,
                environment_fingerprint=environment_fingerprint,
                config=worker_pool_config,
                endpoint_factory=RayWorkerEndpointFactory(),
                event_sink=capture_pool_event,
            )
        runtime = RayRuntimeBackend(
            data_store=data_store,
            node_registry=node_registry,
            worker_broker=worker_broker,
            cluster_id=cluster_id,
            owner_generation=data_owner_generation,
            controller_generation=generation,
            environment_fingerprint=environment_fingerprint,
            authorization_token=authorization_token,
            recording_error_sink=effective_recorder.record_writer_error,
            inference=inference,
        )
        super().__init__(
            config_fingerprint=config_fingerprint,
            environment_fingerprint=environment_fingerprint,
            build_revision=build_revision,
            node_capacities=node_capacities,
            cluster_id=cluster_id,
            controller_generation=generation,
            data_owner_generation=data_owner_generation,
            data_store_descriptor=data_store.descriptor,
            recovery_store=effective_recovery,
            recovery_claim=recovery_claim,
            clock=clock,
            data_store=data_store,
            recorder=effective_recorder,
            runtime=runtime,
            anchors=anchors,
            placement=effective_placement,
            policy=policy,
            partitioner=partitioner,
            placement_lookahead=placement_lookahead,
            max_bypass_count=max_bypass_count,
            dispatch_timeout_ms=dispatch_timeout_ms,
            recorder_flush_timeout_ms=recorder_flush_timeout_ms,
            inference=inference,
            shutdown_drain_timeout_ms=shutdown_drain_timeout_ms,
            shutdown_cleanup_timeout_ms=shutdown_cleanup_timeout_ms,
        )
        self.cluster_id = cluster_id
        self.authorization_token = authorization_token
        self.ray_namespace = ray_namespace
        self._recovery_enabled = effective_recovery is not None
        self._recovering_generation = recovered_checkpoint is not None
        self._previous_controller_generation = (
            recovered_checkpoint.controller_generation
            if recovered_checkpoint is not None
            else (
                None if recovery_claim is None else recovery_claim.previous_generation
            )
        )
        self._old_code_registry_released = False
        self.ray_data_store = data_store
        self.ray_recorder = effective_recorder
        self.node_registry = node_registry
        self.worker_broker = worker_broker
        self.worker_pool_config = worker_pool_config
        self.pool_events = pool_events
        self.ray_runtime = runtime
        self._node_capacities = {
            capacity.node_id: capacity for capacity in node_capacities
        }
        self.node_rpc_bind_address = node_rpc_bind_address
        self.node_rpc_advertised_host = node_rpc_advertised_host
        self.node_rpc = NodeControlServer(
            cluster_id=cluster_id,
            authorization_token=authorization_token,
            controller_generation=generation,
            environment_fingerprint=environment_fingerprint,
            config_fingerprint=config_fingerprint,
            ray_address=ray_address,
            ray_namespace=ray_namespace,
            node_runtime_policy=node_runtime_policy,
            registry=node_registry,
            recorder=effective_recorder,
            event_sink=runtime.post_node_event,
            on_binding_replaced=self._binding_replaced,
            on_binding_disconnected=self._binding_disconnected,
            on_binding_registered=self._binding_registered,
            registration_validator=self._validate_node_registration,
            on_node_observation=self._node_observation,
            on_service_process_exited=self._service_process_exited,
            on_recovery_inventory=self._recovery_inventory_changed,
            on_node_capacity=self._register_node_capacity,
            clock=self.clock,
        )
        self._recovery_inventory: dict[str, NodeRecoveryInventory] = {}
        self._recovery_tasks: dict[str, asyncio.Task[None]] = {}
        active_recovery_nodes = {
            item.lease.node_id
            for item in (
                () if recovered_checkpoint is None else recovered_checkpoint.leases
            )
            if item.status in {LeaseStatus.RESERVED, LeaseStatus.BOUND}
        }
        if recovered_checkpoint is not None:
            active_recovery_nodes.update(
                str(getattr(item, "placement_lease").node_id)
                for item in recovered_checkpoint.model_instances
                if getattr(item, "placement_lease", None) is not None
            )
        self._recovery_pending_nodes = active_recovery_nodes
        self.local_rpc = (
            None
            if control_socket_path is None
            else LocalControlServer(
                socket_path=control_socket_path,
                status_provider=self._controller_status,
                control_api=self,
                max_inline_control_bytes=max_inline_control_bytes,
            )
        )
        self._ray_host_closed = False
        self._controlled_transport_shutdown = False
        self._defer_local_rpc_close = False
        self._node_drain_states: dict[str, tuple[str, NodeStatus]] = {}
        self._node_drain_observation_floor: dict[str, int] = {}
        self.head_node_agent = head_node_agent
        if isinstance(worker_broker, StandbyWorkerBroker):
            worker_broker.set_resource_changed_sink(
                lambda reason: self._post_pool_resource_changed(reason)
            )

    @property
    def node_rpc_endpoint(self) -> str:
        endpoint = self.node_rpc.endpoint
        if endpoint is None:
            raise RuntimeError("RayHostController is not started")
        return endpoint

    @property
    def recovery_pending_nodes(self) -> tuple[str, ...]:
        return tuple(sorted(self._recovery_pending_nodes))

    async def start(self) -> None:
        if self._started:
            return
        await super().start()
        if self._recovering_generation and not self._recovery_pending_nodes:
            self._release_old_code_registry()
        try:
            await self.node_rpc.start(
                self.node_rpc_bind_address,
                advertised_host=self.node_rpc_advertised_host,
            )
            if self.head_node_agent is not None:
                await self.head_node_agent.start(
                    controller_endpoint=self.node_rpc_endpoint,
                )
            if isinstance(self.worker_broker, StandbyWorkerBroker):
                await self.worker_broker.start()
            if self.local_rpc is not None:
                await self.local_rpc.start()
        except Exception:
            await super().close(force=True, drain_timeout_ms=0)
            raise

    async def close(
        self,
        *,
        force: bool = False,
        drain_timeout_ms: int | None = None,
    ) -> ShutdownResult:
        return await super().close(
            force=force,
            drain_timeout_ms=drain_timeout_ms,
        )

    async def _stop_worker_runtime(self) -> None:
        await self.ray_runtime.close()
        if isinstance(self.worker_broker, StandbyWorkerBroker):
            await self.worker_broker.close()

    async def _stop_runtime_generation(self) -> None:
        self.ray_data_store.close(kill_owner=not self._recovery_enabled)

    async def _stop_control_transports(self) -> None:
        if self._ray_host_closed:
            return
        self._controlled_transport_shutdown = True
        if self.head_node_agent is not None:
            await self.head_node_agent.close(grace_seconds=0)
        if self.local_rpc is not None and not self._defer_local_rpc_close:
            await self.local_rpc.close()
        await self.node_rpc.close()
        self._ray_host_closed = True

    def _collect_extra_incomplete_resources(
        self,
    ) -> tuple[ShutdownResource, ...]:
        resources: list[ShutdownResource] = []
        active_workers = self.worker_broker.active_count()
        if active_workers:
            resources.append(
                ShutdownResource(
                    kind="worker_lease",
                    resource_id="ray_worker_broker",
                    state="active",
                    details={"active_count": active_workers},
                )
            )
        for node_id in sorted(self._recovery_pending_nodes):
            inventory = self._recovery_inventory.get(node_id)
            if inventory is None:
                resources.append(
                    ShutdownResource(
                        kind="node_recovery_inventory",
                        resource_id=node_id,
                        state="unconfirmed",
                        node_id=node_id,
                    )
                )
                continue
            for lease_id in inventory.active_lease_ids:
                resources.append(
                    ShutdownResource(
                        kind="node_agent_lease",
                        resource_id=lease_id,
                        state="active",
                        node_id=node_id,
                    )
                )
            for handle_id in inventory.service_handle_ids:
                resources.append(
                    ShutdownResource(
                        kind="service_process",
                        resource_id=handle_id,
                        state="active",
                        node_id=node_id,
                    )
                )
        return tuple(resources)

    async def crash(self) -> None:
        await super().crash()
        if isinstance(self.worker_broker, StandbyWorkerBroker):
            await self.worker_broker.close()
        if self.local_rpc is not None:
            await self.local_rpc.close()
        await self.node_rpc.close(grace_seconds=0)
        self._ray_host_closed = True

    def _controller_status(self) -> ControllerStatus:
        return ControllerStatus(
            controller_generation=self.controller_generation,
            build_revision=self.build_revision,
            environment_fingerprint=self.environment_fingerprint,
            healthy_node_count=len(self.node_registry.active_bindings()),
        )

    async def _validate_node_drain(self, node_id: str, boot_id: str) -> None:
        try:
            binding = self.node_registry.binding(node_id)
        except KeyError as exc:
            raise StateTransitionError("node has no active NodeAgent binding") from exc
        if binding.boot_id != boot_id:
            raise StateTransitionError("node boot_id changed")
        status = self.node_registry.status(node_id)
        if status not in {
            RuntimeNodeStatus.HEALTHY,
            RuntimeNodeStatus.DRAINING,
            RuntimeNodeStatus.DRAINED,
        }:
            raise StateTransitionError(f"NodeAgent is {status.value}")

    async def _begin_node_drain(self, node_id: str, boot_id: str) -> None:
        binding = self.node_registry.binding(node_id)
        self._node_drain_observation_floor[node_id] = self._node_snapshot(
            node_id
        ).observation_sequence
        self.node_registry.set_status(
            node_id,
            RuntimeNodeStatus.DRAINING,
            boot_id=boot_id,
            agent_generation=binding.agent_generation,
        )
        self._node_drain_states[node_id] = (boot_id, NodeStatus.DRAINING)
        await super()._begin_node_drain(node_id, boot_id)
        if isinstance(self.worker_broker, StandbyWorkerBroker):
            await self.worker_broker.advance_node_drain(node_id, boot_id)

    async def _advance_node_drain(self, node_id: str, boot_id: str) -> None:
        await super()._advance_node_drain(node_id, boot_id)
        if isinstance(self.worker_broker, StandbyWorkerBroker):
            await self.worker_broker.advance_node_drain(node_id, boot_id)

    async def _complete_node_drain(self, node_id: str, boot_id: str) -> None:
        binding = self.node_registry.binding(node_id)
        if binding.boot_id != boot_id:
            raise StateTransitionError("node boot_id changed during drain")
        if self.node_registry.status(node_id) not in {
            RuntimeNodeStatus.DRAINING,
            RuntimeNodeStatus.DRAINED,
        }:
            raise StateTransitionError("NodeAgent disconnected during drain")
        if (
            not self.node_registry.set_status(
                node_id,
                RuntimeNodeStatus.DRAINED,
                boot_id=boot_id,
                agent_generation=binding.agent_generation,
            )
            and self.node_registry.status(node_id) is not RuntimeNodeStatus.DRAINED
        ):
            raise StateTransitionError("NodeAgent could not enter drained state")
        self._node_drain_states[node_id] = (boot_id, NodeStatus.DRAINED)

    async def _prepare_node_resume(self, node_id: str, boot_id: str) -> None:
        await super()._prepare_node_resume(node_id, boot_id)
        binding = self.node_registry.binding(node_id)
        if binding.boot_id != boot_id:
            raise StateTransitionError("NodeAgent boot generation changed")
        if self.node_registry.status(node_id) is not RuntimeNodeStatus.DRAINED:
            raise StateTransitionError("NodeAgent is not connected in drained state")
        node = self._node_snapshot(node_id)
        if (
            node.capacity.capabilities.get("environment_fingerprint")
            != self.environment_fingerprint
        ):
            raise StateTransitionError("NodeAgent environment fingerprint changed")
        if node.capacity.npus and node.observation_sequence < 1:
            raise StateTransitionError(
                "current physical NPU observation is unavailable"
            )
        inventory = self._recovery_inventory.get(node_id)
        if inventory is None:
            raise StateTransitionError("current NodeAgent inventory is unavailable")
        if inventory.active_lease_ids or inventory.service_handle_ids:
            raise StateTransitionError("NodeAgent still reports active resources")
        if not self.node_registry.set_status(
            node_id,
            RuntimeNodeStatus.HEALTHY,
            boot_id=boot_id,
            agent_generation=binding.agent_generation,
        ):
            raise StateTransitionError("NodeAgent could not resume")
        self._node_drain_states.pop(node_id, None)
        self._node_drain_observation_floor.pop(node_id, None)

    def _collect_node_incomplete_resources(
        self, node_id: str, boot_id: str
    ) -> tuple[ShutdownResource, ...]:
        resources = list(super()._collect_node_incomplete_resources(node_id, boot_id))
        active_workers = self.worker_broker.active_count(node_id)
        live_workers = (
            self.worker_broker.live_count(node_id, boot_id)
            if isinstance(self.worker_broker, StandbyWorkerBroker)
            else active_workers
        )
        if active_workers or live_workers:
            resources.append(
                ShutdownResource(
                    kind="worker_pool",
                    resource_id=f"{node_id}:{boot_id}",
                    state="active",
                    node_id=node_id,
                    details={
                        "active_worker_leases": active_workers,
                        "live_worker_processes": live_workers,
                    },
                )
            )
        for dispatch_id in self.ray_runtime.active_dispatch_ids_for_node(
            node_id, boot_id
        ):
            resources.append(
                ShutdownResource(
                    kind="runtime_dispatch",
                    resource_id=dispatch_id,
                    state="active",
                    node_id=node_id,
                )
            )
        inventory = self._recovery_inventory.get(node_id)
        if inventory is None:
            resources.append(
                ShutdownResource(
                    kind="node_agent_inventory",
                    resource_id=f"{node_id}:{boot_id}",
                    state="unconfirmed",
                    node_id=node_id,
                )
            )
        else:
            resources.extend(
                ShutdownResource(
                    kind="node_agent_lease",
                    resource_id=lease_id,
                    state="active",
                    node_id=node_id,
                )
                for lease_id in inventory.active_lease_ids
            )
            resources.extend(
                ShutdownResource(
                    kind="service_process",
                    resource_id=handle_id,
                    state="active",
                    node_id=node_id,
                )
                for handle_id in inventory.service_handle_ids
            )
        node = self._node_snapshot(node_id)
        observation_floor = self._node_drain_observation_floor.get(node_id, -1)
        for npu in node.capacity.npus:
            recovered_free_hbm_mb = max(
                0,
                npu.total_hbm_mb
                - npu.system_reserved_hbm_mb
                - self.placement.npu_hbm_headroom_mb,
            )
            if (
                node.observation_sequence > observation_floor
                and npu.healthy
                and npu.observed_free_hbm_mb is not None
                and npu.observed_free_hbm_mb >= recovered_free_hbm_mb
            ):
                continue
            resources.append(
                ShutdownResource(
                    kind="npu_hbm_recovery",
                    resource_id=f"{node_id}:{boot_id}:{npu.device_id}",
                    state="pending",
                    node_id=node_id,
                    details={
                        "device_id": npu.device_id,
                        "healthy": npu.healthy,
                        "observation_sequence": node.observation_sequence,
                        "observed_free_hbm_mb": npu.observed_free_hbm_mb,
                        "required_free_hbm_mb": recovered_free_hbm_mb,
                    },
                )
            )
        return tuple(sorted(resources, key=lambda item: (item.kind, item.resource_id)))

    def get_run_events(
        self,
        run_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> RunEventPage:
        if not isinstance(self.recorder, HistoricalEventReader):
            raise RuntimeError("configured recorder has no historical event reader")
        return self.recorder.get_run_events(run_id, cursor=cursor, limit=limit)

    def _binding_registered(
        self,
        binding: RuntimeNodeBinding,
        previous: RuntimeNodeBinding | None,
    ) -> None:
        del previous
        capacity = self._node_capacities.get(binding.node_id)
        if capacity is None:
            raise ValueError(f"NodeAgent registered unknown node: {binding.node_id}")
        if capacity.boot_id != binding.boot_id:
            capacity = replace(capacity, boot_id=binding.boot_id)
            self._node_capacities[binding.node_id] = capacity
            self.placement.register_node(capacity)
        drain_state = self._node_drain_states.get(binding.node_id)
        if drain_state is not None and drain_state[0] != binding.boot_id:
            self._node_drain_states.pop(binding.node_id, None)
            self._node_drain_observation_floor.pop(binding.node_id, None)
            drain_state = None
        if drain_state is not None:
            placement_status = drain_state[1]
            runtime_status = (
                RuntimeNodeStatus.DRAINED
                if placement_status is NodeStatus.DRAINED
                else RuntimeNodeStatus.DRAINING
            )
            self.node_registry.set_status(
                binding.node_id,
                runtime_status,
                boot_id=binding.boot_id,
                agent_generation=binding.agent_generation,
            )
            self.placement.set_node_status(
                binding.node_id,
                placement_status,
                now_ms=self.clock.monotonic_ms(),
            )
            self.core.post_resource_changed(
                f"node_binding_registered_drained:{binding.node_id}"
            )
            if isinstance(self.worker_broker, StandbyWorkerBroker):
                self.worker_broker.notify_changed()
            return
        if binding.node_id in self._recovery_pending_nodes:
            self.placement.set_node_status(
                binding.node_id,
                NodeStatus.UNSCHEDULABLE,
                now_ms=self.clock.monotonic_ms(),
            )
            self._schedule_node_reconciliation(binding.node_id)
        elif self.node_registry.status(binding.node_id) is RuntimeNodeStatus.HEALTHY:
            self.placement.set_node_status(
                binding.node_id,
                NodeStatus.HEALTHY,
                now_ms=self.clock.monotonic_ms(),
            )
        else:
            self.placement.set_node_status(
                binding.node_id,
                NodeStatus.UNSCHEDULABLE,
                now_ms=self.clock.monotonic_ms(),
            )
        self.core.post_resource_changed(f"node_binding_registered:{binding.node_id}")
        if isinstance(self.worker_broker, StandbyWorkerBroker):
            self.worker_broker.notify_changed()

    def _validate_node_registration(
        self,
        node_id: str,
        capacity: NodeCapacity | None,
    ) -> None:
        if self.lifecycle_state.value != "ready":
            raise ValueError(
                f"Controller is {self.lifecycle_state.value}; node registration is closed"
            )
        if node_id not in self._node_capacities and capacity is None:
            raise ValueError(f"NodeAgent registered unknown node: {node_id}")

    def _register_node_capacity(self, capacity: NodeCapacity) -> None:
        existing = self._node_capacities.get(capacity.node_id)
        if existing is not None:
            if (
                existing.boot_id != capacity.boot_id
                or existing.node_ip != capacity.node_ip
                or existing.cpu_total != capacity.cpu_total
                or existing.mem_total_mb != capacity.mem_total_mb
                or tuple(
                    (item.device_id, item.chip_type, item.total_hbm_mb)
                    for item in existing.npus
                )
                != tuple(
                    (item.device_id, item.chip_type, item.total_hbm_mb)
                    for item in capacity.npus
                )
            ):
                raise ValueError("NodeAgent capacity conflicts with configured node")
            return
        if (
            capacity.capabilities.get("environment_fingerprint")
            != self.environment_fingerprint
        ):
            raise ValueError("NodeAgent capacity environment fingerprint mismatch")
        self._node_capacities[capacity.node_id] = capacity
        self.placement.register_node(capacity, status=NodeStatus.JOINING)

    def _binding_disconnected(self, binding: RuntimeNodeBinding) -> None:
        if self._controlled_transport_shutdown:
            self.placement.set_node_status(
                binding.node_id,
                NodeStatus.DRAINING,
                now_ms=self.clock.monotonic_ms(),
            )
            return
        self.ray_runtime.invalidate_binding(binding)
        self.placement.set_node_status(
            binding.node_id,
            NodeStatus.OFFLINE,
            now_ms=self.clock.monotonic_ms(),
        )
        self.core.post_resource_changed(f"node_binding_disconnected:{binding.node_id}")
        self.core.post_runtime_binding_invalidated(
            binding.node_id,
            binding.boot_id,
            reason="NodeAgent binding disconnected during Worker startup",
        )
        if isinstance(self.worker_broker, StandbyWorkerBroker):
            self.worker_broker.notify_changed()
        if self.inference is not None:
            self.inference.report_node_generation_lost(
                binding.node_id,
                binding.boot_id,
            )

    def _binding_replaced(self, binding: RuntimeNodeBinding) -> None:
        self.ray_runtime.invalidate_binding(binding)
        self.core.post_runtime_binding_invalidated(
            binding.node_id,
            binding.boot_id,
            reason="NodeAgent generation changed during Worker startup",
        )

    def _service_process_exited(self, event: ServiceProcessExit) -> None:
        if self.inference is None:
            return
        self.inference.report_process_exited(
            event.instance_id,
            event.generation,
            reason=f"service_process_exited:{event.exit_code}",
        )

    def _recovery_inventory_changed(
        self,
        binding: RuntimeNodeBinding,
        inventory: NodeRecoveryInventory,
    ) -> None:
        self._recovery_inventory[binding.node_id] = inventory
        reported = inventory.reported_controller_generation
        old_generation = reported is not None and reported != self.controller_generation
        recovery_active = (
            binding.node_id in self._recovery_pending_nodes or old_generation
        )
        if recovery_active and (
            inventory.active_lease_ids or inventory.service_handle_ids
        ):
            self._recovery_pending_nodes.add(binding.node_id)
            self.placement.set_node_status(
                binding.node_id,
                NodeStatus.UNSCHEDULABLE,
                now_ms=self.clock.monotonic_ms(),
            )
        if recovery_active or binding.node_id in self._recovery_pending_nodes:
            self._schedule_node_reconciliation(binding.node_id)

    def _schedule_node_reconciliation(self, node_id: str) -> None:
        existing = self._recovery_tasks.get(node_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._reconcile_recovery_node(node_id))
        self._recovery_tasks[node_id] = task

    async def _reconcile_recovery_node(self, node_id: str) -> None:
        try:
            if self.inference is not None:
                for instance in self.inference.model_instances():
                    if instance.node_id != node_id:
                        continue
                    await self.inference.instances.stop_if_drained(
                        instance.instance_id,
                        instance.generation,
                    )
            inventory = self._recovery_inventory.get(node_id)
            if inventory is None:
                return
            if inventory.active_lease_ids or inventory.service_handle_ids:
                return
            capacity = self._node_capacities[node_id]
            self.ray_data_store.release_staged_for_node(
                node_id=node_id,
                boot_id=capacity.boot_id,
            )
            self._recovery_pending_nodes.discard(node_id)
            if not self._recovery_pending_nodes:
                self._release_old_code_registry()
            if self.node_registry.status(node_id) is RuntimeNodeStatus.HEALTHY:
                self.placement.set_node_status(
                    node_id,
                    NodeStatus.HEALTHY,
                    now_ms=self.clock.monotonic_ms(),
                )
                self.core.post_resource_changed(
                    f"controller_recovery_complete:{node_id}"
                )
        finally:
            current = self._recovery_tasks.get(node_id)
            if current is asyncio.current_task():
                del self._recovery_tasks[node_id]

    def _release_old_code_registry(self) -> None:
        previous = self._previous_controller_generation
        if self._old_code_registry_released or previous is None:
            return
        self.ray_data_store.release_owner(
            owner_kind="code_registry",
            owner_id=previous,
        )
        self._old_code_registry_released = True

    def _node_observation(self, observation: NodeObservation) -> bool:
        changed = self.placement.update_observation(observation)
        if changed:
            self.core.post_resource_changed(
                f"node_observation:{observation.node_id}:{observation.sequence}"
            )
        return changed

    def _post_pool_resource_changed(self, reason: str) -> None:
        self.core.post_resource_changed(reason)
