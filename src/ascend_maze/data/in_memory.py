"""Thread-safe in-memory DataStore with explicit logical ownership."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from time import perf_counter
from threading import RLock
from typing import Any

from ascend_maze.contracts.data import (
    DataHandle,
    DataOwner,
    SharedFileRef,
    shared_file_metadata,
)
from ascend_maze.core.canonical import (
    CanonicalValue,
    FrozenMap,
    canonical_bytes,
)
from ascend_maze.core.errors import (
    CanonicalizationError,
    DataHandleInvalidError,
    DataOwnershipError,
    DataStoreWriteError,
)
from ascend_maze.core.identifiers import new_id


class _EntryState(str, Enum):
    STAGED = "staged"
    ADOPTED = "adopted"


@dataclass(slots=True)
class _Entry:
    handle: DataHandle
    value: Any
    state: _EntryState
    owner: DataOwner | None
    dedup_key: str | None = None


@dataclass(slots=True)
class _Metrics:
    put_calls: int = 0
    get_calls: int = 0
    put_deepcopy_ms: float = 0.0
    put_digest_ms: float = 0.0
    put_bytes_ms: float = 0.0
    get_deepcopy_ms: float = 0.0
    put_stable_digest_count: int = 0
    put_stable_digest_skipped_count: int = 0
    put_canonicalization_error_count: int = 0
    put_large_identity_ms: float = 0.0
    put_large_identity_count: int = 0
    put_dedup_hit_count: int = 0
    put_dedup_miss_count: int = 0
    put_large_no_deepcopy_count: int = 0
    get_large_no_deepcopy_count: int = 0

    def snapshot(self) -> dict[str, int | float]:
        return {
            "put_calls": self.put_calls,
            "get_calls": self.get_calls,
            "put_deepcopy_ms": self.put_deepcopy_ms,
            "put_digest_ms": self.put_digest_ms,
            "put_bytes_ms": self.put_bytes_ms,
            "get_deepcopy_ms": self.get_deepcopy_ms,
            "put_stable_digest_count": self.put_stable_digest_count,
            "put_stable_digest_skipped_count": self.put_stable_digest_skipped_count,
            "put_canonicalization_error_count": self.put_canonicalization_error_count,
            "put_large_identity_ms": self.put_large_identity_ms,
            "put_large_identity_count": self.put_large_identity_count,
            "put_dedup_hit_count": self.put_dedup_hit_count,
            "put_dedup_miss_count": self.put_dedup_miss_count,
            "put_large_no_deepcopy_count": self.put_large_no_deepcopy_count,
            "get_large_no_deepcopy_count": self.get_large_no_deepcopy_count,
        }


class InMemoryDataStore:
    """Model staged/adopt/release semantics without a data proxy service."""

    def __init__(
        self,
        *,
        large_value_stable_digest_threshold_bytes: int = 256 * 1024,
        enable_large_value_dedup: bool = True,
        unsafe_no_deepcopy_for_large_values: bool = False,
    ) -> None:
        if (
            isinstance(large_value_stable_digest_threshold_bytes, bool)
            or not isinstance(large_value_stable_digest_threshold_bytes, int)
            or large_value_stable_digest_threshold_bytes < 1
        ):
            raise ValueError("large_value_stable_digest_threshold_bytes must be positive")
        if not isinstance(enable_large_value_dedup, bool):
            raise ValueError("enable_large_value_dedup must be a boolean")
        if not isinstance(unsafe_no_deepcopy_for_large_values, bool):
            raise ValueError("unsafe_no_deepcopy_for_large_values must be a boolean")
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._dedup_values: dict[str, Any] = {}
        self._dedup_ref_counts: dict[str, int] = {}
        self._put_count = 0
        self._fail_put_numbers: set[int] = set()
        self._metrics = _Metrics()
        self.large_value_stable_digest_threshold_bytes = (
            large_value_stable_digest_threshold_bytes
        )
        self.enable_large_value_dedup = enable_large_value_dedup
        self.unsafe_no_deepcopy_for_large_values = (
            unsafe_no_deepcopy_for_large_values
        )
        self._lock = RLock()

    @staticmethod
    def _key(handle: DataHandle) -> tuple[str, str]:
        return (handle.owner_generation, handle.staged_handle_id)

    def fail_on_put_number(self, put_number: int) -> None:
        if isinstance(put_number, bool) or not isinstance(put_number, int) or put_number < 1:
            raise ValueError("put_number must be a positive integer")
        with self._lock:
            self._fail_put_numbers.add(put_number)

    def fail_next_put(self) -> int:
        with self._lock:
            number = self._put_count + 1
            self._fail_put_numbers.add(number)
            return number

    def put_staged(self, value: Any, owner_generation: str) -> DataHandle:
        return self._put_staged(
            value,
            owner_generation,
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
            require_stable_digest=False,
        )

    def _put_staged(
        self,
        value: Any,
        owner_generation: str,
        *,
        require_stable_digest: bool,
    ) -> DataHandle:
        if not isinstance(owner_generation, str) or not owner_generation:
            raise DataStoreWriteError("owner_generation is required")
        with self._lock:
            self._put_count += 1
            self._metrics.put_calls += 1
            if self._put_count in self._fail_put_numbers:
                self._fail_put_numbers.remove(self._put_count)
                raise DataStoreWriteError(
                    f"injected put failure at call {self._put_count}"
                )
            stable_digest: str | None = None
            size_bytes: int | None = None
            metadata: FrozenMap[CanonicalValue, CanonicalValue] = FrozenMap(
                (("backend", "memory"),)
            )
            dedup_key: str | None = None
            stored_value: Any
            if isinstance(value, SharedFileRef):
                stored_value = self._copy_for_put(value)
                metadata = FrozenMap(
                    (*metadata.items_tuple(), *shared_file_metadata(stored_value))
                )
            elif not require_stable_digest:
                stored_value = self._copy_for_put(value)
                self._metrics.put_stable_digest_skipped_count += 1
            else:
                large_identity = self._large_value_identity(value)
                if large_identity is not None:
                    dedup_key, size_bytes = large_identity
                    metadata = FrozenMap(
                        (
                            *metadata.items_tuple(),
                            ("size_bytes_source", "json"),
                            ("stable_digest_policy", "skipped_large_value"),
                        )
                    )
                    self._metrics.put_stable_digest_skipped_count += 1
                    if self.enable_large_value_dedup and dedup_key in self._dedup_values:
                        stored_value = self._dedup_values[dedup_key]
                        self._dedup_ref_counts[dedup_key] += 1
                        self._metrics.put_dedup_hit_count += 1
                    else:
                        if self.unsafe_no_deepcopy_for_large_values:
                            stored_value = value
                            self._metrics.put_large_no_deepcopy_count += 1
                        else:
                            stored_value = self._copy_for_put(value)
                        if self.enable_large_value_dedup:
                            self._dedup_values[dedup_key] = stored_value
                            self._dedup_ref_counts[dedup_key] = 1
                            self._metrics.put_dedup_miss_count += 1
                else:
                    stored_value = self._copy_for_put(value)
                    try:
                        started = perf_counter()
                        payload = canonical_bytes(stored_value)
                        self._metrics.put_bytes_ms += _elapsed_ms(started)
                        size_bytes = len(payload)
                        started = perf_counter()
                        stable_digest = hashlib.sha256(payload).hexdigest()
                        self._metrics.put_digest_ms += _elapsed_ms(started)
                        self._metrics.put_stable_digest_count += 1
                    except CanonicalizationError:
                        self._metrics.put_canonicalization_error_count += 1
            handle = DataHandle(
                owner_generation=owner_generation,
                staged_handle_id=new_id("data"),
                stable_digest=stable_digest,
                size_bytes=size_bytes,
                metadata=metadata,
            )
            key = self._key(handle)
            self._entries[key] = _Entry(
                handle=handle,
                value=stored_value,
                state=_EntryState.STAGED,
                owner=None,
                dedup_key=dedup_key,
            )
            return handle

    def get(self, handle: DataHandle) -> Any:
        with self._lock:
            entry = self._require_entry(handle)
            self._metrics.get_calls += 1
            if (
                self.unsafe_no_deepcopy_for_large_values
                and entry.dedup_key is not None
            ):
                self._metrics.get_large_no_deepcopy_count += 1
                return entry.value
            try:
                started = perf_counter()
                result = deepcopy(entry.value)
                self._metrics.get_deepcopy_ms += _elapsed_ms(started)
                return result
            except Exception as exc:
                raise DataHandleInvalidError("stored value cannot be copied") from exc

    def adopt(self, handles: tuple[DataHandle, ...], owner: DataOwner) -> None:
        if not isinstance(handles, tuple):
            raise DataOwnershipError("adopt handles must be a tuple")
        if not isinstance(owner, DataOwner):
            raise DataOwnershipError("adopt owner must be DataOwner")
        with self._lock:
            entries: list[_Entry] = []
            seen: set[tuple[str, str]] = set()
            for handle in handles:
                if not isinstance(handle, DataHandle):
                    raise DataOwnershipError("adopt requires DataHandle values")
                if handle.owner_generation != owner.owner_generation:
                    raise DataOwnershipError("owner generation does not match handle")
                key = self._key(handle)
                if key in seen:
                    raise DataOwnershipError("adopt contains a duplicate handle")
                seen.add(key)
                entry = self._require_entry(handle)
                if entry.state is _EntryState.ADOPTED and entry.owner != owner:
                    raise DataOwnershipError("handle is already adopted by another owner")
                entries.append(entry)
            for entry in entries:
                entry.state = _EntryState.ADOPTED
                entry.owner = owner

    def release(self, handle: DataHandle) -> None:
        key = self._key(handle)
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                return
            self._release_dedup_reference(entry.dedup_key)
            entry.value = None

    def release_many(self, handles: tuple[DataHandle, ...]) -> None:
        for handle in handles:
            self.release(handle)

    def state_of(self, handle: DataHandle) -> str:
        with self._lock:
            entry = self._require_entry(handle)
            return entry.state.value

    def owner_of(self, handle: DataHandle) -> DataOwner | None:
        with self._lock:
            return self._require_entry(handle).owner

    def handles_for_owner(self, owner: DataOwner) -> tuple[DataHandle, ...]:
        with self._lock:
            return tuple(
                entry.handle
                for entry in self._entries.values()
                if entry.owner == owner
            )

    def _require_entry(self, handle: DataHandle) -> _Entry:
        key = self._key(handle)
        entry = self._entries.get(key)
        if entry is None:
            raise DataHandleInvalidError("data handle is unknown or released")
        if entry.handle != handle:
            raise DataHandleInvalidError("data handle metadata does not match")
        return entry

    def _copy_for_put(self, value: Any) -> Any:
        try:
            started = perf_counter()
            stored_value = deepcopy(value)
            self._metrics.put_deepcopy_ms += _elapsed_ms(started)
            return stored_value
        except Exception as exc:
            raise DataStoreWriteError("value cannot be copied into DataStore") from exc

    def _large_value_identity(self, value: Any) -> tuple[str, int] | None:
        if not isinstance(value, (dict, list, tuple)):
            return None
        started = perf_counter()
        try:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            self._metrics.put_large_identity_ms += _elapsed_ms(started)
            return None
        self._metrics.put_large_identity_ms += _elapsed_ms(started)
        size_bytes = len(payload)
        if size_bytes <= self.large_value_stable_digest_threshold_bytes:
            return None
        self._metrics.put_large_identity_count += 1
        return hashlib.sha256(payload).hexdigest(), size_bytes

    def _release_dedup_reference(self, dedup_key: str | None) -> None:
        if dedup_key is None or dedup_key not in self._dedup_ref_counts:
            return
        remaining = self._dedup_ref_counts[dedup_key] - 1
        if remaining > 0:
            self._dedup_ref_counts[dedup_key] = remaining
            return
        del self._dedup_ref_counts[dedup_key]
        self._dedup_values.pop(dedup_key, None)

    def metrics_snapshot(self) -> dict[str, int | float]:
        with self._lock:
            return {
                **self._metrics.snapshot(),
                "active_count": len(self._entries),
                "staged_count": self.staged_count,
                "adopted_count": self.adopted_count,
                "dedup_value_count": len(self._dedup_values),
                "large_value_stable_digest_threshold_bytes": (
                    self.large_value_stable_digest_threshold_bytes
                ),
                "unsafe_no_deepcopy_for_large_values": int(
                    self.unsafe_no_deepcopy_for_large_values
                ),
            }

    def reset_metrics(self) -> None:
        with self._lock:
            self._metrics = _Metrics()

    @property
    def put_count(self) -> int:
        with self._lock:
            return self._put_count

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def staged_count(self) -> int:
        with self._lock:
            return sum(
                entry.state is _EntryState.STAGED
                for entry in self._entries.values()
            )

    @property
    def adopted_count(self) -> int:
        with self._lock:
            return sum(
                entry.state is _EntryState.ADOPTED
                for entry in self._entries.values()
            )


def _elapsed_ms(started: float) -> float:
    return max(0.0, (perf_counter() - started) * 1_000)
