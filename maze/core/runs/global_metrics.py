"""Restart-safe cluster metrics derived from persisted static runs."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Iterable


RUN_STATUS_TEMPLATE = {
    "submitted": 0,
    "running": 0,
    "succeeded": 0,
    "failed": 0,
    "canceled": 0,
    "interrupted": 0,
    "timed_out": 0,
}
TASK_STATUS_TEMPLATE = {
    "running": 0,
    "succeeded": 0,
    "failed": 0,
    "canceled": 0,
    "timed_out": 0,
}
TERMINAL_TASK_STATUSES = {"succeeded", "failed", "canceled", "timed_out"}


def _run_status(value: Any) -> str:
    status = str(value or "submitted")
    if status in {"created", "submitted"}:
        return "submitted"
    return "canceled" if status == "cancelled" else status


def _task_status(value: Any) -> str:
    status = str(value or "")
    return "canceled" if status == "cancelled" else status


def _add_number(target: Dict[str, Any], key: str, value: Any) -> None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        target[key] = target.get(key, 0) + value


def _task_metrics_contribution(metrics: Any) -> Dict[str, Any]:
    contribution = {
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "by_model": {},
    }
    if not isinstance(metrics, dict):
        return contribution

    _add_number(contribution, "tokens_in", metrics.get("tokens_in"))
    _add_number(contribution, "tokens_out", metrics.get("tokens_out"))
    _add_number(contribution, "cost_usd", metrics.get("cost_usd"))

    nested = metrics.get("by_model")
    if isinstance(nested, dict) and nested:
        for model_name, values in nested.items():
            if isinstance(values, dict):
                contribution["by_model"][str(model_name)] = dict(values)
        return contribution

    model_name = metrics.get("model")
    if isinstance(model_name, str) and model_name:
        contribution["by_model"][model_name] = {
            "tokens_in": contribution["tokens_in"],
            "tokens_out": contribution["tokens_out"],
            "cost_usd": contribution["cost_usd"],
            "calls": 1,
        }
    return contribution


def _run_contribution(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    task_statuses = dict(TASK_STATUS_TEMPLATE)
    total_finished = 0
    tokens_in = 0
    tokens_out = 0
    cost_usd = 0.0
    by_model: Dict[str, Dict[str, Any]] = {}

    for task in (snapshot.get("task_nodes") or {}).values():
        if not isinstance(task, dict):
            continue
        status = _task_status(task.get("status"))
        if status in task_statuses:
            task_statuses[status] += 1
        if status in TERMINAL_TASK_STATUSES:
            total_finished += 1

        metrics = _task_metrics_contribution(task.get("metrics"))
        tokens_in += metrics["tokens_in"]
        tokens_out += metrics["tokens_out"]
        cost_usd += metrics["cost_usd"]
        for model_name, values in metrics["by_model"].items():
            bucket = by_model.setdefault(model_name, {})
            for key, value in values.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    bucket[key] = bucket.get(key, 0) + value
                else:
                    bucket[key] = value

    return {
        "workflow_id": snapshot.get("workflow_id"),
        "status": _run_status(snapshot.get("status")),
        "tasks_total_finished": total_finished,
        "tasks_by_status": task_statuses,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "tokens_by_model": by_model,
    }


class GlobalMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._workflow_ids: set[str] = set()
        self._runs: Dict[str, Dict[str, Any]] = {}

    def on_workflow_created(self, workflow_id: str) -> None:
        if workflow_id:
            with self._lock:
                self._workflow_ids.add(str(workflow_id))

    def rebuild(self, snapshots: Iterable[Dict[str, Any]]) -> None:
        contributions = {}
        workflow_ids = set()
        for snapshot in snapshots:
            run_id = snapshot.get("run_id") if isinstance(snapshot, dict) else None
            if not run_id:
                continue
            contribution = _run_contribution(snapshot)
            contributions[str(run_id)] = contribution
            if contribution.get("workflow_id"):
                workflow_ids.add(str(contribution["workflow_id"]))
        with self._lock:
            self._runs = contributions
            self._workflow_ids.update(workflow_ids)

    def sync_run(self, snapshot: Dict[str, Any]) -> None:
        run_id = snapshot.get("run_id") if isinstance(snapshot, dict) else None
        if not run_id:
            raise ValueError("Static run snapshot is missing run_id")
        contribution = _run_contribution(snapshot)
        with self._lock:
            self._runs[str(run_id)] = contribution
            if contribution.get("workflow_id"):
                self._workflow_ids.add(str(contribution["workflow_id"]))

    def snapshot(self, *, workflows_in_memory: int = 0, runs_in_memory: int = 0) -> Dict[str, Any]:
        with self._lock:
            run_contributions = list(self._runs.values())
            workflows_created = len(self._workflow_ids)

        run_statuses = dict(RUN_STATUS_TEMPLATE)
        task_statuses = dict(TASK_STATUS_TEMPLATE)
        tasks_total = 0
        tokens_in = 0
        tokens_out = 0
        cost_usd = 0.0
        tokens_by_model: Dict[str, Dict[str, Any]] = {}

        for contribution in run_contributions:
            status = contribution["status"]
            run_statuses[status] = run_statuses.get(status, 0) + 1
            tasks_total += contribution["tasks_total_finished"]
            for task_status, count in contribution["tasks_by_status"].items():
                task_statuses[task_status] = task_statuses.get(task_status, 0) + count
            tokens_in += contribution["tokens_in"]
            tokens_out += contribution["tokens_out"]
            cost_usd += contribution["cost_usd"]
            for model_name, values in contribution["tokens_by_model"].items():
                bucket = tokens_by_model.setdefault(model_name, {})
                for key, value in values.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        bucket[key] = bucket.get(key, 0) + value
                    else:
                        bucket[key] = value

        return {
            "uptime_sec": int(time.time() - self._started_at),
            "started_at": self._started_at,
            "workflows": {
                "created_total": workflows_created,
                "in_memory_not_submitted": workflows_in_memory,
            },
            "static_runs": {
                "total": len(run_contributions),
                "in_memory": runs_in_memory,
                "by_status": run_statuses,
            },
            "tasks": {
                "total_finished": tasks_total,
                "by_status": task_statuses,
            },
            "tokens": {
                "in": tokens_in,
                "out": tokens_out,
                "cost_usd": round(cost_usd, 6),
                "by_model": tokens_by_model,
            },
        }
