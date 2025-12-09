from ast import List
import ray
import uuid
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
    def __init__(self):
        self.id_to_instance_addr = {}
        self.id_to_instance_actor = {}
        self.id_to_resource_detail = {}

    def get_instance_resource_detail(self, instance_id:str):
        return self.id_to_resource_detail[instance_id]

    def start_llm_instance(self, instance_id:str, model:str, node_ip:str, node_id:str, gpu_id:int, resources:dict):
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
        self.id_to_instance_addr[instance_id] = node_ip + ":" + port

        resources_detail = {
            "node_id": node_id,
            "gpu_id": gpu_id,
            "resources": resources
        }
        self.id_to_resource_detail[instance_id] = resources_detail
        
        return port

    def stop_llm_instance(self, instance_id:str):
        self.id_to_instance_actor[instance_id].stop_server.remote()
        del self.id_to_instance_actor[instance_id]
        del self.id_to_instance_addr[instance_id]
        del self.id_to_resource_detail[instance_id]

