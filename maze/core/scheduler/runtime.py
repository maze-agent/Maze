from _thread import LockType
from re import T
from typing import Any, List,Dict
import threading
from maze.core.scheduler.runner import remote_task_runner
import ray

class TaskRuntime():
    def __init__(self,workflow_id:str,task_id:str,task_input:Dict,task_output:Dict,resources:Dict,code_str:str):
        self.status = "ready"
        self.workflow_id: str = workflow_id
        self.task_id: str = task_id
        self.task_input: Dict[Any, Any] = task_input
        self.task_output: Dict[Any, Any] = task_output
        self.resources: Dict[str, Any] = resources
        self.code_str: str = code_str

        self.object_ref = None
        self.result: None|Dict[Any, Any] = None
        self.node_id:None|str = None
        self.gpu_id:None|str = None
        
class WorkflowRuntime():
    def __init__(self,workflow_id):
        self.workflow_id: str = workflow_id
        self.to_delete = False
        self.tasks: Dict[str, TaskRuntime] = {}
        self.ref_to_taskid = {}
    
    def add_task(self, task:TaskRuntime):
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
    
    def add_runtime_info(self, task_id:str, object_ref, cheosed_node_info:Dict):
        '''
        Add task runtime info.
        '''
        self.tasks[task_id].status = "running"
        self.tasks[task_id].object_ref = object_ref
        self.tasks[task_id].node_id = cheosed_node_info['node_id']
        if 'gpu_id' in cheosed_node_info:
            self.tasks[task_id].gpu_id = cheosed_node_info['gpu_id']

        self.ref_to_taskid[object_ref] = task_id
          
    def get_running_tasks(self):
        running_tasks = []
        for task_id,task in self.tasks.items():
            if task.status == "running":
                running_tasks.append(task)
        return running_tasks

    def set_task_result(self, object_ref,result):
        task_id = self.ref_to_taskid[object_ref]
        self.tasks[task_id].result = result
        self.tasks[task_id].status = "finished"

    def get_task_by_ref(self, ref):
        task_id = self.ref_to_taskid[ref]
        return self.tasks[task_id]

class RuntimeManager():
    def __init__(self):
        self.lock: LockType = threading.Lock() #RuntimeManager对象由submit线程、monitor线程、receive线程 共同操作，需要线程安全
        self.workflows = {}
        self.ref_to_workflow_id = {}
   
    def add_task(self, task:TaskRuntime):
        with self.lock:
            if task.workflow_id not in self.workflows:
                self.workflows[task.workflow_id] = WorkflowRuntime(task.workflow_id)
            
            self.workflows[task.workflow_id].add_task(task)
    
    def run_task(self,task:TaskRuntime,choosed_node_info:Dict):
        with self.lock:
            if "gpu_id" in choosed_node_info:
                pass #todo
            else:
                task_input_data = {}
                for _,input_info in task.task_input["input_params"].items():
                    if input_info["input_schema"] == "from_user":
                        task_input_data[input_info["key"]] = input_info["value"]
                    elif input_info["input_schema"] == "from_task":
                        task_input_data[input_info["key"]] = self.workflows[task.workflow_id].get_task_result(input_info["value"])
    
                result_ref = remote_task_runner.options(
                    num_cpus=task.resources["cpu"],
                    memory=task.resources["cpu_mem"],
                    scheduling_strategy= ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(node_id=choosed_node_info["node_id"], soft=False)
                ).remote(code_str=task.code_str,task_input_data=task_input_data)
                
                self.workflows[task.workflow_id].add_runtime_info(task.task_id,result_ref,choosed_node_info)
                self.ref_to_workflow_id[result_ref] = task.workflow_id
    
    def clear_del_workflows(self):
        with self.lock:
            to_del_workflows_id = []
            for workflow_id,workflow in self.workflows.items():
                if workflow.to_delete:
                    to_del_workflows_id.append(workflow_id)
                    
                    to_delete_ref = []
                    for ref,workflow_id in self.ref_to_workflow_id.items():
                        if workflow_id == workflow.workflow_id:
                            to_delete_ref.append(ref)
                    for ref in to_delete_ref:
                        del self.ref_to_workflow_id[ref]

            for workflow_id in to_del_workflows_id:
                del self.workflows[workflow_id]  
                   
    def get_running_tasks(self):
        with self.lock:
            running_tasks = []
            for workflow in self.workflows.values():
                if not workflow.to_delete:
                    running_tasks.extend(workflow.get_running_tasks())
                
 
        return running_tasks

    def set_task_result(self, object_refs:List, results:List):
        with self.lock:
            for ref,result in zip(object_refs,results):
                workflow = self.workflows[self.ref_to_workflow_id[ref]]
                workflow.set_task_result(ref,result)

    def get_tasks_by_refs(self,object_refs:List):
        with self.lock:
            tasks = []  
            for ref in object_refs:
                workflow = self.workflows[self.ref_to_workflow_id[ref]]
                tasks.append(workflow.get_task_by_ref(ref))
            
            return tasks

    def clear_workflow(self,workflow_id:str):
        with self.lock:
            self.workflows[workflow_id].to_delete = True