from requests.models import Response
import cloudpickle
import requests
import functools
from typing import Any, Dict, Callable
import base64
 

class LanggraphClient():
    def __init__(self,addr:str) -> None:
        self.maze_server_addr = addr
        self.default_resources = {"cpu": 1, "gpu": 0, "cpu_mem": 0, "gpu_mem": 0}
        
        data = self._send_post_request(f"http://{self.maze_server_addr}/create_workflow")
        self.workflow_id = data["workflow_id"]

    def _send_post_request(self, url: str, data: Dict[str, Any]={}):
        response = requests.post(url, json=data)
        if response.status_code == 200:
            data = response.json()
            return data
        else:
            raise Exception(f"Failed to send request: {response.status_code}, {response.text}")

    def task(self, func_or_resources=None, *, resources=None):
        
        if callable(func_or_resources): 
            func = func_or_resources
            resources = self.default_resources
            return self._decorate(func, resources)
        else:
            if resources is None:
                resources = self.default_resources
            for k, v in resources.items():
                if k not in ["cpu", "gpu", "cpu_mem", "gpu_mem"]:
                    raise ValueError(f"Invalid resource type: {k}")
            for k in resources.keys():
                if not isinstance(resources[k], (int, float)):
                    raise ValueError(f"Resource values must be numbers, but got {type(resources[k])}")
            if "cpu" not in resources:
                resources["cpu"] = 1
            if "gpu" not in resources:
                resources["gpu"] = 0
            if "cpu_mem" not in resources:
                resources["cpu_mem"] = 0
            if "gpu_mem" not in resources:
                resources["gpu_mem"] = 0

            return lambda func: self._decorate(func, resources)
          
    def _decorate(self,func: Callable,resources:Dict):
        
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
               
            payload = {
                "workflow_id": self.workflow_id,
                "task_id": wrapper._task_id,
                "args": base64.b64encode(cloudpickle.dumps(args)).decode('utf-8'),
                "kwargs": base64.b64encode(cloudpickle.dumps(kwargs)).decode('utf-8'),
            }

            try:
                response: Response = requests.post(f"http://{self.maze_server_addr}/run_langgraph_task", json=payload)
                
                if response.status_code == 200:
                    data = response.json()
                    return data["result"]
                else:
                    print(f"请求失败，状态码：{response.status_code}")
                    print("响应内容：", response.text)

            except Exception as e:
                raise RuntimeError(f"Failed to execute remote task: {str(e)}")

       
        data = self._send_post_request(f"http://{self.maze_server_addr}/add_langgraph_task",data={
            "workflow_id": self.workflow_id,
            "task_type": "langgraph",
            "task_name": func.__name__,
            "code_ser": base64.b64encode(cloudpickle.dumps(func)).decode('utf-8'),
            "resources" : resources
        })
        
        wrapper._task_id = data["task_id"]
        wrapper._is_maze_task = True
        
        return wrapper

 