 


from typing import Any,Dict


class CodeTask():
    def __init__(self,workflow_id:str,task_id:str):
        self.workflow_id = workflow_id
        self.task_id = task_id
        
        self.task_input = None
        self.task_output = None
        self.code_str = None
        self.resources = None

        self.completed = False #任务是否完成
       

    def save_task(self,task_input, task_output, code_str, resources):
        '''save task info（for pending task）'''
        self.task_input=task_input
        self.task_output=task_output
        self.code_str=code_str
        self.resources=resources
    
    def to_json(self) -> Dict[str, Any]:
        return {
            "workflow_id":self.workflow_id,
            "task_id":self.task_id,
            "task_input":self.task_input,
            "task_output":self.task_output,
            "code_str":self.code_str,
            "resources":self.resources
        }

    