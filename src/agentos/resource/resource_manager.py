import ray
import json
import redis
import time
from typing import Dict, Optional, List, Tuple
import subprocess
import threading

@ray.remote
def get_gpu_info():
    try:
        #  nvidia-smi 
        result = subprocess.run(['nvidia-smi', '--query-gpu=index,name,utilization.gpu,memory.total,memory.used,memory.free', '--format=csv,noheader,nounits'], stdout=subprocess.PIPE)
        
        #

        output = result.stdout.decode('utf-8')
        
        #

        lines = output.strip().split('\n')
        
        info = []
        for line in lines:
            values = line.split(', ')
            gpu_index = int(values[0])
            name = values[1]
            utilization = int(values[2])
            memory_total = int(values[3])
            memory_used = int(values[4])
            memory_free = int(values[5])
            
            gpu_info = {
                'index': gpu_index,
                'name': name,
                'utilization': utilization,
                'memory_total': memory_total,
                'memory_used': memory_used,
                'memory_free': memory_free
            }
            info.append(gpu_info)
            
        return info
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
        
class ComputeNodeResourceManager:
    """
    Resource manager for compute nodes, handling resource allocation and status.
    """

    def __init__(self)-> None:
        """
        Initialize the compute node resource manager.

        :param monitor_cls: Monitor class for node status (defaults to ComputeNodeStatusMonitor)
        :param redis_port: Redis server port (default is 6380)
        """
        self.lock = threading.Lock()
        self.node2avai_resources= {}
        self.id2ip= {}
        self.ip2id= {}

        nodes_info = ray.nodes()
        for info in nodes_info:
            if info["Alive"]:
                node_ip= info["NodeManagerAddress"]
                node_id = info["NodeID"]
                cpu_num = info["Resources"].get("CPU", 0)
                mem = info["Resources"]["memory"]

                self.id2ip[node_id]= node_ip
                self.ip2id[node_ip]= node_id

                self.node2avai_resources[node_id] = {}
                self.node2avai_resources[node_id]["cpu_num"] = cpu_num
                self.node2avai_resources[node_id]["mem"] = mem /1024 /1024
                self.node2avai_resources[node_id]["gpu_info"] = []
                gpus_info = self.collect_gpus_info(node_id)

                for gpu_info in gpus_info:
                    self.node2avai_resources[node_id]["gpu_info"].append({
                        "index":gpu_info["index"],
                        "name":gpu_info["name"],
                        "gpu_mem":gpu_info["memory_free"],
                        "gpu_mem_total": gpu_info["memory_total"],
                        "status": 'FREE',
                        "request_api_url": None,
                        "backend": None,
                        "last_used_time": time.time() # LRU strategy
                    })
                
                self.node2avai_resources[node_id]["io_task"] = 20

        
        print("======node======")
        print(self.node2avai_resources)

    def collect_gpus_info(self,node_id:str):
        object_ref = get_gpu_info.options(
            scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                node_id=node_id,
                soft=False,
            )
        ).remote()  
        res = ray.get(object_ref)     
        print(res) 
        return res

    def find_runner_key_for_gpu(self, node_id: str, gpu_index: int) -> Optional[Tuple[str, frozenset]]:
        """
        GPUIDvLLM RunnerKey
        """
        with self.lock:
            try:
                gpu_info = self.node2avai_resources[node_id]["gpu_info"][gpu_index]
                return gpu_info.get("runner_key")
            except (KeyError, IndexError):
                return None

    def update_gpu_state(self, node_id: str, gpu_indices: List[int], state_updates: dict):
        """GPU"""
        with self.lock:
            try:
                # state_updates  'runner_key' 
                runner_key = state_updates.get("runner_key")
                for gpu_index in gpu_indices:
                    gpu_to_update = self.node2avai_resources[node_id]["gpu_info"][gpu_index]
                    gpu_to_update.update(state_updates)
                    gpu_to_update["last_used_time"] = time.time()
                    if runner_key:
                        gpu_to_update["runner_key"] = runner_key #


                print(f"🔄 GPU State Updated: Node {node_id[:6]}, GPUs {gpu_indices} -> {state_updates}")
            except (KeyError, IndexError) as e:
                print(f"❌ Error updating GPU state for Node {node_id}, GPUs {gpu_indices}: {e}")


    def find_gpus_by_model(self, model_name: str, backend_type: str, status: str= None) -> List[Dict]:
        """modelGPU"""
        found_gpus = []
        with self.lock:
            for node_id, resources in self.node2avai_resources.items():
                for gpu_info in resources.get("gpu_info", []):
                    # <--- MODIFIED:  ---
                    if (gpu_info.get("model_name") == model_name and
                        gpu_info.get("backend") == backend_type and 
                        (not status or gpu_info.get("status") == status)):
                        found_gpus.append({"node_id": node_id, **gpu_info})
        return found_gpus

    def find_all_gpus_by_state(self, status: str) -> List[Dict]:
        """GPU"""
        found_gpus = []
        with self.lock:
            for node_id, resources in self.node2avai_resources.items():
                for gpu_info in resources.get("gpu_info", []):
                    if gpu_info.get("status") == status:
                        found_gpus.append({"node_id": node_id, **gpu_info})
        return found_gpus

    def find_lru_runner(self, idle_gpu_candidates: List[Dict]) -> Optional[Tuple[str, List[int], str]]:
        """
        GPUusemodel (Runner)
         (node_id, [gpu_indices], model_name)
        """
        if not idle_gpu_candidates:
            return None
        # 1:  runner_key GPU
        runners = {}
        for gpu_info in idle_gpu_candidates:
            runner_key = gpu_info.get("runner_key")
            if not runner_key:
                continue
            if runner_key not in runners:
                runners[runner_key] = {
                    "last_used_time": gpu_info.get("last_used_time", time.time()),
                    "model_name": gpu_info.get("model_name"),
                }
        if not runners:
            return None
        # 2:  runner
        lru_runner_key = min(runners.keys(), key=lambda k: runners[k]['last_used_time'])
        # 3: 
        node_id, gpu_indices_set = lru_runner_key
        model_name = runners[lru_runner_key]['model_name']
        return node_id, list(gpu_indices_set), model_name

    def reduce_node_resource(self, node_id: str, gpu_indices: Optional[List[int]], task_info: dict)-> bool:
        """
        Reduce the available resources of a specific node by the required resources for a task.

        :param node_id: Node ID
        :param task_info: Dictionary containing task information
        :return: True if resources were successfully reduced, False otherwise
        """
        self.node2avai_resources[node_id]["mem"]-= task_info["mem"]  
        self.node2avai_resources[node_id]["cpu_num"]-= 1

        if task_info["type"] == "cpu":
            self.node2avai_resources[node_id]["cpu_num"]-= (task_info["cpu_num"]- 1)
        
        if task_info["type"] == "gpu":
            is_multi_gpu_task= len(gpu_indices)> 1
            if is_multi_gpu_task:
                for index in gpu_indices:
                    self.node2avai_resources[node_id]["gpu_info"][index]["gpu_mem"]-= task_info.get("gpu_mem") / len(gpu_indices)
                    self.node2avai_resources[node_id]["gpu_info"][index]["status"]= "OCCUPIED"
            else:
                self.node2avai_resources[node_id]["gpu_info"][gpu_indices[0]]["gpu_mem"]-= task_info["gpu_mem"]

        if task_info["type"] == "io":
            self.node2avai_resources[node_id]["io_task"]-= 1


    def add_node_resource(self, node_id: str, gpu_indices: Optional[List[int]], task_info: dict)-> None:
        """
        Return resources to a specific node after a task is completed.

        :param node_id: Node ID
        :param task_info: Dictionary containing task information
        :return: True if resources were successfully returned, False otherwise
        """
        self.node2avai_resources[node_id]["mem"]+= task_info["mem"]  
        self.node2avai_resources[node_id]["cpu_num"]+= 1
        
        if task_info["type"] == "cpu":
            self.node2avai_resources[node_id]["cpu_num"]+= (task_info["cpu_num"]- 1)

        if task_info["type"] == "gpu":
            is_multi_gpu_task= len(gpu_indices)> 1
            if is_multi_gpu_task:
                for index in gpu_indices:
                    self.node2avai_resources[node_id]["gpu_info"][index]["gpu_mem"]+= task_info.get("gpu_mem") / len(gpu_indices)
                    self.node2avai_resources[node_id]["gpu_info"][index]["status"]= "FREE"       
            else:
                self.node2avai_resources[node_id]["gpu_info"][gpu_indices[0]]["gpu_mem"]+= task_info["gpu_mem"]

        if task_info["type"] == "io":
            self.node2avai_resources[node_id]["io_task"]+= 1