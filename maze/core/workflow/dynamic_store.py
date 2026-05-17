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

    def run_dir(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id:
            raise ValueError(f"Invalid dynamic run id: {run_id}")
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
            "schema": "dynamic_run",
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
            raise ValueError(f"Dynamic run not found: {run_id}")
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

    def list_runs(self) -> List[Dict[str, Any]]:
        snapshots = []
        for path in self.runs_dir.glob("*/run.json"):
            try:
                snapshots.append(self.load_run(path.parent.name))
            except Exception:
                continue
        snapshots.sort(key=lambda item: item.get("created_time") or 0, reverse=True)
        return snapshots

    def delete_run(self, run_id: str):
        shutil.rmtree(self.run_dir(run_id))

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
            "runs": candidates,
            "deleted_run_ids": deleted_run_ids,
        }
