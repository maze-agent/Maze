import copy
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from maze.core.scheduler.result_summary import to_json_safe
from maze.core.workflow.dynamic_store import (
    _EventLogState,
    _event_log_needs_separator,
    _event_payload_json,
    default_workspace_dir,
)


if os.name == "nt":
    import msvcrt
else:
    import fcntl


SCHEMA_VERSION = 1
ACTIVE_STATIC_RUN_STATUSES = {"created", "running"}
TERMINAL_STATIC_RUN_STATUSES = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
FINAL_OUTPUT_REFS_UNSET = object()
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


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
        # Windows does not expose a portable directory FlushFileBuffers API.
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_lock_file(path: Path) -> int:
    _ensure_private_directory(path.parent)
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, PRIVATE_FILE_MODE)
    _set_private_file_descriptor_mode(descriptor)
    os.chmod(path, PRIVATE_FILE_MODE)
    if os.name == "nt" and os.fstat(descriptor).st_size == 0:
        os.write(descriptor, b"\0")
        os.fsync(descriptor)
    return descriptor


def _lock_file(descriptor: int, *, blocking: bool) -> None:
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        msvcrt.locking(descriptor, mode, 1)
        return
    operation = fcntl.LOCK_EX
    if not blocking:
        operation |= fcntl.LOCK_NB
    fcntl.flock(descriptor, operation)


def _unlock_file(descriptor: int) -> None:
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(descriptor, fcntl.LOCK_UN)


class StaticRunStoreLease:
    def __init__(self, path: Path, *, blocking: bool):
        self.path = path
        self.descriptor = _open_lock_file(path)
        try:
            _lock_file(self.descriptor, blocking=blocking)
        except BaseException:
            os.close(self.descriptor)
            self.descriptor = None
            raise

    def release(self) -> None:
        descriptor = self.descriptor
        if descriptor is None:
            return
        self.descriptor = None
        try:
            _unlock_file(descriptor)
        finally:
            os.close(descriptor)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.release()


class FinalOutputResolutionError(ValueError):
    def __init__(self, task_id: str, output_key: str):
        self.task_id = task_id
        self.output_key = output_key
        super().__init__(
            f"Task {task_id} did not return final output {output_key!r}"
        )

    def detail(self, *, finishing_task_id: str) -> Dict[str, Any]:
        return {
            "error_type": "final_output_resolution",
            "message": str(self),
            "details": {
                "finishing_task_id": finishing_task_id,
                "referenced_task_id": self.task_id,
                "output_key": self.output_key,
            },
        }


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_sequence(event: Dict[str, Any]) -> int:
    sequence = event.get("seq")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence <= 0
    ):
        raise ValueError("Static event seq must be a positive integer")
    return sequence


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


def _is_final_output_ref(value: Any) -> bool:
    return isinstance(value, dict) and value.get("__maze_output_ref__") is True


def _validate_final_output_refs(value: Any, workflow: Any):
    if isinstance(value, dict):
        if "__maze_output_ref__" in value:
            if not _is_final_output_ref(value) or set(value) != {
                "__maze_output_ref__",
                "task_id",
                "output_key",
            }:
                raise ValueError("Malformed final output reference")
            task_id = value["task_id"]
            output_key = value["output_key"]
            if not isinstance(task_id, str) or task_id not in workflow.tasks:
                raise ValueError(f"Unknown final output task: {task_id!r}")
            output_params = (workflow.tasks[task_id].task_output or {}).get(
                "output_params",
                {},
            )
            output_names = {item.get("key") for item in output_params.values()}
            if not isinstance(output_key, str) or output_key not in output_names:
                raise ValueError(
                    f"Unknown final output {output_key!r} for task {task_id}"
                )
            return
        for item in value.values():
            _validate_final_output_refs(item, workflow)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_final_output_refs(item, workflow)


