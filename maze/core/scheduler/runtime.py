
import ray
import cloudpickle
from typing import Any, List,Dict,Callable
from maze.core.scheduler.runner import remote_task_runner,remote_lgraph_task_runner

class SelectedNode():
    def __init__(self,node_id:str,node_ip:str,gpu_id:int=None):
        self.node_id = node_id
        self.node_ip = node_ip
        self.gpu_id = gpu_id

class LanggraphTaskRuntime():
    def __init__(self,workflow_id:str,task_id:str,code_ser:str,args:str,kwargs:str,resources:Dict):
        self.status = "ready"
        self.workflow_id: str = workflow_id
        self.task_id: str = task_id
        self.code_ser: str = code_ser
        self.args: str = args
        self.kwargs: str = kwargs
        self.resources: Dict[str, Any] = resources

class TaskRuntime():
    def __init__(self,workflow_id:str,task_id:str,task_input:Dict,task_output:Dict,resources:Dict,code_str:str):
        self.status = "ready" #ready,running,finished
        self.workflow_id: str = workflow_id
        self.task_id: str = task_id
        self.task_input: Dict[Any, Any] = task_input
        self.task_output: Dict[Any, Any] = task_output
        self.resources: Dict[str, Any] = resources
        self.code_str: str = code_str

        self.object_ref = None
        self.result: None|Dict[Any, Any] = None
        self.selected_node = None
        
class WorkflowRuntime():
    def __init__(self,workflow_id):
        self.workflow_id: str = workflow_id
        self.tasks: Dict[str, TaskRuntime|LanggraphTaskRuntime] = {}
        self.ref_to_taskid = {}
 
    def add_task(self, task:TaskRuntime|LanggraphTaskRuntime):
        '''
        Add task to workflow.
        '''
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
        for ref,workflow_id in self.ref_to_workflow_id.items():
            if workflow_id == workflow_id:
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
                ).remote(code_ser=task.code_ser,task_data=task.args,kwargs=task.kwargs,cuda_visible_devices=None)
            
            
            self.workflows[task.workflow_id].add_runtime_info(task.task_id,result_ref,node)
            self.ref_to_workflow_id[result_ref] = task.workflow_id

        elif isinstance(task, TaskRuntime):
            task_input_data = {}
            for _,input_info in task.task_input["input_params"].items():
                if input_info["input_schema"] == "from_user":
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
                ).remote(code_str=task.code_str,task_input_data=task_input_data,cuda_visible_devices=str(node.gpu_id))     
            #cpu task
            else: 
                result_ref = remote_task_runner.options(
                    num_cpus=task.resources["cpu"],
                    memory=task.resources["cpu_mem"],
                    scheduling_strategy= ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(node_id=node.node_id, soft=False)
                ).remote(code_str=task.code_str,task_input_data=task_input_data,cuda_visible_devices=None)
            
            
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
 