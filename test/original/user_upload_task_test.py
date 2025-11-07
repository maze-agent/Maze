"""
用户上传任务测试
演示如何使用用户自定义任务而不是内置任务
"""

from datetime import datetime
from maze.client.maze.client import MaClient
from maze.client.maze.decorator import task

# 定义用户自定义任务1
@task(
    inputs=["task1_input"],
    outputs=["task1_output"],
    resources={"cpu": 1, "cpu_mem": 123, "gpu": 1, "gpu_mem": 123}
)
def user_task1(params):
    """
    用户自定义任务1：获取输入并添加当前时间戳
    
    输入:
        task1_input: 输入字符串
        
    输出:
        task1_output: 输入字符串 + 时间戳
    """

    task_input = params.get("task1_input")
    
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task_input + time_str

    return {
        "task1_output": result
    }


# 定义用户自定义任务2
@task(
    inputs=["task2_input"],
    outputs=["task2_output"],
    resources={"cpu": 10, "cpu_mem": 123, "gpu": 0.8, "gpu_mem": 324}
)
def user_task2(params):
    """
    用户自定义任务2：获取输入并添加当前时间戳和后缀
    
    输入:
        task2_input: 输入字符串
        
    输出:
        task2_output: 输入字符串 + 时间戳 + "===="
    """
    task_input = params.get("task2_input")
    
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task_input + time_str + "===="

    return {
        "task2_output": result
    }


# 创建客户端和工作流
client = MaClient()
workflow = client.create_workflow()

# 添加用户自定义任务1（上传任务）
task1 = workflow.add_task(
    user_task1,
    inputs={"task1_input": "这是task1的输入"}
)

# 添加用户自定义任务2（上传任务），引用task1的输出
task2 = workflow.add_task(
    user_task2,
    inputs={"task2_input": task1.outputs["task1_output"]}  # 直接引用task1的输出，会自动添加边
)

# 不需要手动 add_edge，引用输出时会自动建立依赖关系

# 运行工作流
workflow.run()

# 获取并打印执行结果
for message in workflow.get_results():
    msg_type = message.get("type")
    msg_data = message.get("data", {})
    
    if msg_type == "start_task":
        print(f"▶ 任务开始: {msg_data.get('task_id')}")
        
    elif msg_type == "finish_task":
        print(f"✓ 任务完成: {msg_data.get('task_id')}")
        print(f"  结果: {msg_data.get('result')}\n")
        
    elif msg_type == "finish_workflow":
        print("=" * 60)
        print("🎉 工作流执行完成!")
        print("=" * 60)
        break

