import ray
import time
import logging
import copy
from typing import Any,List,Dict
from maze.core.scheduler.runtime import SelectedNode
from maze.core.scheduler.runtime import TaskRuntime
from maze.core.scheduler.runtime import SelectedNode
from maze.client.maze.agent_sandbox import detect_agent_sandbox_capabilities
from maze.utils.utils import collect_gpu_info

logger = logging.getLogger(__name__)

SUPPORTED_SCHEDULING_POLICIES = {
    "default": {
        "implemented": True,
        "description": "Scan registered nodes in registration order.",
    },
    "least-loaded": {
        "implemented": True,
        "description": "Prefer the registered node with the fewest running Maze tasks.",
    },
    "prefer-gpu-free": {
        "implemented": False,
        "description": "Reserved policy: prefer preserving free GPU nodes for GPU tasks.",
    },
    "spread": {
        "implemented": False,
        "description": "Reserved policy: spread tasks across nodes.",
    },
}


class ResourceSelection:
    def __init__(self, selected_node: SelectedNode | None, decision: Dict[str, Any]):
        self.selected_node = selected_node
        self.decision = decision

    def __bool__(self):
        return self.selected_node is not None

    @property
    def node_id(self):
        return self.selected_node.node_id if self.selected_node else None

    @property
    def node_ip(self):
        return self.selected_node.node_ip if self.selected_node else None

    @property
    def gpu_id(self):
        return self.selected_node.gpu_id if self.selected_node else None

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self.decision)


class Node():
    def __init__(self,node_id:str,node_ip:str,available_resources:dict,total_resources:dict,capabilities:dict | None = None):
        self.node_id = node_id
        self.node_ip = node_ip
        self.available_resources = copy.deepcopy(available_resources)
        self.total_resources = copy.deepcopy(total_resources)
        self.capabilities = copy.deepcopy(capabilities or {"workspace_sandbox": True, "docker_sandbox": False})
        now = time.time()
        self.registered_time = now
        self.last_seen_time = now
        self.last_ray_seen_time = now
        self.last_resource_update_time = now

    def update_registration(self, node_ip: str, resources: dict, capabilities: dict | None = None) -> str:
        normalized_resources = copy.deepcopy(resources)
        normalized_resources["gpu_resource"] = {
            int(k): v for k, v in normalized_resources.get("gpu_resource", {}).items()
        }
        self.last_seen_time = time.time()
        self.node_ip = node_ip
        self.capabilities = copy.deepcopy(capabilities or self.capabilities)

        if normalized_resources == self.total_resources:
            return "already_registered"

        used_cpu = self.total_resources.get("cpu", 0) - self.available_resources.get("cpu", 0)
        used_cpu_mem = self.total_resources.get("cpu_mem", 0) - self.available_resources.get("cpu_mem", 0)
        used_gpu_resources = {}
        for gpu_id, total_gpu in self.total_resources.get("gpu_resource", {}).items():
            available_gpu = self.available_resources.get("gpu_resource", {}).get(gpu_id, {})
            used_gpu_resources[gpu_id] = {
                "gpu_num": total_gpu.get("gpu_num", 0) - available_gpu.get("gpu_num", 0),
                "gpu_mem": total_gpu.get("gpu_mem", 0) - available_gpu.get("gpu_mem", 0),
            }

        self.total_resources = copy.deepcopy(normalized_resources)
        self.available_resources = copy.deepcopy(normalized_resources)
        self.available_resources["cpu"] = max(0, self.total_resources.get("cpu", 0) - used_cpu)
        self.available_resources["cpu_mem"] = max(0, self.total_resources.get("cpu_mem", 0) - used_cpu_mem)
        for gpu_id, used_gpu in used_gpu_resources.items():
            if gpu_id not in self.available_resources.get("gpu_resource", {}):
                continue
            self.available_resources["gpu_resource"][gpu_id]["gpu_num"] = max(
                0,
                self.available_resources["gpu_resource"][gpu_id].get("gpu_num", 0) - used_gpu.get("gpu_num", 0),
            )
            self.available_resources["gpu_resource"][gpu_id]["gpu_mem"] = max(
                0,
                self.available_resources["gpu_resource"][gpu_id].get("gpu_mem", 0) - used_gpu.get("gpu_mem", 0),
            )
        self.last_resource_update_time = time.time()
        return "updated"
  
    def release_resource(self,resources:dict,gpu_id:int = None):
        cpu = resources["cpu"]
        cpu_mem = resources["cpu_mem"]
        gpu = resources["gpu"]
        gpu_mem = resources["gpu_mem"]

        self.available_resources['cpu'] += cpu
        self.available_resources['cpu_mem'] += cpu_mem
        
        if gpu_id is not None:
            self.available_resources['gpu_resource'][gpu_id]['gpu_mem'] += gpu_mem
            self.available_resources['gpu_resource'][gpu_id]['gpu_num'] += gpu
        self.last_resource_update_time = time.time()
            
