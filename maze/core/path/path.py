import hashlib
import math
import os
import time
import uuid
import httpx
import json
import copy
import logging
import queue
import socket
import threading
import ray
from datetime import datetime, timezone
import zmq
import zmq.asyncio
import asyncio
import multiprocessing as mp
from fastapi import WebSocket
from pathlib import Path
from typing import Any,Dict,List
from urllib.parse import urlsplit
from asyncio.queues import Queue
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow
from maze.core.workflow.dynamic import DynamicRun, TERMINAL_DYNAMIC_RUN_STATUSES, dynamic_task_spec_from_payload
from maze.core.workflow.dynamic_store import DynamicRunStore
from maze.core.workflow.dag_spec import build_dag_workflow, dag_definition_from_spec, dag_spec_from_payload
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
from maze.core.scheduler.error import exception_to_error_envelope
from maze.core.scheduler.strategy import DEFAULT_PREDICTED_DURATION_SECONDS, normalize_scheduling_algorithm
from maze.core.scheduler.runtime_estimator import RuntimeEstimator, RuntimePrediction
from maze.core.files.artifact_store import LocalCASArtifactStore
from maze.core.files.lineage import ArtifactError, publish_task_file_manifest
from maze.core.resource_history import ResourceHistoryStore
from maze.core.workflow.resources import ResourceSpecError, normalize_task_semantics, require_schedulable_resources
from maze.utils.utils import get_available_ports

logger = logging.getLogger(__name__)
EPSILON = 1e-3
NODE_SCHEDULING_POLICIES = {"default", "least-loaded", "prefer-gpu-free", "spread"}
RUN_INPUT_REF_MARKER = "__maze_run_input__"
SCHEDULER_START_TIMEOUT_SECONDS = 60.0
SCHEDULER_START_POLL_SECONDS = 0.1
SCHEDULER_RESPONSE_TIMEOUT_SECONDS = 600.0
SCHEDULER_FATAL_EXIT_GRACE_SECONDS = 90.0
SCHEDULER_FATAL_TERMINATE_TIMEOUT_SECONDS = 5.0
SCHEDULER_FATAL_KILL_TIMEOUT_SECONDS = 5.0
SCHEDULER_PROCESS_EXIT_POLL_SECONDS = 0.05
SCHEDULER_START_ABORT_CLEANUP_ATTEMPTS = 3
RUN_WORKFLOW_DISPATCH_FIELD = "dispatch"
RUN_WORKFLOW_CLEANUP_RETRY_SECONDS = 0.25
RUN_WORKFLOW_CLEANUP_MAX_ATTEMPTS = 5


def _global_metrics_static_status(status: Any) -> str:
    if status in {"created", "submitted"}:
        return "submitted"
    if status == "cancelled":
        return "canceled"
    return str(status or "submitted")


class SchedulerUnavailableError(RuntimeError):
    error_code = "scheduler_unavailable"

    def __init__(self, message: str, *, pid: int | None = None, exitcode: int | None = None):
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


class WorkflowNotFoundError(LookupError):
    error_code = "workflow_not_found"

    def __init__(self, workflow_id: str):
        super().__init__(f"Workflow not found: {workflow_id}")
        self.workflow_id = workflow_id

    def detail(self) -> Dict[str, Any]:
        return {
            "code": self.error_code,
            "message": str(self),
            "workflow_id": self.workflow_id,
        }


class WorkflowRunConflictError(ValueError):
    error_code = "workflow_run_conflict"

    def __init__(self, run_id: str, existing_workflow_id: str | None):
        super().__init__("Run ID is already bound to a different workflow submission")
        self.run_id = run_id
        self.existing_workflow_id = existing_workflow_id

    def detail(self) -> Dict[str, Any]:
        return {
            "code": self.error_code,
            "message": str(self),
            "run_id": self.run_id,
            "existing_workflow_id": self.existing_workflow_id,
        }


class WorkflowInitializationError(RuntimeError):
    error_code = "workflow_initialization_failed"

    def __init__(
        self,
        run_id: str,
        workflow_id: str,
        *,
        phase: str,
        message: str,
    ):
        super().__init__(message)
        self.run_id = run_id
        self.workflow_id = workflow_id
        self.phase = phase

    def detail(self) -> Dict[str, Any]:
        return {
            "code": self.error_code,
            "message": str(self),
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "phase": self.phase,
        }


def validate_run_workflow_file_context(
    file_context: Any,
) -> Dict[str, Any] | None:
    """Validate submission file transport before any run state is claimed."""
    if file_context is None:
        return None
    if not isinstance(file_context, dict):
        raise TypeError("file_context must be an object")

    if "enabled" in file_context and not isinstance(file_context["enabled"], bool):
        raise TypeError("file_context.enabled must be a boolean")

    artifact_store = file_context.get("artifact_store")
    if "artifact_store" in file_context and not isinstance(artifact_store, dict):
        raise TypeError("file_context.artifact_store must be an object")
    artifact_store = artifact_store or {}

    workspace_dir = file_context.get("workspace_dir")
    if file_context.get("enabled"):
        if not isinstance(workspace_dir, (str, os.PathLike)):
            raise ValueError("file_context.workspace_dir is required when enabled")
        workspace_path = os.fspath(workspace_dir)
        if not isinstance(workspace_path, str) or not workspace_path.strip():
            raise ValueError("file_context.workspace_dir is required when enabled")
    elif workspace_dir is not None and not isinstance(workspace_dir, (str, os.PathLike)):
        raise TypeError("file_context.workspace_dir must be a path string")

    task_node_ids = file_context.get("task_node_ids")
    if task_node_ids is not None and not isinstance(task_node_ids, dict):
        raise TypeError("file_context.task_node_ids must be an object")

    artifact_root = artifact_store.get("root")
    if artifact_root is not None:
        if not isinstance(artifact_root, (str, os.PathLike)):
            raise TypeError("file_context.artifact_store.root must be a path string")
        root_path = os.fspath(artifact_root)
        if not isinstance(root_path, str) or not root_path.strip():
            raise ValueError(
                "file_context.artifact_store.root must be a non-empty path string"
            )

    if "base_url" in artifact_store:
        base_url = artifact_store["base_url"]
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError(
                "file_context.artifact_store.base_url must be an absolute http(s) URL"
            )
        try:
            parsed = urlsplit(base_url)
            _ = parsed.port
            valid_url = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.netloc)
                and parsed.hostname is not None
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError(
                "file_context.artifact_store.base_url must be an absolute http(s) URL"
            )

    return file_context


def _validate_run_workflow_run_id(run_id: Any) -> str | None:
    if run_id is None:
        return None
    try:
        parsed = uuid.UUID(run_id) if isinstance(run_id, str) else None
    except ValueError:
        parsed = None
    if parsed is None or str(parsed) != run_id:
        raise ValueError("run_id must be a canonical UUID")
    return run_id


