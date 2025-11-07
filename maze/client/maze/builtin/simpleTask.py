"""
Built-in simple task examples

These tasks are defined using the @task decorator and include metadata for inputs, outputs, and resource requirements
"""

from datetime import datetime
from maze.client.maze.decorator import task





@task(
    inputs=["task1_input"],
    outputs=["task1_output"],
    resources={"cpu": 1, "cpu_mem": 123, "gpu": 1, "gpu_mem": 123}
)
def task1(params):
    
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
    Task 2: Get input and add current timestamp and suffix
    
    Input:
        task2_input: Input string
        
    Output:
        task2_output: Input string + timestamp + "===="
    """

    
    task_input = params.get("task2_input")
    
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task_input + time_str + "===="

    return {
        "task2_output": result
    }

