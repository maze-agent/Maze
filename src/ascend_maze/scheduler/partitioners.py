"""Orthogonal heterogeneous and unified queue partitioning."""

from __future__ import annotations

from ascend_maze.scheduler.contracts import SchedulableTaskView


class HeterogeneousPartitioner:
    name = "heterogeneous"

    def partition(self, task: SchedulableTaskView) -> str:
        if task.task_kind not in {"cpu", "npu", "io"}:
            raise ValueError(f"unsupported task kind: {task.task_kind}")
        return task.task_kind


class UnifiedPartitioner:
    name = "unified"

    def partition(self, task: SchedulableTaskView) -> str:
        del task
        return "default"
