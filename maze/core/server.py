import uuid
import signal
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any,List
from maze.core.path.path import MaPath
from fastapi import FastAPI, WebSocket, Request

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   
    allow_credentials=True,
    allow_methods=["*"],  
    allow_headers=["*"],    
)

mapath = MaPath(strategy="FCFS")
   
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
    task_name: str = data.get("task_name")
    
    # 添加参数验证
    if not workflow_id:
        return {"status": "fail", "message": "workflow_id is required"}
    if not task_type:
        return {"status": "fail", "message": "task_type is required"}
    
    if(task_type == "code"):
        mapath.add_task(workflow_id, task_id, task_type,task_name)
    else:
        return {"status": "fail", "message": "Invalid task_type"}

    return {"status":"success","task_id": task_id}

'''
描述：获取工作流中的所有任务
请求路径：/get_workflow_tasks/{workflow_id}
请求参数：工作流ID（路径参数）
响应参数：任务列表，包含id和name
'''
@app.get("/get_workflow_tasks/{workflow_id}")
async def get_workflow_tasks(workflow_id: str):
    try:
        # 调用mapath获取工作流任务
        tasks = mapath.get_workflow_tasks(workflow_id)
        return {"status": "success", "tasks": tasks}
    except Exception as e:
        return {"status": "fail", "message": str(e)}

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
    
    # 添加参数验证
    if not workflow_id:
        return {"status": "fail", "message": "workflow_id is required"}
    if not task_id:
        return {"status": "fail", "message": "task_id is required"}
    
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
    
    # 添加参数验证
    if not workflow_id:
        return {"status": "fail", "message": "workflow_id is required"}
    if not task_id:
        return {"status": "fail", "message": "task_id is required"}
    
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
    
    # 添加参数验证
    if not workflow_id:
        return {"status": "fail", "message": "workflow_id is required"}
    if not source_task_id:
        return {"status": "fail", "message": "source_task_id is required"}
    if not target_task_id:
        return {"status": "fail", "message": "target_task_id is required"}
    
    mapath.add_edge(workflow_id, source_task_id, target_task_id)
   
    return {"status":"success"}

'''
描述：删除边（前端断开任务间连线时发送）

请求参数：工作流ID，源务ID，源任务ID
响应参数：是否删除成功
'''
@app.post("/del_edge")
async def del_edge(req:Request):
    data = await req.json()
   
    workflow_id = data.get("workflow_id")
    source_task_id = data.get("source_task_id")
    target_task_id = data.get("target_task_id")
    
    
    if not workflow_id:
        return {"status": "fail", "message": "workflow_id is required"}
    if not source_task_id:
        return {"status": "fail", "message": "source_task_id is required"}
    if not target_task_id:
        return {"status": "fail", "message": "target_task_id is required"}
    
    mapath.del_edge(workflow_id, source_task_id, target_task_id)
   
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
    
    # 添加参数验证
    if not workflow_id:
        return {"status": "fail", "message": "workflow_id is required"}
    
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
    except Exception as e:
        await mapath.stop_workflow(workflow_id)
        await websocket.close()


################以下请求不是给前端页面用的
'''
描述：获取head节点的ray端口，worker连接使用
'''
@app.post("/get_head_ray_port")
async def get_head_ray_port():
    return await mapath.get_ray_head_port()
      
# '''
# 描述：worker连接注册到head
# '''
# @app.post("/conect_to_head")
# async def conect_to_head():
#     return await mapath.conect_to_head()

 