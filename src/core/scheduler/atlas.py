import os
import sys
import json
import time
import requests
import threading
import traceback
import cloudpickle
import redis
import networkx as nx
import importlib.util
import queue
from collections import defaultdict

#

from core.utils.query_loader import GaiaLoader, TBenchLoader, OpenAGILoader

query_loader_factory = {
    "gaia": GaiaLoader,
    "tbench": TBenchLoader,
    "openagi": OpenAGILoader,
}

def dag_manager_atlas(args, dag_que, dag_status_dict):
    """
    ATLAS (Adaptive Thread-Level Attained Service)  V2.0 (Updated Version)
    
    Reference: Autellix (arXiv:2502.13965)
    
    
    Non-clairvoyant () 
    "" (Attained Service)
    
    Priority Formula:
        p(task) = max( p(parent) + actual_runtime(parent) ) for all parents
         p = 0
    
    :
        LAS (Least Attained Service): p 
        
        
    -  daps.py 
    - 
    - 
    """
    print(f"🌟 ATLAS Scheduler (V2.0 - Updated Version) starting...")

    # --- 1.  ---
    dags = {} 
    dags_lock = threading.Lock()
    status_lock = threading.Lock()
    
    #

    # Item: (priority_tuple, run_id, func_name, scheduler_cost)
    # priority_tuple: (score, sub_time, index)
    submit_queue = queue.PriorityQueue()
    
    # Tie-breaker:  > 
    task_enqueue_index = 0 
    task_enqueue_lock = threading.Lock()

    # Redis 
    redis_client = redis.Redis(host=args.redis_ip, port=args.redis_port, decode_responses=False)

    # ==============================================================================
    #

    # ==============================================================================

    def get_task_priority(dag, node_name):
        """
         ATLAS 
         node  'attained_service' 
        """
        return dag.nodes[node_name].get('attained_service', 0.0)

    def enqueue_task(dag, run_id, func_name):
        """
        
        """
        scheduler_start_time = time.time()
        nonlocal task_enqueue_index
        
        # 1.  ATLAS  (Attained Service)
        priority_score = get_task_priority(dag, func_name)
        
        # 2. 
        with task_enqueue_lock:
            current_index = task_enqueue_index
            task_enqueue_index += 1
            
        # Priority Tuple: (Priority Score, Submit Time, Index)
        # Priority Score  (Least Attained Service)
        #  Submit Time  Index  Tie-Breaker
        priority_tuple = (priority_score, dag.graph.get("sub_time", 0), current_index)
        
        scheduler_end_time = time.time()
        scheduler_cost = scheduler_end_time - scheduler_start_time
        
        # ATLAS  pred_time  pred_cost
        item_to_queue = (priority_tuple, run_id, func_name, scheduler_cost)
        submit_queue.put(item_to_queue)
        
        print(f"  -> Enqueued '{func_name}'. (Attained Service: {priority_score:.4f})")

    # ==============================================================================
    #

    # ==============================================================================

    def dag_creator():
        """
         DAG
        """
        while True:
            try:
                run_id, dag_id, dag_source, dag_type, supplementary_files, task2id, sub_time = dag_que.get()
                
                query_loader = query_loader_factory.get(dag_source)
                if not query_loader: continue

                loader = query_loader(
                    args=args, 
                    dag_id=dag_id, 
                    run_id=run_id, 
                    dag_type=dag_type, 
                    dag_source=dag_source, 
                    supplementary_files=supplementary_files, 
                    sub_time=sub_time
                )
                dag = loader.get_dag(task2id)
                if not dag: continue
                
                dag.graph['lock'] = threading.Lock()
                
                #  attained_service  0
                for node in dag.nodes:
                    dag.nodes[node]['attained_service'] = 0.0
                
                with dags_lock:
                    dags[run_id] = dag
                
                print(f"😊 ATLAS: DAG '{dag_id}' (run_id: {run_id[:8]}) created.")

                #  0 
                for node, in_degree in dag.in_degree():
                    if in_degree == 0:
                        dag.nodes[node]["in_degree"] = -1
                        enqueue_task(dag, run_id, node)
                    else:
                        dag.nodes[node]["in_degree"] = in_degree
            except Exception as e:
                print(f"❌ [Error] In dag_creator: {e}\n{traceback.format_exc()}")
            time.sleep(0.01)
            time.sleep(0.01)

    def scheduler_and_submitter():
        """
         Master
        """
        print("🚀 ATLAS Submitter is running.")
        RETRY_LIMIT, RETRY_DELAY_SECONDS = 3, 1
        
        while True:
            try:
                #

                priority_tuple, run_id, func_name, scheduler_cost = submit_queue.get()
                
                # --- [BUG FIX] ---
                #  priority_tuple[0] (0.0) Master  0.0 
                # Master  PriorityQueue  (0.0, dict1)  (0.0, dict2)  dict 
                #  priority_tuple ( list  JSON )
                #  Master  (0.0, t1, idx1)  (0.0, t1, idx2) idx 
                priority_list_for_master = list(priority_tuple)
                
                succeeded = False
                for attempt in range(RETRY_LIMIT):
                    try:
                        dag = None
                        with dags_lock:
                            dag = dags.get(run_id)
                        if not dag:
                            succeeded = True 
                            break
                            
                        dag.nodes[func_name]['scheduler_cost_time'] = scheduler_cost
                        dag.nodes[func_name]['pred_exec_time'] = 0.0
                        dag.nodes[func_name]['pred_cost_time'] = 0.0
                        
                        node_data = dict(dag.nodes[func_name])
                        
                        payload = {
                            "priority": priority_list_for_master, #  [score, time, index]
                            **node_data,
                            "run_id": run_id, 
                            "dag_id": dag.graph["dag_id"],
                            "dag_func_file": dag.graph.get("dag_func_file"), 
                            "arrival_time": dag.graph.get("arrival_time"),
                            "question": dag.graph.get("question"), 
                            "answer": dag.graph.get("answer"),
                            "supplementary_file_paths": dag.graph.get("supplementary_file_paths")
                        }

                        if not payload.get("dag_func_file"):
                            succeeded = True
                            break
                        
                        task_id = payload["task_id"]
                        redis_func_key = f"func:{task_id}"
                        spec = importlib.util.spec_from_file_location("dag_module", payload["dag_func_file"])
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        func = getattr(module, func_name)
                        redis_client.set(redis_func_key, cloudpickle.dumps(func))

                        if redis_client.get(redis_func_key) is None:
                            raise ConnectionError(f"Redis SET/GET verification failed.")

                        response = requests.post(f"http://{args.master_addr}/inform", json=payload)
                        response.raise_for_status()

                        print(f"🎁 Submitted: '{func_name}' from Run ID {run_id[:8]} (Attained Service: {priority_list_for_master[0]:.4f})")
                        succeeded = True
                        break
                    except Exception as e:
                        print(f"  -> ⚠️ [Submitter] Attempt {attempt + 1}/{RETRY_LIMIT} failed for '{func_name}': {e}. Retrying...")
                        time.sleep(RETRY_DELAY_SECONDS)
                
                if not succeeded:
                    print(f"  -> ❌ [Submitter] FAILED to submit task '{func_name}'. Re-enqueuing.")
                    with dags_lock:
                        dag = dags.get(run_id)
                    if dag:
                        enqueue_task(dag, run_id, func_name)
            
            except Exception as e:
                print(f"❌ [CRITICAL ERROR] In submitter loop: {e}\n{traceback.format_exc()}")
                time.sleep(1)
            time.sleep(0.05)

    def monitor():
        """
         ATLAS 
         ATLAS  Critical Path Attained Service
        """
        redis_monitor_client = redis.Redis(host=args.redis_ip, port=args.redis_port, decode_responses=True)
        completion_queue_name = "task_completion_queue"
        print(f"💠 ATLAS Monitor is listening on Redis queue: '{completion_queue_name}'")
        
        while True:
            try:
                _, message = redis_monitor_client.brpop(completion_queue_name)
                notification = json.loads(message)
                run_id, func_name, status, dispatch_task_time, receive_task_result_time, worker_start_exec_time = notification["run_id"], notification["func_name"], notification["status"], notification["dispatch_task_time"], notification["receive_task_result_time"], notification["worker_start_exec_time"]

                dag = None
                with dags_lock:
                    dag = dags.get(run_id)
                if not dag: continue
                
                print(f"✅ Monitor: Received '{status}' for '{func_name}' (DAG: {dag.graph['dag_id'][:8]})")
                
                with dag.graph['lock']:
                    node_info = dag.nodes[func_name]
                    
                    if status == "finished":
                        task_id = node_info.get("task_id")
                        task_result_raw = redis_monitor_client.get(f"result:{task_id}")
                        actual_duration = 0.0
                        
                        if task_result_raw:
                            task_result = json.loads(task_result_raw)
                            start_t = task_result.get("start_time", 0.0)
                            end_t = task_result.get("end_time", 0.0)
                            #  t_k
                            actual_duration = max(0.0, end_t - start_t)
                            
                            # --- ATLAS  ---
                            # p(child) = max( p(child), p(parent) + t(parent) )
                            #  Attained Service
                            parent_priority = node_info.get('attained_service', 0.0)
                            #

                            path_attained_service = parent_priority + actual_duration
                            
                            #

                            for successor in dag.successors(func_name):
                                current_child_p = dag.nodes[successor].get('attained_service', 0.0)
                                #

                                if path_attained_service > current_child_p:
                                    dag.nodes[successor]['attained_service'] = path_attained_service
                                    # print(f"  -> Updated '{successor}' priority to {path_attained_service:.2f} (via {func_name})")

                            #

                            with status_lock:
                                current_status = dag_status_dict.get(run_id, {})
                                time_record = task_result.get("time_record", {})
                                status_entry = current_status.setdefault(func_name, {})
                                status_entry.update({
                                    "start_exec_time": start_t, 
                                    "finish_exec_time": end_t, 
                                    "status": status,
                                    "arrival_time": dag.graph.get("arrival_time"), 
                                    "sub_time": dag.graph.get("sub_time"),
                                    "leave_time": time.time(),
                                    "scheduler_cost_time": node_info.get('scheduler_cost_time', 0.0),
                                    "pred_exec_time": 0.0, # No prediction
                                    "pred_cost_time": 0.0,
                                    "dispatch_task_time": dispatch_task_time, 
                                    "receive_task_result_time": receive_task_result_time,
                                    "worker_start_exec_time": worker_start_exec_time,
                                    "get_cost_time": time_record.get('get_time', 0),
                                    "put_size_bytes": time_record.get('put_size_bytes', 0),
                                    "get_size_bytes": time_record.get('get_size_bytes', 0),
                                })
                                dag_status_dict[run_id] = current_status

                    #

                    is_dag_complete = True
                    for successor_name in dag.successors(func_name):
                        is_dag_complete = False
                        successor_node = dag.nodes[successor_name]
                        if successor_node.get("in_degree", 0) > 0:
                            successor_node["in_degree"] -= 1
                            if successor_node["in_degree"] == 0:
                                successor_node["in_degree"] = -1
                                enqueue_task(dag, run_id, successor_name)
                    
                    if is_dag_complete:
                        all_nodes_processed = all(d.get('in_degree', -1) == -1 for n, d in dag.nodes.items())
                        if all_nodes_processed:
                            dag_total_time = time.time() - dag.graph.get('arrival_time', dag.graph.get('sub_time', time.time()))
                            print(f"🎉 DAG '{dag.graph['dag_id'][:8]}' COMPLETE in {dag_total_time:.2f}s.")
                            with dags_lock:
                                if run_id in dags:
                                    del dags[run_id]
            except Exception as e:
                print(f"❌ [Error] In monitor loop: {e}\n{traceback.format_exc()}")
                time.sleep(5)
            time.sleep(0.01)

    # --- section ---
    threads = [
        threading.Thread(target=dag_creator, daemon=True, name="ATLAS_Creator"),
        threading.Thread(target=scheduler_and_submitter, daemon=True, name="ATLAS_Submitter"),
        threading.Thread(target=monitor, daemon=True, name="ATLAS_Monitor")
    ]
    
    for t in threads:
        t.start()
    
    print("✅ All ATLAS Scheduler threads started.")

    for t in threads:
        t.join()
