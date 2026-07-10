from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOGS_DIR = PROJECT_ROOT / "logs"
WORKSPACES_DIR = PROJECT_ROOT / "workspaces"
DEFAULT_WORKSPACE_DIR = WORKSPACES_DIR / "default"
LEGACY_WORKSPACE_DIR = PROJECT_ROOT / "workspace"
MODEL_DIR = PROJECT_ROOT / "model_cache"
RUNTIME_CONFIG_PATH = PROJECT_ROOT / ".maze_runtime.json"
CONDA_PREFIX = Path(os.environ.get("MAZE_CONDA_PREFIX") or "/root/miniconda3/envs/maze")
CONDA_SH = Path("/root/miniconda3/etc/profile.d/conda.sh")


SERVICES = {
    "core": {
        "pid": LOGS_DIR / "maze_core_head.pid",
        "port": 8000,
        "log_prefix": "maze_core_head",
    },
    "backend": {
        "pid": LOGS_DIR / "maze_playground_backend.pid",
        "port": 3001,
        "log_prefix": "maze_playground_backend",
    },
    "frontend": {
        "pid": LOGS_DIR / "maze_playground_frontend.pid",
        "port": 5173,
        "log_prefix": "maze_playground_frontend",
    },
}


def _python_bin() -> Path:
    candidate = CONDA_PREFIX / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


def _node_bin(name: str) -> Path:
    candidate = CONDA_PREFIX / "bin" / name
    return candidate if candidate.exists() else Path(name)


def _configured_model_dir() -> Path:
    try:
        config = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
        if config.get("model_dir"):
            return Path(config["model_dir"]).expanduser().resolve()
    except Exception:
        pass
    return MODEL_DIR


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    current_path = env.get("PATH", "")
    conda_bin = str(CONDA_PREFIX / "bin")
    condabin = str(CONDA_PREFIX.parent.parent / "condabin")
    env["PATH"] = f"{conda_bin}:{condabin}:{current_path}"
    env["PYTHONPATH"] = f"{PROJECT_ROOT}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(PROJECT_ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    env["MAZE_WORKSPACE_ROOT_DIR"] = str(WORKSPACES_DIR)
    env["MAZE_WORKSPACES_DIR"] = str(WORKSPACES_DIR)
    env["MAZE_DEFAULT_WORKSPACE_DIR"] = str(DEFAULT_WORKSPACE_DIR)
    env["MAZE_SYSTEM_CATALOG_DIR"] = str(PROJECT_ROOT / "system_catalog")
    env["MAZE_MODEL_DIR"] = str(_configured_model_dir())
    return env


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _is_process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_pid(path: Path, *, timeout: float = 8.0) -> None:
    pid = _read_pid(path)
    if not pid or not _is_process_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_process_alive(pid):
            return
        time.sleep(0.2)
    with contextlib_suppress():
        os.kill(pid, signal.SIGKILL)


class contextlib_suppress:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return True


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _default_head_host() -> str:
    env_host = os.environ.get("MAZE_HEAD_HOST", "").strip()
    if env_host:
        return env_host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def _latest_log(prefix: str) -> Path | None:
    candidates = sorted(LOGS_DIR.glob(f"{prefix}_*.log"), key=lambda item: item.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _tail(path: Path | None, lines: int = 60) -> str:
    if path is None or not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def _start_process(name: str, command: list[str], cwd: Path, env: dict[str, str]) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"{SERVICES[name]['log_prefix']}_{time.strftime('%Y%m%d_%H%M%S')}.log"
    log_handle = log_path.open("ab")
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    SERVICES[name]["pid"].write_text(f"{process.pid}\n", encoding="utf-8")
    log_handle.close()
    return log_path


def _wait_http(url: str, *, timeout: float, label: str, log_path: Path | None = None) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=3)
            if response.status_code < 400:
                try:
                    return response.json()
                except ValueError:
                    return {"ok": True, "text": response.text[:200]}
            last_error = f"{response.status_code} {response.text[:300]}"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(1)
    tail = _tail(log_path)
    raise SystemExit(f"{label} did not become ready: {last_error}\n{tail}")


def _run_ray_stop() -> None:
    ray_bin = shutil.which("ray") or str(CONDA_PREFIX / "bin" / "ray")
    subprocess.run([ray_bin, "stop", "--force"], check=False, env=_base_env())


