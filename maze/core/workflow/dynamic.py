import hashlib
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from maze.core.scheduler.result_summary import to_json_safe
from maze.core.workflow.task import CodeTask


DEFAULT_DYNAMIC_RESOURCES = {"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}
TERMINAL_DYNAMIC_RUN_STATUSES = {"finalized", "failed", "canceled", "timed_out", "interrupted"}


class DynamicTaskSpec:
    def __init__(
        self,
        task_spec_id: str,
        task_name: str,
        code_str: str | None,
        code_ser: str | None,
        inputs: List[Dict[str, Any]] | None = None,
        outputs: List[Dict[str, Any]] | None = None,
        resources: Dict[str, Any] | None = None,
        task_ref_type: str | None = None,
        task_path: str | None = None,
        code_hash: str | None = None,
        code_preview: str | None = None,
    ):
        if code_ser is None and code_str is None:
            raise ValueError("Dynamic task spec requires code_str or code_ser")

        self.task_spec_id = task_spec_id
        self.task_name = task_name or task_spec_id
        self.code_str = code_str
        self.code_ser = code_ser
        self.inputs = _normalize_params(inputs)
        self.outputs = _normalize_params(outputs)
        self.resources = {**DEFAULT_DYNAMIC_RESOURCES, **(resources or {})}
        self.task_ref_type = task_ref_type or "inline"
        self.task_path = task_path
        self.code_hash = code_hash or _hash_code(code_str, code_ser)
        self.code_preview = code_preview or _code_preview(code_str)

    def metadata_snapshot(self) -> Dict[str, Any]:
        payload = {
            "task_spec_id": self.task_spec_id,
            "task_name": self.task_name,
            "task_ref_type": self.task_ref_type,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "resources": self.resources,
            "code_hash": self.code_hash,
            "code_preview": self.code_preview,
            "has_code_str": self.code_str is not None,
            "has_code_ser": self.code_ser is not None,
        }
        if self.task_path:
            payload["task_path"] = self.task_path
        return payload


class DynamicRun:
    def __init__(
        self,
        run_id: str,
        max_tasks: int = 100,
        timeout_seconds: int | None = None,
    ):
        self.run_id = run_id
        self.max_tasks = max_tasks
        self.timeout_seconds = timeout_seconds
        self.created_time = time.time()
        self.updated_time = self.created_time

        self.task_specs: Dict[str, DynamicTaskSpec] = {}
        self.tasks: Dict[str, CodeTask] = {}
        self.task_parents: Dict[str, set[str]] = {}
        self.pending_tasks: Dict[str, CodeTask] = {}
        self.submitted_tasks: set[str] = set()
        self.running_tasks: set[str] = set()
        self.completed_tasks: set[str] = set()
        self.failed_tasks: set[str] = set()
        self.request_ids: Dict[str, str] = {}
        self.event_log: List[Dict[str, Any]] = []
        self.event_seq = 0
        self.status = "created"
        self.finished_time = None
        self.finalized = False
        self.failed = False
        self.final_result = None
        self.cancel_reason = None
        self.failure_reason = None

    def _touch(self):
        self.updated_time = time.time()

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_DYNAMIC_RUN_STATUSES

    def seconds_until_timeout(self) -> float | None:
        if self.timeout_seconds is None or self.is_terminal():
            return None
        return max(0.0, self.timeout_seconds - (time.time() - self.created_time))

    def mark_timed_out_if_needed(self) -> bool:
        if self.timeout_seconds is None or self.is_terminal():
            return False
        if time.time() - self.created_time <= self.timeout_seconds:
            return False

        self.status = "timed_out"
        self.finished_time = time.time()
        self._touch()
        return True

    def _terminal_error(self, action: str) -> ValueError | TimeoutError:
        if self.finalized:
            return ValueError(f"Dynamic run is already finalized and cannot {action}: {self.run_id}")
        if self.failed:
            return ValueError(f"Dynamic run has failed and cannot {action}: {self.run_id}")
        if self.status == "canceled":
            return ValueError(f"Dynamic run is canceled and cannot {action}: {self.run_id}")
        if self.status == "timed_out":
            return TimeoutError(f"Dynamic run timed out and cannot {action}: {self.run_id}")
        return ValueError(f"Dynamic run is terminal ({self.status}) and cannot {action}: {self.run_id}")

    def check_can_mutate(self, action: str = "mutate"):
        self.mark_timed_out_if_needed()
        if self.is_terminal():
            raise self._terminal_error(action)

    def check_can_append(self):
        self.check_can_mutate("append tasks")
        if len(self.tasks) >= self.max_tasks:
            raise ValueError(f"Dynamic run exceeded max_tasks={self.max_tasks}")

    def register_task_spec(self, spec: DynamicTaskSpec) -> DynamicTaskSpec:
        self.check_can_mutate("register task specs")
        self.task_specs[spec.task_spec_id] = spec
        self._touch()
        return spec

    def get_task_for_request_id(self, request_id: str | None) -> CodeTask | None:
        if request_id and request_id in self.request_ids:
            return self.tasks[self.request_ids[request_id]]
        return None

    def resolve_task_spec(
        self,
        task_spec_id: str | None = None,
        inline_task_spec: Dict[str, Any] | None = None,
    ) -> DynamicTaskSpec:
        if inline_task_spec is not None:
            spec = dynamic_task_spec_from_payload(inline_task_spec)
            self.register_task_spec(spec)
            return spec

        if not task_spec_id:
            raise ValueError("append_task requires task_spec_id or task_spec")

        if task_spec_id not in self.task_specs:
            raise ValueError(f"Dynamic task spec not found: {task_spec_id}")

        return self.task_specs[task_spec_id]

    def append_task(
        self,
        spec: DynamicTaskSpec,
        inputs: Dict[str, Any] | None = None,
        parents: List[str] | None = None,
        request_id: str | None = None,
    ) -> tuple[CodeTask, bool]:
        self.check_can_mutate("append tasks")
        existing_task = self.get_task_for_request_id(request_id)
        if existing_task is not None:
            return existing_task, True

        self.check_can_append()

        task_id = str(uuid.uuid4())
        task = CodeTask(self.run_id, task_id, spec.task_name)
        task.dynamic_task_spec_id = spec.task_spec_id
        task.dynamic_request_id = request_id
        task_input, input_parents = build_dynamic_task_input(spec, inputs or {})
        task_output = build_dynamic_task_output(spec)
        task.save_task(
            task_input=task_input,
            task_output=task_output,
            code_str=spec.code_str,
            code_ser=spec.code_ser,
            resources=spec.resources,
        )

        explicit_parents = set(parents or [])
        all_parents = explicit_parents | input_parents
        unknown_parents = all_parents - set(self.tasks)
        if unknown_parents:
            raise ValueError(f"Dynamic task parents not found: {sorted(unknown_parents)}")

        self.tasks[task_id] = task
        self.task_parents[task_id] = all_parents
        if request_id:
            self.request_ids[request_id] = task_id

        if self.is_task_ready(task_id):
            self.submitted_tasks.add(task_id)
        else:
            self.pending_tasks[task_id] = task
        if self.status == "created":
            self.status = "running"
        self._touch()

        return task, False

    def is_task_ready(self, task_id: str) -> bool:
        return self.task_parents.get(task_id, set()).issubset(self.completed_tasks)

    def mark_started(self, task_id: str):
        if self.is_terminal():
            return
        if task_id in self.tasks:
            self.tasks[task_id].mark_started()
        self.running_tasks.add(task_id)
        if self.status == "created":
            self.status = "running"
        self._touch()

    def mark_finished(self, task_id: str) -> List[CodeTask]:
        if self.is_terminal():
            return []
        self.running_tasks.discard(task_id)
        self.completed_tasks.add(task_id)
        if task_id in self.tasks:
            self.tasks[task_id].completed = True
            self.tasks[task_id].finish_time = time.time()

        ready_tasks = []
        for pending_task_id, pending_task in list(self.pending_tasks.items()):
            if self.is_task_ready(pending_task_id):
                ready_tasks.append(pending_task)
                self.submitted_tasks.add(pending_task_id)
                del self.pending_tasks[pending_task_id]

        self._touch()
        return ready_tasks

    def mark_failed(self, task_id: str, reason: str | None = None):
        if self.is_terminal():
            return
        self.running_tasks.discard(task_id)
        self.failed_tasks.add(task_id)
        self.failed = True
        self.status = "failed"
        self.failure_reason = reason
        self.finished_time = time.time()
        self._touch()

    def can_finalize(self) -> bool:
        active_tasks = set(self.tasks) - self.completed_tasks - self.failed_tasks
        return not active_tasks and not self.pending_tasks and not self.running_tasks

    def finalize(self, result: Any = None):
        if self.finalized:
            return
        self.check_can_mutate("finalize")
        if not self.can_finalize():
            raise ValueError("Dynamic run still has active tasks and cannot be finalized")
        self.finalized = True
        self.status = "finalized"
        self.finished_time = time.time()
        self.final_result = result
        self._touch()

    def cancel(self, reason: str | None = None) -> bool:
        if self.status == "canceled":
            return False
        self.check_can_mutate("cancel")
        self.status = "canceled"
        self.cancel_reason = reason
        self.finished_time = time.time()
        self._touch()
        return True

    def interrupt(self, reason: str | None = None):
        if self.is_terminal():
            return False
        self.status = "interrupted"
        self.failure_reason = reason
        self.finished_time = time.time()
        self._touch()
        return True

    def append_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event = dict(event)
        event_data = event.get("data") or {}
        if isinstance(event_data, dict):
            event_data = dict(event_data)
            event_data.setdefault("run_status", self.status)
            event["data"] = event_data

        self.event_seq += 1
        event.setdefault("seq", self.event_seq)
        event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        event.setdefault("schema_version", 1)
        self.event_log.append(event)
        self._touch()
        return event

    def get_events(self, after: int | None = None) -> List[Dict[str, Any]]:
        if after is None:
            return list(self.event_log)
        return [
            event
            for event in self.event_log
            if int(event.get("seq", 0)) > after
        ]

    def snapshot(self, result_summary: Any = None) -> Dict[str, Any]:
        final_result = self.final_result
        if result_summary is not None:
            final_result = result_summary(final_result)

        return {
            "schema": "dynamic_run",
            "schema_version": 1,
            "run_id": self.run_id,
            "status": self.status,
            "max_tasks": self.max_tasks,
            "timeout_seconds": self.timeout_seconds,
            "created_time": self.created_time,
            "updated_time": self.updated_time,
            "finished_time": self.finished_time,
            "task_counts": {
                "total": len(self.tasks),
                "pending": len(self.pending_tasks),
                "submitted": len(self.submitted_tasks),
                "running": len(self.running_tasks),
                "completed": len(self.completed_tasks),
                "failed": len(self.failed_tasks),
            },
            "tasks": {
                "pending": sorted(self.pending_tasks),
                "submitted": sorted(self.submitted_tasks),
                "running": sorted(self.running_tasks),
                "completed": sorted(self.completed_tasks),
                "failed": sorted(self.failed_tasks),
            },
            "task_specs": {
                task_spec_id: spec.metadata_snapshot()
                for task_spec_id, spec in sorted(self.task_specs.items())
            },
            "task_nodes": {
                task_id: self._task_snapshot(task_id, task, result_summary)
                for task_id, task in sorted(self.tasks.items())
            },
            "graph": {
                "nodes": sorted(self.tasks),
                "edges": [
                    {"source": parent_id, "target": task_id}
                    for task_id, parents in sorted(self.task_parents.items())
                    for parent_id in sorted(parents)
                ],
            },
            "request_ids": dict(self.request_ids),
            "event_count": len(self.event_log),
            "last_event_seq": self.event_seq,
            "final_result": final_result,
            "cancel_reason": self.cancel_reason,
            "failure_reason": self.failure_reason,
        }

    def _task_snapshot(
        self,
        task_id: str,
        task: CodeTask,
        result_summary: Any = None,
    ) -> Dict[str, Any]:
        if task_id in self.failed_tasks:
            status = "failed"
        elif task_id in self.completed_tasks:
            status = "completed"
        elif task_id in self.running_tasks:
            status = "running"
        elif task_id in self.submitted_tasks:
            status = "submitted"
        else:
            status = "pending"

        return {
            "task_id": task_id,
            "task_name": task.task_name,
            "task_spec_id": getattr(task, "dynamic_task_spec_id", None),
            "request_id": getattr(task, "dynamic_request_id", None),
            "status": status,
            "parents": sorted(self.task_parents.get(task_id, set())),
            "created_time": task.created_time,
            "start_time": task.start_time,
            "finish_time": task.finish_time,
            "inputs": _task_io_snapshot(task.task_input),
            "outputs": _task_io_snapshot(task.task_output),
            "resources": to_json_safe(task.resources),
        }


def dynamic_task_spec_from_payload(payload: Dict[str, Any]) -> DynamicTaskSpec:
    task_spec_id = payload.get("task_spec_id") or str(uuid.uuid4())
    task_name = payload.get("task_name") or payload.get("name") or task_spec_id
    return DynamicTaskSpec(
        task_spec_id=task_spec_id,
        task_name=task_name,
        code_str=payload.get("code_str"),
        code_ser=payload.get("code_ser"),
        inputs=payload.get("inputs") or payload.get("input_params") or [],
        outputs=payload.get("outputs") or payload.get("output_params") or [],
        resources=payload.get("resources"),
        task_ref_type=payload.get("task_ref_type"),
        task_path=payload.get("task_path") or payload.get("relative_path"),
        code_hash=payload.get("code_hash"),
        code_preview=payload.get("code_preview"),
    )


def _hash_code(code_str: str | None, code_ser: str | None) -> str | None:
    source = code_str if code_str is not None else code_ser
    if source is None:
        return None
    return hashlib.sha256(str(source).encode("utf-8")).hexdigest()


def _code_preview(code_str: str | None, limit: int = 240) -> str | None:
    if not code_str:
        return None
    normalized = code_str.strip()
    if len(normalized) > limit:
        return normalized[: limit - 3] + "..."
    return normalized


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


def _normalize_params(params: Any) -> List[Dict[str, Any]]:
    if not params:
        return []

    normalized = []
    if isinstance(params, dict):
        params = params.values()

    for item in params:
        if isinstance(item, str):
            normalized.append({"name": item, "data_type": "str"})
        elif isinstance(item, dict):
            name = item.get("name") or item.get("key")
            if not name:
                raise ValueError("Task parameter entries require name or key")
            normalized.append({
                "name": name,
                "data_type": item.get("data_type") or item.get("dataType") or "str",
            })
        else:
            raise ValueError(f"Unsupported task parameter entry: {item!r}")

    return normalized


def _is_output_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and (
            value.get("__maze_output_ref__") is True
            or ("task_id" in value and "output_key" in value)
        )
    )


