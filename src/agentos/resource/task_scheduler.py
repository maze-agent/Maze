import os
import ray
import time
import queue
import redis
import random
import threading
from typing import Optional, Dict, List, Tuple
import tracemalloc
import requests
from agentos.utils.execution_backend import VLLMBackend, HuggingFaceBackend
import heapq
import gc
from collections import deque, defaultdict
import json

def write_log(dag_id,run_id,task_id,dag_func_file,func_name,status):
    log_dir = './log'
    log_file = os.path.join(log_dir, 'log.txt')
    os.makedirs(log_dir, exist_ok=True)
    with open(log_file, 'a', encoding='utf-8') as file:
        file.write(f"{dag_id},{run_id},{task_id},{dag_func_file},{func_name},{status},{time.time()}\n")

@ray.remote(num_cpus=0, max_calls=1)
def remote_task_runner(serialized_func: bytes, ctx_actor: object, task_id: str, redis_host:str, redis_port:int, task_type:str, gpu_indices: Optional[List[int]])-> Dict[str, str]:
    try:
        import os
        import json
        import redis
        import cloudpickle
        import torch
        worker_start_exec_time= time.time()
        func= cloudpickle.loads(serialized_func)
       
        if task_type == "gpu" and gpu_indices:
            visible_devices = ",".join(map(str, gpu_indices))
            print(f"  -> Setting CUDA_VISIBLE_DEVICES='{visible_devices}' for task {task_id}.")
            os.environ["CUDA_VISIBLE_DEVICES"] = visible_devices

        tracemalloc.start()
        result = func(ctx_actor)
        torch.cuda.empty_cache()
        gc.collect()
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"{task_id}: peak mem {peak / (1024 ** 2)} MB")
        
        if result:
            r = redis.Redis(host= redis_host, port= redis_port)
            r.set(f"result:{task_id}", result)
        return {"status": "finished", "worker_start_exec_time": worker_start_exec_time}
    except Exception as e:
        error_message= str(e)
        print(f"[FAILED] Task {task_id} failed with error: {error_message}")
        return {"status": "failed", "err_msg": error_message}

