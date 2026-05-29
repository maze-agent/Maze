
import ray
import cloudpickle
import time
from typing import Any, List,Dict,Callable
from maze.core.scheduler.runner import remote_task_runner,remote_lgraph_task_runner


DEFAULT_MAX_RETRIES = 1
DEFAULT_RETRY_ON = ("node_lost", "resource_unavailable")

class SelectedNode():
    def __init__(self,node_id:str,node_ip:str,gpu_id:int=None):
        self.node_id = node_id
        self.node_ip = node_ip
        self.gpu_id = gpu_id

def _normalize_max_retries(max_retries: int | None) -> int:
    if max_retries is None:
        return DEFAULT_MAX_RETRIES
    return max(0, int(max_retries))


def _normalize_retry_on(retry_on: List[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if retry_on is None:
        return DEFAULT_RETRY_ON
    return tuple(str(item) for item in retry_on)


class LanggraphTaskRuntime():
    def __init__(
        self,
        workflow_id:str,
        task_id:str,
        code_ser:str,
        args:str,
        kwargs:str,
        resources:Dict,
        max_retries:int|None=None,
        retry_backoff_seconds:float=0,
        retry_on:List[str]|None=None,
        timeout_seconds:float|None=None,
    ):
        self.status = "ready" #ready,running,finished
        self.workflow_id: str = workflow_id
        self.task_id: str = task_id
        self.code_ser: str = code_ser
        self.args: str = args
        self.kwargs: str = kwargs
        self.resources: Dict[str, Any] = resources
        self.priority = 0
        self.max_retries = _normalize_max_retries(max_retries)
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds or 0))
        self.retry_on = _normalize_retry_on(retry_on)
        self.timeout_seconds = None if timeout_seconds is None else float(timeout_seconds)
        self.attempt = 0
        self.last_error: Dict[str, Any] | None = None
        self.next_eligible_time = 0.0
        self.started_time = None
        self.created_time = time.time()
        self.pending_reason = None
        self.last_schedule_decision: Dict[str, Any] | None = None
        self.object_ref = None
        self.selected_node = None

    def set_priority(self, priority):
        self.priority = priority

    def set_task_status(self, status):
        self.status = status

    def begin_attempt(self):
        self.attempt += 1
        self.started_time = time.time()
        self.pending_reason = None

    def has_timed_out(self, now: float | None = None) -> bool:
        if self.status != "running" or self.timeout_seconds is None or self.started_time is None:
            return False
        return (now or time.time()) - self.started_time >= self.timeout_seconds

    def should_retry(self, error: Dict[str, Any]) -> bool:
        error_type = error.get("error_type")
        if error_type not in self.retry_on:
            return False
        if not error.get("retryable", False):
            return False
        return self.attempt <= self.max_retries

    def schedule_retry(self, error: Dict[str, Any]):
        self.last_error = error
        self.status = "retrying"
        self.pending_reason = None
        self.next_eligible_time = time.time() + self.retry_backoff_seconds

class TaskRuntime():
    def __init__(
        self,
        workflow_id:str,
        task_id:str,
        task_input:Dict,
        task_output:Dict,
        resources:Dict,
        code_str:str=None,
        code_ser:str=None,
        file_context:Dict|None=None,
        max_retries:int|None=None,
        retry_backoff_seconds:float=0,
        retry_on:List[str]|None=None,
        timeout_seconds:float|None=None,
    ):
        self.status = "ready" #ready,running,finished
        self.workflow_id: str = workflow_id
        self.task_id: str = task_id
        self.task_input: Dict[Any, Any] = task_input
        self.task_output: Dict[Any, Any] = task_output
        self.resources: Dict[str, Any] = resources
        self.code_str: str = code_str
        self.code_ser: str = code_ser 
        self.file_context: Dict[str, Any] | None = file_context
        self.max_retries = _normalize_max_retries(max_retries)
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds or 0))
        self.retry_on = _normalize_retry_on(retry_on)
        self.timeout_seconds = None if timeout_seconds is None else float(timeout_seconds)

        self.object_ref = None
        self.result: None|Dict[Any, Any] = None
        self.file_manifest: Dict[str, Any] | None = None
        self.selected_node = None
        self.attempt = 0
        self.last_error: Dict[str, Any] | None = None
        self.next_eligible_time = 0.0
        self.started_time = None
        self.created_time = time.time()
        self.pending_reason = None
        self.last_schedule_decision: Dict[str, Any] | None = None

        self.priority = 0
    
    def set_priority(self, priority):
        self.priority = priority

    def set_task_status(self, status):
        self.status = status

    def begin_attempt(self):
        self.attempt += 1
        self.started_time = time.time()
        self.pending_reason = None

    def has_timed_out(self, now: float | None = None) -> bool:
        if self.status != "running" or self.timeout_seconds is None or self.started_time is None:
            return False
        return (now or time.time()) - self.started_time >= self.timeout_seconds

    def should_retry(self, error: Dict[str, Any]) -> bool:
        error_type = error.get("error_type")
        if error_type not in self.retry_on:
            return False
        if not error.get("retryable", False):
            return False
        return self.attempt <= self.max_retries

    def schedule_retry(self, error: Dict[str, Any]):
        self.last_error = error
        self.status = "retrying"
        self.pending_reason = None
        self.next_eligible_time = time.time() + self.retry_backoff_seconds
        
