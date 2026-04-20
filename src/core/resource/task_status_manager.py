import threading
from typing import Dict, Tuple

class TaskStatusManager:
    """
    Task status manager to handle the status and context of tasks.
    """

    def __init__(self)-> None:
        """
        Initialize task status manager with predefined task statuses.
        """
        self.task_status= {"received", "waiting", "running", "finished", "released", "failed"}
        self.task_status_map= {}       # { (dag_id, task_id): status_str }
        self.task_info_map= {}         # { (dag_id, task_id): task_info_dict }
        self.task2nodeId= {}           # { (dag_id, task_id): node_name }
        self.failed_task_log= {}       # { (dag_id, task_id): {"info": ..., "reason": str} }
        self._lock= threading.Lock()

    def add_task(self, task_info: dict)-> None:
        """
        Add a new task to the manager and set its initial status to 'received'.

        :param task_info: Task information including 'dag_id' and 'task_id'
        """
        run_id= task_info["run_id"]        
        # dag_id= task_info["dag_id"]
        task_id= task_info["task_id"]
        key= (run_id, task_id)
        with self._lock:
            self.task_info_map[key]= task_info
            self.task_status_map[key]= "received"

    def set_status(self, run_id: str, task_id: str, status: str, err_msg: str= "")-> None:
        """
        Set the status of a task (supports recording failure reasons).

        :param dag_id: DAG ID
        :param task_id: Task ID
        :param status: Task status (should be one of the predefined statuses)
        :param err_msg: Error message (optional, for failed tasks)
        :raises ValueError: If the provided status is invalid
        :raises KeyError: If the task is not registered
        """
        if status not in self.task_status:
            raise ValueError(f"Invalid task status: {status}")
        key = (run_id, task_id)
        with self._lock:
            if key not in self.task_status_map:
                raise KeyError(f"Task {key} not registered, cannot set status")
            self.task_status_map[key]= status
            if status== "failed":
                task_info= self.task_info_map.get(key, {})
                self.failed_task_log[key]= {
                    "info": task_info,
                    "err_msg": err_msg
                }

    def get_failed_task_info(self, run_id: str, task_id: str)-> Dict[str, str]:
        """
        Retrieve detailed information about a failed task.

        :param dag_id: DAG ID
        :param task_id: Task ID
        :return: A dictionary with task info and failure reason, or an empty dictionary
        """
        key= (run_id, task_id)
        with self._lock:
            return self.failed_task_log.get(key, {})

    def get_task_info(self, run_id: str, task_id: str) -> Dict[str, object]:
        """
        Retrieve the raw description of the task.

        :param dag_id: The DAG ID
        :param task_id: The task ID
        :return: A dictionary containing the task information, or an empty dictionary if not found
        """
        key = (run_id, task_id)
        with self._lock:
            return self.task_info_map.get(key, {})

    def get_status(self, run_id: str, task_id: str)-> str:
        """
        Get the current status of a task.

        :param dag_id: DAG ID
        :param task_id: Task ID
        :return: Task status as a string
        """
        key= (run_id, task_id)
        with self._lock:
            return self.task_status_map.get(key, "unknown")

    def set_selected_node(self, run_id: str, task_id: str, node_id: str)-> None:
        """
        Set the node name for task scheduling (e.g., m1, c1).

        :param dag_id: DAG ID
        :param task_id: Task ID
        :param node_id: Node ID where the task will be scheduled
        """
        key= (run_id, task_id)
        with self._lock:
            self.task2nodeId[key]= node_id

    def get_selected_node(self, run_id: str, task_id: str)-> str:
        """
        Get the node name where the task is scheduled.

        :param dag_id: DAG ID
        :param task_id: Task ID
        :return: Node ID or None if not found
        """
        key= (run_id, task_id)
        with self._lock:
            return self.task2nodeId.get(key, None)

    def get_all_tasks(self)-> Dict[Tuple[str, str], Dict]:
        """
        Get all tasks under a Dag.

        :return: {(dag_id, task_id): {"status": str, "info": dict}}
        """
        with self._lock:
            return {
                key: {
                    "status": self.task_status_map[key],
                    "info": self.task_info_map[key]
                }
                for key in self.task_status_map
            }