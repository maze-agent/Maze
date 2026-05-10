import pytest
from datetime import datetime
from maze import MaClient, task


# 定义用户自定义任务1
@task(resources={"cpu": 1})
def user_task1(task1_input: str):
    """
    用户自定义任务1：获取输入并添加当前时间戳
    
    输入:
        task1_input: 输入字符串
        
    输出:
        task1_output: 输入字符串 + 时间戳
    """
    task_input = task1_input
    
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task_input + time_str

    return {
        "task1_output": result
    }


# 定义用户自定义任务2
@task
def user_task2(task2_input: str):
    """
    用户自定义任务2：获取输入并添加当前时间戳和后缀
    
    输入:
        task2_input: 输入字符串
        
    输出:
        task2_output: 输入字符串 + 时间戳 + "===="
    """
    task_input = task2_input
    
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task_input + time_str + "===="

    return {
        "task2_output": result
    }
if __name__ == "__main__":
    client = MaClient()
    workflow = client.create_workflow()
    task1 = workflow.add_task(user_task1, inputs={"task1_input": "test"})
    task2 = workflow.add_task(user_task2, inputs={"task2_input": task1.outputs["task1_output"]})

    

    
    run_id = workflow.run()
    
    # 方式1: get_results() - 获取原始消息并自己打印
    print("\n第一次获取结果（从服务器）:")
    messages = workflow.get_results(run_id, verbose=False)  # verbose=False 避免重复打印
    for msg in messages:
        print(msg)
    
    # 演示缓存功能 - 第二次查询同一个 run_id 会从缓存返回
    print("\n第二次获取结果（从缓存，不连接服务器）:")
    messages = workflow.get_results(run_id, verbose=False)  # verbose=False 避免重复打印
    for msg in messages:
        print(msg)
    
    # 演示查询特定任务的结果
    print("\n查询特定任务的结果:")
    task1_result = workflow.get_task_result(run_id, task1.task_id)
    print(f"Task1 结果: {task1_result}")
    
    task2_result = workflow.get_task_result(run_id, task2.task_id)
    print(f"Task2 结果: {task2_result}")
    
