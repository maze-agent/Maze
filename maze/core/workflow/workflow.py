from networkx.classes.digraph import DiGraph
from typing import Any,List
from maze.core.workflow.task import CodeTask,LangGraphTask
from typing import Dict
import networkx as nx
import time

from maze.core.scheduler.strategy import DEFAULT_PREDICTED_DURATION_SECONDS

HACS_TASK_TYPE_AVG_TIMES = dict(DEFAULT_PREDICTED_DURATION_SECONDS)

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
        return getattr(task, "task_kind", "cpu") or "cpu"

    def mark_task_started(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task is not None:
            task.mark_started()

    def prepare_for_strategy(self, strategy: str) -> None:
        if strategy != "HACS":
            return

        total_value_tasks = 0
        for node in self.graph.nodes:
            task_type = self._get_task_type(node)
            self.graph.nodes[node]["task_type"] = task_type
            self.graph.nodes[node]["task_kind"] = task_type
            self.graph.nodes[node]["pred_time"] = HACS_TASK_TYPE_AVG_TIMES[task_type]
            self.graph.nodes[node]["predicted_duration"] = HACS_TASK_TYPE_AVG_TIMES[task_type]
            self.graph.nodes[node]["prediction_source"] = "task_kind_default"
            if task_type == "gpu":
                total_value_tasks += 1

        self.graph.graph["total_gpu_tasks"] = total_value_tasks
        self.graph.graph["remaining_gpu_tasks"] = total_value_tasks
        self.graph.graph["total_value_tasks"] = total_value_tasks
        self.graph.graph["remaining_value_tasks"] = total_value_tasks

        topo_order = list(nx.topological_sort(self.graph))
        for node in topo_order:
            predecessors = list(self.graph.predecessors(node))
            if not predecessors:
                self.graph.nodes[node]["n_anc"] = 0
            else:
                self.graph.nodes[node]["n_anc"] = (
                    max(self.graph.nodes[pred].get("n_anc", 0) for pred in predecessors) + 1
                )

        for node in reversed(topo_order):
            successors = list(self.graph.successors(node))
            if not successors:
                self.graph.nodes[node]["n_desc"] = 0
            else:
                self.graph.nodes[node]["n_desc"] = (
                    max(self.graph.nodes[succ].get("n_desc", 0) for succ in successors) + 1
                )

    def finish_task(self, task_id: str, strategy: str) -> List[CodeTask]:
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

        if strategy == "HACS" and self.graph.nodes[task_id].get("task_type") == "gpu":
            remaining_gpu_tasks = self.graph.graph.get("remaining_gpu_tasks", 0)
            if remaining_gpu_tasks > 0:
                self.graph.graph["remaining_gpu_tasks"] = remaining_gpu_tasks - 1
            remaining_value_tasks = self.graph.graph.get("remaining_value_tasks", 0)
            if remaining_value_tasks > 0:
                self.graph.graph["remaining_value_tasks"] = remaining_value_tasks - 1

        ready_tasks = []
        for successor in self.graph.successors(task_id):
            # Check if all predecessors are completed
            pred_tasks = [self.tasks[p] for p in self.graph.predecessors(successor)]
            if all(pred.completed  for pred in pred_tasks): 
                ready_tasks.append(self.tasks[successor])
        
        return ready_tasks

    
