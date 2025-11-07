import subprocess
import argparse
import sys
import uvicorn
import os
import time
import logging
import signal
from pathlib import Path
from maze.core.worker.worker import Worker
import asyncio
from maze.config.logging_config import setup_logging

logger = logging.getLogger(__name__)

async def _async_start_head(port: int, ray_head_port: int, playground: bool = False):
    from maze.core.server import app,mapath

    mapath.init(ray_head_port=ray_head_port)  
    monitor_coroutine = asyncio.create_task(mapath.monitor_coroutine())

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)

    playground_processes = []
    if playground:
        playground_processes = start_playground()

    try:
        await asyncio.gather(
            server.serve(),
            monitor_coroutine
        )
    except KeyboardInterrupt:
        print("Shutting down...")
        monitor_coroutine.cancel()
        await monitor_coroutine
        
        # 停止 Playground 进程
        if playground_processes:
            stop_playground(playground_processes)

def start_playground():
    processes = []
    
    project_root = Path(__file__).parent.parent.parent
    backend_dir = project_root / "web" / "maze_playground" / "backend"
    frontend_dir = project_root / "web" / "maze_playground" / "frontend"
    
    print("\n" + "="*60)
    print("🎮 Starting Maze Playground...")
    print("="*60)
    
    if backend_dir.exists():
        print("🔧 starting playground backend (http://localhost:3001)...")
        try:
            backend_process = subprocess.Popen(
                ["node", "src/server.js"],
                cwd=str(backend_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
            )
            processes.append(('backend', backend_process))
            time.sleep(2) 
            print("✅ Playground backend started")
        except Exception as e:
            print(f"❌ Failed to start backend: {e}")
    

    if frontend_dir.exists():
        print("🎨 starting playground frontend (http://localhost:5173)...")
        try:
      
            npm_cmd = "npm.cmd" if sys.platform == 'win32' else "npm"
            frontend_process = subprocess.Popen(
                [npm_cmd, "run", "dev"],
                cwd=str(frontend_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
            )
            processes.append(('frontend', frontend_process))
            time.sleep(3) 
            print("✅ Playground frontend started")
        except Exception as e:
            print(f"❌ Failed to start frontend: {e}")
    
    if processes:
        print("\n" + "="*60)
        print("🎉 Playground successfully started!")
        print("="*60)
        print("📱 frontend address: http://localhost:5173")
        print("🔌 backend address: http://localhost:3001")
        print("🎮 open browser to http://localhost:5173 to start using")
        print("="*60 + "\n")
    
    return processes

def stop_playground(processes):
    print("\n🛑 shutting down Playground...")
    for name, process in processes:
        try:
            if sys.platform == 'win32':
               
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], 
                             capture_output=True)
            else:
                
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            print(f"✅ {name} stopped")
        except Exception as e:
            print(f"⚠️  Failed to stop {name}: {e}")
    print("✅ Playground closed")

def start_head(port: int, ray_head_port: int, playground: bool = False):
    asyncio.run(_async_start_head(port, ray_head_port, playground))
   
def start_worker(addr: str):
    Worker.start_worker(addr)

def stop_worker():
    Worker.stop_worker()

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
    start_parser.add_argument("--playground", action="store_true", help="Start Maze Playground visual interface (only applicable to --head)")
    start_parser.add_argument("--log-level", metavar="LOG LEVEL", help="Set log level",default="INFO",choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    start_parser.add_argument("--log-file", metavar="LOG FILE", help="Set log file",default=None)
    

    # === stop subcommand ===
    stop_parser = subparsers.add_parser("stop", help="Stop Maze worker")

    # Parse args
    args = parser.parse_args()
    
    setup_logging(args.log_level, args.log_file)
    if args.command == "start":
        if args.head:
            if args.port is None:
                parser.error("--port is required when using --head")
            if args.ray_head_port is None:
                parser.error("--ray-head-port is required when using --head")
            
           
            if hasattr(args, 'playground') and args.playground:
                start_head(args.port, args.ray_head_port, playground=True)
            else:
                start_head(args.port, args.ray_head_port, playground=False)
        elif args.worker:
            if args.addr is None:
                parser.error("--addr is required when using --worker")
            if hasattr(args, 'playground') and args.playground:
                print("⚠️  Warning: --playground parameter is only applicable to head node, will be ignored")
            start_worker(args.addr)
    elif args.command == "stop":
        stop_worker()
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()