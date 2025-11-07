import requests
import websocket
from typing import Optional, Iterator, Dict, Any, List, Callable, Union
from maze.client.maze.models import MaTask, TaskOutput
from maze.client.maze.decorator import get_task_metadata


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
        
        # 4. Save task configuration (automatically add edges using new interface)
        save_url = f"{self.server_url}/save_task_and_add_edge"
        save_data = {
            'workflow_id': self.workflow_id,
            'task_id': task_id,
            'code_str': metadata.code_str,
            'code_ser': metadata.code_ser,  # Add serialized function
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
    
    def _build_task_input(self, inputs: Dict[str, Any], metadata) -> Dict[str, Any]:
        """Build task input configuration (internal method)"""
        if inputs is None:
            inputs = {}
        
        task_input = {"input_params": {}}
        
        for idx, input_key in enumerate(metadata.inputs, start=1):
            input_value = inputs.get(input_key)
            
            # Check if it's a TaskOutput reference
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
    
    def run(self) -> None:
        """
        Run workflow
        
        Note: This method only submits the workflow execution request, need to call get_results() to get execution results
        
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
            if result.get("status") != "success":
                raise Exception(f"Failed to run workflow: {result.get('message', 'Unknown error')}")
        else:
            raise Exception(f"Request failed, status code: {response.status_code}, response: {response.text}")
    
    def get_results(self, verbose: bool = True) -> Iterator[Dict[str, Any]]:
        """
        Get workflow execution results via WebSocket (generator method)
        
        Args:
            verbose: Whether to print received messages
            
        Yields:
            Dict: Task execution result messages
            
        Example:
            workflow.run()
            for message in workflow.get_results():
                print(f"Received message: {message}")
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
                print(f"Received message: {msg_data}")
        
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
                print(f"WebSocket error: {error}")
        
        def on_close(ws, close_status_code, close_msg):
            if verbose:
                print("WebSocket connection closed")
        
        def on_open(ws):
            if verbose:
                print(f"Connected to {url}")
        
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
        
        # Wait and return messages
        import time
        last_count = 0
        while ws_thread.is_alive() or len(messages) > last_count:
            while len(messages) > last_count:
                yield messages[last_count]
                last_count += 1
            time.sleep(0.1)
        
        if exception_occurred:
            raise Exception("An exception occurred during workflow execution")
    
    def show_results(self) -> None:
        """
        Simple interface to display workflow execution results with formatted output
        
        This is a high-level wrapper around get_results() that automatically formats
        and prints execution progress. Perfect for quick testing and demos.
        
        Example:
            workflow.run()
            workflow.show_results()
        """
        for message in self.get_results(verbose=False):
            msg_type = message.get("type")
            msg_data = message.get("data", {})
            
            if msg_type == "start_task":
                task_id = msg_data.get('task_id', '')[:8]
                print(f"▶ Task started: {task_id}...")
                
            elif msg_type == "finish_task":
                task_id = msg_data.get('task_id', '')[:8]
                result = msg_data.get('result')
                print(f"✓ Task completed: {task_id}")
                if result:
                    print(f"  Result: {result}\n")
                    
            elif msg_type == "finish_workflow":
                print("=" * 60)
                print("🎉 Workflow execution completed!")
                print("=" * 60)
                break
    
    def __repr__(self) -> str:
        return f"MaWorkflow(id='{self.workflow_id[:8]}...', tasks={len(self._tasks)})"
