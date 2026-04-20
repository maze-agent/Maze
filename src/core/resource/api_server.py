import pathlib
import sys

_ROOT_SRC = pathlib.Path(__file__).resolve().parents[2]
_DEFAULT_PROJ_ROOT = str(pathlib.Path(__file__).resolve().parents[3])
if str(_ROOT_SRC) not in sys.path:
    sys.path.insert(0, str(_ROOT_SRC))

import ray
import argparse
from core.utils.remote_llm_route import default_cloud_authorization, default_cloud_completions_url
from task_scheduler import TaskScheduler
from dag_context import DAGContextManager
from flask import Flask, request, jsonify
from task_status_manager import TaskStatusManager
from resource_manager import ComputeNodeResourceManager
import redis
from sys import exit
app= Flask(__name__)

def str_to_bool(val):
    """string 'true'  'false' () """
    if isinstance(val, bool):
        return val
    if val.lower() in ('true', 't', 'yes', '1'):
        return True
    elif val.lower() in ('false', 'f', 'no', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

def parse_args():
    """
    parse command arguments.
    """
    parser= argparse.ArgumentParser(description= "Start the Flask app with configuration parameters.")
    parser.add_argument(
        "--proj_path",
        default=_DEFAULT_PROJ_ROOT,
        help="Path to the maze-sc repository root (directory that contains src/core)",
    )
    parser.add_argument('--redis_ip', default= "172.17.0.2", type= str, required= True, help= "IP for Redis")
    parser.add_argument('--redis_port', default= "6379", type= int,  required= True, help= "Port for Redis")
    parser.add_argument('--flask_port', default= "5000", type= int, required= True, help= "Port for Flask")
    parser.add_argument("--use_online_model", type= str_to_bool, default= True,
                        help= "use online model or no use")
    parser.add_argument("--model_folder",  default="model_cache", 
                        help="Directory for caching downloaded models and intermediate results")
    parser.add_argument("--models_config_path",  default="src/core/config/models.json", 
                        help="Directory for caching downloaded models and intermediate results")
    _public_silicon = "https://api.siliconflow.cn/v1/chat/completions"
    parser.add_argument(
        "--api_url",
        default=default_cloud_completions_url() or _public_silicon,
        help="Absolute chat/completions URL for hosted inference. Prefer MAZE_CLOUD_COMPLETIONS_URL (or OPENAI_BASE_URL).",
    )
    parser.add_argument(
        "--api_key",
        default=default_cloud_authorization() or "",
        help="Optional CLI override for Authorization. Prefer MAZE_CLOUD_API_KEY, SILICONFLOW_API_KEY, or OPENAI_API_KEY.",
    )
    parser.add_argument("--temperature", type= float, default=0.6,
                        help="Sampling temperature for model output (0.0-1.0, lower = more deterministic)")
    parser.add_argument("--max_tokens", type= int, default= 1024,
                        help="Maximum number of tokens allowed in the model's generated output")
    parser.add_argument("--vlm_batch_size", type= int, default= 8,
                        help="Maximum number of tokens allowed in the model's generated output")
    parser.add_argument("--text_batch_size", type= int, default= 8,
                        help="Maximum number of tokens allowed in the model's generated output")    
    parser.add_argument("--top_p", type= float, default=0.9,
                        help="Maximum number of tokens allowed in the model's generated output")
    parser.add_argument("--repetition_penalty", type= float, default=1.1,
                        help="Maximum number of tokens allowed in the model's generated output")
    return parser.parse_args()

def check_redis_connection(host, port):
    try:
        r = redis.Redis(host=host, port=port)
        r.ping()
        print("Redis ")
    except redis.ConnectionError as e:
        print(f" Redis: {e}")
        exit(1)

@app.route('/inform', methods= ['POST'])
def handle_inform()-> object:
    """
    Notify the system of a new task.
    Expected input format:
    {
        "dag_id": "dag1",
        "task_id": "task1",
        "type": "io",     #io,cpu,gpu
        "cpu_num": 1,     #cpu
        "mem": 100,       #cpu,ioMB
        "gpu_mem": 1024   #gpuMB
    }
    :return: JSON confirmation of task reception
    """
    data= request.get_json()
    dag_id= data.get("dag_id")
    run_id = data.get("run_id")
    task_id= data.get("task_id")
    # NEW: 
    priority = data.get("priority") # ltfNone

    print(f"[HTTP] Task received: dag_id: {dag_id}, run_id: {run_id}, task_id: {task_id} from api_server.py")
    scheduler.submit(data, priority)
    return jsonify({"res": "Task accepted"})

@app.route('/status', methods=['POST'])
def get_status() -> object:
    """
    Retrieve the current status and node of a specific task.
    This is for internal or advanced debugging. The main user-facing status
    is in scheduler.py.
    """
    # --- MODIFICATION START ---
    data = request.get_json()
    run_id = data.get("run_id")
    task_id = data.get("task_id")
    if not run_id or not task_id:
        return jsonify({"error": "Missing parameter: run_id and task_id are required"}), 400
    
    status = status_mgr.get_status(run_id, task_id)
    node = status_mgr.get_selected_node(run_id, task_id)
    return jsonify({"run_id": run_id, "task_id": task_id, "status": status, "node": node})

@app.route('/status/all', methods=['POST'])
def get_all_status() -> object:
    """
    Retrieve all tasks' statuses for a given DAG run.
    """
    # --- MODIFICATION START ---
    data = request.get_json()
    run_id = data.get("run_id")
    if run_id is None:
        return jsonify({"error": "Missing parameter: run_id"}), 400

    all_tasks = status_mgr.get_all_tasks()
    result_tasks = []
    # Key in status manager is now (run_id, task_id)
    for (d_run_id, t_id), entry in all_tasks.items():
        if d_run_id == run_id:
            result_tasks.append({
                "task_id": t_id,
                "status": entry["status"],
                "info": entry["info"]
            })
    return jsonify({
        "run_id": run_id,
        "tasks": result_tasks
    })

@app.route('/resource', methods= ['POST'])
def get_resource()-> object:
    """
    Return a snapshot of cluster-wide resource usage.

    :return: JSON with each node's available resource info
    """
    # resource_mgr
    return jsonify(resource_mgr.node2avai_resources)

@app.route('/get_task_error_info', methods= ['POST'])
def get_task_error_info()-> object:
    """
    Retrieve error information if a task failed.
    Input:
    {
        "dag_id": "dag-test",
        "task_id": "task-abc123"
    }

    :return: JSON with error message if available
    """
    data= request.get_json()
    run_id= data.get("run_id")
    task_id= data.get("task_id")
    if not run_id or not task_id:
        return jsonify({"error": "Missing dag_id or task_id"}), 400

    task_status= status_mgr.get_status(run_id, task_id)
    if task_status== "failed":
        failed_info= status_mgr.get_failed_task_info(run_id, task_id)
        if failed_info:
            return jsonify({
                "status": "failed",
                "err_msg": failed_info.get("err_msg", "Unknown error")
            })
        else:
            return jsonify({"status": "failed", "message": "Failure reason not found"}), 404
    else:
        return jsonify({"error": "The task is not failed"}), 400

@app.route('/release_context', methods=['POST'])
def release_context() -> object:
    """
    Release the DAGContext associated with a given run_id.
    """
    # --- MODIFICATION START ---
    data = request.get_json()
    run_id = data.get("run_id")
    if not run_id:
        return jsonify({"status": "error", "msg": "run_id is required"}), 400
    
    print(f"Received request to release context for run_id: {run_id}")
    success = dag_ctx_mgr.release_context(run_id)
    if success:
        return jsonify({"status": "success", "msg": f"Context for run_id '{run_id}' released."})
    else:
        return jsonify({"status": "error", "msg": f"Context for run_id '{run_id}' not found or already released."})
    # --- MODIFICATION END ---

@app.route('/list_contexts', methods=['GET'])
def list_contexts() -> object:
    """
    List all active (not released) DAG run contexts.
    """
    # --- MODIFICATION START ---
    run2ctx = dag_ctx_mgr.get_all_contexts()
    return jsonify({
        "active_dag_run_contexts": list(run2ctx.keys()),
        "count": len(run2ctx)
    })
    # --- MODIFICATION END ---

if __name__ == "__main__":
    args= parse_args()
    redis_ip = args.redis_ip
    redis_port= args.redis_port
    flask_port= args.flask_port
    check_redis_connection(redis_ip, redis_port)
    # Initialize modules
    import os
    ray.init(address= "auto",
        runtime_env={
            "working_dir": os.path.join(args.proj_path, "src") 
        })
    status_mgr= TaskStatusManager()
    dag_ctx_mgr= DAGContextManager(args)
    resource_mgr= ComputeNodeResourceManager()
    try:
        scheduler= TaskScheduler(
            resource_mgr, 
            status_mgr, 
            dag_ctx_mgr, 
            redis_ip=redis_ip,
            redis_port= redis_port, 
            proj_path= args.proj_path, 
            model_folder= args.model_folder, 
            models_config_path= args.models_config_path)
        print("🚀 Starting Flask application...")
        app.run(host= "0.0.0.0", port= flask_port)
    except KeyboardInterrupt:
        #  Ctrl+C
        print("\n gracefully shutting down...")
    finally:
        print("\n🧹 Cleaning up ALL deployed vLLM services by remotely calling shutdown...")
        
        named_actors = ray.util.list_named_actors(all_namespaces=True)
        
        cleanup_futures = []
        for actor_info in named_actors:
            actor_name = actor_info.get("name")
            if actor_name and actor_name.startswith("vllm_runner_"):
                try:
                    print(f"  -> Found active VLLM service actor: '{actor_name}'. Requesting graceful shutdown...")
                    actor_handle = ray.get_actor(actor_name)
                    
                    # Actorstop_serverray.kill()
                    cleanup_futures.append(actor_handle.stop_server.remote())
                    
                except Exception as e:
                    print(f"  -> Error while requesting shutdown for actor '{actor_name}': {e}")
        
        if not cleanup_futures:
            print("  -> No active vLLM runners found on the cluster.")
        else:
            try:
                # stop_server
                ray.get(cleanup_futures, timeout=15)
                print(f"✅ {len(cleanup_futures)} vLLM service(s) have been shut down successfully.")
            except Exception as e:
                print(f"  -> ⚠️  Timed out or failed while waiting for services to shut down: {e}")
        
        ray.shutdown()
        print("Ray has been shut down. Exiting.")
