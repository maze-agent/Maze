import subprocess
import argparse
import ctypes
import sys
import uvicorn
import os
import select
import shutil
import time
import logging
import signal
import json
import requests
import socket
from pathlib import Path
from urllib.parse import urlparse
from contextlib import contextmanager
from maze.core.worker.worker import Worker
from maze.core.application.spec import app_spec_from_payload, load_app_spec_file
from maze.core.scheduler.strategy import SchedulingAlgorithm
import asyncio
from maze.config.logging_config import setup_logging

try:
    import fcntl
except ImportError:  # pragma: no cover - head lifecycle is Linux-only
    fcntl = None

logger = logging.getLogger(__name__)
HEAD_CLEANUP_MAX_ATTEMPTS = 3
PLAYGROUND_STOP_TIMEOUT_SECONDS = 10.0
HEAD_START_TIMEOUT_SECONDS = 90.0
HEAD_STOP_TIMEOUT_SECONDS = 90.0
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_xdg_state_home = os.environ.get("XDG_STATE_HOME")
MAZE_RUNTIME_DIR = Path(
    os.environ.get("MAZE_RUNTIME_DIR")
    or ((_xdg_state_home and Path(_xdg_state_home) / "maze") or Path.home() / ".local" / "state" / "maze")
).expanduser()
HEAD_STATE_PATH = MAZE_RUNTIME_DIR / "head.json"
HEAD_LOG_DIR = MAZE_RUNTIME_DIR / "logs"


@contextmanager
def _head_state_lock():
    if fcntl is None:
        raise RuntimeError("Maze head lifecycle requires Linux file locking")
    HEAD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = HEAD_STATE_PATH.with_suffix(".lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _process_identity(pid: int) -> dict | None:
    """Return the Linux process identity used to guard against PID reuse."""
    if pid <= 1:
        return None
    proc_dir = Path("/proc") / str(pid)
    try:
        stat_text = (proc_dir / "stat").read_text(encoding="utf-8")
        stat_fields = stat_text.rsplit(")", 1)[1].split()
        process_state = stat_fields[0]
        start_time = stat_fields[19]
        command = [
            part.decode(errors="replace")
            for part in (proc_dir / "cmdline").read_bytes().split(b"\0")
            if part
        ]
    except (FileNotFoundError, IndexError, OSError):
        return None
    return {
        "pid": pid,
        "start_time": start_time,
        "process_state": process_state,
        "command": command,
    }


def _open_pidfd(pid: int) -> int:
    opener = getattr(os, "pidfd_open", None)
    if opener is not None:
        return opener(pid, 0)

    libc = ctypes.CDLL(None, use_errno=True)
    opener = getattr(libc, "pidfd_open", None)
    if opener is None:
        raise RuntimeError("This Linux system does not provide pidfd_open")
    opener.argtypes = (ctypes.c_int, ctypes.c_uint)
    opener.restype = ctypes.c_int
    descriptor = opener(pid, 0)
    if descriptor < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), pid)
    return descriptor


def _pidfd_send_signal(pidfd: int, signum: int) -> None:
    sender = getattr(signal, "pidfd_send_signal", None)
    if sender is not None:
        sender(pidfd, signum)
        return

    libc = ctypes.CDLL(None, use_errno=True)
    sender = getattr(libc, "pidfd_send_signal", None)
    if sender is None:
        raise RuntimeError("This Linux system does not provide pidfd_send_signal")
    sender.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    sender.restype = ctypes.c_int
    if sender(pidfd, signum, None, 0) < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), pidfd)


def _wait_for_pidfd(pidfd: int, timeout: float) -> bool:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    timeout_ms = min(int(max(0.0, timeout) * 1000), 2_147_483_647)
    return bool(poller.poll(timeout_ms))


def _check_pidfd_support() -> None:
    pidfd = _open_pidfd(os.getpid())
    try:
        _pidfd_send_signal(pidfd, 0)
    finally:
        os.close(pidfd)


def _read_head_state() -> dict | None:
    try:
        state = json.loads(HEAD_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(state, dict):
        raise ValueError("head state is not a JSON object")
    return state


def _head_process_matches(state: dict) -> tuple[bool, str]:
    try:
        pid = int(state["pid"])
        recorded_start_time = str(state["start_time"])
    except (KeyError, TypeError, ValueError):
        return False, "runtime state has no valid process identity"

    identity = _process_identity(pid)
    if identity is None or identity["process_state"] == "Z":
        return False, f"process {pid} is not running"
    if identity["start_time"] != recorded_start_time:
        return False, f"PID {pid} has been reused by another process"
    command = identity["command"]
    if "start" not in command or "--head" not in command:
        return False, f"PID {pid} is not a Maze head parent"
    return True, "running"


def _head_runtime_status() -> dict:
    try:
        state = _read_head_state()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "invalid", "reason": str(exc), "state_path": str(HEAD_STATE_PATH)}
    if state is None:
        return {"status": "stopped", "reason": "no runtime state", "state_path": str(HEAD_STATE_PATH)}
    matches, reason = _head_process_matches(state)
    return {
        **state,
        "status": "running" if matches else "stale",
        "reason": reason,
        "state_path": str(HEAD_STATE_PATH),
    }


def _remove_head_state_if_owned(pid: int, start_time: str) -> None:
    with _head_state_lock():
        try:
            state = _read_head_state()
        except (OSError, ValueError, json.JSONDecodeError):
            return
        if state is None:
            return
        if state.get("pid") == pid and str(state.get("start_time")) == start_time:
            HEAD_STATE_PATH.unlink(missing_ok=True)


def _ensure_no_running_head_unlocked() -> None:
    try:
        state = _read_head_state()
    except (OSError, ValueError, json.JSONDecodeError):
        HEAD_STATE_PATH.unlink(missing_ok=True)
        return
    if state is None:
        return
    matches, _reason = _head_process_matches(state)
    if matches:
        raise RuntimeError(
            f"Maze head is already running (pid={state['pid']}). Run `maze stop` first."
        )
    HEAD_STATE_PATH.unlink(missing_ok=True)


def _ensure_no_running_head() -> None:
    with _head_state_lock():
        _ensure_no_running_head_unlocked()


def _register_head_runtime(
    *,
    port: int,
    ray_head_port: int,
    playground: bool,
    playground_port: int,
    playground_backend_port: int | None,
    runtime_log: str | None,
) -> dict:
    try:
        _check_pidfd_support()
    except OSError as exc:
        raise RuntimeError(f"Maze head lifecycle requires pidfd support: {exc}") from exc
    identity = _process_identity(os.getpid())
    if identity is None:
        raise RuntimeError("Maze head lifecycle requires readable Linux /proc process metadata")

    state = {
        "version": 1,
        "pid": identity["pid"],
        "start_time": identity["start_time"],
        "started_at": time.time(),
        "port": port,
        "ray_head_port": ray_head_port,
        "playground": playground,
        "playground_port": playground_port,
        "playground_backend_port": (
            playground_backend_port or _default_playground_backend_port(playground_port)
        ),
        "detached": os.environ.get("MAZE_HEAD_DETACHED") == "1",
        "log": runtime_log,
    }
    with _head_state_lock():
        _ensure_no_running_head_unlocked()
        temporary_path = HEAD_STATE_PATH.with_name(f".{HEAD_STATE_PATH.name}.{os.getpid()}.tmp")
        temporary_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        temporary_path.chmod(0o600)
        temporary_path.replace(HEAD_STATE_PATH)
    return state


