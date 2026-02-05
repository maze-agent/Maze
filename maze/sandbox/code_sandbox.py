import ray
from typing import Dict, Optional
from maze.sandbox.launcher import SandboxActor
 
class CodeSandbox:
    def __init__(self, cpu_nums: int = 1, gpu_nums: int = 0, memory_mb: int = 512):
        """
        Initialize code sandbox
        
        Args:
            cpu_nums: Number of CPUs
            gpu_nums: Number of GPUs
            memory_mb: Memory size in MB
        """
        # Initialize Ray
        if not ray.is_initialized():
            ray.init()

        self.cpu_nums = cpu_nums
        self.gpu_nums = gpu_nums
        self.memory_mb = memory_mb
        self.actor = None
        
        memory_bytes = memory_mb * 1024
        
        # Create Ray Actor
        self.actor = SandboxActor.options(
            num_cpus=cpu_nums,
            num_gpus=gpu_nums,
            memory=memory_bytes
        ).remote(cpu_nums, gpu_nums, memory_mb)
        
    async def run_code(self, code: str, timeout: float = 10.0) -> Dict[str, any]:
        """
        Run code in sandbox
        
        Args:
            code: Python code string to execute
            timeout: Execution timeout in seconds (default 10 seconds)
            
        Returns:
            Dictionary containing stdout, stderr, exit_code, timed_out
        """
        if not self.actor:
            raise RuntimeError("Sandbox actor not initialized")
        
        return await self.actor.run_code.remote(code, timeout)
    
    def close(self):
        """Close sandbox and clean up resources"""
        if self.actor:
            # Call actor's close method to clean up container
            ray.get(self.actor.close.remote())
            
            # Delete actor reference
            del self.actor
            self.actor = None
            print("Code sandbox closed")

