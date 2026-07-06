from requests.models import Response
import cloudpickle
import requests
import functools
from typing import Any, Dict, Callable
import base64

class LanggraphClient():
    def __init__(self,addr:str="localhost:8000") -> None:
        self.maze_server_addr = addr
        self.default_resources = {"cpu_num": 1, "gpu_mem": 0, "io_num": 0}
        
        data = self._send_post_request(f"http://{self.maze_server_addr}/create_workflow")
        self.workflow_id = data["workflow_id"]

    def _send_post_request(self, url: str, data: Dict[str, Any]={}):
        response = requests.post(url, json=data)
        if response.status_code == 200:
            data = response.json()
            return data
        else:
            raise Exception(f"Failed to send request: {response.status_code}, {response.text}")

    def _normalize_resources(self, resources: Dict[str, Any] | None) -> Dict[str, int]:
        raw = dict(resources or {})
        normalized = {
            "cpu_num": int(raw.get("cpu_num", raw.get("cpu", 1)) or 1),
            "gpu_mem": int(raw.get("gpu_mem", 0) or 0),
            "io_num": int(raw.get("io_num", 0) or 0),
        }
        normalized["cpu_num"] = max(1, normalized["cpu_num"])
        normalized["gpu_mem"] = max(0, normalized["gpu_mem"])
        normalized["io_num"] = max(0, normalized["io_num"])
        return normalized

    def _normalize_task_kind(self, task_kind: str | None, resources: Dict[str, int]) -> str:
        normalized = (task_kind or ("gpu" if resources.get("gpu_mem", 0) > 0 else "cpu")).strip().lower()
        if normalized not in {"cpu", "gpu", "io"}:
            raise ValueError("task_kind must be one of: cpu, gpu, io")
        if normalized == "gpu" and resources.get("gpu_mem", 0) <= 0:
            raise ValueError("gpu LangGraph tasks must declare resources.gpu_mem")
        return normalized

    def task(self, func_or_resources=None, *, resources=None, task_kind: str | None = None):
        
        if callable(func_or_resources): 
            func = func_or_resources
            normalized_resources = self.default_resources.copy()
            normalized_task_kind = self._normalize_task_kind(task_kind, normalized_resources)
            return self._decorate(func, normalized_resources, normalized_task_kind)
        else:
            if resources is None:
                resources = self.default_resources
            allowed = {"cpu_num", "gpu_mem", "io_num", "cpu"}
            for k, v in resources.items():
                if k not in allowed:
                    raise ValueError(f"Invalid resource type: {k}")
                if not isinstance(v, (int, float)):
                    raise ValueError(f"Resource values must be numbers, but got {type(v)}")
            normalized_resources = self._normalize_resources(resources)
            normalized_task_kind = self._normalize_task_kind(task_kind, normalized_resources)

            return lambda func: self._decorate(func, normalized_resources, normalized_task_kind)
          
    def _decorate(self,func: Callable,resources:Dict,task_kind:str):
        
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
                    print(f"Request failed, status code: {response.status_code}")
                    print("Response content:", response.text)

            except Exception as e:
                raise RuntimeError(f"Failed to execute remote task: {str(e)}")

       
        data = self._send_post_request(f"http://{self.maze_server_addr}/add_langgraph_task",data={
            "workflow_id": self.workflow_id,
            "task_type": "langgraph",
            "task_name": func.__name__,
            "code_ser": base64.b64encode(cloudpickle.dumps(func)).decode('utf-8'),
            "resources" : resources,
            "task_kind": task_kind,
        })
        
        wrapper._task_id = data["task_id"]
        wrapper._is_maze_task = True
        
        return wrapper
