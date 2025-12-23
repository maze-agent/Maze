import time
from typing import Any,Dict
from enum import Enum
from maze.core.predictor.predictor import FEATURES

class TaskType(Enum):
    CODE = "code"
    LANGGRAPH = "langgraph"

class CodeTask():
    def __init__(self,workflow_id:str,task_id:str,task_name:str):
        self.task_type = TaskType.CODE.value
        self.workflow_id = workflow_id
        self.task_id = task_id
        self.task_name=task_name

        self.resources = None
        self.task_input = None
        self.task_output = None
        self.code_str = None
        self.code_ser = None

        self.completed = False
        
        self.start_time = time.time()
        self.finish_time = None
        self.can_predict = False
        if self.task_name in FEATURES:
            self.can_predict = True
            self.predict_feature = {}
            predict_feature_list = FEATURES[self.task_name]
            for feature in predict_feature_list:
                self.predict_feature[feature] = 0 #default value
            
    def set_predict_feature(self, feature: str, value: float):
        if feature in self.predict_feature:
            self.predict_feature[feature] = value

    def save_task(self,task_input:Dict, task_output:Dict, code_str:str,code_ser:str,resources:Dict):
        '''save task info'''
        
        self.task_input=task_input
        self.task_output=task_output
        self.code_str=code_str
        self.code_ser=code_ser
        self.resources=resources
    
    def to_json(self) -> Dict[str, Any]:
        return {
            "task_type":self.task_type,
            "workflow_id":self.workflow_id,
            "task_id":self.task_id,
            "task_name":self.task_name,
            "task_input":self.task_input,
            "task_output":self.task_output,
            "resources":self.resources,
            "code_str":self.code_str,
            "code_ser":self.code_ser,
        }

class LangGraphTask():
    def __init__(self,workflow_id:str,task_id:str,task_name:str,code_ser:str,resources:Dict):
        self.task_type = TaskType.LANGGRAPH.value
        self.workflow_id = workflow_id
        self.task_id = task_id
        self.task_name=task_name
        self.code_ser = code_ser
        self.resources = resources

        self.args = None
        self.kwargs = None
    
    def set_args(self,args):
        self.args = args
    
    def set_kwargs(self,kwargs):
        self.kwargs = kwargs
                
    def to_json(self) -> Dict[str, Any]:
        return {
            "task_type":self.task_type,
            "workflow_id":self.workflow_id,
            "task_id":self.task_id,
            "task_name":self.task_name,
            "resources":self.resources,
            "code_ser":self.code_ser,
            "args":self.args,
            "kwargs":self.kwargs
        }