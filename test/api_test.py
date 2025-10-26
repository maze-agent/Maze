

from typing import Any
import requests
import websocket
from websocket import WebSocketApp




def create_workflow():
    # 定义请求的 URL
    url = "http://localhost:8000/create_workflow" 

    try:
        # 发送 POST 请求
        response = requests.post(url)

        # 检查响应状态码
        if response.status_code == 200:
            data = response.json()
            return data["workflow_id"]
        else:
            print(f"请求失败，状态码：{response.status_code}")
            print("响应内容：", response.text)

    except requests.exceptions.RequestException as e:
        print("请求发生错误：", e)

def add_task(workflow_id):
    
    data = {
        'workflow_id': workflow_id,
        'task_type': 'code',
    }

    # 定义请求的 URL
    url = "http://localhost:8000/add_task" 

    try:
        # 发送 POST 请求
        response = requests.post(url,json=data)


        # 检查响应状态码
        if response.status_code == 200:
            data = response.json()
            #print(data)
            return data["task_id"]
        else:
            print(f"请求失败，状态码：{response.status_code}")
            print("响应内容：", response.text)

    except requests.exceptions.RequestException as e:
        print("请求发生错误：", e)

def save_task(workflow_id,task_id,code_str,task_input,task_output,resources):
    data = {
        'workflow_id': workflow_id,
        'task_id': task_id,
        'code_str': code_str,
        'task_input': task_input,
        'task_output': task_output,
        'resources': resources,
    }

    # 定义请求的 URL
    url = "http://localhost:8000/save_task" 

    try:
        # 发送 POST 请求
        response = requests.post(url,json=data)

        # 检查响应状态码
        if response.status_code == 200:
            data = response.json()
            #print(data)
            #return data["task_id"]
        else:
            print(f"请求失败，状态码：{response.status_code}")
            print("响应内容：", response.text)

    except requests.exceptions.RequestException as e:
        print("请求发生错误：", e)

def del_task(workflow_id,task_id):
    data = {
        'workflow_id': workflow_id,
        'task_id': task_id,
    }

    # 定义请求的 URL
    url = "http://localhost:8000/del_task" 

    try:
        # 发送 POST 请求
        response = requests.post(url,json=data)

        # 检查响应状态码
        if response.status_code == 200:
            data = response.json()
            #print(data)
            #return data["task_id"]
        else:
            print(f"请求失败，状态码：{response.status_code}")
            print("响应内容：", response.text)

    except requests.exceptions.RequestException as e:
        print("请求发生错误：", e)

def add_edge(workflow_id,source_task_id,target_task_id):
    data = {
        'workflow_id': workflow_id,
        'source_task_id': source_task_id,
        'target_task_id': target_task_id,
    }

    # 定义请求的 URL
    url = "http://localhost:8000/add_edge" 

    try:
        # 发送 POST 请求
        response = requests.post(url,json=data)

        # 检查响应状态码
        if response.status_code == 200:
            data = response.json()
            #print(data)
            #return data["task_id"]
        else:
            print(f"请求失败，状态码：{response.status_code}")
            print("响应内容：", response.text)

    except requests.exceptions.RequestException as e:
        print("请求发生错误：", e)

def del_edge(workflow_id,source_task_id,target_task_id):
    data = {
        'workflow_id': workflow_id,
        'source_task_id': source_task_id,
        'target_task_id': target_task_id,
    }

    # 定义请求的 URL
    url = "http://localhost:8000/del_edge" 

    try:
        # 发送 POST 请求
        response = requests.post(url,json=data)

        # 检查响应状态码
        if response.status_code == 200:
            data = response.json()
            #print(data)
            #return data["task_id"]
        else:
            print(f"请求失败，状态码：{response.status_code}")
            print("响应内容：", response.text)

    except requests.exceptions.RequestException as e:
        print("请求发生错误：", e)


def run_workflow(workflow_id):
    data = {
        'workflow_id': workflow_id,
    }

    # 定义请求的 URL
    url = "http://localhost:8000/run_workflow" 

    try:
        # 发送 POST 请求
        response = requests.post(url,json=data)

        # 检查响应状态码
        if response.status_code == 200:
            data = response.json()
            #print(data)
            #return data["task_id"]
        else:
            print(f"请求失败，状态码：{response.status_code}")
            print("响应内容：", response.text)

    except requests.exceptions.RequestException as e:
        print("请求发生错误：", e)

def get_workflow_status(workflow_id):
    #print(type(workflow_id))
    url = f"ws://localhost:8000/get_workflow_res/{workflow_id}"  # 注意：WebSocket 用 ws:// 或 wss://
   
    def on_message(ws, message):
        print(f"Received: {message}")

    def on_error(ws, error):
        error_code = int.from_bytes(error.data, 'big')
        if error_code == 1000:
            return #1000表示正常关闭
        else:
            print("Error:", error)

    def on_close(ws, close_status_code, close_msg):
        print("Connection closed")

    def on_open(ws):
        print(f"Connected to {url}")

    # 创建 WebSocket 连接
    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    # 运行连接（会阻塞直到连接关闭）
    ws.run_forever()

#1.创建workflow
workflow_id = create_workflow()

#2.新增2个任务
task_id1 = add_task(workflow_id)
task_id2 = add_task(workflow_id)
# del_task(workflow_id,task_id2)
# task_id2 = add_task(workflow_id)

#3.保存任务（输入，输出，所需资源，code str）
code_str = """
from datetime import datetime
import time

def task1(params):
    task_input = params.get("task1_input")
    
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task_input + time_str

    return {
        "task1_output" : result
    }
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
task_output = {
    "output_params":{
        "1":{
            "key": "task1_output", #输出参数的key
            "data_type":"str", #输出参数的类型
        }
    }
}
resources = {
    "cpu":1,
    "cpu_mem": 123,
    "gpu":1,
    "gpu_mem" : 123
}
save_task(workflow_id=workflow_id,task_id=task_id1,code_str=code_str,task_input=task_input,task_output=task_output,resources=resources)


code_str = """
from datetime import datetime
import time 

def task2(params):
    task_input = params.get("task2_input")
    
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task_input + time_str + "===="

    return {
        "task2_output" : result
    }
"""
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
task_output = {
    "output_params":{
        "1":{
            "key": "task2_output",
            "data_type":"str",
        }
    }
}
resources = {
    "cpu":10,
    "cpu_mem": 123,
    "gpu":0.8,
    "gpu_mem" : 324
}
save_task(workflow_id=workflow_id,task_id=task_id2,code_str=code_str,task_input=task_input,task_output=task_output,resources=resources)

#4.添加边
add_edge(workflow_id=workflow_id,source_task_id=task_id1,target_task_id=task_id2)
# del_edge(workflow_id=workflow_id,source_task_id=task_id1,target_task_id=task_id2)
# add_edge(workflow_id=workflow_id,source_task_id=task_id1,target_task_id=task_id2)

#5.执行任务
run_workflow(workflow_id=workflow_id)

#6.获取任务结果
get_workflow_status(workflow_id=workflow_id)
