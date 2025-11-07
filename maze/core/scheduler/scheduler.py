from logging import Logger


import logging
import ray
import time
import zmq
import threading
import queue
import json
import os
import base64
import cloudpickle
import binascii
import subprocess
import multiprocessing as mp
from queue import Queue
from maze.core.scheduler.resource import SelectedNode
from typing import Any,List,Dict
from maze.core.scheduler.resource import ResourceManager
from maze.core.scheduler.runtime import WorkflowRuntimeManager,TaskRuntime,LanggraphTaskRuntime
from maze.core.workflow.task import TaskType

logger = logging.getLogger(__name__)


def scheduler_process(port1:int,port2:int,strategy:str,ray_head_port:int,ready_queue:mp.Queue):
    if strategy == "Default":
        scheduler = Scheduler(port1,port2,ray_head_port,ready_queue)
    else:
        raise NotImplementedError

    scheduler.start()
 
class Scheduler():
    def __init__(self, port1:int, port2:int, ray_head_port:int, ready_queue:mp.Queue):
        self.lock = threading.Lock()
        self.port1 = port1
        self.port2 = port2
        self.ray_head_port = ray_head_port
        self.ready_queue = ready_queue
 
        self.workflow_manager = WorkflowRuntimeManager()
        self.resource_manager = ResourceManager()

        self.task_queue: Queue[TaskRuntime|LanggraphTaskRuntime] = queue.Queue()
      
    def _cleanup(self):
        command = [
            "ray", "stop", 
        ]
        result = subprocess.run(
            command,
            check=True,                   
            text=True,                    
            capture_output=True,      
        )
      
        os._exit(1)
    
    def _receive_thread(self,port1:int):
        logger.info(f"Receive start")
        assert(self.context is not None)
        socket_from_main = self.context.socket(zmq.ROUTER)
        socket_from_main.bind(f"tcp://127.0.0.1:{port1}")

        try:
            while True:
                frames = socket_from_main.recv_multipart()
                assert(len(frames)==2)
                _, data = frames
                message = json.loads(data.decode('utf-8'))
              
                message_type = message["type"]
                message_data = message["data"]
                if(message_type =="run_task"):
                    if(message_data["task_type"]==TaskType.CODE.value):
                        task_runtime = TaskRuntime(workflow_id=message_data['workflow_id'],
                                                                task_id=message_data['task_id'],
                                                                task_input=message_data['task_input'],
                                                                task_output=message_data['task_output'],
                                                                resources=message_data['resources'],
                                                                code_str=message_data.get('code_str'),
                                                                code_ser=message_data.get('code_ser')
                                                                )  
                        self.task_queue.put(item=task_runtime)
                    elif(message_data["task_type"]==TaskType.LANGGRAPH.value):
                        task_runtime = LanggraphTaskRuntime(workflow_id=message_data['workflow_id'],
                                                                                  task_id=message_data['task_id'],
                                                                                  code_ser=message_data['code_ser'],
                                                                                  args=message_data['args'],
                                                                                  kwargs=message_data['kwargs'],
                                                                                  resources=message_data['resources']
                                                                                )  
                        self.task_queue.put(item=task_runtime)
                elif(message_type =="clear_workflow" ):
                    with self.lock:
                        self.workflow_manager.clear_workflow(workflow_id=message_data["workflow_id"])
                elif(message_type =="stop_workflow" ):
                    with self.lock:
                        cancled_tasks = self.workflow_manager.cancel_workflow(workflow_id=message_data["workflow_id"])
                        if cancled_tasks:
                            self.resource_manager.release_resource(tasks=cancled_tasks)
                            self.workflow_manager.clear_workflow(workflow_id=message_data["workflow_id"]) 
                elif(message_type=="start_worker"):
                    with self.lock:
                        self.resource_manager.start_worker(node_id=message_data["node_id"], resources=message_data["resources"], node_ip=message_data["node_ip"])
                elif(message_type=="stop_worker"):
                    with self.lock:
                        self.resource_manager.stop_worker(node_id=message_data["node_id"])
                elif(message_type=="shutdown"):
                    self._cleanup()
                 
        except Exception as e:
            print(f"_receive_thread error: {e}")
            self._cleanup()
     
    def _submit_thread(self,port2:int):
        logger.info(f"Submit start")
        socket_to_main = self.context.socket(zmq.DEALER)
        socket_to_main.connect(f"tcp://127.0.0.1:{port2}")
         
        while True:
            self.cur_ready_task =  self.task_queue.get()
            self.lock.acquire()
            self.workflow_manager.add_task(self.cur_ready_task)
           
            #Get the node can run the task
            selected_node: SelectedNode | None = self.resource_manager.select_node(task_need_resources=self.cur_ready_task.resources)
            if selected_node:
                #Run task
                self.workflow_manager.run_task(task=self.cur_ready_task,node=selected_node)

                #Send message to main
                message = {
                    "type":"start_task",
                    "data":{
                        "workflow_id":self.cur_ready_task.workflow_id,
                        "task_id":self.cur_ready_task.task_id,
                        "node_ip":selected_node.node_ip,
                        "node_id":selected_node.node_id,
                        "gpu_id":selected_node.gpu_id,
                    }
                }
                serialized_message = json.dumps(message).encode('utf-8')
                socket_to_main.send(serialized_message)
            
                self.cur_ready_task = None
                self.lock.release()
            else:
                self.lock.release()
                self.task_queue.put(self.cur_ready_task)
                time.sleep(1)

    def _supervisor_thread(self, port2:int):
        logger.info(f"Supervisor start")
        socket_to_main = self.context.socket(zmq.DEALER)
        socket_to_main.connect(f"tcp://127.0.0.1:{port2}")

        while True:
            with self.lock:
                self.resource_manager.check_dead_node()

                self.resource_manager.show_all_node_resource()
               

                running_task_refs:List = self.workflow_manager.get_running_task_refs()
                if len(running_task_refs) == 0:
                    continue
                
                
                finished_task_refs, _ = ray.wait(running_task_refs, num_returns=len(running_task_refs),timeout=0)
                if len(finished_task_refs) == 0:
                    continue
                        
                for finished_task_ref in finished_task_refs:
                    finished_task = self.workflow_manager.get_task_by_ref(finished_task_ref)
                    if finished_task is None:
                        continue # The workflow of task is deleted
                    try:
                        result = ray.get(finished_task_ref)

                        self.workflow_manager.set_task_result(finished_task,result) 
                        self.resource_manager.release_resource(tasks=[finished_task])

                        #Send message to main
                        message = {
                            "type":"finish_task",
                            "data":{
                                "workflow_id":finished_task.workflow_id,
                                "task_id":finished_task.task_id,
                                "result":finished_task.result,
                            },
                        }
                        serialized_message = json.dumps(message).encode('utf-8')
                        socket_to_main.send(serialized_message)
 
                    except ray.exceptions.RayTaskError as e:
                        #Internal exception in the code,stop the workflow
                        canceld_tasks = self.workflow_manager.cancel_workflow(finished_task.workflow_id)
                        if len(canceld_tasks) > 0:
                            self.resource_manager.release_resource(tasks=canceld_tasks)
                            self.workflow_manager.clear_workflow(finished_task.workflow_id)

                        #Send message to main
                        message = {
                            "type":"task_exception",
                            "data":{
                                "workflow_id":finished_task.workflow_id,
                                "task_id":finished_task.task_id,
                                "result":f"ray.exceptions.RayTaskError:{str(e)}"
                            }
                            
                        }
                        serialized_message = json.dumps(message).encode('utf-8')
                        socket_to_main.send(serialized_message)
                    except ray.exceptions.TaskCancelledError as e:
                        pass
                    except (ray.exceptions.NodeDiedError, ray.exceptions.ObjectLostError, ray.exceptions.TaskUnschedulableError) as e:
                        #The node of task running is dead,send the task back to the queue to retry.
                        self.task_queue.put(finished_task)
                    except Exception as e:
                        print(f"Exception occurred {type(e)}: {e}")
                 
    def _launch_ray_head(self):
        try:
            command = [
                "ray", "start", "--head","--port",str(self.ray_head_port),
            ]
            result = subprocess.run(
                command,
                check=True,                   
                text=True,
                capture_output=True,
            )
           
            
            if result.returncode != 0:
                raise RuntimeError(f"Failed to start Ray: {result.stderr}")

        except Exception as e:
            print(f"Exception occurred: {e}")

    def start(self): 
        self.context = zmq.Context() #zmq context

        self._launch_ray_head()
        self.resource_manager.init()
        
        self.receive_thread = threading.Thread(target=self._receive_thread,args=(self.port1,)) 
        self.receive_thread.start()

        self.monitor_thread = threading.Thread(target=self._supervisor_thread,args=(self.port2,)) 
        self.monitor_thread.start()
        
        self.submit_thread = threading.Thread(target=self._submit_thread,args=(self.port2,)) 
        self.submit_thread.start()

        self.ready_queue.put("ready")
        self.receive_thread.join()
        self.monitor_thread.join()
        self.submit_thread.join()
            
    
   