def _run_workflow_payload_fingerprint(
    workflow_id: str,
    *,
    file_context: Dict[str, Any] | None,
    timeout_seconds: float | None,
    tags: List[str] | None,
    metadata: Dict[str, Any] | None,
    final_output_refs: Any,
    inputs: Dict[str, Any] | None,
) -> str:
    payload = {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "file_context": copy.deepcopy(file_context),
        "timeout_seconds": timeout_seconds,
        "tags": copy.deepcopy(tags),
        "metadata": copy.deepcopy(metadata),
        "final_output_refs": {
            "present": final_output_refs is not FINAL_OUTPUT_REFS_UNSET,
            "value": (
                None
                if final_output_refs is FINAL_OUTPUT_REFS_UNSET
                else copy.deepcopy(final_output_refs)
            ),
        },
        "inputs": copy.deepcopy(inputs),
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Workflow submission payload must be valid JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


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
    if isinstance(value, (list, tuple)):
        resolved = []
        referenced = set()
        for item in value:
            resolved_item, item_references = _resolve_run_input_refs(item, inputs)
            resolved.append(resolved_item)
            referenced.update(item_references)
        return resolved, referenced
    return copy.deepcopy(value), set()


def _bind_workflow_run_inputs(
    workflow: Workflow,
    inputs: Dict[str, Any] | None,
) -> Dict[str, Any]:
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
                input_info.get("value"),
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
        self._workflow_submission_lock = threading.RLock()

        self.workflows: Dict[str, Workflow] = {}
        self.submit_workflows: Dict[str, Workflow] = {}
        self.static_runs: Dict[str, StaticRun] = {}
        self.static_run_store = StaticRunStore()
        self._core_process_lease = self.static_run_store.acquire_core_process_lease()
        try:
            self.static_run_store.recover_interrupted_runs()
        except BaseException:
            self._core_process_lease.release()
            raise
        self.dynamic_runs: Dict[str, DynamicRun] = {}
        self.dynamic_run_store = DynamicRunStore()
        try:
            self.dynamic_run_store.recover_interrupted_runs()
        except BaseException:
            self._core_process_lease.release()
            raise
        self.task_attempts: Dict[tuple[str, str], Dict[str, Any]] = {}
        self.pre_dispatch_rejections: set[tuple[str, str]] = set()
        self.async_que: Dict[str, asyncio.Queue] = {} 
        self.llm_instance_async_que: Dict[str, asyncio.Queue] = {}
        self.cluster_resource_requests: Dict[str, asyncio.Queue] = {}
        self.cluster_queue_requests: Dict[str, asyncio.Queue] = {}
        self.worker_registration_requests: Dict[str, asyncio.Queue] = {}
        self.cluster_control_requests: Dict[str, asyncio.Queue] = {}
        self.workflow_cleanup_requests: Dict[str, str] = {}
        self.workflow_cleanup_retries: Dict[str, Dict[str, Any]] = {}
        self._scheduler_failure_handled: tuple[int | None, int | None] | None = None

        self.global_metrics = GlobalMetrics()
        self.resource_history = ResourceHistoryStore()
        self.runtime_estimator = RuntimeEstimator()
        for snapshot in self.static_run_store.list_runs():
            run_id = snapshot.get("run_id")
            self.global_metrics.on_run_submitted(run_id)
            metrics_status = _global_metrics_static_status(snapshot.get("status"))
            if metrics_status != "submitted":
                self.global_metrics.on_run_status_change(
                    run_id,
                    "submitted",
                    metrics_status,
                )
         
    def cleanup(self):
        '''
        Clean up the main process and scheduler process.
        '''
        if getattr(self, "_cleanup_complete", False):
            return True
        self._cleanup_started = True

        self.request_scheduler_shutdown()

        scheduler_cleanup_complete, scheduler_cleanup_error = (
            self._stop_scheduler_process(graceful_timeout=75)
        )
        if not scheduler_cleanup_complete:
            logger.error("Scheduler cleanup remains incomplete: %s", scheduler_cleanup_error)

        runtime_cleanup_complete = self._stop_local_ray_best_effort() is True
        cleanup_complete = scheduler_cleanup_complete and runtime_cleanup_complete
        self._cleanup_complete = cleanup_complete
        if cleanup_complete:
            self._close_scheduler_channels()
            self._release_core_process_lease()
        if not cleanup_complete:
            self._cleanup_started = False
        return cleanup_complete

    def _stop_scheduler_process(
        self,
        *,
        graceful_timeout: float,
    ) -> tuple[bool, str | None]:
        scheduler_process = getattr(self, "scheduler_process", None)
        if scheduler_process is None:
            return True, None
        try:
            pid = scheduler_process.pid
        except Exception as exc:
            return False, f"Scheduler process identity could not be read: {exc}"
        if not pid:
            return True, None
        if pid == os.getpid():
            return False, f"Refusing to stop Scheduler process from itself (pid={pid})"

        try:
            scheduler_process.join(timeout=graceful_timeout)
            if scheduler_process.is_alive():
                scheduler_process.terminate()
                scheduler_process.join(timeout=SCHEDULER_FATAL_TERMINATE_TIMEOUT_SECONDS)
            if scheduler_process.is_alive():
                kill = getattr(scheduler_process, "kill", None)
                if kill is not None:
                    kill()
                    scheduler_process.join(timeout=SCHEDULER_FATAL_KILL_TIMEOUT_SECONDS)
            if scheduler_process.is_alive():
                return (
                    False,
                    "Scheduler process remained alive after terminate/kill "
                    f"(pid={pid})",
                )
        except Exception as exc:
            return (
                False,
                "Scheduler process exit could not be confirmed "
                f"(pid={pid}): {exc}",
            )
        return True, None

    def _release_core_process_lease(self) -> None:
        lease = getattr(self, "_core_process_lease", None)
        if lease is None:
            return
        self._core_process_lease = None
        lease.release()

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
        return owner_cleanup_complete and ray_cleanup_complete and local_cleanup is True

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
        cleanup_failures = []
        scheduler_cleanup_complete, scheduler_cleanup_error = (
            self._stop_scheduler_process(graceful_timeout=1)
        )
        if not scheduler_cleanup_complete:
            cleanup_failures.append(scheduler_cleanup_error or "unknown Scheduler error")

        ray_cleanup_complete = False
        ray_cleanup_errors = []
        for attempt in range(1, SCHEDULER_START_ABORT_CLEANUP_ATTEMPTS + 1):
            try:
                ray_cleanup_complete = self._stop_local_ray_best_effort() is True
            except Exception as exc:
                ray_cleanup_errors.append(
                    f"attempt {attempt}: {type(exc).__name__}: {exc}"
                )
            if ray_cleanup_complete:
                break
        if not ray_cleanup_complete:
            detail = (
                f" ({'; '.join(ray_cleanup_errors)})"
                if ray_cleanup_errors
                else ""
            )
            cleanup_failures.append(
                "Scheduler owner/Ray cleanup did not complete after "
                f"{SCHEDULER_START_ABORT_CLEANUP_ATTEMPTS} attempts{detail}"
            )

        if scheduler_cleanup_complete:
            self._close_scheduler_channels()
        if cleanup_failures:
            raise RuntimeError(
                "Scheduler startup cleanup failed: " + "; ".join(cleanup_failures)
            )

    def _wait_for_scheduler_ready(
        self,
        timeout: float = SCHEDULER_START_TIMEOUT_SECONDS,
    ):
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

    def _stop_workflow_after_artifact_failure(self, data: Dict[str, Any]):
        error = data.get("error")
        if not (
            isinstance(error, dict)
            and error.get("error_type") == "artifact_error"
            and error.get("origin") == "core"
        ):
            return
        try:
            self._send_scheduler_message({
                "type": "stop_workflow",
                "data": {"workflow_id": data.get("workflow_id")},
            })
        except Exception:
            logger.exception(
                "Could not clean up workflow after artifact validation failure"
            )

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
        detail = f"pid={pid}, exitcode={exitcode}" if pid else f"exitcode={exitcode}"
        return f"scheduler process exited ({detail}). Restart Maze core to recover the scheduler."

    @staticmethod
    def _task_attempt_identity(data: Dict[str, Any]):
        run_id = data.get("workflow_id")
        task_id = data.get("task_id")
        attempt = data.get("attempt")
        dispatch_id = data.get("dispatch_id")
        lease_id = data.get("lease_id")
        if (
            not isinstance(run_id, str)
            or not run_id
            or not isinstance(task_id, str)
            or not task_id
            or not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 1
            or not isinstance(dispatch_id, str)
            or not dispatch_id
            or not isinstance(lease_id, str)
            or not lease_id
        ):
            return None
        return (run_id, task_id), (attempt, dispatch_id, lease_id)

    @staticmethod
    def _task_attempt_node(data: Dict[str, Any]) -> Dict[str, Any] | None:
        schedule_decision = data.get("schedule_decision") or {}
        selected = schedule_decision.get("selected_node") or {}
        node = {
            "node_id": data.get("node_id") or selected.get("node_id"),
            "node_ip": data.get("node_ip") or selected.get("node_ip"),
            "gpu_id": data.get("gpu_id") if data.get("gpu_id") is not None else selected.get("gpu_id"),
        }
        return node if any(value is not None for value in node.values()) else None

    def _validate_task_file_manifest(self, data: Dict[str, Any]) -> Dict[str, Any] | None:
        manifest = data.get("file_manifest")
        if not manifest:
            return None
        if not isinstance(manifest, dict):
            raise ArtifactError("Task file manifest must be an object")

        expected = {
            "run_id": data.get("workflow_id"),
            "task_id": data.get("task_id"),
            "attempt": data.get("attempt"),
            "dispatch_id": data.get("dispatch_id"),
            "lease_id": data.get("lease_id"),
            "published": False,
        }
        for field, value in expected.items():
            if manifest.get(field) != value:
                raise ArtifactError(
                    f"Task manifest {field} does not match the finishing attempt"
                )
        return manifest

    def _begin_task_attempt_event_transaction(
        self,
        message_type: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        task_attempts = getattr(self, "task_attempts", None)
        if task_attempts is None:
            task_attempts = {}
            self.task_attempts = task_attempts
        pre_dispatch_rejections = getattr(self, "pre_dispatch_rejections", None)
        if pre_dispatch_rejections is None:
            pre_dispatch_rejections = set()
            self.pre_dispatch_rejections = pre_dispatch_rejections

        key = (data.get("workflow_id"), data.get("task_id"))
        run_id = data.get("workflow_id")
        run = None
        if run_id in getattr(self, "static_runs", {}):
            run = self.static_runs[run_id]
        elif run_id in getattr(self, "dynamic_runs", {}):
            run = self.dynamic_runs[run_id]

        return {
            "message_type": message_type,
            "key": key,
            "had_attempt": key in task_attempts,
            "attempt": copy.deepcopy(task_attempts.get(key)),
            "had_pre_dispatch_rejection": key in pre_dispatch_rejections,
            "run": run,
            "run_state": copy.deepcopy(run.__dict__) if run is not None else None,
            "data": data,
            "data_state": copy.deepcopy(data),
            "committed": False,
        }

    @staticmethod
    def _commit_task_attempt_event_transaction(transaction: Dict[str, Any] | None):
        if transaction is not None:
            transaction["committed"] = True

    def _rollback_task_attempt_event_transaction(
        self,
        transaction: Dict[str, Any] | None,
    ):
        if transaction is None or transaction.get("committed"):
            return

        key = transaction["key"]
        if transaction["had_attempt"]:
            self.task_attempts[key] = transaction["attempt"]
        else:
            self.task_attempts.pop(key, None)

        pre_dispatch_rejections = getattr(self, "pre_dispatch_rejections", set())
        if transaction["had_pre_dispatch_rejection"]:
            pre_dispatch_rejections.add(key)
        else:
            pre_dispatch_rejections.discard(key)

        run = transaction.get("run")
        run_state = transaction.get("run_state")
        if run is not None and run_state is not None:
            run.__dict__.clear()
            run.__dict__.update(copy.deepcopy(run_state))

        data = transaction.get("data")
        if isinstance(data, dict):
            data.clear()
            data.update(copy.deepcopy(transaction["data_state"]))

    def _accept_task_attempt_event(self, message_type: str, data: Dict[str, Any]) -> bool:
        pre_dispatch_rejections = getattr(self, "pre_dispatch_rejections", None)
        if pre_dispatch_rejections is None:
            pre_dispatch_rejections = set()
            self.pre_dispatch_rejections = pre_dispatch_rejections
        run_id = data.get("workflow_id")
        task_id = data.get("task_id")
        key = (run_id, task_id)
        if message_type == "task_exception" and data.get("pre_dispatch") is True:
            if (
                not isinstance(run_id, str)
                or not run_id
                or not isinstance(task_id, str)
                or not task_id
                or data.get("attempt") != 0
                or data.get("dispatch_id") is not None
                or data.get("lease_id") is not None
                or key in self.task_attempts
                or key in pre_dispatch_rejections
            ):
                return False
            pre_dispatch_rejections.add(key)
            return True

        if key in pre_dispatch_rejections:
            return False
        parsed_identity = self._task_attempt_identity(data)
        if parsed_identity is None:
            return False
        key, identity = parsed_identity
        current = self.task_attempts.get(key)
        if current is not None and current["state"] == "terminal":
            return False
        if message_type == "finish_task":
            self._validate_task_file_manifest(data)

        next_state = (
            "running"
            if message_type == "start_task"
            else "retrying"
            if message_type == "task_retry"
            else "terminal"
        )
        if current is not None:
            current_identity = current["identity"]
            if identity[0] < current_identity[0] or (
                identity[0] == current_identity[0] and identity != current_identity
            ):
                return False
            if identity == current_identity and (
                current["state"] != "running" or message_type == "start_task"
            ):
                return False

        previous = current if current is not None and current["identity"] == identity else {}
        error = None
        if message_type in {"task_retry", "task_exception"}:
            error = data.get("error", data.get("result"))
        selected_node = copy.deepcopy(previous.get("selected_node")) or {}
        selected_node.update({
            key: value
            for key, value in (self._task_attempt_node(data) or {}).items()
            if value is not None
        })
        self.task_attempts[key] = {
            "identity": identity,
            "attempt": identity[0],
            "dispatch_id": identity[1],
            "lease_id": identity[2],
            "state": next_state,
            "event_type": message_type,
            "selected_node": selected_node or None,
            "error": copy.deepcopy(error),
        }
        return True

    def _publish_task_file_manifest(
        self,
        data: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        manifest = self._validate_task_file_manifest(data)
        if not manifest:
            return None

        parsed_identity = self._task_attempt_identity(data)
        if parsed_identity is None:
            raise ArtifactError("Cannot publish a manifest without a task attempt identity")
        key, identity = parsed_identity
        accepted = self.task_attempts.get(key)
        if (
            accepted is None
            or accepted["identity"] != identity
            or accepted.get("state") != "terminal"
            or accepted.get("event_type") != "finish_task"
        ):
            raise ArtifactError("Cannot publish a manifest from an unaccepted task attempt")

        published = copy.deepcopy(manifest)
        published["published"] = True
        published_time = data.get("finished_at") or manifest.get("created_time")
        if published_time is not None:
            published["published_time"] = published_time
        return published

    def _persist_task_file_manifest(
        self,
        data: Dict[str, Any],
        manifest: Dict[str, Any] | None,
    ):
        if not manifest:
            return
        run_id = data.get("workflow_id")
        file_context = None
        if run_id in getattr(self, "dynamic_runs", {}):
            file_context = self.dynamic_runs[run_id].file_context
        elif run_id in getattr(self, "submit_workflows", {}):
            file_context = self.submit_workflows[run_id].graph.graph.get("file_context")
        if not isinstance(file_context, dict) or not file_context.get("enabled"):
            return

        publish_task_file_manifest(
            {
                **file_context,
                "run_id": data.get("workflow_id"),
                "task_id": data.get("task_id"),
                "attempt": data.get("attempt"),
                "dispatch_id": data.get("dispatch_id"),
                "lease_id": data.get("lease_id"),
            },
            manifest,
        )
        
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
        spec = dag_spec_from_payload(spec)
        workflow_id = spec.get("workflow_id") or str(uuid.uuid4())
        existing = self.workflows.get(workflow_id)
        definition = dag_definition_from_spec(spec)
        if existing is not None:
            if existing.graph.graph.get("dag_definition") != definition:
                raise ValueError(
                    f"workflow_id {workflow_id!r} is already bound to a different DAG"
                )
            return workflow_id
        self.workflows[workflow_id] = build_dag_workflow(workflow_id, spec)
        self.global_metrics.on_workflow_created(workflow_id)
        return workflow_id

    def get_workflow(self,workflow_id:str) -> Workflow:
        '''
        Get a workflow.
        '''
        return self.workflows[workflow_id]
  
    def _get_hacs_priority(self, workflow: Workflow, task_id: str):
        node_info = workflow.graph.nodes[task_id]
        n_desc = node_info.get("n_desc", 0)
        pred_time = max(node_info.get("pred_time", 3.0), EPSILON)
        is_dynamic = 0
        omega = math.log2(2.0 + 2.0 * n_desc)
        return (omega, pred_time, is_dynamic)

    def _get_runtime_estimator(self) -> RuntimeEstimator:
        estimator = getattr(self, "runtime_estimator", None)
        if estimator is None:
            estimator = RuntimeEstimator()
            self.runtime_estimator = estimator
        return estimator

    def _runtime_prediction(
        self,
        task: CodeTask,
        task_kind: str,
        default_duration: float,
        default_source: str,
    ) -> RuntimePrediction:
        if not getattr(task, "task_kind", None):
            task.task_kind = task_kind
        prediction = self._get_runtime_estimator().predict(task)
        if prediction.predicted_duration > 0:
            return prediction
        return RuntimePrediction(
            predicted_duration=default_duration,
            prediction_source=default_source,
            confidence=0.0,
            sample_count=0,
            task_kind=task_kind,
            code_hash=prediction.code_hash,
        )

    def _static_scheduling_context(self, workflow: Workflow, task_id: str, submit_id: str) -> Dict[str, Any]:
        node_info = workflow.graph.nodes[task_id]
        task_kind = node_info.get("task_kind") or node_info.get("task_type") or workflow._get_task_type(task_id)
        default_duration = float(
            node_info.get("predicted_duration")
            or node_info.get("pred_time")
            or DEFAULT_PREDICTED_DURATION_SECONDS.get(task_kind, DEFAULT_PREDICTED_DURATION_SECONDS["cpu"])
        )
        prediction_source = node_info.get("prediction_source") or "task_kind_default"
        prediction = self._runtime_prediction(
            workflow.tasks[task_id],
            task_kind,
            default_duration,
            prediction_source,
        )
        predicted_duration = prediction.predicted_duration
        prediction_source = prediction.prediction_source
        node_info["predicted_duration"] = predicted_duration
        node_info["pred_time"] = predicted_duration
        node_info["prediction_source"] = prediction_source
        node_info["prediction_confidence"] = prediction.confidence
        node_info["prediction_sample_count"] = prediction.sample_count
        node_info["code_hash"] = prediction.code_hash
        return {
            "mode": "static",
            "workflow_id": submit_id,
            "workflow_submitted_time": workflow.graph.graph.get("submission_time") or time.time(),
            "task_id": task_id,
            "task_kind": task_kind,
            "predicted_duration": predicted_duration,
            "prediction_source": prediction_source,
            "prediction_confidence": prediction.confidence,
            "prediction_sample_count": prediction.sample_count,
            "code_hash": prediction.code_hash,
            "n_desc": node_info.get("n_desc", 0),
            "n_anc": node_info.get("n_anc", 0),
            "total_value_tasks": workflow.graph.graph.get("total_value_tasks", 0),
            "remaining_value_tasks": workflow.graph.graph.get("remaining_value_tasks", 0),
        }

    async def _get_task_priority(self, workflow: Workflow, task: CodeTask):
        if self.strategy == "HACS":
            return self._get_hacs_priority(workflow, task.task_id)

        return 0

    def _task_run_payload(self, workflow: Workflow, task: CodeTask, submit_id: str, file_context: Dict[str, Any] | None = None):
        data = task.to_json()
        data['workflow_id'] = submit_id
        data["resources"] = self.resource_history.apply(
            data.get("resources"),
            data.get("model_anchor"),
            data.get("task_name"),
        )
        data["task_kind"], data["resources"] = normalize_task_semantics(
            task_kind=data.get("task_kind"),
            resources=data.get("resources"),
            model_anchor=data.get("model_anchor"),
        )
        try:
            require_schedulable_resources(
                data["task_kind"],
                data["resources"],
                data.get("model_anchor"),
            )
        except ResourceSpecError as exc:
            raise ValueError(f"task {task.task_name}: {exc}") from exc

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

        data["scheduling_context"] = self._static_scheduling_context(workflow, task.task_id, submit_id)

        return data

    def _prepare_initial_artifacts(
        self,
        file_context: Dict[str, Any],
        submit_id: str,
        *,
        capability_owner_id: str | None = None,
    ) -> Dict[str, Any]:
        validate_run_workflow_file_context(file_context)
        prepared_context = copy.deepcopy(file_context)
        prepared_context["run_id"] = submit_id
        if (
            not prepared_context
            or not prepared_context.get("enabled")
            or not prepared_context.get("artifact_store")
        ):
            return prepared_context

        from pathlib import Path

        artifact_store = prepared_context.get("artifact_store")
        if not isinstance(artifact_store, dict):
            raise TypeError("file_context.artifact_store must be an object")
        workspace_value = prepared_context.get("workspace_dir")
        if not isinstance(workspace_value, (str, os.PathLike)) or not str(workspace_value):
            raise ValueError("file_context.workspace_dir is required")
        workspace_dir = Path(workspace_value).expanduser().resolve()
        files_dir = workspace_dir / "files"
        prepared_store = dict(prepared_context.get("artifact_store") or {})
        artifact_root = prepared_store.get("root")
        store = LocalCASArtifactStore(artifact_root)
        private = bool(prepared_context.get("private") or prepared_store.get("private"))
        capability = (
            store.create_capability(owner_id=capability_owner_id or submit_id)
            if private
            else None
        )
        if private:
            prepared_context["private"] = True
            prepared_store["private"] = True
            prepared_store["capability"] = capability
        prepared_context["artifact_store"] = prepared_store
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
                artifact = store.put_file(
                    file_path,
                    private=private,
                    capability=capability,
                )
                initial_files.append({
                    "path": relative_path,
                    "name": file_path.name,
                    "size": artifact["size"],
                    "sha256": artifact["sha256"],
                    "artifact_id": artifact["artifact_id"],
                    "storage_uri": artifact["storage_uri"],
                    "private": artifact.get("private", private),
                    "producer_task_id": "__workspace__",
                    "uri": f"maze://runs/{submit_id}/workspace/files/{relative_path}",
                })

        prepared_context["initial_files"] = initial_files
        # Artifact workers consume immutable HTTP references.  Do not send a
        # Head-local absolute workspace path in their scheduler payload.
        prepared_context.pop("workspace_dir", None)
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
        run_id:Any=None,
    ):
        """
        Start a workflow.
        """
        if not isinstance(workflow_id, str) or not workflow_id:
            raise ValueError("workflow_id must be a non-empty string")
        validate_run_workflow_file_context(file_context)
        run_id = _validate_run_workflow_run_id(run_id) or str(uuid.uuid4())
        submission_digest = _run_workflow_payload_fingerprint(
            workflow_id,
            file_context=file_context,
            timeout_seconds=timeout_seconds,
            tags=tags,
            metadata=metadata,
            final_output_refs=final_output_refs,
            inputs=inputs,
        )
        submission_lock = getattr(self, "_workflow_submission_lock", None)
        if submission_lock is None:
            submission_lock = threading.RLock()
            self._workflow_submission_lock = submission_lock
        with submission_lock:
            run_json_path = getattr(self.static_run_store, "run_json_path", None)
            run_path = run_json_path(run_id) if run_json_path is not None else None
            if run_path is not None and run_path.exists():
                existing = self.static_run_store.load_run(run_id)
                if existing.get("submission_digest") != submission_digest:
                    raise WorkflowRunConflictError(
                        run_id,
                        existing.get("workflow_id"),
                    )
                return run_id
            return self._start_workflow(
                workflow_id,
                run_id=run_id,
                submission_digest=submission_digest,
                file_context=file_context,
                timeout_seconds=timeout_seconds,
                tags=tags,
                metadata=metadata,
                final_output_refs=final_output_refs,
                inputs=inputs,
            )

    def _start_workflow(
        self,
        workflow_id: str,
        *,
        file_context: Dict[str, Any] | None,
        timeout_seconds: float | None,
        tags: List[str] | None,
        metadata: Dict[str, Any] | None,
        final_output_refs: Any,
        inputs: Dict[str, Any] | None,
        run_id: str,
        submission_digest: str,
    ) -> str:
        validate_run_workflow_file_context(file_context)
        self._require_scheduler_available()
        submit_workflow = self._workflow_for_submission(workflow_id)
        run_inputs = _bind_workflow_run_inputs(submit_workflow, inputs)
        artifact_store_context = (file_context or {}).get("artifact_store") or {}
        artifact_enabled = bool(
            file_context
            and file_context.get("enabled")
            and artifact_store_context
        )
        private_artifacts = bool(
            artifact_enabled
            and (
                file_context.get("private")
                or artifact_store_context.get("private")
            )
        )
        artifact_store_root = (
            str(LocalCASArtifactStore(artifact_store_context.get("root")).root)
            if private_artifacts
            else None
        )
        submit_workflow.prepare_for_strategy(self.strategy)
        submit_workflow.graph.graph["submission_time"] = time.time()
        static_run = StaticRun(
            run_id,
            workflow_id,
            submit_workflow,
            timeout_seconds=timeout_seconds,
            tags=tags,
            metadata=metadata,
            final_output_refs=final_output_refs,
            run_inputs=run_inputs,
        )
        static_run.submitted_time = submit_workflow.graph.graph["submission_time"]
        static_run._submission_digest = submission_digest
        root_task_ids = [task.task_id for task in submit_workflow.get_start_task()]
        static_run._dispatch = {
            "schema_version": 1,
            "status": "prepared",
            "root_task_ids": root_task_ids,
            "artifact_status": "pending" if artifact_enabled else "none",
            "artifact_owner_id": run_id if private_artifacts else None,
            "artifact_store_root": artifact_store_root,
            "cleanup_request_id": None,
            "error": None,
        }
        self.submit_workflows[run_id] = submit_workflow
        self.async_que[run_id] = asyncio.Queue()
        self.static_runs[run_id] = static_run
        try:
            self._persist_static_run(run_id)
        except Exception:
            self.static_runs.pop(run_id, None)
            self.submit_workflows.pop(run_id, None)
            self.async_que.pop(run_id, None)
            raise

        try:
            prepared_file_context = (
                self._prepare_initial_artifacts(
                    file_context,
                    run_id,
                    capability_owner_id=run_id,
                )
                if file_context
                else None
            )
            if prepared_file_context:
                submit_workflow.graph.graph["file_context"] = prepared_file_context

            root_messages = []
            for task in submit_workflow.get_start_task():
                static_run.mark_task_queued(task.task_id)
                data = self._task_run_payload(
                    submit_workflow,
                    task,
                    run_id,
                    prepared_file_context,
                )
                data["priority"] = (
                    self._get_hacs_priority(submit_workflow, task.task_id)
                    if self.strategy == "HACS"
                    else 0
                )
                root_messages.append(
                    (task.task_id, {"type": "run_task", "data": data})
                )
            if [task_id for task_id, _ in root_messages] != root_task_ids:
                raise RuntimeError("Workflow root tasks changed during submission")

            dispatch = static_run._dispatch
            dispatch["artifact_status"] = (
                "ready" if artifact_enabled else "none"
            )
            self._persist_static_run(run_id)

            static_run._metrics_started = True
            self.global_metrics.on_run_submitted(run_id)

            self._record_static_event(run_id, {
                "type": "start_workflow",
                "data": {
                    "run_id": run_id,
                    "workflow_id": workflow_id,
                    "run_type": "static",
                    "total_task_num": submit_workflow.get_total_task_num(),
                },
            }, persist_run=False)
            # ponytail: one conservative boundary replaces per-root dispatch journals.
            dispatch["status"] = "dispatching"
            self._persist_static_run(run_id)

            for _, message in root_messages:
                self._send_scheduler_message(message)

            dispatch["status"] = "active"
            self._persist_static_run(run_id)
            return run_id
        except Exception as exc:
            raise self._fail_workflow_dispatch(
                static_run,
                exc,
            ) from exc

    def _workflow_for_submission(self, workflow_id: str) -> Workflow:
        try:
            workflow = self.workflows[workflow_id]
        except KeyError as exc:
            raise WorkflowNotFoundError(workflow_id) from exc
        return copy.deepcopy(workflow)

    @staticmethod
    def _dispatch_error(dispatch: Dict[str, Any], exc: Exception) -> Dict[str, Any]:
        return {
            "error_type": "workflow_initialization_failed",
            "message": str(exc) or type(exc).__name__,
            "phase": str(dispatch.get("status") or "prepared"),
            "cause_type": type(exc).__name__,
        }

    @staticmethod
    def _dispatch_requires_cleanup(static_run: StaticRun | None) -> bool:
        if static_run is None:
            return False
        dispatch = getattr(static_run, "_dispatch", None)
        return bool(
            isinstance(dispatch, dict)
            and dispatch.get("status") in {"dispatching", "cleanup_pending"}
        )

    @staticmethod
    def _validate_dispatch(dispatch: Any) -> Dict[str, Any]:
        if (
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
            raise ValueError("Stored workflow dispatch state is invalid")
        return dispatch

    @staticmethod
    def _revoke_dispatch_artifacts(
        run_id: str,
        dispatch: Dict[str, Any],
    ) -> None:
        owner_id = dispatch.get("artifact_owner_id")
        if not owner_id or dispatch.get("artifact_status") == "revoked":
            return
        if owner_id != run_id:
            raise ValueError("Stored artifact owner does not match its run")
        LocalCASArtifactStore(
            dispatch.get("artifact_store_root")
        ).revoke_owner_capabilities(owner_id)
        dispatch["artifact_status"] = "revoked"

    @staticmethod
    def _mark_static_dispatch_failed(
        static_run: StaticRun,
        error: Dict[str, Any],
    ) -> None:
        now = time.time()
        static_run._dispatch["status"] = "terminal"
        static_run._dispatch["error"] = copy.deepcopy(error)
        static_run.status = "failed"
        static_run.finished_time = now
        static_run.error_summary = copy.deepcopy(error)
        for task in static_run.task_nodes.values():
            if task.get("status") not in {"pending", "queued", "running"}:
                continue
            task["status"] = "cancelled"
            task["finished_time"] = now
            task["pending_reason"] = None
            task["error"] = copy.deepcopy(error)
        static_run._touch()

    @staticmethod
    def _mark_snapshot_dispatch_failed(
        snapshot: Dict[str, Any],
        dispatch: Dict[str, Any],
        error: Dict[str, Any],
    ) -> None:
        now = time.time()
        dispatch["status"] = "terminal"
        dispatch["error"] = copy.deepcopy(error)
        snapshot[RUN_WORKFLOW_DISPATCH_FIELD] = dispatch
        snapshot["status"] = "failed"
        snapshot["finished_time"] = now
        snapshot["updated_time"] = now
        snapshot["error_summary"] = copy.deepcopy(error)
        counts = {
            "total": 0,
            "pending": 0,
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "timed_out": 0,
        }
        for task in (snapshot.get("task_nodes") or {}).values():
            if task.get("status") in {"pending", "queued", "running"}:
                task["status"] = "cancelled"
                task["finished_time"] = now
                task["pending_reason"] = None
                task["error"] = copy.deepcopy(error)
            counts["total"] += 1
            if task.get("status") in counts:
                counts[task["status"]] += 1
        completed = sum(
            counts[name]
            for name in ("succeeded", "failed", "cancelled", "timed_out")
        )
        snapshot["task_counts"] = counts
        snapshot["progress"] = {
            "completed": completed,
            "total": counts["total"],
            "fraction": round(completed / (counts["total"] or 1), 6),
        }

    def _record_snapshot_dispatch_failure(
        self,
        snapshot: Dict[str, Any],
        error: Dict[str, Any],
    ) -> None:
        run_id = snapshot["run_id"]
        events = self.static_run_store.load_events(run_id)
        if not any(
            event.get("type") == "workflow_submission_failed"
            for event in events
        ):
            event = {
                "type": "workflow_submission_failed",
                "seq": max(
                    (int(event.get("seq", 0)) for event in events),
                    default=0,
                ) + 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "schema_version": 1,
                "data": {
                    "run_id": run_id,
                    "workflow_id": snapshot.get("workflow_id"),
                    "run_status": "failed",
                    "error": copy.deepcopy(error),
                },
            }
            self.static_run_store.append_event(run_id, event)
            events.append(event)
        snapshot["event_count"] = len(events)
        snapshot["last_event_seq"] = max(
            (int(event.get("seq", 0)) for event in events),
            default=0,
        )
        self.static_run_store.save_run(snapshot)

    def _finish_workflow_dispatch_failure(
        self,
        run_id: str,
        error: Dict[str, Any],
    ) -> None:
        static_run = self.static_runs.get(run_id)
        if static_run is not None:
            dispatch = self._validate_dispatch(static_run._dispatch)
            if dispatch.get("status") == "terminal":
                return
            previous_status = static_run.status
            self._revoke_dispatch_artifacts(run_id, dispatch)
            self._mark_static_dispatch_failed(static_run, error)
            event = self._record_static_event(run_id, {
                "type": "workflow_submission_failed",
                "data": {"error": copy.deepcopy(error)},
            })
            queue_for_run = self.async_que.get(run_id)
            if queue_for_run is not None:
                queue_for_run.put_nowait(copy.deepcopy(event))
            if getattr(static_run, "_metrics_started", False):
                self.global_metrics.on_run_status_change(
                    run_id,
                    _global_metrics_static_status(previous_status),
                    "failed",
                )
            return

        snapshot = self.static_run_store.load_run(run_id)
        dispatch = self._validate_dispatch(
            copy.deepcopy(snapshot.get(RUN_WORKFLOW_DISPATCH_FIELD))
        )
        if dispatch.get("status") == "terminal":
            return
        previous_status = snapshot.get("status")
        self._revoke_dispatch_artifacts(run_id, dispatch)
        self._mark_snapshot_dispatch_failed(snapshot, dispatch, error)
        self._record_snapshot_dispatch_failure(snapshot, error)
        on_status_change = getattr(self.global_metrics, "on_run_status_change", None)
        if on_status_change is not None:
            on_status_change(
                run_id,
                _global_metrics_static_status(previous_status),
                "failed",
            )

    def _fail_workflow_dispatch(
        self,
        static_run: StaticRun,
        exc: Exception,
    ) -> WorkflowInitializationError:
        dispatch = self._validate_dispatch(static_run._dispatch)
        error = self._dispatch_error(dispatch, exc)
        if dispatch["status"] == "prepared":
            self._finish_workflow_dispatch_failure(static_run.run_id, error)
        else:
            self._request_workflow_cleanup(
                static_run.run_id,
                static_run.workflow_id,
                dispatch,
                error,
                lambda: self._persist_static_run(static_run.run_id),
            )
        return WorkflowInitializationError(
            static_run.run_id,
            static_run.workflow_id,
            phase=error["phase"],
            message=error["message"],
        )

    def _cleanup_maps(self) -> tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
        requests = getattr(self, "workflow_cleanup_requests", None)
        retries = getattr(self, "workflow_cleanup_retries", None)
        if requests is None:
            requests = self.workflow_cleanup_requests = {}
        if retries is None:
            retries = self.workflow_cleanup_retries = {}
        return requests, retries

    def _register_workflow_cleanup(
        self,
        run_id: str,
        workflow_id: str,
        request_id: str,
    ) -> Dict[str, Any]:
        requests, retries = self._cleanup_maps()
        mapped_run_id = requests.get(request_id)
        if mapped_run_id not in {None, run_id}:
            raise ValueError("Workflow cleanup request belongs to another run")
        requests[request_id] = run_id
        retry = retries.setdefault(request_id, {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "attempts": 0,
            "next_attempt": 0.0,
        })
        if retry["run_id"] != run_id or retry["workflow_id"] != workflow_id:
            raise ValueError("Workflow cleanup retry belongs to another run")
        return retry

    def _clear_workflow_cleanup(self, request_id: str) -> None:
        requests, retries = self._cleanup_maps()
        requests.pop(request_id, None)
        retries.pop(request_id, None)

    def _send_workflow_cleanup_request(
        self,
        run_id: str,
        workflow_id: str,
        request_id: str,
    ) -> None:
        retry = self._register_workflow_cleanup(
            run_id,
            workflow_id,
            request_id,
        )
        retry["attempts"] += 1
        retry["next_attempt"] = (
            time.monotonic()
            + min(
                5.0,
                RUN_WORKFLOW_CLEANUP_RETRY_SECONDS
                * (2 ** (retry["attempts"] - 1)),
            )
        )
        try:
            result = self._send_scheduler_message({
                "type": "stop_workflow",
                "data": {
                    "workflow_id": run_id,
                    "request_id": request_id,
                },
            })
        except Exception:
            logger.exception(
                "Workflow %s cleanup request %s could not be sent",
                run_id,
                request_id,
            )
            return
        if result is True:
            self._confirm_workflow_cleanup(run_id, request_id)
        elif isinstance(result, dict):
            data = result.get("data", result)
            if data.get("ok") is True:
                self._confirm_workflow_cleanup(run_id, request_id)

    def _request_workflow_cleanup(
        self,
        run_id: str,
        workflow_id: str,
        dispatch: Dict[str, Any],
        error: Dict[str, Any],
        persist,
    ) -> None:
        if dispatch.get("status") != "cleanup_pending":
            dispatch["status"] = "cleanup_pending"
            dispatch["cleanup_request_id"] = str(uuid.uuid4())
            dispatch["error"] = copy.deepcopy(error)
            persist()
        request_id = dispatch.get("cleanup_request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("Pending workflow cleanup has no request ID")
        self._send_workflow_cleanup_request(
            run_id,
            workflow_id,
            request_id,
        )

    def _confirm_workflow_cleanup(
        self,
        run_id: str,
        request_id: str,
    ) -> None:
        static_run = self.static_runs.get(run_id)
        if static_run is not None:
            dispatch = self._validate_dispatch(static_run._dispatch)
            workflow_id = static_run.workflow_id
        else:
            snapshot = self.static_run_store.load_run(run_id)
            dispatch = self._validate_dispatch(
                snapshot.get(RUN_WORKFLOW_DISPATCH_FIELD)
            )
            workflow_id = snapshot.get("workflow_id")
        if (
            dispatch.get("status") != "cleanup_pending"
            or dispatch.get("cleanup_request_id") != request_id
        ):
            self._clear_workflow_cleanup(request_id)
            return
        error = copy.deepcopy(dispatch.get("error")) or {
            "error_type": "workflow_initialization_failed",
            "message": "Workflow submission failed",
            "phase": "cleanup",
            "cause_type": "RuntimeError",
        }
        self._finish_workflow_dispatch_failure(run_id, error)
        self._clear_workflow_cleanup(request_id)
        logger.info(
            "Scheduler confirmed cleanup for workflow %s (%s)",
            run_id,
            workflow_id,
        )

    def _handle_workflow_cleanup_response(
        self,
        data: Dict[str, Any],
    ) -> None:
        request_id = data.get("request_id")
        requests, _ = self._cleanup_maps()
        run_id = requests.get(request_id) or data.get("workflow_id")
        if not isinstance(request_id, str) or not isinstance(run_id, str):
            return
        if data.get("workflow_id") not in {None, run_id}:
            logger.error("Ignoring mismatched workflow cleanup response %s", request_id)
            return
        if data.get("ok") is True:
            self._confirm_workflow_cleanup(run_id, request_id)
        else:
            logger.error(
                "Scheduler rejected workflow %s cleanup request %s: %s",
                run_id,
                request_id,
                data.get("error") or "unknown error",
            )

    def _cleanup_is_pending(self, run_id: str, request_id: str) -> bool:
        static_run = self.static_runs.get(run_id)
        if static_run is not None:
            dispatch = getattr(static_run, "_dispatch", None)
        else:
            try:
                dispatch = self.static_run_store.load_run(run_id).get(
                    RUN_WORKFLOW_DISPATCH_FIELD
                )
            except (OSError, ValueError):
                return False
        return bool(
            isinstance(dispatch, dict)
            and dispatch.get("status") == "cleanup_pending"
            and dispatch.get("cleanup_request_id") == request_id
        )

    def _retry_pending_workflow_cleanups(self) -> None:
        _, retries = self._cleanup_maps()
        now = time.monotonic()
        for request_id, retry in list(retries.items()):
            run_id = retry["run_id"]
            if not self._cleanup_is_pending(run_id, request_id):
                self._clear_workflow_cleanup(request_id)
                continue
            if now < retry["next_attempt"]:
                continue
            if retry["attempts"] >= RUN_WORKFLOW_CLEANUP_MAX_ATTEMPTS:
                error = {
                    "error_type": "workflow_initialization_failed",
                    "message": (
                        "Scheduler cleanup could not be confirmed after "
                        f"{retry['attempts']} attempts; Scheduler shutdown requested"
                    ),
                    "phase": "cleanup",
                    "cause_type": "SchedulerUnavailableError",
                }
                self.request_scheduler_shutdown()
                self._finish_workflow_dispatch_failure(run_id, error)
                self._clear_workflow_cleanup(request_id)
                continue
            self._send_workflow_cleanup_request(
                run_id,
                retry["workflow_id"],
                request_id,
            )

    def _recover_incomplete_workflow_dispatches(self) -> List[str]:
        recovered = []
        for snapshot in self.static_run_store.list_runs():
            dispatch = snapshot.get(RUN_WORKFLOW_DISPATCH_FIELD)
            if dispatch is None:
                continue
            dispatch = self._validate_dispatch(copy.deepcopy(dispatch))
            status = dispatch["status"]
            if status in {"active", "terminal"}:
                continue
            run_id = snapshot.get("run_id")
            workflow_id = snapshot.get("workflow_id")
            if not isinstance(run_id, str) or not isinstance(workflow_id, str):
                raise ValueError("Stored workflow dispatch identity is invalid")
            error = copy.deepcopy(dispatch.get("error")) or {
                "error_type": "workflow_initialization_failed",
                "message": "Head restarted before workflow submission completed",
                "phase": status,
                "cause_type": "ProcessRestart",
            }
            if status == "prepared":
                self._revoke_dispatch_artifacts(run_id, dispatch)
                self._mark_snapshot_dispatch_failed(snapshot, dispatch, error)
                self._record_snapshot_dispatch_failure(snapshot, error)
            else:
                self._request_workflow_cleanup(
                    run_id,
                    workflow_id,
                    dispatch,
                    error,
                    lambda snapshot=snapshot, dispatch=dispatch: (
                        self.static_run_store.save_run({
                            **snapshot,
                            RUN_WORKFLOW_DISPATCH_FIELD: dispatch,
                        })
                    ),
                )
            recovered.append(run_id)
        return recovered

    async def create_dynamic_run(
        self,
        max_tasks:int=100,
        timeout_seconds:int|None=None,
        file_context:Dict[str,Any]|None=None,
        metadata:Dict[str,Any]|None=None,
    ):
        validate_run_workflow_file_context(file_context)
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
                "task_kind": task_spec.task_kind,
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
        model_anchor:Dict[str,Any]|None=None,
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
            model_anchor=model_anchor,
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
                    "task_kind": task.task_kind,
                    "parents": sorted(dynamic_run.task_parents.get(task.task_id, set())),
                    "request_id": request_id,
                    "status": status,
                    "resources": task.resources,
                    "model_anchor": task.model_anchor,
                },
            })

            if task.task_id in dynamic_run.submitted_tasks:
                self._submit_dynamic_task(task)

        return task, idempotent

    async def finalize_dynamic_run(self, run_id:str, result:Any=None):
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
            return self._dynamic_run_snapshot(run_id)
        return self.dynamic_run_store.load_run(run_id)

    async def get_dynamic_run_events(self, run_id:str, after:int|None=None):
        if run_id in self.dynamic_runs:
            await self._refresh_dynamic_timeout(run_id)
            return self.get_dynamic_run(run_id).get_events(after)
        self.dynamic_run_store.load_run(run_id)
        return self.dynamic_run_store.load_events(run_id, after)

    async def emit_dynamic_run_event(self, run_id:str, event:Dict[str,Any]):
        if not isinstance(event, dict):
            raise ValueError("Dynamic run event must be a JSON object")
        unsupported_fields = sorted(set(event) - {"type", "data"})
        if unsupported_fields:
            raise ValueError(
                "Dynamic run event contains unsupported top-level fields: "
                + ", ".join(unsupported_fields)
            )

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

        event_data = event.get("data", {})
        if not isinstance(event_data, dict):
            raise ValueError("Dynamic run event data must be a JSON object")
        reserved_data_fields = sorted(set(event_data) & {"run_id", "run_status"})
        if reserved_data_fields:
            raise ValueError(
                "Dynamic run event data contains server-managed fields: "
                + ", ".join(reserved_data_fields)
            )

        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        dynamic_run.check_can_mutate("emit events")
        stored_event = await self._emit_dynamic_event(run_id, {
            "type": event_type,
            "data": {
                **event_data,
                "run_id": run_id,
            },
        })
        return stored_event

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
        message = {"type":"stop_workflow","data":{"workflow_id":run_id}}
        self._send_scheduler_message(message)

    def _stop_workflow_best_effort(self, run_id: str):
        try:
            self._send_scheduler_message({"type": "stop_workflow", "data": {"workflow_id": run_id}})
        except SchedulerUnavailableError:
            logger.warning("Could not stop workflow %s because the scheduler is unavailable", run_id)
        except Exception:
            logger.exception("Could not stop workflow %s during terminal-state cleanup", run_id)

    async def _refresh_dynamic_timeout(self, run_id:str) -> bool:
        dynamic_run = self.get_dynamic_run(run_id)
        if not dynamic_run.mark_timed_out_if_needed():
            return False

        await self._emit_dynamic_event(run_id, {
            "type": "timeout_dynamic_run",
            "data": {
                "run_id": run_id,
                "timeout_seconds": dynamic_run.timeout_seconds,
            },
        })
        self._stop_workflow_best_effort(run_id)
        return True

    async def _sweep_run_deadlines(self):
        for run_id, static_run in list(self.static_runs.items()):
            previous_status = static_run.status
            deadline = static_run.deadline_time()
            if self._dispatch_requires_cleanup(static_run):
                if deadline is not None and time.time() > deadline:
                    try:
                        dispatch = static_run._dispatch
                        error = dispatch.get("error") or self._dispatch_error(
                            dispatch,
                            TimeoutError(
                                "Workflow submission exceeded the run deadline"
                            ),
                        )
                        self._request_workflow_cleanup(
                            run_id,
                            static_run.workflow_id,
                            dispatch,
                            error,
                            lambda: self._persist_static_run(run_id),
                        )
                    except Exception:
                        logger.exception(
                            "Could not request deadline cleanup for workflow %s",
                            run_id,
                        )
                continue
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
            self.global_metrics.on_run_status_change(run_id, previous_status, "timed_out")
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
                    "deadline_time": dynamic_run.created_time + float(dynamic_run.timeout_seconds),
                },
            })
            self._stop_workflow_best_effort(run_id)

    def _scheduler_event_is_persisted(
        self,
        store,
        run_id: str,
        event: Dict[str, Any],
    ) -> bool:
        load_events = getattr(store, "load_events", None)
        sequence = event.get("seq")
        if load_events is None or sequence is None:
            return False
        for stored_event in load_events(run_id, after=int(sequence) - 1):
            if int(stored_event.get("seq", 0)) != int(sequence):
                continue
            stored_payload = copy.deepcopy(stored_event)
            event_payload = copy.deepcopy(event)
            stored_payload.pop("timestamp", None)
            event_payload.pop("timestamp", None)
            if stored_payload != event_payload:
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

    def _notify_scheduler_exit_waiters(self, reason: str, scheduler_process) -> None:
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
            "llm_instance_async_que",
            "cluster_resource_requests",
            "cluster_queue_requests",
            "worker_registration_requests",
            "cluster_control_requests",
        ):
            requests = getattr(self, attribute, None)
            if requests is None:
                continue
            for response_queue in list(requests.values()):
                try:
                    response_queue.put_nowait(message)
                except asyncio.QueueFull:
                    try:
                        response_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        response_queue.put_nowait(message)
                    except asyncio.QueueFull:
                        logger.error(
                            "Could not supersede a full %s waiter with scheduler failure",
                            attribute,
                        )
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
            "Scheduler pid %s remained alive after its fatal cleanup grace period; terminating it",
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
        logger.critical("Scheduler pid %s ignored termination; killing it", scheduler_pid)
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
            logger.critical("Scheduler pid %s remained alive after kill", scheduler_pid)
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
                reason = self._scheduler_unavailable_message() or "scheduler process is unavailable"
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
        progress = self._scheduler_exit_progress_for(failure, reason)
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
                    if self._dispatch_requires_cleanup(static_run):
                        dispatch = static_run._dispatch
                        request_id = entry.get("dispatch_cleanup_request_id")
                        if request_id is None:
                            if dispatch.get("status") == "cleanup_pending":
                                request_id = dispatch.get("cleanup_request_id")
                                if not isinstance(request_id, str) or not request_id:
                                    raise ValueError(
                                        "Pending workflow cleanup has no request ID"
                                    )
                                self._register_workflow_cleanup(
                                    run_id,
                                    static_run.workflow_id,
                                    request_id,
                                )
                            else:
                                self._request_workflow_cleanup(
                                    run_id,
                                    static_run.workflow_id,
                                    dispatch,
                                    self._dispatch_error(
                                        dispatch,
                                        RuntimeError(reason),
                                    ),
                                    lambda: self._persist_static_run(run_id),
                                )
                                request_id = dispatch.get("cleanup_request_id")
                            entry["dispatch_cleanup_request_id"] = request_id
                        if progress["ray_cleanup_complete"]:
                            self._confirm_workflow_cleanup(run_id, request_id)
                            if not self._dispatch_requires_cleanup(static_run):
                                entry["complete"] = True
                        continue
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
                        entry["snapshot"] = self._static_run_snapshot(static_run)
                    self._persist_scheduler_exit_entry(
                        self.static_run_store,
                        run_id,
                        entry,
                        dynamic=False,
                    )
                    if not entry["metrics_recorded"]:
                        previous_status = entry["previous_status"]
                        self.global_metrics.on_run_status_change(
                            run_id,
                            _global_metrics_static_status(previous_status),
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

        if progress["ray_cleanup_complete"]:
            for run_id, entry in progress["static"].items():
                if entry["complete"]:
                    continue
                request_id = entry.get("dispatch_cleanup_request_id")
                if request_id is None:
                    continue
                static_run = entry["run"]
                try:
                    self._confirm_workflow_cleanup(run_id, request_id)
                    if not self._dispatch_requires_cleanup(static_run):
                        entry["complete"] = True
                except Exception:
                    logger.exception(
                        "Could not persist globally verified dispatch cleanup for workflow %s; will retry",
                        run_id,
                    )

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
        while True:
            try:
                if not getattr(self, "_cleanup_started", False):
                    await self._handle_scheduler_exit()
                    self._retry_pending_workflow_cleanups()
                    await self._sweep_run_deadlines()
                await asyncio.sleep(max(0.1, float(interval_seconds)))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Maze maintenance loop failed")
                await asyncio.sleep(max(0.1, float(interval_seconds)))

    def _dynamic_scheduling_context(self, dynamic_run: DynamicRun, task: CodeTask) -> Dict[str, Any]:
        depths = getattr(dynamic_run, "_hacs_depths", None)
        if not isinstance(depths, dict):
            depths = {}
            setattr(dynamic_run, "_hacs_depths", depths)

        parent_depths = [
            int(depths.get(parent_id, 0))
            for parent_id in dynamic_run.task_parents.get(task.task_id, set())
        ]
        n_anc = (max(parent_depths) + 1) if parent_depths else 0
        depths[task.task_id] = n_anc

        task_kind = task.task_kind or "cpu"
        predicted_duration = DEFAULT_PREDICTED_DURATION_SECONDS.get(
            task_kind,
            DEFAULT_PREDICTED_DURATION_SECONDS["cpu"],
        )
        prediction_source = "task_kind_default"
        prediction = self._runtime_prediction(
            task,
            task_kind,
            predicted_duration,
            prediction_source,
        )
        predicted_duration = prediction.predicted_duration
        prediction_source = prediction.prediction_source
        return {
            "mode": "dynamic",
            "workflow_id": dynamic_run.run_id,
            "workflow_submitted_time": dynamic_run.created_time,
            "task_id": task.task_id,
            "task_kind": task_kind,
            "predicted_duration": predicted_duration,
            "prediction_source": prediction_source,
            "prediction_confidence": prediction.confidence,
            "prediction_sample_count": prediction.sample_count,
            "code_hash": prediction.code_hash,
            "n_desc": 0,
            "n_anc": n_anc,
            "total_value_tasks": 0,
            "remaining_value_tasks": 0,
        }

    def _dynamic_task_run_payload(self, dynamic_run: DynamicRun, task: CodeTask):
        data = task.to_json()
        data['workflow_id'] = task.workflow_id
        data["resources"] = self.resource_history.apply(
            data.get("resources"),
            data.get("model_anchor"),
            data.get("task_name"),
        )
        data["task_kind"], data["resources"] = normalize_task_semantics(
            task_kind=data.get("task_kind"),
            resources=data.get("resources"),
            model_anchor=data.get("model_anchor"),
        )
        try:
            require_schedulable_resources(
                data["task_kind"],
                data["resources"],
                data.get("model_anchor"),
            )
        except ResourceSpecError as exc:
            raise ValueError(f"task {task.task_name}: {exc}") from exc
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
        data["scheduling_context"] = self._dynamic_scheduling_context(dynamic_run, task)
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
        stored_event = None
        sequence = event.get("seq")
        if sequence is not None:
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence <= 0
            ):
                raise ValueError("Dynamic event seq must be a positive integer")
            for candidate in dynamic_run.event_log:
                if candidate.get("seq") != sequence:
                    continue
                if candidate != event:
                    raise RuntimeError(
                        f"Dynamic event sequence conflict for run {run_id}: {sequence}"
                    )
                stored_event = candidate
                break
        if stored_event is None:
            stored_event = dynamic_run.append_event(event)
        snapshot = self._dynamic_run_snapshot(run_id)
        if not self._scheduler_event_is_persisted(
            self.dynamic_run_store,
            run_id,
            stored_event,
        ):
            self.dynamic_run_store.append_event(
                run_id,
                stored_event,
                snapshot=snapshot,
            )
        self.dynamic_run_store.save_run(snapshot)
        que = self.async_que.get(run_id)
        if que is not None:
            await que.put({"type": "dynamic_event"})
        return stored_event

    def _persist_dynamic_scheduler_event(
        self,
        run_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        dynamic_run = self.get_dynamic_run(run_id)
        stored_event = dynamic_run.append_event(event)
        snapshot = self._dynamic_run_snapshot(run_id)
        if not self._scheduler_event_is_persisted(
            self.dynamic_run_store,
            run_id,
            stored_event,
        ):
            self.dynamic_run_store.append_event(
                run_id,
                stored_event,
                snapshot=snapshot,
            )
        self.dynamic_run_store.save_run(snapshot)
        return stored_event

    async def _notify_dynamic_scheduler_event(self, run_id: str):
        que = self.async_que.get(run_id)
        if que is not None:
            await que.put({"type": "dynamic_event"})

    def _persist_dynamic_run(self, run_id:str):
        self.dynamic_run_store.save_run(self._dynamic_run_snapshot(run_id))

    def _dynamic_run_snapshot(self, run_id: str) -> Dict[str, Any]:
        dynamic_run = self.get_dynamic_run(run_id)
        snapshot = dynamic_run.snapshot(
            lambda result: summarize_task_result(result, run_id=run_id)
        )
        task_nodes = snapshot.get("task_nodes") or {}
        for (attempt_run_id, task_id), attempt_state in getattr(
            self,
            "task_attempts",
            {},
        ).items():
            if attempt_run_id != run_id or task_id not in task_nodes:
                continue
            task_node = task_nodes[task_id]
            task_node["attempt"] = attempt_state.get("attempt")
            task_node["dispatch_id"] = attempt_state.get("dispatch_id")
            task_node["lease_id"] = attempt_state.get("lease_id")
            task_node["attempt_state"] = attempt_state.get("state")
            selected_node = attempt_state.get("selected_node")
            if selected_node:
                task_node["selected_node"] = copy.deepcopy(selected_node)
            if task_node.get("error") is None and attempt_state.get("error") is not None:
                task_node["error"] = copy.deepcopy(attempt_state["error"])
        return snapshot

    def _persist_static_run(self, run_id:str):
        static_run = self.static_runs.get(run_id)
        if static_run is not None:
            self.static_run_store.save_run(self._static_run_snapshot(static_run))

    @staticmethod
    def _static_run_snapshot(static_run: StaticRun) -> Dict[str, Any]:
        snapshot = static_run.snapshot()
        dispatch = getattr(static_run, "_dispatch", None)
        if dispatch is not None:
            snapshot[RUN_WORKFLOW_DISPATCH_FIELD] = copy.deepcopy(dispatch)
        submission_digest = getattr(static_run, "_submission_digest", None)
        if submission_digest is not None:
            snapshot["submission_digest"] = submission_digest
        return snapshot

    def _record_static_event(
        self,
        run_id: str,
        event: Dict[str, Any],
        *,
        persist_run: bool = True,
    ):
        static_run = self.static_runs.get(run_id)
        if static_run is None:
            return event
        stored_event = static_run.append_event(event)
        if not self._scheduler_event_is_persisted(
            self.static_run_store,
            run_id,
            stored_event,
        ):
            self.static_run_store.append_event(run_id, stored_event)
        if persist_run:
            self._persist_static_run(run_id)
        return stored_event

    def _get_static_run_snapshot(self, run_id:str):
        if run_id in self.static_runs:
            return self._static_run_snapshot(self.static_runs[run_id])
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

    def _resource_observation_from_message(
        self,
        run_id: str,
        task_id: str,
        *,
        status: str,
        metrics: Dict[str, Any] | None = None,
        error: Dict[str, Any] | None = None,
        schedule_decision: Dict[str, Any] | None = None,
        node_id: str | None = None,
        attempt_data: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        attempt_data = attempt_data or {}
        observation_key = (
            run_id,
            task_id,
            attempt_data.get("attempt"),
            attempt_data.get("dispatch_id"),
            attempt_data.get("lease_id"),
            status,
        )
        observation_cache = getattr(self, "_task_resource_observations", None)
        if observation_cache is None:
            observation_cache = {}
            self._task_resource_observations = observation_cache
        if observation_key in observation_cache:
            return copy.deepcopy(observation_cache[observation_key])

        task = None
        if run_id in self.submit_workflows:
            task = self.submit_workflows[run_id].tasks.get(task_id)
        elif run_id in self.dynamic_runs:
            task = self.dynamic_runs[run_id].tasks.get(task_id)

        task_name = getattr(task, "task_name", None)
        model_anchor = getattr(task, "model_anchor", None)
        task_resources = copy.deepcopy(getattr(task, "resources", None) or {})
        requested_resources = (
            (schedule_decision or {}).get("requested_resources")
            or task_resources
        )
        selected_node = (schedule_decision or {}).get("selected_node") or {}
        if node_id and "node_id" not in selected_node:
            selected_node = {**selected_node, "node_id": node_id}

        observation = self.resource_history.record(
            run_id=run_id,
            task_id=task_id,
            task_name=task_name,
            status=status,
            requested_resources=requested_resources,
            model_anchor=model_anchor,
            metrics=metrics,
            error=error,
            selected_node=selected_node,
        )
        observation_cache[observation_key] = copy.deepcopy(observation)
        return observation

    def _duration_seconds_from_message(self, task: CodeTask | None, message_data: Dict[str, Any]) -> float | None:
        duration_ms = message_data.get("duration_ms")
        if duration_ms is not None:
            try:
                return max(0.0, float(duration_ms) / 1000.0)
            except (TypeError, ValueError):
                pass

        started_at = message_data.get("started_at") or getattr(task, "start_time", None)
        finished_at = message_data.get("finished_at") or getattr(task, "finish_time", None) or time.time()
        if started_at is None:
            return None
        try:
            return max(0.0, float(finished_at) - float(started_at))
        except (TypeError, ValueError):
            return None

    def _observe_task_runtime(self, task: CodeTask | None, message_data: Dict[str, Any], *, success: bool) -> None:
        if task is None:
            return
        duration_seconds = self._duration_seconds_from_message(task, message_data)
        if duration_seconds is None:
            return
        self._get_runtime_estimator().observe_task(
            task,
            duration_seconds,
            success=success,
        )

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
            snapshot = static_run_summary(self._static_run_snapshot(static_run))
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
                self._dynamic_run_snapshot(run_id)
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

    @staticmethod
    def _finish_continuation_identity(data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "attempt": data.get("attempt"),
            "dispatch_id": data.get("dispatch_id"),
            "lease_id": data.get("lease_id"),
        }

    @staticmethod
    def _finish_continuation_matches(
        continuation: Dict[str, Any],
        data: Dict[str, Any],
    ) -> bool:
        return continuation.get("attempt_identity") == (
            MaPath._finish_continuation_identity(data)
        )

    async def _resume_terminal_finish_continuation(
        self,
        message: Dict[str, Any],
    ) -> bool:
        data = message.get("data") or {}
        run_id = data.get("workflow_id")
        task_id = data.get("task_id")
        if not isinstance(run_id, str) or not isinstance(task_id, str):
            return False

        static_run = getattr(self, "static_runs", {}).get(run_id)
        if static_run is not None:
            continuation = getattr(
                static_run,
                "finish_continuations",
                {},
            ).get(task_id)
            if isinstance(continuation, dict) and self._finish_continuation_matches(
                continuation,
                data,
            ):
                await self._continue_static_finish(run_id, task_id)
                return True

        dynamic_run = getattr(self, "dynamic_runs", {}).get(run_id)
        if dynamic_run is not None:
            continuation = getattr(
                dynamic_run,
                "finish_continuations",
                {},
            ).get(task_id)
            if isinstance(continuation, dict) and self._finish_continuation_matches(
                continuation,
                data,
            ):
                await self._continue_dynamic_finish(run_id, task_id)
                return True
        return False

    async def _continue_dynamic_finish(self, run_id: str, task_id: str) -> None:
        dynamic_run = self.get_dynamic_run(run_id)
        continuation = dynamic_run.finish_continuations[task_id]
        if continuation.get("status") == "completed":
            self._persist_dynamic_run(run_id)
            return

        if continuation.get("finish_notification") != "sent":
            await self._notify_dynamic_scheduler_event(run_id)
            continuation["finish_notification"] = "sent"
            self._persist_dynamic_run(run_id)

        for ready_task_id in continuation.get("ready_task_ids", []):
            ready_state = continuation["ready_tasks"][ready_task_id]
            ready_task = dynamic_run.tasks[ready_task_id]
            if ready_state.get("event") is None:
                ready_state["event"] = copy.deepcopy(dynamic_run.append_event({
                    "type": "task_ready",
                    "data": {
                        "run_id": run_id,
                        "task_id": ready_task.task_id,
                        "task_name": ready_task.task_name,
                        "task_kind": ready_task.task_kind,
                    },
                }))
            if ready_state.get("event_status") != "sent":
                await self._emit_dynamic_event(
                    run_id,
                    copy.deepcopy(ready_state["event"]),
                )
                ready_state["event_status"] = "sent"
                self._persist_dynamic_run(run_id)
            if ready_state.get("dispatch_status") != "sent":
                self._submit_dynamic_task(ready_task)
                ready_state["dispatch_status"] = "sent"
                self._persist_dynamic_run(run_id)

        continuation["status"] = "completed"
        self._persist_dynamic_run(run_id)

    async def _continue_static_finish(self, run_id: str, task_id: str) -> None:
        static_run = self.static_runs[run_id]
        if self._dispatch_requires_cleanup(static_run):
            return
        workflow = self.submit_workflows[run_id]
        continuation = static_run.finish_continuations[task_id]
        if continuation.get("status") == "completed":
            self._persist_static_run(run_id)
            return

        finish_event = continuation.get("finish_event")
        if finish_event is None:
            identity = continuation["attempt_identity"]
            for candidate in reversed(static_run.event_log):
                data = candidate.get("data") or {}
                if (
                    candidate.get("type") == "finish_task"
                    and data.get("task_id") == task_id
                    and self._finish_continuation_identity(data) == identity
                ):
                    finish_event = copy.deepcopy(candidate)
                    continuation["finish_event"] = finish_event
                    break
        if finish_event is None:
            raise RuntimeError(
                f"Missing durable finish event for workflow {run_id}, task {task_id}"
            )

        if continuation.get("task_notification") != "sent":
            queue_for_run = self.async_que.get(run_id)
            if queue_for_run is not None:
                await queue_for_run.put(copy.deepcopy(finish_event))
            continuation["task_notification"] = "sent"
            self._persist_static_run(run_id)

        if not continuation.get("workflow_advanced"):
            file_manifest = continuation.get("file_manifest")
            if file_manifest:
                workflow.graph.nodes[task_id]["file_manifest"] = copy.deepcopy(
                    file_manifest
                )
            ready_tasks = workflow.finish_task(
                task_id=task_id,
                strategy=self.strategy,
            )
            ready_task_ids = [task.task_id for task in ready_tasks]
            continuation["ready_task_ids"] = ready_task_ids
            continuation["dispatch"] = {
                ready_task_id: "pending"
                for ready_task_id in ready_task_ids
            }
            continuation["workflow_advanced"] = True
            self._persist_static_run(run_id)

        for ready_task_id in continuation.get("ready_task_ids", []):
            if continuation["dispatch"].get(ready_task_id) == "sent":
                continue
            task = workflow.tasks[ready_task_id]
            static_run.mark_task_queued(ready_task_id)
            self._persist_static_run(run_id)
            file_context = workflow.graph.graph.get("file_context")
            data = self._task_run_payload(
                workflow,
                task,
                run_id,
                file_context,
            )
            data["priority"] = await self._get_task_priority(workflow, task)
            self._send_scheduler_message({"type": "run_task", "data": data})
            continuation["dispatch"][ready_task_id] = "sent"
            self._persist_static_run(run_id)

        if static_run.status == "succeeded":
            if continuation.get("run_metrics") != "sent":
                self.global_metrics.on_run_status_change(
                    run_id,
                    "running",
                    "succeeded",
                )
                continuation["run_metrics"] = "sent"
                self._persist_static_run(run_id)

            workflow_event = continuation.get("workflow_event")
            if workflow_event is None:
                workflow_event = static_run.append_event({
                    "type": "finish_workflow",
                    "data": {
                        "run_id": run_id,
                        "workflow_id": static_run.workflow_id,
                    },
                })
                continuation["workflow_event"] = copy.deepcopy(workflow_event)
            if not self._scheduler_event_is_persisted(
                self.static_run_store,
                run_id,
                workflow_event,
            ):
                self.static_run_store.append_event(run_id, workflow_event)
            self._persist_static_run(run_id)

            if continuation.get("workflow_notification") != "sent":
                queue_for_run = self.async_que.get(run_id)
                if queue_for_run is not None:
                    await queue_for_run.put(copy.deepcopy(workflow_event))
                continuation["workflow_notification"] = "sent"
                self._persist_static_run(run_id)
            if continuation.get("workflow_cleared") != "sent":
                self._send_scheduler_message({
                    "type": "clear_workflow",
                    "data": {"workflow_id": run_id},
                })
                continuation["workflow_cleared"] = "sent"
                self._persist_static_run(run_id)

        continuation["status"] = "completed"
        self._persist_static_run(run_id)

    async def _handle_dynamic_scheduler_event(
        self,
        message: Dict[str, Any],
        attempt_transaction: Dict[str, Any] | None = None,
    ):
        message_data = message["data"]
        run_id = message_data["workflow_id"]
        task_id = message_data["task_id"]
        message_data.setdefault("run_id", run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        message_type = message["type"]

        if dynamic_run.is_terminal():
            self._rollback_task_attempt_event_transaction(attempt_transaction)
            return

        if message_type == "start_task":
            dynamic_run.mark_started(task_id)
            self._persist_dynamic_scheduler_event(run_id, message)
            self._commit_task_attempt_event_transaction(attempt_transaction)
            await self._notify_dynamic_scheduler_event(run_id)
            return

        if message_type == "finish_task":
            file_manifest = self._publish_task_file_manifest(message_data)
            if file_manifest:
                message_data["file_manifest"] = file_manifest
            observation = self._resource_observation_from_message(
                run_id,
                task_id,
                status="succeeded",
                metrics=message_data.get("metrics") or {},
                schedule_decision=message_data.get("schedule_decision"),
                node_id=message_data.get("node_id"),
                attempt_data=message_data,
            )
            message_data["resource_observation"] = observation
            dynamic_run.set_task_file_manifest(task_id, file_manifest)
            ready_tasks = dynamic_run.mark_finished(
                task_id,
                message_data.get("fault_tolerance"),
            )
            dynamic_run.finish_continuations[task_id] = {
                "schema_version": 1,
                "attempt_identity": self._finish_continuation_identity(
                    message_data
                ),
                "status": "pending",
                "finish_notification": "pending",
                "ready_task_ids": [task.task_id for task in ready_tasks],
                "ready_tasks": {
                    task.task_id: {
                        "event": None,
                        "event_status": "pending",
                        "dispatch_status": "pending",
                    }
                    for task in ready_tasks
                },
            }
            self._persist_dynamic_scheduler_event(run_id, message)
            self._persist_task_file_manifest(message_data, file_manifest)
            self._commit_task_attempt_event_transaction(attempt_transaction)
            self._observe_task_runtime(
                dynamic_run.tasks.get(task_id),
                message_data,
                success=True,
            )
            await self._continue_dynamic_finish(run_id, task_id)
            return

        if message_type == "task_pending":
            await self._emit_dynamic_event(run_id, message)
            return

        if message_type == "task_retry":
            message_data.pop("file_manifest", None)
            dynamic_run.mark_retrying(
                task_id,
                message_data.get("error"),
                message_data.get("fault_tolerance"),
            )
            self._persist_dynamic_scheduler_event(run_id, message)
            self._commit_task_attempt_event_transaction(attempt_transaction)
            await self._notify_dynamic_scheduler_event(run_id)
            return

        if message_type == "task_exception":
            message_data.pop("file_manifest", None)
            error = message_data.get("error", message_data.get("result"))
            observation = self._resource_observation_from_message(
                run_id,
                task_id,
                status="failed",
                metrics=message_data.get("metrics") or {},
                error=error if isinstance(error, dict) else None,
                schedule_decision=message.get("data", {}).get("schedule_decision"),
                attempt_data=message_data,
            )
            message_data["resource_observation"] = observation
            if isinstance(error, dict):
                error = {**error, "resource_observation": observation}
                message_data["error"] = error
                message_data["result"] = error
            dynamic_run.mark_failed(
                task_id,
                error,
                message_data.get("fault_tolerance"),
            )
            self._persist_dynamic_scheduler_event(run_id, message)
            self._commit_task_attempt_event_transaction(attempt_transaction)
            await self._notify_dynamic_scheduler_event(run_id)
            self._stop_workflow_after_artifact_failure(message_data)
            return

        self._rollback_task_attempt_event_transaction(attempt_transaction)

    async def _handle_static_finish_scheduler_event(
        self,
        message: Dict[str, Any],
        attempt_transaction: Dict[str, Any] | None = None,
    ):
        message_data = message["data"]
        submit_id = message_data["workflow_id"]
        if submit_id not in self.async_que or submit_id not in self.submit_workflows:
            self._rollback_task_attempt_event_transaction(attempt_transaction)
            return

        static_run = self.static_runs.get(submit_id)
        if self._dispatch_requires_cleanup(static_run):
            self._rollback_task_attempt_event_transaction(attempt_transaction)
            return
        if static_run is not None and static_run.is_terminal():
            self._rollback_task_attempt_event_transaction(attempt_transaction)
            return

        task_id = message_data["task_id"]
        task_metrics = message_data.get("metrics") or {}
        file_manifest = self._publish_task_file_manifest(message_data)
        final_output_error = None
        if static_run is not None:
            final_output_error = static_run.mark_task_finished(
                task_id,
                result=message_data.get("result"),
                file_manifest=file_manifest,
                metrics=task_metrics,
                started_at=message_data.get("started_at"),
                finished_at=message_data.get("finished_at"),
                duration_ms=message_data.get("duration_ms"),
                node_id=message_data.get("node_id"),
                fault_tolerance=message_data.get("fault_tolerance"),
                attempt=message_data.get("attempt"),
                dispatch_id=message_data.get("dispatch_id"),
                lease_id=message_data.get("lease_id"),
            )

        success = final_output_error is None
        observation = self._resource_observation_from_message(
            submit_id,
            task_id,
            status="succeeded" if success else "failed",
            metrics=task_metrics,
            error=final_output_error,
            schedule_decision=message_data.get("schedule_decision"),
            node_id=message_data.get("node_id"),
            attempt_data=message_data,
        )
        task_metrics = {
            **task_metrics,
            "resource_observation": observation,
        }

        if success:
            message_data["metrics"] = task_metrics
            message_data["resource_observation"] = observation
            if file_manifest:
                message_data["file_manifest"] = file_manifest
            if static_run is not None:
                static_run.task_nodes[task_id]["metrics"] = copy.deepcopy(task_metrics)
        else:
            error = {
                **final_output_error,
                "resource_observation": observation,
            }
            attempt_state = self.task_attempts.get((submit_id, task_id))
            if attempt_state is not None:
                attempt_state["event_type"] = "task_exception"
                attempt_state["error"] = copy.deepcopy(error)
            message = {
                "type": "task_exception",
                "data": {
                    **message_data,
                    "error": error,
                    "result": error,
                    "metrics": task_metrics,
                    "resource_observation": observation,
                },
            }
            message["data"].pop("file_manifest", None)
            message_data = message["data"]
            file_manifest = None
            if static_run is not None:
                static_run.mark_task_failed(
                    task_id,
                    error,
                    fault_tolerance=message_data.get("fault_tolerance"),
                    attempt=message_data.get("attempt"),
                    dispatch_id=message_data.get("dispatch_id"),
                    lease_id=message_data.get("lease_id"),
                )
                static_run.task_nodes[task_id]["metrics"] = copy.deepcopy(task_metrics)

        if success and static_run is not None:
            static_run.finish_continuations[task_id] = {
                "schema_version": 1,
                "attempt_identity": self._finish_continuation_identity(
                    message_data
                ),
                "status": "pending",
                "task_result": copy.deepcopy(message_data["result"]),
                "file_manifest": copy.deepcopy(file_manifest),
                "finish_event": None,
                "task_notification": "pending",
                "workflow_advanced": False,
                "ready_task_ids": [],
                "dispatch": {},
                "run_metrics": "pending",
                "workflow_event": None,
                "workflow_notification": "pending",
                "workflow_cleared": "pending",
            }

        if static_run is not None:
            message = self._record_static_event(submit_id, message)
            if success:
                static_run.finish_continuations[task_id]["finish_event"] = (
                    copy.deepcopy(message)
                )
        if success:
            self._persist_task_file_manifest(message_data, file_manifest)
        self._commit_task_attempt_event_transaction(attempt_transaction)

        task = self.submit_workflows[submit_id].tasks.get(task_id)
        self._observe_task_runtime(task, message_data, success=success)
        self.global_metrics.on_task_finished(
            submit_id,
            task_id,
            "succeeded" if success else "failed",
            task_metrics if success else None,
        )
        if not success and static_run is not None:
            self.global_metrics.on_run_status_change(
                submit_id,
                "running",
                "failed",
            )

        que: Queue[Any] = self.async_que[submit_id]
        if not success:
            await que.put(message)
            self._send_scheduler_message({
                "type": "clear_workflow",
                "data": {"workflow_id": submit_id},
            })
            return

        if static_run is not None:
            await self._continue_static_finish(submit_id, task_id)
            return

        await que.put(message)
        if file_manifest:
            self.submit_workflows[submit_id].graph.nodes[task_id][
                "file_manifest"
            ] = file_manifest
        ready_tasks = self.submit_workflows[submit_id].finish_task(
            task_id=task_id,
            strategy=self.strategy,
        )
        for task in ready_tasks:
            data = self._task_run_payload(
                self.submit_workflows[submit_id],
                task,
                submit_id,
                self.submit_workflows[submit_id].graph.graph.get("file_context"),
            )
            data["priority"] = await self._get_task_priority(
                self.submit_workflows[submit_id],
                task,
            )
            self._send_scheduler_message({"type": "run_task", "data": data})
        
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

    async def set_cluster_node_disabled(self, node_id: str, disabled: bool, timeout: float = 5.0):
        self._require_scheduler_available()
        request_id = str(uuid.uuid4())
        response_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.cluster_control_requests[request_id] = response_queue

        message = {
            "type": "set_node_disabled",
            "data": {
                "request_id": request_id,
                "node_id": node_id,
                "disabled": bool(disabled),
            },
        }
        try:
            self._send_scheduler_message(message)
            result = await self._wait_for_scheduler_response(
                response_queue,
                timeout=timeout,
                operation="update cluster node state",
            )
            if not result.get("ok", False):
                raise RuntimeError(result.get("error") or "cluster control failed")
            return result
        finally:
            self.cluster_control_requests.pop(request_id, None)
    
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

    def init(
        self,
        ray_head_port,
        strategy=None,
        node_scheduling_policy=None,
        scheduling_algorithm=None,
    ):
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
        algorithm_candidate = scheduling_algorithm if scheduling_algorithm is not None else strategy
        if (
            node_scheduling_policy is None
            and str(algorithm_candidate or "").strip().lower() in NODE_SCHEDULING_POLICIES
            and str(algorithm_candidate or "").strip().upper() not in {"FCFS", "HACS"}
        ):
            node_scheduling_policy = algorithm_candidate
            algorithm_candidate = None

        self.strategy = normalize_scheduling_algorithm(algorithm_candidate).value
        self.node_scheduling_policy = node_scheduling_policy
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
                self.node_scheduling_policy,
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
            self._recover_incomplete_workflow_dispatches()
        except Exception:
            self._abort_scheduler_start()
            raise
 
    async def monitor_coroutine(self):
        '''
        Monitor the task from the scheduler process.
        '''
        retry_message = None
        retry_delay = 0.05
        while True:
            attempt_transaction = None
            message = None
            try:
                if retry_message is None:
                    frames = await self.socket_from_scheduler.recv_multipart()
                    assert(len(frames)==2)
                    _, data = frames
                    message = json.loads(data.decode('utf-8'))
                    retry_delay = 0.05
                else:
                    message = retry_message
                    retry_message = None
 
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

                    if message_type == "cluster_control":
                        request_id = message_data.get("request_id")
                        response_queue = self.cluster_control_requests.get(request_id)
                        if response_queue is not None:
                            await response_queue.put(message_data)
                        continue

                    if message_type == "workflow_stopped":
                        self._handle_workflow_cleanup_response(message_data)
                        continue

                    attempt_transaction = None
                    if message_type in {
                        "start_task",
                        "finish_task",
                        "task_pending",
                        "task_retry",
                        "task_exception",
                    }:
                        static_run = self.static_runs.get(
                            message_data.get("workflow_id")
                        )
                        if self._dispatch_requires_cleanup(static_run):
                            continue

                    if message_type in {
                        "start_task",
                        "finish_task",
                        "task_retry",
                        "task_exception",
                    }:
                        attempt_transaction = self._begin_task_attempt_event_transaction(
                            message_type,
                            message_data,
                        )
                        try:
                            accepted = self._accept_task_attempt_event(
                                message_type,
                                message_data,
                            )
                        except ArtifactError as exc:
                            self._rollback_task_attempt_event_transaction(
                                attempt_transaction
                            )
                            failed_message = copy.deepcopy(message)
                            failed_message["type"] = "task_exception"
                            failed_data = failed_message.setdefault("data", {})
                            failed_data.pop("file_manifest", None)
                            error = exception_to_error_envelope(
                                "artifact_error",
                                exc,
                                retryable=False,
                                origin="core",
                                node_id=failed_data.get("node_id"),
                                node_ip=failed_data.get("node_ip"),
                                attempt=failed_data.get("attempt"),
                            )
                            failed_data["error"] = error
                            failed_data["result"] = error
                            retry_message = failed_message
                            logger.exception(
                                "Task artifact validation failed; recording a terminal task failure"
                            )
                            continue
                        if not accepted:
                            attempt_transaction = None
                            if (
                                message_type == "finish_task"
                                and await self._resume_terminal_finish_continuation(
                                    message
                                )
                            ):
                                continue
                            continue
                        if message_type in {"task_retry", "task_exception"}:
                            message_data.pop("file_manifest", None)

                    if(message_type=="finish_task"):
                        submit_id = message_data['workflow_id']
                        if submit_id in self.dynamic_runs:
                            await self._handle_dynamic_scheduler_event(
                                message,
                                attempt_transaction,
                            )
                            continue
                        await self._handle_static_finish_scheduler_event(
                            message,
                            attempt_transaction,
                        )

                    elif(message_type=="start_task" or message_type=="task_pending" or message_type=="task_retry" or message_type=="task_exception"):
                        submit_id = message_data['workflow_id']
                        if submit_id in self.dynamic_runs:
                            await self._handle_dynamic_scheduler_event(
                                message,
                                attempt_transaction,
                            )
                            continue
                        if submit_id not in self.async_que or submit_id not in self.submit_workflows:
                            self._rollback_task_attempt_event_transaction(
                                attempt_transaction
                            )
                            continue

                        static_run = self.static_runs.get(submit_id)
                        if static_run is not None and static_run.is_terminal():
                            self._rollback_task_attempt_event_transaction(
                                attempt_transaction
                            )
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
                                    message_data.get("fault_tolerance"),
                                    node_info=message_data,
                                )
                                message = self._record_static_event(submit_id, message)
                        elif message_type == "task_exception":
                            if static_run is not None:
                                error = message_data.get("error", message_data.get("result"))
                                observation = self._resource_observation_from_message(
                                    submit_id,
                                    message_data["task_id"],
                                    status="failed",
                                    metrics=message_data.get("metrics") or {},
                                    error=error if isinstance(error, dict) else None,
                                    schedule_decision=message_data.get("schedule_decision"),
                                    attempt_data=message_data,
                                )
                                if isinstance(error, dict):
                                    error = {**error, "resource_observation": observation}
                                    message_data["error"] = error
                                    message_data["result"] = error
                                message_data["resource_observation"] = observation
                                static_run.mark_task_failed(
                                    message_data["task_id"],
                                    error,
                                    None,
                                    message_data.get("fault_tolerance"),
                                    attempt=message_data.get("attempt"),
                                    dispatch_id=message_data.get("dispatch_id"),
                                    lease_id=message_data.get("lease_id"),
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

                        self._commit_task_attempt_event_transaction(
                            attempt_transaction
                        )

                        que: Queue[Any] = self.async_que[submit_id]
                        await que.put(message)
                        self._stop_workflow_after_artifact_failure(message_data)
                    
                    elif message_type in {
                        "finish_llm_instance_launch",
                        "fail_llm_instance_launch",
                        "finish_llm_instance_stop",
                        "fail_llm_instance_stop",
                    }:
                        queue_id = message_data.get("request_id") or message_data.get("instance_id")
                        que = self.llm_instance_async_que.get(queue_id)
                        if que is not None:
                            await que.put(message)

            except OSError:
                self._rollback_task_attempt_event_transaction(attempt_transaction)
                if message is None:
                    logger.exception("Error receiving a Scheduler message")
                    continue
                retry_message = message
                logger.exception(
                    "Scheduler event persistence failed; retrying the same message"
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(1.0, retry_delay * 2)
            except Exception:
                self._rollback_task_attempt_event_transaction(attempt_transaction)
                logger.exception("Error in scheduler monitor")
      
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
            static_run = self.static_runs.get(submit_id)
            if self._dispatch_requires_cleanup(static_run):
                dispatch = static_run._dispatch
                self._request_workflow_cleanup(
                    submit_id,
                    static_run.workflow_id,
                    dispatch,
                    dispatch.get("error") or self._dispatch_error(
                        dispatch,
                        RuntimeError(
                            "Workflow was stopped before submission completed"
                        ),
                    ),
                    lambda: self._persist_static_run(submit_id),
                )
                return

            self.async_que.pop(submit_id, None)
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

        message = {"type":"stop_workflow","data":{"workflow_id":submit_id}}
        self._send_scheduler_message(message)
    
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
        request_id = str(uuid.uuid4())
        response_queue = asyncio.Queue(maxsize=1)
        self.llm_instance_async_que[request_id] = response_queue
        message = {
            "type": "start_llm_instance",
            "data": {
                "request_id": request_id,
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
        terminal_received = False
        try:
            self._send_scheduler_message(message)
            response = await self._wait_for_scheduler_response(
                response_queue,
                timeout=timeout,
                operation="start the model instance",
            )
            terminal_received = True
        finally:
            self.llm_instance_async_que.pop(request_id, None)
            if not terminal_received:
                try:
                    self._send_scheduler_message({
                        "type": "stop_llm_instance",
                        "data": {
                            "instance_id": instance_id,
                            "start_request_id": request_id,
                        },
                    })
                except Exception:
                    logger.warning(
                        "Failed to cancel abandoned LLM start request %s",
                        request_id,
                        exc_info=True,
                    )
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
        response_queue = asyncio.Queue(maxsize=1)
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
