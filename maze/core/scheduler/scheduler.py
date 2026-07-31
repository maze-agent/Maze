from logging import Logger
import heapq


import logging
import resource

from traitlets import Instance
from zmq.backend import select
import ray
import time
import zmq
import threading
import queue
import json
import os
import base64
import cloudpickle
import binascii
import subprocess
import multiprocessing as mp
import sys
import uuid
from queue import Queue
from maze.core.scheduler.resource import SelectedNode
from typing import Any,List,Dict
from maze.core.scheduler.resource import ResourceManager
from maze.core.scheduler.llm_instance import (
    LlmInstanceManager,
    LlmInstanceMessage,
    validate_model_backend,
    validate_transformers_model,
)
from maze.core.scheduler.runtime import WorkflowRuntimeManager,TaskRuntime,LanggraphTaskRuntime
from maze.core.scheduler.result_summary import summarize_task_result
from maze.core.scheduler.error import (
    enrich_error_for_task,
    exception_to_error_envelope,
    is_task_error_result,
    make_error_envelope,
)
from maze.core.workflow.task import TaskType
from maze.core.files.lineage import TASK_RESULT_ENVELOPE

logger = logging.getLogger(__name__)

_RAY_NODE_LOSS_EXCEPTIONS = tuple(
    error_type
    for error_type in (
        getattr(ray.exceptions, "NodeDiedError", None),
        getattr(ray.exceptions, "ObjectLostError", None),
        getattr(ray.exceptions, "WorkerCrashedError", None),
    )
    if isinstance(error_type, type)
)
_RAY_RETRYABLE_EXECUTION_EXCEPTIONS = (
    *_RAY_NODE_LOSS_EXCEPTIONS,
    ray.exceptions.TaskUnschedulableError,
)


def _ray_execution_error_type(exc: BaseException) -> str:
    if isinstance(exc, ray.exceptions.TaskUnschedulableError):
        return "resource_unavailable"
    if isinstance(exc, _RAY_NODE_LOSS_EXCEPTIONS):
        return "node_lost"
    return "unknown"


def build_ray_command(*args: str):
    executable_name = "ray.exe" if os.name == "nt" else "ray"
    return [os.path.join(os.path.dirname(sys.executable), executable_name), *args]


def stop_ray_runtime(*, force: bool = True, timeout: float = 15.0):
    command = build_ray_command("stop")
    if force:
        command.append("--force")
    return subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


class PriorityQueue:
    def __init__(self):
        self._queue = []
        self._index = 0
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)

    def put(self, item, priority):
        with self._not_empty:
            heapq.heappush(self._queue, (priority, self._index, item))
            self._index += 1
            self._not_empty.notify()

    def get(self, block=True, timeout=None):
        with self._not_empty:
            if not block:
                if self.is_empty():
                    raise IndexError("get from empty priority queue")
            else:
                success = self._not_empty.wait_for(
                    lambda: not self.is_empty(),
                    timeout=timeout
                )
                if not success:
                    raise TimeoutError("get timeout")

            _, _, item = heapq.heappop(self._queue)
            return item

    def is_empty(self):
        return len(self._queue) == 0

    def size(self):
        with self._lock:
            return len(self._queue)

    def snapshot(self):
        with self._lock:
            return [item for _, _, item in sorted(self._queue)]

    def discard_workflow(self, workflow_id: str) -> int:
        with self._not_empty:
            original_size = len(self._queue)
            self._queue = [
                entry
                for entry in self._queue
                if getattr(entry[2], "workflow_id", None) != workflow_id
            ]
            heapq.heapify(self._queue)
            return original_size - len(self._queue)


def scheduler_process(
    port1: int,
    port2: int,
    strategy: str,
    ray_head_port: int,
    ready_queue: mp.Queue,
    fatal_event=None,
    owner_id: str | None = None,
    owner_cleanup_complete_event=None,
    ray_cleanup_complete_event=None,
    owner_node_sender=None,
):
    try:
        scheduler = Scheduler(
            port1,
            port2,
            ray_head_port,
            ready_queue,
            strategy,
            fatal_event=fatal_event,
            owner_id=owner_id,
            owner_cleanup_complete_event=owner_cleanup_complete_event,
            ray_cleanup_complete_event=ray_cleanup_complete_event,
            owner_node_sender=owner_node_sender,
        )
        scheduler.start()
    except Exception as exc:
        if fatal_event is not None:
            fatal_event.set()
        ready_queue.put({"status": "error", "error": str(exc)})
        raise

