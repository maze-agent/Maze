import requests
from typing import Callable, Optional
from maze.client.maze.agent import AgentPlanner, AgentRun
from maze.client.maze.dynamic import DynamicRun
from maze.client.maze.react import ReActWorkflow
from maze.client.maze.workflow import MaWorkflow


class MaClient:
    """
    Maze client for connecting to Maze server and managing workflows
    
    Example:
        client = MaClient("http://localhost:8000")
        workflow = client.create_workflow()
    """
    
    def __init__(self, server_url: str = "http://localhost:8000"):
        """
        Initialize Maze client
        
        Args:
            server_url: Maze server address, defaults to http://localhost:8000
        """
        self.llm_instance = {}
        self.server_url = server_url.rstrip('/')
        
    def create_workflow(self) -> MaWorkflow:
        """
        Create a new workflow
        
        Returns:
            MaWorkflow: Workflow object
            
        Raises:
            Exception: If creation fails
        """
        url = f"{self.server_url}/create_workflow"
        response = requests.post(url)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "success":
                workflow_id = data["workflow_id"]
                return MaWorkflow(workflow_id, self.server_url)
            else:
                raise Exception(f"Failed to create workflow: {data.get('message', 'Unknown error')}")
        else:
            raise Exception(f"Request failed, status code: {response.status_code}, response: {response.text}")

    def create_dynamic_run(
        self,
        max_tasks: int = 100,
        timeout_seconds: Optional[int] = None,
    ) -> DynamicRun:
        """
        Create a dynamic workflow run.

        Dynamic runs grow through append_task and finish only after finalize().
        """
        url = f"{self.server_url}/dynamic_runs"
        payload = {
            "max_tasks": max_tasks,
            "timeout_seconds": timeout_seconds,
        }
        response = requests.post(url, json=payload)

        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "success":
                return DynamicRun(data["run_id"], self.server_url)
            raise Exception(f"Failed to create dynamic run: {data.get('message', 'Unknown error')}")

        raise Exception(f"Request failed, status code: {response.status_code}, response: {response.text}")

    def create_agent_run(
        self,
        tools: list[Callable],
        planner: AgentPlanner,
        max_steps: int = 10,
        timeout_seconds: Optional[int] = None,
        task_timeout: Optional[float] = None,
    ) -> AgentRun:
        """
        Create a minimal agent runtime backed by a DynamicRun.

        The planner returns either {"tool": name, "args": {...}} to execute a
        registered @task tool, or {"final": value} to finish the run.
        """
        dynamic_run = self.create_dynamic_run(
            max_tasks=max_steps,
            timeout_seconds=timeout_seconds,
        )
        return AgentRun(
            dynamic_run=dynamic_run,
            tools=tools,
            planner=planner,
            max_steps=max_steps,
            task_timeout=task_timeout,
        )

    def create_react_workflow(
        self,
        llm_task: Callable,
        tools: list[Callable],
        max_steps: int = 10,
        system_prompt: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        task_timeout: Optional[float] = None,
    ) -> ReActWorkflow:
        """
        Create a ReAct workflow template backed by a DynamicRun.

        Both the decision node and selected tools execute as Maze tasks.
        """
        dynamic_run = self.create_dynamic_run(
            max_tasks=max_steps * 2,
            timeout_seconds=timeout_seconds,
        )
        return ReActWorkflow(
            dynamic_run=dynamic_run,
            llm_task=llm_task,
            tools=tools,
            max_steps=max_steps,
            system_prompt=system_prompt,
            task_timeout=task_timeout,
        )

    def get_dynamic_run(self, run_id: str) -> DynamicRun:
        return DynamicRun(run_id, self.server_url)

    def list_dynamic_runs(
        self,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        params = {}
        if status:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit

        response = requests.get(f"{self.server_url}/dynamic_runs", params=params or None)
        if response.status_code != 200:
            raise Exception(f"Failed to list dynamic runs: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to list dynamic runs: {data.get('message', 'Unknown error')}")
        return data.get("runs", [])

    def delete_dynamic_run(self, run_id: str) -> dict:
        response = requests.delete(f"{self.server_url}/dynamic_runs/{run_id}")
        if response.status_code != 200:
            raise Exception(f"Failed to delete dynamic run: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to delete dynamic run: {data.get('message', 'Unknown error')}")
        return data

    def cleanup_dynamic_runs(
        self,
        statuses: Optional[list[str]] = None,
        older_than_days: int | float | None = None,
        dry_run: bool = True,
    ) -> dict:
        response = requests.post(
            f"{self.server_url}/dynamic_runs/cleanup",
            json={
                "statuses": statuses,
                "older_than_days": older_than_days,
                "dry_run": dry_run,
            },
        )
        if response.status_code != 200:
            raise Exception(f"Failed to cleanup dynamic runs: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to cleanup dynamic runs: {data.get('message', 'Unknown error')}")
        return data.get("cleanup", {})
    
    def get_workflow(self, workflow_id: str) -> MaWorkflow:
        """
        Get existing workflow object
        
        Args:
            workflow_id: Workflow ID
            
        Returns:
            MaWorkflow: Workflow object
        """
        return MaWorkflow(workflow_id, self.server_url)
    
    def get_ray_head_port(self) -> dict:
        """
        Get Ray head node port (for worker connection)
        
        Returns:
            dict: Dictionary containing port information
        """
        url = f"{self.server_url}/get_head_ray_port"
        response = requests.post(url)
        
        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"Failed to get Ray port, status code: {response.status_code}")

    def start_llm_instance(self, model: str):
        """
        Start LLM instance
        """
        url = f"{self.server_url}/start_llm_instance"
        payload = {
            "model": model,
            "cpu_nums": 5,
            "memory": 1024,
            "gpu_nums": 1,
        }

        response = requests.post(url, json=payload)

        if response.status_code == 200:
            data = response.json()
            host = data['host']
            port = data['port']
            instance_id = data['instance_id']
            self.llm_instance[instance_id] = {"model": model, "host": host, "port": port}
            return instance_id
        else:
            raise Exception(f"Failed to start LLM instance, status code: {response.status_code}")

    def stop_llm_instance(self, instance_id: str):
        """
        Stop LLM instance
        """
        url = f"{self.server_url}/stop_llm_instance"
        response = requests.post(url, json={"instance_id": instance_id})

        if response.status_code == 200:
            del self.llm_instance[instance_id]
            return response.json()
        else:
            raise Exception(f"Failed to stop LLM instance, status code: {response.status_code}")

    def query_llm_instance(self, query: str, instance_id: str):
        """
        Query LLM instance status
        """
        from openai import OpenAI

        openai_api_key = "EMPTY"
        openai_api_base = "http://"+self.llm_instance[instance_id]["host"] + ":" + str(self.llm_instance[instance_id]["port"]) +"/v1"
        client = OpenAI(
            api_key=openai_api_key,
            base_url=openai_api_base,
        )
        completion = client.completions.create(
            model=self.llm_instance[instance_id]["model"],
            prompt=query,
        )
        return completion.choices[0].text

        
