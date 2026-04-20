import os
import sys
import json
import time
import math
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

from agentos.utils.exec_time_pred import DAGTaskPredictor
from agentos.utils.query_loader import GaiaLoader, TBenchLoader, OpenAGILoader

query_loader_factory = {
    "gaia": GaiaLoader,
    "tbench": TBenchLoader,
    "openagi": OpenAGILoader,
}

# --- section ---
TASK_TYPE_AVG_TIMES = {'io': 2.0, 'cpu': 3.0, 'gpu': 60.0} 
HISTORY_AVG_JCT = 60.0
HISTORY_AVG_GPU_COUNT = 5.0
EMA_ALPHA = 0.1
EPSILON = 1e-3

def dag_manager_hacs(args, dag_que, dag_status_dict):
    """
    HACS v5 - Exponential Paper-Aligned Edition
    
    
     SC S(tau) = [Omega(tau) * beta^Phi(tau)] / T_pred(tau)
    HACS  Base/Slope (omega, t_pred, is_dynamic)
    Master  wait_time 
    """
    print(f"🌟 HACS Scheduler v5 (Paper-Aligned) starting...")

    dags = {} 
    dags_lock = threading.Lock()
    status_lock = threading.Lock()
    
    submit_queue = []
    submit_queue_lock = threading.Lock()
    
    global HISTORY_AVG_JCT, HISTORY_AVG_GPU_COUNT
    
    predictor = DAGTaskPredictor(
        args.redis_ip, args.redis_port, args.time_pred_model_path,
        args.min_sample4train, args.min_sample4incremental
    )
    redis_client = redis.Redis(host=args.redis_ip, port=args.redis_port, decode_responses=False)

    # ==============================================================================
    # :  Omega, T_pred, is_dynamic
    # ==============================================================================
    
    def extract_formula_params(dag, node_name):
        """
        Extracts the raw parameters for the Master to compute the exponential Priority Score.
        """
        node_info = dag.nodes[node_name]
        
        # 1.  T_pred ()
        t_pred = node_info.get('pred_time', TASK_TYPE_AVG_TIMES.get(node_info.get('type', 'cpu'), 5.0))
        t_pred = max(t_pred, EPSILON)
        
        # 2.  DAG 
        is_dynamic = dag.graph.get("is_dynamic", False) or "dynamic" in dag.graph.get("dag_id", "")

        if is_dynamic:
            # ======================
            # : Omega(tau) = N_anc + (t_wait / alpha*dct)
            #  N_anc ()
            omega = float(node_info.get('n_anc', 0))
            
            #  Master 
            snapshot_remain = max(HISTORY_AVG_GPU_COUNT, 1.0)
            total_gpu_tasks = max(HISTORY_AVG_GPU_COUNT, 1.0)
        else:
            # ======================
            # : Omega(tau) = log(2 + 2 * N_desc)
            n_desc = node_info.get('n_desc', 0)
            omega = math.log2(2.0 + 2.0 * n_desc)
            
            snapshot_remain = max(dag.graph.get('remaining_gpu_tasks', 0), EPSILON)
            total_gpu_tasks = max(dag.graph.get('total_gpu_tasks', 0), EPSILON)
            
        #  Master (Omega, T_pred, is_dynamic)
        priority_params = (omega, t_pred, is_dynamic)
        return priority_params, snapshot_remain, total_gpu_tasks

    def enqueue_task(dag, run_id, func_name, is_start_node=False):
        scheduler_start_time = time.time()
        node_info = dag.nodes[func_name]
        task_type = node_info.get('type', 'cpu')

        pred_time = None
        pred_start = 0
        pred_cost = 0
        
        if not is_start_node:
            try:
                pred_task_ids = [dag.nodes[p].get("task_id") for p in dag.predecessors(func_name)]
                pred_start = time.time()
                pred_time = predictor.predict(
                    succ_func_name=func_name, 
                    succ_task_type=task_type, 
                    pred_task_ids=pred_task_ids
                )
                pred_cost = time.time() - pred_start
            except Exception as e:
                pass
        
        if not pred_time:
            pred_time = TASK_TYPE_AVG_TIMES.get(task_type, 5.0)
        
        node_info['pred_time'] = float(pred_time)
        
        # []: 
        priority_params, snapshot_remain, total_tasks = extract_formula_params(dag, func_name)
        
        scheduler_end_time = time.time()
        scheduler_cost = scheduler_end_time - scheduler_start_time
        
        item_to_queue = {
            "run_id": run_id,
            "func_name": func_name,
            "scheduler_cost": scheduler_cost,
            "pred_time": pred_time,
            "pred_cost": pred_cost,
            "priority_params": priority_params, # Tuple: (omega, t_pred, is_dynamic)
            "snapshot_remain": snapshot_remain,
            "total_gpu_tasks": total_tasks
        }
        
        with submit_queue_lock:
            submit_queue.append(item_to_queue)
        
        omega, t_pred, is_dyn = priority_params
        print(f"  -> [HACS Enqueue] '{func_name}' Params: Omega={omega:.2f}, T_pred={t_pred:.2f}, Dyn={is_dyn}")

    def precalculate_dag_metrics(dag):
        total_gpu_tasks = 0
        for node in dag.nodes:
            if dag.nodes[node].get('type') == 'gpu':
                total_gpu_tasks += 1
        dag.graph['total_gpu_tasks'] = total_gpu_tasks
        dag.graph['remaining_gpu_tasks'] = total_gpu_tasks
        
        try:
            topo_order = list(nx.topological_sort(dag))
            
            # 1.  n_anc ()
            for node in topo_order:
                preds = list(dag.predecessors(node))
                if not preds:
                    dag.nodes[node]['n_anc'] = 0
                else:
                    dag.nodes[node]['n_anc'] = max(dag.nodes[p].get('n_anc', 0) for p in preds) + 1

            # 2.  n_desc ()
            reverse_topo = reversed(topo_order)
            for node in reverse_topo:
                succs = list(dag.successors(node))
                if not succs:
                    #

                    dag.nodes[node]['n_desc'] = 0
                else:
                    #  =  + 1
                    dag.nodes[node]['n_desc'] = max(dag.nodes[s].get('n_desc', 0) for s in succs) + 1
                
        except nx.NetworkXUnfeasible:
            print("❌ [Error] DAG has cycles!")

    def dag_creator():
        while True:
            try:
                item = dag_que.get()
                if not item: continue
                run_id, dag_id, dag_source, dag_type, supplementary_files, task2id, sub_time = item
                
                query_loader = query_loader_factory.get(dag_source)
                if not query_loader: continue

                loader = query_loader(args=args, dag_id=dag_id, run_id=run_id, dag_type=dag_type, dag_source=dag_source, supplementary_files=supplementary_files, sub_time=sub_time)
                dag = loader.get_dag(task2id)
                if not dag: continue
                
                dag.graph['lock'] = threading.Lock()
                precalculate_dag_metrics(dag)
                
                with dags_lock:
                    dags[run_id] = dag
                
                print(f"😊 HACS: DAG '{dag_id}' created.")

                for node, in_degree in dag.in_degree():
                    if in_degree == 0:
                        dag.nodes[node]["in_degree"] = -1
                        enqueue_task(dag, run_id, node, is_start_node=True)
                    else:
                        dag.nodes[node]["in_degree"] = in_degree
            except Exception as e:
                print(f"❌ [Error] In dag_creator: {e}\n{traceback.format_exc()}")
            time.sleep(0.01)

    def scheduler_and_submitter():
        print("🚀 HACS Submitter is running.")
        RETRY_LIMIT, RETRY_DELAY_SECONDS = 3, 1
        
        while True:
            try:
                task_item = None
                with submit_queue_lock:
                    if submit_queue:
                        task_item = submit_queue.pop(0)
                
                if not task_item:
                    time.sleep(0.01)
                    continue

                run_id = task_item["run_id"]
                func_name = task_item["func_name"]
                priority_params = task_item["priority_params"] 
                
                succeeded = False
                for attempt in range(RETRY_LIMIT):
                    try:
                        dag = None
                        with dags_lock:
                            dag = dags.get(run_id)
                        if not dag:
                            succeeded = True 
                            break
                            
                        dag.nodes[func_name]['scheduler_cost_time'] = task_item["scheduler_cost"]
                        dag.nodes[func_name]['pred_exec_time'] = task_item["pred_time"]
                        dag.nodes[func_name]['pred_cost_time'] = task_item["pred_cost"]
                        
                        node_data = dict(dag.nodes[func_name])
                        current_avg_jct = max(HISTORY_AVG_JCT, 1e-3)
                        payload = {
                            "priority": priority_params, 
                            **node_data,
                            "run_id": run_id, 
                            "dag_id": dag.graph["dag_id"],
                            "dag_func_file": dag.graph.get("dag_func_file"), 
                            "arrival_time": dag.graph.get("arrival_time"),
                            "sub_time": dag.graph.get("sub_time"), 
                            "question": dag.graph.get("question"), 
                            "answer": dag.graph.get("answer"),
                            "snapshot_remain": task_item["snapshot_remain"],
                            "total_gpu_tasks": task_item["total_gpu_tasks"],
                            "supplementary_file_paths": dag.graph.get("supplementary_file_paths"),
                            "latest_avg_jct": current_avg_jct
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

                        response = requests.post(f"http://{args.master_addr}/inform", json=payload)
                        response.raise_for_status()

                        print(f"🎁 Submitted: '{func_name}'")
                        succeeded = True
                        break
                    except Exception as e:
                        print(f"  -> ⚠️ [Submitter] Attempt {attempt + 1} failed: {e}. Retrying...")
                        time.sleep(RETRY_DELAY_SECONDS)
                
                if not succeeded:
                    with submit_queue_lock:
                        submit_queue.append(task_item) 
            
            except Exception as e:
                print(f"❌ [Error] In submitter: {e}\n{traceback.format_exc()}")
                time.sleep(1)

    def monitor():
        global HISTORY_AVG_JCT, HISTORY_AVG_GPU_COUNT
        redis_monitor_client = redis.Redis(host=args.redis_ip, port=args.redis_port, decode_responses=True)
        completion_queue_name = "task_completion_queue"
        
        while True:
            try:
                _, message = redis_monitor_client.brpop(completion_queue_name)
                notification = json.loads(message)
                run_id, func_name, status = notification["run_id"], notification["func_name"], notification["status"]
                
                dispatch_time = notification.get("dispatch_task_time")
                receive_time = notification.get("receive_task_result_time")
                worker_start = notification.get("worker_start_exec_time")

                dag = None
                with dags_lock:
                    dag = dags.get(run_id)
                if not dag: continue
                
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
                            actual_duration = max(0.0, end_t - start_t)
                            
                            predictor.collect_data_for_task(task_id=task_id, func_name=func_name, record_json=task_result)
                            
                            if node_info.get('type') == 'gpu':
                                current_gpu_remain = dag.graph.get('remaining_gpu_tasks', 0)
                                if current_gpu_remain > 0:
                                    dag.graph['remaining_gpu_tasks'] = current_gpu_remain - 1
                            
                            if actual_duration > 0:
                                t_type = node_info.get('type', 'cpu')
                                old_avg = TASK_TYPE_AVG_TIMES.get(t_type, 5.0)
                                TASK_TYPE_AVG_TIMES[t_type] = 0.9 * old_avg + 0.1 * actual_duration

                            with status_lock:
                                current_status = dag_status_dict.get(run_id, {})
                                time_record = task_result.get("time_record", {})
                                status_entry = current_status.setdefault(func_name, {})
                                status_entry.update({
                                    "start_exec_time": start_t, "finish_exec_time": end_t, "status": status, "type": node_info.get("type", "cpu"),
                                    "arrival_time": dag.graph.get("arrival_time"), "sub_time": dag.graph.get("sub_time"),
                                    "leave_time": time.time(),
                                    "scheduler_cost_time": node_info.get('scheduler_cost_time', 0.0),
                                    "pred_exec_time": node_info.get('pred_exec_time', 0.0),
                                    "pred_cost_time": node_info.get('pred_cost_time', 0.0),
                                    "dispatch_task_time": dispatch_time, "receive_task_result_time": receive_time,
                                    "worker_start_exec_time": worker_start,
                                    "get_cost_time": time_record.get('get_time', 0),
                                    "put_size_bytes": time_record.get('put_size_bytes', 0),
                                    "get_size_bytes": time_record.get('get_size_bytes', 0),
                                })
                                dag_status_dict[run_id] = current_status

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
                            dag_jct = time.time() - dag.graph['sub_time']
                            print(f"🎉 DAG '{dag.graph['dag_id'][:8]}' COMPLETE in {dag_jct:.2f}s.")
                            
                            HISTORY_AVG_JCT = (1 - EMA_ALPHA) * HISTORY_AVG_JCT + EMA_ALPHA * dag_jct
                            total_gpu = dag.graph.get('total_gpu_tasks', 0)
                            HISTORY_AVG_GPU_COUNT = (1 - EMA_ALPHA) * HISTORY_AVG_GPU_COUNT + EMA_ALPHA * total_gpu
                            
                            with dags_lock:
                                if run_id in dags:
                                    del dags[run_id]
            except Exception as e:
                print(f"❌ [Error] In monitor loop: {e}\n{traceback.format_exc()}")
            time.sleep(0.01)

    threads = [
        threading.Thread(target=dag_creator, daemon=True, name="HACS_Creator"),
        threading.Thread(target=scheduler_and_submitter, daemon=True, name="HACS_Submitter"),
        threading.Thread(target=monitor, daemon=True, name="HACS_Monitor")
    ]
    for t in threads: t.start()
    for t in threads: t.join()