def _resolve_final_output_refs(value: Any, task_nodes: Dict[str, Dict[str, Any]]) -> Any:
    if _is_final_output_ref(value):
        result = task_nodes[value["task_id"]].get("result_summary")
        if not isinstance(result, dict) or value["output_key"] not in result:
            raise FinalOutputResolutionError(
                value["task_id"],
                value["output_key"],
            )
        return copy.deepcopy(result[value["output_key"]])
    if isinstance(value, dict):
        return {
            key: _resolve_final_output_refs(item, task_nodes)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_resolve_final_output_refs(item, task_nodes) for item in value]
    return copy.deepcopy(value)


class StaticRun:
    def __init__(
        self,
        run_id: str,
        workflow_id: str,
        workflow: Any,
        timeout_seconds: float | None = None,
        tags: List[str] | None = None,
        metadata: Dict[str, Any] | None = None,
        final_output_refs: Any = FINAL_OUTPUT_REFS_UNSET,
        run_inputs: Dict[str, Any] | None = None,
    ):
        self.run_id = run_id
        self.workflow_id = workflow_id
        self.run_type = "static"
        self.status = "created"
        self.timeout_seconds = timeout_seconds
        self.tags = list(tags or [])
        self.metadata = dict(metadata or {})
        self.run_inputs = copy.deepcopy(run_inputs or {})
        self.created_time = time.time()
        self.submitted_time = None
        self.updated_time = self.created_time
        self.started_time = None
        self.finished_time = None
        self.error_summary = None
        self.result_summary = None
        self.has_final_output_refs = final_output_refs is not FINAL_OUTPUT_REFS_UNSET
        self.final_output_refs = (
            copy.deepcopy(final_output_refs) if self.has_final_output_refs else None
        )
        if self.has_final_output_refs:
            _validate_final_output_refs(self.final_output_refs, workflow)
        self.event_log: List[Dict[str, Any]] = []
        self.event_seq = 0
        self.finish_continuations: Dict[str, Dict[str, Any]] = {}

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
                "task_kind": getattr(task, "task_kind", "cpu"),
                "status": "pending" if parents else "queued",
                "parents": parents,
                "children": children,
                "created_time": task.created_time,
                "started_time": None,
                "finished_time": None,
                "duration_seconds": None,
                "resources": to_json_safe(task.resources),
                "model_anchor": to_json_safe(getattr(task, "model_anchor", None)),
                "inputs": _task_io_snapshot(task.task_input),
                "outputs": _task_io_snapshot(task.task_output),
                "selected_node": None,
                "result_summary": None,
                "error": None,
                "attempt": 0,
                "dispatch_id": None,
                "lease_id": None,
                "last_error": None,
                "pending_reason": None,
                "schedule_decision": None,
                "file_manifest": None,
                "fault_tolerance": {
                    "enabled": True,
                    "status": "idle",
                    "attempts": [],
                },
            }

    def _touch(self):
        self.updated_time = time.time()

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATIC_RUN_STATUSES

    def deadline_time(self) -> float | None:
        if self.timeout_seconds is None:
            return None
        return (self.submitted_time or self.created_time) + float(self.timeout_seconds)

    def seconds_until_timeout(self) -> float | None:
        deadline = self.deadline_time()
        if deadline is None or self.is_terminal():
            return None
        return max(0.0, deadline - time.time())

    def mark_timed_out_if_needed(self) -> bool:
        deadline = self.deadline_time()
        if deadline is None or self.is_terminal() or time.time() <= deadline:
            return False

        now = time.time()
        message = f"Run timed out after {self.timeout_seconds} seconds"
        self.status = "timed_out"
        self.error_summary = {
            "error_type": "timeout",
            "message": message,
            "timeout_seconds": self.timeout_seconds,
            "deadline_time": deadline,
        }
        self.finished_time = now
        for task in self.task_nodes.values():
            if task["status"] not in {"pending", "queued", "running"}:
                continue
            task["status"] = "timed_out"
            task["finished_time"] = now
            task["duration_seconds"] = _duration_seconds(task.get("started_time"), now)
            task["pending_reason"] = None
            task["error"] = {
                "error_type": "timeout",
                "message": message,
            }
        self._touch()
        return True

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
        schedule_decision = (node_info or {}).get("schedule_decision") or {}
        task["status"] = "running"
        task["started_time"] = time.time()
        task["pending_reason"] = None
        task["schedule_decision"] = to_json_safe(schedule_decision)
        if schedule_decision.get("requested_resources"):
            task["resources"] = to_json_safe(schedule_decision.get("requested_resources"))
        task["selected_node"] = {
            "node_id": (node_info or {}).get("node_id"),
            "node_ip": (node_info or {}).get("node_ip"),
            "gpu_id": (node_info or {}).get("gpu_id"),
        }
        task["attempt"] = (node_info or {}).get("attempt") or task.get("attempt") or 1
        task["dispatch_id"] = (node_info or {}).get("dispatch_id")
        task["lease_id"] = (node_info or {}).get("lease_id")
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

    def _update_task_fault_tolerance(self, task: Dict[str, Any], fault_tolerance: Dict[str, Any] | None):
        if fault_tolerance:
            task["fault_tolerance"] = to_json_safe(fault_tolerance)

    def mark_task_retry(
        self,
        task_id: str,
        error: Any = None,
        attempt: int | None = None,
        fault_tolerance: Dict[str, Any] | None = None,
        node_info: Dict[str, Any] | None = None,
    ):
        task = self.task_nodes.get(task_id)
        if not task:
            return
        task["status"] = "queued"
        task["last_error"] = to_json_safe(error)
        task["error"] = None
        task["pending_reason"] = None
        node_info = node_info or {}
        attempt = node_info.get("attempt", attempt)
        if attempt is not None:
            task["attempt"] = attempt
        if node_info.get("dispatch_id") is not None:
            task["dispatch_id"] = node_info["dispatch_id"]
        if node_info.get("lease_id") is not None:
            task["lease_id"] = node_info["lease_id"]
        self._update_task_fault_tolerance(task, fault_tolerance)
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
        fault_tolerance: Dict[str, Any] | None = None,
        attempt: int | None = None,
        dispatch_id: str | None = None,
        lease_id: str | None = None,
    ):
        task = self.task_nodes.get(task_id)
        if not task:
            return
        if task["status"] == "succeeded":
            return
        may_resolve_final_outputs = self.has_final_output_refs and all(
            candidate_id == task_id or candidate.get("status") == "succeeded"
            for candidate_id, candidate in self.task_nodes.items()
        )
        previous_state = (
            copy.deepcopy(self.__dict__) if may_resolve_final_outputs else None
        )
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
        if attempt is not None:
            task["attempt"] = attempt
        if dispatch_id is not None:
            task["dispatch_id"] = dispatch_id
        if lease_id is not None:
            task["lease_id"] = lease_id
        task["metrics"] = to_json_safe(metrics or {})
        task["result_summary"] = to_json_safe(result)
        task["file_manifest"] = to_json_safe(file_manifest)
        task["pending_reason"] = None
        self._update_task_fault_tolerance(task, fault_tolerance)

        for child_id in task.get("children", []):
            child = self.task_nodes.get(child_id)
            if child and child["status"] == "pending" and self._parents_succeeded(child_id):
                child["status"] = "queued"

        if self._all_tasks_finished():
            try:
                self.mark_succeeded()
            except FinalOutputResolutionError as exc:
                assert previous_state is not None
                self.__dict__.clear()
                self.__dict__.update(previous_state)
                error = exc.detail(finishing_task_id=task_id)
                self.mark_task_failed(
                    task_id,
                    error,
                    fault_tolerance=fault_tolerance,
                    attempt=attempt,
                    dispatch_id=dispatch_id,
                    lease_id=lease_id,
                )
                return error
        else:
            self._touch()
        return None

    def mark_task_failed(
        self,
        task_id: str,
        error: Any,
        file_manifest: Dict[str, Any] | None = None,
        fault_tolerance: Dict[str, Any] | None = None,
        attempt: int | None = None,
        dispatch_id: str | None = None,
        lease_id: str | None = None,
    ):
        task = self.task_nodes.get(task_id)
        if task:
            error_type = error.get("error_type") if isinstance(error, dict) else None
            task["status"] = "timed_out" if error_type == "timeout" else "failed"
            task["finished_time"] = time.time()
            task["duration_seconds"] = _duration_seconds(task.get("started_time"), task.get("finished_time"))
            task["error"] = to_json_safe(error)
            task["last_error"] = to_json_safe(error)
            if isinstance(file_manifest, dict) and file_manifest.get("published") is True:
                task["file_manifest"] = to_json_safe(file_manifest)
            if attempt is not None:
                task["attempt"] = attempt
            if dispatch_id is not None:
                task["dispatch_id"] = dispatch_id
            if lease_id is not None:
                task["lease_id"] = lease_id
            self._update_task_fault_tolerance(task, fault_tolerance)
        self.status = "timed_out" if isinstance(error, dict) and error.get("error_type") == "timeout" else "failed"
        self.error_summary = to_json_safe(error)
        self.finished_time = time.time()
        self._touch()

    def mark_succeeded(self):
        if self.status == "succeeded":
            return
        if self.has_final_output_refs:
            result_summary = _resolve_final_output_refs(
                self.final_output_refs,
                self.task_nodes,
            )
        else:
            result_summary = {
                task_id: task.get("result_summary")
                for task_id, task in sorted(self.task_nodes.items())
                if task.get("status") == "succeeded"
            }
        self.status = "succeeded"
        self.finished_time = time.time()
        self.result_summary = result_summary
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
            return False
        now = time.time()
        message = reason or "Head process restarted before run completed"
        self.status = "interrupted"
        self.error_summary = {"message": message}
        self.finished_time = now
        for task in self.task_nodes.values():
            if task["status"] not in {"pending", "queued", "running"}:
                continue
            task["status"] = "cancelled"
            task["finished_time"] = now
            task["duration_seconds"] = _duration_seconds(task.get("started_time"), now)
            task["pending_reason"] = None
            task["error"] = {"error_type": "interrupted", "message": message}
        self._touch()
        return True

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
        event["seq"] = self.event_seq
        event["timestamp"] = _utc_timestamp()
        event["schema_version"] = SCHEMA_VERSION
        self.event_log.append(event)
        self._touch()
        return event

    def get_events(self, after: int | None = None) -> List[Dict[str, Any]]:
        if after is None:
            return list(self.event_log)
        return [event for event in self.event_log if int(event.get("seq", 0)) > after]

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
            "deadline_time": self.deadline_time(),
            "tags": self.tags,
            "metadata": self.metadata,
            "run_inputs": copy.deepcopy(self.run_inputs),
            "created_time": self.created_time,
            "submitted_time": self.submitted_time,
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
            "finish_continuations": self.finish_continuations,
            "result_summary": self.result_summary,
            "final_output_refs": to_json_safe(self.final_output_refs),
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
        _ensure_private_directory(self.runs_dir)
        self._event_log_states: Dict[Path, _EventLogState] = {}

    def run_dir(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id:
            raise ValueError(f"Invalid static run id: {run_id}")
        return self.runs_dir / run_id

    def run_json_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.jsonl"

    def acquire_core_process_lease(self) -> StaticRunStoreLease:
        try:
            return StaticRunStoreLease(
                self.runs_dir / ".maze_core_process.lock",
                blocking=False,
            )
        except OSError as exc:
            raise RuntimeError(
                f"Another Maze Core process owns workflow store {self.runs_dir}"
            ) from exc

    def save_run(self, snapshot: Dict[str, Any]):
        run_id = snapshot["run_id"]
        run_dir = self.run_dir(run_id)
        run_dir_existed = run_dir.exists()
        _ensure_private_directory(run_dir)
        if not run_dir_existed:
            _fsync_directory(self.runs_dir)
        payload = {
            **to_json_safe(snapshot),
            "schema": "static_run",
            "schema_version": SCHEMA_VERSION,
        }
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=run_dir,
                prefix=".run.json.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                _set_private_file_descriptor_mode(handle.fileno())
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, PRIVATE_FILE_MODE)
            target_path = self.run_json_path(run_id)
            os.replace(tmp_path, target_path)
            tmp_path = None
            os.chmod(target_path, PRIVATE_FILE_MODE)
            _fsync_directory(run_dir)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

        if snapshot.get("status") in TERMINAL_STATIC_RUN_STATUSES:
            self._event_log_states.pop(self.events_path(run_id), None)

    def _event_log_state(self, run_id: str) -> _EventLogState:
        events_path = self.events_path(run_id)
        try:
            cached = self._event_log_states.get(events_path)
            if cached is not None:
                return cached

            events = self.load_events(run_id)
            state = _EventLogState(
                events={_event_sequence(event): event for event in events},
                last_sequence=(_event_sequence(events[-1]) if events else 0),
                needs_separator=_event_log_needs_separator(events_path),
            )
            self._event_log_states[events_path] = state
            return state
        except BaseException:
            self._event_log_states.pop(events_path, None)
            raise

    @staticmethod
    def _events_match(
        persisted: Dict[str, Any],
        event: Dict[str, Any],
    ) -> bool:
        return _event_payload_json(
            persisted,
            ignore_timestamp=True,
        ) == _event_payload_json(
            event,
            ignore_timestamp=True,
        )

    def _write_event(
        self,
        run_id: str,
        payload: Dict[str, Any],
        *,
        state: _EventLogState | None,
        needs_separator: bool,
    ) -> Dict[str, Any]:
        run_dir = self.run_dir(run_id)
        run_dir_existed = run_dir.exists()
        _ensure_private_directory(run_dir)
        if not run_dir_existed:
            _fsync_directory(self.runs_dir)
        events_path = self.events_path(run_id)
        events_existed = events_path.exists()
        descriptor = os.open(
            str(events_path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            PRIVATE_FILE_MODE,
        )
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            _set_private_file_descriptor_mode(handle.fileno())
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if needs_separator:
                handle.write("\n")
            handle.write(serialized)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(events_path, PRIVATE_FILE_MODE)
        if not events_existed:
            _fsync_directory(run_dir)

        stored_event = json.loads(serialized)
        if state is None or self._event_log_states.get(events_path) is not state:
            self._event_log_states.pop(events_path, None)
            return stored_event
        sequence = _event_sequence(stored_event)
        state.events[sequence] = stored_event
        state.last_sequence = sequence
        state.needs_separator = False
        return stored_event

    def append_event(self, run_id: str, event: Dict[str, Any]):
        _event_sequence(event)
        payload = {
            "schema_version": SCHEMA_VERSION,
            **to_json_safe(event),
        }
        events_path = self.events_path(run_id)
        try:
            self._write_event(
                run_id,
                payload,
                state=None,
                needs_separator=_event_log_needs_separator(events_path),
            )
        except BaseException:
            self._event_log_states.pop(events_path, None)
            raise

    def append_event_once(self, run_id: str, event: Dict[str, Any]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            **to_json_safe(event),
        }
        sequence = _event_sequence(payload)
        events_path = self.events_path(run_id)
        try:
            state = self._event_log_state(run_id)
            persisted = state.events.get(sequence)
            if persisted is not None:
                if not self._events_match(persisted, payload):
                    raise RuntimeError(
                        "Persisted event sequence conflict for run "
                        f"{run_id}: {sequence}"
                    )
                return
            if sequence <= state.last_sequence:
                raise ValueError(
                    "Non-monotonic static event sequence "
                    f"{sequence} for run {run_id}"
                )
            self._write_event(
                run_id,
                payload,
                state=state,
                needs_separator=state.needs_separator,
            )
        except BaseException:
            self._event_log_states.pop(events_path, None)
            raise

    def load_run(self, run_id: str) -> Dict[str, Any]:
        path = self.run_json_path(run_id)
        if not path.exists():
            raise ValueError(f"Static run not found: {run_id}")
        os.chmod(path.parent, PRIVATE_DIR_MODE)
        os.chmod(path, PRIVATE_FILE_MODE)
        with path.open("r", encoding="utf-8") as handle:
            _set_private_file_descriptor_mode(handle.fileno())
            return json.load(handle)

    def load_events(self, run_id: str, after: int | None = None) -> List[Dict[str, Any]]:
        path = self.events_path(run_id)
        if not path.exists():
            return []

        os.chmod(path.parent, PRIVATE_DIR_MODE)
        os.chmod(path, PRIVATE_FILE_MODE)
        events = []
        previous_sequence = None
        with path.open("r", encoding="utf-8") as handle:
            _set_private_file_descriptor_mode(handle.fileno())
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                sequence = _event_sequence(event)
                if (
                    previous_sequence is not None
                    and sequence <= previous_sequence
                ):
                    raise ValueError(
                        "Non-monotonic static event sequence "
                        f"{sequence} for run {run_id} at line {line_number}"
                    )
                previous_sequence = sequence
                if after is None or sequence > after:
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

    def recover_interrupted_runs(self) -> List[Dict[str, Any]]:
        recovered = []
        for snapshot in self.list_runs():
            if snapshot.get("status") not in ACTIVE_STATIC_RUN_STATUSES:
                continue

            run_id = snapshot["run_id"]
            persisted_events = self.load_events(run_id)
            interrupt_events = [
                event
                for event in persisted_events
                if event.get("type") == "interrupt_workflow"
            ]
            if len(interrupt_events) > 1:
                raise ValueError(
                    f"Duplicate interrupt_workflow event for run {run_id}"
                )
            dispatch = snapshot.get("dispatch")
            if dispatch is not None and (
                not isinstance(dispatch, dict)
                or dispatch.get("schema_version") != 1
                or dispatch.get("status")
                not in {
                    "prepared",
                    "dispatching",
                    "active",
                    "cleanup_pending",
                    "terminal",
                }
            ):
                raise ValueError(f"Invalid workflow dispatch state for run {run_id}")
            if (
                isinstance(dispatch, dict)
                and dispatch.get("status")
                in {"prepared", "dispatching", "cleanup_pending"}
            ):
                if interrupt_events:
                    raise ValueError(
                        "Incomplete workflow dispatch has a terminal "
                        f"interrupt event for run {run_id}"
                    )
                # Core owns cleanup because a task may already be in Scheduler.
                continue

            artifact_reservation = snapshot.get("artifact_reservation")
            if (
                isinstance(artifact_reservation, dict)
                and artifact_reservation.get("status") == "pending"
            ):
                from maze.core.files.artifact_store import LocalCASArtifactStore

                LocalCASArtifactStore(
                    artifact_reservation.get("artifact_store_root")
                ).revoke_owner_capabilities(
                    artifact_reservation.get("owner_id")
                )
                artifact_reservation["status"] = "revoked"
            idempotency_initialization = snapshot.get(
                "idempotency_initialization"
            )
            if (
                isinstance(idempotency_initialization, dict)
                and idempotency_initialization.get("status") == "initializing"
                and idempotency_initialization.get("artifact_owner_id")
                and idempotency_initialization.get("artifact_status")
                in {"pending", "ready"}
                and all(
                    state == "pending"
                    for state in (
                        idempotency_initialization.get("root_dispatch") or {}
                    ).values()
                )
            ):
                from maze.core.files.artifact_store import LocalCASArtifactStore

                LocalCASArtifactStore(
                    idempotency_initialization.get("artifact_store_root")
                ).revoke_owner_capabilities(
                    idempotency_initialization.get("artifact_owner_id")
                )
                idempotency_initialization["artifact_status"] = "revoked"
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

            event_count = len(persisted_events)
            last_seq = max(
                (int(event.get("seq", 0)) for event in persisted_events),
                default=0,
            )
            interrupt_event = next(
                (
                    event
                    for event in reversed(persisted_events)
                    if event.get("type") == "interrupt_workflow"
                ),
                None,
            )
            if interrupt_event is None:
                interrupt_event = {
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
                self.append_event(run_id, interrupt_event)
                event_count += 1
                last_seq = int(interrupt_event["seq"])

            snapshot["event_count"] = event_count
            snapshot["last_event_seq"] = last_seq
            self.save_run(snapshot)
            recovered.append(snapshot)
        return recovered

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
        "submitted_time": snapshot.get("submitted_time"),
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
