"""
ServerWorkflow - 可作为Agent服务持续运行的工作流

核心思想：
- 本地保存workflow定义模板（不提交到服务端）
- 每次收到请求时，用实际用户输入创建新的workflow实例
- 每次运行都是独立的workflow_id和run_id
"""

import uuid
from typing import Dict, Any, Optional, Callable, List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from maze.client.front.task import TaskOutput
from maze.client.front.decorator import get_task_metadata


class TaskDefinition:
    """任务定义（模板）"""
    def __init__(self, task_func: Callable, inputs: Dict[str, Any], task_name: str = None):
        self.task_func = task_func
        self.inputs = inputs  # 可能包含None（占位符）或TaskOutput引用
        self.task_name = task_name or task_func.__name__
        self.metadata = get_task_metadata(task_func)
        self.task_id = None  # 运行时分配
        self.outputs_ref = None  # 运行时创建
    
    @property
    def outputs(self):
        """返回输出引用对象"""
        return self.outputs_ref


class EdgeDefinition:
    """边定义（模板）"""
    def __init__(self, source_task_def: TaskDefinition, target_task_def: TaskDefinition):
        self.source_task_def = source_task_def
        self.target_task_def = target_task_def


class TaskOutputReference:
    """任务输出引用（用于ServerWorkflow）"""
    def __init__(self, task_def: 'TaskDefinition', output_key: str):
        self.task_def = task_def
        self.output_key = output_key


