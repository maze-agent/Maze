"""
任务装饰器，用于定义任务的元数据和配置
"""

import inspect
from typing import Dict, List, Any, Callable
from dataclasses import dataclass


@dataclass
class TaskMetadata:
    """任务元数据"""
    func: Callable
    func_name: str
    code_str: str
    inputs: List[str]
    outputs: List[str]
    resources: Dict[str, Any]
    data_types: Dict[str, str]  # 参数的数据类型


def task(inputs: List[str], 
         outputs: List[str],
         resources: Dict[str, Any] = None,
         data_types: Dict[str, str] = None):
    """
    任务装饰器，用于标记和配置任务函数
    
    Args:
        inputs: 输入参数名列表
        outputs: 输出参数名列表
        resources: 资源需求配置，默认为 {"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0}
        data_types: 参数数据类型映射，默认全部为 "str"
        
    示例:
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
        # 获取函数源代码（不包含装饰器）
        source_lines = inspect.getsourcelines(func)[0]
        
        # 找到函数定义的开始（跳过装饰器行）
        func_start_idx = 0
        for idx, line in enumerate(source_lines):
            if line.strip().startswith('def '):
                func_start_idx = idx
                break
        
        # 提取从函数定义开始的代码
        func_lines = source_lines[func_start_idx:]
        code_str = ''.join(func_lines)
        
        # 默认资源配置
        if resources is None:
            resources_config = {"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0}
        else:
            resources_config = resources
        
        # 默认数据类型都是str
        if data_types is None:
            types_config = {param: "str" for param in inputs + outputs}
        else:
            types_config = {param: "str" for param in inputs + outputs}
            types_config.update(data_types)
        
        # 创建元数据
        metadata = TaskMetadata(
            func=func,
            func_name=func.__name__,
            code_str=code_str,
            inputs=inputs,
            outputs=outputs,
            resources=resources_config,
            data_types=types_config
        )
        
        # 将元数据附加到函数上
        func._maze_task_metadata = metadata
        
        return func
    
    return decorator


def get_task_metadata(func: Callable) -> TaskMetadata:
    """
    获取函数的任务元数据
    
    Args:
        func: 被@task装饰的函数
        
    Returns:
        TaskMetadata: 任务元数据
        
    Raises:
        ValueError: 如果函数没有被@task装饰
    """
    if not hasattr(func, '_maze_task_metadata'):
        raise ValueError(f"函数 {func.__name__} 没有使用 @task 装饰器")
    
    return func._maze_task_metadata

