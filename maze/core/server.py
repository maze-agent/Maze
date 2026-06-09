from ast import arg
import asyncio
import uuid
import signal
import copy
import contextlib
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any,List
from urllib.parse import urlsplit, urlunsplit
from maze.core.path.path import MaPath
from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.responses import FileResponse
import cloudpickle
import binascii
from pydantic import BaseModel
from maze.core.workflow.task import TaskType,CodeTask,LangGraphTask
from maze.core.files.artifact_store import LocalCASArtifactStore, sha256_bytes
from maze.core.application.spec import AppSpecError, app_file_context, app_spec_from_payload
from maze.core.workflow.dag_spec import DagSpecError, dag_file_context, dag_spec_from_payload


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

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _is_local_host(host: str | None) -> bool:
    return (host or "").strip().lower().strip("[]") in LOCAL_HOSTS


def _request_host(req: Request) -> str | None:
    header_host = req.headers.get("host", "")
    if not header_host:
        return req.client.host if req.client else None
    return header_host.rsplit(":", 1)[0].strip("[]")


def _worker_reachable_head_host(req: Request, cluster_host: str | None, explicit_host: str | None = None) -> str:
    if explicit_host:
        return explicit_host

    request_host = _request_host(req)
    if cluster_host and not _is_local_host(cluster_host):
        return cluster_host
    if request_host and not _is_local_host(request_host):
        return request_host
    return cluster_host or request_host or "localhost"


def _replace_url_host(base_url: str, host: str, fallback_port: int | None = None) -> str:
    parsed = urlsplit(base_url)
    port = parsed.port or fallback_port
    host_for_netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    netloc = f"{host_for_netloc}:{port}" if port else host_for_netloc
    return urlunsplit((parsed.scheme or "http", netloc, parsed.path, parsed.query, parsed.fragment)).rstrip("/")


def _request_base_url(req: Request) -> str:
    parsed = urlsplit(str(req.base_url))
    return urlunsplit((parsed.scheme or "http", parsed.netloc, "", "", "")).rstrip("/")


async def _worker_reachable_file_context(req: Request, file_context: Dict[str, Any] | None):
    if not file_context or not file_context.get("enabled"):
        return file_context

    artifact_store_context = file_context.get("artifact_store") or {}
    base_url = artifact_store_context.get("base_url")
    if not base_url:
        return file_context

    parsed = urlsplit(base_url)
    if not _is_local_host(parsed.hostname):
        return file_context

    cluster_host = None
    with contextlib.suppress(Exception):
        cluster = await mapath.get_cluster_resources(timeout=2.0)
        cluster_host = cluster.get("head_node_ip")

    head_host = _worker_reachable_head_host(req, cluster_host)
    if not head_host or _is_local_host(head_host):
        return file_context

    prepared_context = copy.deepcopy(file_context)
    prepared_store = dict(prepared_context.get("artifact_store") or {})
    prepared_store["base_url"] = _replace_url_host(base_url, head_host, req.url.port)
    prepared_context["artifact_store"] = prepared_store
    return prepared_context

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
                max_retries=data.get("max_retries"),
                retry_backoff_seconds=data.get("retry_backoff_seconds", 0),
                retry_on=data.get("retry_on"),
                timeout_seconds=data.get("timeout_seconds"),
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
                max_retries=data.get("max_retries"),
                retry_backoff_seconds=data.get("retry_backoff_seconds", 0),
                retry_on=data.get("retry_on"),
                timeout_seconds=data.get("timeout_seconds"),
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
        
        run_id = mapath.run_workflow(
            workflow_id,
            file_context=await _worker_reachable_file_context(req, data.get("file_context")),
            timeout_seconds=data.get("timeout_seconds"),
            tags=data.get("tags"),
            metadata=data.get("metadata"),
        )
        return {"status":"success","run_id": run_id}
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/apps/validate")
async def validate_app_spec(req: Request):
    try:
        data = await req.json()
        payload = data.get("spec", data)
        spec = app_spec_from_payload(
            payload,
            source_path=data.get("source_path"),
            overrides={
                "workspace": data.get("workspace_dir"),
                "timeout_seconds": data.get("timeout_seconds"),
            },
        )
        return {"status": "success", "spec": spec}
    except AppSpecError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/apps/run")