class TaskScheduler:
    def __init__(self, resource_mgr: object, status_mgr: object, dag_ctx_mgr: object, redis_ip:str="127.0.0.1",redis_port: int= 6379, proj_path: str= "", model_folder= "model_cache", models_config_path: str= "")-> None:
        self.master_node_id = ray.get_runtime_context().get_node_id()
        self.resource_mgr = resource_mgr
        self.status_mgr = status_mgr
        self.dag_ctx_mgr = dag_ctx_mgr
        self.models_config = {}
        self.models_config_path = os.path.join(proj_path, models_config_path)
        try:
            with open(self.models_config_path, 'r') as f:
                self.models_config = json.load(f)
            print(f"✅ TaskScheduler: Model configuration loaded from '{self.models_config_path}'.")
        except FileNotFoundError:
            print(f"⚠️ TaskScheduler: models_config_path '{self.models_config_path}' not found. No model configs loaded.")
        except json.JSONDecodeError:
            print(f"❌ TaskScheduler: Error decoding JSON from '{self.models_config_path}'.")
        self.backends = {
            "vllm": VLLMBackend(
                resource_manager=self.resource_mgr,
                proj_path= proj_path,
                model_folder= model_folder,
                models_config_path= self.models_config_path
            ),
            "huggingface": HuggingFaceBackend()
        }
        self.vllm_waiting_queue= deque()
        self.task_queue_cpu = queue.PriorityQueue()
        self.task_queue_gpu = queue.PriorityQueue()
        self.task_queue_io = queue.PriorityQueue()
        self.vllm_replica_load = defaultdict(int)
        self.VLLM_RESERVED_FREE_GPUS = 1
        self.VLLM_LOAD_THRESHOLD = 5 # vLLM
        self.VLLM_EVICTION_GRACE_PERIOD = 60
        self.Check_VLLM_Interval = 10 # seconds
        self.redis_ip = redis_ip
        self.redis_port = redis_port
        self.redis_client = redis.Redis(host= redis_ip, port= redis_port)
        self.running_tasks = []
        self.running_tasks_lock = threading.Lock()
        self.resource_lock = threading.Lock()

        self.SINGLE_GPU_MEM_THRESHOLD = self._calculate_min_gpu_memory_threshold()
        print(f"✅ Dynamically calculated single GPU memory threshold: {self.SINGLE_GPU_MEM_THRESHOLD} MiB")

        self.start_cpu_scheduler_loop()
        self.start_io_scheduler_loop()
        self.start_gpu_scheduler_loop()
        self.start_result_monitor()
        self.start_vllm_monitor_loop()
        self.bug_out_control= 0

    def start_vllm_monitor_loop(self):
        """
        vLLM
        """
        def loop():
            while True:
                time.sleep(self.Check_VLLM_Interval) # 10
                with self.resource_lock:
                    # GPU
                    deploying_gpus = self.resource_mgr.find_all_gpus_by_state("DEPLOYING")
                    if not deploying_gpus:
                        continue
                    
                    print(f"🩺 [vLLM Monitor] Checking {len(deploying_gpus)} deploying GPU(s)...")
                    checked_runners = set() 
                    for gpu_info in deploying_gpus:
                        # GPUrunner
                        runner_key = self.resource_mgr.find_runner_key_for_gpu(gpu_info['node_id'], gpu_info['index']) # GPU
                        if runner_key and runner_key not in checked_runners: #

                            node_id, gpu_indices_set = runner_key #

                            gpu_indices = list(gpu_indices_set)
                            is_ready, api_url = self.backends["vllm"].is_server_ready(node_id, gpu_indices) # vllm
                            if is_ready:
                                print(f"✅ [vLLM Monitor] Model on {node_id[:6]}/GPUs {gpu_indices} is now ready!")
                                #

                                self.resource_mgr.update_gpu_state(node_id, gpu_indices, {
                                                                    "status": "OCCUPIED",
                                                                    "request_api_url": api_url,
                                                                    "runner_key": runner_key,
                                                                    "backend": "vllm",
                                                                    "deployment_finish_time": time.time()
                                                                })
                            checked_runners.add(runner_key)
        
        threading.Thread(target=loop, daemon=True, name="VLLMMonitor").start()

    def _calculate_min_gpu_memory_threshold(self) -> int:
        min_mem = float('inf')
        gpus_found = False
        if not self.resource_mgr or not self.resource_mgr.node2avai_resources:
            print("⚠️ Resource manager not ready, using default threshold 24000 MiB.")
            return 24000
        for node_info in self.resource_mgr.node2avai_resources.values():
            for gpu_info in node_info.get("gpu_info", []):
                total_mem = gpu_info.get("gpu_mem_total")
                if total_mem is not None:
                    gpus_found = True
                    if total_mem < min_mem: min_mem = total_mem
        if not gpus_found:
            print("⚠️ No GPUs found in the cluster, using default threshold 24000 MiB.")
            return 24000
        return int(min_mem)

    def _prepare_dag_context(self, task_info: Dict, dag_ctx: ray.actor.ActorHandle) -> None:
        run_id, dag_id = task_info.get('run_id'), task_info.get('dag_id')
        print(f"💾 Preparing context for DAG {run_id}...")
        context_data = {'run_id': run_id, "dag_id": dag_id, "question": task_info.get("question", ""), "answer": task_info.get("answer", "")}
        supplementary_file_paths = task_info.get('supplementary_file_paths', {})
        if supplementary_file_paths:
            file_contents = {}
            for filename, file_path in supplementary_file_paths.items():
                try:
                    with open(file_path, 'rb') as f: content = f.read()
                    file_contents[filename] = content
                except Exception as e: print(f"❌ [Error] Failed to read file {file_path}: {e}")
            context_data["supplementary_files"] = file_contents
        futures = [dag_ctx.put.remote(k, v) for k, v in context_data.items() if v is not None]
        print(f"✅ Context for DAG {dag_id} (run_id, {run_id}) is ready.")

    def _is_vllm_replica_full(self, api_url: str) -> bool:
        """viavLLMAPIrequest"""
        if not api_url: return True # URL
        if self.vllm_replica_load[api_url]>= self.VLLM_LOAD_THRESHOLD:
            return True

    def _find_gpu_placement_on_node(self, node_id: str, task_info: dict) -> Tuple[bool, List[int]]:
        requested_mem = float(task_info.get("gpu_mem", 0))
        node_res = self.resource_mgr.node2avai_resources.get(node_id)
        if not node_res: return False, []

        is_large_task = requested_mem > self.SINGLE_GPU_MEM_THRESHOLD
        available_gpus = [gpu for gpu in node_res.get("gpu_info", []) if gpu.get("status", 'FREE') == 'FREE']
        
        if is_large_task:
            total_available_mem = sum(gpu['gpu_mem'] for gpu in available_gpus)
            if total_available_mem < requested_mem: return False, []
            
            sorted_gpus = sorted(available_gpus, key=lambda g: g['gpu_mem'], reverse=True)
            mem_sum = 0
            selected_indices = []
            for gpu in sorted_gpus:
                mem_sum += gpu['gpu_mem']
                selected_indices.append(gpu['index'])
                if mem_sum >= requested_mem: return True, selected_indices
            return False, []
        else:
            candidate_gpus = [gpu for gpu in available_gpus if gpu['gpu_mem'] >= requested_mem]
            if candidate_gpus:
                best_gpu = max(candidate_gpus, key=lambda g: g['gpu_mem'])
                return True, [best_gpu['index']]
            else:
                return False, []

    def choice_cpu_gpu_queue_according_resource(self, remaining_gpu_mem, priority, task_info):
        # if remaining_gpu_mem <= 0:
            # APICPU
            # task_info['type'] = 'cpu'
            # task_info.pop('gpu_mem', None)
            # self.task_queue_cpu.put((priority, task_info))
            # print(f"✅ Activated and Demoted task '{task_info['func_name']}' to CPU queue.")
        # else:
        # GPU
        task_info['gpu_mem'] = remaining_gpu_mem
        self.task_queue_gpu.put(((float('-inf'),) + priority, task_info))
        print(f"✅ Activated task '{task_info['func_name']}'. Re-enqueued as normal GPU task.")

    def start_gpu_scheduler_loop(self):
        def loop():
            while True:
                if self.vllm_waiting_queue:
                    _, task_info_peek = self.vllm_waiting_queue[0]
                    model_name_peek = task_info_peek.get("model_name")
                    ready_replicas = self.resource_mgr.find_gpus_by_model(
                        model_name_peek, "vllm", status="OCCUPIED"
                    )
                    available_replicas = [r for r in ready_replicas if not self._is_vllm_replica_full(r.get("request_api_url"))] # “”
                    if available_replicas: #

                        available_replica = min(
                            available_replicas,
                            key=lambda r: self.vllm_replica_load.get(r.get("request_api_url"), 0)
                        )
                        priority, task_info = self.vllm_waiting_queue.popleft() #

                        target_api_url = available_replica.get('request_api_url') # api_url
                        task_info[f'{task_info["func_name"]}_request_api_url'] = target_api_url # url
                        #

                        self.vllm_replica_load[target_api_url]+= 1 # + 1
                        model_mem = self.models_config.get(task_info.get("model_name"), {}).get("gpu_mem", 80000)
                        self.choice_cpu_gpu_queue_according_resource(task_info.get('gpu_mem', 0)- model_mem, priority, task_info)
                        print(f"✅ Activated waiting task '{task_info['func_name']}' for model '{model_name_peek}'.")
                        #

                        continue
                #

                if self.task_queue_gpu.empty(): 
                    time.sleep(0.05)
                    continue

                priority, task_info = self.task_queue_gpu.get()
                run_id, task_id, pre_scheduled_node_id, func_name, model_name, backend_type= task_info["run_id"], task_info["task_id"], task_info.get("node_id"), task_info.get("func_name"), task_info.get("model_name"), task_info.get("backend", "huggingface")
                request_api_url= task_info.get(f"{func_name}_request_api_url", None)
                backend = self.backends[backend_type]
                gpu_indices_for_dispatch = []
                dag_ctx= self.dag_ctx_mgr.get_context(run_id)
                placement_found, selected_node_id, target_api_url= False, None, None

                with self.resource_lock:
                    # A: vLLM
                    if backend_type == 'vllm' and not request_api_url: # vllm
                        replicas = self.resource_mgr.find_gpus_by_model(model_name, "vllm", "OCCUPIED") #

                        ready_replicas = [r for r in replicas if not self._is_vllm_replica_full(r.get("request_api_url"))]
                        model_mem = self.models_config.get(model_name, {}).get("gpu_mem", 80000) # GPU
                        if ready_replicas: #

                            available_replica = min( #

                                ready_replicas,
                                key=lambda r: self.vllm_replica_load.get(r.get("request_api_url"), 0)
                            )
                            target_api_url= available_replica['request_api_url'] #

                            task_info[f"{func_name}_request_api_url"]= target_api_url #

                            self.vllm_replica_load[target_api_url]+= 1 # + 1
                            self.choice_cpu_gpu_queue_according_resource(task_info.get('gpu_mem', 0) - model_mem, priority, task_info) # cpugpu
                            print(f"✅ Found existing replica for '{model_name}' at {target_api_url}.")
                            continue
                        else: # GPU
                            should_deploy_model= False
                            num_requests_waiting= 1+ sum(1 for _, t_info in self.vllm_waiting_queue if t_info.get("model_name")== model_name)
                            ready_replicas_count= len(replicas) #

                            deploying_replicas_count= len(set(gpu.get("runner_key") for gpu in self.resource_mgr.find_all_gpus_by_state("DEPLOYING") if gpu.get("model_name") == model_name)) #

                            replicas_count= ready_replicas_count+ deploying_replicas_count
                            should_deploy_model= True if replicas_count== 0 or num_requests_waiting* 1.0/ replicas_count> self.VLLM_LOAD_THRESHOLD else False
                            if should_deploy_model:
                                # ---  ---
                                num_free_gpus = len(self.resource_mgr.find_all_gpus_by_state("FREE"))
                                if num_free_gpus> self.VLLM_RESERVED_FREE_GPUS:
                                    deployment_node_id, deployment_indices, runner_key= None, None, None
                                    for node_id_search in self.resource_mgr.node2avai_resources.keys(): #

                                        can_deploy, indices = self._find_gpu_placement_on_node(node_id_search, {"gpu_mem": model_mem})
                                        if can_deploy:
                                            deployment_node_id, deployment_indices = node_id_search, indices
                                            runner_key= (deployment_node_id, frozenset(deployment_indices)) #

                                            break
                                    if deployment_node_id: #

                                        print(f"💡 Triggering deployment for '{model_name}' on Node {deployment_node_id[:6]}, GPU(s) {deployment_indices}.")
                                        backend.deploy(deployment_node_id, deployment_indices, model_name)
                                        self.resource_mgr.update_gpu_state(deployment_node_id, deployment_indices, {"status": "DEPLOYING", "model_name": model_name, "backend": "vllm", "runner_key": runner_key}) # GPU
                                        self.resource_mgr.reduce_node_resource(deployment_node_id, deployment_indices, {"mem": 0, "type": "gpu", "gpu_mem": model_mem}) #

                                        self.vllm_waiting_queue.append((priority, task_info)) #

                                        continue
                            else:
                                self.vllm_waiting_queue.append((priority, task_info))
                                continue
                    else:
                        if pre_scheduled_node_id:
                            can_run, indices = self._find_gpu_placement_on_node(pre_scheduled_node_id, task_info)
                            if can_run:
                                placement_found= True
                                print(f"✅ Accepting pre-scheduled placement on node '{pre_scheduled_node_id}' for GPUs {indices}.")
                                selected_node_id = pre_scheduled_node_id
                                gpu_indices_for_dispatch = indices
                                if not dag_ctx:
                                    node_ip = self.resource_mgr.id2ip.get(selected_node_id, "")
                                    dag_ctx = self.dag_ctx_mgr.create_context(pre_scheduled_node_id, node_ip, task_info)
                                    self._prepare_dag_context(task_info, dag_ctx)
                        else:
                            if dag_ctx:
                                affinity_node_id= self.dag_ctx_mgr.ctx2id.get(dag_ctx)
                                # print(f"🔍 Checking data-affinity node '{affinity_node_id}'...")
                                can_run, indices= self._find_gpu_placement_on_node(affinity_node_id, task_info)
                                if can_run:
                                    placement_found= True
                                    print(f"  -> ✅ Affinity node has resources. Placing on GPUs {indices}.")
                                    selected_node_id = affinity_node_id
                                    gpu_indices_for_dispatch = indices

                            if not placement_found:
                                candidate_nodes= []
                                affinity_node_to_exclude = dag_ctx and self.dag_ctx_mgr.ctx2id.get(dag_ctx)
                                for node_id_search in self.resource_mgr.node2avai_resources.keys():
                                    if node_id_search== affinity_node_to_exclude:
                                        continue
                                    can_run, indices = self._find_gpu_placement_on_node(node_id_search, task_info)
                                    # print(f"  -> 🌀 Affinity_node_to_exclude: {affinity_node_to_exclude}, node resource_mgr.node2avai_resources: {self.resource_mgr.node2avai_resources}")
                                    # print(f"  -> 🆒 Task id: {task_id}, checking node '{node_id_search[:6]}' for GPU placement: {can_run}, indices: {indices}.")
                                    if can_run:
                                        candidate_nodes.append(node_id_search)
                                if candidate_nodes:
                                    best_node_id = self.dag_ctx_mgr.get_least_loaded_node(candidate_nodes)
                                    # print(f"  -> 😄Candidate nodes with resources: {candidate_nodes}. Choosing least loaded: '{best_node_id}'.")
                                    _, indices = self._find_gpu_placement_on_node(best_node_id, task_info)
                                    selected_node_id = best_node_id
                                    gpu_indices_for_dispatch = indices
                                    placement_found = True

                                if not dag_ctx and placement_found:
                                    node_ip = self.resource_mgr.id2ip.get(selected_node_id, "")
                                    dag_ctx = self.dag_ctx_mgr.create_context(selected_node_id, node_ip, task_info)
                                    self._prepare_dag_context(task_info, dag_ctx)
                
                if placement_found:
                    self.resource_mgr.reduce_node_resource(selected_node_id, gpu_indices_for_dispatch, task_info)
                    print(f"[{time.strftime('%H:%M:%S')}] 🧠 GPU Scheduler Loop: Dequeued Task")
                    print(f"  -> Task to Schedule: '{func_name}' (run_id: {run_id}) Priority: {priority}")
                    dag_ctx = self.dag_ctx_mgr.get_context(run_id)
                    if request_api_url:ray.get(dag_ctx.put.remote(f"{func_name}_request_api_url", request_api_url)) # API URLDAG
                    print(f"🚀 Dispatching GPU task '{task_info['func_name']}' to node '{selected_node_id}' on GPUs {gpu_indices_for_dispatch}.")
                    self.status_mgr.set_selected_node(run_id, task_id, selected_node_id)
                    self.status_mgr.set_status(run_id, task_id, "running")
                    self._dispatch_task(run_id, task_id, task_info['func_name'], self.redis_ip, self.redis_port, "gpu", gpu_indices_for_dispatch)
                else:
                    # 4: 
                    idle_candidates = [gpu for gpu in self.resource_mgr.find_all_gpus_by_state("OCCUPIED") 
                                       if gpu.get("backend") == "vllm" 
                                       and self.vllm_replica_load.get(gpu.get("request_api_url"), 0) == 0
                                       and (time.time() - gpu.get('deployment_finish_time', 0) > self.VLLM_EVICTION_GRACE_PERIOD)] #

                    # for gpu in self.resource_mgr.find_all_gpus_by_state("OCCUPIED"):
                    #     if gpu.get("backend") == "vllm" and self.bug_out_control% 500== 0:
                    #         print(f"DEBUG: Global search for task '{task_info['func_name']}'. Current ResourceManager state:")
                    #         print(self.resource_mgr.node2avai_resources)
                    #         print(f"  -> Model times situation: {self.vllm_replica_load}")
                    #         print(f"  -> gpu, backend: {backend}; state: {gpu.get('status')}, memory: {gpu.get('gpu_mem')}, request_api_url time: {self.vllm_replica_load.get(gpu.get('request_api_url'), 0)}; protected time: {time.time() - gpu.get('deployment_finish_time', 0)}")
                    # self.bug_out_control+= 1
                    if idle_candidates:
                        lru_runner_info= self.resource_mgr.find_lru_runner(idle_candidates)
                        if lru_runner_info:
                            node_id_to_evict, indices_to_evict, model_to_evict = lru_runner_info
                            print(f"💡 Evicting idle model '{model_to_evict}' on Node {node_id_to_evict[:6]}/GPUs {indices_to_evict} to free up resources.")
                            #

                            model_mem = self.models_config.get(model_to_evict, {}).get("gpu_mem", 80000)
                            self.resource_mgr.add_node_resource(node_id_to_evict, indices_to_evict, 
                                                            {"mem": 0, "type": "gpu", "gpu_mem": model_mem})
                            # Runner
                            self.backends["vllm"].undeploy(node_id_to_evict, indices_to_evict)
                    self.task_queue_gpu.put((priority, task_info))
                time.sleep(0.05)
        threading.Thread(target=loop, daemon=True).start()

    def _find_cpu_io_placement_on_node(self, node_id: str, task_info: dict) -> bool:
        node_res = self.resource_mgr.node2avai_resources.get(node_id)
        if not node_res: return False
        task_type = task_info.get("type")
        if task_type == "cpu":
            cpu_ok = node_res.get("cpu_num", 0) >= float(task_info.get("cpu_num", 1))
            mem_ok = node_res.get("mem", 0) >= float(task_info.get("mem", 0))
            return cpu_ok and mem_ok
        elif task_type == "io":
            io_ok = node_res.get("io_task", 0) > 0
            mem_ok = node_res.get("mem", 0) >= float(task_info.get("mem", 0))
            return io_ok and mem_ok
        return False

    def start_cpu_scheduler_loop(self):
        def loop():
            while True:
                if self.task_queue_cpu.empty(): 
                    time.sleep(0.05)
                    continue

                priority, task_info= self.task_queue_cpu.get()
                dag_id, run_id, task_id, pre_scheduled_node_id, arrival_time = task_info["dag_id"], task_info["run_id"], task_info["task_id"], task_info.get("node_id"), task_info.get("arrival_time")
                selected_node_id = None
                dag_ctx = self.dag_ctx_mgr.get_context(run_id)
                placement_found = False                    

                with self.resource_lock:
                    # 1
                    if pre_scheduled_node_id:
                        can_run = self._find_cpu_io_placement_on_node(pre_scheduled_node_id, task_info)
                        if can_run:
                            placement_found = True
                            print(f"✅ Accepting pre-scheduled placement on node '{pre_scheduled_node_id}' for CPU task.")
                            selected_node_id = pre_scheduled_node_id
                            # DAG
                            if not dag_ctx:
                                node_ip = self.resource_mgr.id2ip.get(selected_node_id, "")
                                dag_ctx = self.dag_ctx_mgr.create_context(pre_scheduled_node_id, node_ip, task_info)
                                self._prepare_dag_context(task_info, dag_ctx)
                    # 2
                    else:
                        if dag_ctx:
                            affinity_node_id = self.dag_ctx_mgr.ctx2id.get(dag_ctx)
                            can_run = self._find_cpu_io_placement_on_node(affinity_node_id, task_info)
                            if can_run:
                                placement_found = True
                                print(f"  -> ✅ Affinity node has resources.")
                                selected_node_id = affinity_node_id
                        
                        # 3
                        if not placement_found:
                            # print(f"🌀 No pre-schedule or affinity placement found for CPU task. Starting global search...")
                            #

                            affinity_node_to_exclude = dag_ctx and self.dag_ctx_mgr.ctx2id.get(dag_ctx)
                            candidate_nodes = []
                            for node_id_search in self.resource_mgr.node2avai_resources.keys():
                                if node_id_search == affinity_node_to_exclude:
                                    continue
                                if self._find_cpu_io_placement_on_node(node_id_search, task_info):
                                    candidate_nodes.append(node_id_search)

                            if candidate_nodes:
                                best_node_id = self.dag_ctx_mgr.get_least_loaded_node(candidate_nodes)
                                print(f"  -> Candidate nodes: {candidate_nodes}. Choosing least loaded: '{best_node_id}'.")
                                selected_node_id = best_node_id
                                placement_found = True
                        
                            # DAG
                            if not dag_ctx and placement_found:
                                node_ip = self.resource_mgr.id2ip.get(selected_node_id, "")
                                dag_ctx = self.dag_ctx_mgr.create_context(selected_node_id, node_ip, task_info)
                                self._prepare_dag_context(task_info, dag_ctx)
                                    
                #

                if placement_found:
                    self.resource_mgr.reduce_node_resource(selected_node_id, None, task_info)
                    print(f"🚀 Dispatching CPU task '{task_info['func_name']}' to node '{selected_node_id}'.")
                    self.status_mgr.set_selected_node(run_id, task_id, selected_node_id)
                    self.status_mgr.set_status(run_id, task_id, "running")
                    self._dispatch_task(run_id, task_id, task_info['func_name'], self.redis_ip, self.redis_port, "cpu", None)
                else:
                    self.task_queue_cpu.put((priority, task_info))
                time.sleep(0.05)
        threading.Thread(target=loop, daemon=True).start()

    def start_io_scheduler_loop(self):
        def loop():
            while True:
                if self.task_queue_io.empty(): 
                    time.sleep(0.05)
                    continue
                priority, task_info = self.task_queue_io.get()
                dag_id, run_id, task_id, pre_scheduled_node_id, arrival_time = task_info["dag_id"], task_info["run_id"], task_info["task_id"], task_info.get("node_id"), task_info.get("arrival_time")
                selected_node_id = None
                dag_ctx = self.dag_ctx_mgr.get_context(run_id)
                placement_found = False

                with self.resource_lock:
                    # 1
                    if pre_scheduled_node_id:
                        can_run = self._find_cpu_io_placement_on_node(pre_scheduled_node_id, task_info)
                        if can_run:
                            placement_found = True
                            print(f"✅ Accepting pre-scheduled placement on node '{pre_scheduled_node_id}' for IO task.")
                            selected_node_id = pre_scheduled_node_id
                            if not dag_ctx:
                                node_ip = self.resource_mgr.id2ip.get(selected_node_id, "")
                                dag_ctx = self.dag_ctx_mgr.create_context(pre_scheduled_node_id, node_ip, task_info)
                                self._prepare_dag_context(task_info, dag_ctx)
                    # 2
                    else:
                        if dag_ctx:
                            affinity_node_id = self.dag_ctx_mgr.ctx2id.get(dag_ctx)
                            # print(f"🔍 Checking data-affinity node '{affinity_node_id}' for IO task...")
                            can_run = self._find_cpu_io_placement_on_node(affinity_node_id, task_info)
                            if can_run:
                                placement_found = True
                                print(f"  -> ✅ Affinity node has resources.")
                                selected_node_id = affinity_node_id
                        
                        # 3
                        if not placement_found:
                            # print(f"🌀 No pre-schedule or affinity placement found for IO task. Starting global search...")
                            affinity_node_to_exclude = dag_ctx and self.dag_ctx_mgr.ctx2id.get(dag_ctx)
                            candidate_nodes = []
                            for node_id_search in self.resource_mgr.node2avai_resources.keys():
                                if node_id_search == affinity_node_to_exclude:
                                    continue
                                if self._find_cpu_io_placement_on_node(node_id_search, task_info):
                                    candidate_nodes.append(node_id_search)
                            if candidate_nodes:
                                best_node_id = self.dag_ctx_mgr.get_least_loaded_node(candidate_nodes)
                                print(f"  -> Candidate nodes: {candidate_nodes}. Choosing least loaded: '{best_node_id}'.")
                                selected_node_id = best_node_id
                                placement_found = True
                                
                            if not dag_ctx and placement_found:
                                node_ip = self.resource_mgr.id2ip.get(selected_node_id, "")
                                dag_ctx = self.dag_ctx_mgr.create_context(selected_node_id, node_ip, task_info)
                                self._prepare_dag_context(task_info, dag_ctx)

                if placement_found:
                    self.resource_mgr.reduce_node_resource(selected_node_id, None, task_info)
                    print(f"🚀 Dispatching IO task '{task_info['func_name']}' to node '{selected_node_id}'.")
                    self.status_mgr.set_selected_node(run_id, task_id, selected_node_id)
                    self.status_mgr.set_status(run_id, task_id, "running")
                    self._dispatch_task(run_id, task_id, task_info['func_name'], self.redis_ip, self.redis_port, "io", None)
                else:
                    self.task_queue_io.put((priority, task_info))
                time.sleep(0.05)
        threading.Thread(target=loop, daemon=True).start()

    def start_result_monitor(self):
        def monitor():
            redis_client = redis.Redis(host=self.redis_ip, port=self.redis_port, decode_responses=True)
            completion_queue_name = "task_completion_queue"
            while True:
                if not self.running_tasks:
                    time.sleep(0.05)
                    continue
                with self.running_tasks_lock:
                    ready_refs = [task["ref"] for task in self.running_tasks]
                    ready, _ = ray.wait(ready_refs, num_returns=len(ready_refs), timeout= 0.05)
                    if not ready: continue
                    ref_to_task = {task["ref"]: task for task in self.running_tasks}
                    for ref in ready:
                        task = ref_to_task.pop(ref)
                        func_name= task.get("func_name")
                        result = ray.get(ref)
                        receive_task_result_time= time.time()
                        with self.resource_lock:
                            if task.get("backend") == "vllm":
                                api_url = task.get(f"{func_name}_request_api_url")
                                if api_url and api_url in self.vllm_replica_load:
                                    self.vllm_replica_load[api_url] = max(0, self.vllm_replica_load[api_url] - 1)
                                    print(f"  -> 📉 Decremented load for {api_url}. New load: {self.vllm_replica_load[api_url]}")
                            self.resource_mgr.add_node_resource(task["node_id"], task.get("gpu_indices"), task)

                        run_id, task_id = task["run_id"], task["task_id"]
                        final_status = "finished" if result.get("status") == "finished" else "failed"
                        if final_status == "finished":
                            self.status_mgr.set_status(run_id, task_id, "finished")
                            print(f"✅ [SUCCESS] Task {task_id} ('{func_name}') completed.")
                        else:
                            self.status_mgr.set_status(run_id, task_id, "failed", err_msg= result.get("err_msg"))
                            print(f"❌ [FAILED] Task {task_id} ('{func_name}') failed: {result.get('err_msg')}")
                        
                        # ****
                        notification = {
                            "dag_id": task.get("dag_id"), "run_id": run_id, "task_id": task_id,
                            "func_name": func_name, "status": final_status, "dispatch_task_time": task["dispatch_task_time"],
                            "receive_task_result_time": receive_task_result_time, "worker_start_exec_time": result.get("worker_start_exec_time")
                        }
                        try:
                            redis_client.lpush(completion_queue_name, json.dumps(notification))
                            print(f"  -> 📬 Notified completion of '{func_name}' via Redis queue.")
                        except Exception as e:
                            print(f"❌ [Error] Failed to send completion notification for '{func_name}': {e}")
                    self.running_tasks = list(ref_to_task.values())
                time.sleep(0.05)
        threading.Thread(target=monitor, daemon=True).start()

    def submit(self, task_info, priority= None):
        self.status_mgr.add_task(task_info)
        task_type = task_info.get("type", "cpu")
        if priority is None:
            priority= time.time()
        if isinstance(priority, list):
            priority = tuple(priority) #  list  tuple
        elif isinstance(priority, (int, float)):
            priority = (priority,) #

        print(f"💮 now receive submit from dag_id: {task_info['dag_id']}, task_id: {task_info['task_id']}")
        item_to_queue= (priority, task_info)
        if task_type == "gpu": self.task_queue_gpu.put(item_to_queue)
        elif task_type == "io": self.task_queue_io.put(item_to_queue)
        else: self.task_queue_cpu.put(item_to_queue)

    def _dispatch_task(self, run_id: str, task_id: str, func_name:str, redis_host: str, redis_port: str, task_type:str, gpu_indices: Optional[List[int]]):
        try:
            redis_func_key= f"func:{task_id}"
            serialized_func= None
            for attempt in range(5):
                serialized_func = self.redis_client.get(redis_func_key)
                if serialized_func:break
                print(f"  -> [Attempt {attempt+1}/5] Function for task {task_id} not yet in Redis. Retrying in 0.1s...")
                time.sleep(0.05* attempt)
            if not serialized_func: raise ValueError(f"Function for task {task_id} not found in Redis.")
            self.redis_client.delete(redis_func_key)

            node_id = self.status_mgr.get_selected_node(run_id, task_id)
            ctx = self.dag_ctx_mgr.get_context(run_id)

            gpus_to_request = 0
            if gpu_indices and task_type == 'gpu':
                if len(gpu_indices) > 1:
                    # GPU: 
                    gpus_to_request = len(gpu_indices)
                    print(f"  -> Dispatching multi-GPU task, requesting {gpus_to_request} full GPUs.")
                else:
                    # GPU: 
                    total_gpu_mem = self.resource_mgr.node2avai_resources[node_id]["gpu_info"][gpu_indices[0]]["gpu_mem_total"]
                    task_info = self.status_mgr.get_task_info(run_id, task_id)
                    requested_mem = task_info.get("gpu_mem", 2048)
                    # (0.001)0
                    gpus_to_request = max(0.001, requested_mem/ total_gpu_mem)
                    print(f"  -> Dispatching single-GPU task, requesting fraction {gpus_to_request:.4f} of a GPU.")
            dispatch_task_time= time.time()
            result_ref = remote_task_runner.options(
                num_gpus= gpus_to_request if gpu_indices and task_type == 'gpu' else 0,
                scheduling_strategy= ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(node_id=node_id, soft=False)
            ).remote(serialized_func, ctx, task_id, redis_host, redis_port, task_type, gpu_indices)
          
            with self.running_tasks_lock:
                task_info = self.status_mgr.task_info_map.get((run_id, task_id), {})
                task_to_run = {
                    "run_id": run_id, "dag_id": task_info.get('dag_id'), "task_id": task_id,
                    "func_name": func_name, "node_id": node_id, "ref": result_ref, "type": task_type,
                    "gpu_indices": gpu_indices,
                    "gpu_mem": task_info.get("gpu_mem", 0),
                    "cpu_num": task_info.get("cpu_num", 0), "mem": task_info.get("mem", 0),
                    "backend": task_info.get("backend"),
                    f"{func_name}_request_api_url": task_info.get(f"{func_name}_request_api_url"),
                    "dispatch_task_time": dispatch_task_time
                }
                self.running_tasks.append(task_to_run)
        except Exception as e:
            print(f"[DISPATCH FAILED] Task {task_id} failed to dispatch: {e}")
            self.status_mgr.set_status(run_id, task_id, "failed", err_msg= str(e))