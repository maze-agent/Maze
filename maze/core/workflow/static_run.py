import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from maze.core.scheduler.result_summary import to_json_safe
from maze.core.workflow.dynamic_store import default_workspace_dir


SCHEMA_VERSION = 1
ACTIVE_STATIC_RUN_STATUSES = {"created", "running"}
TERMINAL_STATIC_RUN_STATUSES = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _duration_seconds(started_time: float | None, finished_time: float | None) -> float | None:
    if started_time is None or finished_time is None:
        return None
    return round(max(0.0, finished_time - started_time), 6)


def _task_io_snapshot(task_io: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    if not task_io:
        return []

    params = task_io.get("input_params") or task_io.get("output_params") or {}
    return [
        {
            "name": info.get("key"),
            "data_type": info.get("data_type", "any"),
            "input_schema": info.get("input_schema"),
            "value": to_json_safe(info.get("value")) if "value" in info else None,
            "has_value": info.get("has_value"),
        }
        for _, info in sorted(params.items(), key=lambda item: str(item[0]))
    ]


class StaticRun:
    def __init__(
        self,
        run_id: str,
        workflow_id: str,
        workflow: Any,
        timeout_seconds: float | None = None,
        tags: List[str] | None = None,
        metadata: Dict[str, Any] | None = None,
    ):
        self.run_id = run_id
        self.workflow_id = workflow_id
        self.run_type = "static"
        self.status = "created"
        self.timeout_seconds = timeout_seconds
        self.tags = list(tags or [])
        self.metadata = dict(metadata or {})
        self.created_time = time.time()
        self.updated_time = self.created_time
        self.started_time = None
        self.finished_time = None
        self.error_summary = None
        self.result_summary = None
        self.event_log: List[Dict[str, Any]] = []
        self.event_seq = 0

        self.graph = {
            "nodes": sorted(workflow.tasks),
            "edges": [
                {"source": source, "target": target}
                for source, target in sorted(workflow.graph.edges())
            ],
        }
        self.task_nodes: Dict[str, Dict[str, Any]] = {}
        for task_id, task in sorted(workflow.tasks.items()):
            parents = sorted(workflow.graph.predecessors(task_id))
            children = sorted(workflow.graph.successors(task_id))
            self.task_nodes[task_id] = {
                "task_id": task_id,
                "task_name": task.task_name,
                "status": "pending" if parents else "queued",
                "parents": parents,
                "children": children,
                "created_time": task.created_time,
                "started_time": None,
                "finished_time": None,
                "duration_seconds": None,
                "resources": to_json_safe(task.resources),
                "inputs": _task_io_snapshot(task.task_input),
                "outputs": _task_io_snapshot(task.task_output),
                "selected_node": None,
                "result_summary": None,
                "error": None,
                "attempt": 0,
                "last_error": None,
                "pending_reason": None,
                "schedule_decision": None,
                "file_manifest": None,
            }

    def _touch(self):
        self.updated_time = time.time()

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATIC_RUN_STATUSES

    def mark_started(self):
        if self.started_time is None:
            self.started_time = time.time()
        if self.status == "created":
            self.status = "running"
        self._touch()

    def mark_task_queued(self, task_id: str):
        task = self.task_nodes.get(task_id)
        if task and task["status"] == "pending":
            task["status"] = "queued"
            self._touch()

    def mark_task_started(self, task_id: str, node_info: Dict[str, Any] | None = None):
        self.mark_started()
        task = self.task_nodes.get(task_id)
        if not task:
            return
        task["status"] = "running"
        task["started_time"] = time.time()
        task["pending_reason"] = None
        task["schedule_decision"] = to_json_safe((node_info or {}).get("schedule_decision"))
        task["selected_node"] = {
            "node_id": (node_info or {}).get("node_id"),
            "node_ip": (node_info or {}).get("node_ip"),
            "gpu_id": (node_info or {}).get("gpu_id"),
        }
        task["attempt"] = (node_info or {}).get("attempt") or task.get("attempt") or 1
        self._touch()

    def mark_task_pending(
        self,
        task_id: str,
        pending_reason: str | None = None,
        schedule_decision: Dict[str, Any] | None = None,
    ):
        task = self.task_nodes.get(task_id)
        if not task:
            return
        if task["status"] in {"pending", "queued"}:
            task["status"] = "queued"
        task["pending_reason"] = pending_reason
        task["schedule_decision"] = to_json_safe(schedule_decision)
        self._touch()

    def mark_task_retry(self, task_id: str, error: Any = None, attempt: int | None = None):
        task = self.task_nodes.get(task_id)
        if not task:
            return
        task["status"] = "queued"
        task["last_error"] = to_json_safe(error)
        task["error"] = None
        task["pending_reason"] = None
        if attempt is not None:
            task["attempt"] = attempt
        self._touch()

    def mark_task_finished(
        self,
        task_id: str,
        result: Any = None,
        file_manifest: Dict[str, Any] | None = None,
        metrics: Dict[str, Any] | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
        duration_ms: int | None = None,
        node_id: str | None = None,
    ):
        task = self.task_nodes.get(task_id)
        if not task:
            return
        task["status"] = "succeeded"
        if started_at is not None:
            task["started_time"] = started_at
        task["finished_time"] = finished_at or time.time()
        task["duration_seconds"] = _duration_seconds(task.get("started_time"), task.get("finished_time"))
        if duration_ms is not None:
            task["duration_ms"] = duration_ms
        if node_id:
            selected_node = task.get("selected_node") or {}
            selected_node["node_id"] = node_id
            task["selected_node"] = selected_node
        task["metrics"] = to_json_safe(metrics or {})
        task["result_summary"] = to_json_safe(result)
        task["file_manifest"] = to_json_safe(file_manifest)
        task["pending_reason"] = None

        for child_id in task.get("children", []):
            child = self.task_nodes.get(child_id)
            if child and child["status"] == "pending" and self._parents_succeeded(child_id):
                child["status"] = "queued"

        if self._all_tasks_finished():
            self.mark_succeeded()
        else:
            self._touch()

    def mark_task_failed(self, task_id: str, error: Any, file_manifest: Dict[str, Any] | None = None):
        task = self.task_nodes.get(task_id)
        if task:
            error_type = error.get("error_type") if isinstance(error, dict) else None
            task["status"] = "timed_out" if error_type == "timeout" else "failed"
            task["finished_time"] = time.time()
            task["duration_seconds"] = _duration_seconds(task.get("started_time"), task.get("finished_time"))
            task["error"] = to_json_safe(error)
            task["last_error"] = to_json_safe(error)
            if file_manifest:
                task["file_manifest"] = to_json_safe(file_manifest)
        self.status = "timed_out" if isinstance(error, dict) and error.get("error_type") == "timeout" else "failed"
        self.error_summary = to_json_safe(error)
        self.finished_time = time.time()
        self._touch()

    def mark_succeeded(self):
        if self.status == "succeeded":
            return
        self.status = "succeeded"
        self.finished_time = time.time()
        self.result_summary = {
            task_id: task.get("result_summary")
            for task_id, task in sorted(self.task_nodes.items())
            if task.get("status") == "succeeded"
        }
        self._touch()

    def mark_cancelled(self, reason: str | None = None):
        if self.is_terminal():
            return
        self.status = "cancelled"
        self.error_summary = {"message": reason or "Run cancelled"}
        self.finished_time = time.time()
        for task in self.task_nodes.values():
            if task["status"] in {"pending", "queued", "running"}:
                task["status"] = "cancelled"
        self._touch()

    def mark_interrupted(self, reason: str | None = None):
        if self.is_terminal():
            return
        self.status = "interrupted"
        self.error_summary = {"message": reason or "Head process restarted before run completed"}
        self.finished_time = time.time()
        self._touch()

    def append_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event = dict(event)
        event_data = event.get("data") or {}
        if isinstance(event_data, dict):
            event_data = dict(event_data)
            event_data.setdefault("run_id", self.run_id)
            event_data.setdefault("workflow_id", self.workflow_id)
            event_data.setdefault("run_status", self.status)
            event["data"] = event_data

        self.event_seq += 1
        event.setdefault("seq", self.event_seq)
        event.setdefault("timestamp", _utc_timestamp())
        event.setdefault("schema_version", SCHEMA_VERSION)
        self.event_log.append(event)
        self._touch()
        return event

    def get_events(self, after: int | None = None) -> List[Dict[str, Any]]:
        if after is None:
            return list(self.event_log)
        return [event for event in self.event_log if int(event.get("seq", 0)) > after]

    def task_snapshot(self, task_id: str) -> Dict[str, Any]:
        if task_id not in self.task_nodes:
            raise ValueError(f"Task not found in static run: {task_id}")
        return to_json_safe(self.task_nodes[task_id])

    def snapshot(self) -> Dict[str, Any]:
        task_counts = self._task_counts()
        total = task_counts["total"] or 1
        completed = (
            task_counts["succeeded"]
            + task_counts["failed"]
            + task_counts["cancelled"]
            + task_counts.get("timed_out", 0)
        )
        return to_json_safe({
            "schema": "static_run",
            "schema_version": SCHEMA_VERSION,
            "kind": "static",
            "run_type": self.run_type,
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "timeout_seconds": self.timeout_seconds,
            "tags": self.tags,
            "metadata": self.metadata,
            "created_time": self.created_time,
            "updated_time": self.updated_time,
            "started_time": self.started_time,
            "finished_time": self.finished_time,
            "duration_seconds": _duration_seconds(self.started_time, self.finished_time),
            "progress": {
                "completed": completed,
                "total": task_counts["total"],
                "fraction": round(completed / total, 6),
            },
            "task_counts": task_counts,
            "task_nodes": self.task_nodes,
            "graph": self.graph,
            "event_count": len(self.event_log),
            "last_event_seq": self.event_seq,
            "result_summary": self.result_summary,
            "error_summary": self.error_summary,
        })

    def _parents_succeeded(self, task_id: str) -> bool:
        task = self.task_nodes.get(task_id) or {}
        return all(
            self.task_nodes.get(parent_id, {}).get("status") == "succeeded"
            for parent_id in task.get("parents", [])
        )

    def _all_tasks_finished(self) -> bool:
        return all(task.get("status") == "succeeded" for task in self.task_nodes.values())

    def _task_counts(self) -> Dict[str, int]:
        counts = {
            "total": len(self.task_nodes),
            "pending": 0,
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "timed_out": 0,
        }
        for task in self.task_nodes.values():
            status = task.get("status")
            if status in counts:
                counts[status] += 1
        return counts


class StaticRunStore:
    def __init__(self, workspace_dir: str | os.PathLike[str] | None = None):
        self.workspace_dir = Path(workspace_dir).expanduser().resolve() if workspace_dir else default_workspace_dir()
        self.runs_dir = self.workspace_dir / "workflow_runs" / "static_runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id:
            raise ValueError(f"Invalid static run id: {run_id}")
        return self.runs_dir / run_id

    def run_json_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.jsonl"

    def save_run(self, snapshot: Dict[str, Any]):
        run_id = snapshot["run_id"]
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            **to_json_safe(snapshot),
            "schema": "static_run",
            "schema_version": SCHEMA_VERSION,
        }
        tmp_path = run_dir / "run.json.tmp"
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, self.run_json_path(run_id))

    def append_event(self, run_id: str, event: Dict[str, Any]):
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            **to_json_safe(event),
        }
        with self.events_path(run_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    def load_run(self, run_id: str) -> Dict[str, Any]:
        path = self.run_json_path(run_id)
        if not path.exists():
            raise ValueError(f"Static run not found: {run_id}")
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def load_events(self, run_id: str, after: int | None = None) -> List[Dict[str, Any]]:
        path = self.events_path(run_id)
        if not path.exists():
            return []

        events = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if after is None or int(event.get("seq", 0)) > after:
                    events.append(event)
        return events

    def list_runs(self, summary: bool = False) -> List[Dict[str, Any]]:
        snapshots = []
        for path in self.runs_dir.glob("*/run.json"):
            try:
                snapshot = self.load_run(path.parent.name)
                snapshots.append(static_run_summary(snapshot) if summary else snapshot)
            except Exception:
                continue
        snapshots.sort(key=lambda item: item.get("created_time") or 0, reverse=True)
        return snapshots

    def delete_run(self, run_id: str):
        shutil.rmtree(self.run_dir(run_id))

    def recover_interrupted_runs(self) -> List[Dict[str, Any]]:
        recovered = []
        for snapshot in self.list_runs():
            if snapshot.get("status") not in ACTIVE_STATIC_RUN_STATUSES:
                continue

            run_id = snapshot["run_id"]
            now = time.time()
            snapshot["status"] = "interrupted"
            snapshot["finished_time"] = snapshot.get("finished_time") or now
            snapshot["updated_time"] = now
            snapshot["error_summary"] = snapshot.get("error_summary") or {
                "message": "Head process restarted before run completed",
            }
            for task in (snapshot.get("task_nodes") or {}).values():
                if task.get("status") in {"pending", "queued", "running"}:
                    task["status"] = "cancelled"
            snapshot["task_counts"] = _snapshot_task_counts(snapshot)
            total = snapshot["task_counts"]["total"] or 1
            completed = (
                snapshot["task_counts"]["succeeded"]
                + snapshot["task_counts"]["failed"]
                + snapshot["task_counts"]["cancelled"]
                + snapshot["task_counts"].get("timed_out", 0)
            )
            snapshot["progress"] = {
                "completed": completed,
                "total": snapshot["task_counts"]["total"],
                "fraction": round(completed / total, 6),
            }

            last_seq = int(snapshot.get("last_event_seq") or 0)
            event = {
                "type": "interrupt_workflow",
                "seq": last_seq + 1,
                "timestamp": _utc_timestamp(),
                "schema_version": SCHEMA_VERSION,
                "data": {
                    "run_id": run_id,
                    "workflow_id": snapshot.get("workflow_id"),
                    "run_status": "interrupted",
                    "reason": "Head process restarted before run completed",
                },
            }
            snapshot["event_count"] = int(snapshot.get("event_count") or 0) + 1
            snapshot["last_event_seq"] = event["seq"]

            self.append_event(run_id, event)
            self.save_run(snapshot)
            recovered.append(snapshot)
        return recovered

    def cleanup(
        self,
        statuses: Iterable[str] | None = None,
        older_than_days: int | float | None = None,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        status_filter = set(statuses or TERMINAL_STATIC_RUN_STATUSES)
        cutoff = None
        if older_than_days is not None:
            cutoff = time.time() - (float(older_than_days) * 86400)

        candidates = []
        for snapshot in self.list_runs():
            status = snapshot.get("status")
            if status not in status_filter or status not in TERMINAL_STATIC_RUN_STATUSES:
                continue
            if cutoff is not None:
                finished_time = snapshot.get("finished_time") or snapshot.get("updated_time")
                if not finished_time or float(finished_time) > cutoff:
                    continue
            candidates.append(snapshot)

        deleted_run_ids = []
        if not dry_run:
            for snapshot in candidates:
                run_id = snapshot["run_id"]
                self.delete_run(run_id)
                deleted_run_ids.append(run_id)

        return {
            "dry_run": dry_run,
            "matched_count": len(candidates),
            "deleted_count": len(deleted_run_ids),
            "runs": [static_run_summary(snapshot) for snapshot in candidates],
            "deleted_run_ids": deleted_run_ids,
        }


def static_run_summary(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return to_json_safe({
        "schema": snapshot.get("schema", "static_run"),
        "schema_version": snapshot.get("schema_version", SCHEMA_VERSION),
        "kind": "static",
        "summary": True,
        "run_type": snapshot.get("run_type", "static"),
        "run_id": snapshot.get("run_id"),
        "workflow_id": snapshot.get("workflow_id"),
        "status": snapshot.get("status"),
        "timeout_seconds": snapshot.get("timeout_seconds"),
        "tags": snapshot.get("tags") or [],
        "metadata": snapshot.get("metadata") or {},
        "created_time": snapshot.get("created_time"),
        "updated_time": snapshot.get("updated_time"),
        "started_time": snapshot.get("started_time"),
        "finished_time": snapshot.get("finished_time"),
        "duration_seconds": snapshot.get("duration_seconds"),
        "progress": snapshot.get("progress") or {},
        "task_counts": snapshot.get("task_counts") or {},
        "event_count": snapshot.get("event_count") or 0,
        "last_event_seq": snapshot.get("last_event_seq") or 0,
        "result_summary": snapshot.get("result_summary"),
        "error_summary": snapshot.get("error_summary"),
    })


def _snapshot_task_counts(snapshot: Dict[str, Any]) -> Dict[str, int]:
    counts = {
        "total": len(snapshot.get("task_nodes") or {}),
        "pending": 0,
        "queued": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
        "timed_out": 0,
    }
    for task in (snapshot.get("task_nodes") or {}).values():
        status = task.get("status")
        if status in counts:
            counts[status] += 1
    return counts
