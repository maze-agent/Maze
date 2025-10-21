from typing import Any,List
from maze.core.workflow.task import CodeTask
from typing import Dict
import networkx as nx

# class Workflow():
#     def __init__(self, id: str):
#         self.id: str = id
#         self.tasks: dict[str, Any] = {}  #key:task_id,value:task

#         self.edges: dict[str, str] = {}  #key:source_task_id,value:target_task_id
#         self.in_degree: dict[str, int]={} #key:task_id,value:in_degree

#         self.successors_id: dict[str, List] =  {} #key:task_id,value:List[successor_task_id]
#         self.prev_id: dict[str, str] = {} #
        
#     def add_task(self,task_id,task:CodeTask):
#         self.tasks[task_id] = task
#         self.in_degree[task_id] = 0
#         self.successors_id[task_id] = []

#     def del_task(self,task_id:str):
#         del self.tasks[task_id]


#     def get_task(self,task_id:str):
#         return self.tasks[task_id]

#     def add_edge(self,source_task_id:str,target_task_id:str):
#         self.edges[source_task_id] = target_task_id
#         self.in_degree[target_task_id] = self.in_degree[target_task_id] + 1
#         self.successors_id[source_task_id].append(target_task_id)

#     def get_start_task(self):
#         start_task = []
#         for task_id in self.tasks:
#             if self.in_degree[task_id] == 0:
#                 start_task.append(self.tasks[task_id])
#         return start_task        
    
#     def get_total_task_num(self):
#         return len(self.tasks)

#     def finish_task(self,task_id:str):
#         new_ready_tasks = []

#         for task_id in self.successors_id[task_id]:
#             self.in_degree[task_id] = self.in_degree[task_id] - 1
#             if self.in_degree[task_id] == 0:
#                 new_ready_tasks.append(self.tasks[task_id])

#         return new_ready_tasks


class Workflow:
    def __init__(self, id: str):
        self.id: str = id
        self.graph = nx.DiGraph()  # 有向图表示工作流
        self.tasks: Dict[str, CodeTask] = {}  # 任务ID -> CodeTask 映射

    def add_task(self, task_id: str, task: CodeTask) -> None:
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
        self.graph.remove_node(task_id)  # 移除节点及其所有边

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

    def finish_task(self, task_id: str) -> List[CodeTask]:
        """
        Finish a task in workflow and return next ready tasks.
        A task is ready if all its predecessors are finished.
        """
        if task_id not in self.tasks:
            raise ValueError(f"Task {task_id} not found in workflow.")
        
        task = self.tasks[task_id]
        task.completed = True

        ready_tasks = []
        for successor in self.graph.successors(task_id):
            pred_tasks = [self.tasks[p] for p in self.graph.predecessors(successor)]
            
            # 如果所有前驱任务都已完成，则该后继任务就绪
            if all(pred.completed  for pred in pred_tasks): 
                ready_tasks.append(self.tasks[successor])
        
        return ready_tasks

    def __repr__(self):
        return f"Workflow(id={self.id}, tasks={list(self.tasks.keys())})"