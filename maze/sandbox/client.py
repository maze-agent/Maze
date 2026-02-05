import aiohttp
from typing import Dict, Any, Optional

class CodeSandboxClient:
    def __init__(self, url: str, cpu_nums: int = 1, gpu_nums: int = 0, memory_mb: int = 512):
        """
        Initialize remote code sandbox client
        
        Args:
            url: Server URL (e.g., "http://localhost:8000")
            cpu_nums: Number of CPUs
            gpu_nums: Number of GPUs
            memory_mb: Memory size in MB
        """
        self.url = url.rstrip('/')
        self.cpu_nums = cpu_nums
        self.gpu_nums = gpu_nums
        self.memory_mb = memory_mb
        self.session_id: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _ensure_session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
    
    async def _create_remote_session(self):
        await self._ensure_session()
        
        payload = {
            "code": "",  
            "cpu_nums": self.cpu_nums,
            "gpu_nums": self.gpu_nums,
            "memory_mb": self.memory_mb
        }
        
        if self._session is None:
            raise RuntimeError("Client session not initialized")
        
        try:
            async with self._session.post(f"{self.url}/create_session", json=payload) as response:
                if response.status != 200:
                    text = await response.text()
                    raise Exception(f"Failed to create session: {response.status}, {text}")
                
                result = await response.json()
                self.session_id = result["session_id"]
                return result
        except Exception as e:
            raise Exception(f"Error connecting to sandbox server: {str(e)}")
    
    async def run_code(self, code: str, timeout: float = 120.0) -> Dict[str, Any]:
        if not self.session_id:
            await self._create_remote_session()
        
        if self._session is None:
            raise RuntimeError("Client session not initialized")
        
        payload = {
            "code": code,
            "timeout": timeout
        }
        
        try:
            async with self._session.post(
                f"{self.url}/execute/{self.session_id}", 
                json=payload
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    raise Exception(f"Code execution failed: {response.status}, {text}")
                
                result = await response.json()
                return result
        except Exception as e:
            raise Exception(f"Error executing code remotely: {str(e)}")
    
    async def close(self):
        if self.session_id and self._session:
            if self._session is not None: 
                try:
                    async with self._session.delete(f"{self.url}/close/{self.session_id}") as response:
                        if response.status != 200:
                            text = await response.text()
                            print(f"Warning: Failed to close session: {response.status}, {text}")
                        else:
                            print("Remote sandbox session closed successfully")
                except Exception as e:
                    print(f"Warning: Error closing remote session: {str(e)}")
        
        if self._session:
            await self._session.close()
            self._session = None
        
        self.session_id = None