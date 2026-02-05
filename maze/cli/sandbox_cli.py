import argparse
import sys
import uvicorn
from maze.config.logging_config import setup_logging
from maze.sandbox.server import app as sandbox_app

def start_sandbox(host: str = "0.0.0.0", port: int = 8000):
    print(f"🚀 Starting Code Sandbox Server at {host}:{port}")
    
    uvicorn.run(
        sandbox_app,
        host=host,
        port=port,
        log_level="info"
    )


def main():
    parser = argparse.ArgumentParser(prog="maze-sandbox", description="Maze code sandbox service")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Available commands")

    # === start subcommand ===
    start_parser = subparsers.add_parser("start", help="Start the sandbox server")
    start_parser.add_argument("--host", type=str, metavar="HOST", help="Host for sandbox server", default="0.0.0.0")
    start_parser.add_argument("--port", type=int, metavar="PORT", help="Port for sandbox server", default=8000)
    

    # Parse args
    args = parser.parse_args()    
    if args.command == "start":
        start_sandbox(args.host, args.port)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()