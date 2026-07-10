from logging import Logger
import copy


import logging

from traitlets import Instance
from zmq.backend import select
import ray
import time
import zmq
import threading
import queue
import json
import os
import sys
import base64
import cloudpickle
import binascii
import subprocess
import multiprocessing as mp
import uuid
from queue import Queue
from maze.core.scheduler.resource import SelectedNode
from typing import Any,List,Dict
from maze.core.scheduler.resource import ResourceManager
from maze.core.scheduler.llm_instance import LlmInstanceManager,LlmInstanceMessage
from maze.core.scheduler.runtime import WorkflowRuntimeManager,TaskRuntime,LanggraphTaskRuntime
from maze.core.scheduler.queues import HeterogeneousTaskQueues
from maze.core.scheduler.result_summary import summarize_task_result
from maze.core.scheduler.standby_worker import StandbyWorkerPoolManager
from maze.core.scheduler.strategy import create_scheduling_strategy
from maze.core.workflow.resources import to_internal_scheduler_resources
from maze.core.scheduler.error import (
    enrich_error_for_task,
    exception_to_error_envelope,
    is_task_error_result,
    looks_like_oom,
    make_error_envelope,
)
from maze.core.fault_tolerance import (
    apply_repair_action,
    diagnose_failure,
    record_final_failure,
    record_retry_decision,
    record_success,
    trace_snapshot,
)
from maze.core.workflow.task import TaskType
from maze.core.files.lineage import TASK_RESULT_ENVELOPE

logger = logging.getLogger(__name__)


