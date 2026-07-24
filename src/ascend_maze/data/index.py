"""Generation-aware run data index and idempotent tombstone destruction."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Condition, RLock
from typing import Mapping

from ascend_maze.contracts.data import DataHandle, DataOwner, DataStore
from ascend_maze.core.errors import RunDataIndexError


class RunDataState(str, Enum):
    ACTIVE = "active"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"


@dataclass(frozen=True, slots=True)
class RunDataIndexRef:
    run_id: str
    controller_generation: str
    index_generation: int


@dataclass(frozen=True, slots=True)
class RunDataTombstone:
    run_id: str
    controller_generation: str
    index_generation: int
    released_handle_count: int
    destroy_succeeded: bool
    completed_at_ms: int
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunDataIndexCheckpoint:
    reference: RunDataIndexRef
    state: RunDataState
    workflow_inputs: tuple[tuple[str, DataHandle], ...]
    task_outputs: tuple[tuple[str, str, DataHandle], ...]
    tombstone: RunDataTombstone | None

    def __post_init__(self) -> None:
        if self.state is RunDataState.DESTROYED and self.tombstone is None:
            raise RunDataIndexError("destroyed checkpoint requires a tombstone")
        if self.state is not RunDataState.DESTROYED and self.tombstone is not None:
            raise RunDataIndexError("active checkpoint cannot contain a tombstone")


class DagContext:
    """Internal Run-scoped mapping from logical bindings to DataStore handles.

    User Task callables never receive this object. The scheduler resolves their
    declared arguments through it and publishes staged outputs only after every
    declared output has been stored successfully.
    """

    def __init__(
        self,
        *,
        reference: RunDataIndexRef,
        data_store: DataStore,
        data_owner_generation: str,
        workflow_inputs: Mapping[str, DataHandle],
    ) -> None:
        if not data_owner_generation:
            raise ValueError("data_owner_generation is required")
        self.reference = reference
        self._data_store = data_store
        self._data_owner_generation = data_owner_generation
        self._state = RunDataState.ACTIVE
        self._workflow_inputs = dict(workflow_inputs)
        self._task_outputs: dict[tuple[str, str], DataHandle] = {}
        self._tombstone: RunDataTombstone | None = None
        self._lock = RLock()
        self._condition = Condition(self._lock)

    @property
    def state(self) -> RunDataState:
        with self._lock:
            return self._state

    @property
    def tombstone(self) -> RunDataTombstone | None:
        with self._lock:
            return self._tombstone

    def _validate_generation(
        self,
        controller_generation: str,
        index_generation: int,
    ) -> None:
        if (
            controller_generation != self.reference.controller_generation
            or index_generation != self.reference.index_generation
        ):
            raise RunDataIndexError("stale controller or index generation")

    def _require_active(self) -> None:
        if self._state is not RunDataState.ACTIVE:
            raise RunDataIndexError(f"run data index is {self._state.value}")

    def workflow_input_handle(
        self,
        name: str,
        *,
        controller_generation: str,
        index_generation: int,
    ) -> DataHandle:
        with self._lock:
            self._validate_generation(controller_generation, index_generation)
            self._require_active()
            try:
                return self._workflow_inputs[name]
            except KeyError as exc:
                raise RunDataIndexError(f"unknown workflow input: {name}") from exc

    def task_output_handle(
        self,
        task_id: str,
        output_name: str,
        *,
        controller_generation: str,
        index_generation: int,
    ) -> DataHandle:
        with self._lock:
            self._validate_generation(controller_generation, index_generation)
            self._require_active()
            try:
                return self._task_outputs[(task_id, output_name)]
            except KeyError as exc:
                raise RunDataIndexError(
                    f"unpublished task output: {task_id}.{output_name}"
                ) from exc

    def read_workflow_input(
        self,
        name: str,
        *,
        controller_generation: str,
        index_generation: int,
    ) -> object:
        with self._lock:
            handle = self.workflow_input_handle(
                name,
                controller_generation=controller_generation,
                index_generation=index_generation,
            )
            return self._data_store.get(handle)

    def read_task_result(
        self,
        task_id: str,
        output_names: tuple[str, ...],
        *,
        controller_generation: str,
        index_generation: int,
    ) -> dict[str, object]:
        with self._lock:
            self._validate_generation(controller_generation, index_generation)
            self._require_active()
            handles: list[tuple[str, DataHandle]] = []
            for output_name in output_names:
                try:
                    handle = self._task_outputs[(task_id, output_name)]
                except KeyError as exc:
                    raise RunDataIndexError(
                        f"unpublished task output: {task_id}.{output_name}"
                    ) from exc
                handles.append((output_name, handle))
            return {
                output_name: self._data_store.get(handle)
                for output_name, handle in handles
            }

    def publish_outputs(
        self,
        *,
        task_id: str,
        output_handles: Mapping[str, DataHandle],
        expected_output_names: tuple[str, ...],
        controller_generation: str,
        index_generation: int,
    ) -> None:
        with self._lock:
            self._validate_generation(controller_generation, index_generation)
            self._require_active()
            if tuple(sorted(output_handles)) != tuple(sorted(expected_output_names)):
                raise RunDataIndexError("output handle names do not match contract")
            keys = tuple((task_id, name) for name in expected_output_names)
            if any(key in self._task_outputs for key in keys):
                raise RunDataIndexError("task outputs are already published")
            handles = tuple(output_handles[name] for name in expected_output_names)
            owner = DataOwner(
                owner_kind="run_index",
                owner_id=f"{self.reference.run_id}:{self.reference.index_generation}",
                owner_generation=self._data_owner_generation,
            )
            self._data_store.adopt(handles, owner)
            for name, handle in zip(expected_output_names, handles, strict=True):
                self._task_outputs[(task_id, name)] = handle

    def matches_published_outputs(
        self,
        *,
        task_id: str,
        output_handles: Mapping[str, DataHandle],
        controller_generation: str,
        index_generation: int,
    ) -> bool:
        with self._lock:
            self._validate_generation(controller_generation, index_generation)
            if self._state is not RunDataState.ACTIVE:
                return False
            published = {
                output_name: handle
                for (published_task_id, output_name), handle in self._task_outputs.items()
                if published_task_id == task_id
            }
            return published == dict(output_handles)

    def handle_count(self) -> int:
        with self._lock:
            return len(self._workflow_inputs) + len(self._task_outputs)

    def checkpoint(self) -> RunDataIndexCheckpoint:
        with self._lock:
            if self._state is RunDataState.DESTROYING:
                raise RunDataIndexError("cannot checkpoint a destroying data index")
            return RunDataIndexCheckpoint(
                reference=self.reference,
                state=self._state,
                workflow_inputs=tuple(sorted(self._workflow_inputs.items())),
                task_outputs=tuple(
                    (task_id, output_name, handle)
                    for (task_id, output_name), handle in sorted(
                        self._task_outputs.items()
                    )
                ),
                tombstone=self._tombstone,
            )

    def destroy(
        self,
        *,
        controller_generation: str,
        index_generation: int,
        completed_at_ms: int,
    ) -> RunDataTombstone:
        with self._condition:
            self._validate_generation(controller_generation, index_generation)
            while self._state is RunDataState.DESTROYING:
                self._condition.wait()
            if self._state is RunDataState.DESTROYED:
                assert self._tombstone is not None
                return self._tombstone
            self._state = RunDataState.DESTROYING
            handles = tuple(self._workflow_inputs.values()) + tuple(
                self._task_outputs.values()
            )
            self._workflow_inputs.clear()
            self._task_outputs.clear()

        errors: list[str] = []
        released = 0
        for handle in handles:
            try:
                self._data_store.release(handle)
                released += 1
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        tombstone = RunDataTombstone(
            run_id=self.reference.run_id,
            controller_generation=self.reference.controller_generation,
            index_generation=self.reference.index_generation,
            released_handle_count=released,
            destroy_succeeded=not errors,
            completed_at_ms=completed_at_ms,
            errors=tuple(errors),
        )
        with self._condition:
            self._tombstone = tombstone
            self._state = RunDataState.DESTROYED
            self._condition.notify_all()
            return tombstone


# Compatibility name retained for existing control-plane callers.
RunDataIndex = DagContext


class RunDataIndexRegistry:
    def __init__(
        self,
        *,
        controller_generation: str,
        data_owner_generation: str | None = None,
        data_store: DataStore,
    ) -> None:
        if not controller_generation:
            raise ValueError("controller_generation is required")
        self.controller_generation = controller_generation
        self.data_owner_generation = data_owner_generation or controller_generation
        self.data_store = data_store
        self._indexes: dict[str, DagContext] = {}
        self._next_generation: dict[str, int] = {}
        self._lock = RLock()

    def create_and_adopt(
        self,
        *,
        run_id: str,
        workflow_inputs: Mapping[str, DataHandle],
    ) -> DagContext:
        with self._lock:
            existing = self._indexes.get(run_id)
            if existing is not None and existing.state is not RunDataState.DESTROYED:
                raise RunDataIndexError(f"active data index already exists: {run_id}")
            generation = self._next_generation.get(run_id, 0) + 1
            reference = RunDataIndexRef(
                run_id=run_id,
                controller_generation=self.controller_generation,
                index_generation=generation,
            )
            handles = tuple(workflow_inputs.values())
            owner = DataOwner(
                owner_kind="run_index",
                owner_id=f"{run_id}:{generation}",
                owner_generation=self.data_owner_generation,
            )
            self.data_store.adopt(handles, owner)
            index = DagContext(
                reference=reference,
                data_store=self.data_store,
                data_owner_generation=self.data_owner_generation,
                workflow_inputs=workflow_inputs,
            )
            self._indexes[run_id] = index
            self._next_generation[run_id] = generation
            return index

    def restore(self, checkpoint: RunDataIndexCheckpoint) -> DagContext:
        """Rebind a persisted index to this Controller without re-adopting data."""

        with self._lock:
            run_id = checkpoint.reference.run_id
            if run_id in self._indexes:
                raise RunDataIndexError(f"run data index already restored: {run_id}")
            generation = max(
                self._next_generation.get(run_id, 0),
                checkpoint.reference.index_generation,
            ) + 1
            reference = RunDataIndexRef(
                run_id=run_id,
                controller_generation=self.controller_generation,
                index_generation=generation,
            )
            index = DagContext(
                reference=reference,
                data_store=self.data_store,
                data_owner_generation=self.data_owner_generation,
                workflow_inputs=dict(checkpoint.workflow_inputs),
            )
            index._task_outputs = {
                (task_id, output_name): handle
                for task_id, output_name, handle in checkpoint.task_outputs
            }
            index._state = checkpoint.state
            if checkpoint.tombstone is not None:
                index._tombstone = RunDataTombstone(
                    run_id=run_id,
                    controller_generation=self.controller_generation,
                    index_generation=generation,
                    released_handle_count=checkpoint.tombstone.released_handle_count,
                    destroy_succeeded=checkpoint.tombstone.destroy_succeeded,
                    completed_at_ms=checkpoint.tombstone.completed_at_ms,
                    errors=checkpoint.tombstone.errors,
                )
            self._indexes[run_id] = index
            self._next_generation[run_id] = generation
            return index

    def get(self, run_id: str) -> DagContext:
        with self._lock:
            try:
                return self._indexes[run_id]
            except KeyError as exc:
                raise RunDataIndexError(f"unknown run data index: {run_id}") from exc

    def dag_context(self, run_id: str) -> DagContext:
        """Return the single internal data context owned by ``run_id``."""

        return self.get(run_id)

    def destroy(self, run_id: str, *, completed_at_ms: int) -> RunDataTombstone:
        index = self.get(run_id)
        reference = index.reference
        return index.destroy(
            controller_generation=reference.controller_generation,
            index_generation=reference.index_generation,
            completed_at_ms=completed_at_ms,
        )

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(
                index.state is not RunDataState.DESTROYED
                for index in self._indexes.values()
            )
