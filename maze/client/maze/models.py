"""
Data model classes for Maze client
"""

import requests
from typing import Dict, Any, Optional


class TaskOutput:
    """
    Task output reference for passing data between tasks
    
    Example:
        task1 = workflow.add_task(func1, inputs={"in": "value"})
        task2 = workflow.add_task(func2, inputs={"in": task1.outputs["out"]})
    """
    
    def __init__(self, task_id: str, output_key: str):
        self.task_id = task_id
        self.output_key = output_key
        
    def to_reference_string(self) -> str:
        """Convert to server-recognizable reference string format"""
        return f"{self.task_id}.output.{self.output_key}"
    
    def __repr__(self) -> str:
        return f"TaskOutput({self.task_id[:8]}...:{self.output_key})"


class TaskOutputs:
    """
    Task output collection with dictionary-style access
    
    Example:
        outputs = task.outputs
        output_ref = outputs["output_key"]
    """
    
    def __init__(self, task_id: str, output_keys: list):
        self.task_id = task_id
        self._outputs = {key: TaskOutput(task_id, key) for key in output_keys}
    
    def __getitem__(self, key: str) -> TaskOutput:
        if key not in self._outputs:
            raise KeyError(f"Task does not have output parameter named '{key}'")
        return self._outputs[key]
    
    def keys(self):
        return self._outputs.keys()
    
    def __repr__(self) -> str:
        return f"TaskOutputs({list(self._outputs.keys())})"


class MaTask:
    """
    Maze task object for configuring and managing individual tasks
    
    Example:
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
        Initialize task object
        
        Args:
            task_id: Task ID
            workflow_id: Workflow ID this task belongs to
            server_url: Server address
            task_name: Task name (optional)
            output_keys: List of output parameter names (optional)
        """
        self.task_id = task_id
        self.workflow_id = workflow_id
        self.server_url = server_url.rstrip('/')
        self.task_name = task_name
        
        # Create output reference object
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
        Save task configuration (inputs, outputs, code, resource requirements)
        
        Args:
            code_str: Task code string
            task_input: Task input parameter configuration
            task_output: Task output parameter configuration
            resources: Resource requirements configuration
            
        Raises:
            Exception: If save fails
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
                raise Exception(f"Failed to save task: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"Request failed, status code: {response.status_code}, response: {response.text}")
    
    def delete(self) -> None:
        """
        Delete task
        
        Raises:
            Exception: If deletion fails
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
                raise Exception(f"Failed to delete task: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"Request failed, status code: {response.status_code}, response: {response.text}")
    
    def __repr__(self) -> str:
        name = f", name='{self.task_name}'" if self.task_name else ""
        return f"MaTask(id='{self.task_id[:8]}...'{name})"

