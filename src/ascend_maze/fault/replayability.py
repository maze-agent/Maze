"""Retry input/code replayability checks without materializing data values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ascend_maze.contracts.data import DataHandle, DataStore


@dataclass(frozen=True, slots=True)
class ReplayabilityResult:
    replayable: bool
    reason: str
    checked_handle_ids: tuple[str, ...]
    required_node_id: str | None = None


class ReplayabilityChecker:
    """Prove that immutable code and adopted input handles remain available."""

    def __init__(self, data_store: DataStore) -> None:
        self._data_store = data_store

    def check(
        self,
        *,
        code_available: bool,
        environment_matches: bool,
        handles: Iterable[DataHandle],
    ) -> ReplayabilityResult:
        checked: list[str] = []
        required_node_id: str | None = None
        if not code_available:
            return ReplayabilityResult(False, "code_handle_unavailable", ())
        if not environment_matches:
            return ReplayabilityResult(False, "environment_mismatch", ())
        for handle in handles:
            checked.append(handle.staged_handle_id)
            try:
                state = self._data_store.state_of(handle)
            except Exception:
                return ReplayabilityResult(
                    False,
                    "data_handle_unavailable",
                    tuple(checked),
                    required_node_id,
                )
            if state != "adopted":
                return ReplayabilityResult(
                    False,
                    "data_handle_not_adopted",
                    tuple(checked),
                    required_node_id,
                )
            local_node = handle.metadata.get("node_local_node_id")
            if local_node is not None:
                if not isinstance(local_node, str) or not local_node:
                    return ReplayabilityResult(
                        False,
                        "node_local_input_identity_invalid",
                        tuple(checked),
                        required_node_id,
                    )
                if required_node_id is not None and required_node_id != local_node:
                    return ReplayabilityResult(
                        False,
                        "node_local_inputs_span_multiple_nodes",
                        tuple(checked),
                        required_node_id,
                    )
                required_node_id = local_node
        if required_node_id is not None:
            # Stage one has no node-pinned retry placement contract. Refuse to move a
            # node-local path rather than silently handing it to another node.
            return ReplayabilityResult(
                False,
                "node_local_input_not_replayable",
                tuple(checked),
                required_node_id,
            )
        return ReplayabilityResult(True, "replayable", tuple(checked))
