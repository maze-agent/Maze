from platform import node
from typing import Any,List


from maze.core.scheduler.runtime import TaskRuntime
import ray
import subprocess
import threading


class ResourceManager():
    def __init__(self):
        self.lock = threading.Lock() #ResourceManager对象由submit线程、monitor线程、receive线程共同操作，需要线程安全
        
        ray.init(address='auto') 
        self.head_node_id = ray.get_runtime_context().get_node_id()
        self.nodes_detail_by_ray = {} #ray.nodes接口获取的节点信息
        self.nodes_available_resources = {} #各个节点的剩余资源
        self.nodeid_to_ip = {self.head_node_id:ray.util.get_node_ip_address()}
        
        self._get_nodes_detail_by_ray()
        self._init_head_node_resource()

    def choose_node(self,task_need_resources:dict):
        #{'cpu': 1, 'cpu_mem': 123, 'gpu': 1, 'gpu_mem': 1432}
        with self.lock:
            cpu_need = task_need_resources["cpu"]
            cpu_mem_need = task_need_resources["cpu_mem"]
            gpu_need = task_need_resources["gpu"]
            gpu_mem_need = task_need_resources["gpu_mem"]
            assert(gpu_need <= 1) #暂时只支持单GPU

            choosed_machine = None
            for node_id,node_resource in self.nodes_available_resources.items():
                if node_resource['cpu'] < cpu_need or node_resource['cpu_mem'] < cpu_mem_need:
                    continue
                
                if gpu_need > 0:
                    for gpu_id,gpu_resource in node_resource["gpu_resource"].items():
                        if gpu_resource['gpu_mem'] < gpu_mem_need or gpu_resource['gpu_num'] < gpu_need:
                            continue
                        
                        #更新资源
                        self.nodes_available_resources[node_id]['cpu'] -= cpu_need
                        self.nodes_available_resources[node_id]['cpu_mem'] -= cpu_mem_need
                        self.nodes_available_resources[node_id]['gpu_resource'][gpu_id]['gpu_mem'] -= gpu_mem_need
                        self.nodes_available_resources[node_id]['gpu_resource'][gpu_id]['gpu_num'] -= gpu_need

                        choosed_machine = {"node_id":node_id, "gpu_id":gpu_id}
                else:
                    #更新资源
                    self.nodes_available_resources[node_id]['cpu'] -= cpu_need
                    self.nodes_available_resources[node_id]['cpu_mem'] -= cpu_mem_need
        
                    choosed_machine = {"node_id":node_id}
            
            if choosed_machine is not None:
                choosed_machine["node_ip"] = self.nodeid_to_ip[choosed_machine["node_id"]]
               
            return choosed_machine

    def release_resource(self,tasks:List[TaskRuntime]):
        with self.lock:
            for task in tasks:
                node_id = task.node_id
                resources = task.resources

                cpu = resources["cpu"]
                cpu_mem = resources["cpu_mem"]
                gpu = resources["gpu"]
                gpu_mem = resources["gpu_mem"]

                self.nodes_available_resources[node_id]['cpu'] += cpu
                self.nodes_available_resources[node_id]['cpu_mem'] += cpu_mem
                
                if gpu > 0:
                    gpu_id = task.gpu_id

                    self.nodes_available_resources[node_id]['gpu_resource'][gpu_id]['gpu_mem'] += gpu_mem
                    self.nodes_available_resources[node_id]['gpu_resource'][gpu_id]['gpu_num'] += gpu

    def _init_head_node_resource(self):
        self.nodes_available_resources[self.head_node_id] = {
            "cpu":self.nodes_detail_by_ray[self.head_node_id]["Resources"]["CPU"],
            "cpu_mem":self.nodes_detail_by_ray[self.head_node_id]["Resources"]["memory"],   
            "gpu_resource":{}
        }

        gpu_info = self._collect_gpus_info()
        if len(gpu_info) > 0:
            for gpu in gpu_info:
                gpu_id = gpu["index"]
                gpu_mem = gpu["memory_free"]
                self.nodes_available_resources[self.head_node_id]["gpu_resource"][gpu_id] = {
                    "gpu_id" : gpu_id,
                    "gpu_mem":gpu_mem,
                    "gpu_num":1
                }
            
    def _get_nodes_detail_by_ray(self):
        nodes: Any = ray.nodes()
        for node in nodes:
            if node['Alive']:
                self.nodes_detail_by_ray[node['NodeID']] = node
    
    def _collect_gpus_info(self):
        try:
            # 执行 nvidia-smi 命令并捕获输出
            result = subprocess.run(['nvidia-smi', '--query-gpu=index,name,utilization.gpu,memory.total,memory.used,memory.free', '--format=csv,noheader,nounits'], stdout=subprocess.PIPE)
            
            # 解码输出为字符串
            output = result.stdout.decode('utf-8')
            
            # 按行分割输出
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

        except Exception as e:
            return []

