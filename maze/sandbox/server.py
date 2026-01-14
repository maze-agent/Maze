import ray
from typing import Dict, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from maze.sandbox.code_sandbox import CodeSandbox

app = FastAPI(title="Code Sandbox Server", description="Remote code execution sandbox service")

class CodeExecutionRequest(BaseModel):
    code: str
    timeout: float = 10.0
    cpu_nums: int = 1
    gpu_nums: int = 0
    memory_mb: int = 512

class SandboxSession:
    def __init__(self, session_id: str, cpu_nums: int, gpu_nums: int, memory_mb: int):
        self.session_id = session_id
        self.cpu_nums = cpu_nums
        self.gpu_nums = gpu_nums
        self.memory_mb = memory_mb
        self.sandbox = CodeSandbox(cpu_nums=cpu_nums, gpu_nums=gpu_nums, memory_mb=memory_mb)

active_sessions: Dict[str, SandboxSession] = {}


@app.post("/create_session")
async def create_session(request: CodeExecutionRequest):
    session_id = f"sandbox_{len(active_sessions) + 1}"
    
    try:
        session = SandboxSession(
            session_id=session_id,
            cpu_nums=request.cpu_nums,
            gpu_nums=request.gpu_nums,
            memory_mb=request.memory_mb
        )
        active_sessions[session_id] = session
        
        return {
            "session_id": session_id,
            "message": "Sandbox session created successfully",
            "cpu_nums": request.cpu_nums,
            "gpu_nums": request.gpu_nums,
            "memory_mb": request.memory_mb
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create sandbox session: {str(e)}")

@app.post("/execute/{session_id}")
async def execute_code(session_id: str, request: CodeExecutionRequest):
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    
    session = active_sessions[session_id]
    
    try:
        result = await session.sandbox.run_code(request.code, timeout=request.timeout)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Code execution failed: {str(e)}")

@app.delete("/close/{session_id}")
async def close_session(session_id: str):
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    
    session = active_sessions[session_id]
    try:
        session.sandbox.close()
        del active_sessions[session_id]
        return {"message": f"Session {session_id} closed successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to close session: {str(e)}")


@app.on_event("startup")
async def startup_event():
    ray.init()

@app.on_event("shutdown")
async def shutdown_event():
    for session_id, session in list(active_sessions.items()):
        try:
            session.sandbox.close()
        except Exception as e:
            print(f"Error closing session {session_id}: {str(e)}")
        finally:
            del active_sessions[session_id]