def _int_resource(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default

def scheduler_process(
    port1:int,
    port2:int,
    strategy:str,
    ray_head_port:int,
    ready_queue:mp.Queue,
    node_scheduling_policy:str|None=None,
):
    scheduler = Scheduler(port1,port2,ray_head_port,ready_queue,strategy,node_scheduling_policy)
    scheduler.start()

class Scheduler():
    def __init__(
        self,
        port1:int,
        port2:int,
        ray_head_port:int,
        ready_queue:mp.Queue,
        strategy:str="FCFS",
        node_scheduling_policy:str|None=None,
    ):
        self.lock = threading.Lock()
        self.port1 = port1
        self.port2 = port2
        self.ray_head_port = ray_head_port
        self.ready_queue = ready_queue

        self.resource_manager = ResourceManager()
        self.resource_manager.set_scheduling_policy(self._node_scheduling_policy(node_scheduling_policy))
        self.llm_instance_manager = LlmInstanceManager()
        self.standby_worker_pool = StandbyWorkerPoolManager.from_env()
        self.workflow_manager = WorkflowRuntimeManager(standby_worker_pool=self.standby_worker_pool)

        self.scheduling_strategy = create_scheduling_strategy(strategy)
        self.strategy = self.scheduling_strategy.name
        self.task_queues = HeterogeneousTaskQueues(self.scheduling_strategy)
        self.llm_instance_queue: Queue = queue.Queue()
        self.stopped_workflow_ids: set[str] = set()
        self.last_llm_scaling_check = 0.0

    def _stopped_workflows(self) -> set[str]:
        if not hasattr(self, "stopped_workflow_ids"):
            self.stopped_workflow_ids = set()
        return self.stopped_workflow_ids

    def _node_scheduling_policy(self, strategy: str | None) -> str:
        normalized = (strategy or "default").strip().lower()
        if normalized in {"least-loaded", "prefer-gpu-free", "spread"}:
            return normalized
        return "default"

    def _task_queue_snapshot_item(self, task: TaskRuntime|LanggraphTaskRuntime, now: float, queue_name: str | None = None):
        strategy = getattr(self, "scheduling_strategy", None) or create_scheduling_strategy(None)
        metadata = strategy.refresh_task_metadata(task, now)
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
            "task_kind": getattr(task, "task_kind", "cpu"),
            "queue_name": queue_name or getattr(task, "queue_name", None) or metadata.get("queue_name"),
            "status": queue_status,
            "runtime_status": task.status,
            "priority": task.priority,
            "predicted_duration": metadata.get("predicted_duration"),
            "prediction_source": metadata.get("prediction_source"),
            "prediction_confidence": metadata.get("prediction_confidence"),
            "prediction_sample_count": metadata.get("prediction_sample_count"),
            "topological_weight": metadata.get("topological_weight"),
            "workflow_wait_time": metadata.get("workflow_wait_time"),
            "remaining_value_tasks": metadata.get("remaining_value_tasks"),
            "hacs_score": metadata.get("hacs_score"),
            "hacs_breakdown": metadata.get("hacs_breakdown"),
            "code_hash": metadata.get("code_hash"),
            "attempt": task.attempt,
            "max_retries": task.max_retries,
            "retry_backoff_seconds": task.retry_backoff_seconds,
            "retry_wait_seconds": round(retry_wait_seconds, 6),
            "next_eligible_time": task.next_eligible_time or None,
            "pending_reason": getattr(task, "pending_reason", None),
            "last_error": getattr(task, "last_error", None),
            "resources": task.resources,
            "schedule_decision": getattr(task, "last_schedule_decision", None),
            "fault_tolerance": trace_snapshot(task),
        }

    def _running_task_snapshot_item(self, task: TaskRuntime|LanggraphTaskRuntime, now: float):
        selected_node = getattr(task, "selected_node", None)
        started_time = getattr(task, "started_time", None)
        elapsed = None if started_time is None else round(max(0.0, now - started_time), 6)
        return {
            "workflow_id": task.workflow_id,
            "task_id": task.task_id,
            "task_type": "langgraph" if isinstance(task, LanggraphTaskRuntime) else "code",
            "task_kind": getattr(task, "task_kind", "cpu"),
            "status": task.status,
            "attempt": task.attempt,
            "resources": task.resources,
            "selected_node": {
                "node_id": getattr(selected_node, "node_id", None),
                "node_ip": getattr(selected_node, "node_ip", None),
                "gpu_id": getattr(selected_node, "gpu_id", None),
            } if selected_node is not None else None,
            "started_time": started_time,
            "elapsed_seconds": elapsed,
            "timeout_seconds": task.timeout_seconds,
            "fault_tolerance": trace_snapshot(task),
        }

    def _public_schedule_decision(self, task: TaskRuntime|LanggraphTaskRuntime, decision: Dict[str, Any]):
        decision = copy.deepcopy(decision)
        decision["internal_requested_resources"] = decision.get("requested_resources")
        decision["requested_resources"] = copy.deepcopy(task.resources)
        decision["queue_name"] = getattr(task, "queue_name", None)
        if getattr(task, "scheduling_metadata", None):
            decision["scheduling"] = copy.deepcopy(task.scheduling_metadata)
        return decision

    def _assign_model_route(self, task: TaskRuntime|LanggraphTaskRuntime, decision: Dict[str, Any]):
        route = self.llm_instance_manager.route_model_request(
            getattr(task, "workflow_id", None),
            getattr(task, "model_anchor", None),
        )
        task.model_route = route
        if route:
            decision["model_route"] = copy.deepcopy(route)
        return route

    def _release_model_route(self, task: TaskRuntime|LanggraphTaskRuntime):
        route = getattr(task, "model_route", None)
        if not route:
            return
        self.llm_instance_manager.release_model_route(route)
        task.model_route = None

    def _manage_llm_instance_scaling(self, now: float | None = None):
        now = now or time.time()
        if now - getattr(self, "last_llm_scaling_check", 0.0) < 5.0:
            return
        self.last_llm_scaling_check = now

        for candidate in self.llm_instance_manager.lru_scale_in_candidates(now=now):
            instance_id = candidate["instance_id"]
            try:
                resource_detail = self.llm_instance_manager.get_instance_resource_detail(instance_id)
                self.resource_manager.release_instance_resource(resource_detail)
            except Exception:
                pass
            self.llm_instance_manager.stop_llm_instance(instance_id)

        for recommendation in self.llm_instance_manager.scale_out_recommendations():
            model = recommendation["model"]
            backend = recommendation["backend"]
            instance_id = str(uuid.uuid4())
            self.llm_instance_manager.mark_model_deploying(model, backend)
            self.llm_instance_queue.put(LlmInstanceMessage("start_llm_instance", {
                "instance_id": instance_id,
                "model": model,
                "backend": backend,
                "cpu_nums": 1,
                "memory": 0,
                "gpu_nums": 1,
                "gpu_mem": recommendation.get("gpu_mem", 0),
                "auto_started": True,
            }))

    def _manage_standby_workers(self):
        pool = getattr(self, "standby_worker_pool", None)
        if pool is None:
            return
        pool.ensure_for_nodes(self.resource_manager.nodes)

    def get_queue_snapshot(self):
        now = time.time()
        queue_tasks = self.task_queues.queue_snapshot(now)
        queued_items = []
        per_queue = {}
        for queue_name, tasks in queue_tasks.items():
            queue_items = [
                self._task_queue_snapshot_item(task, now, queue_name)
                for task in tasks
                if task.workflow_id not in self._stopped_workflows()
            ]
            per_queue[queue_name] = {
                "total": len(queue_items),
                "ready": len([item for item in queue_items if item["status"] == "ready"]),
                "pending": len([item for item in queue_items if item["status"] == "pending"]),
                "retrying": len([item for item in queue_items if item["status"] == "retrying"]),
                "tasks": queue_items,
            }
            queued_items.extend(queue_items)
        running_items = [
            self._running_task_snapshot_item(task, now)
            for task in self.workflow_manager.get_running_tasks()
        ]

        ready_items = [item for item in queued_items if item["status"] == "ready"]
        pending_items = [item for item in queued_items if item["status"] == "pending"]
        retrying_items = [item for item in queued_items if item["status"] == "retrying"]

        return {
            "snapshot_time": now,
            "scheduling_algorithm": self.scheduling_strategy.name,
            "scheduling_policy": self.resource_manager.scheduling_policy,
            "stopped_workflow_ids": sorted(self._stopped_workflows()),
            "counts": {
                "ready": len(ready_items),
                "pending": len(pending_items),
                "retrying": len(retrying_items),
                "running": len(running_items),
                "total_queued": len(queued_items),
                "by_queue": {
                    name: {
                        "ready": data["ready"],
                        "pending": data["pending"],
                        "retrying": data["retrying"],
                        "total": data["total"],
                    }
                    for name, data in per_queue.items()
                },
            },
            "queues": per_queue,
            "ready_tasks": ready_items,
            "pending_tasks": pending_items,
            "retrying_tasks": retrying_items,
            "running_tasks": running_items,
        }

    def _send_task_exception(self, socket_to_main, task: TaskRuntime|LanggraphTaskRuntime, error: Dict[str, Any]):
        file_manifest = getattr(task, "file_manifest", None)
        metrics = getattr(task, "last_metrics", None)
        message = {
            "type": "task_exception",
            "data": {
                "workflow_id": task.workflow_id,
                "task_id": task.task_id,
                "task_kind": getattr(task, "task_kind", "cpu"),
                "queue_name": getattr(task, "queue_name", None),
                "result": error,
                "error": error,
                "attempt": task.attempt,
                "schedule_decision": getattr(task, "last_schedule_decision", None),
                "scheduling": getattr(task, "scheduling_metadata", None),
                "fault_tolerance": trace_snapshot(task),
            },
        }
        if file_manifest:
            message["data"]["file_manifest"] = file_manifest
        if metrics:
            message["data"]["metrics"] = metrics
        socket_to_main.send(json.dumps(message).encode("utf-8"))

    def _send_task_rejected(self, socket_to_main, message_data: Dict[str, Any], exc: BaseException):
        error = exception_to_error_envelope(
            "scheduler_error",
            exc,
            origin="scheduler",
        )
        message = {
            "type": "task_exception",
            "data": {
                "workflow_id": message_data.get("workflow_id"),
                "task_id": message_data.get("task_id"),
                "task_kind": message_data.get("task_kind", "cpu"),
                "result": error,
                "error": error,
                "attempt": 0,
                "resources": message_data.get("resources"),
                "schedule_decision": {
                    "reason": "task_rejected_by_scheduler",
                },
            },
        }
        socket_to_main.send(json.dumps(message).encode("utf-8"))

    def _send_task_retry(self, socket_to_main, task: TaskRuntime|LanggraphTaskRuntime, error: Dict[str, Any]):
        message = {
            "type": "task_retry",
            "data": {
                "workflow_id": task.workflow_id,
                "task_id": task.task_id,
                "task_kind": getattr(task, "task_kind", "cpu"),
                "queue_name": getattr(task, "queue_name", None),
                "error": error,
                "attempt": task.attempt,
                "next_attempt": task.attempt + 1,
                "retry_backoff_seconds": task.retry_backoff_seconds,
                "resources": task.resources,
                "schedule_decision": getattr(task, "last_schedule_decision", None),
                "scheduling": getattr(task, "scheduling_metadata", None),
                "fault_tolerance": trace_snapshot(task),
            },
        }
        socket_to_main.send(json.dumps(message).encode("utf-8"))

    def _send_task_pending(self, socket_to_main, task: TaskRuntime|LanggraphTaskRuntime):
        message = {
            "type": "task_pending",
            "data": {
                "workflow_id": task.workflow_id,
                "task_id": task.task_id,
                "task_kind": getattr(task, "task_kind", "cpu"),
                "queue_name": getattr(task, "queue_name", None),
                "pending_reason": task.pending_reason,
                "schedule_decision": task.last_schedule_decision,
                "attempt": task.attempt,
                "scheduling": getattr(task, "scheduling_metadata", None),
            },
        }
        socket_to_main.send(json.dumps(message).encode("utf-8"))

    def _retry_or_fail_task(
        self,
        socket_to_main,
        task: TaskRuntime|LanggraphTaskRuntime,
        error: Dict[str, Any],
        file_manifest: Dict[str, Any] | None = None,
        metrics: Dict[str, Any] | None = None,
    ):
        error = enrich_error_for_task(error, task)
        task.last_error = error
        task.last_metrics = metrics
        self._release_model_route(task)
        if file_manifest:
            task.file_manifest = file_manifest
        self._stopped_workflows().add(task.workflow_id)
        self.workflow_manager.clear_task_ref(task)

        diagnosis = diagnose_failure(error, task)
        should_retry = task.should_retry(error) and bool(diagnosis.get("recoverable", False))
        repair_action = None
        original_resources = copy.deepcopy(task.resources)
        if should_retry:
            repair_action = apply_repair_action(task, error, diagnosis)
            should_retry = bool(repair_action.get("applied", False))

        if should_retry:
            self._stopped_workflows().discard(task.workflow_id)
            self._release_task_attempt_resource(task, original_resources)
            error["repair_action"] = copy.deepcopy(repair_action)
            if repair_action.get("adjusted_resources"):
                error["adjusted_resources"] = copy.deepcopy(repair_action.get("adjusted_resources"))
            task.schedule_retry(error)
            record_retry_decision(
                task,
                error,
                diagnosis,
                repair_action,
                retry_scheduled=True,
                next_attempt=task.attempt + 1,
            )
            self.task_queues.put(task)
            self._send_task_retry(socket_to_main, task, error)
            return

        if repair_action is None:
            repair_action = {
                "type": "none",
                "applied": False,
                "reason": "retry_policy_rejected" if not task.should_retry(error) else "not_recoverable",
            }
        error["repair_action"] = copy.deepcopy(repair_action)
        record_retry_decision(
            task,
            error,
            diagnosis,
            repair_action,
            retry_scheduled=False,
            next_attempt=None,
        )
        record_final_failure(task, error)

        canceld_tasks = self.workflow_manager.cancel_workflow(task.workflow_id)
        if len(canceld_tasks) > 0:
            self.resource_manager.release_task_resource(tasks=canceld_tasks)
            self.workflow_manager.clear_workflow(task.workflow_id)
        self.resource_manager.release_dag_context(task.workflow_id)
        self._send_task_exception(socket_to_main, task, error)

    def _release_task_attempt_resource(self, task: TaskRuntime|LanggraphTaskRuntime, resources: Dict[str, Any]):
        current_resources = task.resources
        current_scheduler_resources = task.scheduler_resources
        try:
            task.resources = resources
            task.scheduler_resources = to_internal_scheduler_resources(
                resources,
                task_kind=getattr(task, "task_kind", None),
                model_anchor=getattr(task, "model_anchor", None),
            )
            self.resource_manager.release_task_resource(tasks=[task])
        finally:
            task.resources = current_resources
            task.scheduler_resources = current_scheduler_resources

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

    def _cleanup(self):
        try:
            subprocess.run(
                [sys.executable, "-m", "ray.scripts.scripts", "stop"],
                check=False,
                text=True,
                capture_output=True,
            )
        except Exception as exc:
            print(f"Exception occurred while stopping Ray: {exc}")

        os._exit(0)

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
                    try:
                        self._stopped_workflows().discard(message_data["workflow_id"])
                        if(message_data["task_type"]==TaskType.CODE.value):
                            task_runtime = TaskRuntime(workflow_id=message_data['workflow_id'],
                                                                    task_id=message_data['task_id'],
                                                                    task_input=message_data['task_input'],
                                                                    task_output=message_data['task_output'],
                                                                    resources=message_data['resources'],
                                                                    task_kind=message_data.get('task_kind'),
                                                                    model_anchor=message_data.get('model_anchor'),
                                                                    code_str=message_data.get('code_str'),
                                                                    code_ser=message_data.get('code_ser'),
                                                                    file_context=message_data.get('file_context'),
                                                                    max_retries=message_data.get('max_retries'),
                                                                    retry_backoff_seconds=message_data.get('retry_backoff_seconds', 0),
                                                                    retry_on=message_data.get('retry_on'),
                                                                    timeout_seconds=message_data.get('timeout_seconds'),
                                                                    scheduling_context=message_data.get('scheduling_context'),
                                                                    )
                            priority =  message_data.get('priority', 0)
                            task_runtime.set_priority(priority)
                            self.task_queues.put(task_runtime)
                        elif(message_data["task_type"]==TaskType.LANGGRAPH.value):
                            task_runtime = LanggraphTaskRuntime(workflow_id=message_data['workflow_id'],
                                                                                      task_id=message_data['task_id'],
                                                                                      code_ser=message_data['code_ser'],
                                                                                      args=message_data['args'],
                                                                                      kwargs=message_data['kwargs'],
                                                                                      resources=message_data['resources'],
                                                                                      task_kind=message_data.get('task_kind'),
                                                                                      model_anchor=message_data.get('model_anchor'),
                                                                                      max_retries=message_data.get('max_retries'),
                                                                                      retry_backoff_seconds=message_data.get('retry_backoff_seconds', 0),
                                                                                      retry_on=message_data.get('retry_on'),
                                                                                      timeout_seconds=message_data.get('timeout_seconds'),
                                                                                      scheduling_context=message_data.get('scheduling_context'),
                                                                                    )
                            priority =  message_data.get('priority', 0)
                            task_runtime.set_priority(priority)
                            self.task_queues.put(task_runtime)
                    except Exception as exc:
                        logger.exception(
                            "Rejecting task %s before scheduling",
                            message_data.get("task_id"),
                        )
                        self._send_task_rejected(socket_to_main, message_data, exc)
                elif(message_type =="clear_workflow" ):
                    with self.lock:
                        self._stopped_workflows().add(message_data["workflow_id"])
                        self.workflow_manager.clear_workflow(workflow_id=message_data["workflow_id"])
                        self.resource_manager.release_dag_context(message_data["workflow_id"])
                elif(message_type =="stop_workflow" ):
                    with self.lock:
                        self._stopped_workflows().add(message_data["workflow_id"])
                        canceld_tasks = self.workflow_manager.cancel_workflow(workflow_id=message_data["workflow_id"])
                        if len(canceld_tasks) > 0:
                            for task in canceld_tasks:
                                self._release_model_route(task)
                            self.resource_manager.release_task_resource(tasks=canceld_tasks)
                            self.workflow_manager.clear_workflow(workflow_id=message_data["workflow_id"])
                        self.resource_manager.release_dag_context(message_data["workflow_id"])
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
                        resources["standby_workers"] = self.standby_worker_pool.snapshot()
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
                elif(message_type=="set_node_disabled"):
                    request_id = message_data.get("request_id")
                    try:
                        with self.lock:
                            result = self.resource_manager.set_node_disabled(
                                node_id=message_data["node_id"],
                                disabled=bool(message_data.get("disabled")),
                            )
                        response = {
                            "type": "cluster_control",
                            "data": {
                                "request_id": request_id,
                                "ok": True,
                                **result,
                            },
                        }
                    except Exception as exc:
                        response = {
                            "type": "cluster_control",
                            "data": {
                                "request_id": request_id,
                                "ok": False,
                                "error": str(exc),
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

        except Exception as e:
            print(f"_receive_thread error: {e}")
            self._cleanup()

    def _llm_instance_thread(self,port2:int):
        logger.info(f"Llm instance start")
        socket_to_main = self.context.socket(zmq.DEALER)
        socket_to_main.connect(f"tcp://127.0.0.1:{port2}")

        while True:
            llm_instance_message = self.llm_instance_queue.get()
            message_data = llm_instance_message.message_data

            self.lock.acquire()
            if(llm_instance_message.message_type=="start_llm_instance"):
                need_resources = {
                    'cpu':message_data.get('cpu_nums', 0),
                    'cpu_mem':message_data.get('memory', 0),
                    'gpu':message_data.get('gpu_nums', 0),
                    'gpu_mem':message_data.get('gpu_mem', 0)
                }
                selection = self.resource_manager.select_node(
                    task_need_resources=need_resources,
                    reservation_kind="instance",
                )
                if selection:
                    selected_node = selection.selected_node
                    port = self.llm_instance_manager.start_llm_instance(
                                                    instance_id = message_data["instance_id"],
                                                    model=message_data["model"],
                                                    node_ip=selected_node.node_ip,
                                                    node_id=selected_node.node_id,
                                                    gpu_id=selected_node.gpu_id,
                                                    resources=need_resources,
                                                    backend=message_data.get("backend", "vllm"),
                                                )

                    #Send message to main
                    if not message_data.get("auto_started"):
                        message = {
                            "type":"finish_llm_instance_launch",
                            "data":{
                                "host":selected_node.node_ip,
                                "port":port,
                                "instance_id":message_data["instance_id"]
                            }
                        }
                        serialized_message = json.dumps(message).encode('utf-8')
                        socket_to_main.send(serialized_message)

                    self.lock.release()
                else:
                    logger.debug("No node can launch the LLM instance: %s", selection.decision.get("reason"))
                    self.lock.release()
                    self.llm_instance_queue.put(llm_instance_message)
                    time.sleep(1)
            elif(llm_instance_message.message_type=="stop_llm_instance"):
                instance_id = message_data["instance_id"]
                resource_detail = self.llm_instance_manager.get_instance_resource_detail(instance_id=instance_id)
                self.resource_manager.release_instance_resource(resource_detail=resource_detail)

                self.llm_instance_manager.stop_llm_instance(instance_id=instance_id)
                self.lock.release()

    def _submit_thread(self,port2:int):
        logger.info(f"Submit start")
        socket_to_main = self.context.socket(zmq.DEALER)
        socket_to_main.connect(f"tcp://127.0.0.1:{port2}")

        while True:
            if not self.task_queues.wait_for_task(timeout=0.5):
                continue
            dispatched_or_removed = False
            attempted_head = False
            now = time.time()

            for queue_name in self.task_queues.queue_names():
                self.cur_ready_task = self.task_queues.peek(queue_name, now)
                if self.cur_ready_task is None:
                    continue

                if self.cur_ready_task.workflow_id in self._stopped_workflows():
                    self.task_queues.pop_head(queue_name, self.cur_ready_task)
                    self.cur_ready_task = None
                    dispatched_or_removed = True
                    continue

                retry_delay = getattr(self.cur_ready_task, "next_eligible_time", 0) - time.time()
                if retry_delay > 0:
                    continue

                attempted_head = True
                self.lock.acquire()
                try:
                    self.cur_ready_task.set_task_status("ready")
                    self.workflow_manager.add_task(self.cur_ready_task)

                    #Get the node can run the task
                    selection = self.resource_manager.select_node(
                        task_need_resources=self.cur_ready_task.scheduler_resources,
                        model_anchor=getattr(self.cur_ready_task, "model_anchor", None),
                        workflow_id=getattr(self.cur_ready_task, "workflow_id", None),
                    )
                    if selection:
                        selected_node = selection.selected_node
                        self.task_queues.pop_head(queue_name, self.cur_ready_task)
                        self.cur_ready_task.pending_reason = None
                        self._assign_model_route(self.cur_ready_task, selection.decision)
                        selection.decision = self._public_schedule_decision(self.cur_ready_task, selection.decision)
                        self.cur_ready_task.last_schedule_decision = selection.decision
                        #Run task
                        self.workflow_manager.run_task(task=self.cur_ready_task,node=selected_node)

                        #Send message to main
                        message = {
                            "type":"start_task",
                            "data":{
                                "workflow_id":self.cur_ready_task.workflow_id,
                                "task_id":self.cur_ready_task.task_id,
                                "task_kind":getattr(self.cur_ready_task, "task_kind", "cpu"),
                                "queue_name": queue_name,
                                "node_ip":selected_node.node_ip,
                                "node_id":selected_node.node_id,
                                "gpu_id":selected_node.gpu_id,
                                "attempt":self.cur_ready_task.attempt,
                                "schedule_decision":selection.decision,
                                "scheduling": getattr(self.cur_ready_task, "scheduling_metadata", None),
                                "started_at": time.time(),
                            }
                        }
                        serialized_message = json.dumps(message).encode('utf-8')
                        socket_to_main.send(serialized_message)

                        self.cur_ready_task = None
                        dispatched_or_removed = True
                    else:
                        previous_pending_reason = self.cur_ready_task.pending_reason
                        pending_reason = selection.decision.get("reason")
                        self.cur_ready_task.set_task_status("pending")
                        self.cur_ready_task.pending_reason = pending_reason
                        selection.decision = self._public_schedule_decision(self.cur_ready_task, selection.decision)
                        self.cur_ready_task.last_schedule_decision = selection.decision
                        if previous_pending_reason != self.cur_ready_task.pending_reason:
                            self._send_task_pending(socket_to_main, self.cur_ready_task)
                        logger.debug("No node can run task %s: %s", self.cur_ready_task.task_id, self.cur_ready_task.pending_reason)
                finally:
                    self.lock.release()

            if not dispatched_or_removed and attempted_head:
                time.sleep(0.1)
            elif not dispatched_or_removed:
                time.sleep(0.05)

    def _supervisor_thread(self, port2:int):
        logger.info(f"Supervisor start")
        socket_to_main = self.context.socket(zmq.DEALER)
        socket_to_main.connect(f"tcp://127.0.0.1:{port2}")

        while True:
            sleep_seconds = 0
            with self.lock:
                self.resource_manager.check_dead_node()
                self.resource_manager.show_all_node_resource()
                self._manage_standby_workers()
                self._manage_llm_instance_scaling()
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
                            metrics = raw_result.get("metrics")
                            self._retry_or_fail_task(socket_to_main, finished_task, error, file_manifest, metrics)
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

                        self._release_model_route(finished_task)
                        self.workflow_manager.set_task_result(finished_task,result)
                        self.resource_manager.release_task_resource(tasks=[finished_task])
                        self.workflow_manager.clear_task_ref(finished_task)
                        fault_tolerance = record_success(finished_task)

                        #Send message to main
                        node_id = None
                        try:
                            node_id = finished_task.selected_node.node_id if finished_task.selected_node else None
                        except Exception:
                            node_id = None
                        message_data = {
                            "workflow_id": finished_task.workflow_id,
                            "task_id": finished_task.task_id,
                            "task_kind": getattr(finished_task, "task_kind", "cpu"),
                            "result": summarize_task_result(
                                finished_task.result,
                                run_id=finished_task.workflow_id,
                                task_id=finished_task.task_id,
                            ),
                            "attempt": finished_task.attempt,
                            "node_id": node_id,
                            "schedule_decision": getattr(finished_task, "last_schedule_decision", None),
                            "fault_tolerance": fault_tolerance,
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
                            "resource_insufficient" if looks_like_oom(e) else "user_code",
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
                    except (ray.exceptions.NodeDiedError, ray.exceptions.ObjectLostError, ray.exceptions.TaskUnschedulableError) as e:
                        logger.info(f"Task {finished_task.task_id} failed with exception: {e}")
                        error_type = "resource_unavailable" if isinstance(e, ray.exceptions.TaskUnschedulableError) else "node_lost"
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
        try:
            command = [
                sys.executable,
                "-m",
                "ray.scripts.scripts",
                "start",
                "--head",
                "--port",
                str(self.ray_head_port),
            ]
            result = subprocess.run(
                command,
                check=True,
                text=True,
                capture_output=True,
            )


            if result.returncode != 0:
                raise RuntimeError(f"Failed to start Ray: {result.stderr}")

        except Exception as e:
            print(f"Exception occurred: {e}")

    def start(self):
        self.context = zmq.Context() #zmq context

        self._launch_ray_head()
        self.resource_manager.init()

        self.receive_thread = threading.Thread(target=self._receive_thread,args=(self.port1,))
        self.receive_thread.start()

        self.monitor_thread = threading.Thread(target=self._supervisor_thread,args=(self.port2,))
        self.monitor_thread.start()

        self.submit_thread = threading.Thread(target=self._submit_thread,args=(self.port2,))
        self.submit_thread.start()

        self.llm_instance_thread = threading.Thread(target=self._llm_instance_thread,args=(self.port2,))
        self.llm_instance_thread.start()

        self.ready_queue.put("ready")
        self.receive_thread.join()
        self.monitor_thread.join()
        self.submit_thread.join()
