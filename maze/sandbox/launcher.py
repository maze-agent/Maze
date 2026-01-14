
import asyncio
import docker
import ray
from typing import Any, Dict, Optional
import os
from maze.sandbox import CPU_PERIOD
import docker
import tarfile
import io

@ray.remote
class SandboxActor:
    def __init__(self, cpu_nums: int, gpu_nums: int, memory_mb: int):
        self.cpu_nums = cpu_nums
        self.gpu_nums = gpu_nums
        self.memory_mb = memory_mb
        self.client = docker.from_env()
        self.container = None
        self.container_id = None
         
        self._start_container()
        
    def _start_container(self):
        """Start sandbox container"""
        try:
            
            # Start container
            gpu_ids_str = os.environ.get('CUDA_VISIBLE_DEVICES', '') # e.g., "0" or "0,1"
            if gpu_ids_str:
                device_ids = gpu_ids_str.split(',')
            else:
                device_ids = [] 
            self.container = self.client.containers.run(
                image="pytorch/pytorch",
                command="tail -f /dev/null", 
                user='nobody',
                detach=True,
                remove=False, 
                network_mode='default',
                cpu_period=CPU_PERIOD,
                cpu_quota=CPU_PERIOD * self.cpu_nums,
                mem_limit=f"{self.memory_mb}m",
                device_requests=[
                docker.types.DeviceRequest(
                        device_ids=device_ids,
                        capabilities=[['gpu']]  # ⚠️ 强烈建议加上这个！
                    )
                ]   
            )
            
            self.container_id = self.container.id
            print(f"Sandbox container started, ID: {self.container_id[:12]}")
            
        except Exception as e:
            print(f"Failed to start sandbox container: {str(e)}")
            raise e
    
    async def run_code(self, code: str, timeout: float = 10.0) -> Dict[str, any]:
        """Execute code in container"""
        if not self.container:
            return {
                "stdout": "",
                "stderr": "Container not initialized",
                "exit_code": -1,
                "timed_out": True
            }
        
        try:
            code_bytes = code.encode('utf-8')
            tar_stream = io.BytesIO()
            with tarfile.open(fileobj=tar_stream, mode='w') as tar:
                tarinfo = tarfile.TarInfo(name='tmp.py')
                tarinfo.size = len(code_bytes)
                tar.addfile(tarinfo, io.BytesIO(code_bytes))
            tar_stream.seek(0)
            self.container.put_archive(path='/tmp', data=tar_stream.getvalue())
 

            cmd = f"python /tmp/tmp.py"
            
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._exec_command, cmd),
                timeout=timeout 
            )
            
            return result
        except asyncio.TimeoutError:
            # Handle timeout but don't destroy container
            print("Code execution timed out")
            return {
                "stdout": "",
                "stderr": "Execution timed out after specified duration",
                "exit_code": -1,
                "timed_out": True
            }
        except Exception as e:
            print(f"Exception occurred during code execution: {str(e)}")
            return {
                "stdout": "",
                "stderr": str(e),
                "exit_code": -1,
                "timed_out": False
            }
    
    def _exec_command(self, cmd: str) -> Dict[str, any]:
        """Synchronous method to execute command in container"""
        try:
            result = self.container.exec_run(cmd)
            return {
                "stdout": result.output.decode('utf-8'),
                "stderr": "",  # exec_run usually merges stderr into stdout
                "exit_code": result.exit_code,
                "timed_out": False
            }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": str(e),
                "exit_code": -1,
                "timed_out": False
            }
    
    def close(self):
        """Close and clean up container"""
        if self.container:
            try:
                print(f"Stopping and removing sandbox container {self.container_id[:12]}")
                self.container.stop()
                self.container.remove()
                print(f"Sandbox container cleaned up successfully")
            except Exception as e:
                print(f"Exception occurred during container cleanup: {str(e)}")
            finally:
                self.container = None
                self.container_id = None