def _output_ref_to_reference(value: Dict[str, Any]) -> str:
    return f"{value['task_id']}.output.{value['output_key']}"


def _known_input_params(spec: DynamicTaskSpec, inputs: Dict[str, Any]) -> List[Dict[str, Any]]:
    known_params = list(spec.inputs)
    known_names = {param["name"] for param in known_params}

    for key in inputs:
        if key not in known_names:
            known_params.append({"name": key, "data_type": "str"})

    return known_params


def build_dynamic_task_input(
    spec: DynamicTaskSpec,
    inputs: Dict[str, Any],
) -> tuple[Dict[str, Any], set[str]]:
    task_input = {"input_params": {}}
    parents = set()

    for idx, input_param in enumerate(_known_input_params(spec, inputs), start=1):
        input_key = input_param["name"]
        has_value = input_key in inputs
        input_value = inputs.get(input_key)

        if _is_output_ref(input_value):
            input_schema = "from_task"
            value = _output_ref_to_reference(input_value)
            parents.add(input_value["task_id"])
            has_value = True
        else:
            input_schema = "from_user"
            value = input_value

        task_input["input_params"][str(idx)] = {
            "key": input_key,
            "input_schema": input_schema,
            "data_type": input_param.get("data_type", "str"),
            "value": value,
            "has_value": has_value,
        }

    return task_input, parents


def build_dynamic_task_output(spec: DynamicTaskSpec) -> Dict[str, Any]:
    task_output = {"output_params": {}}

    for idx, output_param in enumerate(spec.outputs, start=1):
        task_output["output_params"][str(idx)] = {
            "key": output_param["name"],
            "data_type": output_param.get("data_type", "any"),
        }

    return task_output
