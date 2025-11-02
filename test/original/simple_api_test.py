from maze.client.maze.client import MaClient
from maze.client.maze.builtin import simpleTask
client = MaClient()
workflow = client.create_workflow()
task1 = workflow.add_task(
    simpleTask.task1,
    inputs={"task1_input": "这是task1的输入"}
)
task2 = workflow.add_task(
    simpleTask.task2,
    inputs={"task2_input": task1.outputs["task1_output"]}  # 直接引用task1的输出，会自动添加边
)
# 不需要手动 add_edge，引用输出时会自动建立依赖关系
workflow.run()


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
