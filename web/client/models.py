"""
Maze客户端的数据模型类
"""

import requests
from typing import Dict, Any, Optional


class TaskOutput:
    """
    任务输出引用，用于在任务间传递数据
    
    示例:
        task1 = workflow.add_task(func1, inputs={"in": "value"})
        task2 = workflow.add_task(func2, inputs={"in": task1.outputs["out"]})
    """
    
    def __init__(self, task_id: str, output_key: str):
        self.task_id = task_id
        self.output_key = output_key
        
    def to_reference_string(self) -> str:
        """转换为服务器能识别的引用字符串格式"""
        return f"{self.task_id}.output.{self.output_key}"
    
    def __repr__(self) -> str:
        return f"TaskOutput({self.task_id[:8]}...:{self.output_key})"


class TaskOutputs:
    """
    任务输出集合，支持字典式访问
    
    示例:
        outputs = task.outputs
        output_ref = outputs["output_key"]
    """
    
    def __init__(self, task_id: str, output_keys: list):
        self.task_id = task_id
        self._outputs = {key: TaskOutput(task_id, key) for key in output_keys}
    
    def __getitem__(self, key: str) -> TaskOutput:
        if key not in self._outputs:
            raise KeyError(f"任务没有名为 '{key}' 的输出参数")
        return self._outputs[key]
    
    def keys(self):
        return self._outputs.keys()
    
    def __repr__(self) -> str:
        return f"TaskOutputs({list(self._outputs.keys())})"


class MaTask:
    """
    Maze任务对象，用于配置和管理单个任务
    
    示例:
        task = workflow.add_task(task_func, inputs={"input_key": "value"})
        next_task = workflow.add_task(next_func, inputs={"in": task.outputs["out"]})
    """
    
    def __init__(self, 
                 task_id: str, 
                 workflow_id: str, 
                 server_url: str, 
                 task_name: Optional[str] = None,
                 output_keys: Optional[list] = None):
        """
        初始化任务对象
        
        Args:
            task_id: 任务ID
            workflow_id: 所属工作流ID
            server_url: 服务器地址
            task_name: 任务名称（可选）
            output_keys: 输出参数名列表（可选）
        """
        self.task_id = task_id
        self.workflow_id = workflow_id
        self.server_url = server_url.rstrip('/')
        self.task_name = task_name
        
        # 创建输出引用对象
        if output_keys:
            self.outputs = TaskOutputs(task_id, output_keys)
        else:
            self.outputs = None
        
    def save(self, 
             code_str: str,
             task_input: Dict[str, Any],
             task_output: Dict[str, Any],
             resources: Dict[str, Any]) -> None:
        """
        保存任务配置（输入、输出、代码、资源需求）
        
        Args:
            code_str: 任务代码字符串
            task_input: 任务输入参数配置
            task_output: 任务输出参数配置
            resources: 资源需求配置
            
        Raises:
            Exception: 如果保存失败
        """
        url = f"{self.server_url}/save_task"
        data = {
            'workflow_id': self.workflow_id,
            'task_id': self.task_id,
            'code_str': code_str,
            'task_input': task_input,
            'task_output': task_output,
            'resources': resources,
        }
        
        response = requests.post(url, json=data)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") != "success":
                raise Exception(f"保存任务失败: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"请求失败，状态码：{response.status_code}, 响应：{response.text}")
    
    def delete(self) -> None:
        """
        删除任务
        
        Raises:
            Exception: 如果删除失败
        """
        url = f"{self.server_url}/del_task"
        data = {
            'workflow_id': self.workflow_id,
            'task_id': self.task_id,
        }
        
        response = requests.post(url, json=data)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") != "success":
                raise Exception(f"删除任务失败: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"请求失败，状态码：{response.status_code}, 响应：{response.text}")
    
    def __repr__(self) -> str:
        name = f", name='{self.task_name}'" if self.task_name else ""
        return f"MaTask(id='{self.task_id[:8]}...'{name})"

