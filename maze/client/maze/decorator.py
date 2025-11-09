"""
Task decorators for defining task metadata and configuration
"""

import inspect
import cloudpickle
import base64
from typing import Dict, List, Any, Callable
from dataclasses import dataclass


@dataclass
class TaskMetadata:
    """Task metadata"""
    func: Callable
    func_name: str
    code_str: str
    code_ser: str  # Serialized function (using cloudpickle)
    inputs: List[str]
    outputs: List[str]
    resources: Dict[str, Any]
    data_types: Dict[str, str]  # Parameter data types


def task(inputs: List[str], 
         outputs: List[str],
         resources: Dict[str, Any] = None,
         data_types: Dict[str, str] = None):
    """
    Task decorator for marking and configuring task functions
    
    Args:
        inputs: List of input parameter names
        outputs: List of output parameter names
        resources: Resource requirements configuration, defaults to {"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}
        data_types: Parameter data type mapping, defaults to "str" for all
        
    Example:
        @task(
            inputs=["input_value"],
            outputs=["output_value"],
            resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0}
        )
        def my_task(params):
            value = params.get("input_value")
            return {"output_value": value + " processed"}
    """
    def decorator(func: Callable) -> Callable:
        # Get function source code (excluding decorators)
        source_lines = inspect.getsourcelines(func)[0]
        
        # Find the start of function definition (skip decorator lines)
        func_start_idx = 0
        for idx, line in enumerate(source_lines):
            if line.strip().startswith('def '):
                func_start_idx = idx
                break
        
        # Extract code starting from function definition
        func_lines = source_lines[func_start_idx:]
        code_str = ''.join(func_lines)
        
        # Serialize entire function using cloudpickle (including external imports and dependencies)
        code_ser = base64.b64encode(cloudpickle.dumps(func)).decode('utf-8')
        
        # Default resource configuration
        if resources is None:
            resources_config = {"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}
        else:
            resources_config = resources
        
        # Default data types are all str
        if data_types is None:
            types_config = {param: "str" for param in inputs + outputs}
        else:
            types_config = {param: "str" for param in inputs + outputs}
            types_config.update(data_types)
        
        # Create metadata
        metadata = TaskMetadata(
            func=func,
            func_name=func.__name__,
            code_str=code_str,
            code_ser=code_ser,
            inputs=inputs,
            outputs=outputs,
            resources=resources_config,
            data_types=types_config
        )
        
        # Attach metadata to function
        func._maze_task_metadata = metadata
        
        return func
    
    return decorator


def get_task_metadata(func: Callable) -> TaskMetadata:
    """
    Get task metadata from function
    
    Args:
        func: Function decorated with @task
        
    Returns:
        TaskMetadata: Task metadata
        
    Raises:
        ValueError: If function is not decorated with @task
    """
    if not hasattr(func, '_maze_task_metadata'):
        raise ValueError(f"Function {func.__name__} is not decorated with @task")
    
    return func._maze_task_metadata

