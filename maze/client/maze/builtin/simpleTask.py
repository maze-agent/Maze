"""
Built-in simple task examples

These tasks are defined using the @task decorator and include metadata for inputs, outputs, and resource requirements
"""

from datetime import datetime
from maze.client.maze.decorator import task





@task(resources={"cpu": 1, "cpu_mem": 123, "gpu": 1, "gpu_mem": 123})
def task1(task1_input: str):
    
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task1_input + time_str

    return {
        "task1_output": result
    }


@task(resources={"cpu": 10, "cpu_mem": 123, "gpu": 0.8, "gpu_mem": 324})
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
