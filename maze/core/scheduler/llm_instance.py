from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import hashlib
import logging
import os
from pathlib import Path
import queue
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import ray
import requests

try:
    import fcntl
except ImportError:  # pragma: no cover - owner cleanup requires POSIX primitives
    fcntl = None

logger = logging.getLogger(__name__)

LLM_INSTANCE_ENV_VAR = "MAZE_LLM_INSTANCE_ID"
LLM_GENERATION_ENV_VAR = "MAZE_LLM_GENERATION_ID"
LLM_OWNER_ENV_VAR = "MAZE_LLM_OWNER_ID"
LLM_OWNER_LOCK_FD_ENV_VAR = "MAZE_LLM_OWNER_LOCK_FD"
LLM_PROCESS_STOP_TIMEOUT = 15
LLM_ACTOR_STOP_TIMEOUT = 25
LLM_CLEANUP_TASK_TIMEOUT = LLM_PROCESS_STOP_TIMEOUT + 7
LLM_STOP_TOTAL_TIMEOUT = LLM_ACTOR_STOP_TIMEOUT + LLM_CLEANUP_TASK_TIMEOUT + 5
LLM_OWNER_CLEANUP_GRACE_SECONDS = 7
LLM_RUNTIME_PROBE_TIMEOUT = 2
LLM_RUNTIME_HEALTH_TIMEOUT = 2
LLM_ACTOR_CREATION_WORKERS = 2
LLM_ACTOR_CREATION_QUEUE_SIZE = 2
LLM_LATE_ACTOR_KILL_WORKERS = 2
LLM_LATE_ACTOR_KILL_QUEUE_SIZE = 8
LLM_LATE_ACTOR_KILL_RETRY_SECONDS = 0.1
LLM_STALE_ACTOR_CLEANUP_SLOTS = LLM_LATE_ACTOR_KILL_QUEUE_SIZE
LLM_RAY_CONTROL_WORKERS = 4
LLM_RAY_CONTROL_QUEUE_SIZE = 16
LLM_STOP_ALL_WORKERS = 4
LLM_PORT_RESERVATION_START = 8000
SUPPORTED_MODEL_BACKENDS = ("vllm", "transformers")
_LLM_LAUNCHER_CODE = (
    "import os,sys;"
    f"os.close(int(os.environ.pop({LLM_OWNER_LOCK_FD_ENV_VAR!r})));"
    "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
)


