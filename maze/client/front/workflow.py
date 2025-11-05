import requests
import websocket
import sys
from typing import Optional, Iterator, Dict, Any, List, Callable, Union
from pathlib import Path
from maze.client.front.task import MaTask, TaskOutput
from maze.client.front.decorator import get_task_metadata
from maze.client.front.file_utils import FileInput, is_file_type


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
            'code_ser': metadata.code_ser,  # 传递序列化的函数
            'task_input': task_input,
            'task_output': task_output,
            'resources': metadata.resources,
            'node_type': metadata.node_type,  # 传递节点类型
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
    
    def _upload_file(self, file_input: FileInput) -> str:
        """
        上传文件到服务器（内部方法）
        
        Args:
            file_input: 文件输入对象
            
        Returns:
            str: 服务器文件路径
        """
        url = f"{self.server_url}/upload_file/{self.workflow_id}"
        
        # 读取文件内容
        file_content = file_input.read_bytes()
        
        # 上传文件
        files = {'file': (file_input.filename, file_content)}
        response = requests.post(url, files=files)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") == "success":
                return result["server_path"]
            else:
                raise Exception(f"文件上传失败: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"文件上传失败，状态码：{response.status_code}")
    
    def _build_task_input(self, inputs: Dict[str, Any], metadata) -> Dict[str, Any]:
        """构建任务输入配置（内部方法）"""
        if inputs is None:
            inputs = {}
        
        task_input = {"input_params": {}}
        
        for idx, input_key in enumerate(metadata.inputs, start=1):
            input_value = inputs.get(input_key)
            data_type = metadata.data_types.get(input_key, "str")
            
            # 检查是否是TaskOutput引用
            if isinstance(input_value, TaskOutput):
                input_schema = "from_task"
                value = input_value.to_reference_string()
            # 检查是否是FileInput对象
            elif isinstance(input_value, FileInput):
                input_schema = "from_user"
                # 上传文件并获取服务器路径
                value = self._upload_file(input_value)
            # 检查数据类型是否是文件类型但输入是字符串路径
            elif is_file_type(data_type) and isinstance(input_value, str):
                input_schema = "from_user"
                # 自动将字符串路径转换为FileInput并上传
                try:
                    file_input = FileInput(input_value)
                    value = self._upload_file(file_input)
                except Exception as e:
                    # 如果不是有效的文件路径，直接使用原值
                    value = input_value
            else:
                input_schema = "from_user"
                value = input_value if input_value is not None else ""
            
            task_input["input_params"][str(idx)] = {
                "key": input_key,
                "input_schema": input_schema,
                "data_type": data_type,
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
    
    def get_results(self, verbose: bool = True, output_dir: str = "workflow_results") -> Dict[str, Any]:
        """
        运行工作流并获取最终结果
        
        Args:
            verbose: 是否打印执行进度（默认True）
            output_dir: 文件下载目录（默认 workflow_results）
            
        Returns:
            Dict: 最后一个任务的输出结果（文件路径已替换为本地路径）
            
        示例:
            workflow.run()
            result = workflow.get_results()
            # result = {"output_image": "workflow_results/xxx/image.jpg", "metadata": {...}}
        """
        self._execution_results = {}
        self._downloaded_files = {}  # 服务器路径 -> 本地路径的映射
        ws_url = self.server_url.replace('http://', 'ws://').replace('https://', 'wss://')
        url = f"{ws_url}/get_workflow_res/{self.workflow_id}"
        
        messages = []
        exception_occurred = False
        
        def on_message(ws, message):
            import json
            msg_data = json.loads(message)
            messages.append(msg_data)
            if verbose:
                print(f"Received message: {msg_data}", file=sys.stderr)
        
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
                print(f"WebSocket error: {error}", file=sys.stderr)
        
        def on_close(ws, close_status_code, close_msg):
            if verbose:
                print("WebSocket connection closed", file=sys.stderr)
        
        def on_open(ws):
            if verbose:
                print(f"Connected to {url}", file=sys.stderr)
        
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
        
        # 等待并处理消息
        import time
        from pathlib import Path
        
        last_count = 0
        last_task_result = None
        last_task_id = None
        
        while ws_thread.is_alive() or len(messages) > last_count:
            while len(messages) > last_count:
                msg = messages[last_count]
                msg_type = msg.get("type")
                
                # 提取 data 字段（如果存在）
                msg_data = msg.get("data", msg)  # 兼容两种格式
                
                if verbose:
                    if msg_type == "start_task":
                        task_id = msg_data.get('task_id')
                        if task_id:
                            print(f"▶ Task started: {task_id[:8]}...", file=sys.stderr)
                    elif msg_type == "finish_task":
                        task_id = msg_data.get('task_id')
                        if task_id:
                            print(f"✓ Task completed: {task_id[:8]}...", file=sys.stderr)
                    elif msg_type == "task_exception":
                        task_id = msg_data.get('task_id')
                        if task_id:
                            print(f"\n❌ Task exception: {task_id[:8]}...", file=sys.stderr)
                        print(f"   Error type: {msg_data.get('result')}", file=sys.stderr)
                        
                        # Show detailed error information (if available)
                        if 'error_details' in msg_data:
                            details = msg_data['error_details']
                            print(f"\nDetailed error information:", file=sys.stderr)
                            print(f"  {details.get('error_message', 'N/A')}", file=sys.stderr)
                            if 'traceback' in details:
                                print(f"\nTraceback:", file=sys.stderr)
                                print(details['traceback'], file=sys.stderr)
                
                # 保存任务结果
                if msg_type == "finish_task":
                    task_id = msg_data.get("task_id")
                    result = msg_data.get("result")
                    if task_id and result:
                        self._execution_results[task_id] = result
                        last_task_result = result
                        last_task_id = task_id
                
                elif msg_type == "task_exception":
                    # 保存错误信息
                    task_id = msg_data.get("task_id")
                    if task_id:
                        error_info = {
                            "error": msg_data.get('result'),
                            "details": msg_data.get('error_details', {})
                        }
                        self._execution_results[task_id] = error_info
                
                elif msg_type == "finish_workflow":
                    if verbose:
                        print("✓ Workflow completed", file=sys.stderr)
                
                last_count += 1
            time.sleep(0.1)
        
        if exception_occurred:
            raise Exception("An exception occurred during workflow execution")
        
        # Only download files from the last task
        if last_task_result and isinstance(last_task_result, dict):
            if verbose:
                print("\nDownloading result files...", file=sys.stderr)
            
            final_result = {}
            for key, value in last_task_result.items():
                # 如果是文件路径，下载它
                if isinstance(value, str) and self._looks_like_file_path(value):
                    try:
                        output_dir_path = Path(output_dir) / self.workflow_id
                        output_dir_path.mkdir(parents=True, exist_ok=True)
                        
                        filename = Path(value).name
                        local_path = output_dir_path / filename
                        
                        downloaded_path = self._download_file(value, str(local_path))
                        final_result[key] = downloaded_path
                        
                        if verbose:
                            print(f"  ✓ {key}: {downloaded_path}", file=sys.stderr)
                    except Exception as e:
                        if verbose:
                            print(f"  ✗ {key}: Download failed ({e})", file=sys.stderr)
                        final_result[key] = value
                else:
                    final_result[key] = value
            
            return final_result
        
        return last_task_result if last_task_result else {}
    
    def _download_file(self, server_path: str, local_path: str = None) -> str:
        """
        从服务器下载文件到本地
        
        Args:
            server_path: 服务器文件路径
            local_path: 本地保存路径（可选，默认保存到当前目录）
            
        Returns:
            str: 本地文件路径
            
        Raises:
            Exception: 如果下载失败
            
        示例:
            # 下载单个文件
            local_path = workflow.download_file(
                server_path="temp/workflow_id/output.jpg",
                local_path="./results/output.jpg"
            )
        """
        from pathlib import Path
        
        url = f"{self.server_url}/download_file/{self.workflow_id}"
        params = {"file_path": server_path}
        
        response = requests.get(url, params=params)
        
        if response.status_code == 200:
            # 确定本地保存路径
            if local_path is None:
                # 默认保存到当前目录，使用原文件名
                server_path_obj = Path(server_path)
                local_path = server_path_obj.name
            
            local_path_obj = Path(local_path)
            
            # 确保目录存在
            local_path_obj.parent.mkdir(parents=True, exist_ok=True)
            
            # 保存文件
            with open(local_path_obj, 'wb') as f:
                f.write(response.content)
            
            return str(local_path_obj)
        else:
            # 可能是JSON错误响应
            try:
                result = response.json()
                raise Exception(f"下载文件失败: {result.get('message', 'Unknown error')}")
            except:
                raise Exception(f"下载文件失败，状态码：{response.status_code}")
    
    def download_file(self, server_path: str, local_path: str = None) -> str:
        """
        手动下载指定文件（高级用法）
        
        Args:
            server_path: 服务器文件路径
            local_path: 本地保存路径（可选）
            
        Returns:
            str: 本地文件路径
        """
        return self._download_file(server_path, local_path)
    
    def _looks_like_file_path(self, value: str) -> bool:
        """
        启发式判断字符串是否是文件路径（内部方法）
        """
        # 检查是否包含常见文件扩展名
        common_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp',  # 图片
                            '.mp3', '.wav', '.ogg', '.flac', '.m4a',  # 音频
                            '.mp4', '.avi', '.mov', '.mkv',  # 视频
                            '.txt', '.pdf', '.doc', '.docx',  # 文档
                            '.zip', '.tar', '.gz']  # 压缩
        
        value_lower = value.lower()
        return any(value_lower.endswith(ext) for ext in common_extensions)
    
    def cleanup(self) -> None:
        """
        清理服务端临时文件
        
        注意：get_results() 已自动下载所有文件，无需担心丢失结果
        
        Raises:
            Exception: 如果清理失败
        """
        url = f"{self.server_url}/cleanup_workflow/{self.workflow_id}"
        response = requests.post(url)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") != "success":
                raise Exception(f"清理工作流失败: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"请求失败，状态码：{response.status_code}, 响应：{response.text}")
    
    def __repr__(self) -> str:
        return f"MaWorkflow(id='{self.workflow_id[:8]}...', tasks={len(self._tasks)})"
