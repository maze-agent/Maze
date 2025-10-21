import argparse
import sys
import uvicorn


def start_head(port: int):
    from maze.core.server import app,mapath
    mapath.start()
    uvicorn.run(app, host="0.0.0.0", port=port)


def start_worker(addr: str):
    print(f"[WORKER] Starting worker node connecting to {addr}")
    # 这里可以启动你的 worker 逻辑
    # 例如：connect_to_head_and_work(addr)

def stop():
    print("[STOP] Stopping all maze processes...")
    # 实现停止逻辑，例如发送信号、清理等

def main():
    parser = argparse.ArgumentParser(prog="maze", description="Maze distributed task runner")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Available commands")

    # === start subcommand ===
    start_parser = subparsers.add_parser("start", help="Start a Maze node")
    start_group = start_parser.add_mutually_exclusive_group(required=True)
    start_group.add_argument("--head", action="store_true", help="Start as head node")
    start_group.add_argument("--worker", action="store_true", help="Start as worker node")

    start_parser.add_argument("--port", type=int, metavar="PORT", help="Port for head node (required if --head)",default=8000)
    start_parser.add_argument("--addr", metavar="ADDR", help="Address of head node (required if --worker)")

    # === stop subcommand ===
    stop_parser = subparsers.add_parser("stop", help="Stop Maze processes")

    # Parse args
    args = parser.parse_args()

    if args.command == "start":
        if args.head:
            if args.port is None:
                parser.error("--port is required when using --head")
            start_head(args.port)
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