class _BoundedDaemonExecutor:
    """A fixed daemon worker pool for calls that may never return."""

    def __init__(
        self,
        max_workers: int,
        max_pending: int,
        thread_name_prefix: str,
        *,
        max_abandoned: int = 0,
    ):
        self._tasks = queue.Queue(maxsize=max_pending)
        self._thread_name_prefix = thread_name_prefix
        self._max_abandoned = max(0, int(max_abandoned))
        self._abandoned_futures: set[Future] = set()
        self._state_lock = threading.Lock()
        self._next_worker_id = 0
        self._retired_workers = queue.Queue(maxsize=max(1, self._max_abandoned))
        if self._max_abandoned:
            threading.Thread(
                target=self._reap_retired_workers,
                name=f"retire-{thread_name_prefix}",
                daemon=True,
            ).start()
        for index in range(max_workers):
            self._start_worker()

    def _start_worker(self) -> None:
        worker_id = self._next_worker_id
        self._next_worker_id += 1
        threading.Thread(
            target=self._run,
            name=f"{self._thread_name_prefix}-{worker_id}",
            daemon=True,
        ).start()

    def submit(self, function, *args, **kwargs) -> Future:
        future = Future()
        try:
            self._tasks.put_nowait((future, function, args, kwargs))
        except queue.Full as exc:
            raise RuntimeError("bounded background worker queue is full") from exc
        return future

    def abandon(self, future: Future) -> bool:
        """Replace one stuck running task while keeping the total thread count bounded."""
        with self._state_lock:
            if future.done() or not future.running():
                return False
            if future in self._abandoned_futures:
                return True
            if len(self._abandoned_futures) >= self._max_abandoned:
                return False
            self._abandoned_futures.add(future)
            try:
                self._start_worker()
            except BaseException:
                self._abandoned_futures.discard(future)
                raise
            return True

    def _reap_retired_workers(self) -> None:
        while True:
            worker, future = self._retired_workers.get()
            try:
                worker.join()
                with self._state_lock:
                    self._abandoned_futures.discard(future)
            finally:
                self._retired_workers.task_done()

    def _run(self) -> None:
        while True:
            future, function, args, kwargs = self._tasks.get()
            retire = False
            try:
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = function(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                self._tasks.task_done()
                with self._state_lock:
                    if future in self._abandoned_futures:
                        retire = True
            if retire:
                self._retired_workers.put((threading.current_thread(), future))
                return


class _BoundedCleanupSlots:
    def __init__(self, capacity: int):
        self.capacity = max(1, int(capacity))
        self._semaphore = threading.BoundedSemaphore(self.capacity)
        self._state_lock = threading.Lock()
        self._in_use = 0

    def acquire(self) -> bool:
        if not self._semaphore.acquire(blocking=False):
            return False
        with self._state_lock:
            self._in_use += 1
        return True

    def release(self) -> None:
        self._semaphore.release()
        with self._state_lock:
            self._in_use -= 1

    @property
    def available(self) -> int:
        with self._state_lock:
            return self.capacity - self._in_use


class _PendingActorStart:
    def __init__(self, deadline: float):
        self.deadline = deadline
        self.generation_id = uuid.uuid4().hex
        self.future: Future | None = None
        self.wake_event = threading.Event()
        self.cancel_error: Exception | None = None
        self.actor_creation_submitted = False
        self.actor_delivered = False
        self.late_actor_kill_claimed = False
        self.stale_actor = None
        self._cleanup_slot_lock = threading.Lock()
        self._cleanup_slot_pool: _BoundedCleanupSlots | None = None
        self._cleanup_slot_held = False

    def reserve_cleanup_slot(self, pool: _BoundedCleanupSlots) -> bool:
        with self._cleanup_slot_lock:
            if self._cleanup_slot_held:
                return True
            if not pool.acquire():
                return False
            self._cleanup_slot_pool = pool
            self._cleanup_slot_held = True
            return True

    def release_cleanup_slot(self) -> bool:
        with self._cleanup_slot_lock:
            if not self._cleanup_slot_held:
                return False
            pool = self._cleanup_slot_pool
            self._cleanup_slot_pool = None
            self._cleanup_slot_held = False
        pool.release()
        return True


def _new_background_workers():
    return (
        _BoundedDaemonExecutor(
            LLM_ACTOR_CREATION_WORKERS,
            LLM_ACTOR_CREATION_QUEUE_SIZE,
            "maze-llm-actor-create",
            max_abandoned=LLM_ACTOR_CREATION_WORKERS,
        ),
        _BoundedDaemonExecutor(
            LLM_LATE_ACTOR_KILL_WORKERS,
            LLM_LATE_ACTOR_KILL_QUEUE_SIZE,
            "maze-llm-late-actor-kill",
        ),
        _BoundedDaemonExecutor(
            LLM_RAY_CONTROL_WORKERS,
            LLM_RAY_CONTROL_QUEUE_SIZE,
            "maze-llm-ray-control",
            max_abandoned=LLM_RAY_CONTROL_WORKERS,
        ),
        _BoundedCleanupSlots(LLM_STALE_ACTOR_CLEANUP_SLOTS),
    )


(
    _ACTOR_CREATION_EXECUTOR,
    _LATE_ACTOR_KILL_EXECUTOR,
    _RAY_CONTROL_EXECUTOR,
    _STALE_ACTOR_CLEANUP_SLOTS,
) = _new_background_workers()
_BACKGROUND_WORKER_PID = os.getpid()


def _ensure_background_workers_for_process() -> None:
    global _ACTOR_CREATION_EXECUTOR
    global _LATE_ACTOR_KILL_EXECUTOR
    global _RAY_CONTROL_EXECUTOR
    global _STALE_ACTOR_CLEANUP_SLOTS
    global _BACKGROUND_WORKER_PID

    current_pid = os.getpid()
    if current_pid == _BACKGROUND_WORKER_PID:
        return
    (
        _ACTOR_CREATION_EXECUTOR,
        _LATE_ACTOR_KILL_EXECUTOR,
        _RAY_CONTROL_EXECUTOR,
        _STALE_ACTOR_CLEANUP_SLOTS,
    ) = _new_background_workers()
    _BACKGROUND_WORKER_PID = current_pid


def _run_control_before_deadline(function, deadline: float, operation: str):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{operation} exceeded its deadline")
    future = _RAY_CONTROL_EXECUTOR.submit(function)
    try:
        return future.result(timeout=max(0.0, deadline - time.monotonic()))
    except TimeoutError as exc:
        if future.done():
            raise
        if not future.cancel():
            try:
                _RAY_CONTROL_EXECUTOR.abandon(future)
            except BaseException:
                logger.exception("Failed to retire timed-out %s worker", operation)
        raise TimeoutError(f"{operation} exceeded its deadline") from exc


def _ray_get_before_deadline(ref, deadline: float, timeout: float, operation: str):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{operation} exceeded its deadline")
    return ray.get(ref, timeout=min(max(0.0, float(timeout)), remaining))


def _confirm_actor_terminated(actor, deadline: float, operation: str) -> None:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{operation} was not confirmed before its deadline")
        try:
            ready_ref = actor.__ray_ready__.remote()
        except ray.exceptions.RayActorError:
            return
        except Exception as exc:
            raise RuntimeError(f"{operation} could not submit a death probe") from exc
        try:
            _ray_get_before_deadline(
                ready_ref,
                deadline,
                min(0.2, remaining),
                f"{operation} death probe",
            )
        except ray.exceptions.RayActorError:
            return
        except ray.exceptions.GetTimeoutError:
            continue
        except Exception as exc:
            raise RuntimeError(f"{operation} death probe failed") from exc
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def _port_reservation_root() -> Path:
    root = Path(tempfile.gettempdir()) / "maze-llm-port-reservations"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _release_port_reservation(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _reserve_llm_port(
    start_port: int = LLM_PORT_RESERVATION_START,
    reservation_root: str | os.PathLike[str] | None = None,
) -> tuple[str, int | None]:
    start_port = max(1, int(start_port))
    if fcntl is None or os.name != "posix":  # pragma: no cover - Linux runtime
        for port in range(start_port, 65536):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind(("0.0.0.0", port))
                except OSError:
                    continue
                return str(port), None
        raise RuntimeError(f"No free port found in range {start_port}-65535")

    root = Path(reservation_root) if reservation_root else _port_reservation_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for port in range(start_port, 65536):
        descriptor = os.open(root / f"{port}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("0.0.0.0", port))
        except OSError:
            _release_port_reservation(descriptor)
            continue
        return str(port), descriptor
    raise RuntimeError(f"No free port found in range {start_port}-65535")


def validate_model_backend(
    backend: str | None,
    backend_args: dict | None = None,
) -> tuple[str, dict]:
    if backend is None:
        backend = "vllm"
    if not isinstance(backend, str):
        raise ValueError("backend must be a string")
    backend = backend.strip().lower()
    if backend not in SUPPORTED_MODEL_BACKENDS:
        supported = ", ".join(SUPPORTED_MODEL_BACKENDS)
        raise ValueError(
            f"Unsupported model backend {backend!r}; expected one of: {supported}"
        )

    backend_args = dict(backend_args or {})
    if backend == "transformers" and backend_args:
        unsupported = ", ".join(sorted(backend_args))
        raise ValueError(
            f"Transformers backend does not support vLLM arguments: {unsupported}"
        )
    return backend, backend_args


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(
    process_group_id: int,
    timeout: float,
    proc=None,
) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if proc is not None:
            proc.poll()
        if not _process_group_exists(process_group_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _stop_subprocess(proc, process_group_id: int | None, timeout: float) -> None:
    if process_group_id is None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return

    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if _wait_for_process_group_exit(process_group_id, timeout, proc):
        return

    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if not _wait_for_process_group_exit(
        process_group_id,
        min(max(0.0, float(timeout)), 5),
        proc,
    ):
        raise RuntimeError(f"Process group {process_group_id} did not stop")


def build_model_env(
    gpu_id: str | None,
    instance_id: str | None = None,
    owner_id: str | None = None,
    generation_id: str | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    environment_bin = os.path.dirname(sys.executable)
    current_path = env.get("PATH")
    env["PATH"] = (
        environment_bin
        if not current_path
        else os.pathsep.join((environment_bin, current_path))
    )
    if gpu_id is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    if instance_id is not None:
        env[LLM_INSTANCE_ENV_VAR] = instance_id
    if generation_id is not None:
        env[LLM_GENERATION_ENV_VAR] = generation_id
    if owner_id is not None:
        env[LLM_OWNER_ENV_VAR] = owner_id
    return env


def _owner_state_path(owner_id: str) -> Path:
    owner_key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
    state_root = Path(tempfile.gettempdir()) / "maze-llm-owner-state"
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return state_root / owner_key


@contextmanager
def _locked_owner_state(owner_id: str, timeout: float | None = None):
    state_path = _owner_state_path(owner_id)
    descriptor = os.open(state_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+", encoding="ascii") as state_file:
        if fcntl is not None:
            if timeout is None:
                fcntl.flock(state_file.fileno(), fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + max(0.0, float(timeout))
                while True:
                    try:
                        fcntl.flock(
                            state_file.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                        break
                    except BlockingIOError as exc:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                f"Timed out closing launches for LLM owner {owner_id}"
                            ) from exc
                        time.sleep(min(0.05, remaining))
        yield state_file


@contextmanager
def _owner_launch_guard(owner_id: str | None):
    if owner_id is None:
        yield None
        return

    with _locked_owner_state(owner_id) as state_file:
        state_file.seek(0)
        if state_file.read().strip():
            raise RuntimeError(
                f"LLM owner {owner_id} cleanup has started; refusing a late model launch"
            )
        yield state_file


def _launch_model_subprocess(command: list[str], env: dict, owner_state_file=None):
    popen_command = command
    popen_env = env
    popen_kwargs = {}
    if owner_state_file is not None and fcntl is not None and os.name == "posix":
        lock_fd = owner_state_file.fileno()
        popen_command = [sys.executable, "-c", _LLM_LAUNCHER_CODE, *command]
        popen_env = dict(env)
        popen_env[LLM_OWNER_LOCK_FD_ENV_VAR] = str(lock_fd)
        popen_kwargs["pass_fds"] = (lock_fd,)
    return subprocess.Popen(
        popen_command,
        env=popen_env,
        start_new_session=os.name == "posix",
        **popen_kwargs,
    )


def _close_owner_launches_locally(
    owner_id: str,
    timeout: float = LLM_PROCESS_STOP_TIMEOUT,
) -> None:
    if fcntl is None or os.name != "posix" or not os.path.isdir("/proc"):
        raise RuntimeError(
            "Scheduler owner process cleanup requires POSIX flock and /proc"
        )
    with _locked_owner_state(owner_id, timeout=timeout) as state_file:
        state_file.seek(0)
        state_file.truncate()
        state_file.write("closed\n")
        state_file.flush()
        os.fsync(state_file.fileno())


def build_vllm_command(
    model: str,
    host: str,
    port: str,
    extra_args: dict | None = None,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        model,
        "--host",
        host,
        "--port",
        port,
    ]
    for key, value in (extra_args or {}).items():
        if value is None or value is False:
            continue
        command.append(f"--{key.replace('_', '-')}")
        if value is not True:
            command.append(str(value))
    return command


def build_transformers_command(model: str, host: str, port: str) -> list[str]:
    return [
        os.path.join(os.path.dirname(sys.executable), "transformers"),
        "serve",
        model,
        "--host",
        host,
        "--port",
        port,
        "--device",
        "cuda:0",
        "--dtype",
        "auto",
    ]


def _transformers_cache_root(instance_id: str) -> Path:
    cache_key = hashlib.sha256(instance_id.encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / f"maze-transformers-{cache_key}"


def validate_transformers_model(model: str) -> str | None:
    raw_model_path = Path(model)
    model_path = raw_model_path.expanduser()
    if not model_path.is_dir():
        return None
    if model_path != raw_model_path:
        raise ValueError(
            f"Transformers local model paths must be expanded; use {str(model_path)!r}"
        )
    if "@" in model:
        raise ValueError("Transformers local model paths containing '@' are unsupported")

    repo_name = "models--" + model.replace("\\", "--").replace("/", "--")
    decoded_model = repo_name.removeprefix("models--").replace("--", "/")
    if decoded_model != model:
        raise ValueError(
            "Transformers cannot expose this local model path losslessly through "
            "/v1/models; use a symlink path without '--' or backslashes"
        )

    try:
        name_max = os.pathconf(tempfile.gettempdir(), "PC_NAME_MAX")
    except (AttributeError, OSError, ValueError):
        name_max = 255
    if len(os.fsencode(repo_name)) > name_max:
        raise ValueError(
            "Transformers local model path is too long to expose through /v1/models; "
            "use a shorter symlink path"
        )
    return repo_name


def prepare_transformers_cache(model: str, instance_id: str) -> str | None:
    repo_name = validate_transformers_model(model)
    if repo_name is None:
        return None

    model_path = Path(model)
    cache_root = _transformers_cache_root(instance_id)
    cleanup_transformers_cache(instance_id)
    cache_dir = cache_root / "hub"
    repo_dir = cache_dir / repo_name
    refs_dir = repo_dir / "refs"
    snapshots_dir = repo_dir / "snapshots"
    refs_dir.mkdir(parents=True)
    snapshots_dir.mkdir()

    resolved_model = model_path.resolve()
    revision = hashlib.sha1(str(resolved_model).encode("utf-8")).hexdigest()
    (refs_dir / "main").write_text(revision, encoding="utf-8")
    revision_dir = snapshots_dir / revision
    revision_dir.mkdir()
    (revision_dir / "config.json").symlink_to(resolved_model / "config.json")
    return str(cache_dir)


def cleanup_transformers_cache(instance_id: str) -> None:
    cache_root = _transformers_cache_root(instance_id)
    try:
        shutil.rmtree(cache_root)
    except FileNotFoundError:
        if os.path.lexists(cache_root):
            raise


def _marked_process_groups(environment_variable: str, marker_value: str) -> set[int]:
    if os.name != "posix" or not os.path.isdir("/proc"):
        return set()

    marker = f"{environment_variable}={marker_value}".encode("utf-8")
    process_groups = set()
    own_process_group = os.getpgrp()
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(os.path.join(entry.path, "environ"), "rb") as environ_file:
                    environ = environ_file.read().split(b"\0")
                if marker not in environ:
                    continue
                process_group_id = os.getpgid(int(entry.name))
                if process_group_id > 0 and process_group_id != own_process_group:
                    process_groups.add(process_group_id)
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
    return process_groups


def _instance_process_groups(instance_id: str) -> set[int]:
    return _marked_process_groups(LLM_INSTANCE_ENV_VAR, instance_id)


def _generation_process_groups(generation_id: str) -> set[int]:
    return _marked_process_groups(LLM_GENERATION_ENV_VAR, generation_id)


def _owner_process_groups(owner_id: str) -> set[int]:
    return _marked_process_groups(LLM_OWNER_ENV_VAR, owner_id)


def _stop_marked_process_groups(
    process_groups,
    timeout: float,
    settle_timeout: float,
) -> set[int]:
    started_at = time.monotonic()
    timeout = max(0.0, float(timeout))
    deadline = started_at + timeout
    kill_budget = min(5.0, timeout / 3)
    terminate_deadline = deadline - kill_budget
    settle_deadline = min(
        started_at + max(0.0, float(settle_timeout)),
        terminate_deadline,
    )
    stopped_groups = set()

    while True:
        current_groups = process_groups()
        now = time.monotonic()
        if current_groups:
            settle_deadline = min(
                now + max(0.0, float(settle_timeout)),
                terminate_deadline,
            )
            for process_group_id in current_groups:
                try:
                    os.killpg(process_group_id, signal.SIGTERM)
                except ProcessLookupError:
                    continue
                stopped_groups.add(process_group_id)
        elif now >= settle_deadline:
            return stopped_groups
        if now >= terminate_deadline:
            break
        time.sleep(min(0.05, terminate_deadline - now))

    remaining_groups = process_groups()
    for process_group_id in remaining_groups:
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            continue
        stopped_groups.add(process_group_id)

    while process_groups():
        now = time.monotonic()
        if now >= deadline:
            break
        time.sleep(min(0.05, deadline - now))
    remaining_groups = process_groups()
    if remaining_groups:
        raise RuntimeError(f"Process groups did not stop: {sorted(remaining_groups)}")
    return stopped_groups


@ray.remote(num_cpus=0)
def stop_llm_instance_processes(
    instance_id: str,
    timeout: float = LLM_PROCESS_STOP_TIMEOUT,
    settle_timeout: float = 0.5,
    generation_id: str | None = None,
):
    cleanup_key = generation_id or instance_id
    stopped_groups = _stop_marked_process_groups(
        lambda: (
            _generation_process_groups(generation_id)
            if generation_id is not None
            else _instance_process_groups(instance_id)
        ),
        timeout,
        settle_timeout,
    )
    cleanup_transformers_cache(cleanup_key)
    return {"stopped_process_groups": sorted(stopped_groups)}


def stop_llm_owner_processes_locally(
    owner_id: str,
    timeout: float = LLM_PROCESS_STOP_TIMEOUT,
    settle_timeout: float = 0.5,
):
    if not owner_id:
        raise ValueError("LLM owner id must be non-empty")
    deadline = time.monotonic() + max(0.0, float(timeout))
    _close_owner_launches_locally(owner_id, timeout=timeout)
    remaining = max(0.0, deadline - time.monotonic())
    stopped_groups = _stop_marked_process_groups(
        lambda: _owner_process_groups(owner_id),
        remaining,
        min(settle_timeout, remaining),
    )
    return {"stopped_process_groups": sorted(stopped_groups)}


@ray.remote(num_cpus=0)
def stop_llm_owner_processes(
    owner_id: str,
    timeout: float = LLM_PROCESS_STOP_TIMEOUT,
    settle_timeout: float = 0.5,
):
    return stop_llm_owner_processes_locally(owner_id, timeout, settle_timeout)


def _stop_llm_owner_processes_on_cluster_control(
    owner_id: str,
    timeout: float = LLM_PROCESS_STOP_TIMEOUT,
    expected_nodes: dict[str, str] | None = None,
    deadline: float | None = None,
):
    if deadline is None:
        deadline = (
            time.monotonic()
            + max(0.0, float(timeout))
            + LLM_OWNER_CLEANUP_GRACE_SECONDS
        )
    if time.monotonic() >= deadline:
        raise TimeoutError("LLM owner process cleanup exceeded its deadline")
    alive_nodes = {
        str(node["NodeID"]): node
        for node in ray.nodes()
        if node.get("Alive") and node.get("NodeID")
    }
    if expected_nodes:
        alive_by_ip = {
            str(node.get("NodeManagerAddress")): node_id
            for node_id, node in alive_nodes.items()
            if node.get("NodeManagerAddress")
        }
        unavailable = []
        for expected_node_id, expected_node_ip in expected_nodes.items():
            expected_node_id = str(expected_node_id)
            expected_node_ip = str(expected_node_ip or "")
            if expected_node_id in alive_nodes:
                continue
            if expected_node_ip and expected_node_ip in alive_by_ip:
                continue
            unavailable.append(
                {"node_id": expected_node_id, "node_ip": expected_node_ip or None}
            )
        if unavailable:
            raise RuntimeError(
                "Model process cleanup is unverified on unavailable Ray nodes: "
                f"{unavailable}"
            )

    refs = {}
    for node_id in sorted(alive_nodes):
        if time.monotonic() >= deadline:
            raise TimeoutError("LLM owner process cleanup exceeded its deadline")
        refs[node_id] = stop_llm_owner_processes.options(
            scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                node_id=node_id,
                soft=False,
            ),
        ).remote(owner_id, timeout)
    if not refs:
        return {}

    results = {}
    errors = {}
    for node_id, ref in refs.items():
        try:
            results[node_id] = ray.get(
                ref,
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except Exception as exc:
            errors[node_id] = str(exc)
    if errors:
        raise RuntimeError(f"Model process cleanup failed on Ray nodes: {errors}")
    return results


def stop_llm_owner_processes_on_cluster(
    owner_id: str,
    timeout: float = LLM_PROCESS_STOP_TIMEOUT,
    expected_nodes: dict[str, str] | None = None,
):
    deadline = (
        time.monotonic()
        + max(0.0, float(timeout))
        + LLM_OWNER_CLEANUP_GRACE_SECONDS
    )
    return _run_control_before_deadline(
        lambda: _stop_llm_owner_processes_on_cluster_control(
            owner_id,
            timeout=timeout,
            expected_nodes=expected_nodes,
            deadline=deadline,
        ),
        deadline,
        "LLM owner process cleanup",
    )

class LlmInstanceMessage():
    def __init__(self, message_type: str, message_data: dict) -> None:
        self.message_type = message_type
        self.message_data = message_data

@ray.remote
class LLMServerActor:
    def __init__(
        self,
        model: str,
        gpu_id: int | None,
        instance_id: str | None = None,
        backend: str = "vllm",
        backend_args: dict | None = None,
        owner_id: str | None = None,
        generation_id: str | None = None,
        **legacy_backend_args,
    ):
        self.instance_id = instance_id or f"legacy-{hashlib.sha256(model.encode()).hexdigest()[:16]}"
        self.generation_id = generation_id or self.instance_id
        self.model = model
        self.gpu_id = None if gpu_id is None else str(gpu_id)
        merged_backend_args = dict(backend_args or {})
        merged_backend_args.update(legacy_backend_args)
        self.backend, self.backend_args = validate_model_backend(
            backend,
            merged_backend_args,
        )
        self.owner_id = owner_id
        self.host = "0.0.0.0"
        self._port_reservation_fd = None
        self.port = self._get_free_port()
        self.proc = None
        self.process_group_id = None
        self.ready = False
        self.stop_requested = False

    def get_port(self):
        return self.port

    def _get_free_port(self):
        self.port, self._port_reservation_fd = _reserve_llm_port()
        return self.port

    def _release_port_reservation(self) -> None:
        descriptor = self._port_reservation_fd
        self._port_reservation_fd = None
        _release_port_reservation(descriptor)

    def _stop_process(self, timeout: int = LLM_PROCESS_STOP_TIMEOUT):
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            self.proc.wait()
            self.proc = None
            self.process_group_id = None
            return
        if self.process_group_id is not None:
            try:
                current_process_group = os.getpgid(self.proc.pid)
            except ProcessLookupError:
                self.proc.poll()
                self.proc = None
                self.process_group_id = None
                return
            if current_process_group != self.process_group_id:
                raise RuntimeError(
                    f"Refusing to stop reused process group {self.process_group_id}"
                )
        _stop_subprocess(self.proc, self.process_group_id, timeout)
        self.proc = None
        self.process_group_id = None

    def launch_server(self):
        if self.stop_requested:
            self._release_port_reservation()
            raise RuntimeError(
                f"LLM instance {self.instance_id} was stopped before launch"
            )
        if self.proc is not None and self.proc.poll() is None:
            return {
                "port": self.port,
                "process_group_id": self.process_group_id,
                "backend": self.backend,
            }

        try:
            env = build_model_env(
                self.gpu_id,
                self.instance_id,
                self.owner_id,
                self.generation_id,
            )
            with _owner_launch_guard(self.owner_id) as owner_state_file:
                if self.backend == "vllm":
                    command = build_vllm_command(
                        self.model,
                        self.host,
                        self.port,
                        self.backend_args,
                    )
                else:
                    command = build_transformers_command(
                        self.model,
                        self.host,
                        self.port,
                    )
                    cache_dir = prepare_transformers_cache(
                        self.model,
                        self.generation_id,
                    )
                    if cache_dir is not None:
                        env["HF_HUB_CACHE"] = cache_dir
                        env["HUGGINGFACE_HUB_CACHE"] = cache_dir
                        env["HF_HUB_OFFLINE"] = "1"
                self.proc = _launch_model_subprocess(command, env, owner_state_file)
                if os.name == "posix":
                    self.process_group_id = self.proc.pid
        except BaseException:
            self._release_port_reservation()
            raise
        return {
            "port": self.port,
            "process_group_id": self.process_group_id,
            "backend": self.backend,
        }

    def get_process_status(self):
        return_code = None if self.proc is None else self.proc.poll()
        return {
            "return_code": return_code,
            "ready": self.ready,
            "running": self.proc is not None and return_code is None,
        }

    def mark_ready(self):
        if self.stop_requested or self.proc is None or self.proc.poll() is not None:
            raise RuntimeError(
                f"LLM instance {self.instance_id} stopped during startup"
            )
        self.ready = True
        self._release_port_reservation()
        return True

    def stop_server(self, timeout: int = LLM_PROCESS_STOP_TIMEOUT):
        self.stop_requested = True
        try:
            self._stop_process(timeout)
            cleanup_transformers_cache(self.generation_id)
            self.ready = False
            logger.info("%s instance %s stopped", self.backend, self.model)
            return True
        finally:
            self._release_port_reservation()


class LlmInstanceManager():
    def __init__(
        self,
        max_requests_per_instance: int = 8,
        scale_out_threshold: float = 1.0,
        idle_scale_in_seconds: float = 300.0,
        owner_id: str | None = None,
    ):
        _ensure_background_workers_for_process()
        self.owner_id = owner_id
        self.owner_nodes: dict[str, str] = {}
        self.owner_cleanup_required = False
        self.accepting_launches = True
        self.id_to_instance_addr = {}
        self.id_to_instance_actor = {}
        self.id_to_resource_detail = {}
        self.id_to_instance_metadata = {}
        self.id_to_state = {}
        self.id_to_stop_event = {}
        self.id_to_cleanup_error = {}
        self.id_to_runtime_error = {}
        self.id_to_scale_in_claim = set()
        self.cancelled_start_ids = set()
        self.pending_start_generations: dict[str, _PendingActorStart] = {}
        self.auto_deploy_by_instance = {}
        self.model_to_instances = defaultdict(set)
        self.workflow_model_affinity = {}
        self.pending_model_requests = defaultdict(int)
        self.pending_model_workflows = defaultdict(set)
        self.pending_model_anchors = {}
        self.deploying_model_counts = defaultdict(int)
        self.max_requests_per_instance = max(1, int(max_requests_per_instance))
        self.scale_out_threshold = max(0.1, float(scale_out_threshold))
        self.idle_scale_in_seconds = max(1.0, float(idle_scale_in_seconds))
        self.lock = threading.RLock()

    def begin_shutdown(self) -> None:
        with self.lock:
            self.accepting_launches = False
            for instance_id in list(self.pending_start_generations):
                self._invalidate_pending_start_locked(
                    instance_id,
                    RuntimeError("LLM instance manager is shutting down"),
                )

    def _invalidate_pending_start_locked(
        self,
        instance_id: str,
        error: Exception,
    ) -> _PendingActorStart | None:
        generation = self.pending_start_generations.pop(instance_id, None)
        if generation is None:
            return None
        generation.cancel_error = error
        future = generation.future
        if future is None:
            if not generation.actor_creation_submitted:
                generation.release_cleanup_slot()
        elif future.cancel():
            generation.release_cleanup_slot()
        else:
            try:
                replacement_started = _ACTOR_CREATION_EXECUTOR.abandon(future)
            except BaseException:
                replacement_started = False
                logger.exception(
                    "Failed to replace an abandoned actor creation worker for %s",
                    instance_id,
                )
            if (
                not replacement_started
                and not future.done()
            ):
                logger.error(
                    "Actor creation replacement capacity is exhausted for %s",
                    instance_id,
                )
        generation.wake_event.set()
        return generation

    def request_start_cancellation(self, instance_id: str) -> str | None:
        """Cancel a queued/running start and make an existing instance stoppable."""
        with self.lock:
            self.cancelled_start_ids.add(instance_id)
            pending = self._invalidate_pending_start_locked(
                instance_id,
                RuntimeError(f"LLM instance {instance_id} startup was cancelled"),
            )
            if pending is not None:
                return "creating"
            state = self.id_to_state.get(instance_id)
            if state not in {"launching", "ready"}:
                return state
            self.id_to_state[instance_id] = "stopping"
            detail = self.id_to_resource_detail.get(instance_id)
            if detail is not None:
                detail["status"] = "stopping"
            self._remove_routing_registration(instance_id, status="stopping")
            self.id_to_cleanup_error.pop(instance_id, None)
            self.id_to_stop_event.setdefault(instance_id, threading.Event()).clear()
            self.id_to_scale_in_claim.add(instance_id)
            return state

    def clear_start_cancellation(self, instance_id: str) -> None:
        with self.lock:
            self.cancelled_start_ids.discard(instance_id)

    def _schedule_late_actor_kill(
        self,
        instance_id: str,
        generation: _PendingActorStart,
        actor,
    ) -> None:
        with self.lock:
            if generation.actor_delivered or generation.late_actor_kill_claimed:
                return
            generation.late_actor_kill_claimed = True
            generation.stale_actor = actor

        def kill_late_actor():
            while True:
                termination_error = None
                try:
                    terminate_ref = actor.__ray_terminate__.remote()
                    ray.get(terminate_ref, timeout=LLM_ACTOR_STOP_TIMEOUT)
                except ray.exceptions.ActorDiedError:
                    generation.stale_actor = None
                    generation.release_cleanup_slot()
                    return
                except Exception as exc:
                    termination_error = exc
                else:
                    generation.stale_actor = None
                    generation.release_cleanup_slot()
                    return

                try:
                    ray.kill(actor, no_restart=True)
                    _confirm_actor_terminated(
                        actor,
                        time.monotonic() + LLM_ACTOR_STOP_TIMEOUT,
                        f"stale LLM actor {instance_id} force kill",
                    )
                except Exception:
                    logger.exception(
                        "Failed to force-kill stale LLM actor for instance %s "
                        "after actor-native termination failed (%s); retrying",
                        instance_id,
                        termination_error,
                    )
                    time.sleep(LLM_LATE_ACTOR_KILL_RETRY_SECONDS)
                    continue
                generation.stale_actor = None
                generation.release_cleanup_slot()
                return

        try:
            _LATE_ACTOR_KILL_EXECUTOR.submit(kill_late_actor)
        except RuntimeError as exc:
            with self.lock:
                generation.late_actor_kill_claimed = False
            raise RuntimeError(
                "Stale actor cleanup admission invariant was violated"
            ) from exc

    def _actor_creation_finished(
        self,
        instance_id: str,
        generation: _PendingActorStart,
        future: Future,
    ) -> None:
        generation.wake_event.set()
        if future.cancelled():
            generation.release_cleanup_slot()
            return
        try:
            actor = future.result()
        except BaseException:
            generation.release_cleanup_slot()
            return
        with self.lock:
            is_stale = (
                self.pending_start_generations.get(instance_id) is not generation
            )
        if is_stale:
            self._schedule_late_actor_kill(instance_id, generation, actor)

    def record_owner_node(self, node_id: str, node_ip: str) -> bool:
        with self.lock:
            node_id = str(node_id)
            node_ip = str(node_ip)
            if self.owner_nodes.get(node_id) == node_ip:
                return False
            self.owner_nodes[node_id] = node_ip
            return True

    def get_instance_resource_detail(self, instance_id:str):
        with self.lock:
            return dict(self.id_to_resource_detail[instance_id])

    def get_instance_state(self, instance_id: str):
        with self.lock:
            return self.id_to_state.get(instance_id)

    def has_instance(self, instance_id: str) -> bool:
        with self.lock:
            return instance_id in self.id_to_instance_actor

    def register_instance(
        self,
        instance_id: str,
        model: str,
        node_ip: str,
        node_id: str,
        gpu_id: int,
        port: str | int,
        resources: dict,
        backend: str = "vllm",
        lease_id: str | None = None,
        process_group_id: int | None = None,
        generation_id: str | None = None,
        served_model: str | None = None,
    ):
        backend, _ = validate_model_backend(backend)
        served_model = served_model or model
        addr = node_ip + ":" + str(port)
        endpoint = "http://" + addr + "/v1"
        with self.lock:
            existing_detail = self.id_to_resource_detail.get(instance_id, {})
            detail = {
                **existing_detail,
                "instance_id": instance_id,
                "model": model,
                "served_model": served_model,
                "backend": backend,
                "host": node_ip,
                "port": str(port),
                "endpoint": endpoint,
                "status": "ready",
                "node_id": node_id,
                "node_ip": node_ip,
                "gpu_id": gpu_id,
                "resources": dict(resources),
                "lease_id": (
                    lease_id
                    if lease_id is not None
                    else existing_detail.get("lease_id")
                ),
                "process_group_id": (
                    process_group_id
                    if process_group_id is not None
                    else existing_detail.get("process_group_id")
                ),
                "generation_id": (
                    generation_id
                    if generation_id is not None
                    else existing_detail.get("generation_id")
                ),
            }
            previous_metadata = self.id_to_instance_metadata.get(instance_id, {})
            metadata = {
                **previous_metadata,
                "instance_id": instance_id,
                "model": model,
                "served_model": served_model,
                "backend": backend,
                "node_id": node_id,
                "node_ip": node_ip,
                "gpu_id": gpu_id,
                "port": str(port),
                "addr": addr,
                "endpoint": endpoint,
                "status": "ready",
                "inflight_requests": previous_metadata.get("inflight_requests", 0),
                "total_routed_requests": previous_metadata.get(
                    "total_routed_requests",
                    0,
                ),
                "created_time": previous_metadata.get("created_time", time.time()),
                "last_used_time": previous_metadata.get("last_used_time"),
            }
            self.id_to_instance_addr[instance_id] = addr
            self.id_to_resource_detail[instance_id] = detail
            self.id_to_instance_metadata[instance_id] = metadata
            self.id_to_state[instance_id] = "ready"
            self.model_to_instances[(model, backend)].add(instance_id)
            self._clear_model_deploying_locked(instance_id=instance_id)
            self.pending_model_requests[(model, backend)] = 0
            self.pending_model_workflows[(model, backend)].clear()
            return dict(metadata)

    def _register_starting_instance(
        self,
        instance_id: str,
        actor,
        model: str,
        backend: str,
        node_id: str,
        node_ip: str,
        gpu_id: int | None,
        resources: dict,
        lease_id: str | None,
        generation_id: str | None = None,
        served_model: str | None = None,
    ) -> None:
        served_model = served_model or model
        with self.lock:
            if instance_id in self.cancelled_start_ids:
                raise RuntimeError(f"LLM instance {instance_id} startup was cancelled")
            if instance_id in self.id_to_instance_actor:
                raise RuntimeError(f"LLM instance {instance_id} is already registered")
            auto_deploy_key = self.auto_deploy_by_instance.get(instance_id)
            self.id_to_instance_actor[instance_id] = actor
            self.id_to_resource_detail[instance_id] = {
                "instance_id": instance_id,
                "model": model,
                "served_model": served_model,
                "backend": backend,
                "host": node_ip,
                "port": None,
                "endpoint": None,
                "status": "launching",
                "node_id": node_id,
                "node_ip": node_ip,
                "gpu_id": gpu_id,
                "resources": dict(resources),
                "lease_id": lease_id,
                "process_group_id": None,
                "generation_id": generation_id or uuid.uuid4().hex,
                "auto_deploy_key": (
                    list(auto_deploy_key) if auto_deploy_key is not None else None
                ),
            }
            self.id_to_state[instance_id] = "launching"
            self.id_to_stop_event[instance_id] = threading.Event()
            self.id_to_cleanup_error.pop(instance_id, None)
            self.id_to_runtime_error.pop(instance_id, None)

    def _record_launch(self, instance_id: str, actor, launch_info: dict) -> str:
        with self.lock:
            if self.id_to_instance_actor.get(instance_id) is not actor:
                raise RuntimeError(f"LLM instance {instance_id} launch was cancelled")
            if self.id_to_state.get(instance_id) != "launching":
                raise RuntimeError(f"LLM instance {instance_id} is stopping")
            detail = self.id_to_resource_detail[instance_id]
            if launch_info.get("backend") != detail["backend"]:
                raise RuntimeError(
                    f"LLM instance {instance_id} launched unexpected backend "
                    f"{launch_info.get('backend')!r}"
                )
            port = str(launch_info["port"])
            addr = f"{detail['node_ip']}:{port}"
            detail["port"] = port
            detail["endpoint"] = f"http://{addr}/v1"
            detail["process_group_id"] = launch_info.get("process_group_id")
            self.id_to_instance_addr[instance_id] = addr
            return port

    def _mark_ready(self, instance_id: str, actor) -> dict:
        with self.lock:
            if self.id_to_instance_actor.get(instance_id) is not actor:
                raise RuntimeError(f"LLM instance {instance_id} launch was cancelled")
            if self.id_to_state.get(instance_id) != "launching":
                raise RuntimeError(f"LLM instance {instance_id} is stopping")
            detail = dict(self.id_to_resource_detail[instance_id])
            return self.register_instance(
                instance_id=instance_id,
                model=detail["model"],
                node_ip=detail["node_ip"],
                node_id=detail["node_id"],
                gpu_id=detail["gpu_id"],
                port=detail["port"],
                resources=detail["resources"],
                backend=detail["backend"],
                lease_id=detail.get("lease_id"),
                process_group_id=detail.get("process_group_id"),
                generation_id=detail.get("generation_id"),
                served_model=detail.get("served_model"),
            )

    def get_instance_info(self, instance_id: str) -> dict:
        with self.lock:
            detail = self.id_to_resource_detail[instance_id]
            info = {
                key: detail[key]
                for key in (
                    "instance_id",
                    "model",
                    "backend",
                    "host",
                    "port",
                    "endpoint",
                    "status",
                )
            }
            if detail.get("served_model") != detail["model"]:
                info["served_model"] = detail["served_model"]
            return info

    def _cleanup_remote(
        self,
        instance_id: str,
        node_id: str,
        node_ip: str | None = None,
        generation_id: str | None = None,
    ):
        target_node_id = str(node_id)
        if node_ip:
            alive_nodes = {
                str(node["NodeID"]): node
                for node in ray.nodes()
                if node.get("Alive") and node.get("NodeID")
            }
            if target_node_id not in alive_nodes:
                target_node_id = next(
                    (
                        alive_node_id
                        for alive_node_id, node in alive_nodes.items()
                        if str(node.get("NodeManagerAddress") or "") == str(node_ip)
                    ),
                    "",
                )
                if not target_node_id:
                    raise RuntimeError(
                        "Model process cleanup is unverified on unavailable Ray "
                        f"node {node_id} ({node_ip})"
                    )
        return stop_llm_instance_processes.options(
            scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                node_id=target_node_id,
                soft=False,
            ),
        ).remote(instance_id, generation_id=generation_id)

    def _ready_model_id(
        self,
        base_url: str,
        model: str,
        backend_args: dict,
        timeout: float = 5,
    ) -> str:
        response = requests.get(f"{base_url}/v1/models", timeout=timeout)
        response.raise_for_status()
        model_ids = [item.get("id") for item in response.json().get("data", [])]
        expected_model = backend_args.get("served_model_name") or model
        if expected_model not in model_ids:
            raise RuntimeError(
                f"Model server returned {model_ids!r}, expected {expected_model!r}"
            )
        return expected_model

    def _warmup(
        self,
        base_url: str,
        model_id: str,
        timeout: float = 120,
    ) -> None:
        response = requests.post(
            f"{base_url}/v1/chat/completions",
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "Reply with READY."}],
                "max_tokens": 8,
                "temperature": 0,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        choices = response.json().get("choices") or []
        content = choices[0].get("message", {}).get("content") if choices else None
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Model warmup returned an empty response")

    def _wait_until_ready(
        self,
        instance_id: str,
        actor,
        node_ip: str,
        port: str,
        model: str,
        backend: str,
        backend_args: dict,
        timeout: float = 300,
    ) -> None:
        base_url = f"http://{node_ip}:{port}"
        timeout = max(0.0, float(timeout))
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            with self.lock:
                if (
                    self.id_to_instance_actor.get(instance_id) is not actor
                    or self.id_to_state.get(instance_id) != "launching"
                ):
                    raise RuntimeError(
                        f"LLM instance {instance_id} startup was cancelled"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            process_status = ray.get(
                actor.get_process_status.remote(),
                timeout=min(5, remaining),
            )
            if process_status["return_code"] is not None:
                last_error = RuntimeError(
                    f"{backend} exited with code {process_status['return_code']}"
                )
                break
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                response = requests.get(
                    f"{base_url}/health",
                    timeout=min(2, remaining),
                )
                if response.status_code == 200:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    model_id = self._ready_model_id(
                        base_url,
                        model,
                        backend_args,
                        timeout=min(5, remaining),
                    )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._warmup(
                        base_url,
                        model_id,
                        timeout=min(120, remaining),
                    )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    with self.lock:
                        if (
                            self.id_to_instance_actor.get(instance_id) is not actor
                            or self.id_to_state.get(instance_id) != "launching"
                        ):
                            raise RuntimeError(
                                f"LLM instance {instance_id} startup was cancelled"
                            )
                    ray.get(
                        actor.mark_ready.remote(),
                        timeout=min(5, remaining),
                    )
                    logger.info("%s instance %s is ready", backend, model)
                    return
            except requests.HTTPError as exc:
                last_error = exc
                status_code = (
                    exc.response.status_code if exc.response is not None else None
                )
                if status_code is not None and 400 <= status_code < 500:
                    break
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
            time.sleep(min(1, max(0.0, deadline - time.monotonic())))

        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(
            f"{backend} instance {model} failed to become ready within {timeout}s{detail}"
        )

    def runtime_cleanup_candidates(self) -> list[dict]:
        """Return instances whose process cleanup must be attempted or retried."""
        with self.lock:
            candidates = [
                {
                    "instance_id": instance_id,
                    "state": "cleanup_pending",
                    "reason": self.id_to_cleanup_error.get(
                        instance_id,
                        "cleanup did not finish",
                    ),
                }
                for instance_id, state in self.id_to_state.items()
                if state == "cleanup_pending"
            ]
            ready_instances = [
                (
                    instance_id,
                    self.id_to_instance_actor.get(instance_id),
                    dict(self.id_to_resource_detail[instance_id]),
                )
                for instance_id, state in self.id_to_state.items()
                if state == "ready"
            ]

        for instance_id, actor, detail in ready_instances:
            failure = None
            try:
                if actor is None:
                    raise RuntimeError("model actor is missing")
                process_status = ray.get(
                    actor.get_process_status.remote(),
                    timeout=LLM_RUNTIME_PROBE_TIMEOUT,
                )
                return_code = process_status.get("return_code")
                if return_code is not None:
                    raise RuntimeError(
                        f"model process exited with code {return_code}"
                    )
                if not process_status.get("running", return_code is None):
                    raise RuntimeError("model process is not running")
                if not process_status.get("ready", False):
                    raise RuntimeError("model actor lost its ready state")

                response = requests.get(
                    f"http://{detail['node_ip']}:{detail['port']}/health",
                    timeout=LLM_RUNTIME_HEALTH_TIMEOUT,
                )
                if response.status_code != 200:
                    raise RuntimeError(
                        f"model health check returned HTTP {response.status_code}"
                    )
            except Exception as exc:
                failure = str(exc) or type(exc).__name__

            if failure is None:
                continue

            with self.lock:
                if (
                    self.id_to_instance_actor.get(instance_id) is not actor
                    or self.id_to_state.get(instance_id) != "ready"
                ):
                    continue
                self.id_to_state[instance_id] = "unhealthy"
                self.id_to_resource_detail[instance_id]["status"] = "unhealthy"
                self.id_to_runtime_error[instance_id] = failure
                self._remove_routing_registration(
                    instance_id,
                    status="unhealthy",
                )
                candidates.append({
                    "instance_id": instance_id,
                    "state": "unhealthy",
                    "reason": failure,
                })

        return candidates

    def _model_key_from_anchor(self, model_anchor: dict | None):
        model_anchor = model_anchor or {}
        model = model_anchor.get("local_model") or model_anchor.get("model")
        if not model:
            return None
        backend = model_anchor.get("backend") or model_anchor.get("engine") or "vllm"
        backend, _ = validate_model_backend(backend)
        return model, backend

    def record_model_demand(
        self,
        model_anchor: dict | None,
        count: int = 1,
        workflow_id: str | None = None,
    ):
        key = self._model_key_from_anchor(model_anchor)
        if key is None:
            return None
        with self.lock:
            if workflow_id:
                self.pending_model_workflows[key].add(str(workflow_id))
                self.pending_model_requests[key] = len(
                    self.pending_model_workflows[key]
                )
            else:
                self.pending_model_requests[key] += max(1, int(count))
            self.pending_model_anchors[key] = dict(model_anchor or {})
            return {
                "model": key[0],
                "backend": key[1],
                "pending_requests": self.pending_model_requests[key],
            }

    def mark_model_deploying(
        self,
        model: str,
        backend: str = "vllm",
        *,
        instance_id: str | None = None,
    ) -> bool:
        key = (model, backend)
        with self.lock:
            if instance_id is not None:
                if instance_id in self.auto_deploy_by_instance:
                    return False
                self.auto_deploy_by_instance[instance_id] = key
            self.deploying_model_counts[key] += 1
            return True

    def _clear_model_deploying_locked(
        self,
        model: str | None = None,
        backend: str = "vllm",
        *,
        instance_id: str | None = None,
    ) -> bool:
        if instance_id is not None:
            key = self.auto_deploy_by_instance.pop(instance_id, None)
            if key is None:
                return False
        elif model is not None:
            key = (model, backend)
        else:
            return False
        if self.deploying_model_counts[key] <= 0:
            return False
        self.deploying_model_counts[key] -= 1
        return True

    def clear_model_deploying(
        self,
        model: str | None = None,
        backend: str = "vllm",
        *,
        instance_id: str | None = None,
    ) -> bool:
        with self.lock:
            return self._clear_model_deploying_locked(
                model,
                backend,
                instance_id=instance_id,
            )

    def _estimated_gpu_mem(self, model_anchor: dict | None):
        model_anchor = model_anchor or {}
        for key in ("gpu_mem", "estimated_gpu_mem_mb", "estimated_gpu_memory_mb"):
            value = model_anchor.get(key)
            if value:
                return int(float(value))
        weight_bytes = model_anchor.get("estimated_weight_memory_bytes")
        if weight_bytes:
            return max(1, int(float(weight_bytes) / (1024 * 1024) * 1.2))
        return 0

    def route_model_request(self, workflow_id: str | None, model_anchor: dict | None):
        model_anchor = model_anchor or {}
        key = self._model_key_from_anchor(model_anchor)
        if key is None:
            return None
        with self.lock:
            model, backend = key
            affinity_key = (workflow_id, model, backend)
            candidates = [
                self.id_to_instance_metadata[instance_id]
                for instance_id in sorted(
                    self.model_to_instances.get((model, backend), set())
                )
                if (
                    instance_id in self.id_to_instance_metadata
                    and self.id_to_state.get(instance_id) == "ready"
                )
            ]
            if not candidates:
                self.record_model_demand(model_anchor, workflow_id=workflow_id)
                return None

            affinity_instance_id = self.workflow_model_affinity.get(affinity_key)
            selected = None
            affinity_hit = False
            if affinity_instance_id in {
                candidate["instance_id"] for candidate in candidates
            }:
                affinity_candidate = self.id_to_instance_metadata[
                    affinity_instance_id
                ]
                if (
                    affinity_candidate.get("inflight_requests", 0)
                    < self.max_requests_per_instance
                ):
                    selected = affinity_candidate
                    affinity_hit = True

            if selected is None:
                available = [
                    candidate
                    for candidate in candidates
                    if candidate.get("inflight_requests", 0)
                    < self.max_requests_per_instance
                ]
                if not available:
                    self.record_model_demand(model_anchor, workflow_id=workflow_id)
                    return None
                selected = min(
                    available,
                    key=lambda candidate: (
                        candidate.get("inflight_requests", 0),
                        candidate.get("last_used_time") or 0.0,
                        candidate.get("instance_id") or "",
                    ),
                )
                if workflow_id:
                    self.workflow_model_affinity[affinity_key] = selected[
                        "instance_id"
                    ]

            selected["inflight_requests"] = (
                selected.get("inflight_requests", 0) + 1
            )
            selected["total_routed_requests"] = (
                selected.get("total_routed_requests", 0) + 1
            )
            selected["last_used_time"] = time.time()
            if workflow_id:
                self.pending_model_workflows[key].discard(str(workflow_id))
                self.pending_model_requests[key] = len(
                    self.pending_model_workflows[key]
                )
            else:
                self.pending_model_requests[key] = 0
            return {
                "model": model,
                "served_model": selected.get("served_model") or model,
                "backend": backend,
                "instance_id": selected["instance_id"],
                "endpoint": selected["endpoint"],
                "addr": selected["addr"],
                "node_id": selected["node_id"],
                "node_ip": selected["node_ip"],
                "gpu_id": selected["gpu_id"],
                "workflow_id": workflow_id,
                "affinity_hit": affinity_hit,
                "inflight_requests": selected["inflight_requests"],
            }

    def clear_workflow_state(self, workflow_id: str | None):
        if not workflow_id:
            return
        workflow_id = str(workflow_id)
        with self.lock:
            for key, workflows in self.pending_model_workflows.items():
                workflows.discard(workflow_id)
                self.pending_model_requests[key] = len(workflows)
            for key in list(self.workflow_model_affinity):
                if key[0] == workflow_id:
                    del self.workflow_model_affinity[key]

    def release_model_route(self, model_route: dict | None):
        if not model_route:
            return
        instance_id = model_route.get("instance_id")
        with self.lock:
            metadata = self.id_to_instance_metadata.get(instance_id)
            if metadata is None:
                return
            metadata["inflight_requests"] = max(
                0,
                metadata.get("inflight_requests", 0) - 1,
            )
            metadata["last_used_time"] = time.time()

    def snapshot(self):
        with self.lock:
            return {
                "instances": {
                    instance_id: dict(metadata)
                    for instance_id, metadata in self.id_to_instance_metadata.items()
                },
                "workflow_model_affinity": {
                    "|".join(str(part) for part in key): value
                    for key, value in self.workflow_model_affinity.items()
                },
                "pending_model_requests": {
                    "|".join(key): value
                    for key, value in self.pending_model_requests.items()
                },
                "deploying_model_counts": {
                    "|".join(key): value
                    for key, value in self.deploying_model_counts.items()
                },
                "auto_deploy_by_instance": {
                    instance_id: "|".join(key)
                    for instance_id, key in self.auto_deploy_by_instance.items()
                },
                "instance_states": dict(self.id_to_state),
                "runtime_errors": dict(self.id_to_runtime_error),
                "owner_id": self.owner_id,
                "owner_nodes": dict(self.owner_nodes),
                "accepting_launches": self.accepting_launches,
                "max_requests_per_instance": self.max_requests_per_instance,
                "scale_out_threshold": self.scale_out_threshold,
                "idle_scale_in_seconds": self.idle_scale_in_seconds,
            }

    def scale_out_recommendations(self):
        with self.lock:
            recommendations = []
            for (model, backend), pending_requests in list(
                self.pending_model_requests.items()
            ):
                if pending_requests <= 0:
                    continue
                active_count = len(
                    self.model_to_instances.get((model, backend), set())
                )
                deploying_count = self.deploying_model_counts.get(
                    (model, backend),
                    0,
                )
                if deploying_count > 0:
                    continue
                denominator = active_count + deploying_count
                ratio = (
                    float("inf")
                    if denominator == 0
                    else pending_requests / denominator
                )
                model_anchor = self.pending_model_anchors.get((model, backend), {})
                recommendations.append({
                    "model": model,
                    "backend": backend,
                    "pending_requests": pending_requests,
                    "active_instances": active_count,
                    "deploying_instances": deploying_count,
                    "pending_per_instance": ratio,
                    "gpu_mem": self._estimated_gpu_mem(model_anchor),
                    "model_anchor": dict(model_anchor),
                    "reason": (
                        "no_active_instance"
                        if active_count == 0
                        else "pending_ratio_exceeded"
                    ),
                })
            return recommendations

    def lru_scale_in_candidates(self, now: float | None = None, idle_seconds: float | None = None):
        now = time.time() if now is None else float(now)
        idle_seconds = self.idle_scale_in_seconds if idle_seconds is None else float(idle_seconds)
        with self.lock:
            candidates = []
            for instance_id, metadata in self.id_to_instance_metadata.items():
                if self.id_to_state.get(instance_id) != "ready":
                    continue
                if metadata.get("inflight_requests", 0) > 0:
                    continue
                last_used_time = (
                    metadata.get("last_used_time")
                    or metadata.get("created_time")
                    or now
                )
                idle_for = now - last_used_time
                if idle_for < idle_seconds:
                    continue
                candidates.append({
                    **dict(metadata),
                    "state": "idle",
                    "idle_since": last_used_time,
                    "idle_for_seconds": idle_for,
                    "reason": "lru_idle",
                })
            candidates.sort(
                key=lambda item: (
                    item.get("last_used_time")
                    or item.get("created_time")
                    or 0.0
                )
            )
            return candidates

    def claim_lru_scale_in(
        self,
        instance_id: str,
        *,
        expected_idle_since: float | None = None,
        now: float | None = None,
        idle_seconds: float | None = None,
    ) -> bool:
        """Atomically remove an idle instance from routing before cleanup."""
        now = time.time() if now is None else float(now)
        idle_seconds = (
            self.idle_scale_in_seconds
            if idle_seconds is None
            else float(idle_seconds)
        )
        with self.lock:
            if self.id_to_state.get(instance_id) != "ready":
                return False
            metadata = self.id_to_instance_metadata.get(instance_id)
            if metadata is None or metadata.get("inflight_requests", 0) != 0:
                return False
            idle_since = (
                metadata.get("last_used_time")
                or metadata.get("created_time")
                or now
            )
            if (
                expected_idle_since is not None
                and idle_since != expected_idle_since
            ):
                return False
            if now - idle_since < idle_seconds:
                return False
            self.id_to_state[instance_id] = "stopping"
            self.id_to_resource_detail[instance_id]["status"] = "stopping"
            self._remove_routing_registration(instance_id, status="stopping")
            self.id_to_cleanup_error.pop(instance_id, None)
            self.id_to_stop_event.setdefault(instance_id, threading.Event()).clear()
            self.id_to_scale_in_claim.add(instance_id)
            return True

    def cancel_lru_scale_in_claim(self, instance_id: str) -> bool:
        """Restore routing when an idle cleanup could not be submitted."""
        with self.lock:
            if (
                instance_id not in self.id_to_scale_in_claim
                or self.id_to_state.get(instance_id) != "stopping"
            ):
                return False
            metadata = self.id_to_instance_metadata.get(instance_id)
            detail = self.id_to_resource_detail.get(instance_id)
            if metadata is None or detail is None:
                return False
            self.id_to_scale_in_claim.discard(instance_id)
            self.id_to_state[instance_id] = "ready"
            detail["status"] = "ready"
            metadata["status"] = "ready"
            self.model_to_instances[(metadata["model"], metadata["backend"])].add(
                instance_id
            )
            return True

    def start_llm_instance(
        self,
        instance_id: str,
        model: str,
        node_ip: str,
        node_id: str,
        gpu_id: int | None,
        resources: dict,
        backend: str = "vllm",
        backend_args: dict | None = None,
        launch_model: str | None = None,
        lease_id: str | None = None,
        startup_timeout: float = 300,
        return_info: bool = False,
    ):
        startup_timeout = float(startup_timeout)
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be greater than zero")
        deadline = time.monotonic() + startup_timeout

        backend, backend_args = validate_model_backend(backend, backend_args)
        launch_model = str(launch_model or model)
        served_model = str(backend_args.get("served_model_name") or launch_model)
        if backend == "transformers":
            validate_transformers_model(launch_model)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"LLM instance {instance_id} actor creation timed out after "
                f"{startup_timeout}s"
            )

        actor_args = {
            "instance_id": instance_id,
            "model": launch_model,
            "gpu_id": gpu_id,
            "backend": backend,
            "backend_args": backend_args,
        }
        if self.owner_id is not None:
            actor_args["owner_id"] = self.owner_id

        with self.lock:
            if not self.accepting_launches:
                raise RuntimeError("LLM instance manager is shutting down")
            if instance_id in self.cancelled_start_ids:
                raise RuntimeError(f"LLM instance {instance_id} startup was cancelled")
            if (
                instance_id in self.id_to_instance_actor
                or instance_id in self.pending_start_generations
            ):
                raise RuntimeError(f"LLM instance {instance_id} is already registered")
            generation = _PendingActorStart(deadline)
            actor_args["generation_id"] = generation.generation_id
            if not generation.reserve_cleanup_slot(_STALE_ACTOR_CLEANUP_SLOTS):
                raise RuntimeError(
                    "Stale LLM actor cleanup capacity is exhausted; retry later"
                )
            self.pending_start_generations[instance_id] = generation

        with self.lock:
            if self.pending_start_generations.get(instance_id) is not generation:
                raise generation.cancel_error or RuntimeError(
                    f"LLM instance {instance_id} actor creation was cancelled"
                )
            if time.monotonic() >= deadline:
                timeout_error = TimeoutError(
                    f"LLM instance {instance_id} actor creation timed out after "
                    f"{startup_timeout}s"
                )
                self._invalidate_pending_start_locked(instance_id, timeout_error)
                raise timeout_error
            self.owner_cleanup_required = True
            generation.actor_creation_submitted = True

        actor_class = LLMServerActor

        def create_actor():
            actor_options = actor_class.options(
                scheduling_strategy=(
                    ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                        node_id=node_id,
                        soft=False,
                    )
                ),
            )
            return actor_options.remote(**actor_args)

        try:
            creation_future = _ACTOR_CREATION_EXECUTOR.submit(create_actor)
        except Exception:
            with self.lock:
                generation.actor_creation_submitted = False
                if self.pending_start_generations.get(instance_id) is generation:
                    self.pending_start_generations.pop(instance_id)
            generation.release_cleanup_slot()
            raise

        with self.lock:
            generation.future = creation_future
        creation_future.add_done_callback(
            lambda future: self._actor_creation_finished(
                instance_id,
                generation,
                future,
            )
        )
        with self.lock:
            generation_is_current = (
                self.pending_start_generations.get(instance_id) is generation
            )
        if not generation_is_current:
            replacement_started = False
            if not creation_future.cancel():
                try:
                    replacement_started = _ACTOR_CREATION_EXECUTOR.abandon(
                        creation_future
                    )
                except BaseException:
                    logger.exception(
                        "Failed to replace an abandoned actor creation worker for %s",
                        instance_id,
                    )
            if not replacement_started and not creation_future.done():
                logger.error(
                    "Actor creation replacement capacity is exhausted for %s",
                    instance_id,
                )

        remaining = deadline - time.monotonic()
        if remaining <= 0 or not generation.wake_event.wait(max(0.0, remaining)):
            timeout_error = TimeoutError(
                f"LLM instance {instance_id} actor creation timed out after "
                f"{startup_timeout}s"
            )
            with self.lock:
                if self.pending_start_generations.get(instance_id) is generation:
                    self._invalidate_pending_start_locked(instance_id, timeout_error)
            if creation_future.done() and not creation_future.cancelled():
                try:
                    late_actor = creation_future.result()
                except BaseException:
                    pass
                else:
                    self._schedule_late_actor_kill(
                        instance_id,
                        generation,
                        late_actor,
                    )
            raise timeout_error

        if not creation_future.done():
            raise generation.cancel_error or RuntimeError(
                f"LLM instance {instance_id} actor creation was cancelled"
            )

        try:
            actor = creation_future.result()
        except BaseException:
            with self.lock:
                is_current = (
                    self.pending_start_generations.get(instance_id) is generation
                )
                if is_current:
                    self.pending_start_generations.pop(instance_id)
                cancel_error = generation.cancel_error
            generation.release_cleanup_slot()
            if cancel_error is not None:
                raise cancel_error
            raise

        accepted = False
        start_error = generation.cancel_error
        with self.lock:
            is_current = (
                self.pending_start_generations.get(instance_id) is generation
            )
            if is_current and not self.accepting_launches:
                start_error = RuntimeError("LLM instance manager is shutting down")
            elif is_current and instance_id in self.cancelled_start_ids:
                start_error = RuntimeError(
                    f"LLM instance {instance_id} startup was cancelled"
                )
            elif is_current and time.monotonic() >= deadline:
                start_error = TimeoutError(
                    f"LLM instance {instance_id} actor creation timed out after "
                    f"{startup_timeout}s"
                )
            elif is_current:
                try:
                    self._register_starting_instance(
                        instance_id,
                        actor,
                        model,
                        backend,
                        node_id,
                        node_ip,
                        gpu_id,
                        resources,
                        lease_id,
                        generation.generation_id,
                        served_model=served_model,
                    )
                except BaseException as exc:
                    start_error = exc
                else:
                    self.pending_start_generations.pop(instance_id)
                    self.owner_nodes[str(node_id)] = str(node_ip)
                    generation.actor_delivered = True
                    accepted = True

            if is_current and not accepted:
                self.pending_start_generations.pop(instance_id)

        if not accepted:
            self._schedule_late_actor_kill(instance_id, generation, actor)
            raise start_error or RuntimeError(
                f"LLM instance {instance_id} actor creation was superseded"
            )

        generation.release_cleanup_slot()

        try:
            with self.lock:
                if (
                    self.id_to_instance_actor.get(instance_id) is not actor
                    or self.id_to_state.get(instance_id) != "launching"
                ):
                    raise RuntimeError(
                        f"LLM instance {instance_id} startup was cancelled"
                    )
            launch_ref = actor.launch_server.remote()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"LLM instance {instance_id} actor launch timed out"
                )
            try:
                launch_info = ray.get(launch_ref, timeout=remaining)
            except ray.exceptions.GetTimeoutError as exc:
                raise TimeoutError(
                    f"LLM instance {instance_id} actor launch timed out after "
                    f"{startup_timeout}s"
                ) from exc
            port = self._record_launch(instance_id, actor, launch_info)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"LLM instance {instance_id} startup timed out after "
                    f"{startup_timeout}s"
                )
            self._wait_until_ready(
                instance_id,
                actor,
                node_ip,
                port,
                launch_model,
                backend,
                backend_args,
                timeout=remaining,
            )
            self._mark_ready(instance_id, actor)
        except Exception as start_error:
            try:
                self.stop_llm_instance(
                    instance_id,
                    finalize=False,
                    expected_actor=actor,
                )
            except Exception as cleanup_error:
                raise RuntimeError(
                    f"LLM instance {instance_id} launch failed: {start_error}; "
                    f"cleanup is pending: {cleanup_error}"
                ) from start_error
            raise

        if return_info:
            return self.get_instance_info(instance_id)
        return port

    def _remove_routing_registration(
        self,
        instance_id: str,
        *,
        status: str | None = None,
    ) -> None:
        metadata = self.id_to_instance_metadata.get(instance_id)
        if metadata is not None:
            self.model_to_instances[(metadata["model"], metadata["backend"])].discard(
                instance_id
            )
            if status is not None:
                metadata["status"] = status
        for key, value in list(self.workflow_model_affinity.items()):
            if value == instance_id:
                del self.workflow_model_affinity[key]

    def _stop_instance_control_transaction(
        self,
        instance_id: str,
        actor,
        resource_detail: dict,
        state: str,
        deadline: float,
    ) -> None:
        actor_stop_error = None
        actor_kill_error = None
        cleanup_error = None
        actor_termination_confirmed = actor is None
        process_cleanup_confirmed = False

        if actor is not None:
            try:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"LLM instance {instance_id} graceful stop exceeded its deadline"
                    )
                actor_stop_ref = actor.stop_server.remote(LLM_PROCESS_STOP_TIMEOUT)
                _ray_get_before_deadline(
                    actor_stop_ref,
                    deadline,
                    5 if state == "launching" else LLM_ACTOR_STOP_TIMEOUT,
                    f"LLM instance {instance_id} graceful stop",
                )
            except Exception as exc:
                actor_stop_error = exc

            try:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"LLM instance {instance_id} force kill exceeded its deadline"
                    )
                ray.kill(actor, no_restart=True)
                _confirm_actor_terminated(
                    actor,
                    deadline,
                    f"LLM instance {instance_id} force kill",
                )
                actor_termination_confirmed = True
            except Exception as exc:
                actor_kill_error = exc

        try:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"LLM instance {instance_id} process cleanup exceeded its deadline"
                )
            cleanup_ref = self._cleanup_remote(
                instance_id,
                resource_detail["node_id"],
                resource_detail.get("node_ip"),
                resource_detail.get("generation_id"),
            )
            _ray_get_before_deadline(
                cleanup_ref,
                deadline,
                LLM_CLEANUP_TASK_TIMEOUT,
                f"LLM instance {instance_id} process cleanup",
            )
            process_cleanup_confirmed = True
        except Exception as exc:
            cleanup_error = exc

        failures = []
        if not actor_termination_confirmed:
            failures.append(
                "actor termination was not confirmed"
                f" (graceful stop: {actor_stop_error}; force kill: {actor_kill_error})"
            )
        if not process_cleanup_confirmed:
            failures.append(f"process cleanup was not confirmed ({cleanup_error})")
        if failures:
            raise RuntimeError("; ".join(failures))

    def stop_llm_instance(
        self,
        instance_id: str,
        *,
        finalize: bool = True,
        expected_actor=None,
        deadline: float | None = None,
    ):
        if deadline is None:
            deadline = time.monotonic() + LLM_STOP_TOTAL_TIMEOUT
        else:
            deadline = float(deadline)
        with self.lock:
            current_actor = self.id_to_instance_actor.get(instance_id)
            if expected_actor is not None and current_actor is not expected_actor:
                return None
            if instance_id not in self.id_to_resource_detail:
                raise KeyError(instance_id)
            actor = current_actor
            resource_detail = dict(self.id_to_resource_detail[instance_id])
            state = self.id_to_state.get(instance_id, "ready")
            stop_event = self.id_to_stop_event.setdefault(
                instance_id,
                threading.Event(),
            )
            if state == "stopped":
                if finalize:
                    self.finalize_stopped_instance(
                        instance_id,
                        expected_actor=actor,
                    )
                return resource_detail
            if state == "stopping":
                stop_owner = instance_id in self.id_to_scale_in_claim
                self.id_to_scale_in_claim.discard(instance_id)
            else:
                stop_owner = True
                self.id_to_state[instance_id] = "stopping"
                self.id_to_resource_detail[instance_id]["status"] = "stopping"
                self._remove_routing_registration(instance_id, status="stopping")
                self.id_to_cleanup_error.pop(instance_id, None)
                stop_event.clear()

        if not stop_owner:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not stop_event.wait(remaining):
                raise RuntimeError(f"Timed out waiting to stop LLM instance {instance_id}")
            with self.lock:
                if (
                    expected_actor is not None
                    and self.id_to_instance_actor.get(instance_id) is not expected_actor
                ):
                    return None
                state = self.id_to_state.get(instance_id)
                if state is None:
                    return resource_detail
                if state == "stopped":
                    if finalize:
                        self.finalize_stopped_instance(
                            instance_id,
                            expected_actor=actor,
                        )
                    return resource_detail
                error = self.id_to_cleanup_error.get(
                    instance_id,
                    "cleanup did not finish",
                )
            raise RuntimeError(f"Failed to stop LLM instance {instance_id}: {error}")

        try:
            _run_control_before_deadline(
                lambda: self._stop_instance_control_transaction(
                    instance_id,
                    actor,
                    resource_detail,
                    state,
                    deadline,
                ),
                deadline,
                f"LLM instance {instance_id} cleanup",
            )
        except Exception as cleanup_error:
            with self.lock:
                if (
                    instance_id in self.id_to_resource_detail
                    and self.id_to_instance_actor.get(instance_id) is actor
                ):
                    self.id_to_state[instance_id] = "cleanup_pending"
                    self.id_to_resource_detail[instance_id]["status"] = (
                        "cleanup_pending"
                    )
                    metadata = self.id_to_instance_metadata.get(instance_id)
                    if metadata is not None:
                        metadata["status"] = "cleanup_pending"
                    self.id_to_cleanup_error[instance_id] = str(cleanup_error)
            stop_event.set()
            raise RuntimeError(
                f"Failed to clean up LLM instance {instance_id}: {cleanup_error}"
            ) from cleanup_error

        marked_stopped = False
        with self.lock:
            if (
                instance_id in self.id_to_resource_detail
                and self.id_to_instance_actor.get(instance_id) is actor
            ):
                self.id_to_state[instance_id] = "stopped"
                self.id_to_resource_detail[instance_id]["status"] = "stopped"
                metadata = self.id_to_instance_metadata.get(instance_id)
                if metadata is not None:
                    metadata["status"] = "stopped"
                self.id_to_cleanup_error.pop(instance_id, None)
                self.id_to_runtime_error.pop(instance_id, None)
                marked_stopped = True
        stop_event.set()
        if finalize and marked_stopped:
            self.finalize_stopped_instance(instance_id, expected_actor=actor)
        return resource_detail

    def finalize_stopped_instance(self, instance_id: str, *, expected_actor=None) -> bool:
        with self.lock:
            if (
                expected_actor is not None
                and self.id_to_instance_actor.get(instance_id) is not expected_actor
            ):
                return False
            state = self.id_to_state.get(instance_id)
            if state is None:
                return False
            if state != "stopped":
                raise RuntimeError(
                    f"Cannot forget LLM instance {instance_id} while state is {state}"
                )
            self._remove_routing_registration(instance_id)
            self.id_to_instance_actor.pop(instance_id, None)
            self.id_to_instance_addr.pop(instance_id, None)
            self.id_to_resource_detail.pop(instance_id, None)
            self.id_to_instance_metadata.pop(instance_id, None)
            self.id_to_state.pop(instance_id, None)
            self.id_to_stop_event.pop(instance_id, None)
            self.id_to_cleanup_error.pop(instance_id, None)
            self.id_to_runtime_error.pop(instance_id, None)
            self._clear_model_deploying_locked(instance_id=instance_id)
            self.id_to_scale_in_claim.discard(instance_id)
            self.cancelled_start_ids.discard(instance_id)
            return True

    def stop_all_llm_instances(self):
        with self.lock:
            instances = [
                (instance_id, self.id_to_instance_actor.get(instance_id))
                for instance_id in self.id_to_resource_detail
            ]
        stopped = {}
        errors = {}
        if not instances:
            return stopped, errors
        deadline = time.monotonic() + LLM_STOP_TOTAL_TIMEOUT
        with ThreadPoolExecutor(
            max_workers=min(LLM_STOP_ALL_WORKERS, len(instances))
        ) as executor:
            futures = {
                executor.submit(
                    self.stop_llm_instance,
                    instance_id,
                    finalize=False,
                    expected_actor=actor,
                    deadline=deadline,
                ): instance_id
                for instance_id, actor in instances
            }
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    detail = future.result()
                    if detail is None:
                        errors[instance_id] = "instance was superseded before stop"
                    else:
                        stopped[instance_id] = detail
                except Exception as exc:
                    errors[instance_id] = str(exc)
                    logger.exception(
                        "Failed to stop LLM instance %s during shutdown",
                        instance_id,
                    )
        return stopped, errors

    def stop_owned_llm_processes(self):
        if self.owner_id is None:
            return {}
        with self.lock:
            expected_nodes = dict(self.owner_nodes)
            cleanup_required = bool(
                self.owner_cleanup_required
                or expected_nodes
                or self.pending_start_generations
                or self.id_to_resource_detail
            )
        if not cleanup_required:
            return {}
        if expected_nodes:
            return stop_llm_owner_processes_on_cluster(
                self.owner_id,
                expected_nodes=expected_nodes,
            )
        return stop_llm_owner_processes_on_cluster(self.owner_id)