async def _cleanup_mapath_with_retries(
    mapath,
    max_attempts: int = HEAD_CLEANUP_MAX_ATTEMPTS,
) -> bool:
    max_attempts = max(1, int(max_attempts))
    request_shutdown = getattr(mapath, "request_scheduler_shutdown", None)
    if request_shutdown is not None:
        request_shutdown()

    for attempt in range(1, max_attempts + 1):
        try:
            cleanup_complete = await asyncio.to_thread(mapath.cleanup)
        except Exception:
            logger.exception(
                "Maze head cleanup attempt %s/%s failed",
                attempt,
                max_attempts,
            )
            continue
        if cleanup_complete:
            return True
        logger.warning(
            "Maze head cleanup attempt %s/%s remained incomplete",
            attempt,
            max_attempts,
        )

    logger.error(
        "Maze head cleanup did not complete after %s attempts; "
        "Scheduler-owned resources may require manual cleanup",
        max_attempts,
    )
    return False

def _default_playground_backend_port(frontend_port: int) -> int:
    return 3001 if frontend_port == 5173 else frontend_port + 1


def _ensure_unique_ports(service_ports: list[tuple[str, int]]):
    seen = {}
    for service_name, port in service_ports:
        if port in seen:
            raise RuntimeError(
                f"{service_name} port {port} conflicts with {seen[port]}. "
                "Choose different ports."
            )
        seen[port] = service_name


def _validate_head_ports(
    port: int,
    ray_head_port: int,
    playground: bool,
    playground_port: int,
    playground_backend_port: int | None,
) -> None:
    service_ports = [
        ("Maze core", port),
        ("Ray head", ray_head_port),
    ]
    if playground:
        service_ports.extend([
            (
                "Playground backend",
                playground_backend_port or _default_playground_backend_port(playground_port),
            ),
            ("Playground frontend", playground_port),
        ])
    _ensure_unique_ports(service_ports)
    for service_name, service_port in service_ports:
        _ensure_port_available(service_port, service_name)


async def _async_start_head(
    port: int,
    ray_head_port: int,
    strategy: str = "least-loaded",
    scheduling_algorithm: str = SchedulingAlgorithm.FCFS.value,
    playground: bool = False,
    playground_port: int = 5173,
    playground_backend_port: int | None = None,
):
    from maze.core.server import app as server_app, mapath

    _validate_head_ports(
        port,
        ray_head_port,
        playground,
        playground_port,
        playground_backend_port,
    )

    monitor_coroutine = None
    maintenance_coroutine = None
    server = None
    server_task = None
    playground_processes = []

    try:
        mapath.init(
            ray_head_port=ray_head_port,
            strategy=scheduling_algorithm,
            node_scheduling_policy=strategy,
        )
        monitor_coroutine = asyncio.create_task(mapath.monitor_coroutine())
        maintenance_coroutine = asyncio.create_task(mapath.maintenance_coroutine())

        server_config = uvicorn.Config(
            server_app,
            host="0.0.0.0",
            port=port,
            log_level="info",
        )
        server = uvicorn.Server(server_config)
        server_task = asyncio.create_task(server.serve())

        if playground:
            playground_processes = start_playground(
                core_port=port,
                frontend_port=playground_port,
                backend_port=playground_backend_port,
            )

        done, _pending = await asyncio.wait(
            (server_task, monitor_coroutine, maintenance_coroutine),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            error = task.exception()
            if error is not None:
                raise error
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down Maze head...")
    finally:
        pending_error = sys.exc_info()[1]
        if server is not None:
            server.should_exit = True

        for task in (server_task, monitor_coroutine, maintenance_coroutine):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *[
                task
                for task in (server_task, monitor_coroutine, maintenance_coroutine)
                if task is not None
            ],
            return_exceptions=True,
        )

        cleanup_complete = await _cleanup_mapath_with_retries(mapath)

        if playground_processes:
            stop_playground(playground_processes)

        if not cleanup_complete:
            cleanup_error = RuntimeError(
                "Maze head cleanup failed after "
                f"{HEAD_CLEANUP_MAX_ATTEMPTS} attempts; Scheduler-owned resources "
                "may still be running"
            )
            if pending_error is not None:
                raise cleanup_error from pending_error
            raise cleanup_error

def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            return sock.connect_ex((host, port)) == 0
    except (OSError, OverflowError):
        return False


def _ensure_port_available(port: int, service_name: str):
    if _port_in_use(port):
        raise RuntimeError(
            f"{service_name} port {port} is already in use. "
            "Stop the existing process or choose another port."
        )


def _start_playground_process(command, *, cwd: Path, env: dict[str, str]):
    process_options = {
        "cwd": str(cwd),
        "env": env,
    }
    if sys.platform == "win32":
        process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_options["start_new_session"] = True
    return subprocess.Popen(command, **process_options)


def _wait_for_playground_start(name: str, process, delay_seconds: float):
    time.sleep(delay_seconds)
    return_code = process.poll()
    if return_code is not None:
        raise RuntimeError(f"Playground {name} exited during startup with status {return_code}")


def start_playground(core_port: int = 8000, frontend_port: int = 5173, backend_port: int | None = None):
    processes = []
    backend_port = backend_port or _default_playground_backend_port(frontend_port)
    core_url = f"http://localhost:{core_port}"
    backend_url = f"http://localhost:{backend_port}"

    _ensure_port_available(backend_port, "Playground backend")
    _ensure_port_available(frontend_port, "Playground frontend")

    project_root = Path(__file__).parent.parent.parent
    backend_dir = project_root / "web" / "maze_playground" / "backend"
    frontend_dir = project_root / "web" / "maze_playground" / "frontend"

    print("\n" + "="*60)
    print("🎮 Starting Maze Playground...")
    print("="*60)

    try:
        if backend_dir.exists():
            print(f"🔧 starting playground backend ({backend_url})...")
            backend_env = {
                **os.environ,
                "PORT": str(backend_port),
                "MAZE_CORE_URL": core_url,
            }
            backend_process = _start_playground_process(
                ["node", "src/server.js"],
                cwd=backend_dir,
                env=backend_env,
            )
            processes.append(('backend', backend_process))
            _wait_for_playground_start("backend", backend_process, 2)
            print("✅ Playground backend started")

        if frontend_dir.exists():
            print(f"🎨 starting playground frontend (http://localhost:{frontend_port})...")
            npm_cmd = "npm.cmd" if sys.platform == 'win32' else "npm"
            frontend_env = {
                **os.environ,
                "VITE_MAZE_BACKEND_URL": backend_url,
            }
            frontend_process = _start_playground_process(
                [npm_cmd, "run", "dev", "--", "--host", "0.0.0.0", "--port", str(frontend_port)],
                cwd=frontend_dir,
                env=frontend_env,
            )
            processes.append(('frontend', frontend_process))
            _wait_for_playground_start("frontend", frontend_process, 3)
            print("✅ Playground frontend started")
    except Exception:
        stop_playground(processes)
        raise

    if processes:
        print("\n" + "="*60)
        print("🎉 Playground successfully started!")
        print("="*60)
        print(f"📱 frontend address: http://localhost:{frontend_port}")
        print(f"🔌 backend address: {backend_url}")
        print(f"🧠 core address: {core_url}")
        print(f"🎮 open browser to http://localhost:{frontend_port} to start using")
        print("="*60 + "\n")

    return processes

