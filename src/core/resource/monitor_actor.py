import os
import ray
import math
import time
import json
import redis
import psutil
import GPUtil
import threading
import numpy as np
from typing import Optional, Dict
import subprocess 
@ray.remote
class ComputeNodeStatusMonitor:
    """
    Node status monitor that automatically fetches the Ray ID and IP of the node and collects
    the initial resource status. Starts a periodic resource reporting thread (pushes to the master node's Redis).
    """

    def __init__(self, master_ip: str, node_id: str, window_size: int= 3, ema_alpha: float= 0.3, 
                 net_recv_sent_wei: float= 1.0, disk_read_write_wei: float= 0.0, redis_ip:str="127.0.0.1",redis_port: int= 6379)-> None:
        """
        Initialize the node monitor and collect the initial resource status.

        :param master_ip: Master node IP address
        :param node_id: Node ID
        :param window_size: The size of the window for smoothing (default is 3)
        :param ema_alpha: Exponential moving average smoothing factor (default is 0.3)
        :param net_recv_sent_wei: Weight for network receive and send (default is 1.0)
        :param disk_read_write_wei: Weight for disk read and write (default is 0.0)
        :param redis_port: Redis server port (default is 6379)
        """
        from ray._private.services import get_node_ip_address
        self.hostname= os.uname()[1]
        all_nodes= ray.nodes()
        local_ip= get_node_ip_address()
        for node in all_nodes:
            if node["Alive"] and node["NodeManagerAddress"]== local_ip:
                self.node_id= node["NodeID"]
                self.node_ip= node["NodeManagerAddress"]
                break
        self.node_id= node_id
        self.redis_client= redis.Redis(host= redis_ip, port= redis_port)
        self.window_size= window_size
        self.ema_alpha= ema_alpha
        self.net_recv_sent_wei= net_recv_sent_wei/ 2.0
        self.disk_read_write_wei= disk_read_write_wei/ 2.0
        self.io_data= {"net_recv": [], "net_sent": [], "disk_read": [], "disk_write": []}
       
        self.initial_resource= self._collect_initial_resource()
        key= f"resource_status:{self.node_id}"
        data= {
            "node_id": self.node_id,
            "node_ip": self.node_ip,
            "hostname": self.hostname,
            "initial_resource": self.initial_resource
        }
        self.redis_client.set(key, json.dumps(data))
        
        #self._start_push_loop()

    # def _start_push_loop(self)-> None:
    #     """
    #     Start a loop that periodically reports resource usage to the master node via Redis.
    #     """
    #     def loop() -> None:
    #         while True:
    #             try:
    #                 data= {
    #                     "node_id": self.node_id,
    #                     "node_ip": self.node_ip,
    #                     "hostname": self.hostname,
    #                     "initial_resource": self.initial_resource,
    #                     "current_use_resource": self.get_current_use_resource()
    #                 }
    #                 key= f"resource_status:{self.node_id}"
    #                 self.redis_client.set(key, json.dumps(data))
                    
                    
    #             except Exception as e:
    #                 print(f"[RESOURCE REPORT ERROR] {e}")
    #             time.sleep(1)
    #     threading.Thread(target= loop, daemon= True).start()

    def _collect_initial_resource(self)-> Dict[str, float]:
        """
        Collect the initial resource usage of the node.

        :return: Dictionary containing the initial resource usage like CPU, memory, and GPU memory.
        """
        cpu_num= psutil.cpu_count(logical= True)
        mem_total_MB= psutil.virtual_memory().available/ 1024/ 1024
        gpus= self.get_gpu_info()
        
        gpu_info = []
        for gpu in gpus:
            gpu_info.append({
                "index":gpu['index'],
                "name":gpu['name'],
                "gpu_mem":gpu['memory_free']
            })
          
        return {
            "cpu_num": cpu_num,
            "mem": mem_total_MB*0.9,
            "gpu_info": gpu_info
        }

    # def _collect_current_io(self, interval: float= 1)-> Dict[str, float]:
    #     """
    #     Collect the current network and disk IO statistics.

    #     :param interval: Interval for IO measurement in seconds (default is 1)
    #     :return: Dictionary containing network and disk IO statistics.
    #     """
    #     io1= psutil.net_io_counters()
    #     disk1= psutil.disk_io_counters()
    #     time.sleep(interval)
    #     io2= psutil.net_io_counters()
    #     disk2= psutil.disk_io_counters()
    #     net_recv_MBps= (io2.bytes_recv- io1.bytes_recv)/ 1024/ 1024
    #     net_sent_MBps= (io2.bytes_sent- io1.bytes_sent)/ 1024/ 1024
    #     disk_read_MBps= (disk2.read_bytes- disk1.read_bytes)/ 1024/ 1024
    #     disk_write_MBps= (disk2.write_bytes- disk1.write_bytes)/ 1024/ 1024
    #     # Weighted average to calculate IO
    #     self.io_data["net_recv"].append(net_recv_MBps)
    #     self.io_data["net_sent"].append(net_sent_MBps)
    #     self.io_data["disk_read"].append(disk_read_MBps)
    #     self.io_data["disk_write"].append(disk_write_MBps)
    #     # Keep history data size within the window
    #     if len(self.io_data["net_recv"])> self.window_size:
    #         self.io_data["net_recv"].pop(0)
    #         self.io_data["net_sent"].pop(0)
    #         self.io_data["disk_read"].pop(0)
    #         self.io_data["disk_write"].pop(0)
    #     # Apply Exponential Moving Average (EMA)
    #     net_recv_rate= self._calculate_ema_np(self.io_data["net_recv"], self.ema_alpha)
    #     net_sent_rate= self._calculate_ema_np(self.io_data["net_sent"], self.ema_alpha)
    #     disk_read_rate= self._calculate_ema_np(self.io_data["disk_read"], self.ema_alpha)
    #     disk_write_rate= self._calculate_ema_np(self.io_data["disk_write"], self.ema_alpha)
    #     # Weighted calculation for IO usage index
    #     io_busy_idx= self.net_recv_sent_wei* (net_recv_rate+ net_sent_rate)+ self.disk_read_write_wei* (disk_read_rate+ disk_write_rate)
    #     return {"io_busy_idx": round(io_busy_idx, 3)}

    # def _calculate_ema_np(self, data: list, alpha: float) -> float:
    #     """
    #     Calculate the Exponential Moving Average (EMA) of a list of data.

    #     :param data: List of numerical data
    #     :param alpha: Smoothing factor (between 0 and 1)
    #     :return: The calculated EMA value
    #     """
    #     if not data:
    #         return 0.0
    #     arr= np.array(data, dtype= np.float32)
    #     weights= (1- alpha)** np.arange(len(arr))[::-1]
    #     weights*= alpha
    #     weights/= weights.sum()
    #     return np.dot(weights, arr)

    # def get_current_use_resource(self, interval: float= 1.0)-> Dict[str, float]:
    #     """
    #     Return the currently used resources, including real-time IO rates.

    #     :param interval: The interval for CPU and memory usage calculation (default is 1.0)
    #     :return: Dictionary containing current CPU, memory, and GPU usage as well as IO rates.
    #     """
    #     cpu_num= psutil.cpu_count(logical= True)
    #     cpu_used= psutil.cpu_percent(interval= 0.05)
    #     mem= psutil.virtual_memory()
    #     mem_used_MB= mem.used/ 1024/ 1024
    #     gpu_mem_used_MB= 0.0
    #     max_single_gpu_mem= 0.0
    #     try:
    #         gpus= GPUtil.getGPUs()
    #         gpu_mem_used_MB= sum(gpu.memoryUsed for gpu in gpus)
    #         max_single_gpu_mem= max([gpu.memoryTotal- gpu.memoryUsed for gpu in gpus])
    #     except Exception as e:
    #         print(f"Failed to get GPU info: {e}")
    #     io= self._collect_current_io(interval)
    #     used_resource= {
    #         "cpu_num": math.ceil(cpu_num* cpu_used/ 100.0),
    #         "mem": round(mem_used_MB, 3),
    #         "gpu_mem": round(gpu_mem_used_MB, 3),
    #         "max_single_gpu_mem": round(max_single_gpu_mem, 3),
    #         "update_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
    #     }
    #     used_resource.update(io)
    #     return used_resource

    def get_initial_status(self)-> Dict[str, float]:
        """
        Get the initial resource status of the node.

        :return: Dictionary containing the initial resource status.
        """
        return self.initial_resource

    
    def get_gpu_info(self):
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