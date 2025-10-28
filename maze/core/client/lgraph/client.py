# maze/client.py
from requests.models import Response


import cloudpickle
import requests
import functools
from typing import Any, Dict, Callable

# 全局配置
MAZE_SERVER_ADDR = None

def init(addr: str):
    global MAZE_SERVER_ADDR
    if not addr.startswith("http"):
        addr = "http://" + addr
    MAZE_SERVER_ADDR = addr
    print(f"[maze] Connected to server at {MAZE_SERVER_ADDR}")

def task(resources: Dict[str, float]):
    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if MAZE_SERVER_ADDR is None:
                raise RuntimeError("maze.init() must be called before using @maze.task")

            # 序列化函数和参数
            payload = {
                "func": cloudpickle.dumps(func),
                "args": cloudpickle.dumps(args),
                "kwargs": cloudpickle.dumps(kwargs),
                "resources": resources,
            }

            # 发送到调度服务器
            try:
                response: Response = requests.post(f"{MAZE_SERVER_ADDR}/run_single_task", json={"task_data": cloudpickle.dumps(payload).hex()})
                
                if response.status_code == 200:
                    data = response.json()
                    print("====")
                    print(data)
                    return data["result"]
                else:
                    print(f"请求失败，状态码：{response.status_code}")
                    print("响应内容：", response.text)

            except Exception as e:
                raise RuntimeError(f"Failed to execute remote task: {str(e)}")

        # 标记这是一个 maze 任务，用于后续集成识别
        wrapper._is_maze_task = True
        wrapper._resources = resources
        return wrapper

    return decorator