def stop_playground(processes):
    print("\n🛑 shutting down Playground...")
    for name, process in processes:
        try:
            if process.poll() is not None:
                print(f"✅ {name} already stopped")
                continue
            if sys.platform == 'win32':
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)],
                             capture_output=True)
            else:
                process_group_id = os.getpgid(process.pid)
                if process_group_id == process.pid:
                    os.killpg(process_group_id, signal.SIGTERM)
                else:
                    process.terminate()
            try:
                process.wait(timeout=PLAYGROUND_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                if sys.platform == 'win32':
                    process.kill()
                elif os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=PLAYGROUND_STOP_TIMEOUT_SECONDS)
            print(f"✅ {name} stopped")
        except Exception as e:
            print(f"⚠️  Failed to stop {name}: {e}")
    print("✅ Playground closed")

def start_head(
    port: int,
    ray_head_port: int,
    strategy: str = "least-loaded",
    scheduling_algorithm: str = SchedulingAlgorithm.FCFS.value,
    playground: bool = False,
    playground_port: int = 5173,
    playground_backend_port: int | None = None,
    runtime_log: str | None = None,
):
    runtime_log = os.environ.get("MAZE_HEAD_RUNTIME_LOG") or runtime_log
    state = _register_head_runtime(
        port=port,
        ray_head_port=ray_head_port,
        playground=playground,
        playground_port=playground_port,
        playground_backend_port=playground_backend_port,
        runtime_log=runtime_log,
    )
    previous_sigterm_handler = signal.getsignal(signal.SIGTERM)

    def interrupt_head(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt_head)
    try:
        asyncio.run(
            _async_start_head(
                port,
                ray_head_port,
                strategy,
                scheduling_algorithm,
                playground,
                playground_port,
                playground_backend_port,
            )
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm_handler)
        _remove_head_state_if_owned(state["pid"], state["start_time"])


def _detached_head_command(args) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "maze.cli.cli",
        "start",
        "--head",
        "--port",
        str(args.port),
        "--ray-head-port",
        str(args.ray_head_port),
        "--strategy",
        args.strategy,
        "--scheduling-algorithm",
        args.scheduling_algorithm,
        "--log-level",
        args.log_level,
    ]
    if args.log_file:
        command.extend(["--log-file", args.log_file])
    if args.playground:
        command.extend(["--playground", "--playground-port", str(args.playground_port)])
        if args.playground_backend_port is not None:
            command.extend(["--playground-backend-port", str(args.playground_backend_port)])
    return command


def _tail_log(path: Path, line_count: int = 30) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-line_count:])
    except OSError:
        return ""


def _wait_for_detached_head(process, port: int, log_path: Path) -> dict:
    deadline = time.monotonic() + HEAD_START_TIMEOUT_SECONDS
    last_error = "waiting for runtime registration"
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            tail = _tail_log(log_path)
            detail = f"\n{tail}" if tail else ""
            raise RuntimeError(
                f"Maze head exited during startup with status {return_code}. Log: {log_path}{detail}"
            )

        try:
            state = _read_head_state()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            state = None
        if state is not None and state.get("pid") == process.pid:
            matches, reason = _head_process_matches(state)
            if matches:
                try:
                    response = requests.get(
                        f"http://127.0.0.1:{port}/cluster/resources",
                        timeout=1,
                    )
                    if response.status_code < 400:
                        return state
                    last_error = f"HTTP {response.status_code}"
                except requests.RequestException as exc:
                    last_error = str(exc)
            else:
                last_error = reason
        time.sleep(0.2)

    raise RuntimeError(
        f"Maze head did not become ready within {HEAD_START_TIMEOUT_SECONDS:g}s: "
        f"{last_error}. Log: {log_path}"
    )


def start_head_detached(args) -> None:
    _ensure_no_running_head()
    _validate_head_ports(
        args.port,
        args.ray_head_port,
        args.playground,
        args.playground_port,
        args.playground_backend_port,
    )
    HEAD_LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = HEAD_LOG_DIR / f"head_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.log"
    environment = os.environ.copy()
    environment["MAZE_HEAD_DETACHED"] = "1"
    environment["MAZE_HEAD_RUNTIME_LOG"] = str(log_path)
    environment["PYTHONUNBUFFERED"] = "1"
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            _detached_head_command(args),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    child_identity = _process_identity(process.pid)
    try:
        state = _wait_for_detached_head(process, args.port, log_path)
    except BaseException:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=PLAYGROUND_STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        finally:
            if child_identity is not None:
                _remove_head_state_if_owned(process.pid, child_identity["start_time"])
        raise
    print(f"Maze head started (pid={state['pid']}, log={log_path})")


def stop_head(*, timeout: float = HEAD_STOP_TIMEOUT_SECONDS, force: bool = False) -> None:
    pidfd = None
    with _head_state_lock():
        try:
            state = _read_head_state()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            HEAD_STATE_PATH.unlink(missing_ok=True)
            print(f"Removed invalid Maze head runtime state: {exc}")
            return
        if state is None:
            print("Maze head is not running (no runtime state).")
            return
        try:
            pid = int(state["pid"])
            start_time = str(state["start_time"])
        except (KeyError, TypeError, ValueError):
            HEAD_STATE_PATH.unlink(missing_ok=True)
            print("Removed invalid Maze head runtime state: missing process identity")
            return

        try:
            pidfd = _open_pidfd(pid)
        except ProcessLookupError:
            HEAD_STATE_PATH.unlink(missing_ok=True)
            print(f"Removed stale Maze head runtime state: process {pid} is not running.")
            return
        except OSError as exc:
            raise RuntimeError(f"Unable to verify Maze head {pid} with pidfd: {exc}") from exc
        matches, reason = _head_process_matches(state)
        if not matches:
            os.close(pidfd)
            HEAD_STATE_PATH.unlink(missing_ok=True)
            print(f"Removed stale Maze head runtime state: {reason}.")
            return

        try:
            _pidfd_send_signal(pidfd, signal.SIGTERM)
        except ProcessLookupError:
            os.close(pidfd)
            HEAD_STATE_PATH.unlink(missing_ok=True)
            print(f"Maze head already stopped (pid={pid}).")
            return
        except OSError as exc:
            os.close(pidfd)
            raise RuntimeError(f"Unable to signal Maze head {pid}: {exc}") from exc
        except RuntimeError:
            os.close(pidfd)
            raise

    try:
        if _wait_for_pidfd(pidfd, timeout):
            _remove_head_state_if_owned(pid, start_time)
            print(f"Maze head stopped (pid={pid}).")
            return

        if not force:
            raise RuntimeError(
                f"Maze head {pid} did not stop within {timeout:g}s. "
                "Run `maze stop --force` to terminate the same verified process."
            )

        try:
            _pidfd_send_signal(pidfd, signal.SIGKILL)
        except ProcessLookupError:
            _remove_head_state_if_owned(pid, start_time)
            print(f"Maze head stopped (pid={pid}).")
            return
        except OSError as exc:
            raise RuntimeError(f"Unable to force-stop Maze head {pid}: {exc}") from exc
        if not _wait_for_pidfd(pidfd, PLAYGROUND_STOP_TIMEOUT_SECONDS):
            raise RuntimeError(f"Maze head {pid} remained alive after SIGKILL")
        _remove_head_state_if_owned(pid, start_time)
        print(f"Maze head force-stopped (pid={pid}).")
    finally:
        os.close(pidfd)


def start_worker(addr: str, agent: bool = False, heartbeat_interval: float = 10):
    try:
        return Worker.start_worker(
            addr,
            agent=agent,
            heartbeat_interval=heartbeat_interval,
        )
    finally:
        if agent:
            pending_error = sys.exc_info()[1]
            try:
                Worker.stop_worker()
            except Exception:
                if pending_error is None:
                    raise
                logger.exception(
                    "Failed to stop the local Ray worker while handling %s",
                    type(pending_error).__name__,
                )

