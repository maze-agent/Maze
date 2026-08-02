import ray
import time
import logging
import copy
import uuid
from typing import Any,List,Dict
from maze.core.scheduler.dag_context import DAGContextManager
from maze.core.scheduler.runtime import SelectedNode
from maze.core.scheduler.runtime import TaskRuntime
from maze.core.scheduler.runtime import SelectedNode
from maze.core.local_models import scan_local_model_refs
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


class RayNodeQueryError(RuntimeError):
    """Raised when Ray cannot provide an authoritative node snapshot."""


class ResourceSelection:
    def __init__(
        self,
        selected_node: SelectedNode | None,
        decision: Dict[str, Any],
        lease_id: str | None = None,
    ):
        self.selected_node = selected_node
        self.decision = decision
        self.lease_id = lease_id

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

        self.available_resources['cpu'] = min(
            self.total_resources.get('cpu', 0),
            self.available_resources.get('cpu', 0) + cpu,
        )
        self.available_resources['cpu_mem'] = min(
            self.total_resources.get('cpu_mem', 0),
            self.available_resources.get('cpu_mem', 0) + cpu_mem,
        )
        
        if gpu_id is not None:
            total_gpu = self.total_resources.get('gpu_resource', {}).get(gpu_id)
            available_gpu = self.available_resources.get('gpu_resource', {}).get(gpu_id)
            if total_gpu is not None and available_gpu is not None:
                available_gpu['gpu_mem'] = min(
                    total_gpu.get('gpu_mem', 0),
                    available_gpu.get('gpu_mem', 0) + gpu_mem,
                )
                available_gpu['gpu_num'] = min(
                    total_gpu.get('gpu_num', 0),
                    available_gpu.get('gpu_num', 0) + gpu,
                )
        self.last_resource_update_time = time.time()
            
