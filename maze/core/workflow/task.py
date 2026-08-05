import time
from typing import Any,Dict
from maze.core.workflow.resources import DEFAULT_TASK_KIND, normalize_task_semantics

class CodeTask():
    def __init__(self,workflow_id:str,task_id:str,task_name:str):
        self.task_type = "code"
        self.workflow_id = workflow_id
        self.task_id = task_id
        self.task_name=task_name
        self.task_kind = DEFAULT_TASK_KIND

        self.resources = None
        self.task_input = None
        self.task_output = None
        self.code_str = None
        self.code_ser = None
        self.file_context = None
        self.model_anchor = None
        self.max_retries = None
        self.retry_backoff_seconds = 0
        self.retry_on = None
        self.timeout_seconds = None

        self.completed = False
        
        self.created_time = time.time()
        self.start_time = None
        self.finish_time = None

    def mark_started(self):
        self.start_time = time.time()

    def save_task(
        self,
        task_input:Dict,
        task_output:Dict,
        code_str:str,
        code_ser:str,
        resources:Dict,
        task_kind:str|None=None,
        file_context:Dict|None=None,
        model_anchor:Dict|None=None,
        max_retries:int|None=None,
        retry_backoff_seconds:float=0,
        retry_on:list[str]|None=None,
        timeout_seconds:float|None=None,
    ):
        '''save task info'''
        
        self.task_input=task_input
        self.task_output=task_output
        self.code_str=code_str
        self.code_ser=code_ser
        self.task_kind, self.resources = normalize_task_semantics(
            task_kind=task_kind,
            resources=resources,
            model_anchor=model_anchor,
        )
        self.file_context=file_context
        self.model_anchor=model_anchor
        self.max_retries=max_retries
        self.retry_backoff_seconds=retry_backoff_seconds or 0
        self.retry_on=retry_on
        self.timeout_seconds=timeout_seconds
    
    def to_json(self) -> Dict[str, Any]:
        return {
            "task_type":self.task_type,
            "workflow_id":self.workflow_id,
            "task_id":self.task_id,
            "task_name":self.task_name,
            "task_kind":self.task_kind,
            "task_input":self.task_input,
            "task_output":self.task_output,
            "resources":self.resources,
            "code_str":self.code_str,
            "code_ser":self.code_ser,
            "file_context":self.file_context,
            "model_anchor":self.model_anchor,
            "max_retries":self.max_retries,
            "retry_backoff_seconds":self.retry_backoff_seconds,
            "retry_on":self.retry_on,
            "timeout_seconds":self.timeout_seconds,
        }
