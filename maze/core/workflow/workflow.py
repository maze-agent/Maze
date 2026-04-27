from networkx.classes.digraph import DiGraph
from typing import Any,List
from maze.core.workflow.task import CodeTask,LangGraphTask
from typing import Dict
import networkx as nx
import httpx
import time

HACS_TASK_TYPE_AVG_TIMES = {
    "cpu": 3.0,
    "gpu": 60.0,
}

class LangGraphWorkflow:
    def __init__(self, id: str):
        self.id: str = id
        self.tasks: Dict[str, LangGraphTask] = {} 

    def add_task(self, task_id: str, task: LangGraphTask) -> None:
        """
        Add a task to workflow
        """
        if task_id != task.task_id:
            raise ValueError("task_id must match task.task_id")
        self.tasks[task_id] = task
        self.graph.add_node(task_id)

    def del_task(self, task_id: str) -> None:
        """
        Delete a task from workflow
        """
        if task_id in self.tasks:
            del self.tasks[task_id]
        
    def get_task(self, task_id: str) -> LangGraphTask:
        """
        Get a task from workflow
        """
        return self.tasks.get(task_id)

class Workflow:
    def __init__(self, id: str):
        self.id: str = id
        self.graph: DiGraph[Any] = nx.DiGraph()
        self.tasks: Dict[str, CodeTask] = {}
        self.remaining_task_num: int = 0
        self.graph.graph["total_gpu_tasks"] = 0
        self.graph.graph["remaining_gpu_tasks"] = 0

    def add_task(self, task_id: str, task: CodeTask) -> None:
        """
        Add a task to workflow
        """
        if task_id != task.task_id:
            raise ValueError("task_id must match task.task_id")
        self.tasks[task_id] = task
        self.graph.add_node(task_id)
        self.remaining_task_num += 1

    def del_task(self, task_id: str) -> None:
        """
        Delete a task from workflow
        """
        if task_id in self.tasks:
            del self.tasks[task_id]
        self.graph.remove_node(task_id)
        self.remaining_task_num -= 1

    def get_task(self, task_id: str) -> CodeTask:
        """
        Get a task from workflow
        """
        return self.tasks.get(task_id)

    def add_edge(self, source_task_id: str, target_task_id: str) -> None:
        """
        Add a edge to workflow (dependency: source -> target)
        """
        if source_task_id not in self.graph or target_task_id not in self.graph:
            raise ValueError("Both tasks must exist in the workflow before adding an edge.")
        self.graph.add_edge(source_task_id, target_task_id)
        if not nx.is_directed_acyclic_graph(self.graph):
            self.remove_edge(source_task_id, target_task_id)
            raise ValueError("The edge would make the workflow contain a cycle.")
       
    def del_edge(self, source_task_id: str, target_task_id: str) -> None:
        """
        Delete a edge from workflow
        """
        if source_task_id not in self.graph or target_task_id not in self.graph:
            raise ValueError("Both tasks must exist in the workflow before deleting an edge.")
        self.graph.remove_edge(source_task_id, target_task_id)

    def get_start_task(self) -> List[CodeTask]:
        """
        Get start tasks from workflow (tasks with no incoming edges)
        """
        start_nodes = [node for node in self.graph.nodes if self.graph.in_degree(node) == 0]
        return [self.tasks[node] for node in start_nodes]

    def get_total_task_num(self) -> int:
        """
        Get total task number in workflow
        """
        return self.graph.number_of_nodes()

    def _get_task_type(self, task_id: str) -> str:
        task = self.tasks[task_id]
        resources = task.resources or {}
        if resources.get("gpu", 0) > 0:
            return "gpu"
        return "cpu"

    def mark_task_started(self, task_id: str) -> None:
        if task_id not in self.tasks:
            return
        self.tasks[task_id].mark_started()

    def prepare_for_strategy(self, strategy: str) -> None:
        if strategy not in ("HACS", "ATLAS"):
            return

        for node in self.graph.nodes:
            self.graph.nodes[node]["attained_service"] = 0.0

        if strategy == "HACS":
            total_gpu_tasks = 0
            for node in self.graph.nodes:
                task_type = self._get_task_type(node)
                self.graph.nodes[node]["task_type"] = task_type
                self.graph.nodes[node]["pred_time"] = HACS_TASK_TYPE_AVG_TIMES[task_type]
                if task_type == "gpu":
                    total_gpu_tasks += 1

            self.graph.graph["total_gpu_tasks"] = total_gpu_tasks
            self.graph.graph["remaining_gpu_tasks"] = total_gpu_tasks

            topo_order = list(nx.topological_sort(self.graph))
            for node in reversed(topo_order):
                successors = list(self.graph.successors(node))
                if not successors:
                    self.graph.nodes[node]["n_desc"] = 0
                else:
                    self.graph.nodes[node]["n_desc"] = (
                        max(self.graph.nodes[succ].get("n_desc", 0) for succ in successors) + 1
                    )

    def finish_task(self, task_id: str,task_result:Dict,strategy:str) -> List[CodeTask]:
        """
        Finish a task in workflow and return next ready tasks.
        A task is ready if all its predecessors are finished.
        """
        if task_id not in self.tasks:
            raise ValueError(f"Task {task_id} not found in workflow.")
        
        self.remaining_task_num -= 1

        task = self.tasks[task_id]
        task.completed = True
        task.finish_time = time.time()
        actual_start_time = task.start_time or task.created_time
        actual_runtime = max(0.0, task.finish_time - actual_start_time)

        try:
            if task.can_predict and strategy == "DAPS":
                payload = {"task_name": task.task_name, "features": task.predict_feature,"execution_time": actual_runtime}
                response = httpx.post("http://127.0.0.1:8001/collect_data", json=payload, timeout=httpx.Timeout(5.0))
        except httpx.ReadTimeout as e:
            pass

        if strategy == "HACS" and self.graph.nodes[task_id].get("task_type") == "gpu":
            remaining_gpu_tasks = self.graph.graph.get("remaining_gpu_tasks", 0)
            if remaining_gpu_tasks > 0:
                self.graph.graph["remaining_gpu_tasks"] = remaining_gpu_tasks - 1

        if strategy == "ATLAS":
            parent_attained_service = self.graph.nodes[task_id].get("attained_service", 0.0)
            path_attained_service = parent_attained_service + actual_runtime
            for successor in self.graph.successors(task_id):
                current_attained_service = self.graph.nodes[successor].get("attained_service", 0.0)
                if path_attained_service > current_attained_service:
                    self.graph.nodes[successor]["attained_service"] = path_attained_service

        ready_tasks = []
        for successor in self.graph.successors(task_id):
            # Set features for the successor task
            successor_task = self.tasks[successor]
            if successor_task.can_predict:
                for key, value in task_result.items():
                    successor_task.set_predict_feature(key, value)

            # Check if all predecessors are completed
            pred_tasks = [self.tasks[p] for p in self.graph.predecessors(successor)]
            if all(pred.completed  for pred in pred_tasks): 
                ready_tasks.append(self.tasks[successor])
        
        return ready_tasks

    