class ResourceManager():
    def __init__(self):
        self.head_node_id = None
        self.head_node_ip = None
        self.nodes:Dict[str,Node] = {}
        self.active_leases: Dict[str, Dict[str, Any]] = {}
        self.running_task_counts: Dict[str, int] = {}
        self.disabled_node_ids: set[str] = set()
        self.dag_context_manager = DAGContextManager()
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
                        {
                            **detect_agent_sandbox_capabilities(),
                            "local_models": scan_local_model_refs(),
                        },
                    )
                    self.running_task_counts.setdefault(self.head_node_id, 0)
                    return
                    
    def check_dead_node(self):
        try:
            nodes = self._ray_node_index()
        except RayNodeQueryError:
            return False
        for node_id in list(self.nodes):
            ray_node = nodes.get(node_id)
            if ray_node is not None and ray_node.get("Alive", False):
                continue
            self.nodes.pop(node_id, None)
            self.running_task_counts.pop(node_id, None)
            self.disabled_node_ids.discard(node_id)
            self.dag_context_manager.release_node_contexts(node_id)
        return True
                
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
        except Exception as exc:
            logger.warning("Unable to query the current Ray node membership: %s", exc)
            raise RayNodeQueryError("Current Ray node membership is unavailable") from exc

    def _is_node_alive(self, node_id: str, ray_nodes: Dict[str, Dict[str, Any]]):
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

    def _refresh_head_local_models(self):
        if self.head_node_id in self.nodes:
            self.nodes[self.head_node_id].capabilities["local_models"] = scan_local_model_refs()

    def get_cluster_resources(self):
        self._refresh_head_local_models()
        try:
            ray_nodes = self._ray_node_index()
            ray_query = {"status": "available"}
        except RayNodeQueryError:
            ray_nodes = {}
            ray_query = {
                "status": "unavailable",
                "error_code": "ray_cluster_unavailable",
            }
        registered_nodes = []

        for node_id, node in self.nodes.items():
            ray_node = ray_nodes.get(node_id)
            alive = (
                bool(ray_node.get("Alive", False))
                if ray_query["status"] == "available"
                else None
            )
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
                "disabled": node_id in self.disabled_node_ids,
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
                "local_models": copy.deepcopy(node.capabilities.get("local_models") or []),
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
                "local_models": [],
                "ray_resources": ray_node.get("Resources", {}),
            })

        return {
            "head_node_id": self.head_node_id,
            "head_node_ip": self.head_node_ip,
            "ray_query": ray_query,
            "scheduling_policy": self.scheduling_policy,
            "supported_scheduling_policies": copy.deepcopy(SUPPORTED_SCHEDULING_POLICIES),
            "disabled_node_ids": sorted(self.disabled_node_ids),
            "dag_contexts": self.dag_context_manager.snapshot(),
            "nodes": sorted(registered_nodes, key=lambda item: (item["role"] != "head", item["node_ip"] or "")),
            "unregistered_ray_nodes": sorted(unregistered_ray_nodes, key=lambda item: item["node_ip"] or ""),
        }

    def set_node_disabled(self, node_id: str, disabled: bool):
        if node_id == self.head_node_id and disabled:
            raise ValueError("head node cannot be disabled")
        if node_id not in self.nodes:
            raise KeyError(f"node not registered: {node_id}")
        if disabled:
            self.disabled_node_ids.add(node_id)
        else:
            self.disabled_node_ids.discard(node_id)
        return {
            "node_id": node_id,
            "disabled": node_id in self.disabled_node_ids,
            "cluster": self.get_cluster_resources(),
        }
            
            
    def stop_worker(self,node_id:str):
        '''
        Stop worker node
        '''
        del self.nodes[node_id]
        self.disabled_node_ids.discard(node_id)
        self.dag_context_manager.release_node_contexts(node_id)
        
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
        if all("node_disabled" in candidate.get("reject_reasons", []) for candidate in candidates):
            return "all_candidate_nodes_disabled"

        reason_priority = [
            "specified_node_unavailable",
            "node_disabled",
            "missing_capability",
            "missing_model",
            "avoided_after_failure",
            "insufficient_cpu",
            "insufficient_cpu_mem",
            "insufficient_gpu",
            "insufficient_gpu_mem",
        ]
        for reason in reason_priority:
            if any(reason in candidate.get("reject_reasons", []) for candidate in candidates):
                return reason
        return "resource_unavailable"

    def _candidate_sort_key(
        self,
        candidate: Dict[str, Any],
        gpu_need: int,
        workflow_id: str | None = None,
        affinity_node_id: str | None = None,
    ):
        node_id = candidate["node_id"]
        dag_context_key = ()
        if workflow_id:
            affinity_rank = 0 if affinity_node_id and node_id == affinity_node_id else 1
            if not affinity_node_id:
                affinity_rank = 0
            dag_context_key = (
                affinity_rank,
                self.dag_context_manager.node_context_load(node_id),
            )

        if self.scheduling_policy in {"least-loaded", "spread"}:
            return (
                *dag_context_key,
                self.running_task_counts.get(node_id, 0),
                -candidate.get("available_cpu", 0),
                candidate.get("node_ip") or "",
            )
        if self.scheduling_policy == "prefer-gpu-free" and gpu_need == 0:
            gpu = candidate.get("available_resources", {}).get("gpu", {})
            has_free_gpu = gpu.get("available_count", 0) > 0
            return (
                *dag_context_key,
                0 if not has_free_gpu else 1,
                self.running_task_counts.get(node_id, 0),
                candidate.get("node_ip") or "",
            )
        return (*dag_context_key, candidate.get("order", 0))

    def select_node(
        self,
        task_need_resources:dict,
        *,
        reservation_kind: str = "task",
        model_anchor: Dict[str, Any] | None = None,
        workflow_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        attempt: int | None = None,
        dispatch_id: str | None = None,
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
        avoid_node_ids = {
            str(node_id)
            for node_id in (task_need_resources.get("avoid_node_ids") or [])
            if node_id
        }
        required_capability = task_need_resources.get("required_capability")
        required_model = (model_anchor or {}).get("local_model")
        required_backend = (model_anchor or {}).get("backend") or "transformers"
        dag_context = self.dag_context_manager.get_context(workflow_id)
        affinity_node_id = dag_context.preferred_node_id if dag_context else None
        self._refresh_head_local_models()
        try:
            ray_nodes = self._ray_node_index()
        except RayNodeQueryError:
            return ResourceSelection(None, {
                "selected": False,
                "reason": "ray_cluster_unavailable",
                "requested_resources": copy.deepcopy(task_need_resources),
                "scheduling_policy": self.scheduling_policy,
                "dag_context": {
                    "workflow_id": workflow_id,
                    "preferred_node_id": affinity_node_id,
                    "preferred_node_ip": dag_context.preferred_node_ip if dag_context else None,
                    "affinity_active": affinity_node_id is not None,
                } if workflow_id else None,
                "candidate_nodes": [],
            })
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
            if node_id in avoid_node_ids:
                reject_reasons.append("avoided_after_failure")
            if node_id in self.disabled_node_ids:
                reject_reasons.append("node_disabled")
                if target_node_id == node_id:
                    reject_reasons.append("specified_node_unavailable")

            if node.available_resources.get('cpu', 0) < cpu_need:
                reject_reasons.append("insufficient_cpu")
            if node.available_resources.get('cpu_mem', 0) < cpu_mem_need:
                reject_reasons.append("insufficient_cpu_mem")
            if required_capability and not node.capabilities.get(required_capability):
                reject_reasons.append("missing_capability")
            if required_model:
                local_models = node.capabilities.get("local_models") or []
                has_model = any(
                    model.get("id") == required_model
                    and (model.get("backend") or "transformers") == required_backend
                    for model in local_models
                    if isinstance(model, dict)
                )
                if not has_model:
                    reject_reasons.append("missing_model")

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
                "disabled": node_id in self.disabled_node_ids,
                "registered": True,
                "running_task_count": self.running_task_counts.get(node_id, 0),
                "dag_context_affinity": bool(affinity_node_id and node_id == affinity_node_id),
                "dag_context_load": self.dag_context_manager.node_context_load(node_id),
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
            "dag_context": {
                "workflow_id": workflow_id,
                "preferred_node_id": affinity_node_id,
                "preferred_node_ip": dag_context.preferred_node_ip if dag_context else None,
                "affinity_active": affinity_node_id is not None,
            } if workflow_id else None,
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
            key=lambda candidate: self._candidate_sort_key(
                candidate,
                gpu_need,
                workflow_id,
                affinity_node_id,
            ),
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

        context, context_created = self.dag_context_manager.record_selection(workflow_id, node_id, node.node_ip)
        selected_node = SelectedNode(node_id=node_id,node_ip=node.node_ip,gpu_id=gpu_id)
        lease_id = str(uuid.uuid4())
        self.active_leases[lease_id] = {
            "lease_id": lease_id,
            "reservation_kind": reservation_kind,
            "run_id": run_id or workflow_id,
            "task_id": task_id,
            "attempt": attempt,
            "dispatch_id": dispatch_id,
            "node_id": node_id,
            "gpu_id": gpu_id,
            "resources": copy.deepcopy(task_need_resources),
        }
        decision["selected"] = True
        decision["reason"] = "selected"
        decision["lease_id"] = lease_id
        decision["selected_node"] = {
            "node_id": selected_node.node_id,
            "node_ip": selected_node.node_ip,
            "gpu_id": selected_node.gpu_id,
            "capabilities": copy.deepcopy(node.capabilities),
        }
        if context is not None:
            decision["dag_context"] = {
                **context.to_dict(),
                "context_created": context_created,
                "selected_node_id": node_id,
                "affinity_hit": bool(affinity_node_id and node_id == affinity_node_id),
            }
        return ResourceSelection(selected_node, decision, lease_id)

    def release_dag_context(self, workflow_id: str | None) -> bool:
        return self.dag_context_manager.release_context(workflow_id)

    def _recompute_node_available_resources(self, node_id: str) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            return

        reserved_cpu = 0
        reserved_cpu_mem = 0
        reserved_gpu_resources: Dict[int, Dict[str, Any]] = {}
        for lease in self.active_leases.values():
            if lease.get("node_id") != node_id:
                continue
            resources = lease.get("resources") or {}
            reserved_cpu += resources.get("cpu", 0)
            reserved_cpu_mem += resources.get("cpu_mem", 0)
            gpu_id = lease.get("gpu_id")
            if gpu_id is None:
                continue
            reserved_gpu = reserved_gpu_resources.setdefault(
                gpu_id,
                {"gpu_num": 0, "gpu_mem": 0},
            )
            reserved_gpu["gpu_num"] += resources.get("gpu", 0)
            reserved_gpu["gpu_mem"] += resources.get("gpu_mem", 0)

        available = copy.deepcopy(node.total_resources)
        available["cpu"] = max(0, available.get("cpu", 0) - reserved_cpu)
        available["cpu_mem"] = max(
            0,
            available.get("cpu_mem", 0) - reserved_cpu_mem,
        )
        for gpu_id, gpu_resource in available.get("gpu_resource", {}).items():
            reserved_gpu = reserved_gpu_resources.get(gpu_id, {})
            gpu_resource["gpu_num"] = max(
                0,
                gpu_resource.get("gpu_num", 0) - reserved_gpu.get("gpu_num", 0),
            )
            gpu_resource["gpu_mem"] = max(
                0,
                gpu_resource.get("gpu_mem", 0) - reserved_gpu.get("gpu_mem", 0),
            )
        node.available_resources = available
        node.last_resource_update_time = time.time()

    def release_lease(self, lease_id: str | None) -> bool:
        lease = self.active_leases.pop(lease_id, None)
        if lease is None:
            return False

        node_id = lease["node_id"]
        if node_id in self.nodes:
            if lease["reservation_kind"] == "task":
                self.running_task_counts[node_id] = max(
                    0,
                    self.running_task_counts.get(node_id, 0) - 1,
                )
            self._recompute_node_available_resources(node_id)
        return True

    def release_task_resource(self,tasks:List[TaskRuntime]):
        '''
        Release resource according to task
        '''
        assert isinstance(tasks,list)
        for task in tasks:
            self.release_lease(getattr(task, "lease_id", None))

    def release_instance_resource(self,resource_detail:dict):
        '''
        Release an instance reservation.
        '''
        self.release_lease(resource_detail.get("lease_id"))
        

    def start_worker(self,node_id:str,node_ip:str,resources:dict,capabilities:dict | None = None):
        '''
        Start worker node
        '''
        try:
            ray_nodes = self._ray_node_index()
        except RayNodeQueryError:
            return {
                "registration_status": "ray_cluster_unavailable",
                "error_code": "ray_cluster_unavailable",
                "error": {
                    "code": "ray_cluster_unavailable",
                    "message": "Current Ray node membership is temporarily unavailable",
                },
                "node_id": node_id,
                "node_ip": node_ip,
            }
        ray_node = ray_nodes.get(node_id)
        if ray_node is None or not ray_node.get("Alive", False):
            live_node_ids = sorted(
                current_node_id
                for current_node_id, current_node in ray_nodes.items()
                if current_node.get("Alive", False)
            )
            logger.warning(
                "Rejecting worker from a different Ray cluster: node_id=%s node_ip=%s",
                node_id,
                node_ip,
            )
            return {
                "registration_status": "cluster_mismatch",
                "error_code": "ray_cluster_mismatch",
                "error": {
                    "code": "ray_cluster_mismatch",
                    "message": "Worker node is not alive in the current Maze Ray cluster",
                    "worker_node_id": node_id,
                    "current_cluster_node_ids": live_node_ids,
                },
                "node_id": node_id,
                "node_ip": node_ip,
            }

        ray_node_ip = ray_node.get("NodeManagerAddress")
        if ray_node_ip and ray_node_ip != node_ip:
            logger.warning(
                "Using Ray's canonical worker address: node_id=%s requested_ip=%s ray_ip=%s",
                node_id,
                node_ip,
                ray_node_ip,
            )
            node_ip = str(ray_node_ip)

        resources = copy.deepcopy(resources)
        gpu_resource = {int(k): v for k, v in resources['gpu_resource'].items()}
        resources["gpu_resource"] = gpu_resource
        capabilities = copy.deepcopy(capabilities or {"workspace_sandbox": True, "docker_sandbox": False})

        removed_node_ids = []
        for existing_node_id, existing_node in list(self.nodes.items()):
            if existing_node_id == node_id or existing_node.node_ip != node_ip:
                continue
            existing_ray_node = ray_nodes.get(existing_node_id)
            if existing_ray_node is not None and existing_ray_node.get("Alive", False):
                continue
            self.nodes.pop(existing_node_id, None)
            self.running_task_counts.pop(existing_node_id, None)
            self.disabled_node_ids.discard(existing_node_id)
            self.dag_context_manager.release_node_contexts(existing_node_id)
            removed_node_ids.append(existing_node_id)

        if node_id in self.nodes:
            registration_status = self.nodes[node_id].update_registration(node_ip, resources, capabilities)
        else:
            self.nodes[node_id] = Node(node_id,node_ip,resources,resources,capabilities)
            registration_status = "created"
        self.running_task_counts[node_id] = sum(
            lease.get("reservation_kind") == "task"
            and lease.get("node_id") == node_id
            for lease in self.active_leases.values()
        )
        self._recompute_node_available_resources(node_id)
        log_registration = logger.debug if registration_status == "already_registered" else logger.info
        log_registration(
            "Worker registration %s: node_id=%s node_ip=%s",
            registration_status,
            node_id,
            node_ip,
        )
        return {
            "registration_status": registration_status,
            "node_id": node_id,
            "node_ip": node_ip,
            "resources": copy.deepcopy(self.nodes[node_id].total_resources),
            "capabilities": copy.deepcopy(self.nodes[node_id].capabilities),
            "registered_time": self.nodes[node_id].registered_time,
            "last_seen_time": self.nodes[node_id].last_seen_time,
            "removed_stale_node_ids": removed_node_ids,
        }