class ServerWorkflow:
    """
    服务工作流 - 可作为Agent持续运行
    
    核心思想：
    - 本地保存workflow定义模板
    - 每次请求时创建新的workflow实例提交到服务端
    - 每次运行独立，互不干扰
    
    示例:
        workflow = ServerWorkflow(name="health_agent", server_url="...", agent_port=8001)
        
        task1 = workflow.add_task(func1, inputs={"user_input": None})
        task2 = workflow.add_task(func2, inputs={"data": task1.outputs["result"]})
        
        workflow.deploy()  # 启动HTTP服务
    """
    
    def __init__(self, name: str, server_url: str, agent_port: int):
        """
        初始化服务工作流
        
        Args:
            name: 工作流名称（用于API路径）
            server_url: Maze服务器地址
            agent_port: Agent服务端口
        """
        self.name = name
        self.server_url = server_url.rstrip('/')
        self.agent_port = agent_port
        
        # 本地保存的workflow模板定义
        self.task_definitions: List[TaskDefinition] = []
        self.edge_definitions: List[EdgeDefinition] = []
        self.user_input_keys = []  # 需要用户输入的参数名
        
        # 运行记录
        self.run_results = {}  # run_id -> result
        self.app = None
        
    def add_task(self, 
                 task_func: Callable,
                 inputs: Dict[str, Any] = None,
                 task_name: str = None) -> TaskDefinition:
        """
        添加任务定义（不提交到服务端，仅保存在本地）
        
        Args:
            task_func: 使用@task装饰的函数
            inputs: 输入字典，None表示运行时提供
            task_name: 任务名称
            
        Returns:
            TaskDefinition: 任务定义对象
            
        示例:
            task1 = workflow.add_task(
                my_func,
                inputs={"user_input": None}  # 运行时提供
            )
        """
        if inputs is None:
            inputs = {}
        
        # 记录需要用户输入的参数
        for key, value in inputs.items():
            if value is None:
                if key not in self.user_input_keys:
                    self.user_input_keys.append(key)
        
        # 创建任务定义
        task_def = TaskDefinition(task_func, inputs, task_name)
        self.task_definitions.append(task_def)
        
        # 创建输出引用对象（用于后续任务引用）
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
        """
        添加任务依赖边
        
        Args:
            source_task: 源任务定义
            target_task: 目标任务定义
        """
        edge_def = EdgeDefinition(source_task, target_task)
        self.edge_definitions.append(edge_def)
    
    def _create_workflow_instance(self, user_inputs: Dict[str, Any], run_id: str = None):
        """
        根据模板和用户输入创建实际的workflow实例
        
        Args:
            user_inputs: 用户输入字典
            run_id: 运行ID（可选）
            
        Returns:
            MaWorkflow: 实际的workflow实例
        """
        import requests
        from maze.client.workflow import MaWorkflow
        from maze.client.file_utils import FileInput, is_file_type
        
        # 1. 创建workflow（携带 agent 信息）
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
            raise Exception(f"创建workflow失败: {response.status_code}")
        
        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"创建workflow失败: {data.get('message')}")
        
        workflow_id = data["workflow_id"]
        workflow = MaWorkflow(workflow_id, self.server_url)
        
        # 2. 建立任务定义到实际任务的映射
        task_def_to_task = {}
        
        # 3. 添加所有任务
        for task_def in self.task_definitions:
            # 填充用户输入
            actual_inputs = {}
            for key, value in task_def.inputs.items():
                if value is None:
                    # 从用户输入中获取
                    actual_inputs[key] = user_inputs.get(key)
                elif isinstance(value, TaskOutputReference):
                    # 任务间引用
                    source_task = task_def_to_task[value.task_def]
                    actual_inputs[key] = source_task.outputs[value.output_key]
                else:
                    # 固定值
                    actual_inputs[key] = value
            
            # 添加任务到workflow
            task = workflow.add_task(task_def.task_func, inputs=actual_inputs)
            task_def_to_task[task_def] = task
        
        # 4. 添加所有边
        for edge_def in self.edge_definitions:
            source_task = task_def_to_task[edge_def.source_task_def]
            target_task = task_def_to_task[edge_def.target_task_def]
            workflow.add_edge(source_task, target_task)
        
        return workflow
    
    def set_user_inputs_and_run(self, user_inputs: Dict[str, Any], 
                                output_dir: str = None,
                                verbose: bool = False) -> Dict[str, Any]:
        """
        设置用户输入并运行工作流
        
        Args:
            user_inputs: 用户输入字典 {参数名: 值}
            output_dir: 文件下载目录
            verbose: 是否打印执行进度
            
        Returns:
            Dict: {"run_id": "xxx", "result": {...}}
            
        示例:
            result = workflow.set_user_inputs_and_run({
                "user_question": "我最近总是头疼",
                "user_age": 25
            })
        """
        run_id = str(uuid.uuid4())
        
        try:
            # 创建新的workflow实例
            workflow = self._create_workflow_instance(user_inputs, run_id)
            
            # 运行workflow
            if output_dir is None:
                output_dir = f"agent_results/{self.name}"
            
            workflow.run()
            result = workflow.get_results(verbose=verbose, output_dir=output_dir)
            
            # 清理服务端临时文件
            workflow.cleanup()
            
            # 保存结果
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
        """
        获取指定运行的结果
        
        Args:
            run_id: 运行ID
            
        Returns:
            Dict: 运行结果，如果不存在返回None
        """
        return self.run_results.get(run_id)
    
    def _register_agent(self):
        """
        向 server 注册 agent（私有方法）
        """
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
        """
        将工作流部署为HTTP服务
        
        Args:
            host: 服务监听地址，默认 0.0.0.0
            **kwargs: 传递给uvicorn的其他参数
            
        提供的API:
            POST /{workflow_name}/run
                - 接收用户输入并运行
                - 返回: {"run_id": "xxx", "result": {...}}
            
            GET /{workflow_name}/result/{run_id}
                - 获取运行结果
                - 返回: {"result": {...}}
        """
        # 向 server 注册 agent
        self._register_agent()
        
        self.app = FastAPI(title=f"{self.name} Agent")
        
        # 添加CORS
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        
        # 创建API端点
        @self.app.post(f"/{self.name}/run")
        async def run_workflow(user_inputs: Dict[str, Any]):
            """运行工作流"""
            try:
                result = self.set_user_inputs_and_run(user_inputs, verbose=False)
                return result
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.get(f"/{self.name}/result/{{run_id}}")
        async def get_result(run_id: str):
            """获取运行结果"""
            result = self.get_run_result(run_id)
            if result is None:
                raise HTTPException(status_code=404, detail="Run ID not found")
            return {"result": result}
        
        @self.app.get(f"/{self.name}/info")
        async def get_info():
            """获取Agent信息"""
            return {
                "name": self.name,
                "user_input_keys": self.user_input_keys,
                "total_tasks": len(self.task_definitions),
                "total_runs": len(self.run_results)
            }
        
        # Start service
        print(f"🚀 Deploying {self.name} Agent service...")
        print(f"   Address: http://{host}:{self.agent_port}")
        print(f"   Run endpoint: POST http://{host}:{self.agent_port}/{self.name}/run")
        print(f"   Result endpoint: GET http://{host}:{self.agent_port}/{self.name}/result/{{run_id}}")
        print(f"   Info endpoint: GET http://{host}:{self.agent_port}/{self.name}/info")
        
        # 阻塞式运行（按 Ctrl+C 停止）
        uvicorn.run(self.app, host=host, port=self.agent_port, **kwargs)
    
    def __repr__(self) -> str:
        return f"ServerWorkflow(name='{self.name}', tasks={len(self.task_definitions)}, runs={len(self.run_results)})"

