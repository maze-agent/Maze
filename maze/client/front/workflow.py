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
    Maze workflow object for managing tasks and execution flow
    
    Example:
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
        Initialize workflow object
        
        Args:
            workflow_id: Workflow ID
            server_url: Server address
        """
        self.workflow_id = workflow_id
        self.server_url = server_url.rstrip('/')
        self._tasks: Dict[str, MaTask] = {}
        
    def add_task(self, 
                 task_func: Callable = None,
                 inputs: Dict[str, Any] = None,
                 task_type: str = "code", 
                 task_name: Optional[str] = None,
                 # Legacy API compatibility
                 code_str: str = None,
                 task_input: Dict[str, Any] = None,
                 task_output: Dict[str, Any] = None,
                 resources: Dict[str, Any] = None) -> MaTask:
        """
        Add task to workflow (supports decorator function or manual configuration)
        
        New API (recommended):
            task1 = workflow.add_task(
                task_func=my_decorated_func,
                inputs={"input_key": "value"}
            )
            
        Or more concise:
            task1 = workflow.add_task(my_decorated_func, inputs={"input_key": "value"})
            
        Reference other task outputs:
            task2 = workflow.add_task(
                func2, 
                inputs={"input_key": task1.outputs["output_key"]}
            )
        
        Legacy API (still supported):
            task = workflow.add_task(task_type="code", task_name="task")
            task.save(code_str, task_input, task_output, resources)
        
        Args:
            task_func: Function decorated with @task
            inputs: Input parameter dictionary {param_name: value or TaskOutput}
            task_type: Task type, defaults to "code"
            task_name: Task name
            
        Returns:
            MaTask: Created task object
        """
        # New API: Use decorator function
        if task_func is not None:
            return self._add_task_from_decorator(task_func, inputs, task_name)
        
        # Legacy API: Manual configuration (kept for compatibility)
        return self._add_task_manual(task_type, task_name)
    
    def _add_task_from_decorator(self, 
                                  task_func: Callable,
                                  inputs: Dict[str, Any],
                                  task_name: Optional[str] = None) -> MaTask:
        """
        Create task from decorator function (internal method)
        """
        # Get function metadata
        metadata = get_task_metadata(task_func)
        
        # Use function name as task name (if not specified)
        if task_name is None:
            task_name = metadata.func_name
        
        # 1. Create task
        url = f"{self.server_url}/add_task"
        data = {
            'workflow_id': self.workflow_id,
            'task_type': 'code',
            'task_name': task_name
        }
        
        response = requests.post(url, json=data)
        
        if response.status_code != 200:
            raise Exception(f"Request failed, status code: {response.status_code}, response: {response.text}")
        
        result = response.json()
        if result.get("status") != "success":
            raise Exception(f"Failed to add task: {result.get('message', 'Unknown error')}")
        
        task_id = result["task_id"]
        
        # 2. Build input parameter configuration
        task_input = self._build_task_input(inputs, metadata)
        
        # 3. Build output parameter configuration
        task_output = self._build_task_output(metadata)
        
        # 4. Save task configuration
        save_url = f"{self.server_url}/save_task"
        save_data = {
            'workflow_id': self.workflow_id,
            'task_id': task_id,
            'code_str': metadata.code_str,
            'code_ser': metadata.code_ser,  # 传递序列化的函数
            'task_input': task_input,
            'task_output': task_output,
            'resources': metadata.resources,
        }
        
        save_response = requests.post(save_url, json=save_data)
        
        if save_response.status_code != 200:
            raise Exception(f"Failed to save task, status code: {save_response.status_code}")
        
        save_result = save_response.json()
        if save_result.get("status") != "success":
            raise Exception(f"Failed to save task: {save_result.get('message', 'Unknown error')}")
        
        # 5. Create task object
        task = MaTask(task_id, self.workflow_id, self.server_url, task_name, metadata.outputs)
        self._tasks[task_id] = task
        
        return task
    
    def _upload_file(self, file_input: FileInput) -> str:
        """
        Upload file to server (internal method)
        
        Args:
            file_input: File input object
            
        Returns:
            str: Server file path
        """
        url = f"{self.server_url}/upload_file/{self.workflow_id}"
        
        # Read file content
        file_content = file_input.read_bytes()
        
        # Upload file
        files = {'file': (file_input.filename, file_content)}
        response = requests.post(url, files=files)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") == "success":
                return result["server_path"]
            else:
                raise Exception(f"File upload failed: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"File upload failed, status code: {response.status_code}")
    
    def _build_task_input(self, inputs: Dict[str, Any], metadata) -> Dict[str, Any]:
        """Build task input configuration (internal method)"""
        if inputs is None:
            inputs = {}
        
        task_input = {"input_params": {}}
        
        for idx, input_key in enumerate(metadata.inputs, start=1):
            input_value = inputs.get(input_key)
            data_type = metadata.data_types.get(input_key, "str")
            
            # Check if it's a TaskOutput reference
            if isinstance(input_value, TaskOutput):
                input_schema = "from_task"
                value = input_value.to_reference_string()
            # Check if it's a FileInput object
            elif isinstance(input_value, FileInput):
                input_schema = "from_user"
                # Upload file and get server path
                value = self._upload_file(input_value)
            # Check if data type is file type but input is string path
            elif is_file_type(data_type) and isinstance(input_value, str):
                input_schema = "from_user"
                # Automatically convert string path to FileInput and upload
                try:
                    file_input = FileInput(input_value)
                    value = self._upload_file(file_input)
                except Exception as e:
                    # If not a valid file path, use original value directly
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
        """Build task output configuration (internal method)"""
        task_output = {"output_params": {}}
        
        for idx, output_key in enumerate(metadata.outputs, start=1):
            task_output["output_params"][str(idx)] = {
                "key": output_key,
                "data_type": metadata.data_types.get(output_key, "str")
            }
        
        return task_output
    
    def _add_task_manual(self, task_type: str, task_name: Optional[str]) -> MaTask:
        """
        Manually add task (legacy API, internal method)
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
                raise Exception(f"Failed to add task: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"Request failed, status code: {response.status_code}, response: {response.text}")
    
    def get_tasks(self) -> List[Dict[str, str]]:
        """
        Get list of all tasks in workflow
        
        Returns:
            List[Dict]: Task list, each task contains id and name
        """
        url = f"{self.server_url}/get_workflow_tasks/{self.workflow_id}"
        response = requests.get(url)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") == "success":
                return result.get("tasks", [])
            else:
                raise Exception(f"Failed to get task list: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"Request failed, status code: {response.status_code}, response: {response.text}")
    
    def add_edge(self, source_task: MaTask, target_task: MaTask) -> None:
        """
        Add dependency edge between tasks (source_task -> target_task)
        
        Args:
            source_task: Source task
            target_task: Target task
            
        Raises:
            Exception: If addition fails
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
                raise Exception(f"Failed to add edge: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"Request failed, status code: {response.status_code}, response: {response.text}")
    
    def del_edge(self, source_task: MaTask, target_task: MaTask) -> None:
        """
        Delete dependency edge between tasks
        
        Args:
            source_task: Source task
            target_task: Target task
            
        Raises:
            Exception: If deletion fails
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
                raise Exception(f"Failed to delete edge: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"Request failed, status code: {response.status_code}, response: {response.text}")
    
    def run(self) -> str:
        """
        Run workflow
        
        Note: This method submits the workflow execution request and returns run_id
        
        Returns:
            str: Run ID for this execution
        
        Raises:
            Exception: If execution fails
        """
        url = f"{self.server_url}/run_workflow"
        data = {
            'workflow_id': self.workflow_id,
        }
        
        response = requests.post(url, json=data)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") == "success":
                return result.get("run_id")
            else:
                raise Exception(f"Failed to run workflow: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"Request failed, status code: {response.status_code}, response: {response.text}")
    
    def get_results(self, run_id: str, verbose: bool = False, output_dir: str = "workflow_results") -> Dict[str, Any]:
        """
        Get workflow execution results and download files
        
        Note: This method is primarily for the front client (file download support).
        For maze client, use show_results() for formatted display.
        
        Args:
            run_id: Run ID returned by run() method
            verbose: Whether to print execution progress (default False)
            output_dir: File download directory (default workflow_results)
            
        Returns:
            Dict: Output result of the last task (file paths replaced with local paths)
            
        Example:
            run_id = workflow.run()
            result = workflow.get_results(run_id)
            # result = {"output_image": "workflow_results/xxx/image.jpg", "metadata": {...}}
        """
        self._execution_results = {}
        self._downloaded_files = {}  # Server path -> local path mapping
        ws_url = self.server_url.replace('http://', 'ws://').replace('https://', 'wss://')
        url = f"{ws_url}/get_workflow_res/{self.workflow_id}/{run_id}"
        
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
            # Check if it's a normal closure
            if hasattr(error, 'data'):
                try:
                    error_code = int.from_bytes(error.data, 'big')
                    if error_code == 1000:
                        return  # Normal closure
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
        
        # Run WebSocket in background thread
        import threading
        ws_thread = threading.Thread(target=ws.run_forever)
        ws_thread.daemon = True
        ws_thread.start()
        
        # Wait and process messages
        import time
        from pathlib import Path
        
        last_count = 0
        last_task_result = None
        last_task_id = None
        
        while ws_thread.is_alive() or len(messages) > last_count:
            while len(messages) > last_count:
                msg = messages[last_count]
                msg_type = msg.get("type")
                
                # Extract data field (if exists)
                msg_data = msg.get("data", msg)  # Compatible with both formats
                
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
                
                # Save task results
                if msg_type == "finish_task":
                    task_id = msg_data.get("task_id")
                    result = msg_data.get("result")
                    if task_id and result:
                        self._execution_results[task_id] = result
                        last_task_result = result
                        last_task_id = task_id
                
                elif msg_type == "task_exception":
                    # Save error information
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
                # If it's a file path, download it
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
    
    def show_results(self, run_id: str, output_dir: str = "workflow_results") -> Dict[str, Any]:
        """
        Simple interface to display workflow results with automatic progress printing
        
        This is a high-level wrapper around get_results() that automatically prints
        execution progress and returns the final result. Perfect for quick testing and demos.
        
        Args:
            run_id: Run ID returned by run() method
            output_dir: File download directory (default workflow_results)
        
        Returns:
            Dict: Output result of the last task (file paths replaced with local paths)
        
        Example:
            run_id = workflow.run()
            result = workflow.show_results(run_id)
            print(f"Final output: {result}")
        """
        return self.get_results(run_id, verbose=True, output_dir=output_dir)
    
    def _download_file(self, server_path: str, local_path: str = None) -> str:
        """
        Download file from server to local
        
        Args:
            server_path: Server file path
            local_path: Local save path (optional, defaults to current directory)
            
        Returns:
            str: Local file path
            
        Raises:
            Exception: If download fails
            
        Example:
            # Download single file
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
            # Determine local save path
            if local_path is None:
                # Default to current directory, use original filename
                server_path_obj = Path(server_path)
                local_path = server_path_obj.name
            
            local_path_obj = Path(local_path)
            
            # Ensure directory exists
            local_path_obj.parent.mkdir(parents=True, exist_ok=True)
            
            # Save file
            with open(local_path_obj, 'wb') as f:
                f.write(response.content)
            
            return str(local_path_obj)
        else:
            # Possibly JSON error response
            try:
                result = response.json()
                raise Exception(f"Failed to download file: {result.get('message', 'Unknown error')}")
            except:
                raise Exception(f"Failed to download file, status code: {response.status_code}")
    
    def download_file(self, server_path: str, local_path: str = None) -> str:
        """
        Manually download specified file (advanced usage)
        
        Args:
            server_path: Server file path
            local_path: Local save path (optional)
            
        Returns:
            str: Local file path
        """
        return self._download_file(server_path, local_path)
    
    def _looks_like_file_path(self, value: str) -> bool:
        """
        Heuristically determine if string is a file path (internal method)
        """
        # Check if it contains common file extensions
        common_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp',  # Images
                            '.mp3', '.wav', '.ogg', '.flac', '.m4a',  # Audio
                            '.mp4', '.avi', '.mov', '.mkv',  # Video
                            '.txt', '.pdf', '.doc', '.docx',  # Documents
                            '.zip', '.tar', '.gz']  # Archives
        
        value_lower = value.lower()
        return any(value_lower.endswith(ext) for ext in common_extensions)
    
    def cleanup(self) -> None:
        """
        Clean up server-side temporary files
        
        Note: get_results() has automatically downloaded all files, no need to worry about losing results
        
        Raises:
            Exception: If cleanup fails
        """
        url = f"{self.server_url}/cleanup_workflow/{self.workflow_id}"
        response = requests.post(url)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") != "success":
                raise Exception(f"Failed to cleanup workflow: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"Request failed, status code: {response.status_code}, response: {response.text}")
    
    def __repr__(self) -> str:
        return f"MaWorkflow(id='{self.workflow_id[:8]}...', tasks={len(self._tasks)})"