async def run_app(req: Request):
    try:
        data = await req.json()
        payload = data.get("spec", data)
        spec = app_spec_from_payload(
            payload,
            source_path=data.get("source_path"),
            overrides={
                "workspace": data.get("workspace_dir"),
                "timeout_seconds": data.get("timeout_seconds"),
            },
        )
        workflow_id = mapath.create_app_workflow(spec)
        artifact_mode = data.get("artifact_mode", True)
        file_context = data.get("file_context")
        if file_context is None:
            file_context = app_file_context(
                spec,
                artifact_base_url=_request_base_url(req),
                artifact_mode=artifact_mode,
            )
        file_context = await _worker_reachable_file_context(req, file_context)
        metadata = {
            **dict(spec.get("metadata") or {}),
            **dict(data.get("metadata") or {}),
            "app_name": spec["name"],
            "workflow_name": spec["name"],
            "run_kind": "app",
            "app_spec": spec,
        }
        tags = list(dict.fromkeys([*spec.get("tags", []), *data.get("tags", []), "app"]))
        run_id = mapath.run_workflow(
            workflow_id,
            file_context=file_context,
            timeout_seconds=spec.get("timeout_seconds"),
            tags=tags,
            metadata=metadata,
        )
        return {
            "status": "success",
            "run_id": run_id,
            "workflow_id": workflow_id,
            "spec": spec,
        }
    except AppSpecError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/workflows/validate")
async def validate_dag_workflow(req: Request):
    try:
        data = await req.json()
        payload = data.get("spec", data)
        spec = dag_spec_from_payload(payload)
        return {"status": "success", "spec": spec}
    except DagSpecError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/workflows/submit")
async def submit_dag_workflow(req: Request):
    try:
        data = await req.json()
        payload = data.get("spec", data)
        spec = dag_spec_from_payload(payload)
        workflow_id = mapath.create_dag_workflow(spec)

        run_config = spec.get("run") or {}
        artifact_mode = bool(run_config.get("artifact_mode", data.get("artifact_mode", True)))
        file_context = data.get("file_context")
        if file_context is None:
            file_context = dag_file_context(
                spec,
                artifact_base_url=_request_base_url(req),
                artifact_mode=artifact_mode,
            )
        file_context = await _worker_reachable_file_context(req, file_context)

        metadata = {
            **dict(spec.get("metadata") or {}),
            **dict(run_config.get("metadata") or {}),
            **dict(data.get("metadata") or {}),
            "workflow_name": spec["name"],
            "run_kind": "dag",
            "dag_spec": spec,
        }
        tags = list(dict.fromkeys([
            *spec.get("tags", []),
            *run_config.get("tags", []),
            *data.get("tags", []),
            "dag",
        ]))
        run_id = mapath.run_workflow(
            workflow_id,
            file_context=file_context,
            timeout_seconds=run_config.get("timeout_seconds"),
            tags=tags,
            metadata=metadata,
        )
        return {
            "status": "success",
            "workflow_id": workflow_id,
            "run_id": run_id,
            "spec": spec,
        }
    except DagSpecError as e:
        raise HTTPException(status_code=400, detail=str(e))
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

