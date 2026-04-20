import ray
import os
import subprocess
import time
import requests
import json
import signal
from abc import ABC, abstractmethod
from typing import Optional, Dict, List, Tuple

# ==============================================================================
# 1.  (The "Contract")
# ==============================================================================
class ExecutionBackend(ABC):
    """
    definemodel
    """
    
    @abstractmethod
    def get_backend_type(self) -> str:
        """stringe.g. 'vllm'  'huggingface'"""
        pass

    @abstractmethod
    def deploy(self, node_id: str, gpu_index: int, model_name: str) -> Optional[Dict]:
        """
        model
        api_urlNone
        """
        pass

    @abstractmethod
    def undeploy(self, node_id: str, gpu_index: int) -> bool:
        """"""
        pass

# ==============================================================================
# 2. vLLM  (VLLMBackend and its Helper)
# ==============================================================================

@ray.remote
class VLLMRunner:
    """
    vLLMRay Actor
    """
    def __init__(self, proj_path: str, model_folder: str, model_name: str, gpu_indices: List[int], vllm_params: Dict):
        self.proj_path = proj_path
        self.model_path = os.path.join(proj_path, model_folder, model_name)
        self.gpu_indices = gpu_indices
        self.vllm_params = vllm_params
        self.process = None
        self.port = self._find_free_port()
        self.node_ip = ray.util.get_node_ip_address()
        self.api_url = f"http://{self.node_ip}:{self.port}"
        # ---  ---
        self.stdout_log = None
        self.stderr_log = None

    def _find_free_port(self):
        """"""
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            return s.getsockname()[1]

    def start_server(self) -> None:
        """
        vLLMserviceemiterroremitfile
        """
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available!")
        print(f"CUDA devices: {torch.cuda.device_count()}")

        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = ",".join(map(str, self.gpu_indices))
        print(f"  -> Starting vLLM server and model_path: {self.model_path} on GPUs {self.gpu_indices} at port {self.port}...")
        vllm_command = [
            "python", "-m", "vllm.entrypoints.openai.api_server",
            "--model", self.model_path,
            "--host", "0.0.0.0",
            "--port", str(self.port),
        ]
        
        for key, value in self.vllm_params.items():
            vllm_command.append(f"--{key}")
            if value is not None:
                 vllm_command.append(str(value))
        vllm_command.append("--trust-remote-code")
        try:
            model_display_name = self.vllm_params.get("served-model-name", os.path.basename(self.model_path))
            print(f"🚀 Launching vLLM for model '{model_display_name}' on GPUs {self.gpu_indices} at port {self.port}...")
            # --- vLLM ---
            log_dir = os.path.join(self.proj_path, "src", "core", "logs", "vllm_logs")
            os.makedirs(log_dir, exist_ok=True)
            print(f"  -> vllm log_dir: {log_dir}")
            model_name_safe = model_display_name.replace("/", "_") #

            
            stdout_log_path = os.path.join(log_dir, f"{model_name_safe}_{self.port}_stdout.log")
            stderr_log_path = os.path.join(log_dir, f"{model_name_safe}_{self.port}_stderr.log")

            print(f"  -> 📜 vLLM stdout log: {stdout_log_path}")
            print(f"  -> 📜 vLLM stderr log: {stderr_log_path}")

            self.stdout_log = open(stdout_log_path, 'w')
            self.stderr_log = open(stderr_log_path, 'w')
            
            self.process = subprocess.Popen(
                vllm_command, 
                env=env, 
                stdout=self.stdout_log, 
                stderr=self.stderr_log,
                preexec_fn= os.setsid
            )
            # ---  ---

        except Exception as e:
            print(f"❌ vLLM process Popen failed for model '{model_display_name}': {e}")
            if self.process: self.process.kill()

    def stop_server(self):
        if self.process:
            print(f"🛑 Stopping vLLM server process group at port {self.port}...")
            try:
                # --- 2 ---
                # ID (pgid)
                pgid = os.getpgid(self.process.pid)
                os.killpg(pgid, signal.SIGTERM) # SIGTERM
                
                #

                self.process.wait(timeout=5)
                print(f"  -> Process group for port {self.port} terminated.")
            except (ProcessLookupError, TimeoutError) as e:
                 print(f"  -> Graceful shutdown failed ({e}), attempting forceful kill...")
                 try:
                     # SIGKILL
                     os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                 except Exception as kill_e:
                     print(f"  -> Forceful kill failed: {kill_e}")
            
            self.process = None
        
        if self.stdout_log: self.stdout_log.close()
        if self.stderr_log: self.stderr_log.close()
        return True

    def get_api_url(self) -> str:
        return self.api_url

