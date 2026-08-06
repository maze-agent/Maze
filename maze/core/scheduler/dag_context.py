from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from typing import Dict


@dataclass
class DAGContext:
    workflow_id: str
    preferred_node_id: str
    preferred_node_ip: str
    created_time: float
    last_used_time: float
    selected_task_count: int = 0

    def touch(self):
        self.last_used_time = time.time()
        self.selected_task_count += 1

    def to_dict(self):
        return {
            "workflow_id": self.workflow_id,
            "preferred_node_id": self.preferred_node_id,
            "preferred_node_ip": self.preferred_node_ip,
            "created_time": self.created_time,
            "last_used_time": self.last_used_time,
            "selected_task_count": self.selected_task_count,
        }


class DAGContextManager:
    def __init__(self):
        self.run2ctx: Dict[str, DAGContext] = {}
        self.node_load_counter: Counter[str] = Counter()

    def get_context(self, workflow_id: str | None) -> DAGContext | None:
        if not workflow_id:
            return None
        return self.run2ctx.get(workflow_id)

    def preferred_node_id(self, workflow_id: str | None) -> str | None:
        context = self.get_context(workflow_id)
        return context.preferred_node_id if context else None

    def node_context_load(self, node_id: str) -> int:
        return int(self.node_load_counter.get(node_id, 0))

    def record_selection(self, workflow_id: str | None, node_id: str, node_ip: str) -> tuple[DAGContext | None, bool]:
        if not workflow_id:
            return None, False

        context = self.run2ctx.get(workflow_id)
        if context is None:
            now = time.time()
            context = DAGContext(
                workflow_id=workflow_id,
                preferred_node_id=node_id,
                preferred_node_ip=node_ip,
                created_time=now,
                last_used_time=now,
            )
            self.run2ctx[workflow_id] = context
            self.node_load_counter[node_id] += 1
            created = True
        else:
            created = False

        context.touch()
        return context, created

    def release_context(self, workflow_id: str | None) -> bool:
        if not workflow_id:
            return False

        context = self.run2ctx.pop(workflow_id, None)
        if context is None:
            return False

        node_id = context.preferred_node_id
        if node_id in self.node_load_counter:
            self.node_load_counter[node_id] -= 1
            if self.node_load_counter[node_id] <= 0:
                del self.node_load_counter[node_id]
        return True

    def release_node_contexts(self, node_id: str) -> list[str]:
        released = [
            workflow_id
            for workflow_id, context in list(self.run2ctx.items())
            if context.preferred_node_id == node_id
        ]
        for workflow_id in released:
            self.release_context(workflow_id)
        return released

    def snapshot(self):
        return {
            "contexts": {
                workflow_id: context.to_dict()
                for workflow_id, context in self.run2ctx.items()
            },
            "node_loads": dict(self.node_load_counter),
        }
