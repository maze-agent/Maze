import os
import json
import zmq.asyncio
import asyncio
import multiprocessing as mp
from fastapi import WebSocket
from typing import Any,Dict,List
from asyncio.queues import Queue
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow
from maze.core.scheduler.scheduler import scheduler_process
from maze.utils.utils import get_available_ports

class MaPath:
    def __init__(self,strategy:str):
        assert strategy == "FCFS" #暂时只支持FCFS策略
        self.strategy=strategy

        self.lock = lock = asyncio.Lock()

        self.workflows: Dict[str, Workflow] = {}
        self.async_que: Dict[str, asyncio.Queue] = {} #workflow_id:queue     
         
    def cleanup(self):
        '''
        Clean up the main process and scheduler process.
        '''
        message = {"type":"shutdown"}
        serialized: bytes = json.dumps(message).encode('utf-8')
        self.socket_to_receive.send(serialized)

        self.scheduler_process.join()
        os._exit(1)
        
    def create_workflow(self,workflow_id:str):
        '''
        Create a workflow.
        '''
        self.workflows[workflow_id] = Workflow(workflow_id)
            
    def add_task(self,workflow_id:str,task_id:str,task_type:str,task_name:str):
        self.workflows[workflow_id].add_task(task_id,CodeTask(workflow_id,task_id,task_name))

    def del_task(self,workflow_id:str,task_id:str):
        '''
        Delete a task from the workflow.
        '''
        self.workflows[workflow_id].del_task(task_id)

    def save_task(self,workflow_id:str,task_id:str,task_input:str,task_output:str,code_str:str,resources:str):
        '''
        Save a task.

        '''
        task = self.workflows[workflow_id].get_task(task_id)
        task.save_task(task_input=task_input, task_output=task_output, code_str = code_str, resources=resources)

       
    def add_edge(self,workflow_id:str,source_task_id:str,target_task_id:str):
        '''
        Add an edge to the workflow.
        '''
        self.workflows[workflow_id].add_edge(source_task_id,target_task_id)

    def del_edge(self,workflow_id:str,source_task_id:str,target_task_id:str):
        '''
        Delete an edge from the workflow.

        '''
        self.workflows[workflow_id].del_edge(source_task_id,target_task_id)

    def get_workflow_tasks(self,workflow_id:str):
        """
        获取工作流中的所有任务，返回任务列表（包含id和name）
        """
        if workflow_id not in self.workflows:
            return []
        
        workflow = self.workflows[workflow_id]
        tasks = []
        
        # 遍历工作流中的所有任务
        for task_id, task in workflow.tasks.items():
            tasks.append({
                "id": task_id,
                "name": task.task_name if hasattr(task, 'task_name') else f"任务_{task_id[:8]}"
            })
        
        return tasks

    def run_workflow(self,workflow_id:str):
        """
        Start a workflow.
        """
        workflow = self.workflows[workflow_id]
        self.async_que[workflow_id] = asyncio.Queue()
        start_task:List = workflow.get_start_task()
        for task in start_task:
            message = {
                "type":"run_task",
                "task":task.to_json()
            }
            serialized: bytes = json.dumps(message).encode('utf-8')
            self.socket_to_receive.send(serialized)
  
    def get_ray_head_port(self):
        '''
        Get the ray head port.

        '''
        return self.ray_head_port
    
    def init(self,ray_head_port):
        '''
        Initialize.
        '''
        self.ray_head_port = ray_head_port
        self.context = zmq.asyncio.Context()
        available_ports = get_available_ports(2)
      
        port1 = available_ports[0]
        port2 = available_ports[1]

         
        self.socket_to_receive = self.context.socket(zmq.DEALER)
        self.socket_to_receive.connect(f"tcp://127.0.0.1:{port1}")
        
        self.socket_from_submit_supervisor = self.context.socket(zmq.ROUTER)
        self.socket_from_submit_supervisor.bind(f"tcp://127.0.0.1:{port2}")

        
        #Create the scheduler process and wait for it to be ready
        self.ready_queue = mp.Queue()
        self.scheduler_process = mp.Process(target=scheduler_process, args=(port1,port2,self.strategy,self.ray_head_port,self.ready_queue))
        self.scheduler_process.start()
        message = self.ready_queue.get()
        if message == 'ready':
            pass
        else:
            raise Exception('scheduler process error')
 
    async def monitor_coroutine(self):
        '''
        Monitor the task from the scheduler process.
        '''
        while True:
            try:
                frames = await self.socket_from_submit_supervisor.recv_multipart()
                assert(len(frames)==2)
                identity, data = frames
                message = json.loads(data.decode('utf-8'))
               
                async with self.lock:
                    if message['workflow_id'] not in self.async_que:
                        continue

                    que: Queue[Any] = self.async_que[message['workflow_id']]
                    await que.put(message)

                    if(message["type"]=="finish_task"):
                        new_ready_tasks  = self.workflows[message["workflow_id"]].finish_task(task_id=message["task_id"])
                        if len(new_ready_tasks) > 0:
                            for task in new_ready_tasks:
                                message = {
                                    "type":"run_task",
                                    "task":task.to_json()
                                }                 
                                serialized: bytes = json.dumps(message).encode('utf-8')
                                self.socket_to_receive.send(serialized)
            except Exception as e:
                print(f"Error in monitor: {e}")
                await asyncio.sleep(1)

    async def get_workflow_res(self,workflow_id:str,websocket:WebSocket):    
        """
        Get the workflow result and send to websocket.
        """
        workflow = self.workflows[workflow_id]
        total_task_num = workflow.get_total_task_num()

        que = self.async_que[workflow_id]
        assert que != None

        count = 0
        while True:
            data = await que.get()
            await websocket.send_json(data)

            if data["type"]=="finish_task":
                count += 1
                if(count == total_task_num):
                    message = {"type":"finish_workflow","workflow_id":workflow_id}
                    serialized: bytes = json.dumps(message).encode('utf-8')
                    self.socket_to_receive.send(serialized)
                     
                    break
            elif data["type"]=="task_exception":
                raise Exception(data["message"])
          
    async def stop_workflow(self,workflow_id:str):
        '''
        Stop workflow
        '''
        async with self.lock:
            del self.async_que[workflow_id]

        message = {"type":"stop_workflow","workflow_id":workflow_id}
        serialized: bytes = json.dumps(message).encode('utf-8')
        self.socket_to_receive.send(serialized)
    
    

   
