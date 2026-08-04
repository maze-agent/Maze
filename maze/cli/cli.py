import subprocess
import argparse
import sys
import uvicorn
import os
import time
import logging
import signal
import json
import requests
import socket
from pathlib import Path
from maze.core.worker.worker import Worker
from maze.core.application.spec import app_spec_from_payload, load_app_spec_file
from maze.core.scheduler.strategy import SchedulingAlgorithm
from maze.cli.dev import add_dev_parser, handle_dev_command
import asyncio
from maze.config.logging_config import setup_logging

logger = logging.getLogger(__name__)
HEAD_CLEANUP_MAX_ATTEMPTS = 3
PLAYGROUND_STOP_TIMEOUT_SECONDS = 10.0


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

    service_ports = [
        ("Maze core", port),
        ("Ray head", ray_head_port),
    ]
    if playground:
        resolved_playground_backend_port = (
            playground_backend_port
            or _default_playground_backend_port(playground_port)
        )
        service_ports.extend([
            ("Playground backend", resolved_playground_backend_port),
            ("Playground frontend", playground_port),
        ])
    _ensure_unique_ports(service_ports)
    for service_name, service_port in service_ports:
        _ensure_port_available(service_port, service_name)

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

        await asyncio.gather(server_task, monitor_coroutine, maintenance_coroutine)
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
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


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
):
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
    for status in ("submitted", "running", "succeeded", "failed", "canceled", "interrupted"):
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

    start_parser.add_argument("--port", type=int, metavar="PORT", help="Port for head node (required if --head)",default=8000)
    start_parser.add_argument("--strategy", metavar="STRATEGY", help="Node placement strategy",default="least-loaded")
    start_parser.add_argument(
        "--scheduling-algorithm",
        choices=[item.value for item in SchedulingAlgorithm],
        default=SchedulingAlgorithm.FCFS.value,
        help="Task queue scheduling algorithm",
    )
    start_parser.add_argument("--ray-head-port", type=int, metavar="RAY HEAD PORT", help="Port for ray head (required if --head)",default=6379)
    start_parser.add_argument("--addr", metavar="ADDR", help="Address of head node (required if --worker)")
    start_parser.add_argument("--playground", action="store_true", help="Start Maze Playground visual interface (only applicable to --head)")
    start_parser.add_argument("--playground-port", type=int, default=5173, help="Port for the Playground web UI")
    start_parser.add_argument("--playground-backend-port", type=int, default=None, help="Port for the Playground backend API (default: 3001, or --playground-port + 1 when the UI port is changed)")
    start_parser.add_argument("--agent", action="store_true", help="Keep worker alive and periodically re-register with Maze core")
    start_parser.add_argument("--heartbeat-interval", type=float, default=10, help="Worker agent registration interval in seconds")
    start_parser.add_argument("--log-level", metavar="LOG LEVEL", help="Set log level",default="INFO",choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    start_parser.add_argument("--log-file", metavar="LOG FILE", help="Set log file",default=None)
    

    # === stop subcommand ===
    stop_parser = subparsers.add_parser("stop", help="Stop Maze worker")
    stop_parser.add_argument("--log-level", metavar="LOG LEVEL", help="Set log level",default="INFO",choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    stop_parser.add_argument("--log-file", metavar="LOG FILE", help="Set log file",default=None)

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
        choices=["submitted", "running", "succeeded", "failed", "canceled", "interrupted"],
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
    add_dev_parser(subparsers)

    # Parse args
    args = parser.parse_args()
    
    setup_logging(getattr(args, "log_level", "INFO"), getattr(args, "log_file", None))
    if handle_dev_command(args):
        return
    if args.command == "start":
        if args.head:
            if args.port is None:
                parser.error("--port is required when using --head")
            if args.ray_head_port is None:
                parser.error("--ray-head-port is required when using --head")
            
           
            if hasattr(args, 'playground') and args.playground:
                try:
                    start_head(
                        args.port,
                        args.ray_head_port,
                        args.strategy,
                        args.scheduling_algorithm,
                        playground=True,
                        playground_port=args.playground_port,
                        playground_backend_port=args.playground_backend_port,
                    )
                except RuntimeError as exc:
                    print(f"❌ {exc}", file=sys.stderr)
                    sys.exit(1)
            else:
                try:
                    start_head(
                        args.port,
                        args.ray_head_port,
                        args.strategy,
                        args.scheduling_algorithm,
                        playground=False,
                    )
                except RuntimeError as exc:
                    print(f"❌ {exc}", file=sys.stderr)
                    sys.exit(1)
        elif args.worker:
            if args.addr is None:
                parser.error("--addr is required when using --worker")
            if hasattr(args, 'playground') and args.playground:
                print("⚠️  Warning: --playground parameter is only applicable to head node, will be ignored")
            start_worker(args.addr, agent=args.agent, heartbeat_interval=args.heartbeat_interval)
    elif args.command == "stop":
        stop_worker()
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
