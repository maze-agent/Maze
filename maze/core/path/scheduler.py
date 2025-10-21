from email import message
from math import pi
from re import S
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
from maze.core.path.resource_manager import ResourceManager
from maze.core.path.view import TasksView
from maze.core.path.runner import remote_task_runner

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
         
        
        self.tasks_view = TasksView() #任务视图（维护任务所有相关信息）
        
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
    
    def _run_task(self,choosed_node_info,task):
        if "gpu_id" in choosed_node_info:
            pass
        else:
            """
            task_input = {
                "input_params" : {
                    "1":{
                        "key": "task1_input", #输入参数的key，在代码中可通过params.get("input_key")获取输入参数的值
                        "input_schema":"from_user",  #输入模式
                        "data_type":"str",  #输入参数的类型     
                        "value" : "这是task1的输入",   #输入参数的value
                    },
                }
            }

            task_input = {
                "input_params" : {
                    "1":{
                    "key": "task2_input", #输入参数的key，在代码中可通过params.get("input_key")获取输入参数的值
                    "input_schema":"from_task",  #输入模式
                    "data_type":"str",  #输入参数的类型     
                    "value" : f"{task_id1}.output.task1_output",   #输入参数的value
                    },
                }
            }
            """
            task_input = {}
            for _,input_info in task['task_input']["input_params"].items():
                if input_info["input_schema"] == "from_user":
                    task_input[input_info["key"]] = input_info["value"]
                elif input_info["input_schema"] == "from_task":
                    task_input[input_info["key"]] = self.tasks_view.get_result(input_info["value"])
  
            result_ref = remote_task_runner.options(
                num_cpus=task["resources"]["cpu"],
                memory=task["resources"]["cpu_mem"],
                scheduling_strategy= ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(node_id=choosed_node_info["node_id"], soft=False)
            ).remote(code_str=task['code_str'],task_input=task_input)
            
            return result_ref

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
                    self.task_queue.put(message['task'])
                elif(message["type"]=="clear_workflow"):
                    pass
                elif(message["type"]=="join_worker"):
                    pass
                    #新节点加入
                    #todo：更新资源
                elif(message["type"]=="stop_worker"):
                    pass
                elif(message["type"]=="shutdown"):
                    self._cleanup()

        except Exception as e:
            print(f"[子进程] 发生错误: {e}")
            self._cleanup()

    def _monitor(self, port2:int):
        print("====_monitor======")
        socket_to_mapath_monitor = self.context.socket(zmq.DEALER)
        socket_to_mapath_monitor.connect(f"tcp://127.0.0.1:{port2}")

        while True:
            #获取一批running任务并等待完成（等待上限：1s）
            running_tasks_ref:List = self.tasks_view.get_running_tasks_ref()

            if len(running_tasks_ref) == 0:
                time.sleep(1)
                continue

            finished_tasks_ref, _ = ray.wait(running_tasks_ref, num_returns=len(running_tasks_ref), timeout=1.0)
            tasks_res = ray.get(finished_tasks_ref)
          
            #记录完成任务的结果
            self.tasks_view.set_task_finished(finished_tasks_ref,tasks_res)
            
            #释放资源
            finished_tasks = self.tasks_view.get_task_by_ref(finished_tasks_ref)
            self.resource_manager.release_resource(finished_tasks)

            #todo:zmq发送任务结果
            for task in finished_tasks:
                message = {
                    "type":"finish_task",
                    "workflow_id":task['detail']['workflow_id'],
                    "task_id":task['detail']['task_id'],
                    "result":task['result']
                }
                serialized_message = json.dumps(message).encode('utf-8')
                socket_to_mapath_monitor.send(serialized_message)

    def _submit_thread(self):
        print("====_submit_thread======")

        #FCFS
        self.cur_ready_task = None
        while True:
            if self.cur_ready_task is None:
                self.cur_ready_task = self.task_queue.get()
                self.tasks_view.add_new_task(self.cur_ready_task)
                 
            #1.获取可调度节点
            choosed_node = self.resource_manager.choose_node(self.cur_ready_task["resources"]) 
            if choosed_node:  
                #print(f"任务执行")
                #print(self.cur_ready_task)

                ref = self._run_task(choosed_node,self.cur_ready_task)
                self.tasks_view.set_task_running(self.cur_ready_task["task_id"],ref,choosed_node)

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
        self.submit_thread = threading.Thread(target=self._submit_thread) 
        self.submit_thread.start()


        self.receive_thread.join()
        self.monitor_thread.join()
        self.submit_thread.join()
            
    
   