from asyncio.queues import Queue
import math
import os
import time
import uuid
import httpx
import json
import copy
import logging
import ray
import zmq
import zmq.asyncio
import asyncio
import multiprocessing as mp
import queue
import socket
import subprocess
from fastapi import WebSocket
from pathlib import Path
from typing import Any,Dict,List
from asyncio.queues import Queue
from maze.core.workflow.task import CodeTask, LangGraphTask,TaskType
from maze.core.workflow.workflow import Workflow,LangGraphWorkflow
from maze.core.workflow.dynamic import DynamicRun, TERMINAL_DYNAMIC_RUN_STATUSES, dynamic_task_spec_from_payload
from maze.core.workflow.dynamic_store import DynamicRunStore
from maze.core.workflow.dag_spec import build_dag_workflow
from maze.core.workflow.static_run import (
    FINAL_OUTPUT_REFS_UNSET,
    StaticRun,
    StaticRunStore,
    static_run_summary,
)
from maze.core.application.spec import build_app_workflow
from maze.core.runs import GlobalMetrics
from maze.core.scheduler.scheduler import scheduler_process, stop_ray_runtime
from maze.core.scheduler.llm_instance import (
    stop_llm_owner_processes_locally,
    stop_llm_owner_processes_on_cluster,
)
from maze.core.scheduler.result_summary import summarize_task_result
from maze.core.files.artifact_store import LocalCASArtifactStore
from maze.core.files.lineage import ArtifactError
from maze.utils.utils import get_available_ports

logger = logging.getLogger(__name__)
EPSILON = 1e-3
RUN_INPUT_REF_MARKER = "__maze_run_input__"
SCHEDULER_START_TIMEOUT_SECONDS = 60.0
SCHEDULER_START_POLL_SECONDS = 0.1
SCHEDULER_RESPONSE_TIMEOUT_SECONDS = 600.0
SCHEDULER_FATAL_EXIT_GRACE_SECONDS = 90.0
SCHEDULER_FATAL_TERMINATE_TIMEOUT_SECONDS = 5.0
SCHEDULER_FATAL_KILL_TIMEOUT_SECONDS = 5.0
SCHEDULER_PROCESS_EXIT_POLL_SECONDS = 0.05


class SchedulerUnavailableError(RuntimeError):
    error_code = "scheduler_unavailable"

    def __init__(
        self,
        message: str,
        *,
        pid: int | None = None,
        exitcode: int | None = None,
    ):
        super().__init__(message)
        self.pid = pid
        self.exitcode = exitcode

    def detail(self) -> Dict[str, Any]:
        return {
            "code": self.error_code,
            "message": str(self),
            "scheduler_pid": self.pid,
            "scheduler_exitcode": self.exitcode,
        }


def _run_input_ref(value: Any) -> Dict[str, Any] | None:
    if not isinstance(value, dict) or RUN_INPUT_REF_MARKER not in value:
        return None
    if (
        value.get(RUN_INPUT_REF_MARKER) is not True
        or set(value) != {RUN_INPUT_REF_MARKER, "key"}
        or not isinstance(value.get("key"), str)
        or not value["key"]
    ):
        raise ValueError("Malformed workflow run input reference")
    return value


def _resolve_run_input_refs(
    value: Any,
    inputs: Dict[str, Any],
) -> tuple[Any, set[str]]:
    reference = _run_input_ref(value)
    if reference is not None:
        key = reference["key"]
        if key not in inputs:
            raise ValueError(f"Workflow run input contract mismatch: {key}")
        return copy.deepcopy(inputs[key]), {key}
    if isinstance(value, dict):
        resolved = {}
        referenced = set()
        for key, item in value.items():
            resolved_item, item_references = _resolve_run_input_refs(item, inputs)
            resolved[key] = resolved_item
            referenced.update(item_references)
        return resolved, referenced
    elif isinstance(value, (list, tuple)):
        resolved = []
        referenced = set()
        for item in value:
            resolved_item, item_references = _resolve_run_input_refs(item, inputs)
            resolved.append(resolved_item)
            referenced.update(item_references)
        return resolved, referenced
    return copy.deepcopy(value), set()


def _bind_workflow_run_inputs(workflow: Workflow, inputs: Dict[str, Any] | None) -> Dict[str, Any]:
    if inputs is None:
        inputs = {}
    if not isinstance(inputs, dict):
        raise TypeError("run inputs must be a dictionary")
    if not all(isinstance(key, str) for key in inputs):
        raise TypeError("run input names must be strings")

    contract = workflow.graph.graph.get("workflow_input_contract")
    if contract is None:
        constants = set()
        runtime = {}
    else:
        constants = set(contract["constants"])
        runtime = contract["runtime"]

    provided = set(inputs)
    conflicts = sorted(provided & constants)
    unknown = sorted(provided - constants - set(runtime))
    missing = sorted(
        key
        for key, spec in runtime.items()
        if spec["required"] and key not in provided
    )
    if conflicts:
        raise ValueError(
            "Run inputs cannot override template constants: " + ", ".join(conflicts)
        )
    if unknown:
        raise ValueError("Unknown workflow run inputs: " + ", ".join(unknown))
    if missing:
        raise ValueError("Missing workflow run inputs: " + ", ".join(missing))

    effective_inputs = {
        key: copy.deepcopy(inputs[key] if key in inputs else spec["default"])
        for key, spec in runtime.items()
    }
    referenced = set()
    for task in workflow.tasks.values():
        for input_info in (task.task_input or {}).get("input_params", {}).values():
            if input_info.get("input_schema") != "from_run":
                continue
            resolved, references = _resolve_run_input_refs(
                input_info["value"],
                effective_inputs,
            )
            if not references:
                raise ValueError("Malformed workflow run input reference")
            referenced.update(references)
            input_info["value"] = resolved
            input_info["input_schema"] = "from_user"
            input_info["has_value"] = True
    mismatched = sorted(referenced ^ set(runtime))
    if mismatched:
        raise ValueError(
            "Workflow run input contract mismatch: " + ", ".join(mismatched)
        )
    return effective_inputs

