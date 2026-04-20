import os
import sys
import json
import time
import queue
import requests
import threading
import traceback
import cloudpickle
import networkx as nx
import importlib.util
import redis
from agentos.utils.query_loader import GaiaLoader, TBenchLoader, OpenAGILoader

# A dictionary to map dag_source to the appropriate loader class
query_loader_factory = {
    "gaia": GaiaLoader,
    "tbench": TBenchLoader,
    "openagi": OpenAGILoader,
}

def dag_manager_fcfs(args, dag_que, dag_status_dict):
    """
    Manages DAG execution using a simple Round-Robin (FIFO) scheduling strategy.
    """
    # Dictionary to hold the graph representation of each running DAG
    dags = {}
    # A single FIFO queue for all ready tasks, which is the core of Round-Robin
    submit_queue = queue.Queue()
    # Redis client for function serialization and inter-process communication
    redis_client = redis.Redis(host=args.redis_ip, port=args.redis_port, decode_responses=False)

    def dag_creator():
        """
        Continuously fetches new DAGs from the input queue,
        builds their graph representation, and adds initial ready tasks to the submit_queue.
        """
        while True:
            try:
                # Get a new DAG submission from the main queue
                run_id, dag_id, dag_source, dag_type, supplementary_files, task2id, sub_time= dag_que.get()
                # Use the factory to find the correct loader for the given dag_source
                query_loader = query_loader_factory.get(dag_source)
                if not query_loader:
                    print(f"❌ [Error] No loader found for dag_source: {dag_source}")
                    continue
                
                # Instantiate the loader and get the DAG graph
                loader = query_loader(args= args, dag_id= dag_id, run_id= run_id, dag_type= dag_type, dag_source= dag_source, supplementary_files= supplementary_files, sub_time= sub_time)
                dag = loader.get_dag(task2id)
                dags[run_id] = dag
                
                print(f"😊 DAG '{dag_id}' (Run ID: {run_id}) created. Finding initial tasks...")

                # Calculate in-degrees for all nodes and find the entry points (in-degree == 0)
                for node, in_degree in dag.in_degree():
                    node_info = dag.nodes[node]
                    node_info["in_degree"] = in_degree
                    node_info["arrival_time"] = dag.graph.get('arrival_time', time.time()) # Ensure arrival_time exists

                    if in_degree == 0:
                        # This is an entry task, add it to the queue to be submitted
                        submit_queue.put((run_id, node_info["func_name"]))
                        print(f"  -> Adding initial task '{node_info['func_name']}' to RR queue.")
            except Exception as e:
                print(f"❌ [FATAL] Error in dag_creator thread: {e}\n{traceback.format_exc()}")
            time.sleep(0.01)

    def submitter():
        """
        Continuously fetches ready tasks from the FIFO queue and submits them to the master.
        """
        task_order= 1
        while True:
            try:
                # Get the next ready task in FIFO order
                run_id, func_name = submit_queue.get()
                scheduler_cost_time, pred_exec_time, pred_cost_time= 0, 0, 0
                dag = dags[run_id]
                dag.nodes[func_name]['scheduler_cost_time'] = scheduler_cost_time
                dag.nodes[func_name]['pred_exec_time'] = pred_exec_time
                dag.nodes[func_name]['pred_cost_time'] = pred_cost_time
                node_data = dict(dag.nodes[func_name])

                # Prepare the task submission payload
                payload = {
                    "run_id": run_id,
                    "dag_id": dag.graph["dag_id"],
                    "task_id": node_data["task_id"],
                    "func_name": func_name,
                    "question": dag.graph.get("question"),
                    "answer": dag.graph.get("answer"),
                    "supplementary_file_paths": dag.graph.get("supplementary_file_paths"),
                    "dag_func_file": dag.graph.get("dag_func_file"),
                    "arrival_time": dag.graph.get("arrival_time"),
                    **node_data  # Add all other node-specific attributes
                }
                
                print(f"🎁 Submitting task: '{func_name}' from Run ID: {run_id}")

                # Serialize the task function and store it in Redis
                spec = importlib.util.spec_from_file_location("dag_module", payload["dag_func_file"])
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                func = getattr(module, func_name)
                serialized_func = cloudpickle.dumps(func)
                redis_func_key = f"func:{payload['task_id']}"
                redis_client.set(redis_func_key, serialized_func)
                # (dag.graph["arrival_time"], task_order)
                payload["priority"]= task_order
                task_order+= 1
                # Notify the master scheduler about the new task
                requests.post(f"http://{args.master_addr}/inform", json=payload)
                
            except requests.exceptions.RequestException as e:
                print(f"❌ Failed to notify master for task '{func_name}': {e}")
            except Exception as e:
                 print(f"❌ [FATAL] Error in submitter thread: {e}\n{traceback.format_exc()}")
            time.sleep(0.1)

    def monitor():
        """
        Listens for task completion notifications from Redis.
        Updates the DAG status and unlocks successor tasks.
        """
        redis_monitor_client = redis.Redis(host=args.redis_ip, port=args.redis_port, decode_responses=True)
        completion_queue_name = "task_completion_queue"
        print(f"💠 RR Monitor is now listening on Redis queue: '{completion_queue_name}'")

        while True:
            try:
                # Blocking pop from the completion queue
                _, message = redis_monitor_client.brpop(completion_queue_name)
                if not message:
                    continue

                notification = json.loads(message)
                run_id = notification["run_id"]
                func_name = notification["func_name"]
                status = notification["status"]

                print(f"✅ Monitor: Received completion for '{func_name}' (Run ID: {run_id}) with status '{status}'.")

                dag = dags.get(run_id)
                if not dag:
                    continue
                node_info = dag.nodes[func_name]
                task_id = node_info.get("task_id")
                if not task_id:
                    continue

                # Update the shared dag_status_dict with detailed stats
                task_result_raw = redis_monitor_client.get(f"result:{task_id}")
                task_result = json.loads(task_result_raw) if task_result_raw else {}
                start_exec_time = task_result.get("start_time", 0.0)
                finish_exec_time = task_result.get("end_time", 0.0)
                sub_time = dag.graph.get("sub_time", 0.0)
                
                current_status = dict(dag_status_dict.get(run_id, {}))
                status_entry = current_status.setdefault(func_name, {})
                status_entry.update({
                    "start_exec_time": start_exec_time,
                    "finish_exec_time": finish_exec_time,
                    "arrival_time": dag.graph["arrival_time"],
                    "sub_time": sub_time,
                    "leave_time": time.time(),
                    "status": status,
                    "scheduler_cost_time": node_info.get('scheduler_cost_time', 0.0),
                    "pred_exec_time": node_info.get('pred_exec_time', 0.0),
                    "pred_cost_time": node_info.get('pred_cost_time', 0.0),
                })
                dag_status_dict[run_id] = current_status

                # If task finished successfully, unlock its successors
                if status == "finished":
                    for successor_name in dag.successors(func_name):
                        successor_node = dag.nodes[successor_name]
                        successor_node["in_degree"] -= 1
                        
                        # If all parent tasks are done, the successor is ready
                        if successor_node["in_degree"] == 0:
                            print(f"  -> Unlocking successor task '{successor_name}'. Adding to RR queue.")
                            submit_queue.put((run_id, successor_name))
            except Exception as e:
                print(f"❌ [FATAL] An error occurred in the monitor thread: {e}\n{traceback.format_exc()}")
                time.sleep(5)
            time.sleep(0.01)

    # --- Start all worker threads ---
    creator_thread = threading.Thread(target=dag_creator, daemon=True)
    submitter_thread = threading.Thread(target=submitter, daemon=True)
    monitor_thread = threading.Thread(target=monitor, daemon=True)

    creator_thread.start()
    submitter_thread.start()
    monitor_thread.start()

    print("😊 Round-Robin (RR) DAG manager started with all threads running.")
    
    # Keep the main thread alive to let daemon threads run
    creator_thread.join()
    submitter_thread.join()
    monitor_thread.join()