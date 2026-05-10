import requests
import websocket
from typing import Optional, Iterator, Dict, Any, List, Callable, Union
from maze.client.maze.models import MaTask, TaskOutput
from maze.client.maze.decorator import get_task_metadata
import warnings


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
        
        # Graph structure for visualization
        self._nodes: Dict[str, Dict[str, Any]] = {}  # {task_id: {name, func_name, inputs, outputs}}
        self._edges: List[tuple] = []  # [(source_task_id, target_task_id)]
        
        # Results cache to support multiple queries (workaround for server's consume-once logic)
        self._results_cache: Dict[str, List[Dict[str, Any]]] = {}  # {run_id: [messages]}
        
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
        
        # 6. Record node for visualization
        self._nodes[task_id] = {
            "name": task_name,
            "func_name": metadata.func_name,
            "inputs": metadata.inputs,
            "outputs": metadata.outputs,
            "resources": metadata.resources
        }
        
        # 7. Record edges (detect dependencies from TaskOutput references)
        if inputs:
            for input_value in inputs.values():
                if isinstance(input_value, TaskOutput):
                    source_task_id = input_value.task_id
                    if (source_task_id, task_id) not in self._edges:
                        self._edges.append((source_task_id, task_id))
        
        return task
    
    def _build_task_input(self, inputs: Dict[str, Any], metadata) -> Dict[str, Any]:
        """Build task input configuration (internal method)"""
        if inputs is None:
            inputs = {}
        
        task_input = {"input_params": {}}
        
        for idx, input_key in enumerate(metadata.inputs, start=1):
            has_input_value = input_key in inputs
            input_value = inputs.get(input_key)
            
            # Check if it's a TaskOutput reference
            if isinstance(input_value, TaskOutput):
                input_schema = "from_task"
                value = input_value.to_reference_string()
                has_value = True
            else:
                input_schema = "from_user"
                value = input_value
                has_value = has_input_value
            
            task_input["input_params"][str(idx)] = {
                "key": input_key,
                "input_schema": input_schema,
                "data_type": metadata.data_types.get(input_key, "str"),
                "value": value,
                "has_value": has_value
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
    
    def get_results(self, run_id: str, verbose: bool = True) -> List[Dict[str, Any]]:
        """
        Get workflow execution results via WebSocket (returns raw messages)
        
        Results are cached locally, so subsequent calls with the same run_id
        will return cached data without reconnecting to the server.
        
        Args:
            run_id: Run ID returned by run() method
            verbose: Whether to print raw messages (default True)
            
        Returns:
            List[Dict]: List of all messages with complete information
                        Each message format: {'type': '...', 'data': {...}}
            
        Example:
            run_id = workflow.run()
            messages = workflow.get_results(run_id)
            for msg in messages:
                print(msg)  # {'type': 'finish_task', 'data': {...}}
        """
        # Check cache first
        if run_id in self._results_cache:
            cached_messages = self._results_cache[run_id]
            if verbose:
                for msg in cached_messages:
                    print(msg)
            return cached_messages
        
        # Fetch from server if not cached
        ws_url = self.server_url.replace('http://', 'ws://').replace('https://', 'wss://')
        url = f"{ws_url}/get_workflow_res/{self.workflow_id}/{run_id}"
        
        messages = []
        exception_occurred = False
        
        def on_message(ws, message):
            import json
            msg_data = json.loads(message)
            messages.append(msg_data)
            if verbose:
                print(msg_data)
        
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
                print(f"❌ WebSocket error: {error}")
        
        def on_close(ws, close_status_code, close_msg):
            pass  # Silent close
        
        def on_open(ws):
            pass  # Silent open
        
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
        
        # Wait and collect messages
        import time
        last_count = 0
        
        while ws_thread.is_alive() or len(messages) > last_count:
            while len(messages) > last_count:
                last_count += 1
            time.sleep(0.1)
        
        if exception_occurred:
            raise Exception("An exception occurred during workflow execution")
        
        # Cache the results
        self._results_cache[run_id] = messages
        
        return messages
    
    def show_results(self, run_id: str) -> Dict[str, Any]:
        """
        Display workflow execution results with formatted output
        
        Args:
            run_id: Run ID returned by run() method
            
        Returns:
            Dict: Dictionary containing parsed task results
                  Format: {
                      "task_results": {task_id: result, ...},
                      "workflow_completed": bool,
                      "has_exception": bool,
                      "exception_tasks": [task_id, ...]
                  }
        
        Example:
            run_id = workflow.run()
            results = workflow.show_results(run_id)
        """
        print(f"🔗 Connected to the server. Starting workflow execution...")
        
        # Get raw messages without printing
        messages = self.get_results(run_id, verbose=False)
        
        # Parse and display
        task_results = {}
        workflow_completed = False
        has_exception = False
        exception_tasks = []
        
        for msg in messages:
            msg_type = msg.get("type")
            msg_data = msg.get("data", {})
            
            # Collect task results
            if msg_type == "finish_task":
                task_id = msg_data.get('task_id')
                result = msg_data.get('result')
                if task_id:
                    task_results[task_id] = result
            elif msg_type == "task_exception":
                has_exception = True
                task_id = msg_data.get('task_id')
                if task_id:
                    exception_tasks.append(task_id)
            elif msg_type == "finish_workflow":
                workflow_completed = True
            
            # Print formatted output
            if msg_type == "start_task":
                task_id = msg_data.get('task_id', '')[:8]
                print(f"▶ Task started: {task_id}...")
            elif msg_type == "finish_task":
                task_id = msg_data.get('task_id', '')[:8]
                result = msg_data.get('result')
                print(f"✓ Task completed: {task_id}")
                if result:
                    print(f"  Result: {result}")
            elif msg_type == "task_exception":
                task_id = msg_data.get('task_id', '')[:8]
                error_msg = msg_data.get('result', 'Unknown error')
                print(f"❌ Task exception: {task_id}")
                # Keep the terminal output compact by showing only the key error line.
                if isinstance(error_msg, str):
                    error_lines = error_msg.split('\n')
                    if len(error_lines) > 0:
                        print(f"  Error: {error_lines[0][:200]}")
                else:
                    print(f"  Error: {str(error_msg)[:200]}")
            elif msg_type == "finish_workflow":
                print("=" * 60)
                if has_exception:
                    print("⚠️  Workflow completed with exceptions")
                else:
                    print("🎉 Workflow completed!")
                print("=" * 60)
        
        return {
            "task_results": task_results,
            "workflow_completed": workflow_completed,
            "has_exception": has_exception,
            "exception_tasks": exception_tasks
        }
    
    def get_task_result(self, run_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        """
        Get result of a specific task from a workflow run (from cache)
        
        Note: This method assumes get_results() or show_results() has been called
        for this run_id, as it only reads from the local cache.
        
        Args:
            run_id: Run ID returned by run() method
            task_id: Task ID (can be full UUID or short 8-char prefix)
            
        Returns:
            Dict: Task result data, or None if not found
                  Format: {
                      "task_id": "...",
                      "result": {...},
                      "status": "success" or "exception",
                      "error": "..." (if exception)
                  }
            
        Example:
            run_id = workflow.run()
            workflow.get_results(run_id)  # Fetch and cache results
            
            # Query specific task result
            task_result = workflow.get_task_result(run_id, task1.task_id)
            print(task_result["result"])
        """
        # Check if run_id has cached results
        if run_id not in self._results_cache:
            raise ValueError(
                f"No cached results for run_id: {run_id}. "
                f"Please call get_results() or show_results() first."
            )
        
        messages = self._results_cache[run_id]
        
        # Search for the task in messages
        # Support both full task_id and short prefix (8 chars)
        for msg in messages:
            msg_type = msg.get("type")
            msg_data = msg.get("data", {})
            msg_task_id = msg_data.get("task_id", "")
            
            # Match full ID or short prefix
            if msg_task_id == task_id or msg_task_id.startswith(task_id):
                if msg_type == "finish_task":
                    return {
                        "task_id": msg_task_id,
                        "result": msg_data.get("result"),
                        "status": "success"
                    }
                elif msg_type == "task_exception":
                    return {
                        "task_id": msg_task_id,
                        "result": None,
                        "status": "exception",
                        "error": msg_data.get("result", "Unknown error")
                    }
        
        # Task not found
        return None
    
    def list_cached_runs(self) -> List[str]:
        """
        List all run IDs that have cached results
        
        Returns:
            List[str]: List of run IDs with cached results
            
        Example:
            runs = workflow.list_cached_runs()
            for run_id in runs:
                print(f"Cached run: {run_id}")
        """
        return list(self._results_cache.keys())
    
    def clear_cache(self, run_id: Optional[str] = None) -> None:
        """
        Clear cached results
        
        Args:
            run_id: Optional run ID to clear. If None, clears all cached results.
            
        Example:
            # Clear specific run
            workflow.clear_cache(run_id)
            
            # Clear all cached results
            workflow.clear_cache()
        """
        if run_id is None:
            self._results_cache.clear()
        elif run_id in self._results_cache:
            del self._results_cache[run_id]
    
    def get_graph_mermaid(self) -> str:
        """
        Generate Mermaid diagram syntax for workflow visualization
        
        Returns:
            str: Mermaid diagram code
            
        Example:
            mermaid_code = workflow.get_graph_mermaid()
            print(mermaid_code)
            # Or save to file and render with Mermaid tools
        """
        lines = ["graph TD"]
        
        # Add nodes
        for task_id, node_info in self._nodes.items():
            task_short_id = task_id[:8]
            task_name = node_info["name"]
            func_name = node_info["func_name"]
            
            # Format: ID["Task Name\n(function)"]
            label = f"{task_name}"
            if func_name != task_name:
                label += f"\\n({func_name})"
            
            lines.append(f"    {task_short_id}[\"{label}\"]")
        
        # Add edges
        for source_id, target_id in self._edges:
            source_short = source_id[:8]
            target_short = target_id[:8]
            lines.append(f"    {source_short} --> {target_short}")
        
        return "\n".join(lines)
    
    def get_graph_ascii(self) -> str:
        """
        Generate ASCII art visualization of workflow
        
        Returns:
            str: ASCII representation of the workflow
            
        Example:
            workflow.print_graph()
        """
        if not self._nodes:
            return "Empty workflow (no tasks)"
        
        lines = []
        lines.append("=" * 60)
        lines.append("Workflow Structure")
        lines.append("=" * 60)
        
        # Find start nodes (nodes with no incoming edges)
        incoming = {node_id: [] for node_id in self._nodes}
        for source, target in self._edges:
            if target in incoming:
                incoming[target].append(source)
        
        start_nodes = [node_id for node_id, sources in incoming.items() if not sources]
        
        if not start_nodes:
            # If no clear start (might be cyclic), just list all
            start_nodes = list(self._nodes.keys())
        
        # Build tree representation
        def print_node(node_id, level=0, visited=None):
            if visited is None:
                visited = set()
            
            if node_id in visited:
                return []
            visited.add(node_id)
            
            node_info = self._nodes.get(node_id, {})
            name = node_info.get("name", "Unknown")
            func_name = node_info.get("func_name", "")
            
            indent = "  " * level
            node_lines = []
            
            if level == 0:
                node_lines.append(f"{indent}┌─ {name} ({func_name})")
            else:
                node_lines.append(f"{indent}└─ {name} ({func_name})")
            
            # Show inputs and outputs
            inputs_list = node_info.get("inputs", [])
            outputs_list = node_info.get("outputs", [])
            
            if inputs_list:
                node_lines.append(f"{indent}   📥 Inputs: {', '.join(inputs_list)}")
            if outputs_list:
                node_lines.append(f"{indent}   📤 Outputs: {', '.join(outputs_list)}")
            
            # Find children (tasks that depend on this task)
            children = [target for source, target in self._edges if source == node_id]
            
            for child in children:
                node_lines.append(f"{indent}   │")
                node_lines.extend(print_node(child, level + 1, visited))
            
            return node_lines
        
        # Print each start node
        for start_node in start_nodes:
            lines.extend(print_node(start_node))
            lines.append("")
        
        lines.append("=" * 60)
        lines.append(f"Total Tasks: {len(self._nodes)}")
        lines.append(f"Total Edges: {len(self._edges)}")
        lines.append("=" * 60)
        
        return "\n".join(lines)
    
    def print_graph(self) -> None:
        """
        Print ASCII visualization of workflow structure
        
        Example:
            workflow.print_graph()
        """
        print(self.get_graph_ascii())
    
    def get_graph_info(self) -> Dict[str, Any]:
        """
        Get detailed graph information
        
        Returns:
            Dict containing:
                - nodes: List of node information
                - edges: List of edges
                - stats: Statistics about the workflow
                
        Example:
            info = workflow.get_graph_info()
            print(f"Tasks: {len(info['nodes'])}")
            print(f"Dependencies: {len(info['edges'])}")
        """
        nodes_info = []
        for task_id, node_data in self._nodes.items():
            nodes_info.append({
                "task_id": task_id,
                "task_id_short": task_id[:8],
                "name": node_data["name"],
                "func_name": node_data["func_name"],
                "inputs": node_data["inputs"],
                "outputs": node_data["outputs"],
                "resources": node_data["resources"]
            })
        
        edges_info = []
        for source_id, target_id in self._edges:
            source_name = self._nodes.get(source_id, {}).get("name", "Unknown")
            target_name = self._nodes.get(target_id, {}).get("name", "Unknown")
            edges_info.append({
                "source_id": source_id,
                "source_id_short": source_id[:8],
                "source_name": source_name,
                "target_id": target_id,
                "target_id_short": target_id[:8],
                "target_name": target_name
            })
        
        # Calculate statistics
        stats = {
            "total_tasks": len(self._nodes),
            "total_edges": len(self._edges),
            "start_tasks": self._get_start_tasks(),
            "end_tasks": self._get_end_tasks()
        }
        
        return {
            "nodes": nodes_info,
            "edges": edges_info,
            "stats": stats
        }
    
    def _get_start_tasks(self) -> List[str]:
        """Get tasks with no incoming edges (start tasks)"""
        incoming = {node_id: [] for node_id in self._nodes}
        for source, target in self._edges:
            if target in incoming:
                incoming[target].append(source)
        
        start_tasks = [node_id for node_id, sources in incoming.items() if not sources]
        return [self._nodes[tid]["name"] for tid in start_tasks]
    
    def _get_end_tasks(self) -> List[str]:
        """Get tasks with no outgoing edges (end tasks)"""
        outgoing = {node_id: [] for node_id in self._nodes}
        for source, target in self._edges:
            if source in outgoing:
                outgoing[source].append(target)
        
        end_tasks = [node_id for node_id, targets in outgoing.items() if not targets]
        return [self._nodes[tid]["name"] for tid in end_tasks]
    
    def draw_graph(self, 
                   output_path: str = "workflow.png",
                   method: str = "auto",
                   figsize: tuple = (12, 8),
                   dpi: int = 300) -> str:
        """
        Draw workflow graph and save as image file
        
        Args:
            output_path: Output file path (supports .png, .pdf, .svg, etc.)
            method: Visualization method - "auto", "graphviz", or "matplotlib"
                   "auto" will try graphviz first, then matplotlib
            figsize: Figure size for matplotlib (width, height in inches)
            dpi: DPI for output image
            
        Returns:
            str: Path to the saved image file
            
        Example:
            # Using Graphviz (recommended)
            workflow.draw_graph("my_workflow.png")
            
            # Using Matplotlib
            workflow.draw_graph("my_workflow.png", method="matplotlib")
            
            # Custom size
            workflow.draw_graph("my_workflow.png", figsize=(16, 10), dpi=150)
            
        Note:
            Requires either:
            - graphviz (pip install graphviz) + Graphviz binary installation
            - matplotlib + networkx (pip install matplotlib networkx)
        """
        if not self._nodes:
            raise ValueError("Workflow is empty. Add tasks before drawing.")
        
        if method == "auto":
            # Try graphviz first
            try:
                return self._draw_with_graphviz(output_path, dpi)
            except (ImportError, Exception) as e:
                warnings.warn(f"Graphviz not available: {e}. Trying matplotlib...")
                try:
                    return self._draw_with_matplotlib(output_path, figsize, dpi)
                except ImportError as e2:
                    raise ImportError(
                        "Neither graphviz nor matplotlib is available. "
                        "Please install one of them:\n"
                        "  pip install graphviz  (also need system graphviz)\n"
                        "  pip install matplotlib networkx"
                    ) from e2
        elif method == "graphviz":
            return self._draw_with_graphviz(output_path, dpi)
        elif method == "matplotlib":
            return self._draw_with_matplotlib(output_path, figsize, dpi)
        else:
            raise ValueError(f"Unknown method: {method}. Use 'auto', 'graphviz', or 'matplotlib'")
    
    def _draw_with_graphviz(self, output_path: str, dpi: int = 300) -> str:
        """Draw graph using Graphviz"""
        try:
            import graphviz
        except ImportError:
            raise ImportError(
                "graphviz package not found. Install it with:\n"
                "  pip install graphviz\n"
                "Also ensure Graphviz is installed on your system:\n"
                "  - Ubuntu/Debian: sudo apt-get install graphviz\n"
                "  - macOS: brew install graphviz\n"
                "  - Windows: https://graphviz.org/download/"
            )
        
        # Determine format from file extension
        file_ext = output_path.split('.')[-1].lower()
        if file_ext not in ['png', 'pdf', 'svg', 'jpg', 'jpeg']:
            file_ext = 'png'
            output_path = output_path.rsplit('.', 1)[0] + '.png'
        
        # Create graph
        dot = graphviz.Digraph(comment='Workflow Graph')
        dot.attr(rankdir='TB', dpi=str(dpi))
        dot.attr('node', shape='box', style='rounded,filled', fillcolor='lightblue', 
                 fontname='Arial', fontsize='12')
        dot.attr('edge', color='gray40', arrowsize='0.8')
        
        # Add nodes
        for task_id, node_info in self._nodes.items():
            task_short_id = task_id[:8]
            task_name = node_info["name"]
            func_name = node_info["func_name"]
            inputs = node_info.get("inputs", [])
            outputs = node_info.get("outputs", [])
            resources = node_info.get("resources", {})
            
            # Build label with HTML-like formatting
            label_parts = [f"<B>{task_name}</B>"]
            if func_name != task_name:
                label_parts.append(f"<I>({func_name})</I>")
            
            if inputs:
                label_parts.append(f"<BR/>📥 {', '.join(inputs[:3])}")
                if len(inputs) > 3:
                    label_parts.append("...")
            
            if outputs:
                label_parts.append(f"<BR/>📤 {', '.join(outputs[:3])}")
                if len(outputs) > 3:
                    label_parts.append("...")
            
            # Add resource info if significant
            if resources.get('gpu', 0) > 0:
                label_parts.append(f"<BR/>🎮 GPU: {resources['gpu']}")
            if resources.get('cpu', 0) > 1:
                label_parts.append(f"<BR/>💻 CPU: {resources['cpu']}")
            
            label = '<' + '<BR/>'.join(label_parts) + '>'
            dot.node(task_short_id, label)
        
        # Add edges
        for source_id, target_id in self._edges:
            source_short = source_id[:8]
            target_short = target_id[:8]
            dot.edge(source_short, target_short)
        
        # Render
        output_base = output_path.rsplit('.', 1)[0]
        dot.render(output_base, format=file_ext, cleanup=True)
        
        return output_path
    
    def _draw_with_matplotlib(self, output_path: str, figsize: tuple, dpi: int) -> str:
        """Draw graph using Matplotlib and NetworkX"""
        try:
            import matplotlib.pyplot as plt
            import networkx as nx
        except ImportError:
            raise ImportError(
                "matplotlib or networkx not found. Install them with:\n"
                "  pip install matplotlib networkx"
            )
        
        # Create directed graph
        G = nx.DiGraph()
        
        # Add nodes with attributes
        for task_id, node_info in self._nodes.items():
            task_short_id = task_id[:8]
            G.add_node(task_short_id, **node_info)
        
        # Add edges
        for source_id, target_id in self._edges:
            source_short = source_id[:8]
            target_short = target_id[:8]
            G.add_edge(source_short, target_short)
        
        # Create figure
        fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
        
        # Use hierarchical layout for better visualization
        try:
            # Try to use hierarchical layout if possible
            pos = nx.spring_layout(G, k=2, iterations=50, seed=42)
        except:
            pos = nx.spring_layout(G, seed=42)
        
        # Draw nodes
        node_colors = []
        for node_id in G.nodes():
            node_data = G.nodes[node_id]
            resources = node_data.get('resources', {})
            # Color by resource usage
            if resources.get('gpu', 0) > 0:
                node_colors.append('#FFD700')  # Gold for GPU tasks
            elif resources.get('cpu', 1) > 2:
                node_colors.append('#87CEEB')  # Sky blue for CPU-intensive
            else:
                node_colors.append('#98FB98')  # Pale green for normal tasks
        
        nx.draw_networkx_nodes(G, pos, node_color=node_colors, 
                               node_size=3000, alpha=0.9, ax=ax)
        
        # Draw edges
        nx.draw_networkx_edges(G, pos, edge_color='gray', 
                               arrows=True, arrowsize=20, 
                               arrowstyle='->', width=2, 
                               connectionstyle='arc3,rad=0.1', ax=ax)
        
        # Draw labels
        labels = {}
        for node_id in G.nodes():
            node_data = G.nodes[node_id]
            name = node_data.get('name', node_id)
            func_name = node_data.get('func_name', '')
            
            if name != func_name and func_name:
                labels[node_id] = f"{name}\n({func_name})"
            else:
                labels[node_id] = name
        
        nx.draw_networkx_labels(G, pos, labels, font_size=10, 
                                font_weight='bold', ax=ax)
        
        # Set title and remove axis
        ax.set_title(f"Workflow Graph\n({len(G.nodes())} tasks, {len(G.edges())} edges)",
                     fontsize=14, fontweight='bold', pad=20)
        ax.axis('off')
        
        # Add legend
        from matplotlib.patches import Patch
        legend_elements = [
            Patch(facecolor='#FFD700', label='GPU Task'),
            Patch(facecolor='#87CEEB', label='CPU-Intensive Task'),
            Patch(facecolor='#98FB98', label='Normal Task')
        ]
        ax.legend(handles=legend_elements, loc='upper right', fontsize=10)
        
        # Save figure
        plt.tight_layout()
        plt.savefig(output_path, dpi=dpi, bbox_inches='tight', 
                   facecolor='white', edgecolor='none')
        plt.close()
        
        return output_path
    
    def __repr__(self) -> str:
        return f"MaWorkflow(id='{self.workflow_id[:8]}...', tasks={len(self._tasks)})"