class MaPath:
    def __init__(self):
        self.lock = lock = asyncio.Lock()

        self.workflows: Dict[str, Workflow|LangGraphWorkflow] = {}
        self.submit_workflows: Dict[str, Workflow] = {}
        self.static_runs: Dict[str, StaticRun] = {}
        self.static_run_store = StaticRunStore()
        self.static_run_store.recover_interrupted_runs()
        self.dynamic_runs: Dict[str, DynamicRun] = {}
        self.dynamic_run_store = DynamicRunStore()
        self.dynamic_run_store.recover_interrupted_runs()
        self.async_que: Dict[str, asyncio.Queue] = {} 
        self.langgraph_task_requests: Dict[str, asyncio.Queue] = {}
        self.llm_instance_async_que: Dict[str, asyncio.Queue] = {}
        self.cluster_resource_requests: Dict[str, asyncio.Queue] = {}
        self.cluster_queue_requests: Dict[str, asyncio.Queue] = {}
        self.worker_registration_requests: Dict[str, asyncio.Queue] = {}
        self.task_attempts: Dict[tuple[str, str], Dict[str, Any]] = {}
        self.atlas_enqueue_index = 0
        self._scheduler_failure_handled: tuple[int | None, int | None] | None = None

        self.can_predict_task = ['llm_process','llm_fuse','vlm_process','speech_process']
        self.global_metrics = GlobalMetrics()
        for snapshot in self.static_run_store.list_runs():
            status = snapshot.get("status")
            if status in ("submitted", "running"):
                self.global_metrics.on_run_status_change(
                    snapshot["run_id"],
                    status,
                    "interrupted",
                )
         
    def cleanup(self):
        '''
        Clean up the main process and scheduler process.
        '''
        if getattr(self, "_cleanup_complete", False):
            return True
        self._cleanup_started = True

        self.request_scheduler_shutdown()

        scheduler_process = getattr(self, "scheduler_process", None)
        if scheduler_process is not None:
            try:
                if scheduler_process.pid and scheduler_process.pid != os.getpid():
                    scheduler_process.join(timeout=75)
                    if scheduler_process.is_alive():
                        scheduler_process.terminate()
                        scheduler_process.join(timeout=5)
                    if scheduler_process.is_alive() and hasattr(scheduler_process, "kill"):
                        scheduler_process.kill()
                        scheduler_process.join(timeout=5)
            except AssertionError:
                pass
            except Exception:
                pass

        cleanup_complete = self._stop_local_ray_best_effort()

        self._close_scheduler_channels()
        self._cleanup_complete = cleanup_complete
        if not cleanup_complete:
            self._cleanup_started = False
        return cleanup_complete

    def request_scheduler_shutdown(self) -> None:
        if getattr(self, "_scheduler_shutdown_requested", False):
            return
        self._scheduler_shutdown_requested = True
        try:
            self._send_scheduler_message({"type": "shutdown"})
        except Exception:
            pass

    def _close_scheduler_channels(self):

        for socket_name in ("socket_to_scheduler", "socket_from_scheduler"):
            socket = getattr(self, socket_name, None)
            if socket is not None:
                try:
                    socket.close(linger=0)
                except Exception:
                    pass

    def _drain_scheduler_owner_nodes(self) -> Dict[str, str]:
        owner_nodes = getattr(self, "_scheduler_owner_nodes", None)
        if owner_nodes is None:
            owner_nodes = {}
            self._scheduler_owner_nodes = owner_nodes
        receiver = getattr(self, "_scheduler_owner_node_receiver", None)
        if receiver is None:
            return dict(owner_nodes)
        while receiver.poll():
            try:
                placement = receiver.recv()
            except (EOFError, OSError):
                break
            if not isinstance(placement, dict):
                logger.error("Ignoring invalid Scheduler owner placement receipt")
                continue
            node_id = placement.get("node_id")
            node_ip = placement.get("node_ip")
            if node_id and node_ip:
                owner_nodes[str(node_id)] = str(node_ip)
        return dict(owner_nodes)

    def _stop_local_ray_best_effort(self) -> bool:
        self._drain_scheduler_owner_nodes()
        owner_cleanup_event = getattr(
            self,
            "_scheduler_owner_cleanup_complete_event",
            None,
        )
        ray_cleanup_event = getattr(
            self,
            "_scheduler_ray_cleanup_complete_event",
            None,
        )
        owner_cleanup_complete = bool(
            owner_cleanup_event is not None and owner_cleanup_event.is_set()
        )
        if not owner_cleanup_complete:
            cluster_cleanup = self._stop_owned_llm_processes_via_ray_best_effort()
            if cluster_cleanup is True:
                owner_cleanup_complete = True
                if owner_cleanup_event is not None:
                    owner_cleanup_event.set()
        else:
            cluster_cleanup = True

        if not owner_cleanup_complete:
            logger.warning(
                "Preserving the Ray runtime because cluster owner cleanup is unverified"
            )

        ray_cleanup_complete = bool(
            ray_cleanup_event is not None and ray_cleanup_event.is_set()
        )
        if owner_cleanup_complete and not ray_cleanup_complete:
            try:
                result = stop_ray_runtime(force=True)
                if result.returncode != 0:
                    logger.warning(
                        "Ray cleanup exited with status %s: %s",
                        result.returncode,
                        (result.stderr or result.stdout or "unknown error").strip(),
                    )
                else:
                    ray_cleanup_complete = True
                    if ray_cleanup_event is not None:
                        ray_cleanup_event.set()
            except Exception as exc:
                logger.warning("Unable to stop the local Ray runtime: %s", exc)
        local_cleanup = self._stop_owned_llm_processes_locally_best_effort()
        return (
            owner_cleanup_complete
            and ray_cleanup_complete
            and local_cleanup is True
        )

    def _stop_owned_llm_processes_via_ray_best_effort(self) -> bool | None:
        owner_id = getattr(self, "_scheduler_owner_id", None)
        if not owner_id:
            return True

        connected_here = False
        try:
            if not ray.is_initialized():
                try:
                    with socket.create_connection(
                        ("127.0.0.1", int(self.ray_head_port)),
                        timeout=0.25,
                    ):
                        pass
                except (OSError, TypeError, ValueError):
                    logger.info(
                        "Skipping Ray model cleanup because the local Ray head is unavailable"
                    )
                    return None
                ray.init(
                    address=f"127.0.0.1:{self.ray_head_port}",
                    ignore_reinit_error=True,
                    logging_level=logging.ERROR,
                )
                connected_here = True
            expected_nodes = self._drain_scheduler_owner_nodes()
            if expected_nodes:
                stop_llm_owner_processes_on_cluster(
                    owner_id,
                    expected_nodes=expected_nodes,
                )
            else:
                stop_llm_owner_processes_on_cluster(owner_id)
            return True
        except Exception as exc:
            logger.warning(
                "Unable to clean Scheduler-owned model processes through Ray: %s",
                exc,
            )
            return False
        finally:
            if connected_here:
                try:
                    ray.shutdown()
                except Exception:
                    logger.exception("Unable to disconnect from Ray after model cleanup")

    def _stop_owned_llm_processes_locally_best_effort(self) -> bool:
        owner_id = getattr(self, "_scheduler_owner_id", None)
        if not owner_id:
            return True
        try:
            stop_llm_owner_processes_locally(owner_id)
            return True
        except Exception as exc:
            logger.warning("Unable to clean local Scheduler-owned model processes: %s", exc)
            return False

    def _abort_scheduler_start(self):
        scheduler_process = getattr(self, "scheduler_process", None)
        if scheduler_process is not None:
            try:
                scheduler_process.join(timeout=1)
                if scheduler_process.is_alive():
                    scheduler_process.terminate()
                    scheduler_process.join(timeout=5)
                if scheduler_process.is_alive() and hasattr(scheduler_process, "kill"):
                    scheduler_process.kill()
                    scheduler_process.join(timeout=5)
            except (AssertionError, OSError):
                pass
        self._stop_local_ray_best_effort()
        self._close_scheduler_channels()

    def _wait_for_scheduler_ready(self, timeout: float = SCHEDULER_START_TIMEOUT_SECONDS):
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Scheduler did not become ready within {float(timeout):g} seconds"
                )
            try:
                message = self.ready_queue.get(
                    timeout=min(SCHEDULER_START_POLL_SECONDS, remaining)
                )
            except queue.Empty:
                if self.scheduler_process.is_alive():
                    continue
                self.scheduler_process.join(timeout=0)
                raise RuntimeError(
                    "Scheduler exited before becoming ready "
                    f"(pid={self.scheduler_process.pid}, "
                    f"exitcode={self.scheduler_process.exitcode})"
                )

            if message == "ready":
                return
            if isinstance(message, dict) and message.get("status") == "error":
                raise RuntimeError(
                    f"Scheduler failed to start: {message.get('error', 'unknown error')}"
                )
            raise RuntimeError(f"Unexpected scheduler readiness message: {message!r}")

    def _send_scheduler_message(self, message: Dict[str, Any]):
        self._require_scheduler_available()
        serialized: bytes = json.dumps(message).encode('utf-8')
        self.socket_to_scheduler.send(serialized)

    def _require_scheduler_available(self):
        unavailable = self._scheduler_unavailable_message()
        if unavailable is None:
            return
        scheduler_process = getattr(self, "scheduler_process", None)
        raise SchedulerUnavailableError(
            unavailable,
            pid=getattr(scheduler_process, "pid", None),
            exitcode=getattr(scheduler_process, "exitcode", None),
        )

    def _scheduler_unavailable_message(self) -> str | None:
        scheduler_process = getattr(self, "scheduler_process", None)
        if scheduler_process is None:
            return "scheduler process is not initialized"
        fatal_event = getattr(self, "_scheduler_fatal_event", None)
        if fatal_event is not None and fatal_event.is_set():
            pid = scheduler_process.pid
            process_detail = f"pid={pid}" if pid else "pid=unknown"
            return (
                f"scheduler reported a fatal failure ({process_detail}). "
                "Restart Maze core to recover the scheduler."
            )
        if scheduler_process.is_alive():
            return None

        exitcode = scheduler_process.exitcode
        pid = scheduler_process.pid
        process_detail = f"pid={pid}, exitcode={exitcode}" if pid else f"exitcode={exitcode}"
        return (
            f"scheduler process exited ({process_detail}). "
            "Restart Maze core to recover the scheduler."
        )

    def _validate_task_file_manifest(self, data: Dict[str, Any]) -> Dict[str, Any] | None:
        manifest = data.get("file_manifest")
        if not manifest:
            return None

        expected = {
            "run_id": data.get("workflow_id"),
            "task_id": data.get("task_id"),
            "attempt": data.get("attempt"),
            "dispatch_id": data.get("dispatch_id"),
            "published": False,
        }
        for field, value in expected.items():
            if manifest.get(field) != value:
                raise ArtifactError(
                    f"Task manifest {field} does not match the finishing attempt"
                )
        return manifest

    def _accept_task_attempt_event(self, message_type: str, data: Dict[str, Any]) -> bool:
        key = (data.get("workflow_id"), data.get("task_id"))
        attempt = data.get("attempt")
        dispatch_id = data.get("dispatch_id")
        lease_id = data.get("lease_id")
        if None in key or attempt is None or not dispatch_id or not lease_id:
            return False
        if message_type == "finish_task":
            self._validate_task_file_manifest(data)

        identity = (attempt, dispatch_id, lease_id)
        next_state = "running" if message_type == "start_task" else (
            "retrying" if message_type == "task_retry" else "terminal"
        )
        current = self.task_attempts.get(key)
        if current is not None and current["state"] == "terminal":
            return False
        if current is None or attempt > current["identity"][0]:
            self.task_attempts[key] = {
                "identity": identity,
                "state": next_state,
                "event_type": message_type,
            }
            return True
        if attempt < current["identity"][0] or identity != current["identity"]:
            return False
        if current["state"] != "running" or message_type == "start_task":
            return False

        current["state"] = next_state
        current["event_type"] = message_type
        return True

    def _publish_task_file_manifest(
        self,
        data: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        manifest = self._validate_task_file_manifest(data)
        if not manifest:
            return None

        key = (data.get("workflow_id"), data.get("task_id"))
        identity = (data.get("attempt"), data.get("dispatch_id"), data.get("lease_id"))
        accepted = self.task_attempts.get(key)
        if (
            accepted is None
            or accepted["identity"] != identity
            or accepted.get("event_type") != "finish_task"
        ):
            raise ArtifactError("Cannot publish a manifest from an unaccepted task attempt")

        published = copy.deepcopy(manifest)
        published["published"] = True
        published["published_time"] = time.time()
        data["file_manifest"] = published
        return published
        
    def create_workflow(self,workflow_id:str):
        '''
        Create a workflow.
        '''
        self.workflows[workflow_id] = Workflow(workflow_id)
        self.global_metrics.on_workflow_created(workflow_id)

    def create_app_workflow(self, spec:Dict[str,Any]) -> str:
        '''
        Create a single-task application workflow from an AppSpec.
        '''
        workflow_id = str(uuid.uuid4())
        self.workflows[workflow_id] = build_app_workflow(workflow_id, spec)
        return workflow_id

    def create_dag_workflow(self, spec:Dict[str,Any]) -> str:
        '''
        Create a static workflow from an external DAG submit spec.
        '''
        workflow_id = str(uuid.uuid4())
        self.workflows[workflow_id] = build_dag_workflow(workflow_id, spec)
        self.global_metrics.on_workflow_created(workflow_id)
        return workflow_id

    def get_workflow(self,workflow_id:str) -> Workflow|LangGraphWorkflow:
        '''
        Get a workflow.
        '''
        return self.workflows[workflow_id]
  
    def get_workflow_tasks(self,workflow_id:str):
        """
        Get all tasks in a workflow.
        """
        if workflow_id not in self.workflows:
            return []
        
        workflow = self.workflows[workflow_id]
        tasks = []
        
       
        for task_id, task in workflow.tasks.items():
            tasks.append({
                "id": task_id,
                "name": task.task_name if hasattr(task, 'task_name') else f"任务_{task_id[:8]}"
            })
        
        return tasks

    async def _get_daps_priority(self, task_name:str, features:Dict, remaining_task_num:int, total_task_num:int, w1:int, w2:int):
        payload = {"task_name": task_name, "features": features}
        async with httpx.AsyncClient() as client:
            response = await client.post("http://127.0.0.1:8001/predict", json=payload)
        predict_time = response.json()['predict_time']

        score_urgency = 1.0 - (remaining_task_num / total_task_num)
        return w1 * score_urgency + w2 * predict_time

    
    def _get_hacs_priority(self, workflow: Workflow, task_id: str):
        node_info = workflow.graph.nodes[task_id]
        n_desc = node_info.get("n_desc", 0)
        pred_time = max(node_info.get("pred_time", 3.0), EPSILON)
        is_dynamic = 0
        omega = math.log2(2.0 + 2.0 * n_desc)
        return (omega, pred_time, is_dynamic)

    def _get_atlas_priority(self, workflow: Workflow, task_id: str):
        attained_service = workflow.graph.nodes[task_id].get("attained_service", 0.0)
        submission_time = workflow.graph.graph.get("submission_time", 0.0)
        priority = (attained_service, submission_time, self.atlas_enqueue_index)
        self.atlas_enqueue_index += 1
        return priority

    async def _get_task_priority(self, workflow: Workflow, task: CodeTask):
        if self.strategy == "Default":
            return 0

        if task.can_predict and self.strategy == "DAPS":
            remaining_task_num: int = workflow.remaining_task_num
            total_task_num: int = len(workflow.tasks)
            return await self._get_daps_priority(
                task.task_name,
                task.predict_feature,
                remaining_task_num,
                total_task_num,
                0.5,
                0.5,
            )

        if self.strategy == "HACS":
            return self._get_hacs_priority(workflow, task.task_id)

        if self.strategy == "ATLAS":
            return self._get_atlas_priority(workflow, task.task_id)

        return 0

    def _task_run_payload(self, workflow: Workflow, task: CodeTask, submit_id: str, file_context: Dict[str, Any] | None = None):
        data = task.to_json()
        data['workflow_id'] = submit_id
        file_context = file_context or task.file_context

        if file_context and file_context.get("enabled"):
            task_node_ids = file_context.get("task_node_ids") or {}
            parent_file_manifests = []
            for parent_task_id in workflow.graph.predecessors(task.task_id):
                manifest = workflow.graph.nodes[parent_task_id].get("file_manifest")
                if manifest:
                    parent_file_manifests.append(manifest)
            data["file_context"] = {
                **file_context,
                "enabled": True,
                "run_id": submit_id,
                "submit_id": submit_id,
                "task_id": task.task_id,
                "node_id": task_node_ids.get(task.task_id),
                "parent_task_ids": list(workflow.graph.predecessors(task.task_id)),
                "parent_file_manifests": parent_file_manifests,
            }

        return data

    def _prepare_initial_artifacts(self, file_context: Dict[str, Any], submit_id: str) -> Dict[str, Any]:
        prepared_context = copy.deepcopy(file_context)
        if (
            not prepared_context
            or not prepared_context.get("enabled")
            or not prepared_context.get("artifact_store")
        ):
            return prepared_context

        from pathlib import Path

        workspace_dir = Path(prepared_context["workspace_dir"]).expanduser().resolve()
        files_dir = workspace_dir / "files"
        artifact_root = prepared_context.get("artifact_store", {}).get("root")
        store = LocalCASArtifactStore(artifact_root)
        initial_files = []

        if files_dir.is_symlink():
            raise ValueError("Workspace files directory cannot be a symbolic link")
        if files_dir.exists():
            for file_path in sorted(files_dir.rglob("*")):
                if file_path.is_symlink():
                    relative_path = file_path.relative_to(files_dir).as_posix()
                    raise ValueError(
                        f"Workspace files cannot contain symbolic links: {relative_path}"
                    )
                if not file_path.is_file() or "__pycache__" in file_path.parts or file_path.suffix == ".pyc":
                    continue
                relative_path = file_path.relative_to(files_dir).as_posix()
                artifact = store.put_file(file_path)
                initial_files.append({
                    "path": relative_path,
                    "name": file_path.name,
                    "size": artifact["size"],
                    "sha256": artifact["sha256"],
                    "artifact_id": artifact["artifact_id"],
                    "storage_uri": artifact["storage_uri"],
                    "producer_task_id": "__workspace__",
                    "uri": f"maze://runs/{submit_id}/workspace/files/{relative_path}",
                })

        prepared_context["workspace_dir"] = str(workspace_dir)
        prepared_context["run_id"] = submit_id
        prepared_context["initial_files"] = initial_files
        return prepared_context

    def run_workflow(
        self,
        workflow_id:str,
        file_context:Dict[str,Any]|None=None,
        timeout_seconds:float|None=None,
        tags:List[str]|None=None,
        metadata:Dict[str,Any]|None=None,
        final_output_refs:Any=FINAL_OUTPUT_REFS_UNSET,
        inputs:Dict[str,Any]|None=None,
    ):
        """
        Start a workflow.
        """
        self._require_scheduler_available()
        submit_workflow = copy.deepcopy(self.workflows[workflow_id])
        run_inputs = _bind_workflow_run_inputs(submit_workflow, inputs)
        submit_id = str(uuid.uuid4())
        file_context = self._prepare_initial_artifacts(file_context, submit_id) if file_context else None
        if file_context:
            submit_workflow.graph.graph["file_context"] = file_context
        submit_workflow.prepare_for_strategy(self.strategy)
        submit_workflow.graph.graph["submission_time"] = time.time()
        static_run = StaticRun(
            submit_id,
            workflow_id,
            submit_workflow,
            timeout_seconds=timeout_seconds,
            tags=tags,
            metadata=metadata,
            final_output_refs=final_output_refs,
            run_inputs=run_inputs,
        )
        self.submit_workflows[submit_id] = submit_workflow
        self.async_que[submit_id] = asyncio.Queue()
        self.static_runs[submit_id] = static_run
        self._persist_static_run(submit_id)
        self.global_metrics.on_run_submitted(submit_id)
        self._record_static_event(submit_id, {
            "type": "start_workflow",
            "data": {
                "run_id": submit_id,
                "workflow_id": workflow_id,
                "run_type": "static",
                "total_task_num": submit_workflow.get_total_task_num(),
            },
        })
        start_task:List = submit_workflow.get_start_task()
        
        for task in start_task:
            static_run.mark_task_queued(task.task_id)
            data = self._task_run_payload(submit_workflow, task, submit_id, file_context)
            if self.strategy == "HACS":
                data['priority'] = self._get_hacs_priority(submit_workflow, task.task_id)
            elif self.strategy == "ATLAS":
                data['priority'] = self._get_atlas_priority(submit_workflow, task.task_id)
            else:
                data['priority'] = 0
            message = {
                "type":"run_task",
                "data": data
            }
            self._send_scheduler_message(message)

        self._persist_static_run(submit_id)
        return submit_id

    async def create_dynamic_run(
        self,
        max_tasks:int=100,
        timeout_seconds:int|None=None,
        file_context:Dict[str,Any]|None=None,
        metadata:Dict[str,Any]|None=None,
    ):
        self._require_scheduler_available()
        run_id = str(uuid.uuid4())
        file_context = self._prepare_initial_artifacts(file_context, run_id) if file_context else None
        self.dynamic_runs[run_id] = DynamicRun(
            run_id=run_id,
            max_tasks=max_tasks,
            timeout_seconds=timeout_seconds,
            file_context=file_context,
            metadata=metadata,
        )
        self.async_que[run_id] = asyncio.Queue()
        await self._emit_dynamic_event(run_id, {
            "type": "start_dynamic_run",
            "data": {
                "run_id": run_id,
                "max_tasks": max_tasks,
                "timeout_seconds": timeout_seconds,
                "file_context_enabled": bool(file_context and file_context.get("enabled")),
            },
        })
        return run_id

    def get_dynamic_run(self, run_id:str) -> DynamicRun:
        if run_id not in self.dynamic_runs:
            raise ValueError(f"Dynamic run not found: {run_id}")
        return self.dynamic_runs[run_id]

    async def list_dynamic_runs(
        self,
        status: str | None = None,
        limit: int | None = None,
        detail: bool = False,
    ):
        snapshots = self.dynamic_run_store.list_runs(summary=not detail)
        if status:
            snapshots = [snapshot for snapshot in snapshots if snapshot.get("status") == status]
        if limit is not None:
            snapshots = snapshots[: max(0, limit)]
        return snapshots

    async def register_dynamic_task_spec(self, run_id:str, task_spec_payload:Dict[str,Any]):
        self._require_scheduler_available()
        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        task_spec = dynamic_task_spec_from_payload(task_spec_payload)
        dynamic_run.register_task_spec(task_spec)
        await self._emit_dynamic_event(run_id, {
            "type": "register_task_spec",
            "data": {
                "run_id": run_id,
                "task_spec_id": task_spec.task_spec_id,
                "task_name": task_spec.task_name,
                "inputs": task_spec.inputs,
                "outputs": task_spec.outputs,
                "resources": task_spec.resources,
            },
        })
        return task_spec

    async def append_dynamic_task(
        self,
        run_id:str,
        task_spec_id:str|None=None,
        task_spec_payload:Dict[str,Any]|None=None,
        inputs:Dict[str,Any]|None=None,
        parents:List[str]|None=None,
        request_id:str|None=None,
        resources:Dict[str,Any]|None=None,
    ):
        self._require_scheduler_available()
        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        dynamic_run.check_can_mutate("append tasks")
        existing_task = dynamic_run.get_task_for_request_id(request_id)
        if existing_task is not None:
            return existing_task, True

        dynamic_run.check_can_append()
        task_spec = dynamic_run.resolve_task_spec(task_spec_id, task_spec_payload)
        task, idempotent = dynamic_run.append_task(
            task_spec,
            inputs=inputs,
            parents=parents,
            request_id=request_id,
            resources=resources,
        )

        if not idempotent:
            status = "ready" if task.task_id in dynamic_run.submitted_tasks else "pending"
            await self._emit_dynamic_event(run_id, {
                "type": "append_task",
                "data": {
                    "run_id": run_id,
                    "task_id": task.task_id,
                    "task_spec_id": task_spec.task_spec_id,
                    "task_name": task.task_name,
                    "parents": sorted(dynamic_run.task_parents.get(task.task_id, set())),
                    "request_id": request_id,
                    "status": status,
                    "resources": task.resources,
                },
            })

            if task.task_id in dynamic_run.submitted_tasks:
                self._submit_dynamic_task(task)

        return task, idempotent

    async def finalize_dynamic_run(self, run_id:str, result:Any=None):
        self._require_scheduler_available()
        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        dynamic_run.finalize(result)
        await self._emit_dynamic_event(run_id, {
            "type": "finish_workflow",
            "data": {
                "run_id": run_id,
                "result": summarize_task_result(result, run_id=run_id),
            },
        })

        message = {"type":"clear_workflow","data":{"workflow_id":run_id}}
        self._send_scheduler_message(message)

    async def cancel_dynamic_run(self, run_id:str, reason:str|None=None):
        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        changed = dynamic_run.cancel(reason)
        if changed:
            await self._emit_dynamic_event(run_id, {
                "type": "cancel_dynamic_run",
                "data": {
                    "run_id": run_id,
                    "reason": reason,
                },
            })
            self._stop_dynamic_runtime(run_id)
        return dynamic_run

    async def get_dynamic_run_snapshot(self, run_id:str):
        if run_id in self.dynamic_runs:
            await self._refresh_dynamic_timeout(run_id)
            return self.get_dynamic_run(run_id).snapshot(
                lambda result: summarize_task_result(result, run_id=run_id)
            )
        return self.dynamic_run_store.load_run(run_id)

    async def get_dynamic_run_events(self, run_id:str, after:int|None=None):
        if run_id in self.dynamic_runs:
            await self._refresh_dynamic_timeout(run_id)
            return self.get_dynamic_run(run_id).get_events(after)
        self.dynamic_run_store.load_run(run_id)
        return self.dynamic_run_store.load_events(run_id, after)

    async def emit_dynamic_run_event(self, run_id:str, event:Dict[str,Any]):
        await self._refresh_dynamic_timeout(run_id)
        if not isinstance(event, dict):
            raise ValueError("Dynamic run event must be a JSON object")

        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("Dynamic run event requires a non-empty string type")

        if event_type in {
            "start_dynamic_run",
            "register_task_spec",
            "append_task",
            "task_ready",
            "start_task",
            "finish_task",
            "task_exception",
            "finish_workflow",
            "cancel_dynamic_run",
            "timeout_dynamic_run",
            "interrupt_dynamic_run",
        }:
            raise ValueError(f"Dynamic run event type is reserved: {event_type}")

        event_data = event.get("data") or {}
        if not isinstance(event_data, dict):
            raise ValueError("Dynamic run event data must be a JSON object")

        await self._emit_dynamic_event(run_id, {
            **event,
            "data": {
                **event_data,
                "run_id": run_id,
            },
        })
        return self.get_dynamic_run(run_id).event_log[-1]

    async def update_dynamic_run_metadata(self, run_id:str, metadata:Dict[str,Any]):
        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        updated = dynamic_run.update_metadata(metadata)
        self._persist_dynamic_run(run_id)
        return updated

    async def upsert_dynamic_permission_request(self, run_id:str, request:Dict[str,Any]):
        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        if not isinstance(request, dict):
            raise ValueError("permission request must be a JSON object")
        request_id = str(request.get("request_id") or request.get("id") or "").strip()
        if not request_id:
            raise ValueError("permission request_id is required")
        requests_map = dict(dynamic_run.metadata.get("permission_requests") or {})
        existing = requests_map.get(request_id) if isinstance(requests_map.get(request_id), dict) else {}
        now = time.time()
        normalized = {
            **existing,
            **request,
            "request_id": request_id,
            "status": str(request.get("status") or existing.get("status") or "pending"),
            "created_time": existing.get("created_time") or now,
            "updated_time": now,
        }
        requests_map[request_id] = normalized
        pending = [
            item
            for item in requests_map.values()
            if isinstance(item, dict) and item.get("status") == "pending"
        ]
        dynamic_run.update_metadata({
            "permission_requests": requests_map,
            "pending_permission_request_count": len(pending),
        })
        await self._emit_dynamic_event(run_id, {
            "type": "agent_permission_request_created",
            "data": normalized,
        })
        return normalized

    async def decide_dynamic_permission_request(self, run_id:str, request_id:str, decision:Dict[str,Any]):
        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        request_key = str(request_id or "").strip()
        if not request_key:
            raise ValueError("permission request_id is required")
        requests_map = dict(dynamic_run.metadata.get("permission_requests") or {})
        request_payload = requests_map.get(request_key)
        if not isinstance(request_payload, dict):
            raise ValueError(f"Permission request not found: {request_key}")
        if request_payload.get("status") != "pending":
            return request_payload
        action = str((decision or {}).get("action") or "").strip().lower()
        if action not in {"allow", "deny"}:
            raise ValueError("permission decision action must be allow or deny")
        now = time.time()
        decided = {
            **request_payload,
            "status": "allowed" if action == "allow" else "denied",
            "decision": {
                "action": action,
                "reason": str((decision or {}).get("reason") or "").strip(),
                "decided_by": str((decision or {}).get("decided_by") or "user").strip() or "user",
                "decided_time": now,
            },
            "updated_time": now,
        }
        requests_map[request_key] = decided
        pending = [
            item
            for item in requests_map.values()
            if isinstance(item, dict) and item.get("status") == "pending"
        ]
        dynamic_run.update_metadata({
            "permission_requests": requests_map,
            "pending_permission_request_count": len(pending),
        })
        await self._emit_dynamic_event(run_id, {
            "type": "agent_permission_request_decided",
            "data": decided,
        })
        return decided

    async def delete_dynamic_run(self, run_id:str):
        snapshot = await self.get_dynamic_run_snapshot(run_id)
        if snapshot.get("status") not in TERMINAL_DYNAMIC_RUN_STATUSES:
            raise ValueError("Only terminal dynamic runs can be deleted")

        self.dynamic_runs.pop(run_id, None)
        self.async_que.pop(run_id, None)
        self.dynamic_run_store.delete_run(run_id)
        return {"run_id": run_id, "deleted": True}

    async def cleanup_dynamic_runs(
        self,
        statuses: List[str] | None = None,
        older_than_days: int | float | None = None,
        dry_run: bool = True,
    ):
        cleanup_result = self.dynamic_run_store.cleanup(
            statuses=statuses,
            older_than_days=older_than_days,
            dry_run=dry_run,
        )
        if not dry_run:
            for run_id in cleanup_result.get("deleted_run_ids", []):
                self.dynamic_runs.pop(run_id, None)
                self.async_que.pop(run_id, None)
        return cleanup_result

    def _stop_dynamic_runtime(self, run_id:str):
        self._stop_workflow_best_effort(run_id)

    def _stop_workflow_best_effort(self, run_id: str):
        try:
            self._send_scheduler_message({
                "type": "stop_workflow",
                "data": {"workflow_id": run_id},
            })
        except SchedulerUnavailableError:
            logger.warning(
                "Could not stop workflow %s because the scheduler is unavailable",
                run_id,
            )
        except Exception:
            logger.exception(
                "Could not stop workflow %s during terminal-state cleanup",
                run_id,
            )

    async def _refresh_dynamic_timeout(self, run_id:str) -> bool:
        dynamic_run = self.get_dynamic_run(run_id)
        if not dynamic_run.mark_timed_out_if_needed():
            return False

        await self._emit_dynamic_event(run_id, {
            "type": "timeout_dynamic_run",
            "data": {
                "run_id": run_id,
                "timeout_seconds": dynamic_run.timeout_seconds,
                "deadline_time": dynamic_run.created_time + float(dynamic_run.timeout_seconds),
            },
        })
        self._stop_workflow_best_effort(run_id)
        return True

    async def _sweep_run_deadlines(self):
        for run_id, static_run in list(self.static_runs.items()):
            previous_status = static_run.status
            deadline = static_run.deadline_time()
            if not static_run.mark_timed_out_if_needed():
                continue

            event = self._record_static_event(run_id, {
                "type": "timeout_workflow",
                "data": {
                    "run_id": run_id,
                    "workflow_id": static_run.workflow_id,
                    "timeout_seconds": static_run.timeout_seconds,
                    "deadline_time": deadline,
                },
            })
            metrics_status = "submitted" if previous_status == "created" else previous_status
            self.global_metrics.on_run_status_change(
                run_id,
                metrics_status,
                "timed_out",
            )
            queue = self.async_que.get(run_id)
            if queue is not None:
                queue.put_nowait(event)
            self._stop_workflow_best_effort(run_id)

        for run_id, dynamic_run in list(self.dynamic_runs.items()):
            if not dynamic_run.mark_timed_out_if_needed():
                continue
            await self._emit_dynamic_event(run_id, {
                "type": "timeout_dynamic_run",
                "data": {
                    "run_id": run_id,
                    "timeout_seconds": dynamic_run.timeout_seconds,
                    "deadline_time": (
                        dynamic_run.created_time + float(dynamic_run.timeout_seconds)
                    ),
                },
            })
            self._stop_workflow_best_effort(run_id)

    def _scheduler_event_is_persisted(self, store, run_id: str, event: Dict[str, Any]) -> bool:
        load_events = getattr(store, "load_events", None)
        sequence = event.get("seq")
        if load_events is None or sequence is None:
            return False

        for stored_event in load_events(run_id, after=int(sequence) - 1):
            if int(stored_event.get("seq", 0)) != int(sequence):
                continue
            if stored_event != event:
                raise RuntimeError(
                    f"Persisted event sequence conflict for run {run_id}: {sequence}"
                )
            return True
        return False

    def _persist_scheduler_exit_entry(
        self,
        store,
        run_id: str,
        entry: Dict[str, Any],
        *,
        dynamic: bool,
    ):
        event = entry["event"]
        snapshot = entry["snapshot"]
        if not entry["event_persisted"]:
            append_event_once = getattr(store, "append_event_once", None)
            if append_event_once is not None:
                if dynamic:
                    append_event_once(run_id, event, snapshot=snapshot)
                else:
                    append_event_once(run_id, event)
            elif not self._scheduler_event_is_persisted(store, run_id, event):
                if dynamic:
                    store.append_event(run_id, event, snapshot=snapshot)
                else:
                    store.append_event(run_id, event)
            entry["event_persisted"] = True
        if not entry["snapshot_persisted"]:
            store.save_run(snapshot)
            entry["snapshot_persisted"] = True

    def _scheduler_exit_progress_for(
        self,
        failure: tuple[int | None, int | None],
        reason: str,
    ) -> Dict[str, Any]:
        progress = getattr(self, "_scheduler_exit_progress", None)
        if progress is not None and progress.get("failure") == failure:
            return progress

        progress = {
            "failure": failure,
            "reason": reason,
            "static": {},
            "dynamic": {},
            "waiters_notified": False,
            "ray_cleanup_complete": False,
        }
        for run_id, static_run in list(self.static_runs.items()):
            progress["static"][run_id] = {
                "run": static_run,
                "previous_status": static_run.status,
                "interrupted": None,
                "event": None,
                "snapshot": None,
                "event_persisted": False,
                "snapshot_persisted": False,
                "metrics_recorded": False,
                "queue_notified": False,
                "complete": False,
            }
        for run_id, dynamic_run in list(self.dynamic_runs.items()):
            progress["dynamic"][run_id] = {
                "run": dynamic_run,
                "interrupted": None,
                "event": None,
                "snapshot": None,
                "event_persisted": False,
                "snapshot_persisted": False,
                "queue_notified": False,
                "complete": False,
            }
        self._scheduler_exit_progress = progress
        logger.error("%s", reason)
        return progress

    @staticmethod
    def _raise_scheduler_unavailable_response(response: Dict[str, Any]) -> None:
        if response.get("type") != "scheduler_unavailable":
            return
        data = response.get("data") or {}
        raise SchedulerUnavailableError(
            data.get("message") or "scheduler process is unavailable",
            pid=data.get("scheduler_pid"),
            exitcode=data.get("scheduler_exitcode"),
        )

    async def _wait_for_scheduler_response(
        self,
        response_queue: asyncio.Queue,
        *,
        timeout: float,
        operation: str,
    ) -> Dict[str, Any]:
        timeout = max(0.0, float(timeout))
        try:
            response = await asyncio.wait_for(response_queue.get(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            scheduler_process = getattr(self, "scheduler_process", None)
            message = self._scheduler_unavailable_message()
            if message is None:
                message = (
                    f"Timed out after {timeout:g} seconds while waiting for "
                    f"the scheduler to {operation}"
                )
            raise SchedulerUnavailableError(
                message,
                pid=getattr(scheduler_process, "pid", None),
                exitcode=getattr(scheduler_process, "exitcode", None),
            ) from exc
        self._raise_scheduler_unavailable_response(response)
        unavailable = self._scheduler_unavailable_message()
        if unavailable is not None:
            scheduler_process = getattr(self, "scheduler_process", None)
            raise SchedulerUnavailableError(
                unavailable,
                pid=getattr(scheduler_process, "pid", None),
                exitcode=getattr(scheduler_process, "exitcode", None),
            )
        return response

    def _notify_scheduler_exit_waiters(
        self,
        reason: str,
        scheduler_process,
    ) -> None:
        unavailable = SchedulerUnavailableError(
            reason,
            pid=getattr(scheduler_process, "pid", None),
            exitcode=getattr(scheduler_process, "exitcode", None),
        ).detail()
        message = {
            "type": "scheduler_unavailable",
            "data": {**unavailable, "status": "interrupted"},
        }
        for attribute in (
            "langgraph_task_requests",
            "llm_instance_async_que",
            "cluster_resource_requests",
            "cluster_queue_requests",
            "worker_registration_requests",
        ):
            requests = getattr(self, attribute, None)
            if requests is None:
                continue
            for response_queue in list(requests.values()):
                response_queue.put_nowait(message)
            requests.clear()

    async def _wait_for_scheduler_process_exit(
        self,
        scheduler_process,
        timeout: float,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while scheduler_process.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(SCHEDULER_PROCESS_EXIT_POLL_SECONDS, remaining))
        try:
            scheduler_process.join(timeout=0)
        except (AssertionError, OSError):
            pass
        return True

    async def _stop_fatal_scheduler_process(self, scheduler_process) -> bool:
        scheduler_pid = getattr(scheduler_process, "pid", None)
        if not scheduler_process.is_alive():
            return True
        if scheduler_pid is None or scheduler_pid == os.getpid():
            logger.critical(
                "Refusing to terminate invalid Scheduler pid %s after fatal failure",
                scheduler_pid,
            )
            return False

        logger.critical(
            "Scheduler pid %s remained alive after its fatal cleanup grace period; "
            "terminating it",
            scheduler_pid,
        )
        try:
            scheduler_process.terminate()
        except (AssertionError, OSError):
            logger.exception("Unable to terminate fatal Scheduler pid %s", scheduler_pid)
        terminate_timeout = getattr(
            self,
            "_scheduler_fatal_terminate_timeout_seconds",
            SCHEDULER_FATAL_TERMINATE_TIMEOUT_SECONDS,
        )
        if await self._wait_for_scheduler_process_exit(
            scheduler_process,
            terminate_timeout,
        ):
            return True

        kill = getattr(scheduler_process, "kill", None)
        if kill is None:
            logger.critical(
                "Scheduler pid %s ignored termination and cannot be killed",
                scheduler_pid,
            )
            return False
        logger.critical(
            "Scheduler pid %s ignored termination; killing it",
            scheduler_pid,
        )
        try:
            kill()
        except (AssertionError, OSError):
            logger.exception("Unable to kill fatal Scheduler pid %s", scheduler_pid)
        kill_timeout = getattr(
            self,
            "_scheduler_fatal_kill_timeout_seconds",
            SCHEDULER_FATAL_KILL_TIMEOUT_SECONDS,
        )
        stopped = await self._wait_for_scheduler_process_exit(
            scheduler_process,
            kill_timeout,
        )
        if not stopped:
            logger.critical(
                "Scheduler pid %s remained alive after kill",
                scheduler_pid,
            )
        return stopped

    async def _handle_scheduler_exit(self):
        scheduler_process = getattr(self, "scheduler_process", None)
        if scheduler_process is None:
            return

        if scheduler_process.is_alive():
            fatal_event = getattr(self, "_scheduler_fatal_event", None)
            if fatal_event is None or not fatal_event.is_set():
                return
            scheduler_pid = getattr(scheduler_process, "pid", None)
            fatal_state = getattr(self, "_scheduler_fatal_exit_state", None)
            if fatal_state is None or fatal_state.get("pid") != scheduler_pid:
                grace_seconds = max(
                    0.0,
                    float(getattr(
                        self,
                        "_scheduler_fatal_exit_grace_seconds",
                        SCHEDULER_FATAL_EXIT_GRACE_SECONDS,
                    )),
                )
                fatal_state = {
                    "pid": scheduler_pid,
                    "deadline": time.monotonic() + grace_seconds,
                }
                self._scheduler_fatal_exit_state = fatal_state

            if getattr(self, "_scheduler_fatal_waiters_notified", None) != scheduler_pid:
                reason = (
                    self._scheduler_unavailable_message()
                    or "scheduler process is unavailable"
                )
                try:
                    self._notify_scheduler_exit_waiters(reason, scheduler_process)
                except Exception:
                    logger.exception("Could not notify fatal Scheduler waiters; will retry")
                else:
                    self._scheduler_fatal_waiters_notified = scheduler_pid

            if time.monotonic() < fatal_state["deadline"]:
                return
            if not await self._stop_fatal_scheduler_process(scheduler_process):
                return

        self._scheduler_fatal_exit_state = None

        failure = (scheduler_process.pid, scheduler_process.exitcode)
        if self._scheduler_failure_handled == failure:
            return

        reason = self._scheduler_unavailable_message() or "scheduler process is unavailable"
        progress = self._scheduler_exit_progress_for(
            failure,
            reason,
        )
        reason = progress["reason"]

        if not progress["waiters_notified"]:
            try:
                self._notify_scheduler_exit_waiters(reason, scheduler_process)
            except Exception:
                logger.exception("Could not notify scheduler waiters; will retry")
            else:
                progress["waiters_notified"] = True

        try:
            for run_id, entry in progress["static"].items():
                if entry["complete"]:
                    continue
                try:
                    static_run = entry["run"]
                    if entry["interrupted"] is None:
                        entry["interrupted"] = static_run.mark_interrupted(reason)
                    if not entry["interrupted"]:
                        entry["complete"] = True
                        continue
                    if entry["event"] is None:
                        entry["event"] = static_run.append_event({
                            "type": "interrupt_workflow",
                            "data": {
                                "run_id": run_id,
                                "workflow_id": static_run.workflow_id,
                                "reason": reason,
                                "scheduler_pid": scheduler_process.pid,
                                "scheduler_exitcode": scheduler_process.exitcode,
                            },
                        })
                    if entry["snapshot"] is None:
                        entry["snapshot"] = static_run.snapshot()
                    self._persist_scheduler_exit_entry(
                        self.static_run_store,
                        run_id,
                        entry,
                        dynamic=False,
                    )
                    if not entry["metrics_recorded"]:
                        metrics_status = (
                            "submitted"
                            if entry["previous_status"] == "created"
                            else entry["previous_status"]
                        )
                        self.global_metrics.on_run_status_change(
                            run_id,
                            metrics_status,
                            "interrupted",
                        )
                        entry["metrics_recorded"] = True
                    if not entry["queue_notified"]:
                        queue = self.async_que.get(run_id)
                        if queue is not None:
                            queue.put_nowait(entry["event"])
                        entry["queue_notified"] = True
                    entry["complete"] = True
                except Exception:
                    logger.exception(
                        "Could not finish scheduler-exit persistence for static run %s; will retry",
                        run_id,
                    )

            for run_id, entry in progress["dynamic"].items():
                if entry["complete"]:
                    continue
                try:
                    dynamic_run = entry["run"]
                    if entry["interrupted"] is None:
                        entry["interrupted"] = dynamic_run.interrupt(reason)
                    if not entry["interrupted"]:
                        entry["complete"] = True
                        continue
                    if entry["event"] is None:
                        entry["event"] = dynamic_run.append_event({
                            "type": "interrupt_dynamic_run",
                            "data": {
                                "run_id": run_id,
                                "reason": reason,
                                "scheduler_pid": scheduler_process.pid,
                                "scheduler_exitcode": scheduler_process.exitcode,
                            },
                        })
                    if entry["snapshot"] is None:
                        entry["snapshot"] = dynamic_run.snapshot(
                            lambda result: summarize_task_result(result, run_id=run_id)
                        )
                    self._persist_scheduler_exit_entry(
                        self.dynamic_run_store,
                        run_id,
                        entry,
                        dynamic=True,
                    )
                    if not entry["queue_notified"]:
                        queue = self.async_que.get(run_id)
                        if queue is not None:
                            await queue.put({"type": "dynamic_event"})
                        entry["queue_notified"] = True
                    entry["complete"] = True
                except Exception:
                    logger.exception(
                        "Could not finish scheduler-exit persistence for dynamic run %s; will retry",
                        run_id,
                    )
        finally:
            if not progress["ray_cleanup_complete"]:
                try:
                    cleanup_succeeded = await asyncio.to_thread(
                        self._stop_local_ray_best_effort
                    )
                except Exception:
                    logger.exception("Could not clean up Ray after scheduler exit; will retry")
                else:
                    if not cleanup_succeeded:
                        logger.error(
                            "Ray or Scheduler-owned model cleanup remained incomplete; will retry"
                        )
                    else:
                        progress["ray_cleanup_complete"] = True

        runs_complete = all(
            entry["complete"]
            for entries in (progress["static"], progress["dynamic"])
            for entry in entries.values()
        )
        if (
            runs_complete
            and progress["waiters_notified"]
            and progress["ray_cleanup_complete"]
        ):
            self._scheduler_failure_handled = failure
            self._scheduler_exit_progress = None

    async def maintenance_coroutine(self, interval_seconds: float = 1.0):
        interval_seconds = max(0.1, float(interval_seconds))
        while True:
            try:
                if not getattr(self, "_cleanup_started", False):
                    await self._handle_scheduler_exit()
                    await self._sweep_run_deadlines()
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Maze maintenance loop failed")
                await asyncio.sleep(interval_seconds)

    def _dynamic_task_run_payload(self, dynamic_run: DynamicRun, task: CodeTask):
        data = task.to_json()
        data['workflow_id'] = task.workflow_id
        file_context = dynamic_run.file_context
        if file_context and file_context.get("enabled"):
            parent_task_ids = sorted(dynamic_run.task_parents.get(task.task_id, set()))
            parent_file_manifests = [
                dynamic_run.task_file_manifests[parent_task_id]
                for parent_task_id in parent_task_ids
                if parent_task_id in dynamic_run.task_file_manifests
            ]
            data["file_context"] = {
                **file_context,
                "enabled": True,
                "run_id": dynamic_run.run_id,
                "submit_id": dynamic_run.run_id,
                "task_id": task.task_id,
                "parent_task_ids": parent_task_ids,
                "parent_file_manifests": parent_file_manifests,
            }
        return data

    def _submit_dynamic_task(self, task: CodeTask):
        dynamic_run = self.get_dynamic_run(task.workflow_id)
        data = self._dynamic_task_run_payload(dynamic_run, task)
        data['priority'] = 0
        message = {
            "type":"run_task",
            "data": data,
        }
        self._send_scheduler_message(message)

    async def _emit_dynamic_event(self, run_id:str, event:Dict[str,Any]):
        dynamic_run = self.get_dynamic_run(run_id)
        stored_event = dynamic_run.append_event(event)
        snapshot = dynamic_run.snapshot(lambda result: summarize_task_result(result, run_id=run_id))
        self.dynamic_run_store.append_event(run_id, stored_event, snapshot=snapshot)
        self.dynamic_run_store.save_run(snapshot)
        que = self.async_que.get(run_id)
        if que is not None:
            await que.put({"type": "dynamic_event"})

    def _persist_dynamic_run(self, run_id:str):
        dynamic_run = self.get_dynamic_run(run_id)
        self.dynamic_run_store.save_run(
            dynamic_run.snapshot(lambda result: summarize_task_result(result, run_id=run_id))
        )

    def _persist_static_run(self, run_id:str):
        static_run = self.static_runs.get(run_id)
        if static_run is not None:
            self.static_run_store.save_run(static_run.snapshot())

    def _record_static_event(self, run_id:str, event:Dict[str,Any]):
        static_run = self.static_runs.get(run_id)
        if static_run is None:
            return event
        stored_event = static_run.append_event(event)
        self.static_run_store.append_event(run_id, stored_event)
        self._persist_static_run(run_id)
        return stored_event

    def _get_static_run_snapshot(self, run_id:str):
        if run_id in self.static_runs:
            return self.static_runs[run_id].snapshot()
        return self.static_run_store.load_run(run_id)

    def _get_static_run_events(self, run_id:str, after:int|None=None):
        if run_id in self.static_runs:
            return self.static_runs[run_id].get_events(after)
        self.static_run_store.load_run(run_id)
        return self.static_run_store.load_events(run_id, after)

    def _get_static_run_tasks(self, run_id:str):
        snapshot = self._get_static_run_snapshot(run_id)
        return list((snapshot.get("task_nodes") or {}).values())

    def _get_static_run_task(self, run_id:str, task_id:str):
        snapshot = self._get_static_run_snapshot(run_id)
        task_nodes = snapshot.get("task_nodes") or {}
        if task_id not in task_nodes:
            raise ValueError(f"Task not found in run {run_id}: {task_id}")
        return task_nodes[task_id]

    def get_global_metrics_snapshot(self) -> Dict[str, Any]:
        return self.global_metrics.snapshot(
            workflows_in_memory=max(0, len(self.workflows) - len(self.submit_workflows)),
            runs_in_memory=len(self.submit_workflows),
        )

    def list_static_runs(
        self,
        status: str | None = None,
        limit: int | None = None,
    ) -> List[Dict[str, Any]]:
        store_ids = set()
        result: List[Dict[str, Any]] = []
        for snapshot in self.static_run_store.list_runs(summary=True):
            if status and snapshot.get("status") != status:
                continue
            store_ids.add(snapshot.get("run_id"))
            result.append(snapshot)
        for run_id, static_run in self.static_runs.items():
            if run_id in store_ids:
                continue
            snapshot = static_run_summary(static_run.snapshot())
            if status and snapshot.get("status") != status:
                continue
            result.append(snapshot)
        result.sort(key=lambda item: item.get("created_time") or 0, reverse=True)
        if limit is not None:
            result = result[: max(0, int(limit))]
        return result

    def get_static_run_snapshot(self, run_id: str) -> Dict[str, Any]:
        return self._get_static_run_snapshot(run_id)

    def get_static_current_task(self, run_id: str) -> Dict[str, Any]:
        snapshot = self._get_static_run_snapshot(run_id)
        task_nodes = snapshot.get("task_nodes") or {}
        running = [
            {
                "task_id": task.get("task_id"),
                "task_name": task.get("task_name"),
                "started_time": task.get("started_time"),
                "node_id": (task.get("selected_node") or {}).get("node_id"),
            }
            for task in task_nodes.values()
            if task.get("status") == "running"
        ]
        task_counts = snapshot.get("task_counts") or {}
        return {
            "run_id": run_id,
            "status": snapshot.get("status"),
            "running": running,
            "pending_count": task_counts.get("queued", 0) + task_counts.get("pending", 0),
            "done_count": (
                task_counts.get("succeeded", 0)
                + task_counts.get("failed", 0)
                + task_counts.get("cancelled", 0)
            ),
            "task_total": task_counts.get("total", 0),
        }

    def _normalize_dynamic_run_snapshot(self, snapshot:Dict[str,Any]):
        status_map = {
            "created": "created",
            "running": "running",
            "finalized": "succeeded",
            "failed": "failed",
            "canceled": "cancelled",
            "timed_out": "timed_out",
            "interrupted": "interrupted",
        }
        normalized = copy.deepcopy(snapshot)
        normalized["kind"] = "dynamic"
        normalized["run_type"] = "dynamic"
        normalized["native_status"] = snapshot.get("status")
        normalized["status"] = status_map.get(snapshot.get("status"), snapshot.get("status"))
        normalized["result_summary"] = snapshot.get("final_result")
        normalized["error_summary"] = snapshot.get("failure_reason") or snapshot.get("cancel_reason")
        return normalized

    def _normalize_dynamic_run_summary(self, snapshot:Dict[str,Any]):
        normalized = self._normalize_dynamic_run_snapshot(snapshot)
        normalized["summary"] = True
        return normalized

    async def list_runs(
        self,
        status: str | None = None,
        kind: str | None = None,
        limit: int | None = None,
        detail: bool = False,
    ):
        runs = []
        include_static = kind in (None, "static")
        include_dynamic = kind in (None, "dynamic")

        if include_static:
            static_runs = self.static_run_store.list_runs(summary=not detail)
            runs.extend(static_runs)

        if include_dynamic:
            dynamic_runs = self.dynamic_run_store.list_runs(summary=not detail)
            normalized_dynamic = [
                self._normalize_dynamic_run_snapshot(run) if detail else self._normalize_dynamic_run_summary(run)
                for run in dynamic_runs
            ]
            runs.extend(normalized_dynamic)

        if status:
            runs = [run for run in runs if run.get("status") == status or run.get("native_status") == status]

        runs.sort(key=lambda item: item.get("created_time") or 0, reverse=True)
        if limit is not None:
            runs = runs[: max(0, limit)]
        return runs

    async def get_run_snapshot(self, run_id:str):
        try:
            return self._get_static_run_snapshot(run_id)
        except Exception:
            pass

        if run_id in self.dynamic_runs:
            return self._normalize_dynamic_run_snapshot(
                self.get_dynamic_run(run_id).snapshot(
                    lambda result: summarize_task_result(result, run_id=run_id)
                )
            )
        return self._normalize_dynamic_run_snapshot(self.dynamic_run_store.load_run(run_id))

    async def get_run_events(self, run_id:str, after:int|None=None):
        try:
            return self._get_static_run_events(run_id, after)
        except Exception:
            pass

        if run_id in self.dynamic_runs:
            await self._refresh_dynamic_timeout(run_id)
            return self.get_dynamic_run(run_id).get_events(after)
        self.dynamic_run_store.load_run(run_id)
        return self.dynamic_run_store.load_events(run_id, after)

    async def get_run_tasks(self, run_id:str):
        try:
            return self._get_static_run_tasks(run_id)
        except Exception:
            pass

        snapshot = await self.get_run_snapshot(run_id)
        task_nodes = snapshot.get("task_nodes") or {}
        return list(task_nodes.values())

    async def get_run_task(self, run_id:str, task_id:str):
        try:
            return self._get_static_run_task(run_id, task_id)
        except Exception:
            pass

        snapshot = await self.get_run_snapshot(run_id)
        task_nodes = snapshot.get("task_nodes") or {}
        if task_id not in task_nodes:
            raise ValueError(f"Task not found in run {run_id}: {task_id}")
        return task_nodes[task_id]

    def _artifacts_from_task_snapshot(self, run_id: str, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        manifest = task.get("file_manifest") or {}
        artifacts = []
        for file_info in manifest.get("files") or []:
            artifact = copy.deepcopy(file_info)
            artifact.setdefault("run_id", run_id)
            artifact.setdefault("task_id", task.get("task_id") or manifest.get("task_id"))
            artifact.setdefault("producer_task_id", artifact.get("task_id"))
            artifacts.append(artifact)
        return artifacts

    def _artifacts_from_run_events(self, run_id: str, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        artifacts = []
        seen = set()

        def add_artifact(artifact: Dict[str, Any], data: Dict[str, Any] | None = None):
            artifact = copy.deepcopy(artifact)
            if not artifact:
                return
            if not artifact.get("sha256") or not artifact.get("path"):
                return
            data = data or {}
            artifact.setdefault("run_id", run_id)
            artifact.setdefault("task_id", data.get("task_id"))
            artifact.setdefault("producer_task_id", artifact.get("task_id"))
            dedupe_key = (
                artifact.get("sha256"),
                artifact.get("task_id"),
                artifact.get("path"),
            )
            if dedupe_key in seen:
                return
            seen.add(dedupe_key)
            artifacts.append(artifact)

        def walk_result_summary(value: Any, data: Dict[str, Any] | None = None):
            if isinstance(value, dict):
                artifact = value.get("artifact")
                if isinstance(artifact, dict):
                    add_artifact(artifact, data)
                for item in value.values():
                    walk_result_summary(item, data)
            elif isinstance(value, list):
                for item in value:
                    walk_result_summary(item, data)

        for event in events:
            data = event.get("data") or {}
            if event.get("type") == "agent_tool_output_artifact":
                add_artifact(data.get("artifact") or {}, data)
                continue
            if event.get("type") in {"finish_task", "finish_workflow"}:
                walk_result_summary(data.get("result"), data)
        return artifacts

    async def get_run_artifacts(self, run_id:str):
        snapshot = await self.get_run_snapshot(run_id)
        task_nodes = snapshot.get("task_nodes") or {}
        events = await self.get_run_events(run_id)
        artifacts = []
        for task in task_nodes.values():
            artifacts.extend(self._artifacts_from_task_snapshot(run_id, task))
        artifacts.extend(self._artifacts_from_run_events(run_id, events))
        artifacts.sort(key=lambda item: (item.get("task_id") or "", item.get("path") or ""))
        return artifacts

    async def get_run_task_artifacts(self, run_id:str, task_id:str):
        task = await self.get_run_task(run_id, task_id)
        artifacts = self._artifacts_from_task_snapshot(run_id, task)
        events = await self.get_run_events(run_id)
        artifacts.extend([
            artifact
            for artifact in self._artifacts_from_run_events(run_id, events)
            if artifact.get("task_id") == task_id or artifact.get("producer_task_id") == task_id
        ])
        artifacts.sort(key=lambda item: item.get("path") or "")
        return artifacts

    def _event_log_line(self, event:Dict[str,Any]) -> Dict[str,Any]:
        data = event.get("data") or {}
        task_id = data.get("task_id")
        message = data.get("pending_reason") or data.get("reason")
        if not message:
            error = data.get("error") or data.get("result")
            if isinstance(error, dict):
                message = error.get("message") or error.get("error_type")
            elif error:
                message = str(error)
        if not message:
            if event.get("type") == "start_task":
                message = "task started"
            elif event.get("type") == "finish_task":
                message = "task finished"
            elif event.get("type") == "finish_workflow":
                message = "run finished"
            else:
                message = event.get("type")
        return {
            "timestamp": event.get("timestamp"),
            "seq": event.get("seq"),
            "stream": "event",
            "task_id": task_id,
            "type": event.get("type"),
            "message": message,
        }

    def _read_artifact_text(self, artifact:Dict[str,Any], max_bytes:int=256_000) -> str | None:
        storage_path = artifact.get("storage_path")
        source_path = Path(storage_path) if storage_path else None
        if source_path is None and artifact.get("sha256"):
            source_path = LocalCASArtifactStore().blob_path(artifact["sha256"])
        if source_path is None or not source_path.is_file():
            return None
        with source_path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        return data.decode("utf-8", errors="replace")

    async def get_run_logs(self, run_id:str, tail:int|None=500, task_id:str|None=None):
        events = await self.get_run_events(run_id)
        artifacts = await self.get_run_artifacts(run_id)
        lines = []

        for event in events:
            line = self._event_log_line(event)
            if task_id and line.get("task_id") not in (None, task_id):
                continue
            lines.append(line)

        log_artifacts = [
            artifact for artifact in artifacts
            if (not task_id or artifact.get("task_id") == task_id or artifact.get("producer_task_id") == task_id)
            and str(artifact.get("path") or "").startswith("logs/")
            and str(artifact.get("path") or "").rsplit("/", 1)[-1].startswith("maze-command")
        ]

        for artifact in sorted(log_artifacts, key=lambda item: item.get("path") or ""):
            text = self._read_artifact_text(artifact)
            if text is None:
                continue
            stream = "stderr" if str(artifact.get("path", "")).endswith(".stderr") else "stdout"
            if str(artifact.get("path", "")).endswith(".json"):
                stream = "metadata"
            for index, content in enumerate(text.splitlines()):
                lines.append({
                    "timestamp": None,
                    "seq": None,
                    "stream": stream,
                    "task_id": artifact.get("task_id") or artifact.get("producer_task_id"),
                    "path": artifact.get("path"),
                    "line": index + 1,
                    "message": content,
                })

        if tail is not None and tail >= 0:
            lines = lines[-tail:]
        return {
            "run_id": run_id,
            "task_id": task_id,
            "line_count": len(lines),
            "lines": lines,
        }

    async def _handle_dynamic_scheduler_event(self, message:Dict[str,Any]):
        run_id = message["data"]["workflow_id"]
        task_id = message["data"]["task_id"]
        dynamic_run = self.get_dynamic_run(run_id)
        message_type = message["type"]

        if dynamic_run.is_terminal():
            return

        if message_type == "start_task":
            dynamic_run.mark_started(task_id)
            await self._emit_dynamic_event(run_id, message)
            return

        if message_type == "finish_task":
            file_manifest = self._publish_task_file_manifest(message["data"])
            dynamic_run.set_task_file_manifest(task_id, file_manifest)
            ready_tasks = dynamic_run.mark_finished(task_id)
            await self._emit_dynamic_event(run_id, message)
            self._persist_dynamic_run(run_id)
            for ready_task in ready_tasks:
                await self._emit_dynamic_event(run_id, {
                    "type": "task_ready",
                    "data": {
                        "run_id": run_id,
                        "task_id": ready_task.task_id,
                        "task_name": ready_task.task_name,
                    },
                })
                self._submit_dynamic_task(ready_task)
            return

        if message_type == "task_pending":
            await self._emit_dynamic_event(run_id, message)
            return

        if message_type == "task_retry":
            dynamic_run.mark_retrying(task_id, message.get("data", {}).get("error"))
            await self._emit_dynamic_event(run_id, message)
            return

        if message_type == "task_exception":
            error = message.get("data", {}).get("error", message.get("data", {}).get("result"))
            dynamic_run.mark_failed(task_id, error)
            await self._emit_dynamic_event(run_id, message)
            return
        
    def get_ray_head_port(self):
        '''
        Get the ray head port.

        '''
        return self.ray_head_port

    async def get_cluster_resources(self, timeout: float = 5.0):
        self._require_scheduler_available()
        request_id = str(uuid.uuid4())
        response_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.cluster_resource_requests[request_id] = response_queue

        message = {
            "type": "get_cluster_resources",
            "data": {
                "request_id": request_id,
            },
        }
        try:
            self._send_scheduler_message(message)
            return await self._wait_for_scheduler_response(
                response_queue,
                timeout=timeout,
                operation="return cluster resources",
            )
        finally:
            self.cluster_resource_requests.pop(request_id, None)

    async def get_cluster_queues(self, timeout: float = 5.0):
        self._require_scheduler_available()
        request_id = str(uuid.uuid4())
        response_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.cluster_queue_requests[request_id] = response_queue

        message = {
            "type": "get_cluster_queues",
            "data": {
                "request_id": request_id,
            },
        }
        try:
            self._send_scheduler_message(message)
            return await self._wait_for_scheduler_response(
                response_queue,
                timeout=timeout,
                operation="return cluster queues",
            )
        finally:
            self.cluster_queue_requests.pop(request_id, None)
    
    async def start_worker(self,node_ip:str,node_id:str,resources:Dict, capabilities: Dict | None = None, timeout: float = 5.0):
        self._require_scheduler_available()
        request_id = str(uuid.uuid4())
        response_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.worker_registration_requests[request_id] = response_queue
        message = {
            "type":"start_worker",
            "data":{
                "request_id":request_id,
                "node_ip":node_ip,
                "node_id":node_id,
                "resources":resources,
                "capabilities":capabilities or {"workspace_sandbox": True, "docker_sandbox": False},
            }
        }
        try:
            self._send_scheduler_message(message)
            return await self._wait_for_scheduler_response(
                response_queue,
                timeout=timeout,
                operation="register the worker",
            )
        finally:
            self.worker_registration_requests.pop(request_id, None)

    def init(self,ray_head_port,strategy):
        '''
        Initialize.
        '''
        self._cleanup_started = False
        self._cleanup_complete = False
        self._scheduler_failure_handled = None
        self._scheduler_exit_progress = None
        self._scheduler_fatal_waiters_notified = None
        self._scheduler_fatal_exit_state = None
        self._scheduler_shutdown_requested = False
        self._scheduler_fatal_event = mp.Event()
        self._scheduler_owner_cleanup_complete_event = mp.Event()
        self._scheduler_ray_cleanup_complete_event = mp.Event()
        self._scheduler_owner_id = uuid.uuid4().hex
        self._scheduler_owner_nodes = {}
        (
            self._scheduler_owner_node_receiver,
            owner_node_sender,
        ) = mp.Pipe(duplex=False)
        self.strategy = strategy
        self.ray_head_port = ray_head_port
        available_ports = get_available_ports(2)
      
        port1 = available_ports[0]
        port2 = available_ports[1]
        
        #Create the scheduler process and wait for it to be ready
        self.ready_queue = mp.Queue()
        self.scheduler_process = mp.Process(
            target=scheduler_process,
            args=(
                port1,
                port2,
                self.strategy,
                self.ray_head_port,
                self.ready_queue,
                self._scheduler_fatal_event,
                self._scheduler_owner_id,
                self._scheduler_owner_cleanup_complete_event,
                self._scheduler_ray_cleanup_complete_event,
                owner_node_sender,
            ),
        )
        try:
            self.scheduler_process.start()
        finally:
            owner_node_sender.close()

        self.send_context = zmq.Context()
        self.socket_to_scheduler = self.send_context.socket(zmq.DEALER)
        self.socket_to_scheduler.connect(f"tcp://127.0.0.1:{port1}")

        self.context = zmq.asyncio.Context()
        self.socket_from_scheduler = self.context.socket(zmq.ROUTER)
        self.socket_from_scheduler.bind(f"tcp://127.0.0.1:{port2}")

        try:
            self._wait_for_scheduler_ready()
        except Exception:
            self._abort_scheduler_start()
            raise
 
    async def monitor_coroutine(self):
        '''
        Monitor the task from the scheduler process.
        '''
        while True:
            try:
                frames = await self.socket_from_scheduler.recv_multipart()
                assert(len(frames)==2)
                _, data = frames
                message = json.loads(data.decode('utf-8'))
 
                message_type = message["type"]
                message_data = message.get("data", {})
              
                async with self.lock:
                    if message_type == "cluster_resources":
                        request_id = message_data.get("request_id")
                        response_queue = self.cluster_resource_requests.get(request_id)
                        if response_queue is not None:
                            await response_queue.put(message_data.get("resources", {}))
                        continue

                    if message_type == "cluster_queues":
                        request_id = message_data.get("request_id")
                        response_queue = self.cluster_queue_requests.get(request_id)
                        if response_queue is not None:
                            await response_queue.put(message_data.get("queues", {}))
                        continue

                    if message_type == "worker_started":
                        request_id = message_data.get("request_id")
                        response_queue = self.worker_registration_requests.get(request_id)
                        if response_queue is not None:
                            await response_queue.put(message_data.get("worker", {}))
                        continue

                    if message_type in {"start_task", "finish_task", "task_retry", "task_exception"}:
                        if not self._accept_task_attempt_event(message_type, message_data):
                            continue

                    if(message_type=="finish_task"):
                        if message_data["task_id"] in self.langgraph_task_requests:
                            que: Queue[Any] = self.langgraph_task_requests[message_data['task_id']]
                            await que.put(message)
                        else:
                            submit_id = message_data['workflow_id']
                            if submit_id in self.dynamic_runs:
                                await self._handle_dynamic_scheduler_event(message)
                                continue
                            if submit_id not in self.async_que or submit_id not in self.submit_workflows:
                                continue

                            static_run = self.static_runs.get(submit_id)
                            if static_run is not None and static_run.is_terminal():
                                continue
                            task_metrics = message_data.get("metrics") or {}
                            file_manifest = self._publish_task_file_manifest(
                                message_data,
                            )
                            if static_run is not None:
                                static_run.mark_task_finished(
                                    message_data["task_id"],
                                    result=message_data.get("result"),
                                    file_manifest=file_manifest,
                                    metrics=task_metrics,
                                    started_at=message_data.get("started_at"),
                                    finished_at=message_data.get("finished_at"),
                                    duration_ms=message_data.get("duration_ms"),
                                    node_id=message_data.get("node_id"),
                                )
                                message = self._record_static_event(submit_id, message)

                            self.global_metrics.on_task_finished(
                                submit_id,
                                message_data["task_id"],
                                "succeeded",
                                task_metrics,
                            )

                            que: Queue[Any] = self.async_que[submit_id]
                            await que.put(message)
 
                            if file_manifest:
                                self.submit_workflows[submit_id].graph.nodes[message_data["task_id"]]["file_manifest"] = file_manifest

                            new_ready_tasks  = self.submit_workflows[submit_id].finish_task(task_id=message_data["task_id"],task_result=message_data["result"],strategy=self.strategy)
                            if len(new_ready_tasks) > 0:
                                for task in new_ready_tasks:
                                    if static_run is not None:
                                        static_run.mark_task_queued(task.task_id)
                                        self._persist_static_run(submit_id)
                                    file_context = self.submit_workflows[submit_id].graph.graph.get("file_context")
                                    data = self._task_run_payload(
                                        self.submit_workflows[submit_id],
                                        task,
                                        submit_id,
                                        file_context,
                                    )
                                    data['priority'] = await self._get_task_priority(
                                        self.submit_workflows[submit_id],
                                        task,
                                    )
                                    message = {
                                        "type":"run_task",
                                        "data":data,
                                    }                 
                                    self._send_scheduler_message(message)

                            if static_run is not None and static_run.status == "succeeded":
                                self.global_metrics.on_run_status_change(
                                    submit_id,
                                    "running",
                                    "succeeded",
                                )
                                finish_message = self._record_static_event(submit_id, {
                                    "type": "finish_workflow",
                                    "data": {
                                        "run_id": submit_id,
                                        "workflow_id": static_run.workflow_id,
                                    },
                                })
                                await que.put(finish_message)
                                clear_message = {"type":"clear_workflow","data":{"workflow_id":submit_id}}
                                self._send_scheduler_message(clear_message)

                    elif(message_type=="start_task" or message_type=="task_pending" or message_type=="task_retry" or message_type=="task_exception"):
                        if message_data["task_id"] in self.langgraph_task_requests:
                            que: Queue[Any] = self.langgraph_task_requests[message_data['task_id']]
                            await que.put(message)
                        else:
                            submit_id = message_data['workflow_id']
                            if submit_id in self.dynamic_runs:
                                await self._handle_dynamic_scheduler_event(message)
                                continue
                            if submit_id not in self.async_que or submit_id not in self.submit_workflows:
                                continue

                            static_run = self.static_runs.get(submit_id)
                            if static_run is not None and static_run.is_terminal():
                                continue
                            if message_type == "start_task":
                                self.submit_workflows[submit_id].mark_task_started(message_data["task_id"])
                                if static_run is not None:
                                    if static_run.status == "created":
                                        self.global_metrics.on_run_status_change(
                                            submit_id,
                                            "submitted",
                                            "running",
                                        )
                                    static_run.mark_task_started(message_data["task_id"], message_data)
                                    message = self._record_static_event(submit_id, message)
                                self.global_metrics.on_task_started(
                                    submit_id,
                                    message_data["task_id"],
                                )
                            elif message_type == "task_pending":
                                if static_run is not None:
                                    static_run.mark_task_pending(
                                        message_data["task_id"],
                                        message_data.get("pending_reason"),
                                        message_data.get("schedule_decision"),
                                    )
                                    message = self._record_static_event(submit_id, message)
                            elif message_type == "task_retry":
                                if static_run is not None:
                                    static_run.mark_task_retry(
                                        message_data["task_id"],
                                        message_data.get("error"),
                                        message_data.get("attempt"),
                                    )
                                    message = self._record_static_event(submit_id, message)
                            elif message_type == "task_exception":
                                if static_run is not None:
                                    error = message_data.get("error", message_data.get("result"))
                                    static_run.mark_task_failed(
                                        message_data["task_id"],
                                        error,
                                    )
                                    message = self._record_static_event(submit_id, message)
                                    if static_run.status == "failed":
                                        self.global_metrics.on_run_status_change(
                                            submit_id,
                                            "running",
                                            "failed",
                                        )
                                self.global_metrics.on_task_finished(
                                    submit_id,
                                    message_data["task_id"],
                                    "failed",
                                    None,
                                )
    
                            que: Queue[Any] = self.async_que[submit_id]
                            await que.put(message)
                    
                    elif message_type in {
                        "finish_llm_instance_launch",
                        "fail_llm_instance_launch",
                        "finish_llm_instance_stop",
                        "fail_llm_instance_stop",
                    }:
                        queue_id = message_data.get("request_id") or message_data["instance_id"]
                        que = self.llm_instance_async_que.get(queue_id)
                        if que is not None:
                            await que.put(message)

            except Exception as e:
                print(f"Error in monitor: {e}")
      
    async def get_workflow_res(self,workflow_id:str,submit_id:str,websocket:WebSocket):    
        """
        Get the workflow result and send to websocket.
        """
        que = self.async_que[submit_id]
        assert que != None

        while True:
            data = await que.get()
            await websocket.send_json(data)

            if data["type"] in {
                "finish_workflow",
                "timeout_workflow",
                "interrupt_workflow",
                "cancel_workflow",
            }:
                if data["type"] == "finish_workflow":
                    message = {"type":"clear_workflow","data":{"workflow_id":submit_id}}
                    self._send_scheduler_message(message)
                break
            elif data["type"]=="task_exception":
                raise Exception("task_exception")

    async def get_dynamic_run_res(self, run_id:str, websocket:WebSocket):
        dynamic_run = self.get_dynamic_run(run_id)
        que = self.async_que[run_id]
        event_index = 0

        while True:
            await self._refresh_dynamic_timeout(run_id)
            while event_index < len(dynamic_run.event_log):
                event = dynamic_run.event_log[event_index]
                event_index += 1
                await websocket.send_json(event)
                if event["type"] in ("finish_workflow", "task_exception", "cancel_dynamic_run", "timeout_dynamic_run", "interrupt_dynamic_run"):
                    if dynamic_run.is_terminal():
                        return

            if dynamic_run.is_terminal():
                return

            timeout = dynamic_run.seconds_until_timeout()
            try:
                await asyncio.wait_for(que.get(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._refresh_dynamic_timeout(run_id)
          
    async def stop_workflow(self,submit_id:str):
        '''
        Stop workflow
        '''
        async with self.lock:
            self.async_que.pop(submit_id, None)
            static_run = self.static_runs.get(submit_id)
            if static_run is not None:
                static_run.mark_cancelled("Workflow stopped")
                self.global_metrics.on_run_status_change(submit_id, "running", "canceled")
                self._record_static_event(submit_id, {
                    "type": "cancel_workflow",
                    "data": {
                        "run_id": submit_id,
                        "workflow_id": static_run.workflow_id,
                        "reason": "Workflow stopped",
                    },
                })

        self._stop_workflow_best_effort(submit_id)
    
    async def run_langgraph_task(
        self,
        workflow_id: str,
        task_id: str,
        args: str,
        kwargs: str,
        timeout: float = SCHEDULER_RESPONSE_TIMEOUT_SECONDS,
    ):
        """
        Run langgraph task
        """
        self._require_scheduler_available()
        que: Queue[Any] = asyncio.Queue()
        self.langgraph_task_requests[task_id] = que

        task: LangGraphTask = self.workflows[workflow_id].get_task(task_id)
        task.set_args(args)
        task.set_kwargs(kwargs)
        data = task.to_json()
        data['priority'] = 0
        message: dict[str, str] = {
            "type":"run_task",
            "data":data,
        }
        try:
            self._send_scheduler_message(message)
            result = None
            deadline = time.monotonic() + max(0.0, float(timeout))
            while True:
                message = await self._wait_for_scheduler_response(
                    que,
                    timeout=max(0.0, deadline - time.monotonic()),
                    operation="finish the LangGraph task",
                )
                message_type = message["type"]
                message_data = message["data"]

                if message_type=="finish_task":
                    result = message_data["result"]
                    break
                elif message_type=="task_exception":
                    result = message_data["result"]
                    break
        finally:
            self.langgraph_task_requests.pop(task_id, None)
        return result
    
    async def start_llm_instance(
        self,
        instance_id: str,
        model: str,
        cpu_nums: int,
        gpu_nums: int,
        memory: int,
        gpu_mem: int,
        backend: str = "vllm",
        backend_args: dict | None = None,
        timeout: float = SCHEDULER_RESPONSE_TIMEOUT_SECONDS,
    ):
        response_queue = asyncio.Queue()
        self.llm_instance_async_que[instance_id] = response_queue
        message = {
            "type": "start_llm_instance",
            "data": {
                "instance_id": instance_id,
                "model": model,
                "backend": backend,
                "cpu_nums": cpu_nums,
                "gpu_nums": gpu_nums,
                "gpu_mem": gpu_mem,
                "memory": memory,
                "backend_args": backend_args or {},
            },
        }
        try:
            self._send_scheduler_message(message)
            response = await self._wait_for_scheduler_response(
                response_queue,
                timeout=timeout,
                operation="start the model instance",
            )
        finally:
            self.llm_instance_async_que.pop(instance_id, None)
        data = response["data"]
        if response["type"] == "fail_llm_instance_launch":
            raise RuntimeError(data["error"])
        return data

    async def stop_llm_instance(
        self,
        instance_id: str,
        timeout: float = SCHEDULER_RESPONSE_TIMEOUT_SECONDS,
    ):
        request_id = str(uuid.uuid4())
        response_queue = asyncio.Queue()
        self.llm_instance_async_que[request_id] = response_queue
        message = {
            "type": "stop_llm_instance",
            "data": {"instance_id": instance_id, "request_id": request_id},
        }
        try:
            self._send_scheduler_message(message)
            response = await self._wait_for_scheduler_response(
                response_queue,
                timeout=timeout,
                operation="stop the model instance",
            )
        finally:
            self.llm_instance_async_que.pop(request_id, None)
        if response["type"] == "fail_llm_instance_stop":
            raise RuntimeError(response["data"]["error"])
        return response["data"]
