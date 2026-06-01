from datetime import datetime, timezone
from networkx.classes.digraph import DiGraph
from typing import Any,List,Optional
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

        # --- Run-level observability state (used after submit) ---
        # `id` is the template id; the run id (== submit_id in MaPath) is set on submit.
        self.run_id: Optional[str] = None
        self.status: str = "created"  # created|submitted|running|succeeded|failed|canceled
        self.created_time: float = time.time()
        self.submitted_time: Optional[float] = None
        self.started_time: Optional[float] = None
        self.finished_time: Optional[float] = None
        self.updated_time: float = self.created_time
        self.failure_reason: Optional[str] = None
        self.cancel_reason: Optional[str] = None

        # task_id -> {status, started_at, finished_at, duration_ms, node_id, metrics, retry_count, error}
        self.task_states: Dict[str, Dict[str, Any]] = {}

        # Event log (in-memory, persisted via StaticRunStore).
        self.event_log: List[Dict[str, Any]] = []
        self.event_seq: int = 0

    def add_task(self, task_id: str, task: CodeTask) -> None:
        """
        Add a task to workflow
        """
        if task_id != task.task_id:
            raise ValueError("task_id must match task.task_id")
        self.tasks[task_id] = task
        self.graph.add_node(task_id)
        self.remaining_task_num += 1
        self.task_states[task_id] = {
            "task_id": task_id,
            "task_name": task.task_name,
            "status": "pending",
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "node_id": None,
            "metrics": {},
            "retry_count": 0,
            "error": None,
        }

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

    def mark_task_started(
        self,
        task_id: str,
        node_id: Optional[str] = None,
        started_at: Optional[float] = None,
    ) -> None:
        if task_id not in self.tasks:
            return
        self.tasks[task_id].mark_started()
        ts = started_at or self.tasks[task_id].start_time or time.time()
        state = self.task_states.setdefault(task_id, {
            "task_id": task_id,
            "task_name": self.tasks[task_id].task_name,
            "status": "pending",
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "node_id": None,
            "metrics": {},
            "retry_count": 0,
            "error": None,
        })
        state["status"] = "running"
        state["started_at"] = ts
        if node_id:
            state["node_id"] = node_id
        self.updated_time = ts

    def mark_task_finished(
        self,
        task_id: str,
        *,
        status: str = "succeeded",
        finished_at: Optional[float] = None,
        metrics: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        if task_id not in self.task_states:
            return
        state = self.task_states[task_id]
        ts = finished_at or time.time()
        state["status"] = status
        state["finished_at"] = ts
        if state.get("started_at"):
            state["duration_ms"] = int((ts - state["started_at"]) * 1000)
        if metrics:
            existing = state.get("metrics") or {}
            for k, v in metrics.items():
                if k == "by_model" and isinstance(v, dict):
                    bucket = existing.setdefault("by_model", {})
                    for model_name, model_metrics in v.items():
                        if not isinstance(model_metrics, dict):
                            continue
                        target = bucket.setdefault(model_name, {})
                        for sub_k, sub_v in model_metrics.items():
                            if isinstance(sub_v, (int, float)):
                                target[sub_k] = target.get(sub_k, 0) + sub_v
                            else:
                                target[sub_k] = sub_v
                    continue
                if isinstance(v, (int, float)) and isinstance(existing.get(k), (int, float)):
                    existing[k] = existing[k] + v
                else:
                    existing[k] = v
            state["metrics"] = existing
        if error:
            state["error"] = error
        self.updated_time = ts

    # --- Run-level observability helpers ---

    def append_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Append a structured event to the in-memory event log.

        Returns the stored copy (with seq/ts/timestamp filled in).
        """
        self.event_seq += 1
        now = time.time()
        stored = {
            "seq": self.event_seq,
            "ts": now,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event.get("type"),
            "data": event.get("data") or {},
        }
        self.event_log.append(stored)
        self.updated_time = now
        return stored

    def get_running_task_views(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = now or time.time()
        out: List[Dict[str, Any]] = []
        for state in self.task_states.values():
            if state.get("status") != "running":
                continue
            duration_so_far = None
            if state.get("started_at"):
                duration_so_far = int((now - state["started_at"]) * 1000)
            out.append({
                "task_id": state["task_id"],
                "task_name": state.get("task_name"),
                "started_at": state.get("started_at"),
                "node_id": state.get("node_id"),
                "duration_so_far_ms": duration_so_far,
            })
        return out

    def task_count_done(self) -> int:
        return sum(1 for s in self.task_states.values() if s.get("status") in ("succeeded", "failed", "canceled"))

    def task_count_running(self) -> int:
        return sum(1 for s in self.task_states.values() if s.get("status") == "running")

    def task_count_pending(self) -> int:
        return sum(1 for s in self.task_states.values() if s.get("status") in ("pending", "ready"))

    def to_snapshot_dict(self) -> Dict[str, Any]:
        """Build a snapshot payload (consumed by StaticRunSnapshot.from_json)."""
        from maze.core.runs.static_snapshot import RunMetrics

        run_metrics = RunMetrics()
        for state in self.task_states.values():
            run_metrics.absorb(state.get("metrics") or {})

        return {
            "run_id": self.run_id or self.id,
            "workflow_id": self.id,
            "kind": "static",
            "status": self.status,
            "created_time": self.created_time,
            "submitted_time": self.submitted_time,
            "started_time": self.started_time,
            "finished_time": self.finished_time,
            "updated_time": self.updated_time,
            "task_total": self.get_total_task_num(),
            "tasks": dict(self.task_states),
            "metrics": run_metrics.to_json(),
            "failure_reason": self.failure_reason,
            "cancel_reason": self.cancel_reason,
            "event_count": len(self.event_log),
            "last_event_seq": self.event_seq,
        }

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

    