def cmd_up(args: argparse.Namespace) -> None:
    env = _base_env()
    head_host = _default_head_host()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    if args.clean_ray:
        _run_ray_stop()

    for service in ("core", "backend", "frontend"):
        if service == "backend" and not args.playground:
            continue
        if service == "frontend" and not args.playground:
            continue
        _kill_pid(SERVICES[service]["pid"])

    core_log = _start_process(
        "core",
        [
            str(_python_bin()),
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
        ],
        PROJECT_ROOT,
        env,
    )
    _wait_http(f"http://127.0.0.1:{args.port}/cluster/resources", timeout=args.timeout, label="Maze core", log_path=core_log)
    print(f"core ready: http://127.0.0.1:{args.port} log={core_log}")

    if args.playground:
        backend_env = {
            **env,
            "MAZE_CORE_URL": f"http://127.0.0.1:{args.port}",
            "MAZE_HEAD_HOST": head_host,
            "PYTHON_BIN": str(_python_bin()),
            "MAZE_CONDA_PREFIX": str(CONDA_PREFIX),
            "NODE_ENV": "development",
        }
        backend_log = _start_process(
            "backend",
            [str(_node_bin("node")), "src/server.js"],
            PROJECT_ROOT / "web" / "maze_playground" / "backend",
            backend_env,
        )
        _wait_http("http://127.0.0.1:3001/health", timeout=args.timeout, label="Playground backend", log_path=backend_log)
        print(f"backend ready: http://127.0.0.1:3001 log={backend_log}")

        frontend_env = {**env, "VITE_API_BASE_URL": "http://127.0.0.1:3001"}
        frontend_log = _start_process(
            "frontend",
            [str(_node_bin("npm")), "run", "dev", "--", "--host", "0.0.0.0"],
            PROJECT_ROOT / "web" / "maze_playground" / "frontend",
            frontend_env,
        )
        _wait_http("http://127.0.0.1:5173/", timeout=args.timeout, label="Playground frontend", log_path=frontend_log)
        print(f"frontend ready: http://127.0.0.1:5173 log={frontend_log}")


def cmd_down(args: argparse.Namespace) -> None:
    for service in ("frontend", "backend", "core"):
        _kill_pid(SERVICES[service]["pid"])
        print(f"stopped {service}")
    if args.ray:
        _run_ray_stop()


def _service_status(name: str) -> dict[str, Any]:
    spec = SERVICES[name]
    pid = _read_pid(spec["pid"])
    alive = _is_process_alive(pid)
    return {
        "name": name,
        "pid": pid,
        "alive": alive,
        "port": spec["port"],
        "listening": _port_open(spec["port"]),
        "pid_file": str(spec["pid"]),
        "latest_log": str(_latest_log(spec["log_prefix"]) or ""),
    }


def cmd_status(args: argparse.Namespace) -> None:
    status = {
        "project_root": str(PROJECT_ROOT),
        "workspaces_dir": str(WORKSPACES_DIR),
        "default_workspace_dir": str(DEFAULT_WORKSPACE_DIR),
        "services": [_service_status(name) for name in ("core", "backend", "frontend")],
    }
    try:
        resources = requests.get(f"{args.server_url.rstrip('/')}/cluster/resources", timeout=5).json()
        status["cluster"] = resources.get("cluster", resources)
    except Exception as exc:
        status["cluster_error"] = str(exc)
    try:
        queues = requests.get(f"{args.server_url.rstrip('/')}/cluster/queues", timeout=5).json()
        status["queues"] = queues.get("queues", queues)
    except Exception as exc:
        status["queues_error"] = str(exc)

    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return

    print(f"Project: {status['project_root']}")
    print(f"Workspace root: {status['workspaces_dir']}")
    for item in status["services"]:
        print(
            f"{item['name']}: pid={item['pid'] or '-'} alive={item['alive']} "
            f"port={item['port']} listening={item['listening']} log={item['latest_log'] or '-'}"
        )
    cluster = status.get("cluster") or {}
    if cluster:
        nodes = cluster.get("nodes") or []
        print(f"Cluster: head={cluster.get('head_node_ip')} nodes={len(nodes)}")
        for node in nodes:
            resources = node.get("resources") or {}
            cpu = resources.get("cpu") or {}
            gpu = resources.get("gpu") or {}
            print(
                f"  {node.get('role')} {node.get('node_ip')} alive={node.get('alive')} "
                f"registered={node.get('registered')} cpu={cpu.get('available')}/{cpu.get('total')} "
                f"gpu={gpu.get('available_count')}/{gpu.get('total_count')}"
            )
    queues = status.get("queues") or {}
    counts = queues.get("counts") or {}
    if counts:
        print(
            f"Queues: ready={counts.get('ready', 0)} pending={counts.get('pending', 0)} "
            f"retrying={counts.get('retrying', 0)} running={counts.get('running', 0)}"
        )


