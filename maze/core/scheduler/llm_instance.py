from collections import defaultdict
import ray
import logging
import os
import subprocess
import time
import requests

logger = logging.getLogger(__name__)

class LlmInstanceMessage():
    def __init__(self, message_type: str, message_data: dict) -> None:
        self.message_type = message_type
        self.message_data = message_data

def run_llm_server(model: str, port: int, gpu_id:str,**kwargs):
    import os 
    os.environ["CUDA_VISIBLE_DEVICES"] = gpu_id


@ray.remote
class LLMServerActor:
    def __init__(self, model: str, gpu_id: int,**kwargs):
        self.model = model
        self.gpu_id = str(gpu_id)
        self.host = "0.0.0.0"
        self.port = self._get_free_port()
        self.extra_args = kwargs
        self.server_process = None
        self.ready = False

    def get_port(self):
        return self.port

    def _get_free_port(self):
        'Get a free port on the local machine(from 8000).'
        import socket
        port = 8000
        while port <= 65535:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(('127.0.0.1', port))
                    return str(port)
                except OSError:
                    # Port is already in use, try next one
                    port += 1
        raise RuntimeError("No free port found in range 8000-65535")

    def start_server(self, timeout: int = 120):
        if self.ready:
            return True

        cmd = [
            "python", "-m", "vllm.entrypoints.openai.api_server",
            "--model", self.model,
            "--host", self.host,
            "--port", self.port
        ]
        if self.extra_args:
            cmd.extend(self.extra_args)

       
        env = os.environ.copy()
        if self.gpu_id is not None:
            env["CUDA_VISIBLE_DEVICES"] = self.gpu_id
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,env=env)

        # wait for server to be ready
        health_url = f"http://127.0.0.1:{self.port}/health"
        start_time = time.time()
        self.ready = False
        while time.time() - start_time < timeout:
            try:
                resp = requests.get(health_url, timeout=2)
                if resp.status_code == 200:
                    self.ready = True
                    break
            except requests.RequestException:
                pass
            time.sleep(1)

        if not self.ready:
            self.proc.terminate()
            self.proc.wait()
            print(f"[ERROR] vLLM instance {self.model} failed to start within {timeout}s.")
            return False

        print(f"[INFO] vLLM instance {self.model} is ready.")
        return True


    def stop_server(self, timeout: int = 5):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=timeout)
            print(f"[INFO] vLLM instance {self.model} stopped successfully.")
        except subprocess.TimeoutExpired:
            print(f"[WARN] Force killing vLLM instance {self.model}.")
            self.proc.kill()
            self.proc.wait()


