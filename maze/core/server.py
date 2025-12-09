from ast import arg
import uuid
import signal
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any,List
from maze.core.path.path import MaPath
from fastapi import FastAPI, WebSocket, Request, HTTPException
import cloudpickle
import binascii
from pydantic import BaseModel
from maze.core.workflow.task import TaskType,CodeTask,LangGraphTask


app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   
    allow_credentials=True,
    allow_methods=["*"],  
    allow_headers=["*"],    
)

mapath = MaPath()

def signal_handler(signum, frame):
    mapath.cleanup()
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

@app.post("/create_workflow")
async def create_workflow(req:Request):
    try:
        workflow_id: str = str(uuid.uuid4())
        mapath.create_workflow(workflow_id)
        return {"status": "success","workflow_id": workflow_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
     
@app.post("/add_task")
async def add_task(req:Request):
    try:
        data = await req.json()
        workflow_id:str = data["workflow_id"]
        task_type:str = data["task_type"]
        task_name: str =data["task_name"]
        task_id: str = str(uuid.uuid4())
     
        if(task_type == TaskType.CODE.value):
            mapath.get_workflow(workflow_id).add_task(task_id,CodeTask(workflow_id,task_id,task_name))
        else:
            raise HTTPException(status_code=500, detail="Invalid task_type")

        return {"status":"success","task_id": task_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/get_workflow_tasks/{workflow_id}")
async def get_workflow_tasks(workflow_id: str):
    try:
        # 调用mapath获取工作流任务
        tasks = mapath.get_workflow_tasks(workflow_id)
        return {"status": "success", "tasks": tasks}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/del_task")
async def del_task(req:Request):
    try:
        data = await req.json()
        workflow_id:str = data["workflow_id"]
        task_id: str = data["task_id"]
      
        mapath.get_workflow(workflow_id).del_task(task_id)
        return {"status":"success","task_id": task_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/save_task")
async def save_task(req:Request):
    try:
        data = await req.json()
        workflow_id = data["workflow_id"]
        task_id = data["task_id"]
        resources = data["resources"]

        task = mapath.get_workflow(workflow_id).get_task(task_id)
        if(task.task_type == TaskType.CODE.value):    
            task_input = data["task_input"]
            task_output = data["task_output"]
            code_str = data.get("code_str")
            code_ser = data.get("code_ser")
            if code_ser is None and code_str is None:
                raise HTTPException(status_code=500, detail="code_str or code_ser is required")
            task.save_task(task_input=task_input, task_output=task_output, code_str = code_str, code_ser = code_ser, resources=resources)
        else:
            raise HTTPException(status_code=500, detail="Invalid task_type")
  
        return {"status":"success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/save_task_and_add_edge")
async def save_task_and_add_edge(req:Request):
    try:
        data = await req.json()
        workflow_id = data["workflow_id"]
        task_id = data["task_id"]
        resources = data["resources"]

        workflow = mapath.get_workflow(workflow_id)
        task = workflow.get_task(task_id)
        if(task.task_type == TaskType.CODE.value):    
            task_input = data["task_input"]
            task_output = data["task_output"]
            code_str = data.get("code_str")
            code_ser = data.get("code_ser")
            if code_ser is None and code_str is None:
                raise HTTPException(status_code=500, detail="code_str or code_ser is required")
            task.save_task(task_input=task_input, task_output=task_output, code_str = code_str, code_ser = code_ser, resources=resources)

            # 修复：正确遍历 input_params
            for _, input_param in task_input.get("input_params", {}).items():
                if input_param.get('input_schema') == 'from_task':
                    source_task_id = input_param['value'].split('.')[0]
                    target_task_id = task_id
                    workflow.add_edge(source_task_id, target_task_id)
        else:
            raise HTTPException(status_code=500, detail="Invalid task_type")
  
        return {"status":"success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/add_edge")
async def add_edge(req:Request):
    try:
        data = await req.json()
        workflow_id = data["workflow_id"]
        source_task_id = data["source_task_id"]
        target_task_id = data["target_task_id"]
         
        mapath.get_workflow(workflow_id).add_edge(source_task_id, target_task_id)
        return {"status":"success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/del_edge")
async def del_edge(req:Request):
    try:
        data = await req.json()
    
        workflow_id = data["workflow_id"]
        source_task_id = data["source_task_id"]
        target_task_id = data["target_task_id"]
        mapath.get_workflow(workflow_id).del_edge(source_task_id, target_task_id)
    
        return {"status":"success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/run_workflow")
async def run_workflow(req:Request):
    try:
        data = await req.json()
        workflow_id = data["workflow_id"]
        
        run_id = mapath.run_workflow(workflow_id)
        return {"status":"success","run_id": run_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/get_workflow_res/{workflow_id}/{run_id}")
async def get_workflow_res(websocket: WebSocket, workflow_id: str, run_id: str):
    try:
        await websocket.accept()
        await mapath.get_workflow_res(workflow_id,run_id,websocket)
        await websocket.close()
    except Exception as e:
        await mapath.stop_workflow(run_id)
        await websocket.close()

@app.post("/add_langgraph_task")
async def add_langgraph_task(req:Request):
    try:
        data = await req.json()
        workflow_id:str = data["workflow_id"]
        task_type:str = data["task_type"]
        task_name: str =data["task_name"]
        code_ser = data["code_ser"]
        resources = data["resources"]
        task_id: str = str(uuid.uuid4())
     
        if(task_type == TaskType.LANGGRAPH.value):
            mapath.get_workflow(workflow_id).add_task(task_id,LangGraphTask(workflow_id,task_id,task_name,code_ser=code_ser,resources=resources))
            
        else:
            raise HTTPException(status_code=500, detail="Invalid task_type")

        return {"status":"success","task_id": task_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/run_langgraph_task")
async def run_langgraph_task(req:Request):
    try:
        data = await req.json()
        workflow_id = data["workflow_id"]
        task_id = data["task_id"]
        args = data["args"]
        kwargs = data["kwargs"]
        result = await mapath.run_langgraph_task(workflow_id=workflow_id,task_id=task_id,args=args,kwargs=kwargs)
        return {"status": "success","result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
         
@app.post("/get_head_ray_port")
async def get_head_ray_port():
    try:
        port =  mapath.get_ray_head_port()
        return {"status": "success","port": port}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/start_worker")
async def start_worker(req:Request):
    try:
        data = await req.json()
        mapath.start_worker(data["node_ip"], data["node_id"], data["resources"])
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# for multiple llm inference instance
@app.post("/start_llm_instance")
async def start_llm_instance(req:Request):
    try:
        data = await req.json()
        instance_id = str(uuid.uuid4())
        host,port = await mapath.start_llm_instance(instance_id, data["model"], data["cpu_nums"], data["gpu_nums"], data["memory"], data.get("gpu_mem",0))
        return {"status": "success","host": host,"port": port,"instance_id": instance_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/stop_llm_instance")
async def stop_llm_instance(req:Request):
    try:
        data = await req.json()
        await mapath.stop_llm_instance(data["instance_id"])
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
