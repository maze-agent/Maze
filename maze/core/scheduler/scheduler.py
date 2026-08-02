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
from concurrent.futures import CancelledError, ThreadPoolExecutor, wait as wait_for_futures
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
from maze.core.scheduler.queues import HeterogeneousTaskQueues
from maze.core.scheduler.result_summary import summarize_task_result
from maze.core.scheduler.standby_worker import StandbyWorkerPoolManager
from maze.core.scheduler.strategy import create_scheduling_strategy
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

LLM_START_EXECUTOR_WORKERS = 2
LLM_MAINTENANCE_EXECUTOR_WORKERS = 4
LLM_CONTROL_POLL_SECONDS = 0.05
SCHEDULER_MESSAGE_SEND_TIMEOUT_SECONDS = 5.0
SCHEDULER_EVENT_QUEUE_MAXSIZE = 64
SCHEDULER_PRODUCER_STOP_TIMEOUT_SECONDS = 2.0
SCHEDULER_EXECUTOR_STOP_TIMEOUT_SECONDS = 2.0
SCHEDULER_PROCESS_MARKER_ENV = "MAZE_SCHEDULER_PROCESS"
SCHEDULER_OWNER_ID_ENV = "MAZE_SCHEDULER_OWNER_ID"
SCHEDULER_IDENTITY_MEMFD_NAME = "maze-scheduler-identity-v1"
SCHEDULER_IDENTITY_SCHEMA = "maze_scheduler_process_identity/v1"
_SCHEDULER_IDENTITY_FD = None
_SCHEDULER_EVENT_SENDER_STOP = object()


class SchedulerMessageSendTimeout(TimeoutError):
    pass