class WorkflowRuntime():
    def __init__(self,workflow_id):
        self.workflow_id: str = workflow_id
        self.tasks: Dict[str, TaskRuntime|LanggraphTaskRuntime] = {}
        self.ref_to_taskid = {}
 
    def add_task(self, task:TaskRuntime|LanggraphTaskRuntime):
        '''
        Add task to workflow.
        '''
        if task.task_id not in self.tasks:
            self.tasks[task.task_id] = task
     
         
    def get_task_result(self,key):
        '''
        Get task result by key. # key = {task_id}.output.{task_ouput_key}
        '''
        task_id = key.split(".")[0]
        task_ouput_key = key.split(".")[2]
        return self.tasks[task_id].result.get(task_ouput_key)
    
    def add_runtime_info(self, task_id:str, object_ref, selected_node:SelectedNode):
        '''
        Add task runtime info.
        '''
        self.tasks[task_id].status = "running"
        self.tasks[task_id].object_ref = object_ref
        self.tasks[task_id].selected_node = selected_node
          
        self.ref_to_taskid[object_ref] = task_id

    def get_running_task_refs(self):
        running_task_refs = []
        for task_id,task in self.tasks.items():
            if task.status == "running":
                running_task_refs.append(task.object_ref)
        return running_task_refs

    def set_task_result(self, task:TaskRuntime,result:Dict):
        self.tasks[task.task_id].result = result
        self.tasks[task.task_id].status = "finished"

    def get_task_by_ref(self, ref) -> TaskRuntime:
        task_id = self.ref_to_taskid[ref]
        return self.tasks[task_id]

    def get_running_tasks(self):
        running_tasks = []
        for task_id,task in self.tasks.items():
            if task.status == "running":
                running_tasks.append(task)
        return running_tasks

    def clear_task_ref(self, task:TaskRuntime|LanggraphTaskRuntime):
        object_ref = getattr(task, "object_ref", None)
        if object_ref in self.ref_to_taskid:
            del self.ref_to_taskid[object_ref]
        
