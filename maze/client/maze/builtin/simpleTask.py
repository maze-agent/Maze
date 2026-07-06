"""
Built-in simple task examples

These tasks are defined using the @task decorator and include metadata for inputs, outputs, and resource requirements
"""

from datetime import datetime
from maze.client.maze.decorator import task





@task(task_kind="gpu", resources={"cpu_num": 1, "gpu_mem": 123, "io_num": 0})
def task1(task1_input: str):
    
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task1_input + time_str

    return {
        "task1_output": result
    }


@task(task_kind="gpu", resources={"cpu_num": 10, "gpu_mem": 324, "io_num": 0})
def task2(task2_input: str):
    """
    Task 2: Get input and add current timestamp and suffix
    
    Input:
        task2_input: Input string
        
    Output:
        task2_output: Input string + timestamp + "===="
    """
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task2_input + time_str + "===="

    return {
        "task2_output": result
    }
