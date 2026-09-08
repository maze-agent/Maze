"""Foreground managed-local startup and thin Engine API commands."""

import argparse
import asyncio
import json
import os
import socket
import subprocess
from pathlib import Path

from maze.spec import EngineConfig, parse_document


def start(config: EngineConfig) -> None:
    import ray
    import uvicorn

    from maze.api import create_app
    from maze.engine import Engine

    token = os.environ.get(config.api.token_env)
    if not token:
        raise ValueError(f"Set {config.api.token_env} before starting Maze")
    if ray.is_initialized() or os.environ.get("RAY_ADDRESS"):
        raise RuntimeError("Managed-local requires a dedicated Ray boundary")
    # Read-only ownership check; never kill processes based on names or ports.
    processes = subprocess.check_output(["ps", "-axo", "comm="], text=True)
    if any(
        Path(p.strip()).name in ("raylet", "gcs_server") for p in processes.splitlines()
    ):
        raise RuntimeError(
            "Existing Ray processes detected; use a dedicated host/session"
        )
    listener = socket.socket()
    try:
        listener.bind((config.api.host, config.api.port))
        listener.listen(128)
        ray.init(
            address="local",
            num_cpus=config.cluster.num_cpus,
            object_store_memory=config.cluster.object_store_memory,
            include_dashboard=False,
        )

        async def serve() -> None:
            engine = Engine(config)
            app = create_app(
                engine, token, stop=lambda: setattr(server, "should_exit", True)
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    access_log=False,
                    timeout_graceful_shutdown=config.timeouts.shutdown_grace_s,
                )
            )
            try:
                await server.serve(sockets=[listener])
            finally:
                await engine.close()

        asyncio.run(serve())
    finally:
        listener.close()
        ray.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(prog="maze")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000")
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--config", type=Path)
    commands.add_parser("status")
    commands.add_parser("stop")
    deploy = commands.add_parser("deploy")
    deploy.add_argument("dag", type=Path)
    deploy.add_argument("--deployment-config", type=Path)
    deployments = commands.add_parser("deployments")
    deployments.add_argument("action", choices=["list", "show"])
    deployments.add_argument("id", nargs="?")
    invoke = commands.add_parser("invoke")
    invoke.add_argument("id")
    invoke.add_argument("--input", required=True, type=Path)
    invoke.add_argument(
        "--metrics", choices=["none", "basic", "detailed"], default="basic"
    )
    invoke.add_argument("--timeout-s", type=float)
    invoke.add_argument("--invocation-id")
    for name in ("cancel", "undeploy"):
        commands.add_parser(name).add_argument("id")
    args = parser.parse_args()
    if args.command == "start":
        config = (
            EngineConfig.model_validate(parse_document(args.config.read_text()))
            if args.config
            else EngineConfig()
        )
        start(config)
        return
    from maze.client import MazeClient

    token = os.environ.get("MAZE_API_TOKEN", "")
    with MazeClient(args.endpoint, token) as client:
        match args.command:
            case "status":
                result = client.request("GET", "/readyz")
            case "stop":
                result = client.request("POST", "/v1/stop")
            case "deploy":
                body = {"dag": parse_document(args.dag.read_text())}
                if args.deployment_config:
                    body["deployment"] = parse_document(
                        args.deployment_config.read_text()
                    )
                result = client.request("POST", "/v1/deployments", body)
            case "deployments":
                if args.action == "show" and not args.id:
                    parser.error("deployments show requires an ID")
                path = "/v1/deployments" + (
                    f"/{args.id}" if args.action == "show" else ""
                )
                result = client.request("GET", path)
            case "invoke":
                result = client.request(
                    "POST",
                    f"/v1/deployments/{args.id}/invoke",
                    {
                        "input": parse_document(args.input.read_text()),
                        "metrics": args.metrics,
                        "timeout_s": args.timeout_s,
                        "invocation_id": args.invocation_id,
                    },
                )
            case "cancel":
                result = client.request("POST", f"/v1/invocations/{args.id}/cancel")
            case "undeploy":
                result = client.request("DELETE", f"/v1/deployments/{args.id}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