def stop_worker():
    Worker.stop_worker()


def _directory_writable(path: Path) -> tuple[bool, str]:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.exists():
        return False, f"no existing parent for {path}"
    if path.exists() and not path.is_dir():
        return False, f"{path} is not a directory"
    writable = os.access(candidate, os.R_OK | os.W_OK | os.X_OK)
    return writable, f"{path} ({'writable' if writable else 'not writable'})"


def _http_health(url: str) -> tuple[bool, str]:
    try:
        response = requests.get(url, timeout=3)
    except requests.RequestException as exc:
        return False, str(exc)
    if response.status_code >= 400:
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    return True, f"HTTP {response.status_code}"


def _find_binary(name: str) -> str | None:
    adjacent = Path(sys.executable).parent / name
    if name == "ray":
        return str(adjacent) if adjacent.is_file() and os.access(adjacent, os.X_OK) else None
    resolved = shutil.which(name)
    if resolved:
        return resolved
    return str(adjacent) if adjacent.is_file() and os.access(adjacent, os.X_OK) else None


def _doctor_results(args) -> list[dict]:
    results = []

    def add(name: str, ok: bool, detail: str, *, required: bool = True) -> None:
        results.append({"name": name, "ok": bool(ok), "required": required, "detail": detail})

    supported_python = (3, 10) <= sys.version_info[:2] < (3, 14)
    add(
        "python",
        supported_python and Path(sys.executable).is_file(),
        f"{sys.executable} ({sys.version.split()[0]})",
    )
    try:
        _check_pidfd_support()
    except (OSError, RuntimeError) as exc:
        add("lifecycle:pidfd", False, str(exc))
    else:
        add(
            "lifecycle:pidfd",
            True,
            "pidfd process validation and signaling available",
        )
    for binary, required in (("ray", True), ("node", False), ("npm", False)):
        resolved = _find_binary(binary)
        add(f"binary:{binary}", resolved is not None, resolved or "not found on PATH", required=required)

    add("directory:package", (PROJECT_ROOT / "maze").is_dir(), str(PROJECT_ROOT / "maze"))
    runtime_writable, runtime_detail = _directory_writable(MAZE_RUNTIME_DIR)
    add("directory:runtime", runtime_writable, runtime_detail)
    for name, path in (
        ("playground-backend", PROJECT_ROOT / "web" / "maze_playground" / "backend"),
        ("playground-frontend", PROJECT_ROOT / "web" / "maze_playground" / "frontend"),
    ):
        add(f"directory:{name}", path.is_dir(), str(path), required=False)

    runtime = _head_runtime_status()
    add(
        "runtime:head",
        runtime["status"] in {"running", "stopped"},
        f"{runtime['status']}: {runtime['reason']}",
    )

    head_running = runtime["status"] == "running"

    def select_port(name: str, explicit, state_key: str, default: int) -> int:
        value = explicit
        if value is None:
            value = runtime.get(state_key, default) if head_running else default
        try:
            port = int(value)
        except (TypeError, ValueError):
            add(f"config:{name}-port", False, f"invalid port: {value!r}")
            return default
        if not 1 <= port <= 65535:
            add(f"config:{name}-port", False, f"port out of range: {port}")
            return default
        return port

    recorded_core_port = select_port("core", None, "port", 8000)
    ray_head_port = select_port("ray", args.ray_head_port, "ray_head_port", 6379)
    playground_port = select_port(
        "playground-frontend",
        args.playground_port,
        "playground_port",
        5173,
    )
    playground_backend_port = select_port(
        "playground-backend",
        args.playground_backend_port,
        "playground_backend_port",
        _default_playground_backend_port(playground_port),
    )

    configured_server_url = args.server_url or os.environ.get("MAZE_CORE_URL")
    server_url = configured_server_url or f"http://127.0.0.1:{recorded_core_port}"
    server_url = server_url if "://" in server_url else f"http://{server_url}"
    try:
        parsed = urlparse(server_url)
        core_host = parsed.hostname or "127.0.0.1"
        core_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        add("config:server-url", False, f"invalid server URL {server_url!r}: {exc}")
        parsed = urlparse(f"http://127.0.0.1:{recorded_core_port}")
        core_host = "127.0.0.1"
        core_port = recorded_core_port
    if parsed.scheme not in {"http", "https"}:
        add("config:server-url", False, f"unsupported URL scheme: {parsed.scheme or 'missing'}")
    port_specs = (
        ("core", core_host, core_port, True),
        ("ray", "127.0.0.1", ray_head_port, True),
        ("playground-backend", "127.0.0.1", playground_backend_port, False),
        ("playground-frontend", "127.0.0.1", playground_port, False),
    )
    for name, host, port, required in port_specs:
        is_open = _port_in_use(port, host)
        if name == "core" and configured_server_url is not None:
            ok = is_open
        elif name in {"core", "ray"}:
            ok = is_open if head_running else not is_open
        else:
            ok = True
        add(
            f"port:{name}",
            ok,
            f"{host}:{port} is {'listening' if is_open else 'free'}",
            required=required,
        )

    core_health_url = f"{server_url.rstrip('/')}/cluster/resources"
    core_ok, core_detail = _http_health(core_health_url)
    add(
        "http:core",
        core_ok,
        f"{core_health_url}: {core_detail}",
        required=head_running or configured_server_url is not None,
    )

    backend_health_url = f"http://127.0.0.1:{playground_backend_port}/health"
    backend_ok, backend_detail = _http_health(backend_health_url)
    add(
        "http:playground-backend",
        backend_ok,
        f"{backend_health_url}: {backend_detail}",
        required=False,
    )
    frontend_url = f"http://127.0.0.1:{playground_port}/"
    frontend_ok, frontend_detail = _http_health(frontend_url)
    add("http:playground-frontend", frontend_ok, f"{frontend_url}: {frontend_detail}", required=False)
    return results


def cmd_doctor(args) -> None:
    results = _doctor_results(args)
    failed_required = [item for item in results if item["required"] and not item["ok"]]
    failed_any = [item for item in results if not item["ok"]]
    ok = not failed_required and not (args.strict and failed_any)
    if args.json:
        print(json.dumps({"checks": results, "ok": ok}, indent=2))
    else:
        print("Maze doctor")
        for item in results:
            if item["ok"]:
                label = "ok"
            elif item["required"]:
                label = "error"
            else:
                label = "warn"
            print(f"[{label}] {item['name']}: {item['detail']}")
    if not ok:
        raise SystemExit(1)

def _server_url(args) -> str:
    return getattr(args, "server_url", None) or os.environ.get("MAZE_CORE_URL") or "http://localhost:8000"

def _request_core(method: str, server_url: str, path: str, **kwargs):
    url = server_url.rstrip("/") + path
    request_timeout = kwargs.pop("timeout", 10)
    try:
        response = requests.request(method, url, timeout=request_timeout, **kwargs)
    except requests.RequestException as exc:
        raise SystemExit(f"Failed to connect to Maze core at {server_url}: {exc}") from exc
    if response.status_code >= 400:
        raise SystemExit(f"Maze core request failed: {response.status_code} {response.text}")
    payload = response.json()
    if payload.get("status") not in (None, "success", "ready"):
        raise SystemExit(f"Maze core returned error: {payload}")
    return payload

def _print_payload(payload, as_json: bool = False):
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
        return
    print(json.dumps(payload, indent=2, ensure_ascii=False))

def _short_id(value, length: int = 12) -> str:
    if not value:
        return "-"
    text = str(value)
    return text if len(text) <= length else text[:length]

