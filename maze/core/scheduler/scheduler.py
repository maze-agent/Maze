from email import message
from math import pi
from re import S
import resource
from typing import Any,List
from unittest import result

import ray
import time
import zmq
import threading
import queue
import json
import subprocess

import os
import sys
from maze.core.scheduler.resource import ResourceManager
from maze.core.scheduler.runtime import RuntimeManager,TaskRuntime
from maze.core.scheduler.runner import remote_task_runner

def scheduler_process(port1:int,port2:int,strategy:str):
    if strategy == "FCFS":
        scheduler = FCFSScheduler(port1,port2)
    else:
        raise NotImplementedError

    scheduler.start()
 
class Scheduler():
    def _launch_ray_head(self):
        try:
            command = [
                "ray", "start", "--head"
            ]
            result = subprocess.run(
                command,
                check=True,                    # 如果命令失败（返回码非0），抛出异常
                text=True,                     # 以字符串形式处理输出
                capture_output=True,           # 捕获 stdout 和 stderr
            )
            # print("Ray 头节点启动成功！")
            # print("标准输出:\n", result.stdout)
            # print("标准错误:\n", result.stderr)
            
            if result.returncode != 0:
                raise RuntimeError(f"Failed to start Ray: {result.stderr}")

        except Exception as e:
            print(f"发生异常：{e}")
   
class FCFSScheduler(Scheduler):
    """
    First come first serve scheduler.
    """
    def __init__(self, port1:int, port2:int):
        self.port1 = port1
        self.port2 = port2
        self.task_queue = queue.Queue() #任务队列（receive线程和submit线程之间交互）
        self.workflow_manager = RuntimeManager() # 工作流管理器
        
    def _cleanup(self):
        command = [
            "ray", "stop", 
        ]
        result = subprocess.run(
            command,
            check=True,                    # 如果命令失败（返回码非0），抛出异常
            text=True,                     # 以字符串形式处理输出
            capture_output=True,           # 捕获 stdout 和 stderr
        )
        # print("Ray 关闭成功！")
        # print("标准输出:\n", result.stdout)
        # print("标准错误:\n", result.stderr)
        os._exit(1)
    
    def _receive_thread(self,port1:int):
        print("====_receive_thread======")
        
        assert(self.context is not None)
        socket_from_mapath_main_monitor = self.context.socket(zmq.ROUTER)
        socket_from_mapath_main_monitor.bind(f"tcp://127.0.0.1:{port1}")

        try:
            while True:
                frames = socket_from_mapath_main_monitor.recv_multipart()
                assert(len(frames)==2)
                identity, data = frames
                message = json.loads(data.decode('utf-8'))
                
                if(message["type"]=="run_task"):
                    task = message['task']
                    task_runtime = TaskRuntime(workflow_id=task['workflow_id'],
                                                            task_id=task['task_id'],
                                                            task_input=task['task_input'],
                                                            task_output=task['task_output'],
                                                            resources=task['resources'],
                                                            code_str=task['code_str']
                                                            )                
                    self.task_queue.put(task_runtime)
                elif(message["type"]=="clear_workflow"):
                    self.workflow_manager.clear_workflow(workflow_id=message['workflow_id'])
                elif(message["type"]=="join_worker"):
                    pass
                    #新节点加入
                    #todo：更新资源
                elif(message["type"]=="stop_worker"):
                    pass
                elif(message["type"]=="shutdown"):
                    self._cleanup()

        except Exception as e:
            print(f"_receive_thread error: {e}")
            self._cleanup()

    def _monitor(self, port2:int):
        print("====_monitor======")
        socket_to_main_monitor = self.context.socket(zmq.DEALER)
        socket_to_main_monitor.connect(f"tcp://127.0.0.1:{port2}")

        while True:
            #1.清除待删除的工作流
            self.workflow_manager.clear_del_workflows()


            #2.获取一批running任务并等待完成（等待上限：1s）
            running_tasks:List = self.workflow_manager.get_running_tasks()
            if len(running_tasks) == 0:
                time.sleep(1)
                continue

            running_tasks_ref = [task.object_ref for task in running_tasks]
            finished_tasks_ref, _ = ray.wait(running_tasks_ref, num_returns=len(running_tasks_ref), timeout=1.0)
            if len(finished_tasks_ref) == 0:
                continue
            tasks_res = ray.get(finished_tasks_ref)
          
            #3.记录完成任务的结果
            self.workflow_manager.set_task_result(finished_tasks_ref,tasks_res)
            
            #4.释放资源
            finished_tasks = self.workflow_manager.get_tasks_by_refs(finished_tasks_ref)
            assert(len(finished_tasks)==len(finished_tasks_ref))
            self.resource_manager.release_resource(finished_tasks)

            #5.zmq发送任务结果
            for task in finished_tasks:
                message = {
                    "type":"finish_task",
                    "workflow_id":task.workflow_id,
                    "task_id":task.task_id,
                    "result":task.result
                }
                serialized_message = json.dumps(message).encode('utf-8')
                socket_to_main_monitor.send(serialized_message)

    def _submit_thread(self,port2:int):
        print("====_submit_thread======")
        socket_to_main_monitor = self.context.socket(zmq.DEALER)
        socket_to_main_monitor.connect(f"tcp://127.0.0.1:{port2}")

        #FCFS
        self.cur_ready_task: None|TaskRuntime = None
        while True:
            if self.cur_ready_task is None:
                self.cur_ready_task =  self.task_queue.get()
                self.workflow_manager.add_task(self.cur_ready_task)
            assert(self.cur_ready_task is not None)   

            #1.获取调度节点
            choosed_node_info = self.resource_manager.choose_node(task_need_resources=self.cur_ready_task.resources) #choosed_node = {"node_id":"node_id","gpu_id":"gpu_id"}
            if choosed_node_info:  
                #print(f"任务执行")
                #print(self.cur_ready_task)

                #2.运行任务
                self.workflow_manager.run_task(self.cur_ready_task,choosed_node_info)

                #3.推送更新任务状态
                message = {
                    "type":"start_task",
                    "workflow_id":self.cur_ready_task.workflow_id,
                    "task_id":self.cur_ready_task.task_id,
                    "node_ip":choosed_node_info.get("node_ip"),
                    "node_id":choosed_node_info.get("node_id"),
                    "gpu_id":choosed_node_info.get("gpu_id"),
                }
                serialized_message = json.dumps(message).encode('utf-8')
                socket_to_main_monitor.send(serialized_message)

                self.cur_ready_task = None
            else: #当前无满足所需资源的机器
                time.sleep(1)

    def start(self): 
        self.context = zmq.Context() #zmq context

        #启动ray head和资源管理器
        self._launch_ray_head()
        self.resource_manager = ResourceManager()
        
        #创建receive线程，用于接收主进程消息
        self.receive_thread = threading.Thread(target=self._receive_thread,args=(self.port1,)) 
        self.receive_thread.start()

        #创建monitor线程，用于监控任务完成情况
        self.monitor_thread = threading.Thread(target=self._monitor,args=(self.port2,)) 
        self.monitor_thread.start()
        
        #创建submit线程，用于提交任务
        self.submit_thread = threading.Thread(target=self._submit_thread,args=(self.port2,)) 
        self.submit_thread.start()


        self.receive_thread.join()
        self.monitor_thread.join()
        self.submit_thread.join()
            
    
   