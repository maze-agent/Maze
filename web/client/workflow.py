import requests
import websocket
from typing import Optional, Iterator, Dict, Any, List, Callable, Union
from maze.core.client.models import MaTask, TaskOutput
from maze.core.client.decorator import get_task_metadata


class MaWorkflow:
    """
    Maze工作流对象，用于管理任务和执行流程
    
    示例:
        workflow = client.create_workflow()
        task1 = workflow.add_task(func1, inputs={"in": "value"})
        task2 = workflow.add_task(func2, inputs={"in": task1.outputs["out"]})
        workflow.add_edge(task1, task2)
        workflow.run()
        for msg in workflow.get_results():
            print(msg)
    """
    
    def __init__(self, workflow_id: str, server_url: str):
        """
        初始化工作流对象
        
        Args:
            workflow_id: 工作流ID
            server_url: 服务器地址
        """
        self.workflow_id = workflow_id
        self.server_url = server_url.rstrip('/')
        self._tasks: Dict[str, MaTask] = {}
        
    def add_task(self, 
                 task_func: Callable = None,
                 inputs: Dict[str, Any] = None,
                 task_type: str = "code", 
                 task_name: Optional[str] = None,
                 # 兼容旧API
                 code_str: str = None,
                 task_input: Dict[str, Any] = None,
                 task_output: Dict[str, Any] = None,
                 resources: Dict[str, Any] = None) -> MaTask:
        """
        添加任务到工作流（支持装饰器函数或手动配置）
        
        新API（推荐）:
            task1 = workflow.add_task(
                task_func=my_decorated_func,
                inputs={"input_key": "value"}
            )
            
        或更简洁：
            task1 = workflow.add_task(my_decorated_func, inputs={"input_key": "value"})
            
        引用其他任务的输出：
            task2 = workflow.add_task(
                func2, 
                inputs={"input_key": task1.outputs["output_key"]}
            )
        
        旧API（仍然支持）:
            task = workflow.add_task(task_type="code", task_name="任务")
            task.save(code_str, task_input, task_output, resources)
        
        Args:
            task_func: 使用@task装饰的函数
            inputs: 输入参数字典 {参数名: 值或TaskOutput}
            task_type: 任务类型，默认"code"
            task_name: 任务名称
            
        Returns:
            MaTask: 创建的任务对象
        """
        # 新API：使用装饰器函数
        if task_func is not None:
            return self._add_task_from_decorator(task_func, inputs, task_name)
        
        # 旧API：手动配置（为了兼容性保留）
        return self._add_task_manual(task_type, task_name)
    
    def _add_task_from_decorator(self, 
                                  task_func: Callable,
                                  inputs: Dict[str, Any],
                                  task_name: Optional[str] = None) -> MaTask:
        """
        从装饰器函数创建任务（内部方法）
        """
        # 获取函数的元数据
        metadata = get_task_metadata(task_func)
        
        # 使用函数名作为任务名（如果没有指定）
        if task_name is None:
            task_name = metadata.func_name
        
        # 1. 创建任务
        url = f"{self.server_url}/add_task"
        data = {
            'workflow_id': self.workflow_id,
            'task_type': 'code',
            'task_name': task_name
        }
        
        response = requests.post(url, json=data)
        
        if response.status_code != 200:
            raise Exception(f"请求失败，状态码：{response.status_code}, 响应：{response.text}")
        
        result = response.json()
        if result.get("status") != "success":
            raise Exception(f"添加任务失败: {result.get('message', 'Unknown error')}")
        
        task_id = result["task_id"]
        
        # 2. 构建输入参数配置
        task_input = self._build_task_input(inputs, metadata)
        
        # 3. 构建输出参数配置
        task_output = self._build_task_output(metadata)
        
        # 4. 保存任务配置
        save_url = f"{self.server_url}/save_task"
        save_data = {
            'workflow_id': self.workflow_id,
            'task_id': task_id,
            'code_str': metadata.code_str,
            'task_input': task_input,
            'task_output': task_output,
            'resources': metadata.resources,
        }
        
        save_response = requests.post(save_url, json=save_data)
        
        if save_response.status_code != 200:
            raise Exception(f"保存任务失败，状态码：{save_response.status_code}")
        
        save_result = save_response.json()
        if save_result.get("status") != "success":
            raise Exception(f"保存任务失败: {save_result.get('message', 'Unknown error')}")
        
        # 5. 创建任务对象
        task = MaTask(task_id, self.workflow_id, self.server_url, task_name, metadata.outputs)
        self._tasks[task_id] = task
        
        return task
    
    def _build_task_input(self, inputs: Dict[str, Any], metadata) -> Dict[str, Any]:
        """构建任务输入配置（内部方法）"""
        if inputs is None:
            inputs = {}
        
        task_input = {"input_params": {}}
        
        for idx, input_key in enumerate(metadata.inputs, start=1):
            input_value = inputs.get(input_key)
            
            # 检查是否是TaskOutput引用
            if isinstance(input_value, TaskOutput):
                input_schema = "from_task"
                value = input_value.to_reference_string()
            else:
                input_schema = "from_user"
                value = input_value if input_value is not None else ""
            
            task_input["input_params"][str(idx)] = {
                "key": input_key,
                "input_schema": input_schema,
                "data_type": metadata.data_types.get(input_key, "str"),
                "value": value
            }
        
        return task_input
    
    def _build_task_output(self, metadata) -> Dict[str, Any]:
        """构建任务输出配置（内部方法）"""
        task_output = {"output_params": {}}
        
        for idx, output_key in enumerate(metadata.outputs, start=1):
            task_output["output_params"][str(idx)] = {
                "key": output_key,
                "data_type": metadata.data_types.get(output_key, "str")
            }
        
        return task_output
    
    def _add_task_manual(self, task_type: str, task_name: Optional[str]) -> MaTask:
        """
        手动添加任务（旧API，内部方法）
        """
        url = f"{self.server_url}/add_task"
        data = {
            'workflow_id': self.workflow_id,
            'task_type': task_type,
            'task_name': task_name
        }
        
        response = requests.post(url, json=data)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") == "success":
                task_id = result["task_id"]
                task = MaTask(task_id, self.workflow_id, self.server_url, task_name)
                self._tasks[task_id] = task
                return task
            else:
                raise Exception(f"添加任务失败: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"请求失败，状态码：{response.status_code}, 响应：{response.text}")
    
    def get_tasks(self) -> List[Dict[str, str]]:
        """
        获取工作流中的所有任务列表
        
        Returns:
            List[Dict]: 任务列表，每个任务包含id和name
        """
        url = f"{self.server_url}/get_workflow_tasks/{self.workflow_id}"
        response = requests.get(url)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") == "success":
                return result.get("tasks", [])
            else:
                raise Exception(f"获取任务列表失败: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"请求失败，状态码：{response.status_code}, 响应：{response.text}")
    
    def add_edge(self, source_task: MaTask, target_task: MaTask) -> None:
        """
        添加任务间的依赖边（source_task -> target_task）
        
        Args:
            source_task: 源任务
            target_task: 目标任务
            
        Raises:
            Exception: 如果添加失败
        """
        url = f"{self.server_url}/add_edge"
        data = {
            'workflow_id': self.workflow_id,
            'source_task_id': source_task.task_id,
            'target_task_id': target_task.task_id,
        }
        
        response = requests.post(url, json=data)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") != "success":
                raise Exception(f"添加边失败: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"请求失败，状态码：{response.status_code}, 响应：{response.text}")
    
    def del_edge(self, source_task: MaTask, target_task: MaTask) -> None:
        """
        删除任务间的依赖边
        
        Args:
            source_task: 源任务
            target_task: 目标任务
            
        Raises:
            Exception: 如果删除失败
        """
        url = f"{self.server_url}/del_edge"
        data = {
            'workflow_id': self.workflow_id,
            'source_task_id': source_task.task_id,
            'target_task_id': target_task.task_id,
        }
        
        response = requests.post(url, json=data)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") != "success":
                raise Exception(f"删除边失败: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"请求失败，状态码：{response.status_code}, 响应：{response.text}")
    
    def run(self) -> None:
        """
        运行工作流
        
        注意：此方法只是提交工作流执行请求，需要调用 get_results() 来获取执行结果
        
        Raises:
            Exception: 如果运行失败
        """
        url = f"{self.server_url}/run_workflow"
        data = {
            'workflow_id': self.workflow_id,
        }
        
        response = requests.post(url, json=data)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") != "success":
                raise Exception(f"运行工作流失败: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"请求失败，状态码：{response.status_code}, 响应：{response.text}")
    
    def get_results(self, verbose: bool = True) -> Iterator[Dict[str, Any]]:
        """
        通过WebSocket获取工作流执行结果（生成器方式）
        
        Args:
            verbose: 是否打印接收到的消息
            
        Yields:
            Dict: 任务执行结果消息
            
        示例:
            workflow.run()
            for message in workflow.get_results():
                print(f"收到消息: {message}")
        """
        ws_url = self.server_url.replace('http://', 'ws://').replace('https://', 'wss://')
        url = f"{ws_url}/get_workflow_res/{self.workflow_id}"
        
        messages = []
        exception_occurred = False
        
        def on_message(ws, message):
            import json
            msg_data = json.loads(message)
            messages.append(msg_data)
            if verbose:
                print(f"收到消息: {msg_data}")
        
        def on_error(ws, error):
            nonlocal exception_occurred
            # 检查是否是正常关闭
            if hasattr(error, 'data'):
                try:
                    error_code = int.from_bytes(error.data, 'big')
                    if error_code == 1000:
                        return  # 正常关闭
                except:
                    pass
            exception_occurred = True
            if verbose:
                print(f"WebSocket错误: {error}")
        
        def on_close(ws, close_status_code, close_msg):
            if verbose:
                print("WebSocket连接已关闭")
        
        def on_open(ws):
            if verbose:
                print(f"已连接到 {url}")
        
        ws = websocket.WebSocketApp(
            url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        
        # 在后台线程中运行WebSocket
        import threading
        ws_thread = threading.Thread(target=ws.run_forever)
        ws_thread.daemon = True
        ws_thread.start()
        
        # 等待并返回消息
        import time
        last_count = 0
        while ws_thread.is_alive() or len(messages) > last_count:
            while len(messages) > last_count:
                yield messages[last_count]
                last_count += 1
            time.sleep(0.1)
        
        if exception_occurred:
            raise Exception("工作流执行过程中发生异常")
    
    def __repr__(self) -> str:
        return f"MaWorkflow(id='{self.workflow_id[:8]}...', tasks={len(self._tasks)})"
