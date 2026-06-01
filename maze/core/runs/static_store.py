"""Persistence layer for static workflow runs.

Storage layout (mirrors DynamicRunStore for future merge):
  {workspace}/workflow_runs/static/{run_id}/run.json
  {workspace}/workflow_runs/static/{run_id}/events.jsonl
  {workspace}/workflow_runs/_index.jsonl   (append-only summary index)
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from maze.core.runs.static_snapshot import (
    ACTIVE_STATIC_RUN_STATUSES,
    TERMINAL_STATIC_RUN_STATUSES,
    StaticRunSnapshot,
)
from maze.core.scheduler.result_summary import to_json_safe


SCHEMA_VERSION = 1


def default_workspace_dir() -> Path:
    project_root = Path(__file__).resolve().parents[3]
    return Path(os.environ.get("MAZE_WORKSPACE_DIR", project_root / "workspace")).expanduser().resolve()


class StaticRunStore:
    """File-based persistence for static workflow runs.

    Concurrency: writes use a process-wide lock. Reads do not lock (they
    re-read files each call, so they always see the latest committed state).
    """

    def __init__(self, workspace_dir: str | os.PathLike[str] | None = None):
        self.workspace_dir = (
            Path(workspace_dir).expanduser().resolve() if workspace_dir else default_workspace_dir()
        )
        self.runs_dir = self.workspace_dir / "workflow_runs" / "static"
        self.index_path = self.workspace_dir / "workflow_runs" / "_index.jsonl"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # --- paths --------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id:
            raise ValueError(f"Invalid static run id: {run_id}")
        return self.runs_dir / run_id

    def run_json_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.jsonl"

    # --- write --------------------------------------------------------

    def save_run(self, snapshot: StaticRunSnapshot) -> None:
        run_dir = self.run_dir(snapshot.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            **to_json_safe(snapshot.to_json()),
            "schema": "static_run",
            "schema_version": SCHEMA_VERSION,
        }
        tmp_path = run_dir / "run.json.tmp"
        with self._lock:
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_path, self.run_json_path(snapshot.run_id))

    def append_event(self, run_id: str, event: Dict[str, Any]) -> None:
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            **to_json_safe(event),
        }
        with self._lock:
            with self.events_path(run_id).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                handle.write("\n")

    def append_index_line(self, summary: Dict[str, Any]) -> None:
        line = {
            "schema_version": SCHEMA_VERSION,
            "ts": time.time(),
            **to_json_safe(summary),
        }
        with self._lock:
            with self.index_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True))
                handle.write("\n")

    # --- read ---------------------------------------------------------

    def load_run(self, run_id: str) -> StaticRunSnapshot:
        path = self.run_json_path(run_id)
        if not path.exists():
            raise ValueError(f"Static run not found: {run_id}")
        with path.open("r", encoding="utf-8") as handle:
            return StaticRunSnapshot.from_json(json.load(handle))

    def load_events(self, run_id: str, after: Optional[int] = None) -> List[Dict[str, Any]]:
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

    def list_runs(
        self,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[StaticRunSnapshot]:
        snapshots: List[StaticRunSnapshot] = []
        if not self.runs_dir.exists():
            return snapshots
        for run_json in self.runs_dir.glob("*/run.json"):
            try:
                with run_json.open("r", encoding="utf-8") as handle:
                    snapshots.append(StaticRunSnapshot.from_json(json.load(handle)))
            except Exception:
                continue
        if status:
            snapshots = [s for s in snapshots if s.status == status]
        snapshots.sort(key=lambda s: s.created_time or 0, reverse=True)
        if limit is not None:
            snapshots = snapshots[: max(0, int(limit))]
        return snapshots

    # --- delete / cleanup --------------------------------------------

    def delete_run(self, run_id: str) -> None:
        target = self.run_dir(run_id)
        if target.exists():
            shutil.rmtree(target)

    def recover_interrupted_runs(self) -> List[StaticRunSnapshot]:
        recovered: List[StaticRunSnapshot] = []
        for snapshot in self.list_runs():
            if snapshot.status not in ACTIVE_STATIC_RUN_STATUSES:
                continue
            now = time.time()
            snapshot.status = "interrupted"
            snapshot.finished_time = snapshot.finished_time or now
            snapshot.updated_time = now
            snapshot.failure_reason = (
                snapshot.failure_reason or "Head process restarted before run completed"
            )
            event = {
                "type": "run_interrupted",
                "seq": snapshot.last_event_seq + 1,
                "ts": now,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": {
                    "run_id": snapshot.run_id,
                    "reason": snapshot.failure_reason,
                },
            }
            snapshot.event_count += 1
            snapshot.last_event_seq = event["seq"]
            self.append_event(snapshot.run_id, event)
            self.save_run(snapshot)
            recovered.append(snapshot)
        return recovered

    def cleanup(
        self,
        statuses: Optional[Iterable[str]] = None,
        older_than_days: Optional[float] = None,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        status_filter = set(statuses or TERMINAL_STATIC_RUN_STATUSES)
        cutoff = None
        if older_than_days is not None:
            cutoff = time.time() - (float(older_than_days) * 86400)

        candidates: List[StaticRunSnapshot] = []
        for snap in self.list_runs():
            if snap.status not in status_filter:
                continue
            if snap.status not in TERMINAL_STATIC_RUN_STATUSES:
                continue
            if cutoff is not None:
                ft = snap.finished_time or snap.updated_time
                if not ft or float(ft) > cutoff:
                    continue
            candidates.append(snap)

        deleted: List[str] = []
        if not dry_run:
            for snap in candidates:
                self.delete_run(snap.run_id)
                deleted.append(snap.run_id)

        return {
            "dry_run": dry_run,
            "matched_count": len(candidates),
            "deleted_count": len(deleted),
            "runs": [s.summary_json() for s in candidates],
            "deleted_run_ids": deleted,
        }