class WorkflowRuntimeManager():
    def __init__(self):
        self.workflows = {}
        self.ref_to_workflow_id = {}
    
    def _get_workflow_by_ref(self, ref):
        if ref not in self.ref_to_workflow_id:
            return None

        return self.workflows[self.ref_to_workflow_id[ref]]

    def clear_workflow(self, workflow_id:str):
        '''
        Clear workflow.
        '''
        if workflow_id not in self.workflows:
            return

        refs_to_del = []
        for ref,id in self.ref_to_workflow_id.items():
            if id == workflow_id:
                refs_to_del.append(ref)
        for ref in refs_to_del:
            del self.ref_to_workflow_id[ref]

        del self.workflows[workflow_id]
        
    def cancel_workflow(self, workflow_id:str):
        '''
        Cancel running tasks of workflow and return running tasks.
        '''
        if workflow_id not in self.workflows:
            return []
   
        running_tasks = self.workflows[workflow_id].get_running_tasks()
        for task in running_tasks:
            ray.cancel(task.object_ref,force=True)
 
        self.clear_workflow(workflow_id)
        return running_tasks

    def add_task(self, task:TaskRuntime|LanggraphTaskRuntime):
        '''
        Add task to workflow. If the workflow does not exist, create a new workflow.(Means that the task is the first task of the workflow)
        '''
        if task.workflow_id not in self.workflows:
            self.workflows[task.workflow_id] = WorkflowRuntime(task.workflow_id)

        self.workflows[task.workflow_id].add_task(task)
    
    def run_task(self,task:TaskRuntime|LanggraphTaskRuntime,node:SelectedNode):
        '''
        Run task in node.
        '''
        if task.workflow_id not in self.workflows:
            return 
        task.begin_attempt()

        if isinstance(task, LanggraphTaskRuntime):
            #gpu task
            if node.gpu_id is not None: 
                result_ref = remote_lgraph_task_runner.options(
                    num_cpus=task.resources["cpu"],
                    num_gpus=task.resources["gpu"],
                    memory=task.resources["cpu_mem"],
                    scheduling_strategy= ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(node_id=node.node_id, soft=False)
                ).remote(code_ser=task.code_ser,args=task.args,kwargs=task.kwargs,cuda_visible_devices=str(node.gpu_id))
            #cpu task
            else: 
                result_ref = remote_lgraph_task_runner.options(
                    num_cpus=task.resources["cpu"],
                    memory=task.resources["cpu_mem"],
                    scheduling_strategy= ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(node_id=node.node_id, soft=False)
                ).remote(code_ser=task.code_ser,args=task.args,kwargs=task.kwargs,cuda_visible_devices=None)
            
            
            self.workflows[task.workflow_id].add_runtime_info(task.task_id,result_ref,node)
            self.ref_to_workflow_id[result_ref] = task.workflow_id

        elif isinstance(task, TaskRuntime):
            task_input_data = {}
            for _,input_info in task.task_input["input_params"].items():
                if input_info["input_schema"] == "from_user":
                    if input_info.get("has_value", True):
                        task_input_data[input_info["key"]] = input_info["value"]
                elif input_info["input_schema"] == "from_task":
                    task_input_data[input_info["key"]] = self.workflows[task.workflow_id].get_task_result(input_info["value"])

            #gpu task
            if node.gpu_id is not None: 
                result_ref = remote_task_runner.options(
                    num_cpus=task.resources["cpu"],
                    num_gpus=task.resources["gpu"],
                    memory=task.resources["cpu_mem"],
                    scheduling_strategy= ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(node_id=node.node_id, soft=False)
                ).remote(
                    code_str=task.code_str,
                    code_ser=task.code_ser,
                    task_input_data=task_input_data,
                    cuda_visible_devices=str(node.gpu_id),
                    file_context=task.file_context,
                )
            #cpu task
            else: 
                result_ref = remote_task_runner.options(
                    num_cpus=task.resources["cpu"],
                    memory=task.resources["cpu_mem"],
                    scheduling_strategy= ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(node_id=node.node_id, soft=False)
                ).remote(
                    code_str=task.code_str,
                    code_ser=task.code_ser,
                    task_input_data=task_input_data,
                    cuda_visible_devices=None,
                    file_context=task.file_context,
                )
            
            
            self.workflows[task.workflow_id].add_runtime_info(task.task_id,result_ref,node)
            self.ref_to_workflow_id[result_ref] = task.workflow_id
  
    def get_running_task_refs(self):
        '''
        Get running task refs.
        '''
        running_task_refs = []
        for workflow in self.workflows.values():
            running_task_refs.extend(workflow.get_running_task_refs())
                
        return running_task_refs

    def get_running_tasks(self):
        running_tasks = []
        for workflow in self.workflows.values():
            running_tasks.extend(workflow.get_running_tasks())
        return running_tasks

    def set_task_result(self, task:TaskRuntime, result:Dict):
        '''
        Set task result.
        '''
        self.workflows[task.workflow_id].set_task_result(task,result)

    def get_task_by_ref(self,object_ref) -> TaskRuntime:
        '''
        Get task by object_ref
        '''
        workflow = self._get_workflow_by_ref(object_ref)
        if workflow is None:
            return None
        else:
            return workflow.get_task_by_ref(object_ref)

    def clear_task_ref(self, task:TaskRuntime|LanggraphTaskRuntime):
        workflow = self.workflows.get(task.workflow_id)
        if workflow is not None:
            workflow.clear_task_ref(task)
        object_ref = getattr(task, "object_ref", None)
        if object_ref in self.ref_to_workflow_id:
            del self.ref_to_workflow_id[object_ref]
 