class LlmInstanceManager():
    def __init__(
        self,
        max_requests_per_instance: int = 8,
        scale_out_threshold: float = 1.0,
        idle_scale_in_seconds: float = 300.0,
    ):
        self.id_to_instance_addr = {}
        self.id_to_instance_actor = {}
        self.id_to_resource_detail = {}
        self.id_to_instance_metadata = {}
        self.model_to_instances = defaultdict(set)
        self.workflow_model_affinity = {}
        self.pending_model_requests = defaultdict(int)
        self.pending_model_anchors = {}
        self.deploying_model_counts = defaultdict(int)
        self.max_requests_per_instance = max(1, int(max_requests_per_instance))
        self.scale_out_threshold = max(0.1, float(scale_out_threshold))
        self.idle_scale_in_seconds = max(1.0, float(idle_scale_in_seconds))

    def get_instance_resource_detail(self, instance_id:str):
        return self.id_to_resource_detail[instance_id]

    def register_instance(
        self,
        instance_id: str,
        model: str,
        node_ip: str,
        node_id: str,
        gpu_id: int,
        port: str | int,
        resources: dict,
        backend: str = "vllm",
    ):
        addr = node_ip + ":" + str(port)
        endpoint = "http://" + addr
        backend = backend or "vllm"
        self.id_to_instance_addr[instance_id] = addr
        self.id_to_resource_detail[instance_id] = {
            "node_id": node_id,
            "gpu_id": gpu_id,
            "resources": resources,
        }
        metadata = {
            "instance_id": instance_id,
            "model": model,
            "backend": backend,
            "node_id": node_id,
            "node_ip": node_ip,
            "gpu_id": gpu_id,
            "port": str(port),
            "addr": addr,
            "endpoint": endpoint,
            "inflight_requests": 0,
            "total_routed_requests": 0,
            "created_time": time.time(),
            "last_used_time": None,
        }
        self.id_to_instance_metadata[instance_id] = metadata
        self.model_to_instances[(model, backend)].add(instance_id)
        if self.deploying_model_counts[(model, backend)] > 0:
            self.deploying_model_counts[(model, backend)] -= 1
        self.pending_model_requests[(model, backend)] = 0
        return metadata

    def _model_key_from_anchor(self, model_anchor: dict | None):
        model_anchor = model_anchor or {}
        model = model_anchor.get("local_model") or model_anchor.get("model")
        if not model:
            return None
        backend = model_anchor.get("backend") or model_anchor.get("engine") or "vllm"
        return model, backend

    def record_model_demand(self, model_anchor: dict | None, count: int = 1):
        key = self._model_key_from_anchor(model_anchor)
        if key is None:
            return None
        self.pending_model_requests[key] += max(1, int(count))
        self.pending_model_anchors[key] = dict(model_anchor or {})
        return {
            "model": key[0],
            "backend": key[1],
            "pending_requests": self.pending_model_requests[key],
        }

    def mark_model_deploying(self, model: str, backend: str = "vllm"):
        self.deploying_model_counts[(model, backend)] += 1

    def clear_model_deploying(self, model: str, backend: str = "vllm"):
        key = (model, backend)
        if self.deploying_model_counts[key] > 0:
            self.deploying_model_counts[key] -= 1

    def _estimated_gpu_mem(self, model_anchor: dict | None):
        model_anchor = model_anchor or {}
        for key in ("gpu_mem", "estimated_gpu_mem_mb", "estimated_gpu_memory_mb"):
            value = model_anchor.get(key)
            if value:
                return int(float(value))
        weight_bytes = model_anchor.get("estimated_weight_memory_bytes")
        if weight_bytes:
            return max(1, int(float(weight_bytes) / (1024 * 1024) * 1.2))
        return 0

    def route_model_request(self, workflow_id: str | None, model_anchor: dict | None):
        model_anchor = model_anchor or {}
        key = self._model_key_from_anchor(model_anchor)
        if key is None:
            return None

        model, backend = key
        affinity_key = (workflow_id, model, backend)
        candidates = [
            self.id_to_instance_metadata[instance_id]
            for instance_id in sorted(self.model_to_instances.get((model, backend), set()))
            if instance_id in self.id_to_instance_metadata
        ]
        if not candidates:
            self.record_model_demand(model_anchor)
            return None

        affinity_instance_id = self.workflow_model_affinity.get(affinity_key)
        selected = None
        affinity_hit = False
        if affinity_instance_id in self.id_to_instance_metadata:
            affinity_candidate = self.id_to_instance_metadata[affinity_instance_id]
            if affinity_candidate.get("inflight_requests", 0) < self.max_requests_per_instance:
                selected = affinity_candidate
                affinity_hit = True

        if selected is None:
            available = [
                candidate
                for candidate in candidates
                if candidate.get("inflight_requests", 0) < self.max_requests_per_instance
            ]
            if not available:
                available = candidates
                self.record_model_demand(model_anchor)
            selected = min(
                available,
                key=lambda candidate: (
                    candidate.get("inflight_requests", 0),
                    candidate.get("last_used_time") or 0.0,
                    candidate.get("instance_id") or "",
                ),
            )
            if workflow_id:
                self.workflow_model_affinity[affinity_key] = selected["instance_id"]

        selected["inflight_requests"] = selected.get("inflight_requests", 0) + 1
        selected["total_routed_requests"] = selected.get("total_routed_requests", 0) + 1
        selected["last_used_time"] = time.time()
        return {
            "model": model,
            "backend": backend,
            "instance_id": selected["instance_id"],
            "endpoint": selected["endpoint"],
            "addr": selected["addr"],
            "node_id": selected["node_id"],
            "node_ip": selected["node_ip"],
            "gpu_id": selected["gpu_id"],
            "workflow_id": workflow_id,
            "affinity_hit": affinity_hit,
            "inflight_requests": selected["inflight_requests"],
        }

    def release_model_route(self, model_route: dict | None):
        if not model_route:
            return
        instance_id = model_route.get("instance_id")
        metadata = self.id_to_instance_metadata.get(instance_id)
        if metadata is None:
            return
        metadata["inflight_requests"] = max(0, metadata.get("inflight_requests", 0) - 1)
        metadata["last_used_time"] = time.time()

    def snapshot(self):
        return {
            "instances": {
                instance_id: dict(metadata)
                for instance_id, metadata in self.id_to_instance_metadata.items()
            },
            "workflow_model_affinity": {
                "|".join(str(part) for part in key): value
                for key, value in self.workflow_model_affinity.items()
            },
            "pending_model_requests": {
                "|".join(key): value
                for key, value in self.pending_model_requests.items()
            },
            "deploying_model_counts": {
                "|".join(key): value
                for key, value in self.deploying_model_counts.items()
            },
            "max_requests_per_instance": self.max_requests_per_instance,
            "scale_out_threshold": self.scale_out_threshold,
            "idle_scale_in_seconds": self.idle_scale_in_seconds,
        }

    def scale_out_recommendations(self):
        recommendations = []
        for (model, backend), pending_requests in list(self.pending_model_requests.items()):
            if pending_requests <= 0:
                continue
            active_count = len(self.model_to_instances.get((model, backend), set()))
            deploying_count = self.deploying_model_counts.get((model, backend), 0)
            denominator = active_count + deploying_count
            ratio = float("inf") if denominator == 0 else pending_requests / denominator
            if denominator > 0 and ratio <= self.scale_out_threshold:
                continue
            model_anchor = self.pending_model_anchors.get((model, backend), {})
            recommendations.append({
                "model": model,
                "backend": backend,
                "pending_requests": pending_requests,
                "active_instances": active_count,
                "deploying_instances": deploying_count,
                "pending_per_instance": ratio,
                "gpu_mem": self._estimated_gpu_mem(model_anchor),
                "model_anchor": dict(model_anchor),
                "reason": "no_active_instance" if active_count == 0 else "pending_ratio_exceeded",
            })
        return recommendations

    def lru_scale_in_candidates(self, now: float | None = None, idle_seconds: float | None = None):
        now = now or time.time()
        idle_seconds = self.idle_scale_in_seconds if idle_seconds is None else float(idle_seconds)
        candidates = []
        for metadata in self.id_to_instance_metadata.values():
            if metadata.get("inflight_requests", 0) > 0:
                continue
            last_used_time = metadata.get("last_used_time") or metadata.get("created_time") or now
            idle_for = now - last_used_time
            if idle_for < idle_seconds:
                continue
            candidates.append({
                **dict(metadata),
                "idle_for_seconds": idle_for,
                "reason": "lru_idle",
            })
        candidates.sort(key=lambda item: (item.get("last_used_time") or item.get("created_time") or 0.0))
        return candidates

    def start_llm_instance(
        self,
        instance_id: str,
        model: str,
        node_ip: str,
        node_id: str,
        gpu_id: int,
        resources: dict,
        backend: str = "vllm",
    ):
        actor = LLMServerActor.options(
            scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                #num_cpus=0,
                node_id=node_id,
                soft=False
            ),
        ).remote(model=model, gpu_id=gpu_id)
        ray.get(actor.start_server.remote())

        self.id_to_instance_actor[instance_id] = actor
        port = ray.get(actor.get_port.remote())
        self.register_instance(instance_id, model, node_ip, node_id, gpu_id, port, resources, backend=backend)
        
        return port

    def stop_llm_instance(self, instance_id:str):
        actor = self.id_to_instance_actor.pop(instance_id, None)
        if actor is not None:
            actor.stop_server.remote()
        metadata = self.id_to_instance_metadata.pop(instance_id, None)
        if metadata is not None:
            self.model_to_instances[(metadata["model"], metadata["backend"])].discard(instance_id)
            for key, value in list(self.workflow_model_affinity.items()):
                if value == instance_id:
                    del self.workflow_model_affinity[key]
        self.id_to_instance_addr.pop(instance_id, None)
        self.id_to_resource_detail.pop(instance_id, None)