def _compact_value(value, max_length: int = 160) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= max_length else text[: max_length - 3] + "..."

def _error_summary(error) -> str:
    if not error:
        return ""
    if isinstance(error, str):
        return _compact_value(error)
    if isinstance(error, dict):
        label = error.get("error_type") or error.get("type") or error.get("kind") or error.get("origin")
        message = error.get("message") or error.get("error") or error.get("detail")
        if label and message:
            return _compact_value(f"{label}: {message}")
        return _compact_value(error)
    return _compact_value(error)

def _task_reason(task: dict) -> str:
    schedule_decision = task.get("schedule_decision") or {}
    return (
        task.get("pending_reason")
        or schedule_decision.get("reason")
        or _error_summary(task.get("last_error") or task.get("error"))
        or ""
    )

def _candidate_rejects(task: dict, limit: int = 2) -> list[str]:
    schedule_decision = task.get("schedule_decision") or {}
    candidates = schedule_decision.get("candidate_nodes") or []
    rejects = []
    for candidate in candidates:
        reasons = candidate.get("reject_reasons") or []
        if not reasons:
            continue
        node = candidate.get("node_ip") or _short_id(candidate.get("node_id"))
        rejects.append(f"{node}: {', '.join(map(str, reasons))}")
        if len(rejects) >= limit:
            break
    return rejects

def _print_cluster_resources(args):
    payload = _request_core("GET", _server_url(args), "/cluster/resources")
    if args.json:
        _print_payload(payload, True)
        return
    cluster = payload.get("cluster", {})
    print(f"Head: {cluster.get('head_node_ip')} ({cluster.get('head_node_id')})")
    print(f"Scheduling policy: {cluster.get('scheduling_policy', 'default')}")
    print("Registered nodes:")
    for node in cluster.get("nodes", []):
        resources = node.get("resources", {})
        cpu = resources.get("cpu", {})
        gpu = resources.get("gpu", {})
        print(
            f"  {node.get('role')} {node.get('node_ip')} "
            f"alive={node.get('alive')} stale={node.get('stale', False)} "
            f"cpu={cpu.get('available')}/{cpu.get('total')} "
            f"gpu={gpu.get('available_count')}/{gpu.get('total_count')} "
            f"running={node.get('running_task_count', 0)}"
        )
    unregistered = cluster.get("unregistered_ray_nodes") or []
    if unregistered:
        print("Unregistered Ray nodes:")
        for node in unregistered:
            print(f"  {node.get('node_ip')} ({node.get('node_id')})")

def _print_cluster_queues(args):
    payload = _request_core("GET", _server_url(args), "/cluster/queues")
    if args.json:
        _print_payload(payload, True)
        return
    queues = payload.get("queues", {})
    counts = queues.get("counts", {})
    print(
        "Queues: "
        f"ready={counts.get('ready', 0)} "
        f"pending={counts.get('pending', 0)} "
        f"retrying={counts.get('retrying', 0)} "
        f"running={counts.get('running', 0)} "
        f"total={counts.get('total_queued', 0)}"
    )
    stopped = queues.get("stopped_workflow_ids") or []
    if stopped:
        print(f"Stopped workflows: {len(stopped)}")
    for label, key in (("Pending", "pending_tasks"), ("Retrying", "retrying_tasks"), ("Running", "running_tasks")):
        items = queues.get(key) or []
        if not items:
            continue
        print(f"{label} tasks:")
        for task in items:
            selected = task.get("selected_node") or (task.get("schedule_decision") or {}).get("selected_node") or {}
            placement = selected.get("node_ip") or "-"
            gpu_id = selected.get("gpu_id")
            wait = task.get("retry_wait_seconds")
            timeout = task.get("timeout_seconds")
            suffix = []
            if gpu_id is not None:
                suffix.append(f"gpu={gpu_id}")
            if wait:
                suffix.append(f"wait={wait:.2f}s")
            if timeout is not None:
                suffix.append(f"timeout={timeout}s")
            reason = _task_reason(task)
            print(
                f"  {_short_id(task.get('task_id'))} run={_short_id(task.get('workflow_id'))} "
                f"attempt={task.get('attempt', 0)}/{task.get('max_retries', 0)} "
                f"node={placement} reason={reason or '-'}"
                + (f" {' '.join(suffix)}" if suffix else "")
            )
            for reject in _candidate_rejects(task):
                print(f"    reject: {reject}")

def _print_join_command(args):
    params = {"host": args.host} if args.host else None
    payload = _request_core("GET", _server_url(args), "/cluster/join_command", params=params)
    if args.json:
        _print_payload(payload, True)
        return
    print(payload.get("command"))
    print(f"agent: {payload.get('agent_command')}")

def _print_reconcile_workers(args):
    payload = _request_core(
        "POST",
        _server_url(args),
        "/cluster/reconcile_workers",
        json={"host": args.host} if args.host else {},
    )
    if args.json:
        _print_payload(payload, True)
        return
    print(f"Head URL: {payload.get('head_url')}")
    print(f"Ray head port: {payload.get('ray_head_port')}")
    print(f"Unregistered Ray nodes: {payload.get('unregistered_count', 0)}")
    commands = payload.get("recommended_commands", [])
    if not commands:
        print("All live Ray nodes are registered with Maze.")
        return
    for item in commands:
        print(f"  {item.get('node_ip')} ({item.get('node_id')}):")
        print(f"    register: {item.get('command')}")
        print(f"    agent:    {item.get('agent_command')}")

def _runs_list(args):
    params = {}
    if args.status:
        params["status"] = args.status
    if args.kind:
        params["kind"] = args.kind
    if args.limit is not None:
        params["limit"] = args.limit
    payload = _request_core("GET", _server_url(args), "/runs", params=params or None)
    if args.json:
        _print_payload(payload, True)
        return
    for run in payload.get("runs", []):
        progress = run.get("progress") or run.get("task_counts") or {}
        progress_text = ""
        if isinstance(progress, dict):
            completed = progress.get("completed", 0)
            total = progress.get("total", 0)
            failed = progress.get("failed", 0)
            running = progress.get("running", 0)
            progress_text = f" tasks={completed}/{total}"
            if running:
                progress_text += f" running={running}"
            if failed:
                progress_text += f" failed={failed}"
        error = _error_summary(run.get("error_summary") or run.get("failure_reason"))
        print(
            f"{run.get('run_id')} {run.get('kind') or run.get('run_type')} "
            f"{run.get('status')} events={run.get('event_count', 0)}{progress_text}"
            + (f" error={error}" if error else "")
        )

def _runs_show(args):
    payload = _request_core("GET", _server_url(args), f"/runs/{args.run_id}")
    if args.json:
        _print_payload(payload, True)
        return
    run = payload.get("run", {})
    print(f"Run: {run.get('run_id')}")
    print(f"Type: {run.get('kind') or run.get('run_type')}")
    print(f"Status: {run.get('status')}")
    print(f"Progress: {run.get('progress') or run.get('task_counts')}")
    if run.get("error_summary") or run.get("failure_reason"):
        print(f"Error: {_error_summary(run.get('error_summary') or run.get('failure_reason'))}")
    tasks = (run.get("task_nodes") or {}).values()
    for task in tasks:
        selected = task.get("selected_node") or {}
        reason = _task_reason(task)
        extra = []
        if selected.get("gpu_id") is not None:
            extra.append(f"gpu={selected.get('gpu_id')}")
        if task.get("duration_seconds") is not None:
            extra.append(f"duration={task.get('duration_seconds')}s")
        if task.get("timeout_seconds") is not None:
            extra.append(f"timeout={task.get('timeout_seconds')}s")
        if reason:
            extra.append(f"reason={reason}")
        print(
            f"  {task.get('task_id')} {task.get('task_name')} {task.get('status')} "
            f"node={selected.get('node_ip')}"
            + (f" {' '.join(extra)}" if extra else "")
        )
        for reject in _candidate_rejects(task):
            print(f"    reject: {reject}")