@app.post("/dynamic_runs")
async def create_dynamic_run(req: Request):
    try:
        data = await req.json()
        run_id = await mapath.create_dynamic_run(
            max_tasks=data.get("max_tasks", 100),
            timeout_seconds=data.get("timeout_seconds"),
            file_context=await _worker_reachable_file_context(req, data.get("file_context")),
        )
        return {"status": "success", "run_id": run_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs")
async def list_runs(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    limit: Optional[int] = None,
    detail: bool = False,
):
    try:
        return {
            "status": "success",
            "runs": await mapath.list_runs(status=status, kind=kind, limit=limit, detail=detail),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    try:
        return {
            "status": "success",
            "run": await mapath.get_run_snapshot(run_id),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs/{run_id}/tasks")
async def get_run_tasks(run_id: str):
    try:
        return {
            "status": "success",
            "run_id": run_id,
            "tasks": await mapath.get_run_tasks(run_id),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs/{run_id}/tasks/{task_id}")
async def get_run_task(run_id: str, task_id: str):
    try:
        return {
            "status": "success",
            "run_id": run_id,
            "task": await mapath.get_run_task(run_id, task_id),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs/{run_id}/artifacts")
async def get_run_artifacts(run_id: str):
    try:
        return {
            "status": "success",
            "run_id": run_id,
            "artifacts": await mapath.get_run_artifacts(run_id),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs/{run_id}/tasks/{task_id}/artifacts")
async def get_run_task_artifacts(run_id: str, task_id: str):
    try:
        return {
            "status": "success",
            "run_id": run_id,
            "task_id": task_id,
            "artifacts": await mapath.get_run_task_artifacts(run_id, task_id),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs/{run_id}/events")
async def get_run_events(run_id: str, after: Optional[int] = None):
    try:
        return {
            "status": "success",
            "run_id": run_id,
            "events": await mapath.get_run_events(run_id, after),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/runs/{run_id}/logs")
async def get_run_logs(run_id: str, tail: Optional[int] = 500, task_id: Optional[str] = None):
    try:
        return {
            "status": "success",
            **await mapath.get_run_logs(run_id, tail=tail, task_id=task_id),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, req: Request):
    try:
        try:
            data = await req.json()
        except Exception:
            data = {}

        if run_id in mapath.dynamic_runs:
            dynamic_run = await mapath.cancel_dynamic_run(run_id, data.get("reason"))
            return {
                "status": "success",
                "run_id": run_id,
                "run_status": dynamic_run.status,
            }

        await mapath.stop_workflow(run_id)
        return {
            "status": "success",
            "run_id": run_id,
            "run_status": "cancelled",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/runs/{run_id}/retry")
async def retry_run(run_id: str, req: Request):
    try:
        try:
            data = await req.json()
        except Exception:
            data = {}
        snapshot = await mapath.get_run_snapshot(run_id)
        metadata = snapshot.get("metadata") or {}
        spec = metadata.get("app_spec")
        if not spec:
            raise HTTPException(status_code=400, detail="Only AppSpec runs can be retried through this endpoint")

        spec = app_spec_from_payload(
            spec,
            source_path=spec.get("source_path"),
            overrides={
                "workspace": data.get("workspace_dir"),
                "timeout_seconds": data.get("timeout_seconds"),
            },
        )
        workflow_id = mapath.create_app_workflow(spec)
        file_context = data.get("file_context")
        if file_context is None:
            file_context = app_file_context(
                spec,
                artifact_base_url=_request_base_url(req),
                artifact_mode=data.get("artifact_mode", True),
            )
        file_context = await _worker_reachable_file_context(req, file_context)
        retry_metadata = {
            **metadata,
            **dict(data.get("metadata") or {}),
            "app_name": spec["name"],
            "workflow_name": spec["name"],
            "run_kind": "app",
            "app_spec": spec,
            "retried_from_run_id": run_id,
        }
        previous_tags = snapshot.get("tags") or []
        tags = list(dict.fromkeys([*previous_tags, *data.get("tags", []), "app", "retry"]))
        new_run_id = mapath.run_workflow(
            workflow_id,
            file_context=file_context,
            timeout_seconds=spec.get("timeout_seconds"),
            tags=tags,
            metadata=retry_metadata,
        )
        return {
            "status": "success",
            "run_id": new_run_id,
            "workflow_id": workflow_id,
            "retried_from_run_id": run_id,
            "spec": spec,
        }
    except HTTPException:
        raise
    except AppSpecError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
            resources=data.get("resources") or data.get("resource_override"),
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
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for scheduler cluster resources")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cluster/queues")
async def get_cluster_queues():
    try:
        queues = await mapath.get_cluster_queues()
        return {"status": "success", "queues": queues}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for scheduler queue snapshot")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cluster/join_command")
async def get_cluster_join_command(req: Request, host: Optional[str] = None):
    try:
        cluster = await mapath.get_cluster_resources()
        head_host = _worker_reachable_head_host(req, cluster.get("head_node_ip"), host)
        port = req.url.port or 80
        head_url = f"http://{head_host}:{port}"
        command = f"maze start --worker --addr {head_host}:{port}"
        return {
            "status": "success",
            "head_host": head_host,
            "head_url": head_url,
            "ray_head_port": mapath.get_ray_head_port(),
            "command": command,
            "agent_command": f"{command} --agent",
        }
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for scheduler cluster resources")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/cluster/reconcile_workers")
async def reconcile_workers(req: Request):
    try:
        try:
            data = await req.json()
        except Exception:
            data = {}
        cluster = await mapath.get_cluster_resources()
        host = _worker_reachable_head_host(req, cluster.get("head_node_ip"), data.get("host"))
        port = int(data.get("port") or req.url.port or 80)
        ray_head_port = mapath.get_ray_head_port()
        head_url = f"http://{host}:{port}"
        commands = [
            {
                "node_id": node.get("node_id"),
                "node_ip": node.get("node_ip"),
                "command": f"maze start --worker --addr {host}:{port}",
                "agent_command": f"maze start --worker --addr {host}:{port} --agent",
            }
            for node in cluster.get("unregistered_ray_nodes", [])
        ]
        return {
            "status": "success",
            "head_host": host,
            "head_url": head_url,
            "ray_head_port": ray_head_port,
            "unregistered_count": len(commands),
            "unregistered_ray_nodes": cluster.get("unregistered_ray_nodes", []),
            "recommended_commands": commands,
            "executed": False,
        }
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for scheduler cluster resources")
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

@app.get("/artifacts/sha256/{sha256}/metadata")
async def get_artifact_metadata(sha256: str):
    try:
        if not artifact_store.exists(sha256):
            raise HTTPException(status_code=404, detail="Artifact not found")
        return {"status": "success", "artifact": artifact_store.metadata(sha256)}
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
        worker = await mapath.start_worker(
            data["node_ip"],
            data["node_id"],
            data["resources"],
            data.get("capabilities"),
        )
        return {"status": "success", "worker": worker}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for scheduler worker registration")
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


# === Phase 1 observability API (static workflows) ===

@app.get("/v1/metrics")
async def get_global_metrics():
    """Cluster-wide aggregate metrics for static workflows."""
    try:
        return mapath.get_global_metrics_snapshot()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/runs")
async def list_runs(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """List static workflow runs (newest first)."""
    try:
        runs = mapath.list_static_runs(status=status, limit=limit + offset)
        offset = max(0, int(offset))
        limit = max(0, int(limit))
        return {
            "runs": runs[offset:offset + limit] if limit else runs[offset:],
            "total": len(runs),
            "offset": offset,
            "limit": limit,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/runs/{run_id}/snapshot")
async def get_run_snapshot(run_id: str):
    """Full snapshot of a static run (in-memory if active, else from store)."""
    try:
        return mapath.get_static_run_snapshot(run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/runs/{run_id}/current-task")
async def get_run_current_task(run_id: str):
    """What is the run currently doing?"""
    try:
        return mapath.get_static_current_task(run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/runs/{run_id}/tasks")
async def list_run_tasks(run_id: str):
    """All tasks of a static run with their states and metrics."""
    try:
        snapshot = mapath.get_static_run_snapshot(run_id)
        return {
            "run_id": run_id,
            "task_total": snapshot.get("task_total"),
            "tasks": snapshot.get("tasks") or {},
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/runs/{run_id}/timeline")
async def get_run_timeline(run_id: str, after: Optional[int] = None):
    """Event log of a static run (one event per scheduling moment)."""
    try:
        events = mapath._get_static_run_events(run_id, after=after)
        return {"run_id": run_id, "events": events}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
