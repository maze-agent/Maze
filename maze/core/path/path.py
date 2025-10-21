import json
from typing import Any,Dict,List
import multiprocessing as mp
import zmq
import threading
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow
from fastapi import WebSocket
import asyncio
from maze.core.path.scheduler import scheduler_process
from maze.utils.utils import get_available_ports
import os

class MaPath:
    def __init__(self,strategy:str):
        assert strategy == "FCFS" #暂时只支持FCFS策略
        self.strategy=strategy

        self.workflows: Dict[str, Workflow] = {}
        self.finished_queues: Dict[Any, asyncio.Queue] = {} #workflow_id:queue     
         
    def cleanup(self):
        #通知调度进程关闭，（调度进程释放
        message = {"type":"shutdown"}
        serialized: bytes = json.dumps(message).encode('utf-8')
        self.socket_to_scheduler_receive.send(serialized)

        self.scheduler_process.join()
        os._exit(1)
        
     
    def create_workflow(self,workflow_id:str):
        self.workflows[workflow_id] = Workflow(workflow_id)

    def add_task(self,workflow_id:str,task_id:str,task_type:str):
        self.workflows[workflow_id].add_task(task_id,CodeTask(workflow_id,task_id))

    def save_task(self,workflow_id:str,task_id:str,task_input:str,task_output:str,code_str:str,resources:str):
        task = self.workflows[workflow_id].get_task(task_id)
        task.save_task(task_input=task_input, task_output=task_output, code_str = code_str, resources=resources)

    def add_edge(self,workflow_id:str,source_task_id:str,target_task_id:str):
        self.workflows[workflow_id].add_edge(source_task_id,target_task_id)

    def run_workflow(self,workflow_id:str):
        """
        运行工作流，将工作流起始任务加入调度器
        """
        workflow = self.workflows[workflow_id]
        self.finished_queues[workflow_id] = asyncio.Queue()
        start_task:List = workflow.get_start_task()
        for task in start_task:
            message = {
                "type":"run_task",
                "task":task.to_json()
            }
            serialized: bytes = json.dumps(message).encode('utf-8')
            self.socket_to_scheduler_receive.send(serialized)
            
    def _monitor_thread(self,port1,port2):
        assert self.context != None

        socket_to_scheduler_receive = self.context.socket(zmq.DEALER)
        socket_to_scheduler_receive.connect(f"tcp://127.0.0.1:{port1}")

        socket_from_scheduler_monitor = self.context.socket(zmq.ROUTER)
        socket_from_scheduler_monitor.bind(f"tcp://127.0.0.1:{port2}")

        while True:
            frames = socket_from_scheduler_monitor.recv_multipart()
            assert(len(frames)==2)
            identity, data = frames
            message = json.loads(data.decode('utf-8'))
          
            if(message["type"]=="finish_task"):
                '''
                message = {
                    "type":"finish_task",
                    "workflow_id":"workflow_id",
                    "task_id":"task_id",
                    "result":{
                        "key" : "value"
                    }
                }
                '''
                #添加任务结果到异步队列中，main线程通过队列获取任务结果并用SSE机制向客户端推送
                self.finished_queues[message["workflow_id"]].put_nowait(
                    item = {
                        "task_id":message["task_id"],
                        "workflow_id":message["workflow_id"],
                        "result":message["result"]
                    }
                )

                #标记任务完成，检测是否有新的入度为0的任务
                new_ready_tasks  = self.workflows[message["workflow_id"]].finish_task(task_id=message["task_id"])
                if len(new_ready_tasks) > 0:
                    for task in new_ready_tasks:
                        message = {
                            "type":"run_task",
                            "task":task.to_json()
                        }                 
                        serialized: bytes = json.dumps(message).encode('utf-8')
                        socket_to_scheduler_receive.send(serialized)

    async def get_workflow_res(self,workflow_id:str,websocket:WebSocket):    
        """
        持续获取工作流每个任务的运行结果
        """
        workflow = self.workflows[workflow_id]
        total_task_num = workflow.get_total_task_num()

        finished_queue = self.finished_queues.get(workflow_id)

        assert finished_queue != None

        count = 0
        while True:
            data = await finished_queue.get()
            await websocket.send_json(data)
            count += 1
            if(count == total_task_num):
                message = {"type":"clear_workflow"}
                serialized: bytes = json.dumps(message).encode('utf-8')
                self.socket_to_scheduler_receive.send(serialized)
                break
       
    def start(self):
        self.context = zmq.Context() #zmq context
        available_ports = get_available_ports(2)
      
        port1 = available_ports[0]
        port2 = available_ports[1]

        #创建与scheduler_process中的receive线程通信的socket
        self.socket_to_scheduler_receive = self.context.socket(zmq.DEALER)
        self.socket_to_scheduler_receive.connect(f"tcp://127.0.0.1:{port1}")
        
        #创建monitor线程
        self.monitor_thread = threading.Thread(target=self._monitor_thread, args=(port1,port2,))
        self.monitor_thread.start()

        #创建scheduler进程
        self.scheduler_process = mp.Process(target=scheduler_process, args=(port1,port2,self.strategy,))
        self.scheduler_process.start()
        
        

    
