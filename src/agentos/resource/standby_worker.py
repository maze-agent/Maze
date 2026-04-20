import ray
import os
import time
import cloudpickle
import gc
import tracemalloc
import redis
from typing import List, Dict, Optional

# num_gpus=0  Lookahead  Ray 
@ray.remote(num_cpus=0.1, num_gpus=0)
class StandbyWorker:
    def __init__(self, node_id: str, worker_type: str = "gpu"):
        """
        worker_type: 'gpu' ( torch/vllm)  'cpu' ()
        """
        self.node_id = node_id
        self.worker_type = worker_type
        # print(f"🔥 [StandbyWorker] Warming up ({worker_type}) on {node_id}...")
        # --- 1.  (Resource-Aware Warmup) ---
        self.torch = None
        if self.worker_type == "gpu":
            #  GPU  400MB+ 
            import torch
            from transformers import AutoTokenizer, AutoModelForCausalLM
            self.torch = torch
        # CPU/IO 
        import numpy
        self.ready = True
        print(f"✅ [StandbyWorker] Ready ({worker_type}) on {node_id}. PID: {os.getpid()}")

    def execute(self, serialized_func: bytes, ctx_actor, task_id: str, redis_host: str, redis_port: int, task_type: str, gpu_indices: Optional[List[int]]) -> Dict:
        worker_start_exec_time = time.time()
        # --- 2. JIT Binding ( GPU ) ---
        if task_type == "gpu" and gpu_indices:
            if self.worker_type != "gpu":
                return {"status": "failed", "err_msg": "Worker type mismatch: GPU task dispatched to CPU worker"}
            visible_devices = ",".join(map(str, gpu_indices))
            #  PyTorch  GPU
            os.environ["CUDA_VISIBLE_DEVICES"] = visible_devices
            print(f"⚡ [StandbyWorker] JIT Binding: Task {task_id} bound to GPUs {visible_devices}")
        try:
            func = cloudpickle.loads(serialized_func)
            # --- section ---
            #  func  execution logic
            result = func(ctx_actor)
            #

            if self.worker_type == "gpu" and self.torch:
                self.torch.cuda.empty_cache()
            gc.collect()
            if result:
                r = redis.Redis(host=redis_host, port=redis_port)
                r.set(f"result:{task_id}", result)
            return {"status": "finished", "worker_start_exec_time": worker_start_exec_time}

        except Exception as e:
            error_message = str(e)
            print(f"❌ [StandbyWorker] Task {task_id} failed: {error_message}")
            return {"status": "failed", "err_msg": error_message}