"""Ray Object Store adapter backed by one long-lived owner actor."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from threading import RLock
from time import perf_counter
from typing import Any

import ray
from ray.exceptions import RayActorError

from ascend_maze.contracts.data import (
    DataHandle,
    DataOwner,
    SharedFileRef,
    shared_file_metadata,
)
from ascend_maze.core.canonical import FrozenMap, canonical_bytes
from ascend_maze.core.errors import (
    CanonicalizationError,
    DataHandleInvalidError,
    DataOwnershipError,
    DataStoreWriteError,
)
from ascend_maze.core.identifiers import new_id


def _elapsed_ms(started: float) -> float:
    return max(0.0, (perf_counter() - started) * 1_000)


class RayDataStoreOwnerUnavailableError(DataHandleInvalidError):
    """The descriptor is valid, but its detached owner actor no longer exists."""


@dataclass(frozen=True, slots=True)
class RayDataStoreDescriptor:
    owner_actor_name: str
    owner_namespace: str
    owner_generation: str

    def __post_init__(self) -> None:
        for name in ("owner_actor_name", "owner_namespace", "owner_generation"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} is required")


@dataclass(slots=True)
class _OwnerEntry:
    handle: DataHandle
    object_ref: ray.ObjectRef
    state: str
    owner: DataOwner | None


@dataclass(frozen=True, slots=True)
class _PutMetrics:
    source: str
    canonicalize_ms: float
    ray_put_ms: float
    canonicalize_count: int
    value_size_bytes: int | None


class _DataStoreOwner:
    def __init__(self, owner_generation: str) -> None:
        self.owner_generation = owner_generation
        self._entries: dict[str, _OwnerEntry] = {}
        self._tombstones: set[str] = set()
        self._stage_count = 0
        self._resolve_count = 0
        self._resolve_batch_count = 0
        self._fail_stage_numbers: set[int] = set()
        self._put_metrics: dict[str, int | float] = {
            "canonicalize_ms": 0.0,
            "ray_put_ms": 0.0,
            "owner_stage_ms": 0.0,
            "canonicalize_count": 0,
            "value_size_bytes": 0,
            "value_size_known_count": 0,
            "value_size_unknown_count": 0,
        }
        for source in ("submission_input", "runtime_output", "code_package"):
            self._put_metrics.update(
                {
                    f"{source}_put_count": 0,
                    f"{source}_canonicalize_ms": 0.0,
                    f"{source}_ray_put_ms": 0.0,
                    f"{source}_owner_stage_ms": 0.0,
                    f"{source}_canonicalize_count": 0,
                    f"{source}_value_size_bytes": 0,
                    f"{source}_value_size_known_count": 0,
                    f"{source}_value_size_unknown_count": 0,
                }
            )

    def stage(
        self,
        handle: DataHandle,
        boxed_ref: list[ray.ObjectRef],
        put_metrics: _PutMetrics,
    ) -> None:
        stage_started = perf_counter()
        self._stage_count += 1
        if self._stage_count in self._fail_stage_numbers:
            self._fail_stage_numbers.remove(self._stage_count)
            raise RuntimeError(f"injected stage failure at call {self._stage_count}")
        self._validate_handle_generation(handle)
        if len(boxed_ref) != 1 or not isinstance(boxed_ref[0], ray.ObjectRef):
            raise TypeError("stage requires one nested ObjectRef")
        existing = self._entries.get(handle.staged_handle_id)
        if existing is not None:
            if (
                existing.handle != handle
                or existing.object_ref.hex() != boxed_ref[0].hex()
            ):
                raise RuntimeError("staged_handle_id payload conflict")
        else:
            self._entries[handle.staged_handle_id] = _OwnerEntry(
                handle=handle,
                object_ref=boxed_ref[0],
                state="staged",
                owner=None,
            )
            self._tombstones.discard(handle.staged_handle_id)
        self._record_put_metrics(
            put_metrics,
            owner_stage_ms=_elapsed_ms(stage_started),
        )

    def resolve(self, handle: DataHandle) -> ray.ObjectRef:
        self._resolve_count += 1
        return self._require_entry(handle).object_ref

    def resolve_many(
        self,
        handles: tuple[DataHandle, ...],
    ) -> tuple[ray.ObjectRef, ...]:
        self._resolve_batch_count += 1
        self._resolve_count += len(handles)
        return tuple(self._require_entry(handle).object_ref for handle in handles)

    def state_of(self, handle: DataHandle) -> str:
        entry = self._entries.get(handle.staged_handle_id)
        if entry is None:
            if handle.staged_handle_id in self._tombstones:
                return "released"
            raise RuntimeError("data handle is unknown")
        if entry.handle != handle:
            raise RuntimeError("data handle metadata does not match")
        return entry.state

    def owner_of(self, handle: DataHandle) -> DataOwner | None:
        return self._require_entry(handle).owner

    def adopt(self, handles: tuple[DataHandle, ...], owner: DataOwner) -> None:
        if owner.owner_generation != self.owner_generation:
            raise RuntimeError("owner generation does not match DataStoreOwner")
        entries: list[_OwnerEntry] = []
        seen: set[str] = set()
        for handle in handles:
            if handle.staged_handle_id in seen:
                raise RuntimeError("adopt contains a duplicate handle")
            seen.add(handle.staged_handle_id)
            entry = self._require_entry(handle)
            if entry.state == "adopted" and entry.owner != owner:
                raise RuntimeError("handle is already adopted by another owner")
            entries.append(entry)
        for entry in entries:
            entry.state = "adopted"
            entry.owner = owner

    def release(self, handle: DataHandle) -> bool:
        self._validate_handle_generation(handle)
        entry = self._entries.get(handle.staged_handle_id)
        if entry is None:
            return False
        if entry.handle != handle:
            raise RuntimeError("data handle metadata does not match")
        del self._entries[handle.staged_handle_id]
        self._tombstones.add(handle.staged_handle_id)
        del entry.object_ref
        return True

    def release_staged_for_runtime_node(
        self,
        node_id: str,
        boot_id: str,
        runtime_generation: int,
    ) -> int:
        handles = tuple(
            entry.handle
            for entry in self._entries.values()
            if entry.state == "staged"
            and entry.handle.metadata.get("source_node_id") == node_id
            and entry.handle.metadata.get("source_boot_id") == boot_id
            and entry.handle.metadata.get("source_runtime_generation")
            == runtime_generation
        )
        for handle in handles:
            self.release(handle)
        return len(handles)

    def release_staged_for_node(self, node_id: str, boot_id: str) -> int:
        handles = tuple(
            entry.handle
            for entry in self._entries.values()
            if entry.state == "staged"
            and entry.handle.metadata.get("source_node_id") == node_id
            and entry.handle.metadata.get("source_boot_id") == boot_id
        )
        for handle in handles:
            self.release(handle)
        return len(handles)

    def release_owner(self, owner_kind: str, owner_id: str) -> int:
        handles = tuple(
            entry.handle
            for entry in self._entries.values()
            if entry.owner is not None
            and entry.owner.owner_kind == owner_kind
            and entry.owner.owner_id == owner_id
        )
        for handle in handles:
            self.release(handle)
        return len(handles)

    def stats(self) -> dict[str, int | float | str]:
        return {
            "owner_generation": self.owner_generation,
            "active_count": len(self._entries),
            "staged_count": sum(
                entry.state == "staged" for entry in self._entries.values()
            ),
            "adopted_count": sum(
                entry.state == "adopted" for entry in self._entries.values()
            ),
            "tombstone_count": len(self._tombstones),
            "stage_count": self._stage_count,
            "resolve_count": self._resolve_count,
            "resolve_batch_count": self._resolve_batch_count,
            **self._put_metrics,
        }

    def _record_put_metrics(
        self,
        metrics: _PutMetrics,
        *,
        owner_stage_ms: float,
    ) -> None:
        if metrics.source not in {
            "submission_input",
            "runtime_output",
            "code_package",
        }:
            raise RuntimeError("RayDataStore put metrics source is invalid")
        prefix = metrics.source
        for key, value in (
            ("canonicalize_ms", metrics.canonicalize_ms),
            ("ray_put_ms", metrics.ray_put_ms),
            ("owner_stage_ms", owner_stage_ms),
            ("canonicalize_count", metrics.canonicalize_count),
        ):
            self._put_metrics[key] += value
            self._put_metrics[f"{prefix}_{key}"] += value
        self._put_metrics[f"{prefix}_put_count"] += 1
        if metrics.value_size_bytes is None:
            self._put_metrics["value_size_unknown_count"] += 1
            self._put_metrics[f"{prefix}_value_size_unknown_count"] += 1
            return
        self._put_metrics["value_size_bytes"] += metrics.value_size_bytes
        self._put_metrics[f"{prefix}_value_size_bytes"] += metrics.value_size_bytes
        self._put_metrics["value_size_known_count"] += 1
        self._put_metrics[f"{prefix}_value_size_known_count"] += 1

    def fail_on_stage_number(self, stage_number: int) -> None:
        if stage_number < 1:
            raise ValueError("stage_number must be positive")
        self._fail_stage_numbers.add(stage_number)

    def _require_entry(self, handle: DataHandle) -> _OwnerEntry:
        self._validate_handle_generation(handle)
        try:
            entry = self._entries[handle.staged_handle_id]
        except KeyError as exc:
            state = (
                "released" if handle.staged_handle_id in self._tombstones else "unknown"
            )
            raise RuntimeError(f"data handle is {state}") from exc
        if entry.handle != handle:
            raise RuntimeError("data handle metadata does not match")
        return entry

    def _validate_handle_generation(self, handle: DataHandle) -> None:
        if handle.owner_generation != self.owner_generation:
            raise RuntimeError("data handle owner generation mismatch")


_DATA_STORE_OWNER_ACTOR: Any = ray.remote(
    num_cpus=0,
    max_restarts=0,
    max_task_retries=0,
)(_DataStoreOwner)


class RayDataStore:
    """Synchronous DataStore facade; Scheduler invokes it outside its event loop."""

    def __init__(
        self,
        descriptor: RayDataStoreDescriptor,
        owner_actor: ray.actor.ActorHandle,
        *,
        shutdown_ray_on_close: bool = False,
    ) -> None:
        self.descriptor = descriptor
        self._owner_actor = owner_actor
        self._local_get_count = 0
        self._local_lock = RLock()
        self._shutdown_ray_on_close = shutdown_ray_on_close

    @classmethod
    def start(
        cls,
        *,
        owner_generation: str,
        namespace: str,
        actor_name: str | None = None,
    ) -> "RayDataStore":
        name = actor_name or new_id("data_store_owner")
        actor = _DATA_STORE_OWNER_ACTOR.options(
            name=name,
            namespace=namespace,
            lifetime="detached",
        ).remote(owner_generation)
        descriptor = RayDataStoreDescriptor(name, namespace, owner_generation)
        ray.get(actor.stats.remote())
        return cls(descriptor, actor)

    @classmethod
    def connect(cls, descriptor: RayDataStoreDescriptor) -> "RayDataStore":
        try:
            actor = ray.get_actor(
                descriptor.owner_actor_name,
                namespace=descriptor.owner_namespace,
            )
        except ValueError as exc:
            raise RayDataStoreOwnerUnavailableError(
                "DataStoreOwner actor is unavailable: "
                f"{descriptor.owner_namespace}/{descriptor.owner_actor_name}"
            ) from exc
        try:
            stats = ray.get(actor.stats.remote())
        except RayActorError as exc:
            raise RayDataStoreOwnerUnavailableError(
                "DataStoreOwner actor exited before it could be connected: "
                f"{descriptor.owner_namespace}/{descriptor.owner_actor_name}"
            ) from exc
        if stats["owner_generation"] != descriptor.owner_generation:
            raise DataHandleInvalidError("DataStoreOwner generation changed")
        return cls(descriptor, actor)

    @classmethod
    def connect_client(cls, descriptor: RayDataStoreDescriptor) -> "RayDataStore":
        """Join the managed Head cluster before resolving its detached owner."""

        initialized_here = not ray.is_initialized()
        if initialized_here:
            ray.init(address="auto", namespace=descriptor.owner_namespace)
        store = cls.connect(descriptor)
        store._shutdown_ray_on_close = initialized_here
        return store

    def put_staged(self, value: Any, owner_generation: str) -> DataHandle:
        return self._put_staged(
            value,
            owner_generation,
            FrozenMap((("backend", "ray"),)),
            source="submission_input",
            require_stable_digest=True,
        )

    def put_staged_for_submission_input(
        self,
        value: Any,
        owner_generation: str,
    ) -> DataHandle:
        return self._put_staged(
            value,
            owner_generation,
            FrozenMap((("backend", "ray"),)),
            source="submission_input",
            require_stable_digest=False,
        )

    def put_staged_for_code_package(
        self,
        value: Any,
        owner_generation: str,
    ) -> DataHandle:
        return self._put_staged(
            value,
            owner_generation,
            FrozenMap((("backend", "ray"),)),
            source="code_package",
            require_stable_digest=False,
        )

    def put_staged_for_runtime_node(
        self,
        value: Any,
        owner_generation: str,
        *,
        node_id: str,
        boot_id: str,
        runtime_generation: int,
    ) -> DataHandle:
        if not node_id or not boot_id:
            raise DataStoreWriteError("runtime node and boot identity are required")
        if (
            isinstance(runtime_generation, bool)
            or not isinstance(runtime_generation, int)
            or runtime_generation < 1
        ):
            raise DataStoreWriteError("runtime generation must be positive")
        return self._put_staged(
            value,
            owner_generation,
            FrozenMap(
                (
                    ("backend", "ray"),
                    ("source_boot_id", boot_id),
                    ("source_node_id", node_id),
                    ("source_runtime_generation", runtime_generation),
                )
            ),
            source="runtime_output",
            require_stable_digest=False,
        )

    def _put_staged(
        self,
        value: Any,
        owner_generation: str,
        metadata: FrozenMap[Any, Any],
        *,
        source: str,
        require_stable_digest: bool,
    ) -> DataHandle:
        if owner_generation != self.descriptor.owner_generation:
            raise DataStoreWriteError("owner generation does not match DataStoreOwner")
        stable_digest: str | None = None
        size_bytes: int | None = None
        canonicalize_count = 0
        canonicalize_ms = 0.0
        if isinstance(value, SharedFileRef):
            metadata = FrozenMap(
                (*metadata.items_tuple(), *shared_file_metadata(value))
            )
            size_bytes = value.size_bytes
        elif require_stable_digest:
            canonicalize_count = 1
            canonicalize_started = perf_counter()
            try:
                payload = canonical_bytes(value)
                canonicalize_ms = _elapsed_ms(canonicalize_started)
                stable_digest = hashlib.sha256(payload).hexdigest()
                size_bytes = len(payload)
            except CanonicalizationError:
                canonicalize_ms = _elapsed_ms(canonicalize_started)
        try:
            ray_put_started = perf_counter()
            object_ref = ray.put(value, _owner=self._owner_actor)
            ray_put_ms = _elapsed_ms(ray_put_started)
            handle = DataHandle(
                owner_generation=owner_generation,
                staged_handle_id=new_id("data"),
                stable_digest=stable_digest,
                size_bytes=size_bytes,
                metadata=FrozenMap(
                    (
                        *metadata.items_tuple(),
                        ("ray_object_ref_id", object_ref.hex()),
                    )
                ),
            )
            ray.get(
                self._owner_actor.stage.remote(
                    handle,
                    [object_ref],
                    _PutMetrics(
                        source=source,
                        canonicalize_ms=canonicalize_ms,
                        ray_put_ms=ray_put_ms,
                        canonicalize_count=canonicalize_count,
                        value_size_bytes=size_bytes,
                    ),
                )
            )
        except Exception as exc:
            raise DataStoreWriteError(f"Ray put_staged failed: {exc}") from exc
        return handle

    def resolve_ref(self, handle: DataHandle) -> ray.ObjectRef:
        """Resolve one logical handle without materializing its payload."""

        return self.resolve_refs((handle,))[0]

    def resolve_refs(
        self,
        handles: tuple[DataHandle, ...],
    ) -> tuple[ray.ObjectRef, ...]:
        """Resolve logical handles to owner-backed ObjectRefs in one control RPC."""

        if not isinstance(handles, tuple):
            raise TypeError("resolve_refs handles must be a tuple")
        for handle in handles:
            if not isinstance(handle, DataHandle):
                raise TypeError("resolve_refs requires DataHandle values")
            self._validate_generation(handle)
        if not handles:
            return ()
        try:
            refs = ray.get(self._owner_actor.resolve_many.remote(handles))
            if not isinstance(refs, tuple) or len(refs) != len(handles):
                raise TypeError("DataStoreOwner returned an invalid ObjectRef batch")
            for handle, object_ref in zip(handles, refs, strict=True):
                if not isinstance(object_ref, ray.ObjectRef):
                    raise TypeError("DataStoreOwner returned a non-ObjectRef value")
                expected_id = handle.metadata.get("ray_object_ref_id")
                if expected_id is not None and object_ref.hex() != expected_id:
                    raise RuntimeError("DataHandle ObjectRef identity changed")
            return refs
        except Exception as exc:
            raise DataHandleInvalidError(
                f"Ray data handle cannot be resolved: {exc}"
            ) from exc

    def get(self, handle: DataHandle) -> Any:
        with self._local_lock:
            self._local_get_count += 1
        try:
            return ray.get(self.resolve_ref(handle))
        except Exception as exc:
            raise DataHandleInvalidError(
                f"Ray data handle cannot be read: {exc}"
            ) from exc

    def adopt(self, handles: tuple[DataHandle, ...], owner: DataOwner) -> None:
        if not isinstance(handles, tuple):
            raise DataOwnershipError("adopt handles must be a tuple")
        try:
            ray.get(self._owner_actor.adopt.remote(handles, owner))
        except Exception as exc:
            raise DataOwnershipError(f"Ray adopt failed: {exc}") from exc

    def release(self, handle: DataHandle) -> None:
        self._validate_generation(handle)
        try:
            ray.get(self._owner_actor.release.remote(handle))
        except Exception as exc:
            raise DataHandleInvalidError(f"Ray release failed: {exc}") from exc

    def release_many(self, handles: tuple[DataHandle, ...]) -> None:
        for handle in handles:
            self.release(handle)

    def state_of(self, handle: DataHandle) -> str:
        self._validate_generation(handle)
        try:
            return str(ray.get(self._owner_actor.state_of.remote(handle)))
        except Exception as exc:
            raise DataHandleInvalidError(
                f"Ray data handle state is unavailable: {exc}"
            ) from exc

    def owner_of(self, handle: DataHandle) -> DataOwner | None:
        self._validate_generation(handle)
        try:
            value = ray.get(self._owner_actor.owner_of.remote(handle))
        except Exception as exc:
            raise DataHandleInvalidError(
                f"Ray data owner is unavailable: {exc}"
            ) from exc
        return value if isinstance(value, DataOwner) else None

    def stats(self) -> dict[str, int | float | str]:
        return dict(ray.get(self._owner_actor.stats.remote()))

    def metrics_snapshot(self) -> dict[str, int | float | str]:
        return self.stats()

    @property
    def active_count(self) -> int:
        return int(self.stats()["active_count"])

    @property
    def staged_count(self) -> int:
        return int(self.stats()["staged_count"])

    @property
    def adopted_count(self) -> int:
        return int(self.stats()["adopted_count"])

    @property
    def local_get_count(self) -> int:
        with self._local_lock:
            return self._local_get_count

    @property
    def put_count(self) -> int:
        return int(self.stats()["stage_count"])

    def fail_on_put_number(self, put_number: int) -> None:
        if (
            isinstance(put_number, bool)
            or not isinstance(put_number, int)
            or put_number < 1
        ):
            raise ValueError("put_number must be a positive integer")
        try:
            ray.get(self._owner_actor.fail_on_stage_number.remote(put_number))
        except Exception as exc:
            raise DataStoreWriteError(
                f"failed to inject Ray put failure: {exc}"
            ) from exc

    def release_staged_for_runtime_node(
        self,
        *,
        node_id: str,
        boot_id: str,
        runtime_generation: int,
    ) -> int:
        try:
            return int(
                ray.get(
                    self._owner_actor.release_staged_for_runtime_node.remote(
                        node_id, boot_id, runtime_generation
                    )
                )
            )
        except Exception as exc:
            raise DataHandleInvalidError(
                f"failed to release stale runtime handles: {exc}"
            ) from exc

    def release_staged_for_node(self, *, node_id: str, boot_id: str) -> int:
        try:
            return int(
                ray.get(
                    self._owner_actor.release_staged_for_node.remote(node_id, boot_id)
                )
            )
        except Exception as exc:
            raise DataHandleInvalidError(
                f"failed to release recovered runtime handles: {exc}"
            ) from exc

    def release_owner(self, *, owner_kind: str, owner_id: str) -> int:
        try:
            return int(
                ray.get(self._owner_actor.release_owner.remote(owner_kind, owner_id))
            )
        except Exception as exc:
            raise DataHandleInvalidError(
                f"failed to release recovered data owner: {exc}"
            ) from exc

    def close(self, *, kill_owner: bool) -> None:
        if kill_owner:
            ray.kill(self._owner_actor, no_restart=True)
        if self._shutdown_ray_on_close:
            self._shutdown_ray_on_close = False
            if ray.is_initialized():
                ray.shutdown()

    def _validate_generation(self, handle: DataHandle) -> None:
        if handle.owner_generation != self.descriptor.owner_generation:
            raise DataHandleInvalidError("data handle owner generation mismatch")
