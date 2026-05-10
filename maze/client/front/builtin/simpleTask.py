"""
Built-in simple task examples

These tasks are defined using the @task decorator and include metadata for inputs, outputs, and resource requirements
"""

from maze.client.front.decorator import task


@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def task1(task1_input: str):
    """
    Add the current timestamp to the input text.

    Input:
        task1_input: Text provided by the user.

    Output:
        task1_output: Input text followed by the current timestamp.
    """
    from datetime import datetime
    
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task1_input + time_str

    return {
        "task1_output": result
    }


@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def task2(task2_input: str):
    """
    Add the current timestamp and a suffix to the input text.
    
    Input:
        task2_input: Text from the user or an upstream task.
        
    Output:
        task2_output: Input text followed by the current timestamp and "====".
    """
    from datetime import datetime
    
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task2_input + time_str + "===="

    return {
        "task2_output": result
    }
