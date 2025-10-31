import subprocess
import argparse
import sys
import uvicorn
from maze.core.worker.worker import Worker
import asyncio

async def _async_start_head(port: int, ray_head_port: int):
    from maze.core.server import app,mapath

    mapath.init(ray_head_port=ray_head_port)  
    monitor_coroutine = asyncio.create_task(mapath.monitor_coroutine())

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)

    try:
        await asyncio.gather(
            server.serve(),
            monitor_coroutine
        )
    except KeyboardInterrupt:
        print("Shutting down...")
        monitor_coroutine.cancel()
        await monitor_coroutine

def start_head(port: int,ray_head_port: int):
    asyncio.run(_async_start_head(port, ray_head_port))
   
def start_worker(addr: str):
    Worker.start_worker(addr)

def stop():
    try:
        command = [
            "ray", "stop",
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
        print(f"发生异常：{e}")


def main():
    parser = argparse.ArgumentParser(prog="maze", description="Maze distributed task runner")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Available commands")

    # === start subcommand ===
    start_parser = subparsers.add_parser("start", help="Start a Maze node")
    start_group = start_parser.add_mutually_exclusive_group(required=True)
    start_group.add_argument("--head", action="store_true", help="Start as head node")
    start_group.add_argument("--worker", action="store_true", help="Start as worker node")

    start_parser.add_argument("--port", type=int, metavar="PORT", help="Port for head node (required if --head)",default=8000)
    start_parser.add_argument("--ray-head-port", type=int, metavar="RAY HEAD PORT", help="Port for ray head (required if --head)",default=6379)
    start_parser.add_argument("--addr", metavar="ADDR", help="Address of head node (required if --worker)")

    # === stop subcommand ===
    stop_parser = subparsers.add_parser("stop", help="Stop Maze processes")

    # Parse args
    args = parser.parse_args()

    if args.command == "start":
        if args.head:
            if args.port is None:
                parser.error("--port is required when using --head")
            if args.ray_head_port is None:
                parser.error("--ray-head-port is required when using --head")
            start_head(args.port, args.ray_head_port)
        elif args.worker:
            if args.addr is None:
                parser.error("--addr is required when using --worker")
            start_worker(args.addr)
    elif args.command == "stop":
        stop()
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()