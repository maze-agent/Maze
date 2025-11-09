"""
测试直接使用HTTP API和WebSocket的工作流
使用 pytest 运行: pytest test/test_http_api.py -v
"""

import pytest
import requests
import websocket
import json
import threading
from typing import List, Dict, Any


# 基础URL配置
BASE_URL = "http://localhost:8000"
WS_BASE_URL = "ws://localhost:8000"


@pytest.fixture
def api_client():
    """提供API客户端辅助函数"""
    class APIClient:
        @staticmethod
        def create_workflow():
            """创建工作流"""
            response = requests.post(f"{BASE_URL}/create_workflow")
            assert response.status_code == 200, f"创建工作流失败: {response.text}"
            data = response.json()
            assert data["status"] == "success"
            return data["workflow_id"]
        
        @staticmethod
        def add_task(workflow_id: str, task_name: str):
            """添加任务"""
            data = {
                'workflow_id': workflow_id,
                'task_type': 'code',
                'task_name': task_name,
            }
            response = requests.post(f"{BASE_URL}/add_task", json=data)
            assert response.status_code == 200, f"添加任务失败: {response.text}"
            result = response.json()
            assert result["status"] == "success"
            return result["task_id"]
        
        @staticmethod
        def save_task(workflow_id: str, task_id: str, code_str: str, 
                      task_input: Dict, task_output: Dict, resources: Dict):
            """保存任务"""
            data = {
                'workflow_id': workflow_id,
                'task_id': task_id,
                'code_str': code_str,
                'task_input': task_input,
                'task_output': task_output,
                'resources': resources,
            }
            response = requests.post(f"{BASE_URL}/save_task", json=data)
            assert response.status_code == 200, f"保存任务失败: {response.text}"
            result = response.json()
            assert result["status"] == "success"
        
        @staticmethod
        def add_edge(workflow_id: str, source_task_id: str, target_task_id: str):
            """添加边"""
            data = {
                'workflow_id': workflow_id,
                'source_task_id': source_task_id,
                'target_task_id': target_task_id,
            }
            response = requests.post(f"{BASE_URL}/add_edge", json=data)
            assert response.status_code == 200, f"添加边失败: {response.text}"
            result = response.json()
            assert result["status"] == "success"
        
        @staticmethod
        def run_workflow(workflow_id: str):
            """运行工作流"""
            data = {'workflow_id': workflow_id}
            response = requests.post(f"{BASE_URL}/run_workflow", json=data)
            assert response.status_code == 200, f"运行工作流失败: {response.text}"
            result = response.json()
            assert result["status"] == "success"
            assert result["run_id"] is not None
            return result['run_id']
        
        @staticmethod
        def get_workflow_results(workflow_id: str,run_id:str) -> List[Dict[str, Any]]:
            """通过WebSocket获取工作流结果"""
            url = f"{WS_BASE_URL}/get_workflow_res/{workflow_id}/{run_id}"
            messages = []
            error = None
            
            def on_message(ws, message):
                try:
                    msg_data = json.loads(message)
                    messages.append(msg_data)
                    print(f"收到消息: {msg_data}")
                except json.JSONDecodeError:
                    messages.append({"raw": message})
            
            def on_error(ws, err):
                nonlocal error
                # 检查是否是正常关闭 (1000)
                if hasattr(err, 'data'):
                    error_code = int.from_bytes(err.data, 'big')
                    if error_code != 1000:
                        error = err
                        print(f"WebSocket错误: {err}")
            
            def on_close(ws, close_status_code, close_msg):
                print(f"WebSocket连接关闭: {close_status_code}")
            
            def on_open(ws):
                print(f"已连接到 {url}")
            
            # 创建并运行WebSocket连接
            ws = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            
            # 在新线程中运行WebSocket
            ws_thread = threading.Thread(target=ws.run_forever)
            ws_thread.daemon = True
            ws_thread.start()
            ws_thread.join(timeout=30)  # 最多等待30秒
            
            if error and not (hasattr(error, 'data') and int.from_bytes(error.data, 'big') == 1000):
                raise Exception(f"WebSocket错误: {error}")
            
            return messages
    
    return APIClient()