class Scheduler():
    def __init__(
        self,
        port1: int,
        port2: int,
        ray_head_port: int,
        ready_queue: mp.Queue,
        strategy: str = "Default",
        fatal_event=None,
        owner_id: str | None = None,
        owner_cleanup_complete_event=None,
        ray_cleanup_complete_event=None,
        owner_node_sender=None,
    ):
        self.lock = threading.Lock()
        self._process_exit_lock = threading.Lock()
        self.port1 = port1
        self.port2 = port2
        self.ray_head_port = ray_head_port
        self.ready_queue = ready_queue
        self.strategy = strategy
        self.fatal_event = fatal_event
        self.owner_id = owner_id or uuid.uuid4().hex
        self.owner_cleanup_complete_event = owner_cleanup_complete_event
        self.ray_cleanup_complete_event = ray_cleanup_complete_event
        self.owner_node_sender = owner_node_sender

        self.workflow_manager = WorkflowRuntimeManager()
        self.resource_manager = ResourceManager()
        self.resource_manager.set_scheduling_policy(self._node_scheduling_policy(strategy))
        self.llm_instance_manager = LlmInstanceManager(owner_id=self.owner_id)

        self.task_queue: PriorityQueue[TaskRuntime|LanggraphTaskRuntime] = PriorityQueue()
        self.llm_instance_queue: Queue = queue.Queue()
        self.stopped_workflow_ids: set[str] = set()

    def _stopped_workflows(self) -> set[str]:
        if not hasattr(self, "stopped_workflow_ids"):
            self.stopped_workflow_ids = set()
        return self.stopped_workflow_ids

    def _node_scheduling_policy(self, strategy: str | None) -> str:
        normalized = (strategy or "default").strip().lower()
        if normalized in {"least-loaded", "prefer-gpu-free", "spread"}:
            return normalized
        return "default"

    def _task_queue_snapshot_item(self, task: TaskRuntime|LanggraphTaskRuntime, now: float):
        retry_wait_seconds = max(0.0, getattr(task, "next_eligible_time", 0.0) - now)
        if retry_wait_seconds > 0:
            queue_status = "retrying"
        elif getattr(task, "pending_reason", None):
            queue_status = "pending"
        else:
            queue_status = "ready"

        return {
            "workflow_id": task.workflow_id,
            "task_id": task.task_id,
            "task_type": "langgraph" if isinstance(task, LanggraphTaskRuntime) else "code",
            "status": queue_status,
            "runtime_status": task.status,
            "priority": task.priority,
            "attempt": task.attempt,
            "dispatch_id": task.dispatch_id,
            "lease_id": task.lease_id,
            "max_retries": task.max_retries,
            "retry_backoff_seconds": task.retry_backoff_seconds,
            "retry_wait_seconds": round(retry_wait_seconds, 6),
            "next_eligible_time": task.next_eligible_time or None,
            "pending_reason": getattr(task, "pending_reason", None),
            "last_error": getattr(task, "last_error", None),
            "resources": task.resources,
            "schedule_decision": getattr(task, "last_schedule_decision", None),
        }

    def _running_task_snapshot_item(self, task: TaskRuntime|LanggraphTaskRuntime, now: float):
        selected_node = getattr(task, "selected_node", None)
        started_time = getattr(task, "started_time", None)
        elapsed = None if started_time is None else round(max(0.0, now - started_time), 6)
        return {
            "workflow_id": task.workflow_id,
            "task_id": task.task_id,
            "task_type": "langgraph" if isinstance(task, LanggraphTaskRuntime) else "code",
            "status": task.status,
            "attempt": task.attempt,
            "dispatch_id": task.dispatch_id,
            "lease_id": task.lease_id,
            "resources": task.resources,
            "selected_node": {
                "node_id": getattr(selected_node, "node_id", None),
                "node_ip": getattr(selected_node, "node_ip", None),
                "gpu_id": getattr(selected_node, "gpu_id", None),
            } if selected_node is not None else None,
            "started_time": started_time,
            "elapsed_seconds": elapsed,
            "timeout_seconds": task.timeout_seconds,
        }

    def get_queue_snapshot(self):
        now = time.time()
        active_lease_counts_by_kind: Dict[str, int] = {}
        for lease in self.resource_manager.active_leases.values():
            kind = str(lease.get("reservation_kind") or "unknown")
            active_lease_counts_by_kind[kind] = active_lease_counts_by_kind.get(kind, 0) + 1

        queued_items = [
            self._task_queue_snapshot_item(task, now)
            for task in self.task_queue.snapshot()
            if task.workflow_id not in self._stopped_workflows()
        ]
        running_items = [
            self._running_task_snapshot_item(task, now)
            for task in self.workflow_manager.get_running_tasks()
        ]

        ready_items = [item for item in queued_items if item["status"] == "ready"]
        pending_items = [item for item in queued_items if item["status"] == "pending"]
        retrying_items = [item for item in queued_items if item["status"] == "retrying"]

        return {
            "snapshot_time": now,
            "scheduling_policy": self.resource_manager.scheduling_policy,
            "stopped_workflow_ids": sorted(self._stopped_workflows()),
            "active_lease_count": len(self.resource_manager.active_leases),
            "active_lease_counts_by_kind": dict(sorted(active_lease_counts_by_kind.items())),
            "counts": {
                "ready": len(ready_items),
                "pending": len(pending_items),
                "retrying": len(retrying_items),
                "running": len(running_items),
                "total_queued": len(queued_items),
            },
            "ready_tasks": ready_items,
            "pending_tasks": pending_items,
            "retrying_tasks": retrying_items,
            "running_tasks": running_items,
        }

    def _send_task_exception(self, socket_to_main, task: TaskRuntime|LanggraphTaskRuntime, error: Dict[str, Any]):
        message = {
            "type": "task_exception",
            "data": {
                "workflow_id": task.workflow_id,
                "task_id": task.task_id,
                "result": error,
                "error": error,
                "attempt": task.attempt,
                "dispatch_id": task.dispatch_id,
                "lease_id": task.lease_id,
            },
        }
        socket_to_main.send(json.dumps(message).encode("utf-8"))

    def _send_task_retry(self, socket_to_main, task: TaskRuntime|LanggraphTaskRuntime, error: Dict[str, Any]):
        message = {
            "type": "task_retry",
            "data": {
                "workflow_id": task.workflow_id,
                "task_id": task.task_id,
                "error": error,
                "attempt": task.attempt,
                "dispatch_id": task.dispatch_id,
                "lease_id": task.lease_id,
                "next_attempt": task.attempt + 1,
                "retry_backoff_seconds": task.retry_backoff_seconds,
            },
        }
        socket_to_main.send(json.dumps(message).encode("utf-8"))

    def _send_task_pending(self, socket_to_main, task: TaskRuntime|LanggraphTaskRuntime):
        message = {
            "type": "task_pending",
            "data": {
                "workflow_id": task.workflow_id,
                "task_id": task.task_id,
                "pending_reason": task.pending_reason,
                "schedule_decision": task.last_schedule_decision,
                "attempt": task.attempt,
            },
        }
        socket_to_main.send(json.dumps(message).encode("utf-8"))

    def _retry_or_fail_task(
        self,
        socket_to_main,
        task: TaskRuntime|LanggraphTaskRuntime,
        error: Dict[str, Any],
        file_manifest: Dict[str, Any] | None = None,
    ):
        error = enrich_error_for_task(error, task)
        task.last_error = error
        if file_manifest:
            task.file_manifest = file_manifest
        self.workflow_manager.clear_task_ref(task)

        if task.should_retry(error):
            self.resource_manager.release_task_resource(tasks=[task])
            task.schedule_retry(error)
            failed_node_id = error.get("node_id")
            target_node_id = task.resources.get("target_node_id") or task.resources.get("node_id")
            if error.get("error_type") == "node_lost" and failed_node_id and not target_node_id:
                avoid_node_ids = list(task.resources.get("avoid_node_ids") or [])
                if failed_node_id not in avoid_node_ids:
                    avoid_node_ids.append(failed_node_id)
                task.resources["avoid_node_ids"] = avoid_node_ids
            self._send_task_retry(socket_to_main, task, error)
            task.clear_attempt_identity()
            self.task_queue.put(task, task.priority)
            return

        self._stopped_workflows().add(task.workflow_id)
        self.task_queue.discard_workflow(task.workflow_id)
        canceld_tasks = self.workflow_manager.cancel_workflow(task.workflow_id)
        if len(canceld_tasks) > 0:
            self.resource_manager.release_task_resource(tasks=canceld_tasks)
            self.workflow_manager.clear_workflow(task.workflow_id)
        self._send_task_exception(socket_to_main, task, error)

    def _fail_timed_out_tasks(self, socket_to_main):
        for task in list(self.workflow_manager.get_running_tasks()):
            if not task.has_timed_out():
                continue
            try:
                ray.cancel(task.object_ref, force=True)
            except Exception:
                pass
            error = make_error_envelope(
                "timeout",
                f"Task timed out after {task.timeout_seconds} seconds",
                origin="scheduler",
                attempt=task.attempt,
            )
            self._retry_or_fail_task(socket_to_main, task, error)

    def _run_task_with_lease(
        self,
        task: TaskRuntime|LanggraphTaskRuntime,
        selection,
        dispatch_id: str,
    ):
        try:
            self.workflow_manager.run_task(
                task=task,
                node=selection.selected_node,
                dispatch_id=dispatch_id,
                lease_id=selection.lease_id,
            )
        except Exception:
            self.resource_manager.release_lease(selection.lease_id)
            raise

    def _finalize_stopped_llm_instance(self, instance_id: str, resource_detail: dict):
        with self.lock:
            self.resource_manager.release_instance_resource(resource_detail)
        self.llm_instance_manager.finalize_stopped_instance(instance_id)

    def _rollback_failed_llm_launch(self, instance_id: str, lease_id: str) -> bool:
        instance_state = self.llm_instance_manager.get_instance_state(instance_id)
        if instance_state == "stopped":
            try:
                resource_detail = self.llm_instance_manager.get_instance_resource_detail(
                    instance_id
                )
                self._finalize_stopped_llm_instance(instance_id, resource_detail)
                return True
            except KeyError:
                instance_state = None
        if instance_state is None:
            with self.lock:
                self.resource_manager.release_lease(lease_id)
            return True
        logger.error(
            "Retaining Lease %s for LLM instance %s in state %s",
            lease_id,
            instance_id,
            instance_state,
        )
        return False

    def _handle_llm_instance_stop(self, socket_to_main, message_data: dict):
        instance_id = message_data["instance_id"]
        try:
            resource_detail = self.llm_instance_manager.stop_llm_instance(
                instance_id=instance_id
            )
            self._finalize_stopped_llm_instance(instance_id, resource_detail)
            message = {
                "type": "finish_llm_instance_stop",
                "data": {
                    "instance_id": instance_id,
                    "backend": resource_detail["backend"],
                    "request_id": message_data.get("request_id"),
                },
            }
        except Exception as exc:
            message = {
                "type": "fail_llm_instance_stop",
                "data": {
                    "instance_id": instance_id,
                    "request_id": message_data.get("request_id"),
                    "error": str(exc),
                },
            }
            logger.exception("Failed to stop LLM instance %s", instance_id)
        socket_to_main.send(json.dumps(message).encode("utf-8"))

    def _record_llm_owner_node(self, node_id: str, node_ip: str) -> None:
        is_new_placement = self.llm_instance_manager.record_owner_node(node_id, node_ip)
        if not is_new_placement:
            return
        owner_node_sender = getattr(self, "owner_node_sender", None)
        if owner_node_sender is not None:
            owner_node_sender.send({
                "node_id": str(node_id),
                "node_ip": str(node_ip),
            })

    def _cleanup(self, exit_code: int = 0):
        if exit_code:
            self._signal_fatal()
        exit_lock = getattr(self, "_process_exit_lock", None)
        if exit_lock is None:
            exit_lock = threading.Lock()
            self._process_exit_lock = exit_lock

        with exit_lock:
            instance_cleanup_complete = False
            try:
                begin_shutdown = getattr(
                    self.llm_instance_manager,
                    "begin_shutdown",
                    None,
                )
                if begin_shutdown is not None:
                    begin_shutdown()
                stopped, cleanup_errors = self.llm_instance_manager.stop_all_llm_instances()
                for instance_id, resource_detail in stopped.items():
                    try:
                        self._finalize_stopped_llm_instance(instance_id, resource_detail)
                    except Exception as exc:
                        cleanup_errors[instance_id] = str(exc)
                        logger.exception(
                            "Failed to finalize LLM instance %s during shutdown",
                            instance_id,
                        )
                if cleanup_errors:
                    logger.critical(
                        "LLM cleanup remained incomplete during shutdown: %s",
                        cleanup_errors,
                    )
                else:
                    instance_cleanup_complete = True
            except BaseException:
                logger.exception("Unexpected failure while cleaning up LLM instances")

            owner_sweep_complete = False
            try:
                self.llm_instance_manager.stop_owned_llm_processes()
            except BaseException:
                logger.exception("Failed to clean Scheduler-owned model processes")
            else:
                owner_sweep_complete = True

            owner_cleanup_complete = (
                instance_cleanup_complete and owner_sweep_complete
            )
            if owner_cleanup_complete:
                owner_cleanup_event = getattr(
                    self,
                    "owner_cleanup_complete_event",
                    None,
                )
                if owner_cleanup_event is not None:
                    owner_cleanup_event.set()
            elif owner_sweep_complete:
                logger.critical(
                    "Owner sweep succeeded but registered model cleanup remained incomplete"
                )

            if owner_cleanup_complete:
                try:
                    result = stop_ray_runtime(force=True)
                    if result.returncode != 0:
                        logger.error(
                            "Ray cleanup exited with status %s: %s",
                            result.returncode,
                            (result.stderr or result.stdout or "unknown error").strip(),
                        )
                    else:
                        ray_cleanup_event = getattr(
                            self,
                            "ray_cleanup_complete_event",
                            None,
                        )
                        if ray_cleanup_event is not None:
                            ray_cleanup_event.set()
                except BaseException:
                    logger.exception("Failed to stop Ray during scheduler shutdown")
            else:
                logger.critical(
                    "Preserving Ray after incomplete Scheduler-owned model cleanup"
                )

            os._exit(exit_code)

    def _signal_fatal(self):
        fatal_event = getattr(self, "fatal_event", None)
        if fatal_event is not None:
            fatal_event.set()

    def _enqueue_task_message(self, message_data: Dict[str, Any]) -> bool:
        workflow_id = message_data["workflow_id"]
        with self.lock:
            if workflow_id in self._stopped_workflows():
                logger.debug(
                    "Ignoring late task dispatch for stopped workflow %s, task %s",
                    workflow_id,
                    message_data["task_id"],
                )
                return False
            if message_data["task_type"] == TaskType.CODE.value:
                task_runtime = TaskRuntime(
                    workflow_id=workflow_id,
                    task_id=message_data["task_id"],
                    task_input=message_data["task_input"],
                    task_output=message_data["task_output"],
                    resources=message_data["resources"],
                    code_str=message_data.get("code_str"),
                    code_ser=message_data.get("code_ser"),
                    file_context=message_data.get("file_context"),
                    max_retries=message_data.get("max_retries"),
                    retry_backoff_seconds=message_data.get("retry_backoff_seconds", 0),
                    retry_on=message_data.get("retry_on"),
                    timeout_seconds=message_data.get("timeout_seconds"),
                )
                priority = message_data.get("priority", 0)
                task_runtime.set_priority(priority)
                self.task_queue.put(task_runtime, priority)
                return True
            if message_data["task_type"] == TaskType.LANGGRAPH.value:
                task_runtime = LanggraphTaskRuntime(
                    workflow_id=workflow_id,
                    task_id=message_data["task_id"],
                    code_ser=message_data["code_ser"],
                    args=message_data["args"],
                    kwargs=message_data["kwargs"],
                    resources=message_data["resources"],
                    max_retries=message_data.get("max_retries"),
                    retry_backoff_seconds=message_data.get("retry_backoff_seconds", 0),
                    retry_on=message_data.get("retry_on"),
                    timeout_seconds=message_data.get("timeout_seconds"),
                )
                priority = message_data.get("priority", 0)
                task_runtime.set_priority(priority)
                self.task_queue.put(task_runtime, 0)
                return True
        return False

    def _receive_thread(self,port1:int):
        logger.info(f"Receive start")
        assert(self.context is not None)
        socket_from_main = self.context.socket(zmq.ROUTER)
        socket_from_main.bind(f"tcp://127.0.0.1:{port1}")
        socket_to_main = self.context.socket(zmq.DEALER)
        socket_to_main.connect(f"tcp://127.0.0.1:{self.port2}")

        try:
            while True:
                frames = socket_from_main.recv_multipart()
                assert(len(frames)==2)
                _, data = frames
                message = json.loads(data.decode('utf-8'))

                message_type = message["type"]
                message_data = message.get("data", {})
                if(message_type =="run_task"):
                    self._enqueue_task_message(message_data)
                elif(message_type =="clear_workflow" ):
                    with self.lock:
                        self._stopped_workflows().add(message_data["workflow_id"])
                        self.task_queue.discard_workflow(message_data["workflow_id"])
                        self.workflow_manager.clear_workflow(workflow_id=message_data["workflow_id"])
                elif(message_type =="stop_workflow" ):
                    with self.lock:
                        self._stopped_workflows().add(message_data["workflow_id"])
                        self.task_queue.discard_workflow(message_data["workflow_id"])
                        canceld_tasks = self.workflow_manager.cancel_workflow(workflow_id=message_data["workflow_id"])
                        if len(canceld_tasks) > 0:
                            self.resource_manager.release_task_resource(tasks=canceld_tasks)
                            self.workflow_manager.clear_workflow(workflow_id=message_data["workflow_id"])
                elif(message_type=="start_worker"):
                    with self.lock:
                        worker = self.resource_manager.start_worker(
                            node_id=message_data["node_id"],
                            resources=message_data["resources"],
                            node_ip=message_data["node_ip"],
                            capabilities=message_data.get("capabilities"),
                        )
                    request_id = message_data.get("request_id")
                    if request_id:
                        response = {
                            "type": "worker_started",
                            "data": {
                                "request_id": request_id,
                                "worker": worker,
                            },
                        }
                        socket_to_main.send(json.dumps(response).encode("utf-8"))
                elif(message_type=="get_cluster_resources"):
                    request_id = message_data.get("request_id")
                    with self.lock:
                        resources = self.resource_manager.get_cluster_resources()
                    response = {
                        "type": "cluster_resources",
                        "data": {
                            "request_id": request_id,
                            "resources": resources,
                        },
                    }
                    socket_to_main.send(json.dumps(response).encode("utf-8"))
                elif(message_type=="get_cluster_queues"):
                    request_id = message_data.get("request_id")
                    with self.lock:
                        queues = self.get_queue_snapshot()
                    response = {
                        "type": "cluster_queues",
                        "data": {
                            "request_id": request_id,
                            "queues": queues,
                        },
                    }
                    socket_to_main.send(json.dumps(response).encode("utf-8"))
                elif(message_type=="stop_worker"):
                    with self.lock:
                        self.resource_manager.stop_worker(node_id=message_data["node_id"])
                elif(message_type=="start_llm_instance" or message_type=="stop_llm_instance"):
                    self.llm_instance_queue.put(LlmInstanceMessage(message_type, message_data))
                elif(message_type=="shutdown"):
                    self._cleanup()

        except Exception:
            logger.exception("Scheduler receive thread failed")
            raise

    def _llm_instance_thread(self,port2:int):
        logger.info(f"Llm instance start")
        socket_to_main = self.context.socket(zmq.DEALER)
        socket_to_main.connect(f"tcp://127.0.0.1:{port2}")

        while True:
            llm_instance_message = self.llm_instance_queue.get()
            message_data = llm_instance_message.message_data

            if(llm_instance_message.message_type=="start_llm_instance"):
                try:
                    backend, backend_args = validate_model_backend(
                        message_data.get("backend"),
                        message_data.get("backend_args"),
                    )
                    if backend == "transformers":
                        validate_transformers_model(message_data["model"])
                except ValueError as exc:
                    message = {
                        "type": "fail_llm_instance_launch",
                        "data": {
                            "instance_id": message_data["instance_id"],
                            "backend": message_data.get("backend"),
                            "error": str(exc),
                        },
                    }
                    socket_to_main.send(json.dumps(message).encode("utf-8"))
                    continue

                need_resources = {
                    'cpu':message_data.get('cpu_nums', 0),
                    'cpu_mem':message_data.get('memory', 0),
                    'gpu':message_data.get('gpu_nums', 0),
                    'gpu_mem':message_data.get('gpu_mem', 0)
                }
                with self.lock:
                    selection = self.resource_manager.select_node(
                        task_need_resources=need_resources,
                        reservation_kind="instance",
                        run_id=message_data["instance_id"],
                    )
                if selection:
                    selected_node = selection.selected_node
                    try:
                        self._record_llm_owner_node(
                            selected_node.node_id,
                            selected_node.node_ip,
                        )
                        instance_info = self.llm_instance_manager.start_llm_instance(
                            instance_id=message_data["instance_id"],
                            model=message_data["model"],
                            backend=backend,
                            node_ip=selected_node.node_ip,
                            node_id=selected_node.node_id,
                            gpu_id=selected_node.gpu_id,
                            resources=need_resources,
                            lease_id=selection.lease_id,
                            backend_args=backend_args,
                        )
                    except Exception as exc:
                        instance_id = message_data["instance_id"]
                        self._rollback_failed_llm_launch(
                            instance_id,
                            selection.lease_id,
                        )
                        message = {
                            "type": "fail_llm_instance_launch",
                            "data": {
                                "instance_id": message_data["instance_id"],
                                "backend": backend,
                                "error": str(exc),
                            },
                        }
                        socket_to_main.send(json.dumps(message).encode("utf-8"))
                        logger.exception("Failed to launch LLM instance %s", message_data["instance_id"])
                        continue

                    #Send message to main
                    message = {
                        "type":"finish_llm_instance_launch",
                        "data": instance_info,
                    }
                    serialized_message = json.dumps(message).encode('utf-8')
                    socket_to_main.send(serialized_message)

                else:
                    reason = selection.decision.get("reason", "No node can launch the LLM instance")
                    message = {
                        "type": "fail_llm_instance_launch",
                        "data": {
                            "instance_id": message_data["instance_id"],
                            "backend": backend,
                            "error": reason,
                        },
                    }
                    socket_to_main.send(json.dumps(message).encode("utf-8"))
                    logger.info(
                        "Failed to place LLM instance %s: %s",
                        message_data["instance_id"],
                        reason,
                    )
            elif(llm_instance_message.message_type=="stop_llm_instance"):
                self._handle_llm_instance_stop(socket_to_main, message_data)

    def _submit_thread(self,port2:int):
        logger.info(f"Submit start")
        socket_to_main = self.context.socket(zmq.DEALER)
        socket_to_main.connect(f"tcp://127.0.0.1:{port2}")

        while True:
            self.cur_ready_task =  self.task_queue.get()
            if self.cur_ready_task.workflow_id in self._stopped_workflows():
                self.cur_ready_task = None
                continue
            retry_delay = getattr(self.cur_ready_task, "next_eligible_time", 0) - time.time()
            if retry_delay > 0:
                with self.lock:
                    if self.cur_ready_task.workflow_id in self._stopped_workflows():
                        self.cur_ready_task = None
                        continue
                    self.task_queue.put(self.cur_ready_task, self.cur_ready_task.priority)
                    self.cur_ready_task = None
                time.sleep(min(retry_delay, 1))
                continue
            with self.lock:
                if self.cur_ready_task.workflow_id in self._stopped_workflows():
                    self.cur_ready_task = None
                    continue
                self.cur_ready_task.set_task_status("ready")
                if not self.workflow_manager.add_task(self.cur_ready_task):
                    logger.debug(
                        "Ignoring duplicate task dispatch for workflow %s, task %s",
                        self.cur_ready_task.workflow_id,
                        self.cur_ready_task.task_id,
                    )
                    self.cur_ready_task = None
                    continue

                #Get the node can run the task
                dispatch_id = str(uuid.uuid4())
                selection = self.resource_manager.select_node(
                    task_need_resources=self.cur_ready_task.resources,
                    run_id=self.cur_ready_task.workflow_id,
                    task_id=self.cur_ready_task.task_id,
                    attempt=self.cur_ready_task.attempt + 1,
                    dispatch_id=dispatch_id,
                )
                if selection:
                    selected_node = selection.selected_node
                    self.cur_ready_task.pending_reason = None
                    self.cur_ready_task.last_schedule_decision = selection.decision
                    #Run task
                    try:
                        self._run_task_with_lease(self.cur_ready_task, selection, dispatch_id)
                    except Exception as exc:
                        error = exception_to_error_envelope(
                            "scheduler_error",
                            exc,
                            origin="scheduler",
                            attempt=self.cur_ready_task.attempt,
                        )
                        try:
                            self._retry_or_fail_task(socket_to_main, self.cur_ready_task, error)
                        finally:
                            self.cur_ready_task = None
                        continue

                    #Send message to main
                    message = {
                        "type":"start_task",
                        "data":{
                            "workflow_id":self.cur_ready_task.workflow_id,
                            "task_id":self.cur_ready_task.task_id,
                            "node_ip":selected_node.node_ip,
                            "node_id":selected_node.node_id,
                            "gpu_id":selected_node.gpu_id,
                            "attempt":self.cur_ready_task.attempt,
                            "dispatch_id":self.cur_ready_task.dispatch_id,
                            "lease_id":self.cur_ready_task.lease_id,
                            "schedule_decision":selection.decision,
                            "started_at": time.time(),
                        }
                    }
                    serialized_message = json.dumps(message).encode('utf-8')
                    socket_to_main.send(serialized_message)

                    self.cur_ready_task = None
                    continue

                previous_pending_reason = self.cur_ready_task.pending_reason
                self.cur_ready_task.set_task_status("pending")
                self.cur_ready_task.pending_reason = selection.decision.get("reason")
                self.cur_ready_task.last_schedule_decision = selection.decision
                if previous_pending_reason != self.cur_ready_task.pending_reason:
                    self._send_task_pending(socket_to_main, self.cur_ready_task)
                logger.debug("No node can run task %s: %s", self.cur_ready_task.task_id, self.cur_ready_task.pending_reason)
                self.task_queue.put(self.cur_ready_task, self.cur_ready_task.priority)
                self.cur_ready_task = None
            time.sleep(1)

    def _supervisor_thread(self, port2:int):
        logger.info(f"Supervisor start")
        socket_to_main = self.context.socket(zmq.DEALER)
        socket_to_main.connect(f"tcp://127.0.0.1:{port2}")

        while True:
            sleep_seconds = 0
            with self.lock:
                self.resource_manager.check_dead_node()
                self.resource_manager.show_all_node_resource()
                self._fail_timed_out_tasks(socket_to_main)

                running_task_refs:List = self.workflow_manager.get_running_task_refs()
                if len(running_task_refs) == 0:
                    finished_task_refs = []
                    sleep_seconds = 0.05
                else:
                    finished_task_refs, _ = ray.wait(
                        running_task_refs,
                        num_returns=len(running_task_refs),
                        timeout=0,
                    )
                    if len(finished_task_refs) == 0:
                        sleep_seconds = 0.05

                for finished_task_ref in finished_task_refs:
                    finished_task = self.workflow_manager.get_task_by_ref(finished_task_ref)
                    if finished_task is None:
                        continue # The workflow of task is deleted
                    try:
                        raw_result = ray.get(finished_task_ref)
                        if is_task_error_result(raw_result):
                            error = raw_result["error"]
                            file_manifest = raw_result.get("file_manifest")
                            self._retry_or_fail_task(socket_to_main, finished_task, error, file_manifest)
                            continue

                        file_manifest = None
                        metrics = None
                        started_at = None
                        finished_at = None
                        duration_ms = None
                        if isinstance(raw_result, dict) and raw_result.get(TASK_RESULT_ENVELOPE):
                            result = raw_result.get("result") or {}
                            file_manifest = raw_result.get("file_manifest")
                            metrics = raw_result.get("metrics")
                            started_at = raw_result.get("started_at")
                            finished_at = raw_result.get("finished_at")
                            duration_ms = raw_result.get("duration_ms")
                            finished_task.file_manifest = file_manifest
                        else:
                            result = raw_result

                        self.workflow_manager.set_task_result(finished_task,result)
                        self.resource_manager.release_task_resource(tasks=[finished_task])
                        self.workflow_manager.clear_task_ref(finished_task)

                        #Send message to main
                        node_id = None
                        try:
                            node_id = finished_task.selected_node.node_id if finished_task.selected_node else None
                        except Exception:
                            node_id = None
                        message_data = {
                            "workflow_id": finished_task.workflow_id,
                            "task_id": finished_task.task_id,
                            "result": summarize_task_result(
                                finished_task.result,
                                run_id=finished_task.workflow_id,
                                task_id=finished_task.task_id,
                            ),
                            "attempt": finished_task.attempt,
                            "dispatch_id": finished_task.dispatch_id,
                            "lease_id": finished_task.lease_id,
                            "node_id": node_id,
                        }
                        if started_at is not None:
                            message_data["started_at"] = started_at
                        if finished_at is not None:
                            message_data["finished_at"] = finished_at
                        if duration_ms is not None:
                            message_data["duration_ms"] = duration_ms
                        if file_manifest:
                            message_data["file_manifest"] = file_manifest
                        if metrics:
                            message_data["metrics"] = metrics
                        message = {
                            "type":"finish_task",
                            "data": message_data,
                        }
                        serialized_message = json.dumps(message).encode('utf-8')
                        socket_to_main.send(serialized_message)

                    except ray.exceptions.RayTaskError as e:
                        logger.info(f"Task {finished_task.task_id} failed with exception: {e}")
                        error = exception_to_error_envelope(
                            "user_code",
                            e,
                            origin="scheduler",
                            attempt=finished_task.attempt,
                        )
                        self._retry_or_fail_task(socket_to_main, finished_task, error)
                    except ray.exceptions.TaskCancelledError as e:
                        logger.info(f"Task {finished_task.task_id} failed with exception: {e}")
                        error = exception_to_error_envelope(
                            "cancelled",
                            e,
                            origin="scheduler",
                            attempt=finished_task.attempt,
                        )
                        self._retry_or_fail_task(socket_to_main, finished_task, error)
                    except _RAY_RETRYABLE_EXECUTION_EXCEPTIONS as e:
                        logger.info(f"Task {finished_task.task_id} failed with exception: {e}")
                        error_type = _ray_execution_error_type(e)
                        error = exception_to_error_envelope(
                            error_type,
                            e,
                            origin="scheduler",
                            attempt=finished_task.attempt,
                        )
                        self._retry_or_fail_task(socket_to_main, finished_task, error)
                    except Exception as e:
                        logger.error(f"Task {finished_task.task_id} failed with exception: {e}")
                        error = exception_to_error_envelope(
                            "unknown",
                            e,
                            origin="scheduler",
                            attempt=finished_task.attempt,
                        )
                        self._retry_or_fail_task(socket_to_main, finished_task, error)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

    def _launch_ray_head(self):
        command = build_ray_command("start", "--head", "--port", str(self.ray_head_port))
        last_error = None
        for attempt in range(2):
            try:
                subprocess.run(
                    command,
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                last_error = exc
                if attempt == 0:
                    logger.warning(
                        "Ray head startup failed; cleaning a possible stale local runtime before retry"
                    )
                    try:
                        stop_ray_runtime(force=True)
                    except Exception:
                        logger.exception("Failed to clean stale Ray runtime before retry")

        exc = last_error
        detail = (
            getattr(exc, "stderr", None)
            or getattr(exc, "stdout", None)
            or str(exc)
        ).strip()
        raise RuntimeError(f"Failed to start Ray: {detail}") from exc

    def _run_critical_thread(self, name: str, target, *args):
        try:
            target(*args)
        except BaseException as exc:
            self._signal_fatal()
            logger.critical("Critical scheduler thread %s failed", name, exc_info=True)
            try:
                self.ready_queue.put({
                    "status": "error",
                    "error": f"Critical scheduler thread {name} failed: {exc}",
                })
            except Exception:
                pass
            self._cleanup(exit_code=1)

    def start(self):
        self.context = zmq.Context() #zmq context

        self._launch_ray_head()
        self.resource_manager.init()

        self.receive_thread = threading.Thread(
            name="maze-scheduler-receive",
            target=self._run_critical_thread,
            args=("receive", self._receive_thread, self.port1),
        )
        self.receive_thread.start()

        self.monitor_thread = threading.Thread(
            name="maze-scheduler-supervisor",
            target=self._run_critical_thread,
            args=("supervisor", self._supervisor_thread, self.port2),
        )
        self.monitor_thread.start()

        self.submit_thread = threading.Thread(
            name="maze-scheduler-submit",
            target=self._run_critical_thread,
            args=("submit", self._submit_thread, self.port2),
        )
        self.submit_thread.start()

        self.llm_instance_thread = threading.Thread(
            name="maze-scheduler-llm-instance",
            target=self._run_critical_thread,
            args=("llm-instance", self._llm_instance_thread, self.port2),
        )
        self.llm_instance_thread.start()

        self.ready_queue.put("ready")
        self.receive_thread.join()
        self.monitor_thread.join()
        self.submit_thread.join()
