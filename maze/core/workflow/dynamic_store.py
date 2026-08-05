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
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
TERMINAL_DYNAMIC_EVENT_STATUSES = {
    "finish_workflow": "finalized",
    "task_exception": "failed",
    "cancel_dynamic_run": "canceled",
    "timeout_dynamic_run": "timed_out",
    "interrupt_dynamic_run": "interrupted",
}


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path, PRIVATE_DIR_MODE)


def _set_private_file_descriptor_mode(descriptor: int) -> None:
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, PRIVATE_FILE_MODE)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def default_workspace_dir() -> Path:
    project_root = Path(__file__).resolve().parents[3]
    return Path(os.environ.get("MAZE_WORKSPACE_DIR", project_root / "workspaces" / "default")).expanduser().resolve()


class DynamicRunStore:
    def __init__(self, workspace_dir: str | os.PathLike[str] | None = None):
        self.workspace_dir = Path(workspace_dir).expanduser().resolve() if workspace_dir else default_workspace_dir()
        self.runs_dir = self.workspace_dir / "workflow_runs" / "dynamic"
        _ensure_private_directory(self.runs_dir)
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
        payload = {
            **to_json_safe(snapshot),
            "schema": "dynamic_run",
            "schema_version": SCHEMA_VERSION,
        }
        canonical_path = self.run_json_path(run_id)
        target_paths = [canonical_path]
        workspace_run_dir = self.workspace_run_dir_from_snapshot(snapshot)
        if workspace_run_dir is not None:
            target_paths.append(self.dynamic_run_json_path(workspace_run_dir))

        for target_path in dict.fromkeys(target_paths):
            parent_existed = target_path.parent.exists()
            _ensure_private_directory(target_path.parent)
            if not parent_existed and target_path.parent.parent.exists():
                _fsync_directory(target_path.parent.parent)
            tmp_path = target_path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
            try:
                descriptor = os.open(
                    str(tmp_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    PRIVATE_FILE_MODE,
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    _set_private_file_descriptor_mode(handle.fileno())
                    json.dump(
                        payload,
                        handle,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp_path, PRIVATE_FILE_MODE)
                os.replace(tmp_path, target_path)
                os.chmod(target_path, PRIVATE_FILE_MODE)
                _fsync_directory(target_path.parent)
            finally:
                tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _event_sequence(event: Dict[str, Any]) -> int:
        sequence = event.get("seq")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
        ):
            raise ValueError("Dynamic event seq must be a positive integer")
        return sequence

    @staticmethod
    def _load_events_path(events_path: Path) -> List[Dict[str, Any]]:
        if not events_path.exists():
            return []
        os.chmod(events_path.parent, PRIVATE_DIR_MODE)
        os.chmod(events_path, PRIVATE_FILE_MODE)
        events = []
        seen_sequences = set()
        previous_sequence = None
        with events_path.open("r", encoding="utf-8") as handle:
            _set_private_file_descriptor_mode(handle.fileno())
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                sequence = DynamicRunStore._event_sequence(event)
                if sequence in seen_sequences:
                    raise ValueError(
                        "Duplicate dynamic event sequence "
                        f"{sequence} in {events_path} at line {line_number}"
                    )
                if previous_sequence is not None and sequence <= previous_sequence:
                    raise ValueError(
                        "Non-monotonic dynamic event sequence "
                        f"{sequence} in {events_path} at line {line_number}"
                    )
                seen_sequences.add(sequence)
                previous_sequence = sequence
                events.append(event)
        return events

    @classmethod
    def _event_is_persisted(
        cls,
        events_path: Path,
        event: Dict[str, Any],
    ) -> bool:
        events = cls._load_events_path(events_path)
        expected_seq = cls._event_sequence(event)
        for candidate in events:
            if cls._event_sequence(candidate) != expected_seq:
                continue
            if candidate != event:
                raise ValueError(
                    "Conflicting dynamic event sequence "
                    f"{expected_seq} in {events_path}"
                )
            return True
        if events and expected_seq <= max(cls._event_sequence(item) for item in events):
            raise ValueError(
                "Non-monotonic dynamic event sequence "
                f"{expected_seq} in {events_path}"
            )
        return False

    def append_event(
        self,
        run_id: str,
        event: Dict[str, Any],
        snapshot: Dict[str, Any] | None = None,
        *,
        deduplicate: bool = False,
    ):
        payload = {
            "schema_version": SCHEMA_VERSION,
            **to_json_safe(event),
        }
        target_paths = [self.events_path(run_id)]
        workspace_run_dir = self.workspace_run_dir_from_snapshot(snapshot or {})
        if workspace_run_dir is not None:
            target_paths.append(self.dynamic_events_path(workspace_run_dir))

        for events_path in dict.fromkeys(target_paths):
            persisted = self._event_is_persisted(events_path, payload)
            if persisted:
                if deduplicate:
                    continue
                raise ValueError(
                    "Duplicate dynamic event sequence "
                    f"{self._event_sequence(payload)} in {events_path}"
                )
            parent_existed = events_path.parent.exists()
            _ensure_private_directory(events_path.parent)
            if not parent_existed and events_path.parent.parent.exists():
                _fsync_directory(events_path.parent.parent)
            events_existed = events_path.exists()
            descriptor = os.open(
                str(events_path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                PRIVATE_FILE_MODE,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                _set_private_file_descriptor_mode(handle.fileno())
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(events_path, PRIVATE_FILE_MODE)
            if not events_existed:
                _fsync_directory(events_path.parent)

    def load_run(self, run_id: str) -> Dict[str, Any]:
        canonical_path = self.run_json_path(run_id)
        path = (
            canonical_path
            if canonical_path.exists()
            else self.located_run_json_path(run_id)
        )
        if not path.exists():
            raise ValueError(f"Dynamic run not found: {run_id}")
        os.chmod(path.parent, PRIVATE_DIR_MODE)
        os.chmod(path, PRIVATE_FILE_MODE)
        with path.open("r", encoding="utf-8") as handle:
            _set_private_file_descriptor_mode(handle.fileno())
            return json.load(handle)

    def load_events(self, run_id: str, after: int | None = None) -> List[Dict[str, Any]]:
        canonical_path = self.events_path(run_id)
        path = (
            canonical_path
            if canonical_path.exists()
            else self.located_events_path(run_id)
        )
        events = self._load_events_path(path)
        if after is None:
            return events
        return [event for event in events if self._event_sequence(event) > after]

    def load_canonical_events(
        self,
        run_id: str,
        after: int | None = None,
    ) -> List[Dict[str, Any]]:
        events = self._load_events_path(self.events_path(run_id))
        if after is None:
            return events
        return [event for event in events if self._event_sequence(event) > after]

    @classmethod
    def _terminal_event_for_recovery(
        cls,
        run_id: str,
        events: List[Dict[str, Any]],
    ) -> tuple[Dict[str, Any], str] | None:
        candidates = [
            (event, TERMINAL_DYNAMIC_EVENT_STATUSES[event["type"]])
            for event in events
            if event.get("type") in TERMINAL_DYNAMIC_EVENT_STATUSES
        ]
        if len(candidates) > 1:
            raise ValueError(f"Multiple terminal dynamic events for run {run_id}")
        if not candidates:
            return None

        event, status = candidates[0]
        data = event.get("data")
        if (
            not isinstance(data, dict)
            or data.get("run_id") != run_id
            or data.get("run_status") != status
        ):
            raise ValueError(f"Invalid terminal dynamic event for run {run_id}")
        if cls._event_sequence(event) != max(
            cls._event_sequence(candidate) for candidate in events
        ):
            raise ValueError(f"Terminal dynamic event is not last for run {run_id}")
        timestamp = event.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp:
            raise ValueError(f"Terminal dynamic event has no timestamp for run {run_id}")
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"Terminal dynamic event has an invalid timestamp for run {run_id}"
            ) from exc
        if parsed_timestamp.tzinfo is None:
            raise ValueError(
                f"Terminal dynamic event timestamp has no timezone for run {run_id}"
            )
        return event, status

    @staticmethod
    def _fail_snapshot_tasks(
        snapshot: Dict[str, Any],
        task_ids: set[str],
        error: Any,
        finished_time: float,
    ) -> None:
        if not task_ids:
            return
        tasks = snapshot.get("tasks")
        if isinstance(tasks, dict):
            for active_status in ("pending", "submitted", "running"):
                values = tasks.get(active_status)
                if isinstance(values, list):
                    tasks[active_status] = [
                        task_id for task_id in values if task_id not in task_ids
                    ]
            failed = set(tasks.get("failed") or [])
            failed.update(task_ids)
            tasks["failed"] = sorted(failed)

        task_errors = snapshot.setdefault("task_errors", {})
        if isinstance(task_errors, dict):
            for task_id in task_ids:
                task_errors[task_id] = to_json_safe(error)
        task_nodes = snapshot.get("task_nodes")
        if isinstance(task_nodes, dict):
            for task_id in task_ids:
                task = task_nodes.get(task_id)
                if not isinstance(task, dict):
                    continue
                task["status"] = "failed"
                task["finish_time"] = finished_time
                task["error"] = to_json_safe(error)

        task_counts = snapshot.get("task_counts")
        if isinstance(task_counts, dict) and isinstance(tasks, dict):
            for status in ("pending", "submitted", "running", "completed", "failed"):
                values = tasks.get(status)
                if isinstance(values, list):
                    task_counts[status] = len(values)

    @classmethod
    def _apply_terminal_event_to_snapshot(
        cls,
        snapshot: Dict[str, Any],
        event: Dict[str, Any],
        status: str,
        events: List[Dict[str, Any]],
    ) -> None:
        data = event["data"]
        event_time = datetime.fromisoformat(
            event["timestamp"].replace("Z", "+00:00")
        ).timestamp()
        snapshot["status"] = status
        snapshot["finished_time"] = event_time
        snapshot["updated_time"] = event_time

        if status == "finalized":
            snapshot["final_result"] = to_json_safe(data.get("result"))
        elif status == "canceled":
            snapshot["cancel_reason"] = data.get("reason")
        elif status == "failed":
            error = data.get("error", data.get("result"))
            snapshot["failure_reason"] = to_json_safe(error)
            task_id = data.get("task_id")
            if isinstance(task_id, str) and task_id:
                cls._fail_snapshot_tasks(snapshot, {task_id}, error, event_time)
                file_manifest = data.get("file_manifest")
                if isinstance(file_manifest, dict):
                    snapshot.setdefault("task_file_manifests", {})[task_id] = (
                        to_json_safe(file_manifest)
                    )
                fault_tolerance = data.get("fault_tolerance")
                if isinstance(fault_tolerance, dict):
                    snapshot.setdefault("task_fault_tolerance", {})[task_id] = (
                        to_json_safe(fault_tolerance)
                    )
        elif status in {"timed_out", "interrupted"}:
            active_task_ids = set()
            tasks = snapshot.get("tasks")
            if isinstance(tasks, dict):
                for active_status in ("pending", "submitted", "running"):
                    values = tasks.get(active_status)
                    if isinstance(values, list):
                        active_task_ids.update(
                            task_id for task_id in values if isinstance(task_id, str)
                        )
            if status == "timed_out":
                timeout_seconds = data.get("timeout_seconds")
                error = {
                    "error_type": "timeout",
                    "message": f"Dynamic run timed out after {timeout_seconds} seconds",
                }
                snapshot["failure_reason"] = to_json_safe(error)
            else:
                reason = data.get("reason")
                error = {
                    "error_type": "interrupted",
                    "message": reason
                    or "Scheduler process exited before the dynamic run completed",
                }
                snapshot["failure_reason"] = reason
            cls._fail_snapshot_tasks(
                snapshot,
                active_task_ids,
                error,
                event_time,
            )

        snapshot["event_count"] = len(events)
        snapshot["last_event_seq"] = max(
            (cls._event_sequence(candidate) for candidate in events),
            default=0,
        )

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
                os.chmod(path.parent, PRIVATE_DIR_MODE)
                os.chmod(path, PRIVATE_FILE_MODE)
                with path.open("r", encoding="utf-8") as handle:
                    _set_private_file_descriptor_mode(handle.fileno())
                    snapshot = json.load(handle)
                snapshots.append(dynamic_run_summary(snapshot) if summary else snapshot)
            except Exception:
                continue
        snapshots.sort(key=lambda item: item.get("created_time") or 0, reverse=True)
        return snapshots

    def delete_run(self, run_id: str):
        snapshot = self.load_run(run_id)
        run_dirs = {self.run_dir(run_id)}
        workspace_run_dir = self.workspace_run_dir_from_snapshot(snapshot)
        if workspace_run_dir is not None:
            run_dirs.add(workspace_run_dir)
        for run_dir in run_dirs:
            if run_dir.exists():
                shutil.rmtree(run_dir)

    def recover_interrupted_runs(self) -> List[Dict[str, Any]]:
        recovered = []
        for snapshot in self.list_runs():
            if snapshot.get("status") not in ACTIVE_DYNAMIC_RUN_STATUSES:
                continue

            run_id = snapshot["run_id"]
            persisted_events = self.load_canonical_events(run_id)
            terminal = self._terminal_event_for_recovery(run_id, persisted_events)
            if terminal is not None:
                terminal_event, terminal_status = terminal
                self._apply_terminal_event_to_snapshot(
                    snapshot,
                    terminal_event,
                    terminal_status,
                    persisted_events,
                )
                for persisted_event in persisted_events:
                    self.append_event(
                        run_id,
                        persisted_event,
                        snapshot=snapshot,
                        deduplicate=True,
                    )
                self.save_run(snapshot)
                recovered.append(snapshot)
                continue

            now = time.time()
            snapshot["status"] = "interrupted"
            snapshot["finished_time"] = snapshot.get("finished_time") or now
            snapshot["updated_time"] = now
            snapshot["failure_reason"] = snapshot.get("failure_reason") or "Head process restarted before run completed"

            # The canonical log is authoritative. Workspace logs are mirrors
            # repaired from its events during the idempotent append below.
            last_seq = max(
                (self._event_sequence(event) for event in persisted_events),
                default=0,
            )
            interrupt_event = next(
                (
                    event
                    for event in reversed(persisted_events)
                    if event.get("type") == "interrupt_dynamic_run"
                ),
                None,
            )
            if interrupt_event is None:
                interrupt_event = {
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

            # Recovery may be replaying after the canonical append was fsynced
            # but the snapshot (or a workspace mirror) failed to persist. Replay
            # every canonical event idempotently so a lagging mirror is filled in
            # without ever accepting a conflicting sequence.
            events_to_sync = list(persisted_events)
            if interrupt_event not in events_to_sync:
                events_to_sync.append(interrupt_event)
            for persisted_event in events_to_sync:
                self.append_event(
                    run_id,
                    persisted_event,
                    snapshot=snapshot,
                    deduplicate=True,
                )
            persisted_events = self.load_canonical_events(run_id)
            snapshot["event_count"] = len(persisted_events)
            snapshot["last_event_seq"] = max(
                (self._event_sequence(event) for event in persisted_events),
                default=0,
            )
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

    return "dynamic"


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
