import ray
import hashlib
import logging
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - model process cleanup requires POSIX /proc
    fcntl = None

logger = logging.getLogger(__name__)

LLM_INSTANCE_ENV_VAR = "MAZE_LLM_INSTANCE_ID"
LLM_OWNER_ENV_VAR = "MAZE_LLM_OWNER_ID"
LLM_OWNER_LOCK_FD_ENV_VAR = "MAZE_LLM_OWNER_LOCK_FD"
LLM_PROCESS_STOP_TIMEOUT = 15
LLM_ACTOR_STOP_TIMEOUT = 25
LLM_CLEANUP_TASK_TIMEOUT = LLM_PROCESS_STOP_TIMEOUT + 7
LLM_STOP_TOTAL_TIMEOUT = LLM_ACTOR_STOP_TIMEOUT + LLM_CLEANUP_TASK_TIMEOUT + 5
SUPPORTED_MODEL_BACKENDS = ("vllm", "transformers")
_LLM_LAUNCHER_CODE = (
    "import os,sys;"
    f"os.close(int(os.environ.pop({LLM_OWNER_LOCK_FD_ENV_VAR!r})));"
    "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
)


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
        raise ValueError(f"Unsupported model backend {backend!r}; expected one of: {supported}")

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


def _wait_for_process_group_exit(process_group_id: int, timeout: float, proc=None) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if proc is not None:
            proc.poll()
        if not _process_group_exists(process_group_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _stop_subprocess(proc, process_group_id: int | None, timeout: float):
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
    if not _wait_for_process_group_exit(process_group_id, min(timeout, 5), proc):
        raise RuntimeError(f"Process group {process_group_id} did not stop")


def build_model_env(
    gpu_id: str | None,
    instance_id: str | None = None,
    owner_id: str | None = None,
):
    env = os.environ.copy()
    environment_bin = os.path.dirname(sys.executable)
    current_path = env.get("PATH")
    env["PATH"] = (
        environment_bin
        if not current_path
        else os.pathsep.join((environment_bin, current_path))
    )
    if gpu_id is not None:
        env["CUDA_VISIBLE_DEVICES"] = gpu_id
    if instance_id is not None:
        env[LLM_INSTANCE_ENV_VAR] = instance_id
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


def build_vllm_command(model: str, host: str, port: str, extra_args: dict | None = None):
    cmd = [
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
        cmd.append(f"--{key.replace('_', '-')}")
        if value is not True:
            cmd.append(str(value))
    return cmd


def build_transformers_command(model: str, host: str, port: str):
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
    """Return the lossless Hugging Face cache name for a local model path."""
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
    """Expose a local model through the cache scanned by transformers serve."""
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


def _owner_process_groups(owner_id: str) -> set[int]:
    return _marked_process_groups(LLM_OWNER_ENV_VAR, owner_id)


def _stop_marked_process_groups(
    process_groups,
    timeout: float,
    settle_timeout: float,
):
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
):
    """Stop only process groups carrying this instance's unique marker."""
    stopped_groups = _stop_marked_process_groups(
        lambda: _instance_process_groups(instance_id),
        timeout,
        settle_timeout,
    )
    cleanup_transformers_cache(instance_id)
    return {"stopped_process_groups": sorted(stopped_groups)}


def stop_llm_owner_processes_locally(
    owner_id: str,
    timeout: float = LLM_PROCESS_STOP_TIMEOUT,
    settle_timeout: float = 0.5,
):
    """Stop model process groups created by one Scheduler lifecycle."""
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


def stop_llm_owner_processes_on_cluster(
    owner_id: str,
    timeout: float = LLM_PROCESS_STOP_TIMEOUT,
    expected_nodes: dict[str, str] | None = None,
):
    nodes = list(ray.nodes())
    alive_nodes = {
        str(node["NodeID"]): node
        for node in nodes
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
            unavailable.append({
                "node_id": expected_node_id,
                "node_ip": expected_node_ip or None,
            })
        if unavailable:
            raise RuntimeError(
                "Model process cleanup is unverified on unavailable Ray nodes: "
                f"{unavailable}"
            )

    node_ids = sorted(alive_nodes)
    refs = {
        node_id: stop_llm_owner_processes.options(
            scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                node_id=node_id,
                soft=False,
            ),
        ).remote(owner_id, timeout)
        for node_id in node_ids
    }
    if not refs:
        return {}

    deadline = time.monotonic() + timeout + 7
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


class LlmInstanceMessage():
    def __init__(self, message_type: str, message_data: dict) -> None:
        self.message_type = message_type
        self.message_data = message_data

@ray.remote
class LLMServerActor:
    def __init__(
        self,
        instance_id: str,
        model: str,
        gpu_id: int,
        backend: str = "vllm",
        backend_args: dict | None = None,
        owner_id: str | None = None,
    ):
        self.instance_id = instance_id
        self.model = model
        self.gpu_id = None if gpu_id is None else str(gpu_id)
        self.backend, self.backend_args = validate_model_backend(backend, backend_args)
        self.owner_id = owner_id
        self.host = "0.0.0.0"
        self.port = self._get_free_port()
        self.proc = None
        self.process_group_id = None
        self.ready = False
        self.stop_requested = False

    def _get_free_port(self):
        'Get a free port on the local machine(from 8000).'
        import socket
        port = 8000
        while port <= 65535:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(('127.0.0.1', port))
                    return str(port)
                except OSError:
                    # Port is already in use, try next one
                    port += 1
        raise RuntimeError("No free port found in range 8000-65535")

    def _stop_process(self, timeout: int = 15):
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
            raise RuntimeError(f"LLM instance {self.instance_id} was stopped before launch")
        if self.proc is not None and self.proc.poll() is None:
            return {
                "port": self.port,
                "process_group_id": self.process_group_id,
                "backend": self.backend,
            }
        env = build_model_env(self.gpu_id, self.instance_id, self.owner_id)
        with _owner_launch_guard(self.owner_id) as owner_state_file:
            if self.backend == "vllm":
                command = build_vllm_command(
                    self.model,
                    self.host,
                    self.port,
                    self.backend_args,
                )
            else:
                command = build_transformers_command(self.model, self.host, self.port)
                cache_dir = prepare_transformers_cache(self.model, self.instance_id)
                if cache_dir is not None:
                    env["HF_HUB_CACHE"] = cache_dir
                    env["HUGGINGFACE_HUB_CACHE"] = cache_dir
                    env["HF_HUB_OFFLINE"] = "1"
            self.proc = _launch_model_subprocess(
                command,
                env,
                owner_state_file,
            )
            if os.name == "posix":
                self.process_group_id = self.proc.pid
        return {
            "port": self.port,
            "process_group_id": self.process_group_id,
            "backend": self.backend,
        }

    def get_process_status(self):
        return_code = None if self.proc is None else self.proc.poll()
        return {"return_code": return_code, "ready": self.ready}

    def mark_ready(self):
        if self.stop_requested or self.proc is None or self.proc.poll() is not None:
            raise RuntimeError(f"LLM instance {self.instance_id} stopped during startup")
        self.ready = True


    def stop_server(self, timeout: int = 15):
        self.stop_requested = True
        self._stop_process(timeout)
        cleanup_transformers_cache(self.instance_id)
        self.ready = False
        logger.info("%s instance %s stopped", self.backend, self.model)


class LlmInstanceManager():
    def __init__(self, owner_id: str | None = None):
        self.owner_id = owner_id
        self.owner_nodes = {}
        self.accepting_launches = True
        self.id_to_instance_addr = {}
        self.id_to_instance_actor = {}
        self.id_to_resource_detail = {}
        self.id_to_state = {}
        self.id_to_stop_event = {}
        self.id_to_cleanup_error = {}
        self.lock = threading.RLock()

    def begin_shutdown(self) -> None:
        with self.lock:
            self.accepting_launches = False

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

    def _register_starting_instance(
        self,
        instance_id: str,
        actor,
        model: str,
        backend: str,
        node_id: str,
        node_ip: str,
        gpu_id: int,
        resources: dict,
        lease_id: str | None,
    ):
        with self.lock:
            if instance_id in self.id_to_instance_actor:
                raise RuntimeError(f"LLM instance {instance_id} is already registered")
            self.id_to_instance_actor[instance_id] = actor
            self.id_to_resource_detail[instance_id] = {
                "instance_id": instance_id,
                "model": model,
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
            }
            self.id_to_state[instance_id] = "launching"
            self.id_to_stop_event[instance_id] = threading.Event()
            self.id_to_cleanup_error.pop(instance_id, None)

    def _record_launch(self, instance_id: str, actor, launch_info: dict):
        with self.lock:
            if self.id_to_instance_actor.get(instance_id) is not actor:
                raise RuntimeError(f"LLM instance {instance_id} launch was cancelled")
            if self.id_to_state.get(instance_id) != "launching":
                raise RuntimeError(f"LLM instance {instance_id} is stopping")
            backend = self.id_to_resource_detail[instance_id]["backend"]
            if launch_info.get("backend") != backend:
                raise RuntimeError(
                    f"LLM instance {instance_id} launched unexpected backend "
                    f"{launch_info.get('backend')!r}"
                )
            port = str(launch_info["port"])
            self.id_to_resource_detail[instance_id]["port"] = port
            self.id_to_instance_addr[instance_id] = (
                self.id_to_resource_detail[instance_id]["node_ip"] + ":" + port
            )
            self.id_to_resource_detail[instance_id]["endpoint"] = (
                f"http://{self.id_to_instance_addr[instance_id]}/v1"
            )
            self.id_to_resource_detail[instance_id]["process_group_id"] = (
                launch_info.get("process_group_id")
            )
            return port

    def _mark_ready(self, instance_id: str, actor):
        with self.lock:
            if self.id_to_instance_actor.get(instance_id) is not actor:
                raise RuntimeError(f"LLM instance {instance_id} launch was cancelled")
            if self.id_to_state.get(instance_id) != "launching":
                raise RuntimeError(f"LLM instance {instance_id} is stopping")
            self.id_to_state[instance_id] = "ready"
            self.id_to_resource_detail[instance_id]["status"] = "ready"

    def get_instance_info(self, instance_id: str) -> dict:
        with self.lock:
            detail = self.id_to_resource_detail[instance_id]
            return {
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

    def _cleanup_remote(self, instance_id: str, node_id: str):
        return stop_llm_instance_processes.options(
            scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                node_id=node_id,
                soft=False,
            ),
        ).remote(instance_id)

    def _ready_model_id(
        self,
        base_url: str,
        model: str,
        backend_args: dict,
    ) -> str:
        response = requests.get(f"{base_url}/v1/models", timeout=5)
        response.raise_for_status()
        model_ids = [item.get("id") for item in response.json().get("data", [])]
        expected_model = backend_args.get("served_model_name") or model
        if expected_model not in model_ids:
            raise RuntimeError(f"Model server returned {model_ids!r}, expected {expected_model!r}")
        return expected_model

    def _warmup(self, base_url: str, model_id: str):
        response = requests.post(
            f"{base_url}/v1/chat/completions",
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "Reply with READY."}],
                "max_tokens": 8,
                "temperature": 0,
            },
            timeout=120,
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
        timeout: int = 300,
    ):
        base_url = f"http://{node_ip}:{port}"
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            if self.get_instance_state(instance_id) != "launching":
                raise RuntimeError(f"LLM instance {instance_id} startup was cancelled")
            process_status = ray.get(actor.get_process_status.remote(), timeout=5)
            if process_status["return_code"] is not None:
                last_error = RuntimeError(
                    f"{backend} exited with code {process_status['return_code']}"
                )
                break
            try:
                response = requests.get(f"{base_url}/health", timeout=2)
                if response.status_code == 200:
                    model_id = self._ready_model_id(base_url, model, backend_args)
                    self._warmup(base_url, model_id)
                    ray.get(actor.mark_ready.remote(), timeout=5)
                    logger.info("%s instance %s is ready", backend, model)
                    return
            except requests.HTTPError as exc:
                last_error = exc
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code is not None and 400 <= status_code < 500:
                    break
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
            time.sleep(1)

        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(
            f"{backend} instance {model} failed to become ready within {timeout}s{detail}"
        )

    def start_llm_instance(
        self,
        instance_id:str,
        model:str,
        node_ip:str,
        node_id:str,
        gpu_id:int,
        resources:dict,
        lease_id:str|None=None,
        backend:str="vllm",
        backend_args:dict|None=None,
    ):
        backend, backend_args = validate_model_backend(backend, backend_args)
        actor_options = LLMServerActor.options(
            scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                #num_cpus=0,
                node_id=node_id,
                soft=False
            ),
        )
        actor_args = {
            "instance_id": instance_id,
            "model": model,
            "gpu_id": gpu_id,
            "backend": backend,
            "backend_args": backend_args,
        }
        if self.owner_id is not None:
            actor_args["owner_id"] = self.owner_id
        with self.lock:
            if not self.accepting_launches:
                raise RuntimeError("LLM instance manager is shutting down")
            if instance_id in self.id_to_instance_actor:
                raise RuntimeError(f"LLM instance {instance_id} is already registered")
            self.owner_nodes[str(node_id)] = str(node_ip)
            actor = actor_options.remote(
                **actor_args,
            )
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
            )
        try:
            launch_info = ray.get(actor.launch_server.remote())
            port = self._record_launch(instance_id, actor, launch_info)
            self._wait_until_ready(
                instance_id,
                actor,
                node_ip,
                port,
                model,
                backend,
                backend_args,
            )
            self._mark_ready(instance_id, actor)
        except Exception as start_error:
            try:
                if self.has_instance(instance_id):
                    self.stop_llm_instance(instance_id)
            except Exception as cleanup_error:
                raise RuntimeError(
                    f"LLM instance {instance_id} launch failed: {start_error}; "
                    f"cleanup is pending: {cleanup_error}"
                ) from start_error
            raise
        
        return self.get_instance_info(instance_id)

    def stop_llm_instance(self, instance_id:str):
        with self.lock:
            actor = self.id_to_instance_actor[instance_id]
            resource_detail = dict(self.id_to_resource_detail[instance_id])
            state = self.id_to_state[instance_id]
            stop_event = self.id_to_stop_event[instance_id]
            if state == "stopped":
                return resource_detail
            if state == "stopping":
                stop_owner = False
            else:
                stop_owner = True
                self.id_to_state[instance_id] = "stopping"
                self.id_to_resource_detail[instance_id]["status"] = "stopping"
                self.id_to_cleanup_error.pop(instance_id, None)
                stop_event.clear()

        if not stop_owner:
            if not stop_event.wait(LLM_STOP_TOTAL_TIMEOUT):
                raise RuntimeError(f"Timed out waiting to stop LLM instance {instance_id}")
            with self.lock:
                state = self.id_to_state.get(instance_id)
                if state in {None, "stopped"}:
                    return resource_detail
                error = self.id_to_cleanup_error.get(instance_id, "cleanup did not finish")
            raise RuntimeError(f"Failed to stop LLM instance {instance_id}: {error}")

        actor_stop_succeeded = False
        actor_kill_succeeded = False
        stop_error = None
        try:
            try:
                actor_stop_ref = actor.stop_server.remote(LLM_PROCESS_STOP_TIMEOUT)
            except Exception as exc:
                actor_stop_ref = None
                stop_error = exc

            if actor_stop_ref is not None:
                try:
                    timeout = 5 if state == "launching" else LLM_ACTOR_STOP_TIMEOUT
                    ray.get(actor_stop_ref, timeout=timeout)
                    actor_stop_succeeded = True
                except Exception as exc:
                    stop_error = exc

            try:
                ray.kill(actor, no_restart=True)
                actor_kill_succeeded = True
            except Exception as exc:
                if stop_error is None:
                    stop_error = exc

            ray.get(
                self._cleanup_remote(instance_id, resource_detail["node_id"]),
                timeout=LLM_CLEANUP_TASK_TIMEOUT,
            )
            if not actor_stop_succeeded and not actor_kill_succeeded:
                raise RuntimeError(f"actor termination was not confirmed: {stop_error}")
        except Exception as cleanup_error:
            with self.lock:
                if self.id_to_instance_actor.get(instance_id) is actor:
                    self.id_to_state[instance_id] = "cleanup_pending"
                    self.id_to_resource_detail[instance_id]["status"] = "cleanup_pending"
                    self.id_to_cleanup_error[instance_id] = str(cleanup_error)
                    stop_event.set()
            raise RuntimeError(
                f"Failed to clean up LLM instance {instance_id}: {cleanup_error}"
            ) from cleanup_error

        with self.lock:
            if self.id_to_instance_actor.get(instance_id) is actor:
                self.id_to_state[instance_id] = "stopped"
                self.id_to_resource_detail[instance_id]["status"] = "stopped"
                self.id_to_cleanup_error.pop(instance_id, None)
                stop_event.set()
        return resource_detail

    def finalize_stopped_instance(self, instance_id: str) -> bool:
        with self.lock:
            state = self.id_to_state.get(instance_id)
            if state is None:
                return False
            if state != "stopped":
                raise RuntimeError(
                    f"Cannot forget LLM instance {instance_id} while state is {state}"
                )
            self.id_to_instance_actor.pop(instance_id, None)
            self.id_to_instance_addr.pop(instance_id, None)
            self.id_to_resource_detail.pop(instance_id, None)
            self.id_to_state.pop(instance_id, None)
            self.id_to_stop_event.pop(instance_id, None)
            self.id_to_cleanup_error.pop(instance_id, None)
            return True

    def stop_all_llm_instances(self):
        with self.lock:
            instance_ids = list(self.id_to_instance_actor)
        stopped = {}
        errors = {}
        if not instance_ids:
            return stopped, errors
        with ThreadPoolExecutor(max_workers=len(instance_ids)) as executor:
            futures = {
                executor.submit(self.stop_llm_instance, instance_id): instance_id
                for instance_id in instance_ids
            }
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    stopped[instance_id] = future.result()
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
        if expected_nodes:
            return stop_llm_owner_processes_on_cluster(
                self.owner_id,
                expected_nodes=expected_nodes,
            )
        return stop_llm_owner_processes_on_cluster(self.owner_id)
