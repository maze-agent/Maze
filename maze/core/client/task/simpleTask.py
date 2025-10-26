"""
内置的简单任务示例

这些任务使用 @task 装饰器定义，包含了输入输出和资源需求的元数据
"""

from maze.core.client.decorator import task


@task(
    inputs=["task1_input"],
    outputs=["task1_output"],
    resources={"cpu": 1, "cpu_mem": 123, "gpu": 1, "gpu_mem": 123}
)
def task1(params):
    from datetime import datetime
    import time
    
    task_input = params.get("task1_input")
    
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task_input + time_str

    return {
        "task1_output": result
    }


@task(
    inputs=["task2_input"],
    outputs=["task2_output"],
    resources={"cpu": 10, "cpu_mem": 123, "gpu": 0.8, "gpu_mem": 324}
)
def task2(params):
    """
    任务2：获取输入并添加当前时间戳和后缀
    
    输入:
        task2_input: 输入字符串
        
    输出:
        task2_output: 输入字符串 + 时间戳 + "===="
    """
    from datetime import datetime
    import time 
    
    task_input = params.get("task2_input")
    
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task_input + time_str + "===="

    return {
        "task2_output": result
    }