def _runs_events(args):
    params = {"after": args.after} if args.after is not None else None
    payload = _request_core("GET", _server_url(args), f"/runs/{args.run_id}/events", params=params)
    if args.json:
        _print_payload(payload, True)
        return
    for event in payload.get("events", []):
        data = event.get("data") or {}
        summary = (
            data.get("pending_reason")
            or _error_summary(data.get("error") or data.get("result"))
            or data.get("reason")
            or ""
        )
        print(
            f"{event.get('seq')} {event.get('type')} {event.get('timestamp')}"
            + (f" {summary}" if summary else "")
        )

def _runs_logs(args):
    params = {}
    if args.tail is not None:
        params["tail"] = args.tail
    if args.task_id:
        params["task_id"] = args.task_id
    payload = _request_core("GET", _server_url(args), f"/runs/{args.run_id}/logs", params=params or None)
    if args.json:
        _print_payload(payload, True)
        return
    for line in payload.get("lines", []):
        prefix = line.get("stream") or "log"
        task_id = _short_id(line.get("task_id"))
        print(f"[{prefix} {task_id}] {line.get('message', '')}")

def _runs_retry(args):
    payload = _request_core(
        "POST",
        _server_url(args),
        f"/runs/{args.run_id}/retry",
        json={
            "workspace_dir": args.workspace_dir,
            "artifact_mode": not args.no_artifacts,
            "timeout_seconds": args.timeout_seconds,
            "tags": args.tag,
        },
    )
    if args.json:
        _print_payload(payload, True)
        return
    print(f"Run: {payload.get('run_id')}")
    print(f"Retried from: {payload.get('retried_from_run_id')}")

def _artifacts_list(args):
    payload = _request_core("GET", _server_url(args), f"/runs/{args.run_id}/artifacts")
    if args.json:
        _print_payload(payload, True)
        return
    for artifact in payload.get("artifacts", []):
        artifact_id = artifact.get("artifact_id") or (
            f"sha256:{artifact.get('sha256')}" if artifact.get("sha256") else "-"
        )
        print(
            f"{_short_id(artifact.get('task_id') or artifact.get('producer_task_id'))} "
            f"{artifact.get('path')} {artifact.get('size')} bytes "
            f"{_short_id(artifact_id, 20)}"
        )

def _run_app(args):
    try:
        spec = load_app_spec_file(args.spec)
    except Exception as exc:
        raise SystemExit(f"Invalid app spec: {exc}") from exc

    payload = {
        "spec": spec,
        "source_path": str(Path(args.spec).expanduser().resolve()),
        "workspace_dir": args.workspace_dir,
        "artifact_mode": not args.no_artifacts,
    }
    if args.timeout_seconds is not None:
        payload["timeout_seconds"] = args.timeout_seconds
    if args.tag:
        payload["tags"] = args.tag
    response = _request_core("POST", _server_url(args), "/apps/run", json=payload)
    if args.json:
        _print_payload(response, True)
    else:
        print(f"Run: {response.get('run_id')}")
        print(f"Workflow: {response.get('workflow_id')}")
        print(f"App: {response.get('spec', {}).get('name')}")

    if not args.wait:
        return

    terminal_statuses = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
    deadline = None if args.wait_timeout is None else time.time() + args.wait_timeout
    while True:
        run_payload = _request_core("GET", _server_url(args), f"/runs/{response['run_id']}")
        run = run_payload.get("run", {})
        if run.get("status") in terminal_statuses:
            if args.json:
                _print_payload(run_payload, True)
            else:
                print(f"Status: {run.get('status')}")
                if run.get("error_summary"):
                    print(f"Error: {_error_summary(run.get('error_summary'))}")
            if run.get("status") != "succeeded":
                raise SystemExit(1)
            return
        if deadline is not None and time.time() >= deadline:
            raise SystemExit(f"Timed out waiting for run: {response['run_id']}")
        time.sleep(args.poll_interval)

def _validate_app(args):
    try:
        spec = load_app_spec_file(args.spec)
        spec = app_spec_from_payload(
            spec,
            source_path=str(Path(args.spec).expanduser().resolve()),
            overrides={"workspace": args.workspace_dir},
        )
    except Exception as exc:
        raise SystemExit(f"Invalid app spec: {exc}") from exc
    _print_payload({"status": "success", "spec": spec}, args.json)


def _model_serve(args):
    payload = {
        "model": args.model,
        "backend": args.backend,
        "cpu_nums": args.cpu,
        "memory_mib": args.memory,
        "gpu_nums": 1,
        "gpu_mem": args.gpu_memory,
    }
    if args.gpu_memory_utilization is not None:
        payload["gpu_memory_utilization"] = args.gpu_memory_utilization
    if args.max_model_len is not None:
        payload["max_model_len"] = args.max_model_len
    response = _request_core(
        "POST",
        _server_url(args),
        "/start_llm_instance",
        json=payload,
        timeout=args.timeout,
    )
    if response.get("backend") != args.backend:
        raise SystemExit(
            f"Maze core started backend {response.get('backend')!r}, expected {args.backend!r}"
        )
    if args.json:
        _print_payload(response, True)
        return
    print(f"Instance: {response['instance_id']}")
    print(f"Model: {response['model']}")
    print(f"Backend: {response['backend']}")
    print(f"Endpoint: {response['endpoint']}")


def _model_stop(args):
    response = _request_core(
        "POST",
        _server_url(args),
        "/stop_llm_instance",
        json={"instance_id": args.instance_id},
        timeout=args.timeout,
    )
    if args.json:
        _print_payload(response, True)
        return
    print(f"Stopped instance: {args.instance_id}")


def _format_status(metrics: dict, runs: list) -> str:
    lines = []
    uptime = metrics.get("uptime_sec", 0)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    lines.append("=== Maze Cluster Status ===")
    lines.append(f"Uptime: {int(h)}h {int(m)}m {int(s)}s")
    lines.append("")

    wf = metrics.get("workflows") or {}
    lines.append(
        f"Workflows: {wf.get('created_total', 0)} ever created"
        f" | {wf.get('in_memory_not_submitted', 0)} created-not-submitted"
    )

    sr = metrics.get("static_runs") or {}
    by = sr.get("by_status") or {}
    lines.append(f"Static Runs: {sr.get('total', 0)} total | {sr.get('in_memory', 0)} in-memory")
    for status in (
        "submitted",
        "running",
        "succeeded",
        "failed",
        "canceled",
        "interrupted",
        "timed_out",
    ):
        if by.get(status, 0):
            lines.append(f"  - {status}: {by.get(status, 0)}")
    lines.append("")

    tasks = metrics.get("tasks") or {}
    tby = tasks.get("by_status") or {}
    lines.append(
        f"Tasks: {tasks.get('total_finished', 0)} finished | "
        f"{tby.get('running', 0)} running | "
        f"{tby.get('succeeded', 0)} succeeded | "
        f"{tby.get('failed', 0)} failed"
    )

    tokens = metrics.get("tokens") or {}
    if tokens.get("in") or tokens.get("out"):
        lines.append("")
        lines.append(
            f"Tokens: {tokens.get('in', 0)} in / {tokens.get('out', 0)} out"
            f" (cost ${tokens.get('cost_usd', 0):.4f})"
        )
        for model, m in (tokens.get("by_model") or {}).items():
            lines.append(
                f"  - {model}: {m.get('tokens_in', 0)} in / "
                f"{m.get('tokens_out', 0)} out / {m.get('calls', 0)} calls"
            )

    if runs:
        lines.append("")
        lines.append("=== Active Runs ===")
        lines.append(f"{'run_id':<38} {'status':<10} {'progress':<10} {'updated':<12}")
        for r in runs:
            counts = r.get("task_counts") or {}
            done = counts.get("done", 0)
            total = counts.get("total", 0) or r.get("task_total", 0)
            updated = r.get("updated_time")
            ago = ""
            if updated:
                ago = f"{int(time.time() - float(updated))}s ago"
            lines.append(
                f"{r.get('run_id', ''):<38} {r.get('status', ''):<10} {done}/{total:<8} {ago:<12}"
            )

    return "\n".join(lines)


