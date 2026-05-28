import ray
import time
import logging
import copy
from typing import Any,List,Dict
from maze.core.scheduler.runtime import SelectedNode
from maze.core.scheduler.runtime import TaskRuntime
from maze.core.scheduler.runtime import SelectedNode
from maze.utils.utils import collect_gpu_info

logger = logging.getLogger(__name__)

class Node():
    def __init__(self,node_id:str,node_ip:str,available_resources:dict,total_resources:dict):
        self.node_id = node_id
        self.node_ip = node_ip
        self.available_resources = copy.deepcopy(available_resources)
        self.total_resources = copy.deepcopy(total_resources)
  
    def release_resource(self,resources:dict,gpu_id:int = None):
        cpu = resources["cpu"]
        cpu_mem = resources["cpu_mem"]
        gpu = resources["gpu"]
        gpu_mem = resources["gpu_mem"]

        self.available_resources['cpu'] += cpu
        self.available_resources['cpu_mem'] += cpu_mem
        
        if gpu_id is not None:
            self.available_resources['gpu_resource'][gpu_id]['gpu_mem'] += gpu_mem
            self.available_resources['gpu_resource'][gpu_id]['gpu_num'] += gpu
            
class ResourceManager():
    def __init__(self):
        self.head_node_id = None
        self.head_node_ip = None
        self.nodes:Dict[str,Node] = {}
        
        self.last_time = time.time()
        self.interval = 3

    def _get_head_node_resource(self):
        '''
        Get the maze head node resource
        '''
        head_resource = {}

        head_node = None
        for node in ray.nodes():
            if node["NodeID"] == self.head_node_id:
                head_node = node
                break
        assert(head_node is not None)

        head_resource = {
            "cpu":head_node["Resources"]["CPU"],
            "cpu_mem":head_node["Resources"]["memory"],   
            "gpu_resource":{}
        }

        gpu_info = collect_gpu_info()
        if len(gpu_info) > 0:
            for gpu in gpu_info:
                gpu_id = gpu["index"]
                gpu_mem = gpu["memory_free"]
                head_resource["gpu_resource"][gpu_id] = {
                    "gpu_id" : gpu_id,
                    "gpu_mem":gpu_mem,
                    "gpu_num":1
                }

        return head_resource

    def init(self):
        '''
        Init maze head
        '''
        ray.init(address='auto')
        self.head_node_id = ray.get_runtime_context().get_node_id()
        self.head_node_ip = ray.util.get_node_ip_address()
        head_node_resource = self._get_head_node_resource()

        #Wait for ray head launch
        while True:
            for node in ray.nodes():
                if node["NodeID"] == self.head_node_id and node["Alive"]:     
                    self.nodes[self.head_node_id] = Node(self.head_node_id,self.head_node_ip,head_node_resource,head_node_resource)
                    return
                    
    def check_dead_node(self): 
        nodes = ray.nodes()
        for node in nodes:
            if node["NodeID"] in self.nodes and not node["Alive"]:
                del self.nodes[node["NodeID"]]
                
    def show_all_node_resource(self):
        '''
        Show all node resource
        '''
        cur_time = time.time()
        if cur_time - self.last_time >= self.interval:
            self.last_time = cur_time
            
            logger.debug("===Show All Node===")
            logger.debug("Total Node: %s", len(self.nodes))
            for node_id,node in self.nodes.items():
                logger.debug("node_id:%s, available_resources:%s", node_id, node.available_resources)

    def _ray_node_index(self):
        try:
            return {node["NodeID"]: node for node in ray.nodes()}
        except Exception:
            return {}

    def _gpu_snapshot(self, node: Node):
        gpu_ids = sorted(
            set(node.total_resources.get("gpu_resource", {}).keys())
            | set(node.available_resources.get("gpu_resource", {}).keys())
        )
        devices = []
        total_count = 0
        available_count = 0

        for gpu_id in gpu_ids:
            total_gpu = node.total_resources.get("gpu_resource", {}).get(gpu_id, {})
            available_gpu = node.available_resources.get("gpu_resource", {}).get(gpu_id, {})
            total_num = total_gpu.get("gpu_num", 0)
            available_num = available_gpu.get("gpu_num", 0)
            total_count += total_num
            available_count += available_num
            devices.append({
                "gpu_id": gpu_id,
                "total_count": total_num,
                "available_count": available_num,
                "total_memory": total_gpu.get("gpu_mem", 0),
                "available_memory": available_gpu.get("gpu_mem", 0),
            })

        return {
            "total_count": total_count,
            "available_count": available_count,
            "devices": devices,
        }

    def get_cluster_resources(self):
        ray_nodes = self._ray_node_index()
        registered_nodes = []

        for node_id, node in self.nodes.items():
            ray_node = ray_nodes.get(node_id)
            registered_nodes.append({
                "node_id": node_id,
                "node_ip": node.node_ip,
                "role": "head" if node_id == self.head_node_id else "worker",
                "registered": True,
                "alive": bool(ray_node.get("Alive", False)) if ray_node else False,
                "resources": {
                    "cpu": {
                        "total": node.total_resources.get("cpu", 0),
                        "available": node.available_resources.get("cpu", 0),
                    },
                    "cpu_mem": {
                        "total": node.total_resources.get("cpu_mem", 0),
                        "available": node.available_resources.get("cpu_mem", 0),
                    },
                    "gpu": self._gpu_snapshot(node),
                },
                "ray_resources": ray_node.get("Resources", {}) if ray_node else {},
            })

        unregistered_ray_nodes = []
        for node_id, ray_node in ray_nodes.items():
            if node_id in self.nodes:
                continue
            if not ray_node.get("Alive", False):
                continue
            unregistered_ray_nodes.append({
                "node_id": node_id,
                "node_ip": ray_node.get("NodeManagerAddress"),
                "role": "worker",
                "registered": False,
                "alive": True,
                "ray_resources": ray_node.get("Resources", {}),
            })

        return {
            "head_node_id": self.head_node_id,
            "head_node_ip": self.head_node_ip,
            "nodes": sorted(registered_nodes, key=lambda item: (item["role"] != "head", item["node_ip"] or "")),
            "unregistered_ray_nodes": sorted(unregistered_ray_nodes, key=lambda item: item["node_ip"] or ""),
        }
            
            
    def stop_worker(self,node_id:str):
        '''
        Stop worker node
        '''
        del self.nodes[node_id]
        
    def select_node(self,task_need_resources:dict) -> SelectedNode | None:
        '''
        Select sufficient resources node
        '''
        cpu_need = task_need_resources["cpu"]
        cpu_mem_need = task_need_resources["cpu_mem"]
        gpu_need = task_need_resources["gpu"]
        gpu_mem_need = task_need_resources["gpu_mem"]
        assert(gpu_need <= 1)

        for node_id,node in self.nodes.items():
            if node.available_resources['cpu'] < cpu_need or node.available_resources['cpu_mem'] < cpu_mem_need:
                continue
            
            #gpu task
            if gpu_need > 0: 
                for gpu_id,gpu_resource in node.available_resources["gpu_resource"].items():
                    if gpu_resource['gpu_mem'] < gpu_mem_need or gpu_resource['gpu_num'] < gpu_need:
                        continue
                    
                    self.nodes[node_id].available_resources['cpu'] -= cpu_need
                    self.nodes[node_id].available_resources['cpu_mem'] -= cpu_mem_need
                    self.nodes[node_id].available_resources['gpu_resource'][gpu_id]['gpu_mem'] -= gpu_mem_need
                    self.nodes[node_id].available_resources['gpu_resource'][gpu_id]['gpu_num'] -= gpu_need

                    return SelectedNode(node_id=node_id,node_ip=node.node_ip,gpu_id=gpu_id)
        
            #cpu task
            else: 
                self.nodes[node_id].available_resources['cpu'] -= cpu_need
                self.nodes[node_id].available_resources['cpu_mem'] -= cpu_mem_need
                
                return SelectedNode(node_id=node_id,node_ip=node.node_ip)

            
        return None

    def release_task_resource(self,tasks:List[TaskRuntime]):
        '''
        Release resource according to task
        '''
        assert isinstance(tasks,list)
        for task in tasks:
            node_id = task.selected_node.node_id
            assert(node_id in self.nodes)
            self.nodes[node_id].release_resource(task.resources,task.selected_node.gpu_id)

    def release_instance_resource(self,resource_detail:dict):
        '''
        Release resource
        '''
        node_id = resource_detail["node_id"]
        gpu_id = resource_detail["gpu_id"]
        resources = resource_detail["resources"]
        self.nodes[node_id].release_resource(resources,gpu_id)
        

    def start_worker(self,node_id:str,node_ip:str,resources:dict,):
        '''
        Start worker node
        ''' 
        logger.info("New worker node join: node_id:%s,node_ip:%s", node_id,node_ip)
        gpu_resource = {int(k): v for k, v in resources['gpu_resource'].items()}
        resources["gpu_resource"] = gpu_resource
        self.nodes[node_id] = Node(node_id,node_ip,resources,resources)
