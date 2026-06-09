import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from maze.core.scheduler.result_summary import to_json_safe
from maze.core.workflow.dynamic import TERMINAL_DYNAMIC_RUN_STATUSES


SCHEMA_VERSION = 1
ACTIVE_DYNAMIC_RUN_STATUSES = {"created", "running"}


def default_workspace_dir() -> Path:
    project_root = Path(__file__).resolve().parents[3]
    return Path(os.environ.get("MAZE_WORKSPACE_DIR", project_root / "workspace")).expanduser().resolve()


class DynamicRunStore:
    def __init__(self, workspace_dir: str | os.PathLike[str] | None = None):
        self.workspace_dir = Path(workspace_dir).expanduser().resolve() if workspace_dir else default_workspace_dir()
        self.runs_dir = self.workspace_dir / "workflow_runs" / "dynamic"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir = self.workspace_dir / "workspaces"

    def run_dir(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id:
            raise ValueError(f"Invalid dynamic run id: {run_id}")
        return self.runs_dir / run_id

    def run_json_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.jsonl"

    def workspace_run_dir_from_snapshot(self, snapshot: Dict[str, Any]) -> Path | None:
        file_context = snapshot.get("file_context") or {}
        workspace_dir = file_context.get("workspace_dir")
        run_id = snapshot.get("run_id")
        if not workspace_dir or not run_id:
            return None
        return Path(workspace_dir).expanduser().resolve() / "runs" / str(run_id)

    def dynamic_run_json_path(self, run_dir: Path) -> Path:
        return run_dir / "dynamic_run.json"

    def dynamic_events_path(self, run_dir: Path) -> Path:
        return run_dir / "dynamic_events.jsonl"

    def locate_run_dir(self, run_id: str) -> Path:
        candidates = [
            self.workspace_dir / "runs" / run_id,
            self.run_dir(run_id),
        ]
        if self.workspaces_dir.exists():
            candidates.extend(self.workspaces_dir.glob(f"*/runs/{run_id}"))

        for candidate in candidates:
            if self.dynamic_run_json_path(candidate).exists() or (candidate / "run.json").exists():
                return candidate
        return self.run_dir(run_id)

    def located_run_json_path(self, run_id: str) -> Path:
        run_dir = self.locate_run_dir(run_id)
        dynamic_path = self.dynamic_run_json_path(run_dir)
        if dynamic_path.exists():
            return dynamic_path
        return run_dir / "run.json"

    def located_events_path(self, run_id: str) -> Path:
        run_dir = self.locate_run_dir(run_id)
        dynamic_path = self.dynamic_events_path(run_dir)
        if dynamic_path.exists() or self.dynamic_run_json_path(run_dir).exists():
            return dynamic_path
        return run_dir / "events.jsonl"

    def save_run(self, snapshot: Dict[str, Any]):
        run_id = snapshot["run_id"]
        run_dir = self.workspace_run_dir_from_snapshot(snapshot) or self.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            **to_json_safe(snapshot),
            "schema": "dynamic_run",
            "schema_version": SCHEMA_VERSION,
        }
        target_path = self.dynamic_run_json_path(run_dir) if self.workspace_run_dir_from_snapshot(snapshot) else run_dir / "run.json"
        tmp_path = target_path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, target_path)

    def append_event(self, run_id: str, event: Dict[str, Any], snapshot: Dict[str, Any] | None = None):
        run_dir = self.workspace_run_dir_from_snapshot(snapshot or {}) or self.locate_run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            **to_json_safe(event),
        }
        events_path = self.dynamic_events_path(run_dir) if self.dynamic_run_json_path(run_dir).exists() or snapshot else run_dir / "events.jsonl"
        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    def load_run(self, run_id: str) -> Dict[str, Any]:
        path = self.located_run_json_path(run_id)
        if not path.exists():
            raise ValueError(f"Dynamic run not found: {run_id}")
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def load_events(self, run_id: str, after: int | None = None) -> List[Dict[str, Any]]:
        path = self.located_events_path(run_id)
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
        paths = list(self.runs_dir.glob("*/run.json"))
        paths.extend((self.workspace_dir / "runs").glob("*/dynamic_run.json"))
        if self.workspaces_dir.exists():
            paths.extend(self.workspaces_dir.glob("*/runs/*/dynamic_run.json"))
        seen = set()
        for path in paths:
            try:
                run_id = path.parent.name
                if run_id in seen:
                    continue
                seen.add(run_id)
                with path.open("r", encoding="utf-8") as handle:
                    snapshot = json.load(handle)
                snapshots.append(dynamic_run_summary(snapshot) if summary else snapshot)
            except Exception:
                continue
        snapshots.sort(key=lambda item: item.get("created_time") or 0, reverse=True)
        return snapshots

    def delete_run(self, run_id: str):
        shutil.rmtree(self.locate_run_dir(run_id))

    def recover_interrupted_runs(self) -> List[Dict[str, Any]]:
        recovered = []
        for snapshot in self.list_runs():
            if snapshot.get("status") not in ACTIVE_DYNAMIC_RUN_STATUSES:
                continue

            run_id = snapshot["run_id"]
            now = time.time()
            snapshot["status"] = "interrupted"
            snapshot["finished_time"] = snapshot.get("finished_time") or now
            snapshot["updated_time"] = now
            snapshot["failure_reason"] = snapshot.get("failure_reason") or "Head process restarted before run completed"

            last_seq = int(snapshot.get("last_event_seq") or 0)
            event = {
                "type": "interrupt_dynamic_run",
                "seq": last_seq + 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "schema_version": SCHEMA_VERSION,
                "data": {
                    "run_id": run_id,
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
        status_filter = set(statuses or TERMINAL_DYNAMIC_RUN_STATUSES)
        cutoff = None
        if older_than_days is not None:
            cutoff = time.time() - (float(older_than_days) * 86400)

        candidates = []
        for snapshot in self.list_runs():
            status = snapshot.get("status")
            if status not in status_filter:
                continue
            if status not in TERMINAL_DYNAMIC_RUN_STATUSES:
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
            "runs": [dynamic_run_summary(snapshot) for snapshot in candidates],
            "deleted_run_ids": deleted_run_ids,
        }


def _infer_dynamic_run_mode(snapshot: Dict[str, Any]) -> str:
    final_result = snapshot.get("final_result")
    if isinstance(final_result, dict) and final_result.get("mode"):
        return str(final_result["mode"])

    task_specs = snapshot.get("task_specs") or {}
    if isinstance(task_specs, dict) and "react_llm_decision" in task_specs:
        return "react"

    for event_type in ("agent_run_started", "react_llm_decision"):
        if _event_type_seen(snapshot, event_type):
            return "react" if event_type.startswith("react_") else "agent"

    return "dynamic"


def _event_type_seen(snapshot: Dict[str, Any], event_type: str) -> bool:
    # Run summaries are intentionally derived from run.json only. This helper
    # exists as a future hook for event-indexed summaries without changing the
    # public summary shape.
    return False


def _final_result_summary(final_result: Any) -> Any:
    if not isinstance(final_result, dict):
        return to_json_safe(final_result)

    summary: Dict[str, Any] = {}
    for key in ("mode", "answer", "status", "stop_reason", "step_count", "final_task", "artifacts"):
        if key in final_result:
            summary[key] = to_json_safe(final_result[key])

    if "timings" in final_result and isinstance(final_result["timings"], dict):
        timings = final_result["timings"]
        summary["timings"] = {
            key: to_json_safe(timings[key])
            for key in (
                "total_seconds",
                "task_seconds",
                "llm_seconds",
                "tool_seconds",
                "controller_seconds",
            )
            if key in timings
        }

    return summary or to_json_safe(final_result)


def dynamic_run_summary(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Return a lightweight dynamic-run record for list views."""
    mode = _infer_dynamic_run_mode(snapshot)
    summary = {
        "schema": snapshot.get("schema", "dynamic_run"),
        "schema_version": snapshot.get("schema_version", SCHEMA_VERSION),
        "kind": "dynamic",
        "summary": True,
        "run_id": snapshot.get("run_id"),
        "status": snapshot.get("status"),
        "mode": mode,
        "max_tasks": snapshot.get("max_tasks"),
        "timeout_seconds": snapshot.get("timeout_seconds"),
        "created_time": snapshot.get("created_time"),
        "updated_time": snapshot.get("updated_time"),
        "finished_time": snapshot.get("finished_time"),
        "task_counts": snapshot.get("task_counts") or {},
        "event_count": snapshot.get("event_count") or 0,
        "last_event_seq": snapshot.get("last_event_seq") or 0,
        "cancel_reason": snapshot.get("cancel_reason"),
        "failure_reason": snapshot.get("failure_reason"),
        "final_result": _final_result_summary(snapshot.get("final_result")),
    }
    return to_json_safe(summary)