def cmd_status(addr: str, watch: bool, status_filter: str | None, run_id: str | None):
    """Print cluster status by querying the head's HTTP API."""
    while True:
        try:
            metrics = requests.get(f"{addr}/v1/metrics", timeout=5).json()
            if run_id:
                snap = requests.get(f"{addr}/v1/runs/{run_id}/snapshot", timeout=5).json()
                cur = requests.get(f"{addr}/v1/runs/{run_id}/current-task", timeout=5).json()
                print(f"=== Run {run_id} ===")
                print(f"Status: {snap.get('status')}")
                print(f"Tasks: {snap.get('task_counts')}")
                print(f"Metrics: {snap.get('metrics')}")
                print(f"Currently running: {cur.get('running')}")
            else:
                params = {"limit": 20}
                if status_filter:
                    params["status"] = status_filter
                runs_payload = requests.get(f"{addr}/v1/runs", params=params, timeout=5).json()
                runs = runs_payload.get("runs") if isinstance(runs_payload, dict) else runs_payload
                active_runs = [r for r in (runs or []) if r.get("status") in ("submitted", "running")]
                print(_format_status(metrics, active_runs if not status_filter else (runs or [])))
            if not watch:
                return
            time.sleep(2)
            print("\n" * 2)
        except Exception as e:
            print(f"Failed to fetch status from {addr}: {e}")
            runtime = _head_runtime_status()
            print(f"Local Maze head: {runtime['status']} ({runtime['reason']})")
            if not watch:
                sys.exit(1)
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser(prog="maze", description="Maze distributed task runner")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Available commands")

    # === start subcommand ===
    start_parser = subparsers.add_parser("start", help="Start a Maze node")
    start_group = start_parser.add_mutually_exclusive_group(required=True)
    start_group.add_argument("--head", action="store_true", help="Start as head node")
    start_group.add_argument("--worker", action="store_true", help="Start as worker node")

    start_parser.add_argument("--port", type=int, metavar="PORT", help="Head HTTP port (default: 8000)",default=8000)
    start_parser.add_argument("--strategy", metavar="STRATEGY", help="Node placement strategy",default="least-loaded")
    start_parser.add_argument(
        "--scheduling-algorithm",
        choices=[item.value for item in SchedulingAlgorithm],
        default=SchedulingAlgorithm.FCFS.value,
        help="Task queue scheduling algorithm",
    )
    start_parser.add_argument("--ray-head-port", type=int, metavar="RAY HEAD PORT", help="Ray head port (default: 6379)",default=6379)
    start_parser.add_argument("--addr", metavar="ADDR", help="Address of head node (required if --worker)")
    start_parser.add_argument("--playground", action="store_true", help="Start Maze Playground visual interface (only applicable to --head)")
    start_parser.add_argument("--playground-port", type=int, default=5173, help="Port for the Playground web UI")
    start_parser.add_argument("--playground-backend-port", type=int, default=None, help="Port for the Playground backend API (default: 3001, or --playground-port + 1 when the UI port is changed)")
    start_parser.add_argument("--agent", action="store_true", help="Keep worker alive and periodically re-register with Maze core")
    start_parser.add_argument("--heartbeat-interval", type=float, default=10, help="Worker agent registration interval in seconds")
    start_parser.add_argument("--detach", action="store_true", help="Run the head in the background")
    start_parser.add_argument("--log-level", metavar="LOG LEVEL", help="Set log level",default="INFO",choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    start_parser.add_argument("--log-file", metavar="LOG FILE", help="Set log file",default=None)

    # === stop subcommand ===
    stop_parser = subparsers.add_parser("stop", help="Stop the recorded local Maze head")
    stop_parser.add_argument("--worker", action="store_true", help="Stop the local Ray worker instead")
    stop_parser.add_argument(
        "--timeout",
        type=float,
        default=HEAD_STOP_TIMEOUT_SECONDS,
        help="Seconds to wait for graceful head shutdown",
    )
    stop_parser.add_argument(
        "--force",
        action="store_true",
        help="Send SIGKILL if the verified head does not stop before the timeout",
    )
    stop_parser.add_argument("--log-level", metavar="LOG LEVEL", help="Set log level",default="INFO",choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    stop_parser.add_argument("--log-file", metavar="LOG FILE", help="Set log file",default=None)

    doctor_parser = subparsers.add_parser("doctor", help="Check the local Maze environment and services")
    doctor_parser.add_argument(
        "--server-url",
        default=None,
        help="Maze head HTTP address (default: active local head or MAZE_CORE_URL)",
    )
    doctor_parser.add_argument("--ray-head-port", type=int, default=None)
    doctor_parser.add_argument("--playground-port", type=int, default=None)
    doctor_parser.add_argument("--playground-backend-port", type=int, default=None)
    doctor_parser.add_argument("--json", action="store_true", help="Print structured results")
    doctor_parser.add_argument("--strict", action="store_true", help="Treat optional check failures as errors")

    cluster_parser = subparsers.add_parser("cluster", help="Inspect and operate the Maze cluster")
    cluster_subparsers = cluster_parser.add_subparsers(dest="cluster_command", required=True)
    for subcommand in ("resources", "queues", "join-command", "reconcile-workers"):
        sub = cluster_subparsers.add_parser(subcommand)
        sub.add_argument("--server-url", default=os.environ.get("MAZE_CORE_URL", "http://localhost:8000"))
        sub.add_argument("--json", action="store_true", help="Print raw JSON")
        if subcommand in {"join-command", "reconcile-workers"}:
            sub.add_argument("--host", default=None, help="Host/IP to place in returned worker command")

    runs_parser = subparsers.add_parser("runs", help="Inspect Maze runs")
    runs_subparsers = runs_parser.add_subparsers(dest="runs_command", required=True)
    runs_list = runs_subparsers.add_parser("list")
    runs_list.add_argument("--server-url", default=os.environ.get("MAZE_CORE_URL", "http://localhost:8000"))
    runs_list.add_argument("--status", default=None)
    runs_list.add_argument("--kind", default=None)
    runs_list.add_argument("--limit", type=int, default=None)
    runs_list.add_argument("--json", action="store_true")
    runs_show = runs_subparsers.add_parser("show")
    runs_show.add_argument("run_id")
    runs_show.add_argument("--server-url", default=os.environ.get("MAZE_CORE_URL", "http://localhost:8000"))
    runs_show.add_argument("--json", action="store_true")
    runs_events = runs_subparsers.add_parser("events")
    runs_events.add_argument("run_id")
    runs_events.add_argument("--after", type=int, default=None)
    runs_events.add_argument("--server-url", default=os.environ.get("MAZE_CORE_URL", "http://localhost:8000"))
    runs_events.add_argument("--json", action="store_true")
    runs_logs = runs_subparsers.add_parser("logs")
    runs_logs.add_argument("run_id")
    runs_logs.add_argument("--tail", type=int, default=500)
    runs_logs.add_argument("--task-id", default=None)
    runs_logs.add_argument("--server-url", default=os.environ.get("MAZE_CORE_URL", "http://localhost:8000"))
    runs_logs.add_argument("--json", action="store_true")
    runs_retry = runs_subparsers.add_parser("retry")
    runs_retry.add_argument("run_id")
    runs_retry.add_argument("--workspace-dir", default=None)
    runs_retry.add_argument("--no-artifacts", action="store_true")
    runs_retry.add_argument("--timeout-seconds", type=float, default=None)
    runs_retry.add_argument("--tag", action="append", default=[])
    runs_retry.add_argument("--server-url", default=os.environ.get("MAZE_CORE_URL", "http://localhost:8000"))
    runs_retry.add_argument("--json", action="store_true")

    artifacts_parser = subparsers.add_parser("artifacts", help="Inspect Maze artifacts")
    artifacts_subparsers = artifacts_parser.add_subparsers(dest="artifacts_command", required=True)
    artifacts_list = artifacts_subparsers.add_parser("list")
    artifacts_list.add_argument("run_id")
    artifacts_list.add_argument("--server-url", default=os.environ.get("MAZE_CORE_URL", "http://localhost:8000"))
    artifacts_list.add_argument("--json", action="store_true")

    run_parser = subparsers.add_parser("run", help="Run a Maze application spec")
    run_parser.add_argument("spec", help="Path to maze.yaml/maze.json")
    run_parser.add_argument("--server-url", default=os.environ.get("MAZE_CORE_URL", "http://localhost:8000"))
    run_parser.add_argument("--workspace-dir", default=None, help="Override spec workspace directory")
    run_parser.add_argument("--no-artifacts", action="store_true", help="Disable head HTTP artifact transport")
    run_parser.add_argument("--timeout-seconds", type=float, default=None, help="Override run timeout")
    run_parser.add_argument("--tag", action="append", default=[], help="Add a run tag")
    run_parser.add_argument("--wait", action="store_true", help="Wait until the run reaches a terminal status")
    run_parser.add_argument("--wait-timeout", type=float, default=None, help="Maximum seconds to wait")
    run_parser.add_argument("--poll-interval", type=float, default=0.5, help="Wait polling interval")
    run_parser.add_argument("--json", action="store_true")

    app_parser = subparsers.add_parser("app", help="Validate Maze application specs")
    app_subparsers = app_parser.add_subparsers(dest="app_command", required=True)
    app_validate = app_subparsers.add_parser("validate")
    app_validate.add_argument("spec")
    app_validate.add_argument("--workspace-dir", default=None)
    app_validate.add_argument("--json", action="store_true")

    model_parser = subparsers.add_parser("model", help="Serve local models through Maze")
    model_subparsers = model_parser.add_subparsers(dest="model_command", required=True)
    model_serve = model_subparsers.add_parser("serve")
    model_serve.add_argument("model", help="Local chat model path or model identifier")
    model_serve.add_argument("--backend", choices=("vllm", "transformers"), default="vllm")
    model_serve.add_argument("--server-url", default=os.environ.get("MAZE_CORE_URL", "http://localhost:8000"))
    model_serve.add_argument("--cpu", type=int, default=5)
    model_serve.add_argument("--memory", type=int, default=1024, help="CPU memory reservation in MiB")
    model_serve.add_argument("--gpu-memory", type=int, default=0, help="GPU memory reservation in MiB")
    model_serve.add_argument("--gpu-memory-utilization", type=float, default=None)
    model_serve.add_argument("--max-model-len", type=int, default=None)
    model_serve.add_argument("--timeout", type=float, default=600)
    model_serve.add_argument("--json", action="store_true")

    model_stop = model_subparsers.add_parser("stop")
    model_stop.add_argument("instance_id")
    model_stop.add_argument("--server-url", default=os.environ.get("MAZE_CORE_URL", "http://localhost:8000"))
    model_stop.add_argument("--timeout", type=float, default=60)
    model_stop.add_argument("--json", action="store_true")

    status_parser = subparsers.add_parser("status", help="Show cluster status (/v1/metrics)")
    status_parser.add_argument(
        "--addr",
        metavar="ADDR",
        default=os.environ.get("MAZE_CORE_URL", "http://localhost:8000"),
        help="Head HTTP address (default: MAZE_CORE_URL or http://localhost:8000)",
    )
    status_parser.add_argument("--watch", action="store_true", help="Refresh every 2s")
    status_parser.add_argument(
        "--status",
        dest="status_filter",
        metavar="STATUS",
        choices=["submitted", "running", "succeeded", "failed", "canceled", "interrupted", "timed_out"],
        help="Filter runs by status",
    )
    status_parser.add_argument("--run-id", metavar="RUN_ID", help="Show details of a specific run")
    status_parser.add_argument(
        "--log-level",
        metavar="LOG LEVEL",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    status_parser.add_argument("--log-file", metavar="LOG FILE", default=None)

    # Parse args
    args = parser.parse_args()

    setup_logging(getattr(args, "log_level", "INFO"), getattr(args, "log_file", None))
    if args.command == "start":
        if args.head:
            try:
                if args.detach:
                    start_head_detached(args)
                else:
                    start_head(
                        args.port,
                        args.ray_head_port,
                        args.strategy,
                        args.scheduling_algorithm,
                        playground=args.playground,
                        playground_port=args.playground_port,
                        playground_backend_port=args.playground_backend_port,
                        runtime_log=args.log_file,
                    )
            except RuntimeError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
        elif args.worker:
            if args.addr is None:
                parser.error("--addr is required when using --worker")
            if args.detach:
                parser.error("--detach is only supported with --head")
            if hasattr(args, 'playground') and args.playground:
                print("⚠️  Warning: --playground parameter is only applicable to head node, will be ignored")
            start_worker(args.addr, agent=args.agent, heartbeat_interval=args.heartbeat_interval)
    elif args.command == "stop":
        if args.worker:
            if args.force:
                parser.error("--force only applies when stopping a Maze head")
            stop_worker()
        else:
            try:
                stop_head(timeout=args.timeout, force=args.force)
            except RuntimeError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "cluster":
        if args.cluster_command == "resources":
            _print_cluster_resources(args)
        elif args.cluster_command == "queues":
            _print_cluster_queues(args)
        elif args.cluster_command == "join-command":
            _print_join_command(args)
        elif args.cluster_command == "reconcile-workers":
            _print_reconcile_workers(args)
    elif args.command == "runs":
        if args.runs_command == "list":
            _runs_list(args)
        elif args.runs_command == "show":
            _runs_show(args)
        elif args.runs_command == "events":
            _runs_events(args)
        elif args.runs_command == "logs":
            _runs_logs(args)
        elif args.runs_command == "retry":
            _runs_retry(args)
    elif args.command == "artifacts":
        if args.artifacts_command == "list":
            _artifacts_list(args)
    elif args.command == "run":
        _run_app(args)
    elif args.command == "app":
        if args.app_command == "validate":
            _validate_app(args)
    elif args.command == "model":
        if args.model_command == "serve":
            _model_serve(args)
        elif args.model_command == "stop":
            _model_stop(args)
    elif args.command == "status":
        cmd_status(args.addr, args.watch, args.status_filter, args.run_id)
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
