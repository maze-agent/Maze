from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn
from typing import Optional, Dict, Any,List
import uuid
from fastapi import Request
from fastapi import FastAPI, Request
import uuid
import multiprocessing as mp
from typing import Any, Dict
from maze.core.path.path import MaPath
import signal

app = FastAPI()

mapath = MaPath(strategy="FCFS")
  
#异常退出时需要做相关清理
def signal_handler(signum, frame):
    mapath.cleanup()
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

'''
描述：前端设置一个“创建工作流”按钮，点击后发送该请求，前端创建一个单独的工作流页面

请求路径：/create_workflow
请求参数：无
响应参数：执行状态，工作流ID
'''
@app.post("/create_workflow")
async def create_workflow(req:Request):
    workflow_id = str(uuid.uuid4())
    mapath.create_workflow(workflow_id)
 
    return {"status": "success","workflow_id": workflow_id}

'''
描述：在每个工作流页面有一个“创建任务”按钮，点击后发送该请求，创建一个任务（目前只支持code类型任务）

请求参数：工作流ID，任务类型，任务代码
响应参数：执行状态，任务ID
'''
@app.post("/add_task")
async def add_task(req:Request):
    data = await req.json()
    workflow_id:str = data.get("workflow_id")
    task_type:str = data.get("task_type")    
    task_id: str = str(uuid.uuid4())
    
    if(task_type == "code"):
        mapath.add_task(workflow_id, task_id, task_type)
    else:
        return {"status": "fail", "message": "Invalid task_type"}

    return {"status":"success","task_id": task_id}

'''
描述：在每个任务方框中有一个“删除任务”按钮，按下后发送该请求，删除该任务

请求参数：工作流ID，任务ID
响应参数：删除是否成功
'''
@app.post("/del_task")
async def del_task(req:Request):
    data = await req.json()
    workflow_id:str = data.get("workflow_id")
    task_id: str = data.get("task_id")
    mapath.del_task(workflow_id, task_id)
    return {"status":"success","task_id": task_id}



'''
描述：在每个任务方框中有一个“保存”按钮，按下后发送该请求，保存任务详细信息（输入参数，输出参数，所需资源，任务code_str）

请求参数：工作流ID，任务ID，任务输入参数，任务输出参数，所需资源
响应参数：保存是否成功
'''
@app.post("/save_task")
async def save_task(req:Request):
    data = await req.json()

    workflow_id = data.get("workflow_id")
    task_id = data.get("task_id")
    task_input = data.get("task_input")
    task_output = data.get("task_output")
    resources = data.get("resources")
    code_str = data.get("code_str")
    
    mapath.save_task(workflow_id=workflow_id, 
                     task_id=task_id,
                     task_input=task_input, 
                     task_output=task_output, 
                     code_str = code_str, 
                     resources=resources
                    )

    return {"status":"success"}

'''
描述：在前端通过连线构建任务依赖关系(添加边)

请求参数：工作流ID，任务ID，源任务ID，目标任务ID
响应参数：是否添加成功
'''
@app.post("/add_edge")
async def add_edge(req:Request):
    data = await req.json()
   
    workflow_id = data.get("workflow_id")
    source_task_id = data.get("source_task_id")
    target_task_id = data.get("target_task_id")
    mapath.add_edge(workflow_id, source_task_id, target_task_id)
   
    return {"status":"success"}


'''
描述：在前端点击 “运行工作流” 按钮发送该请求 (注意该请求通过SSE机制实时向前端发送运行结果)

请求参数：工作流ID
响应参数：是否添加成功
'''
@app.post("/run_workflow")
async def run_workflow(req:Request):
    data = await req.json()
    workflow_id = data.get("workflow_id")
    mapath.run_workflow(workflow_id)
    return {"status":"success"}


'''
描述：前端接收到/run_workflow请求后，立即发送该请求，实时获取工作流运行结果

请求参数：工作流ID
响应参数：通过websocket机制实时返回各个任务的运行结果
'''
@app.websocket("/get_workflow_res/{workflow_id}")
async def get_workflow_res(websocket: WebSocket, workflow_id: str):
    try:
        await websocket.accept()
        await mapath.get_workflow_res(workflow_id,websocket)
        await websocket.close()
    except WebSocketDisconnect:
        print(f"{workflow_id} websocket disconnect normally")
    except Exception as e:
        print(f"{workflow_id} websocket disconnect : {e}")
    finally:
        mapath.clear_workflow_runtime(workflow_id)
       

if __name__ == "__main__":
    mapath.start()
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
    