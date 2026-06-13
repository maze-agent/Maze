from asyncio.queues import Queue
import math
import os
import time
import uuid
import httpx
import json
import copy
import logging
import zmq
import zmq.asyncio
import asyncio
import multiprocessing as mp
from fastapi import WebSocket
from pathlib import Path
from typing import Any,Dict,List
from asyncio.queues import Queue
from maze.core.workflow.task import CodeTask, LangGraphTask,TaskType
from maze.core.workflow.workflow import Workflow,LangGraphWorkflow
from maze.core.workflow.dynamic import DynamicRun, TERMINAL_DYNAMIC_RUN_STATUSES, dynamic_task_spec_from_payload
from maze.core.workflow.dynamic_store import DynamicRunStore
from maze.core.workflow.dag_spec import build_dag_workflow
from maze.core.workflow.static_run import StaticRun, StaticRunStore, static_run_summary
from maze.core.application.spec import build_app_workflow
from maze.core.runs import GlobalMetrics
from maze.core.scheduler.scheduler import scheduler_process
from maze.core.scheduler.result_summary import summarize_task_result
from maze.core.files.artifact_store import LocalCASArtifactStore
from maze.utils.utils import get_available_ports

logger = logging.getLogger(__name__)
EPSILON = 1e-3

class MaPath:
    def __init__(self):
        self.lock = lock = asyncio.Lock()

        self.workflows: Dict[str, Workflow|LangGraphWorkflow] = {}
        self.submit_workflows: Dict[str, Workflow] = {}
        self.static_runs: Dict[str, StaticRun] = {}
        self.static_run_store = StaticRunStore()
        self.static_run_store.recover_interrupted_runs()
        self.dynamic_runs: Dict[str, DynamicRun] = {}
        self.dynamic_run_store = DynamicRunStore()
        self.dynamic_run_store.recover_interrupted_runs()
        self.async_que: Dict[str, asyncio.Queue] = {} 
        self.llm_instance_async_que: Dict[str, asyncio.Queue] = {}
        self.cluster_resource_requests: Dict[str, asyncio.Queue] = {}
        self.cluster_queue_requests: Dict[str, asyncio.Queue] = {}
        self.worker_registration_requests: Dict[str, asyncio.Queue] = {}
        self.atlas_enqueue_index = 0

        self.can_predict_task = ['llm_process','llm_fuse','vlm_process','speech_process']
        self.global_metrics = GlobalMetrics()
        for snapshot in self.static_run_store.list_runs():
            status = snapshot.get("status")
            if status in ("submitted", "running"):
                self.global_metrics.on_run_status_change(
                    snapshot["run_id"],
                    status,
                    "interrupted",
                )
         
    def cleanup(self):
        '''
        Clean up the main process and scheduler process.
        '''
        if getattr(self, "_cleanup_started", False):
            return
        self._cleanup_started = True

        try:
            self._send_scheduler_message({"type": "shutdown"})
        except Exception:
            pass

        scheduler_process = getattr(self, "scheduler_process", None)
        if scheduler_process is not None:
            try:
                if scheduler_process.pid and scheduler_process.pid != os.getpid():
                    scheduler_process.join(timeout=10)
                    if scheduler_process.is_alive():
                        scheduler_process.terminate()
                        scheduler_process.join(timeout=5)
            except AssertionError:
                pass
            except Exception:
                pass

        for socket_name in ("socket_to_scheduler", "socket_from_scheduler"):
            socket = getattr(self, socket_name, None)
            if socket is not None:
                try:
                    socket.close(linger=0)
                except Exception:
                    pass

    def _send_scheduler_message(self, message: Dict[str, Any]):
        serialized: bytes = json.dumps(message).encode('utf-8')
        self.socket_to_scheduler.send(serialized)
        
    def create_workflow(self,workflow_id:str):
        '''
        Create a workflow.
        '''
        self.workflows[workflow_id] = Workflow(workflow_id)
        self.global_metrics.on_workflow_created(workflow_id)

    def create_app_workflow(self, spec:Dict[str,Any]) -> str:
        '''
        Create a single-task application workflow from an AppSpec.
        '''
        workflow_id = str(uuid.uuid4())
        self.workflows[workflow_id] = build_app_workflow(workflow_id, spec)
        return workflow_id

    def create_dag_workflow(self, spec:Dict[str,Any]) -> str:
        '''
        Create a static workflow from an external DAG submit spec.
        '''
        workflow_id = str(uuid.uuid4())
        self.workflows[workflow_id] = build_dag_workflow(workflow_id, spec)
        self.global_metrics.on_workflow_created(workflow_id)
        return workflow_id

    def get_workflow(self,workflow_id:str) -> Workflow|LangGraphWorkflow:
        '''
        Get a workflow.
        '''
        return self.workflows[workflow_id]
  
    def get_workflow_tasks(self,workflow_id:str):
        """
        Get all tasks in a workflow.
        """
        if workflow_id not in self.workflows:
            return []
        
        workflow = self.workflows[workflow_id]
        tasks = []
        
       
        for task_id, task in workflow.tasks.items():
            tasks.append({
                "id": task_id,
                "name": task.task_name if hasattr(task, 'task_name') else f"任务_{task_id[:8]}"
            })
        
        return tasks

    async def _get_daps_priority(self, task_name:str, features:Dict, remaining_task_num:int, total_task_num:int, w1:int, w2:int):
        payload = {"task_name": task_name, "features": features}
        async with httpx.AsyncClient() as client:
            response = await client.post("http://127.0.0.1:8001/predict", json=payload)
        predict_time = response.json()['predict_time']

        score_urgency = 1.0 - (remaining_task_num / total_task_num)
        return w1 * score_urgency + w2 * predict_time

    
    def _get_hacs_priority(self, workflow: Workflow, task_id: str):
        node_info = workflow.graph.nodes[task_id]
        n_desc = node_info.get("n_desc", 0)
        pred_time = max(node_info.get("pred_time", 3.0), EPSILON)
        is_dynamic = 0
        omega = math.log2(2.0 + 2.0 * n_desc)
        return (omega, pred_time, is_dynamic)

    def _get_atlas_priority(self, workflow: Workflow, task_id: str):
        attained_service = workflow.graph.nodes[task_id].get("attained_service", 0.0)
        submission_time = workflow.graph.graph.get("submission_time", 0.0)
        priority = (attained_service, submission_time, self.atlas_enqueue_index)
        self.atlas_enqueue_index += 1
        return priority

    async def _get_task_priority(self, workflow: Workflow, task: CodeTask):
        if self.strategy == "Default":
            return 0

        if task.can_predict and self.strategy == "DAPS":
            remaining_task_num: int = workflow.remaining_task_num
            total_task_num: int = len(workflow.tasks)
            return await self._get_daps_priority(
                task.task_name,
                task.predict_feature,
                remaining_task_num,
                total_task_num,
                0.5,
                0.5,
            )

        if self.strategy == "HACS":
            return self._get_hacs_priority(workflow, task.task_id)

        if self.strategy == "ATLAS":
            return self._get_atlas_priority(workflow, task.task_id)

        return 0

    def _task_run_payload(self, workflow: Workflow, task: CodeTask, submit_id: str, file_context: Dict[str, Any] | None = None):
        data = task.to_json()
        data['workflow_id'] = submit_id

        if file_context and file_context.get("enabled"):
            task_node_ids = file_context.get("task_node_ids") or {}
            parent_file_manifests = []
            for parent_task_id in workflow.graph.predecessors(task.task_id):
                manifest = workflow.graph.nodes[parent_task_id].get("file_manifest")
                if manifest:
                    parent_file_manifests.append(manifest)
            data["file_context"] = {
                **file_context,
                "enabled": True,
                "run_id": file_context.get("run_id") or submit_id,
                "submit_id": submit_id,
                "task_id": task.task_id,
                "node_id": task_node_ids.get(task.task_id),
                "parent_task_ids": list(workflow.graph.predecessors(task.task_id)),
                "parent_file_manifests": parent_file_manifests,
            }

        return data

    def _prepare_initial_artifacts(self, file_context: Dict[str, Any], submit_id: str) -> Dict[str, Any]:
        if not file_context or not file_context.get("enabled") or not file_context.get("artifact_store"):
            return file_context

        from pathlib import Path

        workspace_dir = Path(file_context["workspace_dir"]).expanduser().resolve()
        files_dir = workspace_dir / "files"
        artifact_root = file_context.get("artifact_store", {}).get("root")
        store = LocalCASArtifactStore(artifact_root)
        initial_files = []

        if files_dir.exists():
            for file_path in sorted(files_dir.rglob("*")):
                if not file_path.is_file() or "__pycache__" in file_path.parts or file_path.suffix == ".pyc":
                    continue
                relative_path = file_path.relative_to(files_dir).as_posix()
                artifact = store.put_file(file_path)
                initial_files.append({
                    "path": relative_path,
                    "name": file_path.name,
                    "size": artifact["size"],
                    "sha256": artifact["sha256"],
                    "artifact_id": artifact["artifact_id"],
                    "storage_uri": artifact["storage_uri"],
                    "producer_task_id": "__workspace__",
                    "uri": f"maze://runs/{submit_id}/workspace/files/{relative_path}",
                })

        prepared_context = copy.deepcopy(file_context)
        prepared_context["workspace_dir"] = str(workspace_dir)
        prepared_context["run_id"] = file_context.get("run_id") or submit_id
        prepared_context["initial_files"] = initial_files
        return prepared_context

    def run_workflow(
        self,
        workflow_id:str,
        file_context:Dict[str,Any]|None=None,
        timeout_seconds:float|None=None,
        tags:List[str]|None=None,
        metadata:Dict[str,Any]|None=None,
    ):
        """
        Start a workflow.
        """
        submit_id = str(uuid.uuid4())
        file_context = self._prepare_initial_artifacts(file_context, submit_id) if file_context else None
        submit_workflow = copy.deepcopy(self.workflows[workflow_id])
        if file_context:
            submit_workflow.graph.graph["file_context"] = file_context
        submit_workflow.prepare_for_strategy(self.strategy)
        submit_workflow.graph.graph["submission_time"] = time.time()
        self.submit_workflows[submit_id] = submit_workflow
        self.async_que[submit_id] = asyncio.Queue()
        static_run = StaticRun(
            submit_id,
            workflow_id,
            submit_workflow,
            timeout_seconds=timeout_seconds,
            tags=tags,
            metadata=metadata,
        )
        self.static_runs[submit_id] = static_run
        self._persist_static_run(submit_id)
        self.global_metrics.on_run_submitted(submit_id)
        self._record_static_event(submit_id, {
            "type": "start_workflow",
            "data": {
                "run_id": submit_id,
                "workflow_id": workflow_id,
                "run_type": "static",
                "total_task_num": submit_workflow.get_total_task_num(),
            },
        })
        start_task:List = submit_workflow.get_start_task()
        
        for task in start_task:
            static_run.mark_task_queued(task.task_id)
            data = self._task_run_payload(submit_workflow, task, submit_id, file_context)
            if self.strategy == "HACS":
                data['priority'] = self._get_hacs_priority(submit_workflow, task.task_id)
            elif self.strategy == "ATLAS":
                data['priority'] = self._get_atlas_priority(submit_workflow, task.task_id)
            else:
                data['priority'] = 0
            message = {
                "type":"run_task",
                "data": data
            }
            self._send_scheduler_message(message)

        self._persist_static_run(submit_id)
        return submit_id

    async def create_dynamic_run(
        self,
        max_tasks:int=100,
        timeout_seconds:int|None=None,
        file_context:Dict[str,Any]|None=None,
        metadata:Dict[str,Any]|None=None,
    ):
        run_id = str(uuid.uuid4())
        file_context = self._prepare_initial_artifacts(file_context, run_id) if file_context else None
        self.dynamic_runs[run_id] = DynamicRun(
            run_id=run_id,
            max_tasks=max_tasks,
            timeout_seconds=timeout_seconds,
            file_context=file_context,
            metadata=metadata,
        )
        self.async_que[run_id] = asyncio.Queue()
        await self._emit_dynamic_event(run_id, {
            "type": "start_dynamic_run",
            "data": {
                "run_id": run_id,
                "max_tasks": max_tasks,
                "timeout_seconds": timeout_seconds,
                "file_context_enabled": bool(file_context and file_context.get("enabled")),
            },
        })
        return run_id

    def get_dynamic_run(self, run_id:str) -> DynamicRun:
        if run_id not in self.dynamic_runs:
            raise ValueError(f"Dynamic run not found: {run_id}")
        return self.dynamic_runs[run_id]

    async def list_dynamic_runs(
        self,
        status: str | None = None,
        limit: int | None = None,
        detail: bool = False,
    ):
        snapshots = self.dynamic_run_store.list_runs(summary=not detail)
        if status:
            snapshots = [snapshot for snapshot in snapshots if snapshot.get("status") == status]
        if limit is not None:
            snapshots = snapshots[: max(0, limit)]
        return snapshots

    async def register_dynamic_task_spec(self, run_id:str, task_spec_payload:Dict[str,Any]):
        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        task_spec = dynamic_task_spec_from_payload(task_spec_payload)
        dynamic_run.register_task_spec(task_spec)
        await self._emit_dynamic_event(run_id, {
            "type": "register_task_spec",
            "data": {
                "run_id": run_id,
                "task_spec_id": task_spec.task_spec_id,
                "task_name": task_spec.task_name,
                "inputs": task_spec.inputs,
                "outputs": task_spec.outputs,
                "resources": task_spec.resources,
            },
        })
        return task_spec

    async def append_dynamic_task(
        self,
        run_id:str,
        task_spec_id:str|None=None,
        task_spec_payload:Dict[str,Any]|None=None,
        inputs:Dict[str,Any]|None=None,
        parents:List[str]|None=None,
        request_id:str|None=None,
        resources:Dict[str,Any]|None=None,
    ):
        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        dynamic_run.check_can_mutate("append tasks")
        existing_task = dynamic_run.get_task_for_request_id(request_id)
        if existing_task is not None:
            return existing_task, True

        dynamic_run.check_can_append()
        task_spec = dynamic_run.resolve_task_spec(task_spec_id, task_spec_payload)
        task, idempotent = dynamic_run.append_task(
            task_spec,
            inputs=inputs,
            parents=parents,
            request_id=request_id,
            resources=resources,
        )

        if not idempotent:
            status = "ready" if task.task_id in dynamic_run.submitted_tasks else "pending"
            await self._emit_dynamic_event(run_id, {
                "type": "append_task",
                "data": {
                    "run_id": run_id,
                    "task_id": task.task_id,
                    "task_spec_id": task_spec.task_spec_id,
                    "task_name": task.task_name,
                    "parents": sorted(dynamic_run.task_parents.get(task.task_id, set())),
                    "request_id": request_id,
                    "status": status,
                    "resources": task.resources,
                },
            })

            if task.task_id in dynamic_run.submitted_tasks:
                self._submit_dynamic_task(task)

        return task, idempotent

    async def finalize_dynamic_run(self, run_id:str, result:Any=None):
        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        dynamic_run.finalize(result)
        await self._emit_dynamic_event(run_id, {
            "type": "finish_workflow",
            "data": {
                "run_id": run_id,
                "result": summarize_task_result(result, run_id=run_id),
            },
        })

        message = {"type":"clear_workflow","data":{"workflow_id":run_id}}
        self._send_scheduler_message(message)

    async def cancel_dynamic_run(self, run_id:str, reason:str|None=None):
        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        changed = dynamic_run.cancel(reason)
        if changed:
            await self._emit_dynamic_event(run_id, {
                "type": "cancel_dynamic_run",
                "data": {
                    "run_id": run_id,
                    "reason": reason,
                },
            })
            self._stop_dynamic_runtime(run_id)
        return dynamic_run

    async def get_dynamic_run_snapshot(self, run_id:str):
        if run_id in self.dynamic_runs:
            await self._refresh_dynamic_timeout(run_id)
            return self.get_dynamic_run(run_id).snapshot(
                lambda result: summarize_task_result(result, run_id=run_id)
            )
        return self.dynamic_run_store.load_run(run_id)

    async def get_dynamic_run_events(self, run_id:str, after:int|None=None):
        if run_id in self.dynamic_runs:
            await self._refresh_dynamic_timeout(run_id)
            return self.get_dynamic_run(run_id).get_events(after)
        self.dynamic_run_store.load_run(run_id)
        return self.dynamic_run_store.load_events(run_id, after)

    async def emit_dynamic_run_event(self, run_id:str, event:Dict[str,Any]):
        await self._refresh_dynamic_timeout(run_id)
        if not isinstance(event, dict):
            raise ValueError("Dynamic run event must be a JSON object")

        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("Dynamic run event requires a non-empty string type")

        if event_type in {
            "start_dynamic_run",
            "register_task_spec",
            "append_task",
            "task_ready",
            "start_task",
            "finish_task",
            "task_exception",
            "finish_workflow",
            "cancel_dynamic_run",
            "timeout_dynamic_run",
            "interrupt_dynamic_run",
        }:
            raise ValueError(f"Dynamic run event type is reserved: {event_type}")

        event_data = event.get("data") or {}
        if not isinstance(event_data, dict):
            raise ValueError("Dynamic run event data must be a JSON object")

        await self._emit_dynamic_event(run_id, {
            **event,
            "data": {
                **event_data,
                "run_id": run_id,
            },
        })
        return self.get_dynamic_run(run_id).event_log[-1]

    async def update_dynamic_run_metadata(self, run_id:str, metadata:Dict[str,Any]):
        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        updated = dynamic_run.update_metadata(metadata)
        self._persist_dynamic_run(run_id)
        return updated

    async def upsert_dynamic_permission_request(self, run_id:str, request:Dict[str,Any]):
        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        if not isinstance(request, dict):
            raise ValueError("permission request must be a JSON object")
        request_id = str(request.get("request_id") or request.get("id") or "").strip()
        if not request_id:
            raise ValueError("permission request_id is required")
        requests_map = dict(dynamic_run.metadata.get("permission_requests") or {})
        existing = requests_map.get(request_id) if isinstance(requests_map.get(request_id), dict) else {}
        now = time.time()
        normalized = {
            **existing,
            **request,
            "request_id": request_id,
            "status": str(request.get("status") or existing.get("status") or "pending"),
            "created_time": existing.get("created_time") or now,
            "updated_time": now,
        }
        requests_map[request_id] = normalized
        pending = [
            item
            for item in requests_map.values()
            if isinstance(item, dict) and item.get("status") == "pending"
        ]
        dynamic_run.update_metadata({
            "permission_requests": requests_map,
            "pending_permission_request_count": len(pending),
        })
        await self._emit_dynamic_event(run_id, {
            "type": "agent_permission_request_created",
            "data": normalized,
        })
        return normalized

    async def decide_dynamic_permission_request(self, run_id:str, request_id:str, decision:Dict[str,Any]):
        await self._refresh_dynamic_timeout(run_id)
        dynamic_run = self.get_dynamic_run(run_id)
        request_key = str(request_id or "").strip()
        if not request_key:
            raise ValueError("permission request_id is required")
        requests_map = dict(dynamic_run.metadata.get("permission_requests") or {})
        request_payload = requests_map.get(request_key)
        if not isinstance(request_payload, dict):
            raise ValueError(f"Permission request not found: {request_key}")
        if request_payload.get("status") != "pending":
            return request_payload
        action = str((decision or {}).get("action") or "").strip().lower()
        if action not in {"allow", "deny"}:
            raise ValueError("permission decision action must be allow or deny")
        now = time.time()
        decided = {
            **request_payload,
            "status": "allowed" if action == "allow" else "denied",
            "decision": {
                "action": action,
                "reason": str((decision or {}).get("reason") or "").strip(),
                "decided_by": str((decision or {}).get("decided_by") or "user").strip() or "user",
                "decided_time": now,
            },
            "updated_time": now,
        }
        requests_map[request_key] = decided
        pending = [
            item
            for item in requests_map.values()
            if isinstance(item, dict) and item.get("status") == "pending"
        ]
        dynamic_run.update_metadata({
            "permission_requests": requests_map,
            "pending_permission_request_count": len(pending),
        })
        await self._emit_dynamic_event(run_id, {
            "type": "agent_permission_request_decided",
            "data": decided,
        })
        return decided

    async def delete_dynamic_run(self, run_id:str):
        snapshot = await self.get_dynamic_run_snapshot(run_id)
        if snapshot.get("status") not in TERMINAL_DYNAMIC_RUN_STATUSES:
            raise ValueError("Only terminal dynamic runs can be deleted")

        self.dynamic_runs.pop(run_id, None)
        self.async_que.pop(run_id, None)
        self.dynamic_run_store.delete_run(run_id)
        return {"run_id": run_id, "deleted": True}

    async def cleanup_dynamic_runs(
        self,
        statuses: List[str] | None = None,
        older_than_days: int | float | None = None,
        dry_run: bool = True,
    ):
        cleanup_result = self.dynamic_run_store.cleanup(
            statuses=statuses,
            older_than_days=older_than_days,
            dry_run=dry_run,
        )
        if not dry_run:
            for run_id in cleanup_result.get("deleted_run_ids", []):
                self.dynamic_runs.pop(run_id, None)
                self.async_que.pop(run_id, None)
        return cleanup_result

    def _stop_dynamic_runtime(self, run_id:str):
        message = {"type":"stop_workflow","data":{"workflow_id":run_id}}
        self._send_scheduler_message(message)

    async def _refresh_dynamic_timeout(self, run_id:str) -> bool:
        dynamic_run = self.get_dynamic_run(run_id)
        if not dynamic_run.mark_timed_out_if_needed():
            return False

        await self._emit_dynamic_event(run_id, {
            "type": "timeout_dynamic_run",
            "data": {
                "run_id": run_id,
                "timeout_seconds": dynamic_run.timeout_seconds,
            },
        })
        self._stop_dynamic_runtime(run_id)
        return True

    def _dynamic_task_run_payload(self, dynamic_run: DynamicRun, task: CodeTask):
        data = task.to_json()
        data['workflow_id'] = task.workflow_id
        file_context = dynamic_run.file_context
        if file_context and file_context.get("enabled"):
            parent_task_ids = sorted(dynamic_run.task_parents.get(task.task_id, set()))
            parent_file_manifests = [
                dynamic_run.task_file_manifests[parent_task_id]
                for parent_task_id in parent_task_ids
                if parent_task_id in dynamic_run.task_file_manifests
            ]
            data["file_context"] = {
                **file_context,
                "enabled": True,
                "run_id": file_context.get("run_id") or dynamic_run.run_id,
                "submit_id": dynamic_run.run_id,
                "task_id": task.task_id,
                "parent_task_ids": parent_task_ids,
                "parent_file_manifests": parent_file_manifests,
            }
        return data

    def _submit_dynamic_task(self, task: CodeTask):
        dynamic_run = self.get_dynamic_run(task.workflow_id)
        data = self._dynamic_task_run_payload(dynamic_run, task)
        data['priority'] = 0
        message = {
            "type":"run_task",
            "data": data,
        }
        self._send_scheduler_message(message)

    async def _emit_dynamic_event(self, run_id:str, event:Dict[str,Any]):
        dynamic_run = self.get_dynamic_run(run_id)
        stored_event = dynamic_run.append_event(event)
        snapshot = dynamic_run.snapshot(lambda result: summarize_task_result(result, run_id=run_id))
        self.dynamic_run_store.append_event(run_id, stored_event, snapshot=snapshot)
        self.dynamic_run_store.save_run(snapshot)
        que = self.async_que.get(run_id)
        if que is not None:
            await que.put({"type": "dynamic_event"})

    def _persist_dynamic_run(self, run_id:str):
        dynamic_run = self.get_dynamic_run(run_id)
        self.dynamic_run_store.save_run(
            dynamic_run.snapshot(lambda result: summarize_task_result(result, run_id=run_id))
        )

    def _persist_static_run(self, run_id:str):
        static_run = self.static_runs.get(run_id)
        if static_run is not None:
            self.static_run_store.save_run(static_run.snapshot())

    def _record_static_event(self, run_id:str, event:Dict[str,Any]):
        static_run = self.static_runs.get(run_id)
        if static_run is None:
            return event
        stored_event = static_run.append_event(event)
        self.static_run_store.append_event(run_id, stored_event)
        self._persist_static_run(run_id)
        return stored_event

    def _get_static_run_snapshot(self, run_id:str):
        if run_id in self.static_runs:
            return self.static_runs[run_id].snapshot()
        return self.static_run_store.load_run(run_id)

    def _get_static_run_events(self, run_id:str, after:int|None=None):
        if run_id in self.static_runs:
            return self.static_runs[run_id].get_events(after)
        self.static_run_store.load_run(run_id)
        return self.static_run_store.load_events(run_id, after)

    def _get_static_run_tasks(self, run_id:str):
        snapshot = self._get_static_run_snapshot(run_id)
        return list((snapshot.get("task_nodes") or {}).values())

    def _get_static_run_task(self, run_id:str, task_id:str):
        snapshot = self._get_static_run_snapshot(run_id)
        task_nodes = snapshot.get("task_nodes") or {}
        if task_id not in task_nodes:
            raise ValueError(f"Task not found in run {run_id}: {task_id}")
        return task_nodes[task_id]

    def get_global_metrics_snapshot(self) -> Dict[str, Any]:
        return self.global_metrics.snapshot(
            workflows_in_memory=max(0, len(self.workflows) - len(self.submit_workflows)),
            runs_in_memory=len(self.submit_workflows),
        )

    def list_static_runs(
        self,
        status: str | None = None,
        limit: int | None = None,
    ) -> List[Dict[str, Any]]:
        store_ids = set()
        result: List[Dict[str, Any]] = []
        for snapshot in self.static_run_store.list_runs(summary=True):
            if status and snapshot.get("status") != status:
                continue
            store_ids.add(snapshot.get("run_id"))
            result.append(snapshot)
        for run_id, static_run in self.static_runs.items():
            if run_id in store_ids:
                continue
            snapshot = static_run_summary(static_run.snapshot())
            if status and snapshot.get("status") != status:
                continue
            result.append(snapshot)
        result.sort(key=lambda item: item.get("created_time") or 0, reverse=True)
        if limit is not None:
            result = result[: max(0, int(limit))]
        return result

    def get_static_run_snapshot(self, run_id: str) -> Dict[str, Any]:
        return self._get_static_run_snapshot(run_id)

    def get_static_current_task(self, run_id: str) -> Dict[str, Any]:
        snapshot = self._get_static_run_snapshot(run_id)
        task_nodes = snapshot.get("task_nodes") or {}
        running = [
            {
                "task_id": task.get("task_id"),
                "task_name": task.get("task_name"),
                "started_time": task.get("started_time"),
                "node_id": (task.get("selected_node") or {}).get("node_id"),
            }
            for task in task_nodes.values()
            if task.get("status") == "running"
        ]
        task_counts = snapshot.get("task_counts") or {}
        return {
            "run_id": run_id,
            "status": snapshot.get("status"),
            "running": running,
            "pending_count": task_counts.get("queued", 0) + task_counts.get("pending", 0),
            "done_count": (
                task_counts.get("succeeded", 0)
                + task_counts.get("failed", 0)
                + task_counts.get("cancelled", 0)
            ),
            "task_total": task_counts.get("total", 0),
        }

    def _normalize_dynamic_run_snapshot(self, snapshot:Dict[str,Any]):
        status_map = {
            "created": "created",
            "running": "running",
            "finalized": "succeeded",
            "failed": "failed",
            "canceled": "cancelled",
            "timed_out": "timed_out",
            "interrupted": "interrupted",
        }
        normalized = copy.deepcopy(snapshot)
        normalized["kind"] = "dynamic"
        normalized["run_type"] = "dynamic"
        normalized["native_status"] = snapshot.get("status")
        normalized["status"] = status_map.get(snapshot.get("status"), snapshot.get("status"))
        normalized["result_summary"] = snapshot.get("final_result")
        normalized["error_summary"] = snapshot.get("failure_reason") or snapshot.get("cancel_reason")
        return normalized

    def _normalize_dynamic_run_summary(self, snapshot:Dict[str,Any]):
        normalized = self._normalize_dynamic_run_snapshot(snapshot)
        normalized["summary"] = True
        return normalized

    async def list_runs(
        self,
        status: str | None = None,
        kind: str | None = None,
        limit: int | None = None,
        detail: bool = False,
    ):
        runs = []
        include_static = kind in (None, "static")
        include_dynamic = kind in (None, "dynamic", "react", "agent")

        if include_static:
            static_runs = self.static_run_store.list_runs(summary=not detail)
            runs.extend(static_runs)

        if include_dynamic:
            dynamic_runs = self.dynamic_run_store.list_runs(summary=not detail)
            normalized_dynamic = [
                self._normalize_dynamic_run_snapshot(run) if detail else self._normalize_dynamic_run_summary(run)
                for run in dynamic_runs
            ]
            if kind in {"react", "agent"}:
                normalized_dynamic = [
                    run for run in normalized_dynamic
                    if run.get("mode") == kind or run.get("run_type") == kind
                ]
            runs.extend(normalized_dynamic)

        if status:
            runs = [run for run in runs if run.get("status") == status or run.get("native_status") == status]

        runs.sort(key=lambda item: item.get("created_time") or 0, reverse=True)
        if limit is not None:
            runs = runs[: max(0, limit)]
        return runs

    async def get_run_snapshot(self, run_id:str):
        try:
            return self._get_static_run_snapshot(run_id)
        except Exception:
            pass

        if run_id in self.dynamic_runs:
            return self._normalize_dynamic_run_snapshot(
                self.get_dynamic_run(run_id).snapshot(
                    lambda result: summarize_task_result(result, run_id=run_id)
                )
            )
        return self._normalize_dynamic_run_snapshot(self.dynamic_run_store.load_run(run_id))

    async def get_run_events(self, run_id:str, after:int|None=None):
        try:
            return self._get_static_run_events(run_id, after)
        except Exception:
            pass

        if run_id in self.dynamic_runs:
            await self._refresh_dynamic_timeout(run_id)
            return self.get_dynamic_run(run_id).get_events(after)
        self.dynamic_run_store.load_run(run_id)
        return self.dynamic_run_store.load_events(run_id, after)

    async def get_run_tasks(self, run_id:str):
        try:
            return self._get_static_run_tasks(run_id)
        except Exception:
            pass

        snapshot = await self.get_run_snapshot(run_id)
        task_nodes = snapshot.get("task_nodes") or {}
        return list(task_nodes.values())

    async def get_run_task(self, run_id:str, task_id:str):
        try:
            return self._get_static_run_task(run_id, task_id)
        except Exception:
            pass

        snapshot = await self.get_run_snapshot(run_id)
        task_nodes = snapshot.get("task_nodes") or {}
        if task_id not in task_nodes:
            raise ValueError(f"Task not found in run {run_id}: {task_id}")
        return task_nodes[task_id]

    def _artifacts_from_task_snapshot(self, run_id: str, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        manifest = task.get("file_manifest") or {}
        artifacts = []
        for file_info in manifest.get("files") or []:
            artifact = copy.deepcopy(file_info)
            artifact.setdefault("run_id", run_id)
            artifact.setdefault("task_id", task.get("task_id") or manifest.get("task_id"))
            artifact.setdefault("producer_task_id", artifact.get("task_id"))
            artifacts.append(artifact)
        return artifacts

    def _artifacts_from_run_events(self, run_id: str, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        artifacts = []
        seen = set()

        def add_artifact(artifact: Dict[str, Any], data: Dict[str, Any] | None = None):
            artifact = copy.deepcopy(artifact)
            if not artifact:
                return
            if not artifact.get("sha256") or not artifact.get("path"):
                return
            data = data or {}
            artifact.setdefault("run_id", run_id)
            artifact.setdefault("task_id", data.get("task_id"))
            artifact.setdefault("producer_task_id", artifact.get("task_id"))
            dedupe_key = (
                artifact.get("sha256"),
                artifact.get("task_id"),
                artifact.get("path"),
            )
            if dedupe_key in seen:
                return
            seen.add(dedupe_key)
            artifacts.append(artifact)

        def walk_result_summary(value: Any, data: Dict[str, Any] | None = None):
            if isinstance(value, dict):
                artifact = value.get("artifact")
                if isinstance(artifact, dict):
                    add_artifact(artifact, data)
                for item in value.values():
                    walk_result_summary(item, data)
            elif isinstance(value, list):
                for item in value:
                    walk_result_summary(item, data)

        for event in events:
            data = event.get("data") or {}
            if event.get("type") == "agent_tool_output_artifact":
                add_artifact(data.get("artifact") or {}, data)
                continue
            if event.get("type") in {"finish_task", "finish_workflow"}:
                walk_result_summary(data.get("result"), data)
        return artifacts

    async def get_run_artifacts(self, run_id:str):
        snapshot = await self.get_run_snapshot(run_id)
        task_nodes = snapshot.get("task_nodes") or {}
        events = await self.get_run_events(run_id)
        artifacts = []
        for task in task_nodes.values():
            artifacts.extend(self._artifacts_from_task_snapshot(run_id, task))
        artifacts.extend(self._artifacts_from_run_events(run_id, events))
        artifacts.sort(key=lambda item: (item.get("task_id") or "", item.get("path") or ""))
        return artifacts

    async def get_run_task_artifacts(self, run_id:str, task_id:str):
        task = await self.get_run_task(run_id, task_id)
        artifacts = self._artifacts_from_task_snapshot(run_id, task)
        events = await self.get_run_events(run_id)
        artifacts.extend([
            artifact
            for artifact in self._artifacts_from_run_events(run_id, events)
            if artifact.get("task_id") == task_id or artifact.get("producer_task_id") == task_id
        ])
        artifacts.sort(key=lambda item: item.get("path") or "")
        return artifacts

    def _event_log_line(self, event:Dict[str,Any]) -> Dict[str,Any]:
        data = event.get("data") or {}
        task_id = data.get("task_id")
        message = data.get("pending_reason") or data.get("reason")
        if not message:
            error = data.get("error") or data.get("result")
            if isinstance(error, dict):
                message = error.get("message") or error.get("error_type")
            elif error:
                message = str(error)
        if not message:
            if event.get("type") == "start_task":
                message = "task started"
            elif event.get("type") == "finish_task":
                message = "task finished"
            elif event.get("type") == "finish_workflow":
                message = "run finished"
            else:
                message = event.get("type")
        return {
            "timestamp": event.get("timestamp"),
            "seq": event.get("seq"),
            "stream": "event",
            "task_id": task_id,
            "type": event.get("type"),
            "message": message,
        }

    def _read_artifact_text(self, artifact:Dict[str,Any], max_bytes:int=256_000) -> str | None:
        storage_path = artifact.get("storage_path")
        source_path = Path(storage_path) if storage_path else None
        if source_path is None and artifact.get("sha256"):
            source_path = LocalCASArtifactStore().blob_path(artifact["sha256"])
        if source_path is None or not source_path.is_file():
            return None
        with source_path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        return data.decode("utf-8", errors="replace")

    async def get_run_logs(self, run_id:str, tail:int|None=500, task_id:str|None=None):
        events = await self.get_run_events(run_id)
        artifacts = await self.get_run_artifacts(run_id)
        lines = []

        for event in events:
            line = self._event_log_line(event)
            if task_id and line.get("task_id") not in (None, task_id):
                continue
            lines.append(line)

        log_artifacts = [
            artifact for artifact in artifacts
            if (not task_id or artifact.get("task_id") == task_id or artifact.get("producer_task_id") == task_id)
            and str(artifact.get("path") or "").startswith("logs/")
            and str(artifact.get("path") or "").rsplit("/", 1)[-1].startswith("maze-command")
        ]

        for artifact in sorted(log_artifacts, key=lambda item: item.get("path") or ""):
            text = self._read_artifact_text(artifact)
            if text is None:
                continue
            stream = "stderr" if str(artifact.get("path", "")).endswith(".stderr") else "stdout"
            if str(artifact.get("path", "")).endswith(".json"):
                stream = "metadata"
            for index, content in enumerate(text.splitlines()):
                lines.append({
                    "timestamp": None,
                    "seq": None,
                    "stream": stream,
                    "task_id": artifact.get("task_id") or artifact.get("producer_task_id"),
                    "path": artifact.get("path"),
                    "line": index + 1,
                    "message": content,
                })

        if tail is not None and tail >= 0:
            lines = lines[-tail:]
        return {
            "run_id": run_id,
            "task_id": task_id,
            "line_count": len(lines),
            "lines": lines,
        }

    async def _handle_dynamic_scheduler_event(self, message:Dict[str,Any]):
        run_id = message["data"]["workflow_id"]
        task_id = message["data"]["task_id"]
        dynamic_run = self.get_dynamic_run(run_id)
        message_type = message["type"]

        if dynamic_run.is_terminal():
            return

        if message_type == "start_task":
            dynamic_run.mark_started(task_id)
            await self._emit_dynamic_event(run_id, message)
            return

        if message_type == "finish_task":
            await self._emit_dynamic_event(run_id, message)
            dynamic_run.set_task_file_manifest(task_id, message.get("data", {}).get("file_manifest"))
            ready_tasks = dynamic_run.mark_finished(task_id)
            self._persist_dynamic_run(run_id)
            for ready_task in ready_tasks:
                await self._emit_dynamic_event(run_id, {
                    "type": "task_ready",
                    "data": {
                        "run_id": run_id,
                        "task_id": ready_task.task_id,
                        "task_name": ready_task.task_name,
                    },
                })
                self._submit_dynamic_task(ready_task)
            return

        if message_type == "task_pending":
            await self._emit_dynamic_event(run_id, message)
            return

        if message_type == "task_retry":
            dynamic_run.mark_retrying(task_id, message.get("data", {}).get("error"))
            await self._emit_dynamic_event(run_id, message)
            return

        if message_type == "task_exception":
            error = message.get("data", {}).get("error", message.get("data", {}).get("result"))
            dynamic_run.set_task_file_manifest(task_id, message.get("data", {}).get("file_manifest"))
            dynamic_run.mark_failed(task_id, error)
            await self._emit_dynamic_event(run_id, message)
            return
        
    def get_ray_head_port(self):
        '''
        Get the ray head port.

        '''
        return self.ray_head_port

    async def get_cluster_resources(self, timeout: float = 5.0):
        request_id = str(uuid.uuid4())
        response_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.cluster_resource_requests[request_id] = response_queue

        message = {
            "type": "get_cluster_resources",
            "data": {
                "request_id": request_id,
            },
        }
        self._send_scheduler_message(message)

        try:
            return await asyncio.wait_for(response_queue.get(), timeout=timeout)
        finally:
            self.cluster_resource_requests.pop(request_id, None)

    async def get_cluster_queues(self, timeout: float = 5.0):
        request_id = str(uuid.uuid4())
        response_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.cluster_queue_requests[request_id] = response_queue

        message = {
            "type": "get_cluster_queues",
            "data": {
                "request_id": request_id,
            },
        }
        self._send_scheduler_message(message)

        try:
            return await asyncio.wait_for(response_queue.get(), timeout=timeout)
        finally:
            self.cluster_queue_requests.pop(request_id, None)
    
    async def start_worker(self,node_ip:str,node_id:str,resources:Dict, capabilities: Dict | None = None, timeout: float = 5.0):
        request_id = str(uuid.uuid4())
        response_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.worker_registration_requests[request_id] = response_queue
        message = {
            "type":"start_worker",
            "data":{
                "request_id":request_id,
                "node_ip":node_ip,
                "node_id":node_id,
                "resources":resources,
                "capabilities":capabilities or {"workspace_sandbox": True, "docker_sandbox": False},
            }
        }
        self._send_scheduler_message(message)
        try:
            return await asyncio.wait_for(response_queue.get(), timeout=timeout)
        finally:
            self.worker_registration_requests.pop(request_id, None)

    def init(self,ray_head_port,strategy):
        '''
        Initialize.
        '''
        self.strategy = strategy
        self.ray_head_port = ray_head_port
        available_ports = get_available_ports(2)
      
        port1 = available_ports[0]
        port2 = available_ports[1]
        
        #Create the scheduler process and wait for it to be ready
        self.ready_queue = mp.Queue()
        self.scheduler_process = mp.Process(target=scheduler_process, args=(port1,port2,self.strategy,self.ray_head_port,self.ready_queue))
        self.scheduler_process.start()

        self.send_context = zmq.Context()
        self.socket_to_scheduler = self.send_context.socket(zmq.DEALER)
        self.socket_to_scheduler.connect(f"tcp://127.0.0.1:{port1}")

        self.context = zmq.asyncio.Context()
        self.socket_from_scheduler = self.context.socket(zmq.ROUTER)
        self.socket_from_scheduler.bind(f"tcp://127.0.0.1:{port2}")

        message = self.ready_queue.get()
        if message == 'ready':
            pass
        else:
            raise Exception('scheduler process error')
 
    async def monitor_coroutine(self):
        '''
        Monitor the task from the scheduler process.
        '''
        while True:
            try:
                frames = await self.socket_from_scheduler.recv_multipart()
                assert(len(frames)==2)
                _, data = frames
                message = json.loads(data.decode('utf-8'))
 
                message_type = message["type"]
                message_data = message.get("data", {})
              
                async with self.lock:
                    if message_type == "cluster_resources":
                        request_id = message_data.get("request_id")
                        response_queue = self.cluster_resource_requests.get(request_id)
                        if response_queue is not None:
                            await response_queue.put(message_data.get("resources", {}))
                        continue

                    if message_type == "cluster_queues":
                        request_id = message_data.get("request_id")
                        response_queue = self.cluster_queue_requests.get(request_id)
                        if response_queue is not None:
                            await response_queue.put(message_data.get("queues", {}))
                        continue

                    if message_type == "worker_started":
                        request_id = message_data.get("request_id")
                        response_queue = self.worker_registration_requests.get(request_id)
                        if response_queue is not None:
                            await response_queue.put(message_data.get("worker", {}))
                        continue

                    if(message_type=="finish_task"):
                        if message_data["task_id"] in self.async_que: #langgraph task
                            que: Queue[Any] = self.async_que[message_data['task_id']]
                            await que.put(message)
                        else:
                            submit_id = message_data['workflow_id']
                            if submit_id in self.dynamic_runs:
                                await self._handle_dynamic_scheduler_event(message)
                                continue
                            if submit_id not in self.async_que or submit_id not in self.submit_workflows:
                                continue

                            static_run = self.static_runs.get(submit_id)
                            task_metrics = message_data.get("metrics") or {}
                            if static_run is not None:
                                static_run.mark_task_finished(
                                    message_data["task_id"],
                                    result=message_data.get("result"),
                                    file_manifest=message_data.get("file_manifest"),
                                    metrics=task_metrics,
                                    started_at=message_data.get("started_at"),
                                    finished_at=message_data.get("finished_at"),
                                    duration_ms=message_data.get("duration_ms"),
                                    node_id=message_data.get("node_id"),
                                )
                                message = self._record_static_event(submit_id, message)

                            self.global_metrics.on_task_finished(
                                submit_id,
                                message_data["task_id"],
                                "succeeded",
                                task_metrics,
                            )

                            que: Queue[Any] = self.async_que[submit_id]
                            await que.put(message)
 
                            if message_data.get("file_manifest"):
                                self.submit_workflows[submit_id].graph.nodes[message_data["task_id"]]["file_manifest"] = message_data["file_manifest"]

                            new_ready_tasks  = self.submit_workflows[submit_id].finish_task(task_id=message_data["task_id"],task_result=message_data["result"],strategy=self.strategy)
                            if len(new_ready_tasks) > 0:
                                for task in new_ready_tasks:
                                    if static_run is not None:
                                        static_run.mark_task_queued(task.task_id)
                                        self._persist_static_run(submit_id)
                                    file_context = self.submit_workflows[submit_id].graph.graph.get("file_context")
                                    data = self._task_run_payload(
                                        self.submit_workflows[submit_id],
                                        task,
                                        submit_id,
                                        file_context,
                                    )
                                    data['priority'] = await self._get_task_priority(
                                        self.submit_workflows[submit_id],
                                        task,
                                    )
                                    message = {
                                        "type":"run_task",
                                        "data":data,
                                    }                 
                                    self._send_scheduler_message(message)

                            if static_run is not None and static_run.status == "succeeded":
                                self.global_metrics.on_run_status_change(
                                    submit_id,
                                    "running",
                                    "succeeded",
                                )
                                finish_message = self._record_static_event(submit_id, {
                                    "type": "finish_workflow",
                                    "data": {
                                        "run_id": submit_id,
                                        "workflow_id": static_run.workflow_id,
                                    },
                                })
                                await que.put(finish_message)
                                clear_message = {"type":"clear_workflow","data":{"workflow_id":submit_id}}
                                self._send_scheduler_message(clear_message)

                    elif(message_type=="start_task" or message_type=="task_pending" or message_type=="task_retry" or message_type=="task_exception"):
                        if message_data["task_id"] in self.async_que: #langgraph task
                            que: Queue[Any] = self.async_que[message_data['task_id']]
                            await que.put(message)
                        else:
                            submit_id = message_data['workflow_id']
                            if submit_id in self.dynamic_runs:
                                await self._handle_dynamic_scheduler_event(message)
                                continue
                            if submit_id not in self.async_que or submit_id not in self.submit_workflows:
                                continue

                            static_run = self.static_runs.get(submit_id)
                            if message_type == "start_task":
                                self.submit_workflows[submit_id].mark_task_started(message_data["task_id"])
                                if static_run is not None:
                                    if static_run.status == "created":
                                        self.global_metrics.on_run_status_change(
                                            submit_id,
                                            "submitted",
                                            "running",
                                        )
                                    static_run.mark_task_started(message_data["task_id"], message_data)
                                    message = self._record_static_event(submit_id, message)
                                self.global_metrics.on_task_started(
                                    submit_id,
                                    message_data["task_id"],
                                )
                            elif message_type == "task_pending":
                                if static_run is not None:
                                    static_run.mark_task_pending(
                                        message_data["task_id"],
                                        message_data.get("pending_reason"),
                                        message_data.get("schedule_decision"),
                                    )
                                    message = self._record_static_event(submit_id, message)
                            elif message_type == "task_retry":
                                if static_run is not None:
                                    static_run.mark_task_retry(
                                        message_data["task_id"],
                                        message_data.get("error"),
                                        message_data.get("attempt"),
                                    )
                                    message = self._record_static_event(submit_id, message)
                            elif message_type == "task_exception":
                                if static_run is not None:
                                    error = message_data.get("error", message_data.get("result"))
                                    static_run.mark_task_failed(
                                        message_data["task_id"],
                                        error,
                                        message_data.get("file_manifest"),
                                    )
                                    message = self._record_static_event(submit_id, message)
                                    if static_run.status == "failed":
                                        self.global_metrics.on_run_status_change(
                                            submit_id,
                                            "running",
                                            "failed",
                                        )
                                self.global_metrics.on_task_finished(
                                    submit_id,
                                    message_data["task_id"],
                                    "failed",
                                    None,
                                )
    
                            que: Queue[Any] = self.async_que[submit_id]
                            await que.put(message)
                    
                    elif(message_type=="finish_llm_instance_launch"):
                        que: Queue[Any] = self.llm_instance_async_que[message_data['instance_id']]
                        await que.put(message_data)

            except Exception as e:
                print(f"Error in monitor: {e}")
      
    async def get_workflow_res(self,workflow_id:str,submit_id:str,websocket:WebSocket):    
        """
        Get the workflow result and send to websocket.
        """
        que = self.async_que[submit_id]
        assert que != None

        while True:
            data = await que.get()
            await websocket.send_json(data)

            if data["type"]=="finish_workflow":
                message = {"type":"clear_workflow","data":{"workflow_id":submit_id}}
                self._send_scheduler_message(message)
                break
            elif data["type"]=="task_exception":
                raise Exception("task_exception")

    async def get_dynamic_run_res(self, run_id:str, websocket:WebSocket):
        dynamic_run = self.get_dynamic_run(run_id)
        que = self.async_que[run_id]
        event_index = 0

        while True:
            await self._refresh_dynamic_timeout(run_id)
            while event_index < len(dynamic_run.event_log):
                event = dynamic_run.event_log[event_index]
                event_index += 1
                await websocket.send_json(event)
                if event["type"] in ("finish_workflow", "task_exception", "cancel_dynamic_run", "timeout_dynamic_run", "interrupt_dynamic_run"):
                    if dynamic_run.is_terminal():
                        return

            if dynamic_run.is_terminal():
                return

            timeout = dynamic_run.seconds_until_timeout()
            try:
                await asyncio.wait_for(que.get(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._refresh_dynamic_timeout(run_id)
          
    async def stop_workflow(self,submit_id:str):
        '''
        Stop workflow
        '''
        async with self.lock:
            self.async_que.pop(submit_id, None)
            static_run = self.static_runs.get(submit_id)
            if static_run is not None:
                static_run.mark_cancelled("Workflow stopped")
                self.global_metrics.on_run_status_change(submit_id, "running", "canceled")
                self._record_static_event(submit_id, {
                    "type": "cancel_workflow",
                    "data": {
                        "run_id": submit_id,
                        "workflow_id": static_run.workflow_id,
                        "reason": "Workflow stopped",
                    },
                })

        message = {"type":"stop_workflow","data":{"workflow_id":submit_id}}
        self._send_scheduler_message(message)
    
    async def run_langgraph_task(self,workflow_id:str,task_id:str,args:str,kwargs:str):
        """
        Run langgraph task
        """
        que: Queue[Any] = asyncio.Queue()
        self.async_que[task_id] = que #we use task_id in langgraph task

        task: LangGraphTask = self.workflows[workflow_id].get_task(task_id)
        task.set_args(args)
        task.set_kwargs(kwargs)
        data = task.to_json()
        data['priority'] = 0
        message: dict[str, str] = {
            "type":"run_task",
            "data":data,
         }
        self._send_scheduler_message(message)

        result = None
        while True:
            message = await que.get()
            message_type = message["type"]
            message_data = message["data"]
            
            if message_type=="finish_task":
                result = message_data["result"]
                break              
            elif message_type=="task_exception":
                result = message_data["result"]
                break

        del self.async_que[task_id]
        return result
    
    async def start_llm_instance(self, instance_id:str, model: str, cpu_nums: int, gpu_nums: int, memory:int, gpu_mem: int):
        self.llm_instance_async_que[instance_id] = asyncio.Queue()

        message = {"type":"start_llm_instance","data":{"instance_id":instance_id,"model":model,"cpu_nums":cpu_nums,"gpu_nums":gpu_nums,"gpu_mem":gpu_mem,"memory":memory}}
        self._send_scheduler_message(message)

        while True:
            data = await self.llm_instance_async_que[instance_id].get()
            del self.llm_instance_async_que[instance_id]
            return data['host'],data['port']

    async def stop_llm_instance(self, instance_id:str):
        message = {"type":"stop_llm_instance","data":{"instance_id":instance_id}}
        self._send_scheduler_message(message)