class TestHTTPAPI:
    """测试HTTP API和WebSocket功能"""
    
    def test_create_workflow(self, api_client):
        """测试创建工作流"""
        workflow_id = api_client.create_workflow()
        assert workflow_id is not None
        assert len(workflow_id) > 0
        print(f"✓ 成功创建工作流: {workflow_id}")
    
    def test_add_and_save_task(self, api_client):
        """测试添加和保存任务"""
        # 创建工作流
        workflow_id = api_client.create_workflow()
        
        # 添加任务
        task_id = api_client.add_task(workflow_id, "test_task")
        assert task_id is not None
        assert len(task_id) > 0
        
        # 保存任务
        code_str = """
from datetime import datetime

def test_task(params):
    task_input = params.get("test_input")
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task_input + time_str
    return {"test_output": result}
"""
        task_input = {
            "input_params": {
                "1": {
                    "key": "test_input",
                    "input_schema": "from_user",
                    "data_type": "str",
                    "value": "测试输入",
                }
            }
        }
        task_output = {
            "output_params": {
                "1": {
                    "key": "test_output",
                    "data_type": "str",
                }
            }
        }
        resources = {
            "cpu": 1,
            "cpu_mem": 123,
            "gpu": 0,
            "gpu_mem": 0
        }
        
        api_client.save_task(workflow_id, task_id, code_str, task_input, task_output, resources)
        print(f"✓ 成功添加并保存任务: {task_id}")
    
    def test_complete_workflow_execution(self, api_client):
        """测试完整的工作流执行流程"""
        # 1. 创建工作流
        workflow_id = api_client.create_workflow()
        print(f"✓ 创建工作流: {workflow_id}")
        
        # 2. 添加任务1
        task_id1 = api_client.add_task(workflow_id, "task1")
        code_str1 = """
from datetime import datetime

def task1(params):
    task_input = params.get("task1_input")
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task_input + time_str
    return {"task1_output": result}
"""
        task_input1 = {
            "input_params": {
                "1": {
                    "key": "task1_input",
                    "input_schema": "from_user",
                    "data_type": "str",
                    "value": "这是task1的输入",
                }
            }
        }
        task_output1 = {
            "output_params": {
                "1": {
                    "key": "task1_output",
                    "data_type": "str",
                }
            }
        }
        resources1 = {
            "cpu": 1,
            "cpu_mem": 123,
            "gpu": 1,
            "gpu_mem": 123
        }
        api_client.save_task(workflow_id, task_id1, code_str1, task_input1, task_output1, resources1)
        print(f"✓ 添加任务1: {task_id1}")
        
        # 3. 添加任务2
        task_id2 = api_client.add_task(workflow_id, "task2")
        code_str2 = """
from datetime import datetime

def task2(params):
    task_input = params.get("task2_input")
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task_input + time_str + "===="
    return {"task2_output": result}
"""
        task_input2 = {
            "input_params": {
                "1": {
                    "key": "task2_input",
                    "input_schema": "from_task",
                    "data_type": "str",
                    "value": f"{task_id1}.output.task1_output",
                }
            }
        }
        task_output2 = {
            "output_params": {
                "1": {
                    "key": "task2_output",
                    "data_type": "str",
                }
            }
        }
        resources2 = {
            "cpu": 10,
            "cpu_mem": 123,
            "gpu": 0.8,
            "gpu_mem": 324
        }
        api_client.save_task(workflow_id, task_id2, code_str2, task_input2, task_output2, resources2)
        print(f"✓ 添加任务2: {task_id2}")
        
        # 4. 添加边
        api_client.add_edge(workflow_id, task_id1, task_id2)
        print(f"✓ 添加边: {task_id1} -> {task_id2}")
        
        # 5. 运行工作流
        run_id = api_client.run_workflow(workflow_id)
        print(f"✓ 开始运行工作流,run_id:{run_id}")
        
        # 6. 获取结果
        messages = api_client.get_workflow_results(workflow_id,run_id)
        
        # 验证结果
        assert len(messages) > 0, "应该收到至少一条消息"
        
        # 统计消息类型
        start_tasks = [m for m in messages if m.get("type") == "start_task"]
        finish_tasks = [m for m in messages if m.get("type") == "finish_task"]
        finish_workflow = [m for m in messages if m.get("type") == "finish_workflow"]
        
        print(f"\n收到消息统计:")
        print(f"  - 任务开始: {len(start_tasks)} 条")
        print(f"  - 任务完成: {len(finish_tasks)} 条")
        print(f"  - 工作流完成: {len(finish_workflow)} 条")
        
        # 断言
        assert len(start_tasks) == 2, f"应该有2个任务开始，实际 {len(start_tasks)}"
        assert len(finish_tasks) == 2, f"应该有2个任务完成，实际 {len(finish_tasks)}"
        assert len(finish_workflow) >= 1, "工作流应该完成"
        
        # 验证任务结果
        for task in finish_tasks:
            result = task.get("data", {}).get("result")
            assert result is not None, "任务结果不应为空"
            assert isinstance(result, dict), "任务结果应该是字典"
        
        print(f"\n✓ 测试通过: 完整工作流执行成功")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])


