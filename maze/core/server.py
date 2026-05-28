from ast import arg
import uuid
import signal
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any,List
from maze.core.path.path import MaPath
from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.responses import FileResponse
import cloudpickle
import binascii
from pydantic import BaseModel
from maze.core.workflow.task import TaskType,CodeTask,LangGraphTask
from maze.core.files.artifact_store import LocalCASArtifactStore, sha256_bytes


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
artifact_store = LocalCASArtifactStore()

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
            task.save_task(
                task_input=task_input,
                task_output=task_output,
                code_str=code_str,
                code_ser=code_ser,
                resources=resources,
                file_context=data.get("file_context"),
            )
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
            task.save_task(
                task_input=task_input,
                task_output=task_output,
                code_str=code_str,
                code_ser=code_ser,
                resources=resources,
                file_context=data.get("file_context"),
            )

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
        
        run_id = mapath.run_workflow(workflow_id, file_context=data.get("file_context"))
        return {"status":"success","run_id": run_id}
    except Exception as e:
        print(e)
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

@app.post("/dynamic_runs")
async def create_dynamic_run(req: Request):
    try:
        data = await req.json()
        run_id = await mapath.create_dynamic_run(
            max_tasks=data.get("max_tasks", 100),
            timeout_seconds=data.get("timeout_seconds"),
        )
        return {"status": "success", "run_id": run_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dynamic_runs")
async def list_dynamic_runs(
    status: Optional[str] = None,
    limit: Optional[int] = None,
    detail: bool = False,
):
    try:
        return {
            "status": "success",
            "runs": await mapath.list_dynamic_runs(status=status, limit=limit, detail=detail),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs/cleanup")
async def cleanup_dynamic_runs(req: Request):
    try:
        try:
            data = await req.json()
        except Exception:
            data = {}
        return {
            "status": "success",
            "cleanup": await mapath.cleanup_dynamic_runs(
                statuses=data.get("statuses"),
                older_than_days=data.get("older_than_days"),
                dry_run=data.get("dry_run", True),
            ),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs/{run_id}/task_specs")
async def register_dynamic_task_spec(run_id: str, req: Request):
    try:
        data = await req.json()
        task_spec = await mapath.register_dynamic_task_spec(run_id, data)
        return {
            "status": "success",
            "run_id": run_id,
            "task_spec_id": task_spec.task_spec_id,
            "task_name": task_spec.task_name,
            "inputs": task_spec.inputs,
            "outputs": task_spec.outputs,
            "resources": task_spec.resources,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dynamic_runs/{run_id}")
async def get_dynamic_run(run_id: str):
    try:
        return {
            "status": "success",
            "run": await mapath.get_dynamic_run_snapshot(run_id),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/dynamic_runs/{run_id}")
async def delete_dynamic_run(run_id: str):
    try:
        return {
            "status": "success",
            **await mapath.delete_dynamic_run(run_id),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs/{run_id}/append_task")
async def append_dynamic_task(run_id: str, req: Request):
    try:
        data = await req.json()
        task, idempotent = await mapath.append_dynamic_task(
            run_id=run_id,
            task_spec_id=data.get("task_spec_id"),
            task_spec_payload=data.get("task_spec"),
            inputs=data.get("inputs", {}),
            parents=data.get("parents", []),
            request_id=data.get("request_id"),
        )
        outputs = []
        if task.task_output:
            outputs = [
                {
                    "name": output_info.get("key"),
                    "data_type": output_info.get("data_type", "any"),
                }
                for output_info in task.task_output.get("output_params", {}).values()
            ]
        return {
            "status": "success",
            "run_id": run_id,
            "task_id": task.task_id,
            "task_name": task.task_name,
            "outputs": outputs,
            "idempotent": idempotent,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs/{run_id}/finalize")
async def finalize_dynamic_run(run_id: str, req: Request):
    try:
        data = await req.json()
        await mapath.finalize_dynamic_run(run_id, data.get("result"))
        return {"status": "success", "run_id": run_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs/{run_id}/cancel")
async def cancel_dynamic_run(run_id: str, req: Request):
    try:
        try:
            data = await req.json()
        except Exception:
            data = {}
        dynamic_run = await mapath.cancel_dynamic_run(run_id, data.get("reason"))
        return {
            "status": "success",
            "run_id": run_id,
            "run_status": dynamic_run.status,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dynamic_runs/{run_id}/events")
async def get_dynamic_run_events(run_id: str, after: Optional[int] = None):
    try:
        return {
            "status": "success",
            "run_id": run_id,
            "events": await mapath.get_dynamic_run_events(run_id, after),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/dynamic_runs/{run_id}/events")
async def emit_dynamic_run_event(run_id: str, req: Request):
    try:
        data = await req.json()
        event = await mapath.emit_dynamic_run_event(run_id, data)
        return {
            "status": "success",
            "run_id": run_id,
            "event": event,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/dynamic_runs/{run_id}/events")
async def get_dynamic_run_events_ws(websocket: WebSocket, run_id: str):
    try:
        await websocket.accept()
        await mapath.get_dynamic_run_res(run_id, websocket)
        await websocket.close()
    except Exception:
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

@app.get("/cluster/resources")
async def get_cluster_resources():
    try:
        resources = await mapath.get_cluster_resources()
        return {"status": "success", "cluster": resources}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/artifacts/sha256/{sha256}")
async def put_artifact(sha256: str, req: Request):
    try:
        data = await req.body()
        if sha256_bytes(data) != sha256:
            raise HTTPException(status_code=400, detail="Artifact checksum mismatch")
        return artifact_store.put_bytes(sha256, data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.head("/artifacts/sha256/{sha256}")
async def head_artifact(sha256: str):
    try:
        if not artifact_store.exists(sha256):
            raise HTTPException(status_code=404, detail="Artifact not found")
        return artifact_store.metadata(sha256)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/artifacts/sha256/{sha256}")
async def get_artifact(sha256: str):
    try:
        path = artifact_store.blob_path(sha256)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Artifact not found")
        return FileResponse(path, media_type="application/octet-stream", filename=sha256)
    except HTTPException:
        raise
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
