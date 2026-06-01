"""Snapshot dataclasses for static workflow runs.

These structures are persisted to disk and exposed via HTTP APIs.
Field names are stable and considered public API.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


TERMINAL_STATIC_RUN_STATUSES = {"succeeded", "failed", "canceled", "interrupted"}
ACTIVE_STATIC_RUN_STATUSES = {"submitted", "running"}


def merge_metrics(*metrics_dicts: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge multiple metrics dicts.

    Numeric values for the same key are summed. Non-numeric values are
    overwritten by later dicts. Nested dict ``by_model`` is merged per-model.
    """
    merged: Dict[str, Any] = {}
    for m in metrics_dicts:
        if not m:
            continue
        for key, value in m.items():
            if key == "by_model" and isinstance(value, dict):
                bucket = merged.setdefault("by_model", {})
                for model, model_metrics in value.items():
                    if not isinstance(model_metrics, dict):
                        continue
                    target = bucket.setdefault(model, {})
                    for sub_key, sub_value in model_metrics.items():
                        if isinstance(sub_value, (int, float)):
                            target[sub_key] = target.get(sub_key, 0) + sub_value
                        else:
                            target[sub_key] = sub_value
                continue
            if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
                merged[key] = merged[key] + value
            elif isinstance(value, (int, float)) and key not in merged:
                merged[key] = value
            else:
                merged[key] = value
    return merged


@dataclass
class TaskSnapshot:
    task_id: str
    task_name: str
    status: str = "pending"  # pending|ready|running|succeeded|failed|canceled
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    duration_ms: Optional[int] = None
    node_id: Optional[str] = None
    retry_count: int = 0
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "node_id": self.node_id,
            "retry_count": self.retry_count,
            "metrics": self.metrics,
            "error": self.error,
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "TaskSnapshot":
        return cls(
            task_id=data["task_id"],
            task_name=data.get("task_name") or data["task_id"],
            status=data.get("status", "pending"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            duration_ms=data.get("duration_ms"),
            node_id=data.get("node_id"),
            retry_count=int(data.get("retry_count") or 0),
            metrics=dict(data.get("metrics") or {}),
            error=data.get("error"),
        )


@dataclass
class RunMetrics:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    by_model: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def absorb(self, task_metrics: Dict[str, Any]) -> None:
        """Merge a single task's metrics into the run-level aggregate.

        Bucketing rule (avoids double-counting):
          - If the task metrics already contain a ``by_model`` sub-dict,
            we trust it as the source of truth (it was prepared by the
            in-process collector) and ignore the top-level ``model``.
          - Otherwise we fall back to bucketing under the top-level
            ``model`` key (e.g. metrics provided via ``__maze_metrics__``
            without nested by_model).
        """
        if not task_metrics:
            return
        if isinstance(task_metrics.get("tokens_in"), (int, float)):
            self.tokens_in += int(task_metrics["tokens_in"])
        if isinstance(task_metrics.get("tokens_out"), (int, float)):
            self.tokens_out += int(task_metrics["tokens_out"])
        if isinstance(task_metrics.get("cost_usd"), (int, float)):
            self.cost_usd += float(task_metrics["cost_usd"])

        nested = task_metrics.get("by_model")
        if isinstance(nested, dict) and nested:
            for model_name, m in nested.items():
                if not isinstance(m, dict):
                    continue
                bucket = self.by_model.setdefault(
                    model_name, {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "calls": 0}
                )
                for k, v in m.items():
                    if isinstance(v, (int, float)):
                        bucket[k] = bucket.get(k, 0) + v
                    else:
                        bucket[k] = v
            return

        model = task_metrics.get("model")
        if isinstance(model, str) and model:
            bucket = self.by_model.setdefault(
                model, {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "calls": 0}
            )
            if isinstance(task_metrics.get("tokens_in"), (int, float)):
                bucket["tokens_in"] += int(task_metrics["tokens_in"])
            if isinstance(task_metrics.get("tokens_out"), (int, float)):
                bucket["tokens_out"] += int(task_metrics["tokens_out"])
            if isinstance(task_metrics.get("cost_usd"), (int, float)):
                bucket["cost_usd"] = bucket.get("cost_usd", 0.0) + float(task_metrics["cost_usd"])
            bucket["calls"] = bucket.get("calls", 0) + 1

    def to_json(self) -> Dict[str, Any]:
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "by_model": self.by_model,
        }

    @classmethod
    def from_json(cls, data: Optional[Dict[str, Any]]) -> "RunMetrics":
        if not data:
            return cls()
        return cls(
            tokens_in=int(data.get("tokens_in") or 0),
            tokens_out=int(data.get("tokens_out") or 0),
            cost_usd=float(data.get("cost_usd") or 0.0),
            by_model=dict(data.get("by_model") or {}),
        )