def cmd_doctor(args: argparse.Namespace) -> None:
    issues: list[tuple[str, str]] = []
    checks: list[tuple[str, str]] = []

    checks.append(("project_root", str(PROJECT_ROOT)))
    if not (PROJECT_ROOT / "maze").exists():
        issues.append(("project_root", "maze package directory is missing"))
    if not (PROJECT_ROOT / "system_catalog").exists():
        issues.append(("system_catalog", "system_catalog directory is missing"))
    if not WORKSPACES_DIR.exists():
        issues.append(("workspaces", "workspaces directory is missing"))
    checks.append(("model_dir", str(_configured_model_dir())))
    if LEGACY_WORKSPACE_DIR.exists():
        checks.append(("legacy_workspace", f"present at {LEGACY_WORKSPACE_DIR}; keep for samples only"))
    nested = list(LEGACY_WORKSPACE_DIR.glob("workspaces/*")) if LEGACY_WORKSPACE_DIR.exists() else []
    if nested:
        issues.append(("workspace_nesting", f"legacy workspace contains nested workspaces entries: {len(nested)}"))

    python_path = os.environ.get("PYTHONPATH", "")
    if str(PROJECT_ROOT) not in python_path.split(os.pathsep):
        issues.append(("PYTHONPATH", f"{PROJECT_ROOT} is not in PYTHONPATH for this shell"))

    for binary in (str(_python_bin()), str(_node_bin("node")), str(_node_bin("npm"))):
        if "/" in binary and not Path(binary).exists():
            issues.append(("binary", f"missing {binary}"))
        else:
            checks.append(("binary", binary))

    for service in ("core", "backend", "frontend"):
        checks.append((service, json.dumps(_service_status(service), ensure_ascii=False)))

    if args.json:
        print(json.dumps({"checks": checks, "issues": issues}, indent=2, ensure_ascii=False))
        return
    print("Maze dev doctor")
    for label, value in checks:
        print(f"ok {label}: {value}")
    if not issues:
        print("No blocking issues found.")
        return
    print("Issues:")
    for label, value in issues:
        print(f"  {label}: {value}")
    raise SystemExit(1 if args.strict else 0)


def add_dev_parser(subparsers) -> None:
    parser = subparsers.add_parser("dev", help="Operate a local Maze development stack")
    dev_subparsers = parser.add_subparsers(dest="dev_command", required=True)

    up = dev_subparsers.add_parser("up", help="Start local head and optional Playground")
    up.add_argument("--port", type=int, default=8000)
    up.add_argument("--ray-head-port", type=int, default=6379)
    up.add_argument("--strategy", default="least-loaded")
    up.add_argument("--scheduling-algorithm", choices=["FCFS", "HACS"], default="FCFS")
    up.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    up.add_argument("--playground", action="store_true", help="Start Playground backend and frontend")
    up.add_argument("--timeout", type=float, default=90)
    up.add_argument("--clean-ray", action="store_true", help="Run ray stop --force before starting")

    down = dev_subparsers.add_parser("down", help="Stop local dev services")
    down.add_argument("--ray", action="store_true", help="Also run ray stop --force")

    status = dev_subparsers.add_parser("status", help="Show local dev service status")
    status.add_argument("--server-url", default=os.environ.get("MAZE_CORE_URL", "http://127.0.0.1:8000"))
    status.add_argument("--json", action="store_true")

    doctor = dev_subparsers.add_parser("doctor", help="Check common Maze development setup issues")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--strict", action="store_true", help="Exit non-zero when issues are found")


def handle_dev_command(args: argparse.Namespace) -> bool:
    if getattr(args, "command", None) != "dev":
        return False
    if args.dev_command == "up":
        cmd_up(args)
    elif args.dev_command == "down":
        cmd_down(args)
    elif args.dev_command == "status":
        cmd_status(args)
    elif args.dev_command == "doctor":
        cmd_doctor(args)
    else:
        raise SystemExit(f"Unknown dev command: {args.dev_command}")
    return True
