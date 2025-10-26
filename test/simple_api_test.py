from maze.core.client import MaClient
from maze.core.client.task import simpleTask

client = MaClient()
workflow = client.create_workflow()
task1 = workflow.add_task(
    simpleTask.task1,
    inputs={"task1_input": "这是task1的输入"}
)
task2 = workflow.add_task(
    simpleTask.task2,
    inputs={"task2_input": task1.outputs["task1_output"]}  # 直接引用task1的输出
)
workflow.add_edge(task1, task2)
workflow.run()


for message in workflow.get_results():
    msg_type = message.get("type")
    
    if msg_type == "start_task":
        print(f"▶ 任务开始: {message.get('task_id')}")
        
    elif msg_type == "finish_task":
        print(f"✓ 任务完成: {message.get('task_id')}")
        print(f"  结果: {message.get('result')}\n")
        
    elif msg_type == "finish_workflow":
        print("=" * 60)
        print("🎉 工作流执行完成!")
        print("=" * 60)
        break