@dataclass
class StaticRunSnapshot:
    run_id: str
    workflow_id: str
    kind: str = "static"
    status: str = "submitted"  # submitted|running|succeeded|failed|canceled|interrupted
    created_time: float = field(default_factory=time.time)
    submitted_time: Optional[float] = None
    started_time: Optional[float] = None
    finished_time: Optional[float] = None
    updated_time: float = field(default_factory=time.time)
    task_total: int = 0
    tasks: Dict[str, TaskSnapshot] = field(default_factory=dict)
    metrics: RunMetrics = field(default_factory=RunMetrics)
    failure_reason: Optional[str] = None
    cancel_reason: Optional[str] = None
    event_count: int = 0
    last_event_seq: int = 0

    @property
    def task_done(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status in ("succeeded", "failed", "canceled"))

    @property
    def task_running(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status == "running")

    @property
    def task_pending(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status in ("pending", "ready"))

    @property
    def task_succeeded(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status == "succeeded")

    @property
    def task_failed(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status == "failed")

    def to_json(self, include_tasks: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "kind": self.kind,
            "status": self.status,
            "created_time": self.created_time,
            "submitted_time": self.submitted_time,
            "started_time": self.started_time,
            "finished_time": self.finished_time,
            "updated_time": self.updated_time,
            "task_total": self.task_total,
            "task_counts": {
                "total": self.task_total,
                "done": self.task_done,
                "running": self.task_running,
                "pending": self.task_pending,
                "succeeded": self.task_succeeded,
                "failed": self.task_failed,
            },
            "metrics": self.metrics.to_json(),
            "failure_reason": self.failure_reason,
            "cancel_reason": self.cancel_reason,
            "event_count": self.event_count,
            "last_event_seq": self.last_event_seq,
        }
        if include_tasks:
            payload["tasks"] = {tid: t.to_json() for tid, t in self.tasks.items()}
        return payload

    def summary_json(self) -> Dict[str, Any]:
        return self.to_json(include_tasks=False)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "StaticRunSnapshot":
        tasks = {}
        for tid, t in (data.get("tasks") or {}).items():
            tasks[tid] = TaskSnapshot.from_json(t)
        snap = cls(
            run_id=data["run_id"],
            workflow_id=data.get("workflow_id") or data["run_id"],
            kind=data.get("kind", "static"),
            status=data.get("status", "submitted"),
            created_time=float(data.get("created_time") or time.time()),
            submitted_time=data.get("submitted_time"),
            started_time=data.get("started_time"),
            finished_time=data.get("finished_time"),
            updated_time=float(data.get("updated_time") or time.time()),
            task_total=int(data.get("task_total") or len(tasks)),
            tasks=tasks,
            metrics=RunMetrics.from_json(data.get("metrics")),
            failure_reason=data.get("failure_reason"),
            cancel_reason=data.get("cancel_reason"),
            event_count=int(data.get("event_count") or 0),
            last_event_seq=int(data.get("last_event_seq") or 0),
        )
        return snap

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATIC_RUN_STATUSES

    def get_running_tasks_view(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = now or time.time()
        out = []
        for t in self.tasks.values():
            if t.status != "running":
                continue
            duration_so_far = None
            if t.started_at:
                duration_so_far = int((now - t.started_at) * 1000)
            out.append({
                "task_id": t.task_id,
                "task_name": t.task_name,
                "started_at": t.started_at,
                "node_id": t.node_id,
                "duration_so_far_ms": duration_so_far,
            })
        return out