class VLLMBackend(ExecutionBackend):
    """
    vLLM
    vLLM
    """
    def __init__(self, resource_manager, proj_path, model_folder, models_config_path):
        self.resource_manager = resource_manager 
        self.proj_path= proj_path
        self.model_folder = model_folder
        self.active_runners = {} # : {(node_id, gpu_index): actor_handle}
        try:
            with open(models_config_path, 'r') as f:
                self.model_config = json.load(f)
            print("✅ VLLMBackend: Model configuration loaded from 'models.json'.")
        except Exception as e:
            self.model_config = {}
            print("😭 VLLMBackend: Model configuration loaded from 'models.json'.")

    def get_backend_type(self) -> str:
        return "vllm"

    def deploy(self, node_id: str, gpu_indices: List[int], model_name: str):
        if not gpu_indices: return None
        key = (node_id, frozenset(gpu_indices))
        if key in self.active_runners: return
        runner_name = f"vllm_runner_{node_id}_{'_'.join(map(str, gpu_indices))}"
        print(f"  -> Assigning Actor name: {runner_name}")
        model_info = self.model_config.get(model_name)
        if not model_info: return
        INTERNAL_SCHEDULER_KEYS = ['tensor-parallel-size', 'max-model-len']
        vllm_params = {k: v for k, v in model_info.items() if k in INTERNAL_SCHEDULER_KEYS}
        vllm_params["served-model-name"] = model_name

        try:
            runner_actor = VLLMRunner.options(
                name= runner_name,
                num_gpus= len(gpu_indices),
                scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(node_id, soft=False)
            ).remote(self.proj_path, self.model_folder, self.model_config.get(model_name).get("path"), gpu_indices, vllm_params)
            runner_actor.start_server.remote()
            self.active_runners[key] = runner_actor
            print(f"⏳ VLLMBackend: Deployment initiated for '{model_name}' on Node {node_id[:6]}/GPUs {gpu_indices}.")
        
        except Exception as e:
            print(f"❌ VLLMBackend deployment failed to initiate: {e}")


    def undeploy(self, node_id: str, gpu_indices: List[int]) -> bool:
        if not gpu_indices: return False
        key = (node_id, frozenset(gpu_indices))
        runner_actor = self.active_runners.pop(key, None)
        
        if runner_actor:
            try:
                print(f"  -> 📩 Sending stop signal to VLLMRunner on node {node_id}...")
                #

                ray.get(runner_actor.stop_server.remote(), timeout=15) 
                print(f"  -> ✅ VLLMRunner on node {node_id} stopped gracefully.")
            except ray.exceptions.ActorDiedError:
                #  Actor 
                #  Actor 
                print(f"  -> ⚠️  VLLMRunner actor died during stop command (as expected if node is terminating). Continuing cleanup.")
            except ray.exceptions.ActorUnavailableError as e:
                # Actor  SIGTERM
                print(f"  -> ⚠️  VLLMRunner actor unavailable (likely already terminated): {e}. Continuing cleanup.")
            except ray.exceptions.RayTaskError as e:
                #  Actor  stop_server 
                print(f"  -> ❌ Error occurred inside the VLLMRunner's stop_server method: {e}")
            except Exception as e:
                #

                print(f"  -> ❌ An unexpected error occurred during undeploy: {type(e).__name__} - {e}")
            finally:
                #  kill Actor 
                #  Ray 
                print(f"  -> 💥 Killing actor handle to ensure resource release in Ray.")
                ray.kill(runner_actor)

            self.resource_manager.update_gpu_state(node_id, gpu_indices, {
                "status": "FREE",
                "model_name": None, 
                "backend_type": None, 
                "request_api_url": None,
                "runner_key": None,
                "deployment_finish_time": None})
            print(f"  -> 🟢 Resources for node {node_id}, GPUs {gpu_indices} have been marked as FREE.")
            return True
        return False

    def is_server_ready(self, node_id: str, gpu_indices: List[int]) -> Tuple[bool, Optional[str]]:
        """
        viaHTTPrequestvLLMservicemodel
        """
        # key
        key = (node_id, frozenset(gpu_indices))
        runner_actor = self.active_runners.get(key)
        if not runner_actor:
            return False, None
        try:
            api_url = ray.get(runner_actor.get_api_url.remote())
            health_check_url = f"{api_url}/health"
            response = requests.get(health_check_url, timeout=3)
            # --- JSON ---
            # .raise_for_status() 2xx 404, 500
            response.raise_for_status() 
            # 2xx
            print(f"  -> ✅ Health check successful for {api_url}. Server is ready.")
            return True, api_url
            # ---  ---
        except requests.exceptions.RequestException as e:
            # DNS2xx
            if 'api_url' in locals():
                 print(f"   -> [Debug] Health check for {api_url} failed: {type(e).__name__} - {e}")
            return False, None

# ==============================================================================
# 3. HuggingFace Transformers 
# ==============================================================================

class HuggingFaceBackend(ExecutionBackend):
    """
    useHuggingFace Transformersmodel
    """
    def get_backend_type(self) -> str:
        return "huggingface"

    def deploy(self, node_id: str, gpu_index: int, model_name: str) -> Optional[Dict]:
        #

        print(f"✅ Preparing to run '{model_name}' with HuggingFaceBackend on Node {node_id[:6]}, GPU {gpu_index}.")
        # GPU
        # self.resource_manager.update_gpu_state(...) # TaskScheduler
        return {"backend_type": "huggingface"}

    def undeploy(self, node_id: str, gpu_index: int) -> bool:
        # remote_task_runner
        print(f"✅ HuggingFaceBackend task finished on Node {node_id[:6]}, GPU {gpu_index}. Resources are auto-released.")
        return True