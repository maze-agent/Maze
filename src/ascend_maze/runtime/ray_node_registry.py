"""Versioned Maze node identity to Ray node binding registry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock

from ascend_maze.contracts.resources import PlacementLease
from ascend_maze.contracts.runtime import RuntimeDeviceMapping, RuntimeNodeBinding
from ascend_maze.core.errors import StateTransitionError


class RuntimeNodeStatus(str, Enum):
    HEALTHY = "healthy"
    STALE = "stale"
    DRAINING = "draining"
    DRAINED = "drained"
    OFFLINE = "offline"
    UNSCHEDULABLE = "unschedulable"


@dataclass(slots=True)
class _BindingRecord:
    binding: RuntimeNodeBinding
    status: RuntimeNodeStatus
    heartbeat_sequence: int


class RayNodeRegistry:
    def __init__(self) -> None:
        self._records: dict[str, _BindingRecord] = {}
        self._generation_by_node: dict[str, int] = {}
        self._lock = RLock()

    def register(
        self,
        *,
        node_id: str,
        boot_id: str,
        ray_node_id: str,
        agent_generation: str,
        agent_endpoint: str,
        producer_id: str,
        records_locally: bool = False,
        device_mappings: tuple[RuntimeDeviceMapping, ...] = (),
        status: RuntimeNodeStatus = RuntimeNodeStatus.HEALTHY,
    ) -> tuple[RuntimeNodeBinding, RuntimeNodeBinding | None]:
        with self._lock:
            previous = self._records.get(node_id)
            if previous is not None and (
                previous.binding.boot_id == boot_id
                and previous.binding.ray_node_id == ray_node_id
                and previous.binding.agent_generation == agent_generation
                and previous.binding.device_mappings
                == tuple(
                    sorted(
                        device_mappings,
                        key=lambda item: item.physical_device_id,
                    )
                )
            ):
                if not (
                    previous.status
                    in {RuntimeNodeStatus.DRAINING, RuntimeNodeStatus.DRAINED}
                    and status is RuntimeNodeStatus.HEALTHY
                ):
                    previous.status = status
                return previous.binding, None
            generation = self._generation_by_node.get(node_id, 0) + 1
            binding = RuntimeNodeBinding(
                node_id=node_id,
                boot_id=boot_id,
                ray_node_id=ray_node_id,
                runtime_generation=generation,
                agent_generation=agent_generation,
                agent_endpoint=agent_endpoint,
                producer_id=producer_id,
                records_locally=records_locally,
                device_mappings=device_mappings,
            )
            self._records[node_id] = _BindingRecord(
                binding=binding,
                status=status,
                heartbeat_sequence=0,
            )
            self._generation_by_node[node_id] = generation
            return binding, None if previous is None else previous.binding

    def heartbeat(
        self,
        *,
        node_id: str,
        boot_id: str,
        agent_generation: str,
        sequence: int,
    ) -> bool:
        return self.accept_message(
            node_id=node_id,
            boot_id=boot_id,
            agent_generation=agent_generation,
            sequence=sequence,
        )

    def accept_message(
        self,
        *,
        node_id: str,
        boot_id: str,
        agent_generation: str,
        sequence: int,
    ) -> bool:
        with self._lock:
            record = self._records.get(node_id)
            if record is None:
                return False
            binding = record.binding
            if (
                binding.boot_id != boot_id
                or binding.agent_generation != agent_generation
                or sequence <= record.heartbeat_sequence
            ):
                return False
            record.heartbeat_sequence = sequence
            if record.status is RuntimeNodeStatus.STALE:
                record.status = RuntimeNodeStatus.HEALTHY
            return True

    def set_status(
        self,
        node_id: str,
        status: RuntimeNodeStatus,
        *,
        boot_id: str | None = None,
        agent_generation: str | None = None,
    ) -> bool:
        with self._lock:
            record = self._records.get(node_id)
            if record is None or (
                boot_id is not None and record.binding.boot_id != boot_id
            ) or (
                agent_generation is not None
                and record.binding.agent_generation != agent_generation
            ):
                return False
            if record.status is status:
                return False
            record.status = status
            return True

    def resolve_lease(self, lease: PlacementLease) -> RuntimeNodeBinding:
        with self._lock:
            record = self._records.get(lease.node_id)
            if record is None:
                raise StateTransitionError("PlacementLease node is not registered")
            if record.binding.boot_id != lease.boot_id:
                raise StateTransitionError("PlacementLease boot generation is stale")
            if record.status is not RuntimeNodeStatus.HEALTHY:
                raise StateTransitionError(
                    f"PlacementLease node is {record.status.value}"
                )
            return record.binding

    def binding(self, node_id: str) -> RuntimeNodeBinding:
        with self._lock:
            try:
                return self._records[node_id].binding
            except KeyError as exc:
                raise KeyError(f"unknown runtime node: {node_id}") from exc

    def status(self, node_id: str) -> RuntimeNodeStatus:
        with self._lock:
            return self._records[node_id].status

    def producer_for_lease(self, lease: PlacementLease) -> str:
        return self.resolve_lease(lease).producer_id

    def active_bindings(self) -> tuple[RuntimeNodeBinding, ...]:
        with self._lock:
            return tuple(
                self._records[node_id].binding
                for node_id in sorted(self._records)
                if self._records[node_id].status is RuntimeNodeStatus.HEALTHY
            )