class ResourceManager():
    def __init__(self):
        self.head_node_id = None
        self.head_node_ip = None
        self.nodes:Dict[str,Node] = {}
        self.running_task_counts: Dict[str, int] = {}
        self.scheduling_policy = "default"
        self.worker_stale_after_seconds = 30
        
        self.last_time = time.time()
        self.interval = 3

    def set_scheduling_policy(self, policy: str | None):
        normalized = (policy or "default").strip().lower()
        if normalized not in SUPPORTED_SCHEDULING_POLICIES:
            normalized = "default"
        if not SUPPORTED_SCHEDULING_POLICIES[normalized]["implemented"]:
            logger.info("Scheduling policy %s is reserved; using default node scan", normalized)
        self.scheduling_policy = normalized

    def _get_head_node_resource(self):
        '''
        Get the maze head node resource
        '''
        head_resource = {}

        head_node = None
        for node in ray.nodes():
            if node["NodeID"] == self.head_node_id:
                head_node = node
                break
        assert(head_node is not None)

        head_resource = {
            "cpu":head_node["Resources"]["CPU"],
            "cpu_mem":head_node["Resources"]["memory"],   
            "gpu_resource":{}
        }

        gpu_info = collect_gpu_info()
        if len(gpu_info) > 0:
            for gpu in gpu_info:
                gpu_id = gpu["index"]
                gpu_mem = gpu["memory_free"]
                head_resource["gpu_resource"][gpu_id] = {
                    "gpu_id" : gpu_id,
                    "gpu_mem":gpu_mem,
                    "gpu_num":1
                }

        return head_resource

    def init(self):
        '''
        Init maze head
        '''
        ray.init(address='auto')
        self.head_node_id = ray.get_runtime_context().get_node_id()
        self.head_node_ip = ray.util.get_node_ip_address()
        head_node_resource = self._get_head_node_resource()

        #Wait for ray head launch
        while True:
            for node in ray.nodes():
                if node["NodeID"] == self.head_node_id and node["Alive"]:     
                    self.nodes[self.head_node_id] = Node(
                        self.head_node_id,
                        self.head_node_ip,
                        head_node_resource,
                        head_node_resource,
                        detect_agent_sandbox_capabilities(),
                    )
                    self.running_task_counts.setdefault(self.head_node_id, 0)
                    return
                    
    def check_dead_node(self): 
        nodes = ray.nodes()
        for node in nodes:
            if node["NodeID"] in self.nodes and not node["Alive"]:
                del self.nodes[node["NodeID"]]
                self.running_task_counts.pop(node["NodeID"], None)
                
    def show_all_node_resource(self):
        '''
        Show all node resource
        '''
        cur_time = time.time()
        if cur_time - self.last_time >= self.interval:
            self.last_time = cur_time
            
            logger.debug("===Show All Node===")
            logger.debug("Total Node: %s", len(self.nodes))
            for node_id,node in self.nodes.items():
                logger.debug("node_id:%s, available_resources:%s", node_id, node.available_resources)

    def _ray_node_index(self):
        try:
            return {node["NodeID"]: node for node in ray.nodes()}
        except Exception:
            return {}

    def _is_node_alive(self, node_id: str, ray_nodes: Dict[str, Dict[str, Any]]):
        if not ray_nodes:
            return True
        ray_node = ray_nodes.get(node_id)
        return bool(ray_node and ray_node.get("Alive", False))

    def _gpu_snapshot(self, node: Node):
        gpu_ids = sorted(
            set(node.total_resources.get("gpu_resource", {}).keys())
            | set(node.available_resources.get("gpu_resource", {}).keys())
        )
        devices = []
        total_count = 0
        available_count = 0

        for gpu_id in gpu_ids:
            total_gpu = node.total_resources.get("gpu_resource", {}).get(gpu_id, {})
            available_gpu = node.available_resources.get("gpu_resource", {}).get(gpu_id, {})
            total_num = total_gpu.get("gpu_num", 0)
            available_num = available_gpu.get("gpu_num", 0)
            total_count += total_num
            available_count += available_num
            devices.append({
                "gpu_id": gpu_id,
                "total_count": total_num,
                "available_count": available_num,
                "total_memory": total_gpu.get("gpu_mem", 0),
                "available_memory": available_gpu.get("gpu_mem", 0),
            })

        return {
            "total_count": total_count,
            "available_count": available_count,
            "devices": devices,
        }

    def get_cluster_resources(self):
        ray_nodes = self._ray_node_index()
        registered_nodes = []

        for node_id, node in self.nodes.items():
            ray_node = ray_nodes.get(node_id)
            alive = bool(ray_node.get("Alive", False)) if ray_node else False
            if alive:
                node.last_ray_seen_time = time.time()
            stale = bool(
                node_id != self.head_node_id
                and alive
                and time.time() - node.last_seen_time > self.worker_stale_after_seconds
            )
            registered_nodes.append({
                "node_id": node_id,
                "node_ip": node.node_ip,
                "role": "head" if node_id == self.head_node_id else "worker",
                "registered": True,
                "alive": alive,
                "stale": stale,
                "running_task_count": self.running_task_counts.get(node_id, 0),
                "registered_time": node.registered_time,
                "last_seen_time": node.last_seen_time,
                "last_ray_seen_time": node.last_ray_seen_time,
                "last_resource_update_time": node.last_resource_update_time,
                "resources": {
                    "cpu": {
                        "total": node.total_resources.get("cpu", 0),
                        "available": node.available_resources.get("cpu", 0),
                    },
                    "cpu_mem": {
                        "total": node.total_resources.get("cpu_mem", 0),
                        "available": node.available_resources.get("cpu_mem", 0),
                    },
                    "gpu": self._gpu_snapshot(node),
                },
                "capabilities": copy.deepcopy(node.capabilities),
                "ray_resources": ray_node.get("Resources", {}) if ray_node else {},
            })

        unregistered_ray_nodes = []
        for node_id, ray_node in ray_nodes.items():
            if node_id in self.nodes:
                continue
            if not ray_node.get("Alive", False):
                continue
            unregistered_ray_nodes.append({
                "node_id": node_id,
                "node_ip": ray_node.get("NodeManagerAddress"),
                "role": "worker",
                "registered": False,
                "alive": True,
                "capabilities": {"workspace_sandbox": True, "docker_sandbox": False},
                "ray_resources": ray_node.get("Resources", {}),
            })

        return {
            "head_node_id": self.head_node_id,
            "head_node_ip": self.head_node_ip,
            "scheduling_policy": self.scheduling_policy,
            "supported_scheduling_policies": copy.deepcopy(SUPPORTED_SCHEDULING_POLICIES),
            "nodes": sorted(registered_nodes, key=lambda item: (item["role"] != "head", item["node_ip"] or "")),
            "unregistered_ray_nodes": sorted(unregistered_ray_nodes, key=lambda item: item["node_ip"] or ""),
        }
            
            
    def stop_worker(self,node_id:str):
        '''
        Stop worker node
        '''
        del self.nodes[node_id]
        
    def _node_resource_snapshot(self, node: Node) -> Dict[str, Any]:
        return {
            "cpu": node.available_resources.get("cpu", 0),
            "cpu_mem": node.available_resources.get("cpu_mem", 0),
            "gpu": self._gpu_snapshot(node),
        }

    def _candidate_failure_reason(self, candidates: List[Dict[str, Any]], target_node_id: str | None = None):
        if target_node_id and not any(candidate.get("node_id") == target_node_id for candidate in candidates):
            return "specified_node_unavailable"
        if not candidates:
            return "no_registered_alive_node"
        if target_node_id and any("specified_node_unavailable" in candidate.get("reject_reasons", []) for candidate in candidates):
            return "specified_node_unavailable"
        if all("node_not_alive" in candidate.get("reject_reasons", []) for candidate in candidates):
            return "no_registered_alive_node"

        reason_priority = [
            "specified_node_unavailable",
            "missing_capability",
            "insufficient_cpu",
            "insufficient_cpu_mem",
            "insufficient_gpu",
            "insufficient_gpu_mem",
        ]
        for reason in reason_priority:
            if any(reason in candidate.get("reject_reasons", []) for candidate in candidates):
                return reason
        return "resource_unavailable"

    def _candidate_sort_key(self, candidate: Dict[str, Any], gpu_need: int):
        node_id = candidate["node_id"]
        if self.scheduling_policy in {"least-loaded", "spread"}:
            return (
                self.running_task_counts.get(node_id, 0),
                -candidate.get("available_cpu", 0),
                candidate.get("node_ip") or "",
            )
        if self.scheduling_policy == "prefer-gpu-free" and gpu_need == 0:
            gpu = candidate.get("available_resources", {}).get("gpu", {})
            has_free_gpu = gpu.get("available_count", 0) > 0
            return (
                0 if not has_free_gpu else 1,
                self.running_task_counts.get(node_id, 0),
                candidate.get("node_ip") or "",
            )
        return (candidate.get("order", 0),)

    def select_node(
        self,
        task_need_resources:dict,
        *,
        reservation_kind: str = "task",
    ) -> ResourceSelection:
        '''
        Select sufficient resources node
        '''
        cpu_need = task_need_resources["cpu"]
        cpu_mem_need = task_need_resources["cpu_mem"]
        gpu_need = task_need_resources["gpu"]
        gpu_mem_need = task_need_resources["gpu_mem"]
        assert(gpu_need <= 1)

        target_node_id = task_need_resources.get("node_id") or task_need_resources.get("target_node_id")
        required_capability = task_need_resources.get("required_capability")
        ray_nodes = self._ray_node_index()
        candidates = []

        for order, (node_id,node) in enumerate(self.nodes.items()):
            if target_node_id and node_id != target_node_id:
                continue

            reject_reasons = []
            selected_gpu_id = None
            alive = self._is_node_alive(node_id, ray_nodes)
            if not alive:
                reject_reasons.append("node_not_alive")
                if target_node_id == node_id:
                    reject_reasons.append("specified_node_unavailable")

            if node.available_resources.get('cpu', 0) < cpu_need:
                reject_reasons.append("insufficient_cpu")
            if node.available_resources.get('cpu_mem', 0) < cpu_mem_need:
                reject_reasons.append("insufficient_cpu_mem")
            if required_capability and not node.capabilities.get(required_capability):
                reject_reasons.append("missing_capability")

            if gpu_need > 0:
                gpu_options = []
                available_gpu_count = 0
                for gpu_id,gpu_resource in node.available_resources.get("gpu_resource", {}).items():
                    gpu_count = gpu_resource.get('gpu_num', 0)
                    available_gpu_count += gpu_count
                    if gpu_resource.get('gpu_mem', 0) >= gpu_mem_need and gpu_count >= gpu_need:
                        gpu_options.append(gpu_id)

                if available_gpu_count < gpu_need:
                    reject_reasons.append("insufficient_gpu")
                elif not gpu_options:
                    reject_reasons.append("insufficient_gpu_mem")
                else:
                    selected_gpu_id = sorted(gpu_options)[0]

            candidate = {
                "order": order,
                "node_id": node_id,
                "node_ip": node.node_ip,
                "role": "head" if node_id == self.head_node_id else "worker",
                "alive": alive,
                "registered": True,
                "running_task_count": self.running_task_counts.get(node_id, 0),
                "available_cpu": node.available_resources.get("cpu", 0),
                "available_resources": self._node_resource_snapshot(node),
                "capabilities": copy.deepcopy(node.capabilities),
                "reject_reasons": reject_reasons,
                "can_run": len(reject_reasons) == 0,
            }
            if selected_gpu_id is not None:
                candidate["selected_gpu_id"] = selected_gpu_id
            candidates.append(candidate)

        runnable_candidates = [candidate for candidate in candidates if candidate["can_run"]]
        decision = {
            "selected": False,
            "reason": None,
            "requested_resources": copy.deepcopy(task_need_resources),
            "scheduling_policy": self.scheduling_policy,
            "candidate_nodes": [
                {key: value for key, value in candidate.items() if key not in {"order", "available_cpu"}}
                for candidate in candidates
            ],
        }

        if not runnable_candidates:
            decision["reason"] = self._candidate_failure_reason(candidates, target_node_id)
            return ResourceSelection(None, decision)

        selected_candidate = sorted(
            runnable_candidates,
            key=lambda candidate: self._candidate_sort_key(candidate, gpu_need),
        )[0]
        node_id = selected_candidate["node_id"]
        node = self.nodes[node_id]
        gpu_id = selected_candidate.get("selected_gpu_id")

        self.nodes[node_id].available_resources['cpu'] -= cpu_need
        self.nodes[node_id].available_resources['cpu_mem'] -= cpu_mem_need
        if gpu_id is not None:
            self.nodes[node_id].available_resources['gpu_resource'][gpu_id]['gpu_mem'] -= gpu_mem_need
            self.nodes[node_id].available_resources['gpu_resource'][gpu_id]['gpu_num'] -= gpu_need
        self.nodes[node_id].last_resource_update_time = time.time()
        if reservation_kind == "task":
            self.running_task_counts[node_id] = self.running_task_counts.get(node_id, 0) + 1

        selected_node = SelectedNode(node_id=node_id,node_ip=node.node_ip,gpu_id=gpu_id)
        decision["selected"] = True
        decision["reason"] = "selected"
        decision["selected_node"] = {
            "node_id": selected_node.node_id,
            "node_ip": selected_node.node_ip,
            "gpu_id": selected_node.gpu_id,
            "capabilities": copy.deepcopy(node.capabilities),
        }
        return ResourceSelection(selected_node, decision)

    def release_task_resource(self,tasks:List[TaskRuntime]):
        '''
        Release resource according to task
        '''
        assert isinstance(tasks,list)
        for task in tasks:
            if getattr(task, "selected_node", None) is None:
                continue
            node_id = task.selected_node.node_id
            if node_id not in self.nodes:
                continue
            self.nodes[node_id].release_resource(task.resources,task.selected_node.gpu_id)
            self.running_task_counts[node_id] = max(0, self.running_task_counts.get(node_id, 0) - 1)

    def release_instance_resource(self,resource_detail:dict):
        '''
        Release resource
        '''
        node_id = resource_detail["node_id"]
        gpu_id = resource_detail["gpu_id"]
        resources = resource_detail["resources"]
        self.nodes[node_id].release_resource(resources,gpu_id)
        

    def start_worker(self,node_id:str,node_ip:str,resources:dict,capabilities:dict | None = None):
        '''
        Start worker node
        '''
        logger.info("New worker node join: node_id:%s,node_ip:%s", node_id,node_ip)
        gpu_resource = {int(k): v for k, v in resources['gpu_resource'].items()}
        resources["gpu_resource"] = gpu_resource
        capabilities = copy.deepcopy(capabilities or {"workspace_sandbox": True, "docker_sandbox": False})
        if node_id in self.nodes:
            registration_status = self.nodes[node_id].update_registration(node_ip, resources, capabilities)
        else:
            self.nodes[node_id] = Node(node_id,node_ip,resources,resources,capabilities)
            registration_status = "created"
        self.running_task_counts.setdefault(node_id, 0)
        return {
            "registration_status": registration_status,
            "node_id": node_id,
            "node_ip": node_ip,
            "resources": copy.deepcopy(self.nodes[node_id].total_resources),
            "capabilities": copy.deepcopy(self.nodes[node_id].capabilities),
            "registered_time": self.nodes[node_id].registered_time,
            "last_seen_time": self.nodes[node_id].last_seen_time,
        }
