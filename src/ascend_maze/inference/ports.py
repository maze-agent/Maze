"""Generation-exact in-memory PortLease authority for the fake service backend."""

from __future__ import annotations

from threading import RLock

from ascend_maze.core.errors import ContractValidationError, StateTransitionError
from ascend_maze.core.identifiers import new_id
from ascend_maze.inference.contracts import PortLease


class InMemoryPortLeaseManager:
    def __init__(self, *, first_port: int = 25_000, last_port: int = 65_535) -> None:
        if (
            isinstance(first_port, bool)
            or isinstance(last_port, bool)
            or not isinstance(first_port, int)
            or not isinstance(last_port, int)
            or first_port < 1
            or last_port > 65_535
            or first_port > last_port
        ):
            raise ContractValidationError("port range must be within 1..65535")
        self.first_port = first_port
        self.last_port = last_port
        self._next_by_node: dict[tuple[str, str], int] = {}
        self._leases_by_key: dict[tuple[str, str, int], PortLease] = {}
        self._leases_by_owner: dict[tuple[str, int], PortLease] = {}
        self._lock = RLock()

    async def acquire(
        self,
        *,
        node_id: str,
        boot_id: str,
        owner_instance_id: str,
        generation: int,
    ) -> PortLease:
        if not node_id or not boot_id or not owner_instance_id or generation < 1:
            raise ContractValidationError("PortLease owner identity is invalid")
        owner_key = (owner_instance_id, generation)
        node_key = (node_id, boot_id)
        with self._lock:
            existing = self._leases_by_owner.get(owner_key)
            if existing is not None:
                if (existing.node_id, existing.boot_id) != node_key:
                    raise StateTransitionError("PortLease owner moved to another node")
                return existing
            start = self._next_by_node.get(node_key, self.first_port)
            capacity = self.last_port - self.first_port + 1
            for offset in range(capacity):
                port = self.first_port + (
                    (start - self.first_port + offset) % capacity
                )
                key = (node_id, boot_id, port)
                if key in self._leases_by_key:
                    continue
                lease = PortLease(
                    port_lease_id=new_id("port"),
                    node_id=node_id,
                    boot_id=boot_id,
                    port=port,
                    owner_instance_id=owner_instance_id,
                    generation=generation,
                )
                self._leases_by_key[key] = lease
                self._leases_by_owner[owner_key] = lease
                self._next_by_node[node_key] = (
                    self.first_port if port == self.last_port else port + 1
                )
                return lease
        raise RuntimeError(f"no service port is available on {node_id}/{boot_id}")

    async def release(self, lease: PortLease) -> bool:
        key = (lease.node_id, lease.boot_id, lease.port)
        owner_key = (lease.owner_instance_id, lease.generation)
        with self._lock:
            current = self._leases_by_key.get(key)
            if current is None:
                return False
            if current != lease or self._leases_by_owner.get(owner_key) != lease:
                raise StateTransitionError("PortLease release identity is stale")
            del self._leases_by_key[key]
            del self._leases_by_owner[owner_key]
            return True

    def active_count(self) -> int:
        with self._lock:
            return len(self._leases_by_key)

    def leases(self) -> tuple[PortLease, ...]:
        with self._lock:
            return tuple(
                self._leases_by_key[key] for key in sorted(self._leases_by_key)
            )
