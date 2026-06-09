import requests
import time
from pathlib import Path
from typing import Callable, Optional
from maze.client.maze.agent import AgentPlanner, AgentRun
from maze.client.maze.agent_mcp import close_mcp_manager_blocking, discover_mcp_tools_blocking
from maze.client.maze.agent_permissions import AgentPermissionPolicy
from maze.client.maze.agent_skills import create_skill_registry
from maze.client.maze.dynamic import DynamicRun
from maze.client.maze.react import ReActWorkflow
from maze.client.maze.skills import SkillSpec
from maze.client.maze.workflow import MaWorkflow
from maze.client.maze.workflow_authoring import WorkflowDefinition
from maze.core.application.spec import app_spec_from_payload, load_app_spec_file


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

    def create_workflow_from(
        self,
        workflow_def: WorkflowDefinition,
        inputs: Optional[dict] = None,
    ) -> MaWorkflow:
        """
        Create a static workflow from a @workflow definition.

        The decorated Python function is executed in graph-building mode, so
        each @task call becomes a Maze task node instead of running locally.
        """
        if not hasattr(workflow_def, "build"):
            raise TypeError("create_workflow_from expects a @workflow definition")

        workflow = self.create_workflow()
        workflow_def.build(workflow, inputs=inputs or {})
        return workflow

    def create_dynamic_run(
        self,
        max_tasks: int = 100,
        timeout_seconds: Optional[int] = None,
        file_context: Optional[dict] = None,
        workspace_dir: Optional[str] = None,
        artifact_mode: bool = False,
    ) -> DynamicRun:
        """
        Create a dynamic workflow run.

        Dynamic runs grow through append_task and finish only after finalize().
        """
        url = f"{self.server_url}/dynamic_runs"
        prepared_file_context = self._build_file_context(
            file_context=file_context,
            workspace_dir=workspace_dir,
            artifact_mode=artifact_mode,
        )
        payload = {
            "max_tasks": max_tasks,
            "timeout_seconds": timeout_seconds,
        }
        if prepared_file_context:
            payload["file_context"] = prepared_file_context
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
        file_context: Optional[dict] = None,
        workspace_dir: Optional[str] = None,
        artifact_mode: bool = False,
        mcp_clients: Optional[list] = None,
        mcp_servers: Optional[list[dict]] = None,
        skills: Optional[list[str]] = None,
        skill_dirs: Optional[list[str]] = None,
        max_skill_chars: int = 12000,
        permission_policy: AgentPermissionPolicy | dict | None = None,
    ) -> AgentRun:
        """
        Create a minimal agent runtime backed by a DynamicRun.

        The planner returns either {"tool": name, "args": {...}} to execute a
        registered @task tool, or {"final": value} to finish the run.
        """
        dynamic_run = self.create_dynamic_run(
            max_tasks=max_steps,
            timeout_seconds=timeout_seconds,
            file_context=file_context,
            workspace_dir=workspace_dir,
            artifact_mode=artifact_mode,
        )
        mcp_manager = None
        skill_registry = None
        cancel_reason = "Agent creation failed"
        try:
            try:
                skill_registry = create_skill_registry(
                    skills=skills,
                    skill_dirs=skill_dirs,
                    max_instruction_chars=max_skill_chars,
                )
            except Exception as exc:
                cancel_reason = "Skill loading failed"
                self._emit_dynamic_run_event_best_effort(
                    dynamic_run,
                    "agent_skill_load_failed",
                    self._error_event_payload(exc),
                )
                raise
            try:
                mcp_manager, mcp_tools = discover_mcp_tools_blocking(
                    clients=mcp_clients,
                    configs=mcp_servers,
                )
            except Exception as exc:
                cancel_reason = "MCP discovery failed"
                self._emit_dynamic_run_event_best_effort(
                    dynamic_run,
                    "agent_mcp_discovery_failed",
                    self._error_event_payload(exc),
                )
                raise
            return AgentRun(
                dynamic_run=dynamic_run,
                tools=tools,
                planner=planner,
                max_steps=max_steps,
                task_timeout=task_timeout,
                mcp_manager=mcp_manager,
                mcp_tools=mcp_tools,
                skill_registry=skill_registry,
                permission_policy=permission_policy,
            )
        except Exception:
            close_mcp_manager_blocking(mcp_manager)
            self._cancel_dynamic_run_best_effort(dynamic_run, cancel_reason)
            raise

    def create_react_workflow(
        self,
        llm_task: Callable,
        tools: list[Callable],
        max_steps: int = 10,
        system_prompt: Optional[str] = None,
        skills: Optional[list[SkillSpec | str]] = None,
        progressive_skills: bool = True,
        skill_reader_max_chars: int = 12000,
        timeout_seconds: Optional[int] = None,
        task_timeout: Optional[float] = None,
        file_context: Optional[dict] = None,
        workspace_dir: Optional[str] = None,
        artifact_mode: bool = False,
        mcp_clients: Optional[list] = None,
        mcp_servers: Optional[list[dict]] = None,
        agent_skills: Optional[list[str]] = None,
        skill_dirs: Optional[list[str]] = None,
        max_skill_chars: int = 12000,
        permission_policy: AgentPermissionPolicy | dict | None = None,
    ) -> ReActWorkflow:
        """
        Create a ReAct workflow template backed by a DynamicRun.

        Both the decision node and selected tools execute as Maze tasks.
        Skills are Claude/Cursor-style instruction packages. They teach the
        ReAct controller how to use already-registered tools; they do not add
        executable tools except for the optional read_skill_file helper used
        for progressive disclosure.
        """
        dynamic_run = self.create_dynamic_run(
            max_tasks=max_steps * 2,
            timeout_seconds=timeout_seconds,
            file_context=file_context,
            workspace_dir=workspace_dir,
            artifact_mode=artifact_mode,
        )
        react_skills = skills
        registry_skill_names = agent_skills
        if registry_skill_names is None and skill_dirs and skills:
            string_skills = [item for item in skills if isinstance(item, str)]
            if len(string_skills) == len(skills) and not any(Path(item).expanduser().exists() for item in string_skills):
                registry_skill_names = [str(item) for item in string_skills]
                react_skills = None
        mcp_manager = None
        skill_registry = None
        cancel_reason = "ReAct workflow creation failed"
        try:
            try:
                skill_registry = create_skill_registry(
                    skills=registry_skill_names,
                    skill_dirs=skill_dirs,
                    max_instruction_chars=max_skill_chars,
                )
            except Exception as exc:
                cancel_reason = "Skill loading failed"
                self._emit_dynamic_run_event_best_effort(
                    dynamic_run,
                    "agent_skill_load_failed",
                    self._error_event_payload(exc),
                )
                raise
            try:
                mcp_manager, mcp_tools = discover_mcp_tools_blocking(
                    clients=mcp_clients,
                    configs=mcp_servers,
                )
            except Exception as exc:
                cancel_reason = "MCP discovery failed"
                self._emit_dynamic_run_event_best_effort(
                    dynamic_run,
                    "agent_mcp_discovery_failed",
                    self._error_event_payload(exc),
                )
                raise
            return ReActWorkflow(
                dynamic_run=dynamic_run,
                llm_task=llm_task,
                tools=tools,
                max_steps=max_steps,
                system_prompt=system_prompt,
                skills=react_skills,
                progressive_skills=progressive_skills,
                skill_reader_max_chars=skill_reader_max_chars,
                task_timeout=task_timeout,
                mcp_manager=mcp_manager,
                mcp_tools=mcp_tools,
                skill_registry=skill_registry,
                permission_policy=permission_policy,
            )
        except Exception:
            close_mcp_manager_blocking(mcp_manager)
            self._cancel_dynamic_run_best_effort(dynamic_run, cancel_reason)
            raise

    def get_dynamic_run(self, run_id: str) -> DynamicRun:
        return DynamicRun(run_id, self.server_url)

    def run_app(
        self,
        spec: dict | str | Path,
        *,
        workspace_dir: Optional[str] = None,
        artifact_mode: bool = True,
        timeout_seconds: Optional[float] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        """
        Submit a Maze AppSpec/RunSpec and return the created run payload.
        """
        source_path = None
        if isinstance(spec, (str, Path)) and Path(spec).expanduser().exists():
            source_path = str(Path(spec).expanduser().resolve())
            app_spec = load_app_spec_file(source_path)
        elif isinstance(spec, dict):
            app_spec = app_spec_from_payload(spec)
        else:
            raise TypeError("spec must be a dict or an existing app spec file path")

        payload = {
            "spec": app_spec,
            "source_path": source_path,
            "workspace_dir": workspace_dir,
            "artifact_mode": artifact_mode,
            "timeout_seconds": timeout_seconds,
            "tags": tags,
            "metadata": metadata,
        }
        response = requests.post(f"{self.server_url}/apps/run", json=payload)
        if response.status_code != 200:
            raise Exception(f"Failed to run app: {response.status_code}, {response.text}")
        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to run app: {data.get('message', 'Unknown error')}")
        return data

    def validate_workflow_spec(self, spec: dict) -> dict:
        """
        Validate an external DAG workflow submit spec without running it.
        """
        response = requests.post(f"{self.server_url}/workflows/validate", json={"spec": spec})
        if response.status_code != 200:
            raise Exception(f"Failed to validate workflow spec: {response.status_code}, {response.text}")
        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to validate workflow spec: {data.get('message', 'Unknown error')}")
        return data.get("spec", {})

    def submit_workflow(
        self,
        spec: dict,
        *,
        artifact_mode: bool = True,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        """
        Submit an external DAG workflow spec and return workflow_id/run_id.

        This is the stable API intended for decoupled visual DAG builders.
        """
        payload = {
            "spec": spec,
            "artifact_mode": artifact_mode,
            "tags": tags,
            "metadata": metadata,
        }
        response = requests.post(f"{self.server_url}/workflows/submit", json=payload)
        if response.status_code != 200:
            raise Exception(f"Failed to submit workflow spec: {response.status_code}, {response.text}")
        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to submit workflow spec: {data.get('message', 'Unknown error')}")
        return data

    def _build_file_context(
        self,
        file_context: Optional[dict] = None,
        workspace_dir: Optional[str] = None,
        artifact_mode: bool = False,
    ) -> Optional[dict]:
        if file_context is not None:
            context = dict(file_context)
        elif workspace_dir is not None:
            context = {
                "enabled": True,
                "workspace_dir": workspace_dir,
            }
        else:
            return None

        context["enabled"] = context.get("enabled", True)
        if workspace_dir is not None:
            context["workspace_dir"] = workspace_dir
        if artifact_mode and not context.get("artifact_store"):
            context["artifact_store"] = {
                "type": "head_http",
                "base_url": self.server_url,
            }
        return context

    def _error_event_payload(self, exc: Exception) -> dict:
        return {
            "error": {
                "error_type": type(exc).__name__,
                "message": str(exc),
                "repairable": False,
            }
        }

    def _emit_dynamic_run_event_best_effort(self, dynamic_run: DynamicRun, event_type: str, data: dict) -> None:
        try:
            dynamic_run.emit_event(event_type, data)
        except Exception:
            pass

    def _cancel_dynamic_run_best_effort(self, dynamic_run: DynamicRun, reason: str) -> None:
        try:
            status = dynamic_run.get_status().get("status")
            if status not in {"finalized", "failed", "canceled", "timed_out", "interrupted"}:
                dynamic_run.cancel(reason)
        except Exception:
            pass

    def list_dynamic_runs(
        self,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        detail: bool = False,
    ) -> list[dict]:
        params = {}
        if status:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit
        if detail:
            params["detail"] = "true"

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

    def list_runs(
        self,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: Optional[int] = None,
        detail: bool = False,
    ) -> list[dict]:
        params = {}
        if status:
            params["status"] = status
        if kind:
            params["kind"] = kind
        if limit is not None:
            params["limit"] = limit
        if detail:
            params["detail"] = "true"

        response = requests.get(f"{self.server_url}/runs", params=params or None)
        if response.status_code != 200:
            raise Exception(f"Failed to list runs: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to list runs: {data.get('message', 'Unknown error')}")
        return data.get("runs", [])

    def get_run(self, run_id: str) -> dict:
        response = requests.get(f"{self.server_url}/runs/{run_id}")
        if response.status_code != 200:
            raise Exception(f"Failed to get run: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to get run: {data.get('message', 'Unknown error')}")
        return data.get("run", {})

    def get_run_tasks(self, run_id: str) -> list[dict]:
        response = requests.get(f"{self.server_url}/runs/{run_id}/tasks")
        if response.status_code != 200:
            raise Exception(f"Failed to get run tasks: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to get run tasks: {data.get('message', 'Unknown error')}")
        return data.get("tasks", [])

    def get_run_task(self, run_id: str, task_id: str) -> dict:
        response = requests.get(f"{self.server_url}/runs/{run_id}/tasks/{task_id}")
        if response.status_code != 200:
            raise Exception(f"Failed to get run task: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to get run task: {data.get('message', 'Unknown error')}")
        return data.get("task", {})

    def get_run_artifacts(self, run_id: str) -> list[dict]:
        response = requests.get(f"{self.server_url}/runs/{run_id}/artifacts")
        if response.status_code != 200:
            raise Exception(f"Failed to get run artifacts: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to get run artifacts: {data.get('message', 'Unknown error')}")
        return data.get("artifacts", [])

    def get_run_task_artifacts(self, run_id: str, task_id: str) -> list[dict]:
        response = requests.get(f"{self.server_url}/runs/{run_id}/tasks/{task_id}/artifacts")
        if response.status_code != 200:
            raise Exception(f"Failed to get task artifacts: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to get task artifacts: {data.get('message', 'Unknown error')}")
        return data.get("artifacts", [])

    def get_run_events(self, run_id: str, after: Optional[int] = None) -> list[dict]:
        params = {"after": after} if after is not None else None
        response = requests.get(f"{self.server_url}/runs/{run_id}/events", params=params)
        if response.status_code != 200:
            raise Exception(f"Failed to get run events: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to get run events: {data.get('message', 'Unknown error')}")
        return data.get("events", [])

    def get_run_logs(
        self,
        run_id: str,
        tail: Optional[int] = 500,
        task_id: Optional[str] = None,
    ) -> dict:
        params = {}
        if tail is not None:
            params["tail"] = tail
        if task_id:
            params["task_id"] = task_id
        response = requests.get(f"{self.server_url}/runs/{run_id}/logs", params=params or None)
        if response.status_code != 200:
            raise Exception(f"Failed to get run logs: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to get run logs: {data.get('message', 'Unknown error')}")
        return {
            "run_id": data.get("run_id", run_id),
            "task_id": data.get("task_id"),
            "line_count": data.get("line_count", 0),
            "lines": data.get("lines", []),
        }

    def cancel_run(self, run_id: str, reason: Optional[str] = None) -> dict:
        response = requests.post(
            f"{self.server_url}/runs/{run_id}/cancel",
            json={"reason": reason},
        )
        if response.status_code != 200:
            raise Exception(f"Failed to cancel run: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to cancel run: {data.get('message', 'Unknown error')}")
        return data

    def retry_run(
        self,
        run_id: str,
        *,
        workspace_dir: Optional[str] = None,
        artifact_mode: bool = True,
        timeout_seconds: Optional[float] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        response = requests.post(
            f"{self.server_url}/runs/{run_id}/retry",
            json={
                "workspace_dir": workspace_dir,
                "artifact_mode": artifact_mode,
                "timeout_seconds": timeout_seconds,
                "tags": tags,
                "metadata": metadata,
            },
        )
        if response.status_code != 200:
            raise Exception(f"Failed to retry run: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to retry run: {data.get('message', 'Unknown error')}")
        return data

    def wait_run(
        self,
        run_id: str,
        timeout: Optional[float] = None,
        poll_interval: float = 0.5,
    ) -> dict:
        terminal_statuses = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
        deadline = None if timeout is None else time.time() + timeout
        while True:
            run = self.get_run(run_id)
            if run.get("status") in terminal_statuses:
                return run
            if deadline is not None and time.time() >= deadline:
                raise TimeoutError(f"Timed out waiting for run: {run_id}")
            time.sleep(poll_interval)

    def stream_run(self, run_id: str, poll_interval: float = 0.2):
        after = None
        terminal_event_types = {
            "finish_workflow",
            "task_exception",
            "cancel_workflow",
            "interrupt_workflow",
            "cancel_dynamic_run",
            "timeout_dynamic_run",
            "interrupt_dynamic_run",
        }
        while True:
            events = self.get_run_events(run_id, after=after)
            for event in events:
                after = max(after or 0, int(event.get("seq", 0)))
                yield event
                if event.get("type") in terminal_event_types:
                    return
            if self.get_run(run_id).get("status") in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
                return
            time.sleep(poll_interval)
    
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

    def get_cluster_resources(self) -> dict:
        """
        Get Maze scheduler's registered cluster resources.
        """
        url = f"{self.server_url}/cluster/resources"
        response = requests.get(url)

        if response.status_code != 200:
            raise Exception(f"Failed to get cluster resources, status code: {response.status_code}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to get cluster resources: {data.get('message', 'Unknown error')}")
        return data.get("cluster", {})

    def get_cluster_queues(self) -> dict:
        """
        Get scheduler queue and running task diagnostics.
        """
        url = f"{self.server_url}/cluster/queues"
        response = requests.get(url)

        if response.status_code != 200:
            raise Exception(f"Failed to get cluster queues, status code: {response.status_code}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to get cluster queues: {data.get('message', 'Unknown error')}")
        return data.get("queues", {})

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

        
