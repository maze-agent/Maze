import uuid
from typing import Dict, Any, Optional, Callable, List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from maze.client.front.task import TaskOutput
from maze.client.front.decorator import get_task_metadata


class TaskDefinition:
    def __init__(self, task_func: Callable, inputs: Dict[str, Any], task_name: str = None):
        self.task_func = task_func
        self.inputs = inputs
        self.task_name = task_name or task_func.__name__
        self.metadata = get_task_metadata(task_func)
        self.task_id = None
        self.outputs_ref = None
    
    @property
    def outputs(self):
        return self.outputs_ref


class EdgeDefinition:
    def __init__(self, source_task_def: TaskDefinition, target_task_def: TaskDefinition):
        self.source_task_def = source_task_def
        self.target_task_def = target_task_def


class TaskOutputReference:
    def __init__(self, task_def: 'TaskDefinition', output_key: str):
        self.task_def = task_def
        self.output_key = output_key


class ServerWorkflow:
    def __init__(self, name: str, server_url: str, agent_port: int):
        self.name = name
        self.server_url = server_url.rstrip('/')
        self.agent_port = agent_port
        
        self.task_definitions: List[TaskDefinition] = []
        self.edge_definitions: List[EdgeDefinition] = []
        self.user_input_keys = []
        
        self.run_results = {}
        self.app = None
        
    def add_task(self, 
                 task_func: Callable,
                 inputs: Dict[str, Any] = None,
                 task_name: str = None) -> TaskDefinition:
        if inputs is None:
            inputs = {}
        
        for key, value in inputs.items():
            if value is None:
                if key not in self.user_input_keys:
                    self.user_input_keys.append(key)
        
        task_def = TaskDefinition(task_func, inputs, task_name)
        self.task_definitions.append(task_def)
        
        metadata = task_def.metadata
        class TaskOutputsPlaceholder:
            def __init__(self, task_def, output_keys):
                self._task_def = task_def
                self._outputs = {}
                for key in output_keys:
                    self._outputs[key] = TaskOutputReference(task_def, key)
            
            def __getitem__(self, key):
                return self._outputs[key]
            
            def keys(self):
                return self._outputs.keys()
        
        task_def.outputs_ref = TaskOutputsPlaceholder(task_def, metadata.outputs)
        
        return task_def
    
    def add_edge(self, source_task: TaskDefinition, target_task: TaskDefinition) -> None:
        edge_def = EdgeDefinition(source_task, target_task)
        self.edge_definitions.append(edge_def)
    
    def _create_workflow_instance(self, user_inputs: Dict[str, Any], run_id: str = None):
        import requests
        from maze.client.workflow import MaWorkflow
        from maze.client.file_utils import FileInput, is_file_type
        
        url = f"{self.server_url}/create_workflow"
        agent_metadata = {
            "run_id": run_id,
            "user_inputs": list(user_inputs.keys())
        }
        
        response = requests.post(url, json={
            "agent_name": self.name,
            "agent_metadata": agent_metadata
        })
        
        if response.status_code != 200:
            raise Exception(f"Failed to create workflow: {response.status_code}")
        
        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to create workflow: {data.get('message')}")
        
        workflow_id = data["workflow_id"]
        workflow = MaWorkflow(workflow_id, self.server_url)
        
        task_def_to_task = {}
        
        for task_def in self.task_definitions:
            actual_inputs = {}
            for key, value in task_def.inputs.items():
                if value is None:
                    actual_inputs[key] = user_inputs.get(key)
                elif isinstance(value, TaskOutputReference):
                    source_task = task_def_to_task[value.task_def]
                    actual_inputs[key] = source_task.outputs[value.output_key]
                else:
                    actual_inputs[key] = value
            
            task = workflow.add_task(task_def.task_func, inputs=actual_inputs)
            task_def_to_task[task_def] = task
        
        for edge_def in self.edge_definitions:
            source_task = task_def_to_task[edge_def.source_task_def]
            target_task = task_def_to_task[edge_def.target_task_def]
            workflow.add_edge(source_task, target_task)
        
        return workflow
    
    def set_user_inputs_and_run(self, user_inputs: Dict[str, Any], 
                                output_dir: str = None,
                                verbose: bool = False) -> Dict[str, Any]:
        run_id = str(uuid.uuid4())
        
        try:
            workflow = self._create_workflow_instance(user_inputs, run_id)
            
            if output_dir is None:
                output_dir = f"agent_results/{self.name}"
            
            workflow.run()
            result = workflow.get_results(verbose=verbose, output_dir=output_dir)
            
            workflow.cleanup()
            
            self.run_results[run_id] = result
            
            return {
                "run_id": run_id,
                "result": result
            }
        
        except Exception as e:
            error_result = {
                "error": str(e),
                "status": "failed"
            }
            self.run_results[run_id] = error_result
            raise
    
    def get_run_result(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self.run_results.get(run_id)
    
    def _register_agent(self):
        import requests
        
        try:
            url = f"{self.server_url}/register_agent"
            agent_info = {
                "port": self.agent_port,
                "user_input_keys": self.user_input_keys,
                "total_tasks": len(self.task_definitions),
                "total_edges": len(self.edge_definitions)
            }
            
            response = requests.post(url, json={
                "agent_name": self.name,
                "agent_info": agent_info
            })
            
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "success":
                    print(f"✅ Agent '{self.name}' registered to server")
                else:
                    print(f"⚠️  Agent registration failed: {data.get('message')}")
            else:
                print(f"⚠️  Agent registration request failed: {response.status_code}")
        except Exception as e:
            print(f"⚠️  Agent registration exception (but service startup is not affected): {e}")
    
    def deploy(self, host: str = "0.0.0.0", **kwargs):
        self._register_agent()
        
        self.app = FastAPI(title=f"{self.name} Agent")
        
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        
        @self.app.post(f"/{self.name}/run")
        async def run_workflow(user_inputs: Dict[str, Any]):
            try:
                result = self.set_user_inputs_and_run(user_inputs, verbose=False)
                return result
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.get(f"/{self.name}/result/{{run_id}}")
        async def get_result(run_id: str):
            result = self.get_run_result(run_id)
            if result is None:
                raise HTTPException(status_code=404, detail="Run ID not found")
            return {"result": result}
        
        @self.app.get(f"/{self.name}/info")
        async def get_info():
            return {
                "name": self.name,
                "user_input_keys": self.user_input_keys,
                "total_tasks": len(self.task_definitions),
                "total_runs": len(self.run_results)
            }
        
        print(f"🚀 Deploying {self.name} Agent service...")
        print(f"   Address: http://{host}:{self.agent_port}")
        print(f"   Run endpoint: POST http://{host}:{self.agent_port}/{self.name}/run")
        print(f"   Result endpoint: GET http://{host}:{self.agent_port}/{self.name}/result/{{run_id}}")
        print(f"   Info endpoint: GET http://{host}:{self.agent_port}/{self.name}/info")
        
        uvicorn.run(self.app, host=host, port=self.agent_port, **kwargs)
    
    def __repr__(self) -> str:
        return f"ServerWorkflow(name='{self.name}', tasks={len(self.task_definitions)}, runs={len(self.run_results)})"
