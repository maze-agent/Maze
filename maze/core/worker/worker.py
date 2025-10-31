from typing import Any,Dict
import re
import resource
import subprocess
import ray
import requests
from maze.utils.utils import collect_gpu_info

class Worker():
    @staticmethod
    def _send_post_request(url: str, data: Dict[str, Any]={}):
        response = requests.post(url, json=data)
        if response.status_code == 200:
            data = response.json()
            return data
        else:
            raise Exception(f"Failed to send request: {response.status_code}, {response.text}")
 
    @staticmethod
    def start_worker(addr: str):
        try:
            data = Worker._send_post_request(f"http://{addr}/get_head_ray_port")
            head_ray_port = data["port"]
            command = [
                "ray", "start", "--address", addr.split(":")[0] + ":" + str(head_ray_port),  
            ]
            result = subprocess.run(
                command,
                check=True,                   
                text=True,                  
                capture_output=True,        
            )
        
            current_node_id = ray.get_runtime_context().get_node_id()
            current_node_ip = None
            cur_node = None
            nodes = ray.nodes()
            for node in nodes:
                if node['NodeID'] == current_node_id:
                    cur_node = node
                    current_node_ip = node['NodeManagerAddress']
                    break
            assert(cur_node is not None)

            resources = {
                "cpu":cur_node["Resources"]["CPU"],
                "cpu_mem":cur_node["Resources"]["memory"],   
                "gpu_resource":{}
            }
            gpu_info = collect_gpu_info()
            if len(gpu_info) > 0:
                for gpu in gpu_info:
                    gpu_id = gpu["index"]
                    gpu_mem = gpu["memory_free"]
                    resources["gpu_resource"][gpu_id] = {
                        "gpu_id" : gpu_id,
                        "gpu_mem":gpu_mem,
                        "gpu_num":1
                    }
            Worker._send_post_request(url=f"http://{addr}/start_worker",data={"node_ip":current_node_ip,"node_id":current_node_id,"resources":resources})
        except Exception as e:
            print(f"Failed to start worker: {e}")
        