def _int_resource(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


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


def _scheduler_process_start_ticks() -> int:
    with open("/proc/self/stat", encoding="ascii") as handle:
        stat_text = handle.read()
    tail = stat_text[stat_text.rfind(")") + 2 :].split()
    start_ticks = int(tail[19])
    if start_ticks <= 0:
        raise ValueError("Scheduler process start ticks are invalid")
    return start_ticks


def _create_scheduler_identity_memfd(owner_id: str) -> int | None:
    try:
        import fcntl
    except ImportError:
        return None

    required_os_names = ("memfd_create", "MFD_ALLOW_SEALING", "MFD_CLOEXEC")
    required_fcntl_names = (
        "F_ADD_SEALS",
        "F_GET_SEALS",
        "F_SEAL_GROW",
        "F_SEAL_SEAL",
        "F_SEAL_SHRINK",
        "F_SEAL_WRITE",
    )
    if not all(hasattr(os, name) for name in required_os_names) or not all(
        hasattr(fcntl, name) for name in required_fcntl_names
    ):
        return None

    descriptor = None
    try:
        descriptor = os.memfd_create(
            SCHEDULER_IDENTITY_MEMFD_NAME,
            os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC,
        )
        payload = json.dumps(
            {
                "owner_id": owner_id,
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "process": "scheduler",
                "schema": SCHEDULER_IDENTITY_SCHEMA,
                "session_id": os.environ.get("MAZE_PHASE2_ACCEPTANCE_SESSION"),
                "start_ticks": _scheduler_process_start_ticks(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("Scheduler identity receipt write failed")
            offset += written
        os.lseek(descriptor, 0, os.SEEK_SET)
        required_seals = (
            fcntl.F_SEAL_WRITE
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_SEAL
        )
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, required_seals)
        actual_seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
        if actual_seals & required_seals != required_seals:
            raise OSError("Scheduler identity receipt sealing failed")
        return descriptor
    except (IndexError, OSError, ValueError):
        if descriptor is not None:
            os.close(descriptor)
        return None


def _mark_scheduler_process(owner_id: str | None) -> str:
    global _SCHEDULER_IDENTITY_FD

    candidate = uuid.uuid4().hex if owner_id is None else owner_id
    if not isinstance(candidate, str):
        raise ValueError("Scheduler owner_id must be 32 hexadecimal characters")
    normalized = candidate.lower()
    if (
        len(normalized) != 32
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        raise ValueError("Scheduler owner_id must be 32 hexadecimal characters")

    identity_fd = _create_scheduler_identity_memfd(normalized)
    previous_identity_fd = _SCHEDULER_IDENTITY_FD
    _SCHEDULER_IDENTITY_FD = identity_fd
    if previous_identity_fd is not None and previous_identity_fd != identity_fd:
        try:
            os.close(previous_identity_fd)
        except OSError:
            pass

    # Publish the process marker last so in-process observers see full identity.
    os.environ[SCHEDULER_OWNER_ID_ENV] = normalized
    os.environ[SCHEDULER_PROCESS_MARKER_ENV] = "1"
    return normalized

def scheduler_process(
    port1:int,
    port2:int,
    strategy:str,
    ray_head_port:int,
    ready_queue:mp.Queue,
    node_scheduling_policy:str|None=None,
    fatal_event=None,
    owner_id: str | None = None,
    owner_cleanup_complete_event=None,
    ray_cleanup_complete_event=None,
    owner_node_sender=None,
):
    try:
        owner_id = _mark_scheduler_process(owner_id)
        scheduler = Scheduler(
            port1,
            port2,
            ray_head_port,
            ready_queue,
            strategy,
            node_scheduling_policy,
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
        port1:int,
        port2:int,
        ray_head_port:int,
        ready_queue:mp.Queue,
        strategy:str="FCFS",
        node_scheduling_policy:str|None=None,
        fatal_event=None,
        owner_id: str | None = None,
        owner_cleanup_complete_event=None,
        ray_cleanup_complete_event=None,
        owner_node_sender=None,
    ):
        self.lock = threading.Lock()
        self._process_exit_lock = threading.Lock()
        self._scheduler_event_send_lock = threading.Lock()
        self._scheduler_event_queue = None
        self._scheduler_event_sender_stop = None
        self._scheduler_event_sender_ready = None
        self._scheduler_event_sender_stopped = None
        self._scheduler_event_sender_failure = None
        self.scheduler_event_thread = None
        self._shutdown_event = threading.Event()
        self.port1 = port1
        self.port2 = port2
        self.ray_head_port = ray_head_port
        self.ready_queue = ready_queue
        self.fatal_event = fatal_event
        self.owner_id = owner_id or uuid.uuid4().hex
        self.owner_cleanup_complete_event = owner_cleanup_complete_event
        self.ray_cleanup_complete_event = ray_cleanup_complete_event
        self.owner_node_sender = owner_node_sender

        self.resource_manager = ResourceManager()
        self.resource_manager.set_scheduling_policy(self._node_scheduling_policy(node_scheduling_policy))
        self.llm_instance_manager = LlmInstanceManager(owner_id=self.owner_id)
        self.standby_worker_pool = StandbyWorkerPoolManager.from_env()
        self.workflow_manager = WorkflowRuntimeManager(standby_worker_pool=self.standby_worker_pool)

        self.scheduling_strategy = create_scheduling_strategy(strategy)
        self.strategy = self.scheduling_strategy.name
        self.task_queues = HeterogeneousTaskQueues(self.scheduling_strategy)
        self.llm_instance_queue: Queue = queue.Queue()
        self.stopped_workflow_ids: set[str] = set()
        self.last_llm_scaling_check = 0.0
        self._ensure_llm_async_state()

    def _ensure_llm_async_state(self) -> None:
        if not hasattr(self, "_llm_start_executor"):
            self._llm_start_executor = ThreadPoolExecutor(
                max_workers=LLM_START_EXECUTOR_WORKERS,
                thread_name_prefix="maze-llm-start",
            )
        if not hasattr(self, "_llm_maintenance_executor"):
            self._llm_maintenance_executor = ThreadPoolExecutor(
                max_workers=LLM_MAINTENANCE_EXECUTOR_WORKERS,
                thread_name_prefix="maze-llm-maintenance",
            )
        if not hasattr(self, "_llm_pending_starts"):
            self._llm_pending_starts = {}
        if not hasattr(self, "_llm_start_futures"):
            self._llm_start_futures = {}
        if not hasattr(self, "_llm_control_stop_futures"):
            self._llm_control_stop_futures = {}
        if not hasattr(self, "_llm_runtime_probe_future"):
            self._llm_runtime_probe_future = None
        if not hasattr(self, "_llm_runtime_cleanup_futures"):
            self._llm_runtime_cleanup_futures = {}
        if not hasattr(self, "_llm_instance_start_request_ids"):
            self._llm_instance_start_request_ids = {}

    def _shutdown_llm_executors(self) -> bool:
        for record in list(getattr(self, "_llm_pending_starts", {}).values()):
            record["cancel_event"].set()
            self.llm_instance_manager.request_start_cancellation(
                record["message_data"]["instance_id"]
            )
            record["future"].cancel()

        futures = set(getattr(self, "_llm_start_futures", {}))
        futures.update(getattr(self, "_llm_control_stop_futures", {}))
        probe_future = getattr(self, "_llm_runtime_probe_future", None)
        if probe_future is not None:
            futures.add(probe_future)
        futures.update(
            future
            for future, _candidate in getattr(
                self,
                "_llm_runtime_cleanup_futures",
                {},
            ).values()
        )
        for future in futures:
            future.cancel()
        for name in ("_llm_start_executor", "_llm_maintenance_executor"):
            executor = getattr(self, name, None)
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
        if not futures:
            return True
        _done, pending = wait_for_futures(
            futures,
            timeout=SCHEDULER_EXECUTOR_STOP_TIMEOUT_SECONDS,
        )
        if pending:
            logger.error(
                "%s LLM control future(s) did not stop before cleanup",
                len(pending),
            )
            return False
        return True

    def _shutdown_requested(self) -> bool:
        event = getattr(self, "_shutdown_event", None)
        return bool(event is not None and event.is_set())

    def _request_shutdown(self) -> None:
        event = self.__dict__.setdefault("_shutdown_event", threading.Event())
        event.set()

    def _join_scheduler_producers(self) -> bool:
        deadline = time.monotonic() + SCHEDULER_PRODUCER_STOP_TIMEOUT_SECONDS
        all_stopped = True
        current = threading.current_thread()
        for name in (
            "receive_thread",
            "monitor_thread",
            "submit_thread",
            "llm_instance_thread",
        ):
            thread = getattr(self, name, None)
            if thread is None or thread is current or not thread.is_alive():
                continue
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                all_stopped = False
                logger.error(
                    "Scheduler producer thread %s did not stop before cleanup",
                    thread.name,
                )
        return all_stopped

    def _stopped_workflows(self) -> set[str]:
        if not hasattr(self, "stopped_workflow_ids"):
            self.stopped_workflow_ids = set()
        return self.stopped_workflow_ids

    def _scheduler_event_send_timeout(self) -> float:
        return max(
            0.001,
            float(
                getattr(
                    self,
                    "scheduler_message_send_timeout_seconds",
                    SCHEDULER_MESSAGE_SEND_TIMEOUT_SECONDS,
                )
            ),
        )

    @staticmethod
    def _scheduler_event_timeout_error(
        message: Dict[str, Any],
        timeout_seconds: float,
    ) -> SchedulerMessageSendTimeout:
        return SchedulerMessageSendTimeout(
            f"Timed out sending Scheduler event {message.get('type')!r} "
            f"after {timeout_seconds:g} seconds"
        )

    def _send_scheduler_event_direct(
        self,
        socket_to_main,
        message: Dict[str, Any],
        *,
        deadline: float,
        timeout_seconds: float,
    ) -> None:
        send_lock = self.__dict__.setdefault(
            "_scheduler_event_send_lock",
            threading.Lock(),
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not send_lock.acquire(timeout=remaining):
            raise self._scheduler_event_timeout_error(message, timeout_seconds)
        try:
            payload = json.dumps(message).encode("utf-8")
            poll = getattr(socket_to_main, "poll", None)
            if poll is None:
                # Lightweight test doubles do not implement the ZMQ readiness API.
                socket_to_main.send(payload)
                return

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._scheduler_event_timeout_error(
                        message,
                        timeout_seconds,
                    )
                ready = poll(max(1, int(remaining * 1000)), zmq.POLLOUT)
                if not ready:
                    raise self._scheduler_event_timeout_error(
                        message,
                        timeout_seconds,
                    )
                try:
                    socket_to_main.send(payload, flags=zmq.NOBLOCK)
                    return
                except zmq.Again:
                    continue
        finally:
            send_lock.release()

    def _send_scheduler_event(self, socket_to_main, message: Dict[str, Any]) -> None:
        timeout_seconds = max(
            0.001,
            self._scheduler_event_send_timeout(),
        )
        deadline = time.monotonic() + timeout_seconds
        event_queue = getattr(self, "_scheduler_event_queue", None)
        if event_queue is None:
            self._send_scheduler_event_direct(
                socket_to_main,
                message,
                deadline=deadline,
                timeout_seconds=timeout_seconds,
            )
            return

        stop_event = self._scheduler_event_sender_stop
        if stop_event.is_set():
            failure = getattr(self, "_scheduler_event_sender_failure", None)
            if failure is not None:
                raise failure
            raise RuntimeError("Scheduler event sender is stopped")

        request = {
            "message": message,
            "deadline": deadline,
            "timeout_seconds": timeout_seconds,
            "done": threading.Event(),
            "error": None,
        }
        remaining = deadline - time.monotonic()
        try:
            event_queue.put(request, timeout=max(0.0, remaining))
        except queue.Full:
            raise self._scheduler_event_timeout_error(message, timeout_seconds)

        remaining = deadline - time.monotonic()
        if remaining <= 0 or not request["done"].wait(remaining):
            raise self._scheduler_event_timeout_error(message, timeout_seconds)
        if request["error"] is not None:
            raise request["error"]

    def _fail_pending_scheduler_events(self, error: BaseException) -> None:
        event_queue = getattr(self, "_scheduler_event_queue", None)
        if event_queue is None:
            return
        while True:
            try:
                request = event_queue.get_nowait()
            except queue.Empty:
                return
            if request is _SCHEDULER_EVENT_SENDER_STOP:
                continue
            request["error"] = error
            request["done"].set()

    @staticmethod
    def _configure_scheduler_event_socket(socket_to_main) -> None:
        setsockopt = getattr(socket_to_main, "setsockopt", None)
        if setsockopt is None:
            return
        setsockopt(zmq.SNDHWM, SCHEDULER_EVENT_QUEUE_MAXSIZE)
        setsockopt(zmq.IMMEDIATE, 1)

    def _scheduler_event_sender_thread(self, port2: int) -> None:
        socket_to_main = None
        failure = None
        try:
            socket_to_main = self.context.socket(zmq.DEALER)
            self._configure_scheduler_event_socket(socket_to_main)
            socket_to_main.connect(f"tcp://127.0.0.1:{port2}")
            self._scheduler_event_sender_ready.set()

            while True:
                try:
                    request = self._scheduler_event_queue.get(timeout=0.1)
                except queue.Empty:
                    if self._scheduler_event_sender_stop.is_set():
                        return
                    continue
                if request is _SCHEDULER_EVENT_SENDER_STOP:
                    return

                try:
                    self._send_scheduler_event_direct(
                        socket_to_main,
                        request["message"],
                        deadline=request["deadline"],
                        timeout_seconds=request["timeout_seconds"],
                    )
                except BaseException as exc:
                    request["error"] = exc
                    request["done"].set()
                    failure = exc
                    self._scheduler_event_sender_failure = exc
                    raise
                else:
                    request["done"].set()
        except BaseException as exc:
            failure = failure or exc
            self._scheduler_event_sender_failure = failure
            raise
        finally:
            self._scheduler_event_sender_stop.set()
            if not self._scheduler_event_sender_ready.is_set():
                self._scheduler_event_sender_ready.set()
            self._fail_pending_scheduler_events(
                failure or RuntimeError("Scheduler event sender stopped")
            )
            if socket_to_main is not None:
                close = getattr(socket_to_main, "close", None)
                if close is not None:
                    try:
                        close(linger=0)
                    except TypeError:
                        close()
            self._scheduler_event_sender_stopped.set()

    def _start_scheduler_event_sender(self, port2: int) -> None:
        self._scheduler_event_queue = queue.Queue(
            maxsize=SCHEDULER_EVENT_QUEUE_MAXSIZE
        )
        self._scheduler_event_sender_stop = threading.Event()
        self._scheduler_event_sender_ready = threading.Event()
        self._scheduler_event_sender_stopped = threading.Event()
        self._scheduler_event_sender_failure = None
        self.scheduler_event_thread = threading.Thread(
            name="maze-scheduler-event-sender",
            target=self._run_critical_thread,
            args=(
                "event-sender",
                self._scheduler_event_sender_thread,
                port2,
            ),
        )
        self.scheduler_event_thread.start()
        timeout_seconds = self._scheduler_event_send_timeout()
        if not self._scheduler_event_sender_ready.wait(timeout_seconds):
            raise SchedulerMessageSendTimeout(
                f"Scheduler event sender did not start within {timeout_seconds:g} seconds"
            )
        if self._scheduler_event_sender_failure is not None:
            raise self._scheduler_event_sender_failure

    def _stop_scheduler_event_sender(self) -> bool:
        event_queue = getattr(self, "_scheduler_event_queue", None)
        stop_event = getattr(self, "_scheduler_event_sender_stop", None)
        sender_thread = getattr(self, "scheduler_event_thread", None)
        if event_queue is None or stop_event is None or sender_thread is None:
            return True

        stop_event.set()
        if sender_thread is threading.current_thread():
            return False
        try:
            event_queue.put_nowait(_SCHEDULER_EVENT_SENDER_STOP)
        except queue.Full:
            # The sender observes stop_event after draining the bounded queue.
            pass
        sender_thread.join(self._scheduler_event_send_timeout() + 0.5)
        if sender_thread.is_alive():
            logger.error("Scheduler event sender did not stop before shutdown")
            return False
        return True

    def _thread_scheduler_event_socket(self, port2: int):
        if getattr(self, "_scheduler_event_queue", None) is not None:
            return None
        socket_to_main = self.context.socket(zmq.DEALER)
        self._configure_scheduler_event_socket(socket_to_main)
        socket_to_main.connect(f"tcp://127.0.0.1:{port2}")
        return socket_to_main

    def _send_or_defer_scheduler_event(
        self,
        socket_to_main,
        message: Dict[str, Any],
        outbound_messages: list | None = None,
        on_sent=None,
    ) -> None:
        if outbound_messages is not None:
            outbound_messages.append((message, on_sent))
            return
        self._send_scheduler_event(socket_to_main, message)
        if on_sent is not None:
            on_sent()

    def _flush_scheduler_events(self, socket_to_main, outbound_messages: list) -> None:
        for message, on_sent in outbound_messages:
            self._send_scheduler_event(socket_to_main, message)
            if on_sent is not None:
                with self.lock:
                    on_sent()

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
            "dispatch_id": getattr(task, "dispatch_id", None),
            "lease_id": getattr(task, "lease_id", None),
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
            "dispatch_id": getattr(task, "dispatch_id", None),
            "lease_id": getattr(task, "lease_id", None),
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

    def _task_execution_resources(self, task: TaskRuntime|LanggraphTaskRuntime):
        resources = copy.deepcopy(task.scheduler_resources)
        if getattr(task, "model_route", None):
            resources["gpu"] = 0
            resources["gpu_mem"] = 0
        return resources

    def _release_model_route(self, task: TaskRuntime|LanggraphTaskRuntime):
        route = getattr(task, "model_route", None)
        if not route:
            return
        self.llm_instance_manager.release_model_route(route)
        task.model_route = None

    def _clear_model_workflow_state(self, workflow_id: str | None):
        clear = getattr(
            getattr(self, "llm_instance_manager", None),
            "clear_workflow_state",
            None,
        )
        if clear is not None:
            clear(workflow_id)

    def _finalize_stopped_llm_instance(
        self,
        instance_id: str,
        resource_detail: dict,
    ) -> None:
        with self.lock:
            self.resource_manager.release_instance_resource(resource_detail)
        self.llm_instance_manager.finalize_stopped_instance(instance_id)
        getattr(self, "_llm_instance_start_request_ids", {}).pop(instance_id, None)

    def _stop_and_finalize_llm_instance(self, instance_id: str) -> dict:
        resource_detail = self.llm_instance_manager.stop_llm_instance(
            instance_id=instance_id,
            finalize=False,
        )
        self._finalize_stopped_llm_instance(instance_id, resource_detail)
        return resource_detail

    def _rollback_failed_llm_launch(self, instance_id: str, lease_id: str) -> bool:
        instance_state = self.llm_instance_manager.get_instance_state(instance_id)
        if instance_state == "stopped":
            try:
                resource_detail = self.llm_instance_manager.get_instance_resource_detail(
                    instance_id
                )
            except KeyError:
                instance_state = None
            else:
                self._finalize_stopped_llm_instance(instance_id, resource_detail)
                return True
        if instance_state is None:
            with self.lock:
                self.resource_manager.release_lease(lease_id)
            return True
        logger.error(
            "Retaining lease %s for LLM instance %s in state %s",
            lease_id,
            instance_id,
            instance_state,
        )
        return False

    def _handle_llm_instance_stop(self, socket_to_main, message_data: dict) -> None:
        instance_id = message_data["instance_id"]
        try:
            resource_detail = self._stop_and_finalize_llm_instance(instance_id)
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
        self._send_scheduler_event(socket_to_main, message)

    def _record_llm_owner_node(self, node_id: str, node_ip: str) -> None:
        is_new_placement = self.llm_instance_manager.record_owner_node(
            node_id,
            node_ip,
        )
        if not is_new_placement:
            return
        owner_node_sender = getattr(self, "owner_node_sender", None)
        if owner_node_sender is not None:
            owner_node_sender.send({
                "node_id": str(node_id),
                "node_ip": str(node_ip),
            })

    def _send_llm_launch_failure(
        self,
        socket_to_main,
        message_data: dict,
        backend: str | None,
        error: BaseException | str,
    ) -> None:
        if message_data.get("auto_started"):
            logger.warning(
                "Automatic LLM instance %s launch failed: %s",
                message_data["instance_id"],
                error,
            )
            return
        message = {
            "type": "fail_llm_instance_launch",
            "data": {
                "instance_id": message_data["instance_id"],
                "backend": backend,
                "request_id": message_data.get("request_id"),
                "error": str(error),
            },
        }
        self._send_scheduler_event(socket_to_main, message)

    def _clear_auto_model_deploying(
        self,
        message_data: dict,
        backend: str | None,
    ) -> None:
        if not message_data.get("auto_started"):
            return
        clear_model_deploying = getattr(
            self.llm_instance_manager,
            "clear_model_deploying",
            None,
        )
        if clear_model_deploying is None:
            return
        normalized_backend = str(
            backend or message_data.get("backend") or "vllm"
        ).strip().lower()
        clear_model_deploying(
            message_data["model"],
            normalized_backend,
            instance_id=message_data.get("instance_id"),
        )

    def _execute_llm_start(self, record: dict) -> dict:
        message_data = record["message_data"]
        selection = record["selection"]
        backend = record["backend"]
        selected_node = selection.selected_node
        try:
            if record["cancel_event"].is_set():
                raise RuntimeError(
                    f"LLM instance {message_data['instance_id']} startup was cancelled"
                )
            remaining_startup = record["startup_deadline"] - time.monotonic()
            if remaining_startup <= 0:
                raise TimeoutError(
                    f"LLM instance {message_data['instance_id']} startup timed out "
                    "while waiting for a launch worker"
                )
            instance_info = self.llm_instance_manager.start_llm_instance(
                instance_id=message_data["instance_id"],
                model=message_data["model"],
                node_ip=selected_node.node_ip,
                node_id=selected_node.node_id,
                gpu_id=selected_node.gpu_id,
                resources=record["need_resources"],
                backend=backend,
                backend_args=record["backend_args"],
                lease_id=selection.lease_id,
                startup_timeout=remaining_startup,
                return_info=True,
            )
            if record["cancel_event"].is_set():
                self.llm_instance_manager.request_start_cancellation(
                    message_data["instance_id"]
                )
                self._stop_and_finalize_llm_instance(message_data["instance_id"])
                raise RuntimeError(
                    f"LLM instance {message_data['instance_id']} startup was cancelled"
                )
            return {"ok": True, "instance_info": instance_info, "backend": backend}
        except Exception as exc:
            rollback_complete = self._rollback_failed_llm_launch(
                message_data["instance_id"],
                selection.lease_id,
            )
            if rollback_complete:
                self._clear_auto_model_deploying(message_data, backend)
            return {
                "ok": False,
                "backend": backend,
                "error": str(exc),
                "cancelled": record["cancel_event"].is_set(),
                "rollback_complete": rollback_complete,
            }
        finally:
            clear_cancellation = getattr(
                self.llm_instance_manager,
                "clear_start_cancellation",
                None,
            )
            if clear_cancellation is not None:
                clear_cancellation(message_data["instance_id"])

    def _queue_llm_start(self, socket_to_main, message_data: dict) -> None:
        self._ensure_llm_async_state()
        backend = None
        try:
            backend, backend_args = validate_model_backend(
                message_data.get("backend"),
                message_data.get("backend_args"),
            )
            if backend == "transformers":
                validate_transformers_model(message_data["model"])
        except ValueError as exc:
            self._clear_auto_model_deploying(message_data, backend)
            self._send_llm_launch_failure(
                socket_to_main,
                message_data,
                backend or message_data.get("backend"),
                exc,
            )
            return

        instance_id = message_data["instance_id"]
        has_instance = getattr(
            self.llm_instance_manager,
            "has_instance",
            lambda _instance_id: False,
        )
        if (
            instance_id in self._llm_pending_starts
            or has_instance(instance_id)
        ):
            self._send_llm_launch_failure(
                socket_to_main,
                message_data,
                backend,
                f"LLM instance {instance_id} startup is already pending",
            )
            return

        need_resources = {
            "cpu": message_data.get("cpu_nums", 0),
            "cpu_mem": message_data.get("memory", 0),
            "gpu": message_data.get("gpu_nums", 0),
            "gpu_mem": message_data.get("gpu_mem", 0),
        }
        with self.lock:
            selection = self.resource_manager.select_node(
                task_need_resources=need_resources,
                reservation_kind="instance",
                run_id=instance_id,
            )
        if not selection:
            reason = selection.decision.get(
                "reason",
                "No node can launch the LLM instance",
            )
            self._clear_auto_model_deploying(message_data, backend)
            self._send_llm_launch_failure(
                socket_to_main,
                message_data,
                backend,
                reason,
            )
            logger.info("Failed to place LLM instance %s: %s", instance_id, reason)
            return

        selected_node = selection.selected_node
        self._record_llm_owner_node(selected_node.node_id, selected_node.node_ip)
        record = {
            "message_data": dict(message_data),
            "backend": backend,
            "backend_args": backend_args,
            "need_resources": need_resources,
            "selection": selection,
            "cancel_event": threading.Event(),
            "stop_requests": [],
            "stop_future": None,
            "startup_deadline": (
                time.monotonic()
                + float(message_data.get("startup_timeout", 300))
            ),
        }
        future = self._llm_start_executor.submit(self._execute_llm_start, record)
        record["future"] = future
        self._llm_pending_starts[instance_id] = record
        self._llm_start_futures[future] = record
        request_id = message_data.get("request_id")
        if request_id is not None:
            self._llm_instance_start_request_ids[instance_id] = request_id

    def _cancel_and_stop_pending_start(self, record: dict) -> dict:
        instance_id = record["message_data"]["instance_id"]
        deadline = max(
            time.monotonic() + 5.0,
            record["startup_deadline"] + 60.0,
        )
        while True:
            state = self.llm_instance_manager.get_instance_state(instance_id)
            if state is not None:
                try:
                    return self._stop_and_finalize_llm_instance(instance_id)
                except KeyError:
                    continue
            if record["future"].done():
                return {
                    "instance_id": instance_id,
                    "backend": record["backend"],
                    "lease_id": record["selection"].lease_id,
                }
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Timed out cancelling pending LLM instance {instance_id}"
                )
            time.sleep(0.01)

    def _queue_llm_stop(self, message_data: dict) -> None:
        self._ensure_llm_async_state()
        instance_id = message_data["instance_id"]
        start_request_id = message_data.get("start_request_id")
        pending = self._llm_pending_starts.get(instance_id)
        if pending is not None:
            if (
                start_request_id is not None
                and pending["message_data"].get("request_id") != start_request_id
            ):
                logger.warning(
                    "Ignoring stale cancellation for LLM instance %s start request %s",
                    instance_id,
                    start_request_id,
                )
                return
            pending["stop_requests"].append(dict(message_data))
            pending["cancel_event"].set()
            existing_state = self.llm_instance_manager.request_start_cancellation(
                instance_id
            )
            if pending["future"].cancel() and existing_state is None:
                return
            if pending["stop_future"] is None:
                future = self._llm_maintenance_executor.submit(
                    self._cancel_and_stop_pending_start,
                    pending,
                )
                pending["stop_future"] = future
                self._llm_control_stop_futures[future] = {
                    "requests": pending["stop_requests"],
                    "record": pending,
                }
            return

        if (
            start_request_id is not None
            and self._llm_instance_start_request_ids.get(instance_id)
            != start_request_id
        ):
            logger.warning(
                "Ignoring stale cancellation for LLM instance %s start request %s",
                instance_id,
                start_request_id,
            )
            return

        future = self._llm_maintenance_executor.submit(
            self._stop_and_finalize_llm_instance,
            instance_id,
        )
        self._llm_control_stop_futures[future] = {
            "requests": [dict(message_data)],
            "record": None,
        }

    def _send_llm_stop_completion(
        self,
        socket_to_main,
        message_data: dict,
        *,
        resource_detail: dict | None = None,
        error: BaseException | str | None = None,
    ) -> None:
        if error is None:
            message = {
                "type": "finish_llm_instance_stop",
                "data": {
                    "instance_id": message_data["instance_id"],
                    "backend": (resource_detail or {}).get("backend"),
                    "request_id": message_data.get("request_id"),
                },
            }
        else:
            message = {
                "type": "fail_llm_instance_stop",
                "data": {
                    "instance_id": message_data["instance_id"],
                    "request_id": message_data.get("request_id"),
                    "error": str(error),
                },
            }
        self._send_scheduler_event(socket_to_main, message)

    def _drain_llm_control_futures(self, socket_to_main) -> None:
        for future, record in list(self._llm_start_futures.items()):
            if not future.done():
                continue
            self._llm_start_futures.pop(future, None)
            instance_id = record["message_data"]["instance_id"]
            if self._llm_pending_starts.get(instance_id) is record:
                self._llm_pending_starts.pop(instance_id, None)
            try:
                result = future.result()
            except CancelledError:
                rollback_complete = self._rollback_failed_llm_launch(
                    instance_id,
                    record["selection"].lease_id,
                )
                if rollback_complete:
                    self._clear_auto_model_deploying(
                        record["message_data"],
                        record["backend"],
                    )
                clear_cancellation = getattr(
                    self.llm_instance_manager,
                    "clear_start_cancellation",
                    None,
                )
                if clear_cancellation is not None:
                    clear_cancellation(instance_id)
                result = {
                    "ok": False,
                    "backend": record["backend"],
                    "error": f"LLM instance {instance_id} startup was cancelled",
                    "rollback_complete": rollback_complete,
                }
            except Exception as exc:
                logger.exception(
                    "LLM instance %s background launch failed",
                    instance_id,
                )
                try:
                    rollback_complete = self._rollback_failed_llm_launch(
                        instance_id,
                        record["selection"].lease_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to roll back LLM instance %s after worker failure",
                        instance_id,
                    )
                else:
                    if rollback_complete:
                        self._clear_auto_model_deploying(
                            record["message_data"],
                            record["backend"],
                        )
                result = {
                    "ok": False,
                    "backend": record["backend"],
                    "error": str(exc),
                }
            if result["ok"]:
                if not record["message_data"].get("auto_started"):
                    response_data = dict(result["instance_info"])
                    request_id = record["message_data"].get("request_id")
                    if request_id is not None:
                        response_data["request_id"] = request_id
                    self._send_scheduler_event(socket_to_main, {
                        "type": "finish_llm_instance_launch",
                        "data": response_data,
                    })
            else:
                if (
                    self._llm_instance_start_request_ids.get(instance_id)
                    == record["message_data"].get("request_id")
                ):
                    self._llm_instance_start_request_ids.pop(instance_id, None)
                self._send_llm_launch_failure(
                    socket_to_main,
                    record["message_data"],
                    result.get("backend"),
                    result["error"],
                )

            if record["stop_requests"] and record["stop_future"] is None:
                detail = {
                    "backend": record["backend"],
                    "lease_id": record["selection"].lease_id,
                }
                for stop_request in record["stop_requests"]:
                    self._send_llm_stop_completion(
                        socket_to_main,
                        stop_request,
                        resource_detail=detail,
                        error=(
                            None
                            if result.get("rollback_complete", True)
                            else "LLM startup cleanup remains pending"
                        ),
                    )
                record["stop_requests"].clear()

        for future, entry in list(self._llm_control_stop_futures.items()):
            if not future.done():
                continue
            self._llm_control_stop_futures.pop(future, None)
            record = entry.get("record")
            if record is not None:
                record["stop_future"] = None
            try:
                detail = future.result()
                error = None
            except Exception as exc:
                detail = None
                error = exc
                logger.exception("Failed to stop LLM instance asynchronously")
            requests = list(entry["requests"])
            entry["requests"].clear()
            for stop_request in requests:
                self._send_llm_stop_completion(
                    socket_to_main,
                    stop_request,
                    resource_detail=detail,
                    error=error,
                )

    def _submit_runtime_llm_cleanup(self, candidate: dict) -> bool:
        instance_id = candidate["instance_id"]
        if instance_id in self._llm_runtime_cleanup_futures:
            return False
        future = self._llm_maintenance_executor.submit(
            self._stop_and_finalize_llm_instance,
            instance_id,
        )
        self._llm_runtime_cleanup_futures[instance_id] = (future, dict(candidate))
        return True

    def _drain_llm_maintenance_futures(self) -> None:
        probe_future = self._llm_runtime_probe_future
        if probe_future is not None and probe_future.done():
            self._llm_runtime_probe_future = None
            try:
                candidates = probe_future.result()
            except Exception:
                logger.exception("LLM runtime health probe failed")
            else:
                for candidate in candidates:
                    self._submit_runtime_llm_cleanup(candidate)

        for instance_id, item in list(self._llm_runtime_cleanup_futures.items()):
            future, candidate = item
            if not future.done():
                continue
            self._llm_runtime_cleanup_futures.pop(instance_id, None)
            try:
                future.result()
            except Exception:
                logger.exception(
                    "LLM instance %s cleanup remains pending after %s: %s",
                    instance_id,
                    candidate.get("state", "runtime failure"),
                    candidate.get("reason", "unknown error"),
                )

    def _manage_llm_instance_scaling(self, now: float | None = None):
        if self._shutdown_requested():
            return
        self._ensure_llm_async_state()
        self._drain_llm_maintenance_futures()
        now = now or time.time()
        if now - getattr(self, "last_llm_scaling_check", 0.0) < 5.0:
            return
        self.last_llm_scaling_check = now

        runtime_cleanup_candidates = getattr(
            self.llm_instance_manager,
            "runtime_cleanup_candidates",
            None,
        )
        if (
            runtime_cleanup_candidates is not None
            and self._llm_runtime_probe_future is None
        ):
            self._llm_runtime_probe_future = self._llm_maintenance_executor.submit(
                runtime_cleanup_candidates
            )

        for recommendation in self.llm_instance_manager.scale_out_recommendations():
            model = recommendation["model"]
            backend = recommendation["backend"]
            instance_id = str(uuid.uuid4())
            self.llm_instance_manager.mark_model_deploying(
                model,
                backend,
                instance_id=instance_id,
            )
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
        active_lease_counts_by_kind: Dict[str, int] = {}
        for lease in self.resource_manager.active_leases.values():
            kind = str(lease.get("reservation_kind") or "unknown")
            active_lease_counts_by_kind[kind] = active_lease_counts_by_kind.get(kind, 0) + 1
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
            "active_lease_count": len(self.resource_manager.active_leases),
            "active_lease_counts_by_kind": dict(sorted(active_lease_counts_by_kind.items())),
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

    def _send_task_exception(
        self,
        socket_to_main,
        task: TaskRuntime|LanggraphTaskRuntime,
        error: Dict[str, Any],
        outbound_messages: list | None = None,
    ):
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
                "dispatch_id": getattr(task, "dispatch_id", None),
                "lease_id": getattr(task, "lease_id", None),
                "schedule_decision": getattr(task, "last_schedule_decision", None),
                "scheduling": getattr(task, "scheduling_metadata", None),
                "fault_tolerance": trace_snapshot(task),
            },
        }
        if file_manifest and file_manifest.get("published") is True:
            message["data"]["file_manifest"] = file_manifest
        if metrics:
            message["data"]["metrics"] = metrics
        for field, value in (
            getattr(task, "last_attempt_timing", None) or {}
        ).items():
            if value is not None:
                message["data"][field] = value
        self._send_or_defer_scheduler_event(
            socket_to_main,
            message,
            outbound_messages,
        )

    def _send_task_rejected(
        self,
        socket_to_main,
        message_data: Dict[str, Any],
        exc: BaseException,
    ):
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
                "pre_dispatch": True,
                "attempt": 0,
                "dispatch_id": None,
                "lease_id": None,
                "resources": message_data.get("resources"),
                "schedule_decision": {
                    "reason": "task_rejected_by_scheduler",
                },
            },
        }
        self._send_scheduler_event(socket_to_main, message)

    def _terminate_rejected_workflow(self, workflow_id: str | None) -> None:
        if not workflow_id:
            return

        self._stopped_workflows().add(workflow_id)
        self._clear_model_workflow_state(workflow_id)
        running_before = [
            task
            for task in self.workflow_manager.get_running_tasks()
            if task.workflow_id == workflow_id
        ]
        cancelled_tasks = []
        try:
            cancelled_tasks = self.workflow_manager.cancel_workflow(workflow_id)
        except Exception:
            logger.exception(
                "Workflow %s cancellation failed after a pre-dispatch rejection; "
                "falling back to per-task cancellation",
                workflow_id,
            )
            for task in running_before:
                try:
                    ray.cancel(task.object_ref, force=True)
                except Exception:
                    logger.exception(
                        "Could not cancel task %s after rejecting workflow %s",
                        task.task_id,
                        workflow_id,
                    )
            self.workflow_manager.clear_workflow(workflow_id)

        tasks_by_identity = {
            id(task): task
            for task in [*running_before, *(cancelled_tasks or [])]
        }
        tasks = list(tasks_by_identity.values())
        for task in tasks:
            try:
                self._release_model_route(task)
            except Exception:
                logger.exception(
                    "Could not release the model route for rejected task %s",
                    task.task_id,
                )
        self.resource_manager.release_task_resource(tasks=tasks)
        self.workflow_manager.clear_workflow(workflow_id)
        self.resource_manager.release_dag_context(workflow_id)

    def _send_task_retry(
        self,
        socket_to_main,
        task: TaskRuntime|LanggraphTaskRuntime,
        error: Dict[str, Any],
        outbound_messages: list | None = None,
        on_sent=None,
    ):
        message = {
            "type": "task_retry",
            "data": {
                "workflow_id": task.workflow_id,
                "task_id": task.task_id,
                "task_kind": getattr(task, "task_kind", "cpu"),
                "queue_name": getattr(task, "queue_name", None),
                "error": error,
                "attempt": task.attempt,
                "dispatch_id": getattr(task, "dispatch_id", None),
                "lease_id": getattr(task, "lease_id", None),
                "next_attempt": task.attempt + 1,
                "retry_backoff_seconds": task.retry_backoff_seconds,
                "resources": task.resources,
                "schedule_decision": getattr(task, "last_schedule_decision", None),
                "scheduling": getattr(task, "scheduling_metadata", None),
                "fault_tolerance": trace_snapshot(task),
            },
        }
        metrics = getattr(task, "last_metrics", None)
        if metrics:
            message["data"]["metrics"] = metrics
        for field, value in (
            getattr(task, "last_attempt_timing", None) or {}
        ).items():
            if value is not None:
                message["data"][field] = value
        self._send_or_defer_scheduler_event(
            socket_to_main,
            message,
            outbound_messages,
            on_sent,
        )

    def _send_task_pending(
        self,
        socket_to_main,
        task: TaskRuntime|LanggraphTaskRuntime,
        outbound_messages: list | None = None,
    ):
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
        self._send_or_defer_scheduler_event(
            socket_to_main,
            message,
            outbound_messages,
        )

    def _retry_or_fail_task(
        self,
        socket_to_main,
        task: TaskRuntime|LanggraphTaskRuntime,
        error: Dict[str, Any],
        file_manifest: Dict[str, Any] | None = None,
        metrics: Dict[str, Any] | None = None,
        outbound_messages: list | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
        duration_ms: int | None = None,
    ):
        error = enrich_error_for_task(error, task)
        task.last_error = error
        task.last_metrics = metrics
        attempt_started_at = (
            started_at
            if started_at is not None
            else getattr(task, "started_time", None)
        )
        attempt_finished_at = finished_at if finished_at is not None else time.time()
        if duration_ms is None and attempt_started_at is not None:
            duration_ms = int(max(0.0, attempt_finished_at - attempt_started_at) * 1000)
        task.last_attempt_timing = {
            "started_at": attempt_started_at,
            "finished_at": attempt_finished_at,
            "duration_ms": duration_ms,
        }
        self._release_model_route(task)
        self._stopped_workflows().add(task.workflow_id)
        self._clear_model_workflow_state(task.workflow_id)
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
            def enqueue_retry():
                task.clear_attempt_identity()
                self.task_queues.put(task)

            self._send_task_retry(
                socket_to_main,
                task,
                error,
                outbound_messages,
                enqueue_retry,
            )
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
            for cancelled_task in canceld_tasks:
                self._release_model_route(cancelled_task)
            self.resource_manager.release_task_resource(tasks=canceld_tasks)
            self.workflow_manager.clear_workflow(task.workflow_id)
        self.resource_manager.release_dag_context(task.workflow_id)
        self._send_task_exception(
            socket_to_main,
            task,
            error,
            outbound_messages,
        )

    def _release_task_attempt_resource(self, task: TaskRuntime|LanggraphTaskRuntime, resources: Dict[str, Any]):
        self.resource_manager.release_lease(getattr(task, "lease_id", None))

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
            task.dispatch_published = False
        except Exception:
            self.resource_manager.release_lease(selection.lease_id)
            raise

    def _fail_timed_out_tasks(
        self,
        socket_to_main,
        outbound_messages: list | None = None,
    ):
        for task in list(self.workflow_manager.get_running_tasks()):
            if not getattr(task, "dispatch_published", True):
                continue
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
            self._retry_or_fail_task(
                socket_to_main,
                task,
                error,
                outbound_messages=outbound_messages,
            )

    def _signal_fatal(self) -> None:
        fatal_event = getattr(self, "fatal_event", None)
        if fatal_event is not None:
            fatal_event.set()

    def _cleanup(self, exit_code: int = 0):
        if exit_code:
            self._signal_fatal()
        self._request_shutdown()
        exit_lock = getattr(self, "_process_exit_lock", None)
        if exit_lock is None:
            exit_lock = threading.Lock()
            self._process_exit_lock = exit_lock

        with exit_lock:
            producers_stopped = self._join_scheduler_producers()
            self._stop_scheduler_event_sender()
            instance_cleanup_complete = False
            try:
                begin_shutdown = getattr(
                    self.llm_instance_manager,
                    "begin_shutdown",
                    None,
                )
                if begin_shutdown is not None:
                    begin_shutdown()
                executors_stopped = self._shutdown_llm_executors()
                if producers_stopped and executors_stopped:
                    stopped, cleanup_errors = (
                        self.llm_instance_manager.stop_all_llm_instances()
                    )
                else:
                    stopped = {}
                    cleanup_errors = {
                        "scheduler": (
                            "Scheduler producers or LLM control futures remained active"
                        )
                    }
                cleanup_errors = dict(cleanup_errors)
                for instance_id, resource_detail in stopped.items():
                    try:
                        self._finalize_stopped_llm_instance(
                            instance_id,
                            resource_detail,
                        )
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
                producers_stopped
                and instance_cleanup_complete
                and owner_sweep_complete
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

    def _stop_workflow_request(
        self,
        message_data: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        request_id = message_data.get("request_id")
        workflow_id = message_data["workflow_id"]
        try:
            with self.lock:
                self._stopped_workflows().add(workflow_id)
                self._clear_model_workflow_state(workflow_id)
                cleanup_states = self.__dict__.setdefault(
                    "_workflow_cleanup_states",
                    {},
                )
                cleanup_state = cleanup_states.get(workflow_id)
                if cleanup_state is None:
                    cleanup_state = {
                        "tasks": list(self.workflow_manager.cancel_workflow(
                            workflow_id=workflow_id
                        )),
                        "model_routes_released": False,
                        "resources_released": False,
                        "workflow_cleared": False,
                        "dag_released": False,
                    }
                    cleanup_states[workflow_id] = cleanup_state
                canceled_tasks = cleanup_state["tasks"]
                if not cleanup_state["model_routes_released"]:
                    for task in canceled_tasks:
                        self._release_model_route(task)
                    cleanup_state["model_routes_released"] = True
                if not cleanup_state["resources_released"]:
                    if canceled_tasks:
                        self.resource_manager.release_task_resource(
                            tasks=canceled_tasks
                        )
                    cleanup_state["resources_released"] = True
                if not cleanup_state["workflow_cleared"]:
                    if canceled_tasks:
                        self.workflow_manager.clear_workflow(
                            workflow_id=workflow_id
                        )
                    cleanup_state["workflow_cleared"] = True
                if not cleanup_state["dag_released"]:
                    self.resource_manager.release_dag_context(workflow_id)
                    cleanup_state["dag_released"] = True
                cleanup_states.pop(workflow_id, None)
        except Exception as exc:
            logger.exception("Failed to stop workflow %s", workflow_id)
            if not request_id:
                return None
            return {
                "type": "workflow_stopped",
                "data": {
                    "request_id": request_id,
                    "workflow_id": workflow_id,
                    "ok": False,
                    "error": str(exc),
                },
            }
        if not request_id:
            return None
        return {
            "type": "workflow_stopped",
            "data": {
                "request_id": request_id,
                "workflow_id": workflow_id,
                "ok": True,
            },
        }

    def _receive_thread(self,port1:int):
        logger.info(f"Receive start")
        assert(self.context is not None)
        socket_from_main = self.context.socket(zmq.ROUTER)
        socket_from_main.bind(f"tcp://127.0.0.1:{port1}")
        setsockopt = getattr(socket_from_main, "setsockopt", None)
        if setsockopt is not None:
            setsockopt(zmq.RCVTIMEO, 100)
        socket_to_main = self._thread_scheduler_event_socket(self.port2)

        try:
            while not self._shutdown_requested():
                try:
                    frames = socket_from_main.recv_multipart()
                except zmq.Again:
                    continue
                if self._shutdown_requested():
                    return
                assert(len(frames)==2)
                _, data = frames
                message = json.loads(data.decode('utf-8'))

                message_type = message["type"]
                message_data = message.get("data", {})
                if(message_type =="run_task"):
                    try:
                        workflow_id = message_data["workflow_id"]
                        with self.lock:
                            workflow_is_stopped = workflow_id in self._stopped_workflows()
                        if workflow_is_stopped:
                            logger.warning(
                                "Ignoring task %s for stopped workflow %s",
                                message_data.get("task_id"),
                                workflow_id,
                            )
                            continue
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
                        else:
                            raise ValueError(
                                f"Unsupported task_type: {message_data.get('task_type')!r}"
                            )
                    except Exception as exc:
                        logger.exception(
                            "Rejecting task %s before scheduling",
                            message_data.get("task_id"),
                        )
                        with self.lock:
                            self._terminate_rejected_workflow(
                                message_data.get("workflow_id")
                            )
                        self._send_task_rejected(socket_to_main, message_data, exc)
                elif(message_type =="clear_workflow" ):
                    with self.lock:
                        self._stopped_workflows().add(message_data["workflow_id"])
                        self._clear_model_workflow_state(message_data["workflow_id"])
                        self.workflow_manager.clear_workflow(workflow_id=message_data["workflow_id"])
                        self.resource_manager.release_dag_context(message_data["workflow_id"])
                elif(message_type =="stop_workflow" ):
                    response = self._stop_workflow_request(message_data)
                    if response is not None:
                        self._send_scheduler_event(socket_to_main, response)
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
                        self._send_scheduler_event(socket_to_main, response)
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
                    self._send_scheduler_event(socket_to_main, response)
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
                    self._send_scheduler_event(socket_to_main, response)
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
                    self._send_scheduler_event(socket_to_main, response)
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
        socket_to_main = self._thread_scheduler_event_socket(port2)
        self._ensure_llm_async_state()
        while not self._shutdown_requested():
            self._drain_llm_control_futures(socket_to_main)
            try:
                llm_instance_message = self.llm_instance_queue.get(
                    timeout=LLM_CONTROL_POLL_SECONDS
                )
            except queue.Empty:
                continue
            if self._shutdown_requested():
                return
            message_data = llm_instance_message.message_data
            if llm_instance_message.message_type == "start_llm_instance":
                self._queue_llm_start(socket_to_main, message_data)
            elif llm_instance_message.message_type == "stop_llm_instance":
                self._queue_llm_stop(message_data)

    def _submit_thread(self,port2:int):
        logger.info(f"Submit start")
        socket_to_main = self._thread_scheduler_event_socket(port2)

        while not self._shutdown_requested():
            if not self.task_queues.wait_for_task(timeout=0.5):
                continue
            if self._shutdown_requested():
                return
            dispatched_or_removed = False
            attempted_head = False
            now = time.time()
            outbound_messages = []

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
                    if self.cur_ready_task.workflow_id in self._stopped_workflows():
                        self.task_queues.pop_head(queue_name, self.cur_ready_task)
                        self.cur_ready_task = None
                        dispatched_or_removed = True
                        continue
                    self.cur_ready_task.set_task_status("ready")
                    model_route_decision = {}
                    model_anchor = getattr(self.cur_ready_task, "model_anchor", None)
                    requires_model_route = bool(
                        isinstance(model_anchor, dict)
                        and (model_anchor.get("local_model") or model_anchor.get("model"))
                    )
                    if requires_model_route and self._assign_model_route(
                        self.cur_ready_task,
                        model_route_decision,
                    ) is None:
                        previous_pending_reason = self.cur_ready_task.pending_reason
                        self.cur_ready_task.set_task_status("pending")
                        self.cur_ready_task.pending_reason = "model_instance_unavailable"
                        self.cur_ready_task.last_schedule_decision = self._public_schedule_decision(
                            self.cur_ready_task,
                            {
                                "selected": False,
                                "reason": self.cur_ready_task.pending_reason,
                                "requested_resources": copy.deepcopy(
                                    self.cur_ready_task.scheduler_resources
                                ),
                            },
                        )
                        if previous_pending_reason != self.cur_ready_task.pending_reason:
                            self._send_task_pending(
                                socket_to_main,
                                self.cur_ready_task,
                                outbound_messages,
                            )
                        continue
                    if not self.workflow_manager.add_task(self.cur_ready_task):
                        self._release_model_route(self.cur_ready_task)
                        logger.debug(
                            "Ignoring duplicate task dispatch for workflow %s, task %s",
                            self.cur_ready_task.workflow_id,
                            self.cur_ready_task.task_id,
                        )
                        self.task_queues.pop_head(queue_name, self.cur_ready_task)
                        self.cur_ready_task = None
                        dispatched_or_removed = True
                        continue

                    #Get the node can run the task
                    dispatch_id = str(uuid.uuid4())
                    selection = self.resource_manager.select_node(
                        task_need_resources=self._task_execution_resources(
                            self.cur_ready_task
                        ),
                        model_anchor=(
                            None
                            if getattr(self.cur_ready_task, "model_route", None)
                            else getattr(self.cur_ready_task, "model_anchor", None)
                        ),
                        workflow_id=getattr(self.cur_ready_task, "workflow_id", None),
                        run_id=self.cur_ready_task.workflow_id,
                        task_id=self.cur_ready_task.task_id,
                        attempt=self.cur_ready_task.attempt + 1,
                        dispatch_id=dispatch_id,
                    )
                    if selection:
                        selected_node = selection.selected_node
                        self.task_queues.pop_head(queue_name, self.cur_ready_task)
                        self.cur_ready_task.pending_reason = None
                        selection.decision.update(model_route_decision)
                        selection.decision = self._public_schedule_decision(self.cur_ready_task, selection.decision)
                        self.cur_ready_task.last_schedule_decision = selection.decision
                        #Run task
                        try:
                            self._run_task_with_lease(
                                self.cur_ready_task,
                                selection,
                                dispatch_id,
                            )
                        except Exception as exc:
                            error = exception_to_error_envelope(
                                "scheduler_error",
                                exc,
                                origin="scheduler",
                                attempt=self.cur_ready_task.attempt,
                            )
                            self._retry_or_fail_task(
                                socket_to_main,
                                self.cur_ready_task,
                                error,
                                outbound_messages=outbound_messages,
                            )
                            self.cur_ready_task = None
                            dispatched_or_removed = True
                            continue

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
                                "dispatch_id":self.cur_ready_task.dispatch_id,
                                "lease_id":self.cur_ready_task.lease_id,
                                "schedule_decision":selection.decision,
                                "scheduling": getattr(self.cur_ready_task, "scheduling_metadata", None),
                                "started_at": time.time(),
                            }
                        }
                        dispatched_task = self.cur_ready_task
                        self._send_or_defer_scheduler_event(
                            socket_to_main,
                            message,
                            outbound_messages,
                            lambda task=dispatched_task: setattr(
                                task,
                                "dispatch_published",
                                True,
                            ),
                        )

                        self.cur_ready_task = None
                        dispatched_or_removed = True
                    else:
                        self._release_model_route(self.cur_ready_task)
                        previous_pending_reason = self.cur_ready_task.pending_reason
                        pending_reason = selection.decision.get("reason")
                        self.cur_ready_task.set_task_status("pending")
                        self.cur_ready_task.pending_reason = pending_reason
                        selection.decision = self._public_schedule_decision(self.cur_ready_task, selection.decision)
                        self.cur_ready_task.last_schedule_decision = selection.decision
                        if previous_pending_reason != self.cur_ready_task.pending_reason:
                            self._send_task_pending(
                                socket_to_main,
                                self.cur_ready_task,
                                outbound_messages,
                            )
                        logger.debug("No node can run task %s: %s", self.cur_ready_task.task_id, self.cur_ready_task.pending_reason)
                finally:
                    self.lock.release()

            self._flush_scheduler_events(socket_to_main, outbound_messages)
            if not dispatched_or_removed and attempted_head:
                time.sleep(0.1)
            elif not dispatched_or_removed:
                time.sleep(0.05)

    def _supervisor_thread(self, port2:int):
        logger.info(f"Supervisor start")
        socket_to_main = self._thread_scheduler_event_socket(port2)

        while not self._shutdown_requested():
            sleep_seconds = 0
            outbound_messages = []
            self._manage_llm_instance_scaling()
            with self.lock:
                self.resource_manager.check_dead_node()
                self.resource_manager.show_all_node_resource()
                self._manage_standby_workers()
                self._fail_timed_out_tasks(
                    socket_to_main,
                    outbound_messages,
                )

                running_task_refs:List = [
                    task.object_ref
                    for task in self.workflow_manager.get_running_tasks()
                    if getattr(task, "dispatch_published", True)
                    and task.object_ref is not None
                ]
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
                            self._retry_or_fail_task(
                                socket_to_main,
                                finished_task,
                                error,
                                file_manifest,
                                metrics,
                                outbound_messages,
                                started_at=raw_result.get("started_at"),
                                finished_at=raw_result.get("finished_at"),
                                duration_ms=raw_result.get("duration_ms"),
                            )
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

                        if isinstance(finished_task, LanggraphTaskRuntime):
                            self.workflow_manager.clear_workflow(
                                finished_task.workflow_id
                            )
                            self.resource_manager.release_dag_context(
                                finished_task.workflow_id
                            )

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
                            "dispatch_id": finished_task.dispatch_id,
                            "lease_id": finished_task.lease_id,
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
                        self._send_or_defer_scheduler_event(
                            socket_to_main,
                            message,
                            outbound_messages,
                        )

                    except ray.exceptions.RayTaskError as e:
                        logger.info(f"Task {finished_task.task_id} failed with exception: {e}")
                        error = exception_to_error_envelope(
                            "resource_insufficient" if looks_like_oom(e) else "user_code",
                            e,
                            origin="scheduler",
                            attempt=finished_task.attempt,
                        )
                        self._retry_or_fail_task(
                            socket_to_main,
                            finished_task,
                            error,
                            outbound_messages=outbound_messages,
                        )
                    except ray.exceptions.TaskCancelledError as e:
                        logger.info(f"Task {finished_task.task_id} failed with exception: {e}")
                        error = exception_to_error_envelope(
                            "cancelled",
                            e,
                            origin="scheduler",
                            attempt=finished_task.attempt,
                        )
                        self._retry_or_fail_task(
                            socket_to_main,
                            finished_task,
                            error,
                            outbound_messages=outbound_messages,
                        )
                    except (ray.exceptions.NodeDiedError, ray.exceptions.ObjectLostError, ray.exceptions.TaskUnschedulableError) as e:
                        logger.info(f"Task {finished_task.task_id} failed with exception: {e}")
                        error_type = "resource_unavailable" if isinstance(e, ray.exceptions.TaskUnschedulableError) else "node_lost"
                        error = exception_to_error_envelope(
                            error_type,
                            e,
                            origin="scheduler",
                            attempt=finished_task.attempt,
                        )
                        self._retry_or_fail_task(
                            socket_to_main,
                            finished_task,
                            error,
                            outbound_messages=outbound_messages,
                        )
                    except Exception as e:
                        logger.error(f"Task {finished_task.task_id} failed with exception: {e}")
                        error = exception_to_error_envelope(
                            "unknown",
                            e,
                            origin="scheduler",
                            attempt=finished_task.attempt,
                        )
                        self._retry_or_fail_task(
                            socket_to_main,
                            finished_task,
                            error,
                            outbound_messages=outbound_messages,
                        )
            self._flush_scheduler_events(socket_to_main, outbound_messages)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

    def _launch_ray_head(self):
        command = build_ray_command(
            "start",
            "--head",
            "--port",
            str(self.ray_head_port),
        )
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
                        logger.exception(
                            "Failed to clean stale Ray runtime before retry"
                        )

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
            if self._shutdown_requested():
                logger.debug(
                    "Scheduler thread %s exited during shutdown: %s",
                    name,
                    exc,
                )
                return
            self._signal_fatal()
            logger.critical(
                "Critical scheduler thread %s failed",
                name,
                exc_info=True,
            )
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
        self._start_scheduler_event_sender(self.port2)

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
