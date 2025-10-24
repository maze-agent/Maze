import ray
from typing import Any,List,Dict
from maze.core.scheduler.runtime import SelectedNode
from maze.core.scheduler.runtime import TaskRuntime
from maze.core.scheduler.runtime import SelectedNode
from maze.utils.utils import collect_gpu_info

class Node():
    def __init__(self,node_id:str,node_ip:str,available_resources:dict,total_resources:dict):
        self.node_id = node_id
        self.node_ip = node_ip
        self.available_resources = available_resources
        self.total_resources = total_resources
  
    def release_resource(self,resources:dict,gpu_id:int = None):
        cpu = resources["cpu"]
        cpu_mem = resources["cpu_mem"]
        gpu = resources["gpu"]
        gpu_mem = resources["gpu_mem"]

        self.available_resources['cpu'] += cpu
        self.available_resources['cpu_mem'] += cpu_mem
        
        if gpu_id:
            self.available_resources['gpu_resource'][gpu_id]['gpu_mem'] += gpu_mem
            self.available_resources['gpu_resource'][gpu_id]['gpu_num'] += gpu
            
class ResourceManager():
    def __init__(self):
        self.head_node_id = None
        self.head_node_ip = None
        self.nodes:Dict[str,Node] = {}
        #self.nodes_available_resources = {} #各个节点的剩余资源
        #self.nodeid_to_ip = None #{self.head_node_id:ray.util.get_node_ip_address()}
    
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

        self.nodes[self.head_node_id] = Node(self.head_node_id,self.head_node_ip,head_node_resource,head_node_resource)
    
    def show_all_node_resource(self):
        '''
        Show all node resource
        '''
        for node_id,node in self.nodes.items():
            print(node_id,node.available_resources)

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
        assert(gpu_need <= 1) #暂时只支持单GPU

        for node_id,node in self.nodes.items():
            if node.available_resources['cpu'] < cpu_need or node.available_resources['cpu_mem'] < cpu_mem_need:
                continue
            
            #gpu task
            if gpu_need > 0: 
                for gpu_id,gpu_resource in node.available_resources["gpu_resource"].items():
                    if gpu_resource['gpu_mem'] < gpu_mem_need or gpu_resource['gpu_num'] < gpu_need:
                        continue
                    
                    #更新资源
                    self.nodes[node_id].available_resources['cpu'] -= cpu_need
                    self.nodes[node_id].available_resources['cpu_mem'] -= cpu_mem_need
                    self.nodes[node_id].available_resources['gpu_resource'][gpu_id]['gpu_mem'] -= gpu_mem_need
                    self.nodes[node_id].available_resources['gpu_resource'][gpu_id]['gpu_num'] -= gpu_need

                    return SelectedNode(node_id=node_id,node_ip=node.node_ip,gpu_id=gpu_id)
        
            #cpu task
            else: 
                #更新资源
                self.nodes[node_id].available_resources['cpu'] -= cpu_need
                self.nodes[node_id].available_resources['cpu_mem'] -= cpu_mem_need
                
                return SelectedNode(node_id=node_id,node_ip=node.node_ip)

            
        return None

    def release_resource(self,tasks:List[TaskRuntime]):
        '''
        Release resource
        '''
        assert isinstance(tasks,list)
        for task in tasks:
            node_id = task.selected_node.node_id
            assert(node_id in self.nodes)
            self.nodes[node_id].release_resource(task.resources,task.selected_node.gpu_id)
        
    def start_worker(self,node_id:str,node_ip:str,resources:dict,):
        '''
        Start worker node
        ''' 
        self.nodes[node_id] = Node(node_id,node_ip,resources,resources)

 