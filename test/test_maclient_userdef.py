"""
测试用户上传自定义任务的工作流
演示如何使用用户自定义任务而不是内置任务
使用 pytest 运行: pytest test/test_user_upload_task.py -v
"""

import pytest
from datetime import datetime
from maze import MaClient, task


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


@pytest.fixture
def client():
    """创建并返回 MaClient 实例"""
    return MaClient()


@pytest.fixture
def workflow(client):
    """创建并返回工作流实例"""
    return client.create_workflow()


class TestUserUploadTask:
    """测试用户上传自定义任务的工作流"""
    
    def test_user_defined_task_workflow(self, workflow):
        """测试使用用户自定义任务的工作流执行"""
        # 添加用户自定义任务1（上传任务）
        task1 = workflow.add_task(
            user_task1,
            inputs={"task1_input": "这是task1的输入"}
        )
        
        # 添加用户自定义任务2（上传任务），引用task1的输出
        task2 = workflow.add_task(
            user_task2,
            inputs={"task2_input": task1.outputs["task1_output"]}
        )
        
        # 添加任务依赖关系
        workflow.add_edge(task1, task2)
        
        # 运行工作流并获取run_id
        run_id = workflow.run()
        print(f"Workflow started with run_id: {run_id}")
        
        # 获取并显示执行结果（格式化输出）
        results = workflow.show_results(run_id)
        task_results = results["task_results"]
        workflow_completed = results["workflow_completed"]
        
        # 断言：工作流应该完成
        assert workflow_completed, "工作流未完成"
        
        # 断言：应该有2个任务的结果
        assert len(task_results) == 2, f"期望2个任务结果，实际得到 {len(task_results)} 个"
        
        # 断言：每个任务都应该有输出
        for task_id, result in task_results.items():
            assert result is not None, f"任务 {task_id} 的结果为空"
            assert isinstance(result, dict), f"任务 {task_id} 的结果应该是字典类型"
        
        # 验证task1的输出格式
        task1_result = list(task_results.values())[0]
        assert "task1_output" in task1_result, "task1应该有task1_output字段"
        assert "这是task1的输入" in task1_result["task1_output"], "task1输出应包含输入内容"
        
        # 验证task2的输出格式
        task2_result = list(task_results.values())[1]
        assert "task2_output" in task2_result, "task2应该有task2_output字段"
        assert "====" in task2_result["task2_output"], "task2输出应包含后缀'===='"
        
        print(f"\n✓ 测试通过: 用户自定义任务工作流成功执行，完成 {len(task_results)} 个任务")
    
    def test_user_task_with_custom_input(self, workflow):
        """测试使用自定义输入的用户任务"""
        custom_input = "自定义测试输入-"
        
        # 添加任务
        task1 = workflow.add_task(
            user_task1,
            inputs={"task1_input": custom_input}
        )
        
        # 运行工作流并获取run_id
        run_id = workflow.run()
        print(f"Workflow started with run_id: {run_id}")
        
        # 获取并显示执行结果
        results = workflow.show_results(run_id)
        task_results = results["task_results"]
        
        # 获取第一个（也是唯一一个）任务的结果
        assert len(task_results) == 1, "应该有1个任务结果"
        result = list(task_results.values())[0]
        
        # 断言：应该有结果
        assert result is not None, "任务应该有结果"
        assert "task1_output" in result, "结果应该包含task1_output字段"
        
        # 验证输出包含自定义输入
        assert custom_input in result["task1_output"], f"输出应包含自定义输入 '{custom_input}'"
        
        print(f"✓ 测试通过: 自定义输入任务成功执行")
    
    def test_multiple_user_tasks(self, workflow):
        """测试多个用户任务的链式执行"""
        # 添加第一个任务
        task1 = workflow.add_task(
            user_task1,
            inputs={"task1_input": "任务1-"}
        )
        
        # 添加第二个任务
        task2 = workflow.add_task(
            user_task2,
            inputs={"task2_input": task1.outputs["task1_output"]}
        )
        
        # 添加边
        workflow.add_edge(task1, task2)
        
        # 运行工作流并获取run_id
        run_id = workflow.run()
        print(f"Workflow started with run_id: {run_id}")
        
        # 获取并显示执行结果
        results = workflow.show_results(run_id)
        task_results = results["task_results"]
        workflow_completed = results["workflow_completed"]
        
        # 断言：工作流应该完成
        assert workflow_completed, "工作流未完成"
        
        # 断言：应该有2个任务的结果
        assert len(task_results) == 2, f"期望2个任务结果，实际得到 {len(task_results)} 个"
        
        print(f"✓ 测试通过: 成功执行 {len(task_results)} 个链式用户任务")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])


