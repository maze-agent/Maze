import os
import json
import uuid
import time
import argparse
import importlib.util
import networkx as nx
from abc import ABC, abstractmethod
from typing import List, Dict, Optional

class BaseQueryLoader(ABC):
    """
    
     mlq.py  DAG 
    """
    def __init__(self, args: argparse.Namespace, dag_id: str, dag_type: str, dag_source: str, supplementary_files: List[str], sub_time: float, run_id: Optional[str]= None):
        self.args = args
        self.dag_id = dag_id
        self.run_id= run_id
        self.dag_type = dag_type
        self.dag_source = dag_source
        self.question: str= ""
        self.answer: str= ""
        self.supplementary_files = supplementary_files
        self.arrival_time= time.time()
        self.sub_time= sub_time
        self.supplementary_file_paths: Dict[str, str]= {}
    
    @abstractmethod
    def get_dag_folder(self) -> str:
        """"""
        pass

    @abstractmethod
    def get_question_answer(self) -> str:
        """"""
        pass

    @abstractmethod
    def get_supplementary_files(self) -> Dict[str, str]:
        """"""
        pass

    def get_dag(self, task_ids: Dict[str, str]) -> nx.DiGraph:
        """
         dag_folder  task_ids  networkx.DiGraph object
        """
        dag_folder = self.get_dag_folder()
        if not os.path.isdir(dag_folder):
            raise FileNotFoundError(f"DAG folder not found: {dag_folder}")

        dag_json_file = os.path.join(dag_folder, 'dag.json')
        dag_func_file = os.path.join(dag_folder, 'task.py')

        with open(dag_json_file, 'r') as file:
            dag_data = json.load(file)
        
        nodes = dag_data.get('nodes', [])
        edges = dag_data.get('edges', [])

        spec = importlib.util.spec_from_file_location(f"dag_module_{self.dag_id}", dag_func_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        dag = nx.DiGraph()
        dag.graph['dag_id'] = self.dag_id
        dag.graph['run_id'] = self.run_id
        dag.graph['dag_func_file']= dag_func_file
        dag.graph['arrival_time']= self.arrival_time
        dag.graph['sub_time']= self.sub_time
        dag.graph['dag_type']= self.dag_type
        for node in nodes:
            func_name = node["task"]
            func = getattr(module, func_name)
            decorator_info = getattr(func, '_task_decorator', {})
            dag.add_node(
                func_name,
                func_name= func_name,
                task_id= task_ids.get(func_name),
                type= decorator_info.get('type', 'cpu'),
                cpu_num= decorator_info.get('cpu_num', 1),
                backend= decorator_info.get('backend', 'huggingface'),
                mem= decorator_info.get('mem', 1024),
                gpu_mem= decorator_info.get('gpu_mem', 0),
                model_name= decorator_info.get('model_name', None)
            )
        for edge in edges:
            dag.add_edge(edge["source"], edge["target"])
        return dag

class GaiaLoader(BaseQueryLoader):
    """
    Gaia 
    """
    def __init__(self, args: argparse.Namespace, dag_id: str, dag_type: str, dag_source: str, supplementary_files: List[str], sub_time: float, run_id: Optional[str]= None):
        super().__init__(args, dag_id, dag_type, dag_source, supplementary_files, sub_time, run_id)
        self.question, self.answer= self.get_question_answer()
        print(f"😊 GaiaLoader has loaded...")

    def get_question_answer(self):
        task_metadata= self._get_task_metadata()
        return (task_metadata.get("Question", ""), task_metadata.get("Final answer", ""))
    
    def get_dag_folder(self) -> str:
        return os.path.join(self.args.proj_path, self.args.dag_path, self.dag_source, self.dag_type)

    def _find_file_recursively(self, base_path: str, filename: str) -> Optional[str]:
        for dirpath, _, filenames_in_dir in os.walk(base_path):
            if filename in filenames_in_dir:
                return os.path.join(dirpath, filename)
        return None

    def get_supplementary_files(self) -> Dict[str, str]:
        if not self.supplementary_files:
            return {}
        
        base_search_path = os.path.join(os.path.expanduser(self.args.proj_path), self.args.data_path, "gaia")
        found_paths_map = {}
        for filename_to_find in self.supplementary_files:
            full_path = self._find_file_recursively(base_search_path, filename_to_find)
            if full_path:
                found_paths_map[filename_to_find] = full_path
            else:
                print(f"⚠️ [Warning] Supplementary file '{filename_to_find}' not found under '{base_search_path}'")
        return found_paths_map

    def _get_task_metadata(self) -> Dict:
        base_data_path = os.path.join(os.path.expanduser(self.args.proj_path), self.args.data_path, "gaia", "2023")
        metadata_files_to_search = [
            os.path.join(base_data_path, "test", "metadata.jsonl"),
            os.path.join(base_data_path, "validation", "metadata.jsonl")
        ]
        for metadata_path in metadata_files_to_search:
            if os.path.exists(metadata_path):
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        task_data = json.loads(line)
                        if task_data.get("task_id") == self.dag_id:
                            return task_data
        return {}
    
    def get_dag(self, task_ids: Dict[str, str])-> nx.DiGraph:
        dag= super().get_dag(task_ids)
        dag.graph['supplementary_file_paths']= self.get_supplementary_files()        
        dag.graph["answer"]= self.answer
        dag.graph["question"]= self.question
        return dag

class TBenchLoader(BaseQueryLoader):
    """
    TBench 
    """
    def __init__(self, args: argparse.Namespace, dag_id: str, dag_type: str, dag_source: str, supplementary_files: List[str], sub_time: float, run_id: Optional[str]= None):
        super().__init__(args, dag_id, dag_type, dag_source, supplementary_files, sub_time, run_id)
        self.question, self.answer = self.get_question_answer()
        print(f"😊 TBenchLoader has loaded...")

    def get_question_answer(self):
        """TBenchanswerstring"""
        instruction = self._get_task_instruction()
        return (instruction, "")
    
    def get_dag_folder(self) -> str:
        return os.path.join(self.args.proj_path, self.args.dag_path, self.dag_source, self.dag_type)

    def get_supplementary_files(self) -> Dict[str, str]:
        if not self.supplementary_files:
            return {}
        file_path= ""
        if "airline" in self.dag_type:
            file_path= os.path.join(self.args.proj_path, self.args.data_path, self.dag_source, "data/airline")
        elif "retail" in self.dag_type:
            file_path= os.path.join(self.args.proj_path, self.args.data_path, self.dag_source, "data/retail")

        found_paths_map = {}
        for filename_to_find in self.supplementary_files:
            full_path = self._find_file_recursively(file_path, filename_to_find)
            if full_path:
                found_paths_map[filename_to_find] = full_path
            else:
                print(f"⚠️ [Warning] Supplementary file '{filename_to_find}' not found under '{file_path}'")
        return found_paths_map

    def _find_file_recursively(self, base_path: str, filename: str) -> Optional[str]:
        for dirpath, _, filenames_in_dir in os.walk(base_path):
            if filename in filenames_in_dir:
                return os.path.join(dirpath, filename)
        return None

    def _get_task_instruction(self) -> str:
        """useASTins.pyfiledag_idinstruction"""
        # dag_typeins.py
        ins_file_mapping = {
            "retail_cancel": "cancel_ins.py",
            "retail_return": "return_ins.py", 
            "retail_modify": "modify_ins.py",
            "retail_cancel_modify": "can_modify_ins.py",
            "airline_book": "airline_book_ins.py",
            "airline_cancel": "airline_cancel_ins.py",
        }
        
        ins_filename = ins_file_mapping.get(self.dag_type)
        if not ins_filename:
            print(f"⚠️ [Warning] Unknown dag_type: {self.dag_type}")
            return ""
        
        ins_file_path = os.path.join(
            os.path.expanduser(self.args.proj_path), 
            self.args.data_path, 
            "tbench", 
            "question", 
            ins_filename
        )
        
        if not os.path.exists(ins_file_path):
            print(f"⚠️ [Warning] Ins file not found: {ins_file_path}")
            return ""
        
        return self._extract_instruction_from_ast(ins_file_path, self.dag_id)
    
    def _extract_instruction_from_ast(self, file_path: str, target_uuid: str) -> str:
        """useASTPythonfileuuidinstruction"""
        import ast
        
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                content = file.read()
            
            # AST
            tree = ast.parse(content)
            
            # AST
            for node in ast.walk(tree):
                # Task
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Task':
                    uuid_value = None
                    instruction_value = None
                    
                    # Task
                    for keyword in node.keywords:
                        if keyword.arg == 'uuid':
                            # uuid
                            if isinstance(keyword.value, ast.Constant):
                                uuid_value = keyword.value.value
                            elif isinstance(keyword.value, ast.Str):  # Python < 3.8
                                uuid_value = keyword.value.s
                        elif keyword.arg == 'instruction':
                            # instruction
                            if isinstance(keyword.value, ast.Constant):
                                instruction_value = keyword.value.value
                            elif isinstance(keyword.value, ast.Str):  # Python < 3.8
                                instruction_value = keyword.value.s
                    
                    # uuidinstruction
                    if uuid_value == target_uuid and instruction_value:
                        return instruction_value
        
        except Exception as e:
            print(f"file{e}")
        
        return ""
    
    def get_dag(self, task_ids: Dict[str, str]) -> nx.DiGraph:
        dag = super().get_dag(task_ids)
        dag.graph['supplementary_file_paths'] = self.get_supplementary_files()
        dag.graph["answer"] = self.answer
        dag.graph["question"] = self.question
        return dag


class OpenAGILoader(BaseQueryLoader):
    """
    OpenAGI 
    """
    def __init__(self, args: argparse.Namespace, dag_id: str, dag_type: str, dag_source: str, supplementary_files: List[str], sub_time: float, run_id: Optional[str]= None):
        super().__init__(args, dag_id, dag_type, dag_source, supplementary_files, sub_time, run_id)
        self.question, self.answer = self.get_question_answer()
        print(f"😊 OpenAGILoader has loaded...")

    def get_question_answer(self):
        """OpenAGItask"""
        question = self._get_task_question()
        answer = self._get_task_answer()
        return (question, answer)
    
    def get_dag_folder(self) -> str:
        """OpenAGI"""
        return os.path.join(self.args.proj_path, self.args.dag_path, self.dag_source, self.dag_type)

    def get_supplementary_files(self) -> Dict[str, str]:
        """OpenAGIfileinputsfile"""
        if not self.supplementary_files:
            return {}
        
        #  inputs 
        inputs_path = os.path.join(
            os.path.expanduser(self.args.proj_path), 
            self.args.data_path, 
            "openagi", 
            self.dag_type, 
            self.dag_id, 
            "inputs"
        )
        
        found_paths_map = {}
        
        if not os.path.exists(inputs_path):
            print(f"⚠️ [Warning] Inputs folder not found: {inputs_path}")
            return found_paths_map
        
        for filename_to_find in self.supplementary_files:
            file_path = os.path.join(inputs_path, filename_to_find)
            if os.path.exists(file_path):
                found_paths_map[filename_to_find] = file_path
            else:
                print(f"⚠️ [Warning] Supplementary file '{filename_to_find}' not found in '{inputs_path}'")
        
        return found_paths_map

    def _find_file_recursively(self, base_path: str, filename: str) -> Optional[str]:
        """file"""
        for dirpath, _, filenames_in_dir in os.walk(base_path):
            if filename in filenames_in_dir:
                return os.path.join(dirpath, filename)
        return None

    def _get_task_question(self) -> str:
        """inputsfilequestion.txt"""
        inputs_path = os.path.join(
            os.path.expanduser(self.args.proj_path), 
            self.args.data_path, 
            "openagi", 
            self.dag_type, 
            self.dag_id, 
            "inputs"
        )
        
        #  question.txt 
        question_files = ["question.txt", "questions.txt"]  #

        #question.txt", "questions.txt
        context = ""
        for question_file in question_files:
            question_path = os.path.join(inputs_path, question_file)
            if os.path.exists(question_path):
                try:
                    with open(question_path, 'r', encoding='utf-8') as f:
                        content = f.read().strip()
                        if content:
                            context+= content + "\n"   
                except Exception as e:
                    print(f"⚠️ [Warning] Error reading {question_path}: {e}")
        return context
    

    def _get_task_answer(self) -> str:
        """outputsfile"""
        outputs_path = os.path.join(
            os.path.expanduser(self.args.proj_path), 
            self.args.data_path, 
            "openagi", 
            self.dag_type, 
            self.dag_id, 
            "outputs"
        )
        
        #

        answer_files = ["answers.txt","labels.txt"]
        
        for answer_file in answer_files:
            answer_path = os.path.join(outputs_path, answer_file)
            if os.path.exists(answer_path):
                try:
                    with open(answer_path, 'r', encoding='utf-8') as f:
                        content = f.read().strip()
                        if content:
                            return content
                except Exception as e:
                    print(f"⚠️ [Warning] Error reading {answer_path}: {e}")
        
        #

        print(f"⚠️ [Warning] No answer file found in {outputs_path}")
        return ""

    def _get_task_metadata(self) -> Dict:
        """task"""
        #  _get_task_question  _get_task_answer
        return {
            "task": self._get_task_question(),
            "expected_output": self._get_task_answer()
        }
    
    def get_dag(self, task_ids: Dict[str, str]) -> nx.DiGraph:
        """DAG"""
        dag = super().get_dag(task_ids)
        dag.graph['supplementary_file_paths'] = self.get_supplementary_files()
        dag.graph["answer"] = self.answer
        dag.graph["question"] = self.question
        return dag
