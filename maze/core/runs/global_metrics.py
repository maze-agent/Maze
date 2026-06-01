"""Process-wide cluster-level metrics aggregator (in-memory).

Tracks counts of static runs by status, cumulative tasks, and tokens.
Mutating methods are thread-safe.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class GlobalMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._started_at = time.time()

        # Workflow templates ever seen (created, possibly never submitted).
        self.workflows_created: int = 0

        # Static runs (= submitted workflows).
        self.static_runs_total: int = 0
        self.static_runs_by_status: Dict[str, int] = {
            "submitted": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "canceled": 0,
            "interrupted": 0,
        }

        # Tasks (across all static runs).
        self.tasks_total: int = 0
        self.tasks_by_status: Dict[str, int] = {
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "canceled": 0,
        }

        # Tokens.
        self.tokens_in_total: int = 0
        self.tokens_out_total: int = 0
        self.tokens_by_model: Dict[str, Dict[str, Any]] = {}
        self.cost_usd_total: float = 0.0

    # --- workflow / run lifecycle ------------------------------------

    def on_workflow_created(self, workflow_id: str) -> None:
        with self._lock:
            self.workflows_created += 1

    def on_run_submitted(self, run_id: str) -> None:
        with self._lock:
            self.static_runs_total += 1
            self.static_runs_by_status["submitted"] = self.static_runs_by_status.get("submitted", 0) + 1

    def on_run_status_change(self, run_id: str, old: str, new: str) -> None:
        if old == new:
            return
        with self._lock:
            if old in self.static_runs_by_status:
                self.static_runs_by_status[old] = max(0, self.static_runs_by_status[old] - 1)
            self.static_runs_by_status[new] = self.static_runs_by_status.get(new, 0) + 1

    # --- task lifecycle ----------------------------------------------

    def on_task_started(self, run_id: str, task_id: str) -> None:
        with self._lock:
            self.tasks_by_status["running"] = self.tasks_by_status.get("running", 0) + 1

    def on_task_finished(
        self,
        run_id: str,
        task_id: str,
        status: str = "succeeded",
        metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            self.tasks_total += 1
            self.tasks_by_status["running"] = max(0, self.tasks_by_status.get("running", 0) - 1)
            if status not in self.tasks_by_status:
                self.tasks_by_status[status] = 0
            self.tasks_by_status[status] += 1

            if metrics:
                if isinstance(metrics.get("tokens_in"), (int, float)):
                    self.tokens_in_total += int(metrics["tokens_in"])
                if isinstance(metrics.get("tokens_out"), (int, float)):
                    self.tokens_out_total += int(metrics["tokens_out"])
                if isinstance(metrics.get("cost_usd"), (int, float)):
                    self.cost_usd_total += float(metrics["cost_usd"])

                # Bucket by model: prefer the nested ``by_model`` dict if
                # present (filled by the collector). Fall back to top-level
                # ``model`` only when no nested bucket is provided.
                nested = metrics.get("by_model")
                if isinstance(nested, dict) and nested:
                    for model_name, m in nested.items():
                        if not isinstance(m, dict):
                            continue
                        bucket = self.tokens_by_model.setdefault(
                            model_name, {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "calls": 0}
                        )
                        for k, v in m.items():
                            if isinstance(v, (int, float)):
                                bucket[k] = bucket.get(k, 0) + v
                            else:
                                bucket[k] = v
                else:
                    model = metrics.get("model")
                    if isinstance(model, str) and model:
                        bucket = self.tokens_by_model.setdefault(
                            model, {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "calls": 0}
                        )
                        if isinstance(metrics.get("tokens_in"), (int, float)):
                            bucket["tokens_in"] += int(metrics["tokens_in"])
                        if isinstance(metrics.get("tokens_out"), (int, float)):
                            bucket["tokens_out"] += int(metrics["tokens_out"])
                        if isinstance(metrics.get("cost_usd"), (int, float)):
                            bucket["cost_usd"] = bucket.get("cost_usd", 0.0) + float(metrics["cost_usd"])
                        bucket["calls"] = bucket.get("calls", 0) + 1

    # --- snapshot -----------------------------------------------------

    def snapshot(self, *, workflows_in_memory: int = 0, runs_in_memory: int = 0) -> Dict[str, Any]:
        """Return a JSON-serializable view of all metrics.

        Parameters
        ----------
        workflows_in_memory:
            Number of workflow templates currently held by MaPath but not yet submitted.
            Computed by the caller from MaPath.workflows.
        runs_in_memory:
            Number of in-memory submit_workflows entries.
        """
        with self._lock:
            data = {
                "uptime_sec": int(time.time() - self._started_at),
                "started_at": self._started_at,
                "workflows": {
                    "created_total": self.workflows_created,
                    "in_memory_not_submitted": workflows_in_memory,
                },
                "static_runs": {
                    "total": self.static_runs_total,
                    "in_memory": runs_in_memory,
                    "by_status": dict(self.static_runs_by_status),
                },
                "tasks": {
                    "total_finished": self.tasks_total,
                    "by_status": dict(self.tasks_by_status),
                },
                "tokens": {
                    "in": self.tokens_in_total,
                    "out": self.tokens_out_total,
                    "cost_usd": round(self.cost_usd_total, 6),
                    "by_model": {k: dict(v) for k, v in self.tokens_by_model.items()},
                },
            }
            return data
