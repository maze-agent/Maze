import unittest
import requests  pytest
import websocket
import requests
import websocket

MAZE_SERVER_ADDR = "localhost:8000"
 
def create_workflow():
    url = f"http://{MAZE_SERVER_ADDR}/create_workflow"

    try:
        response = requests.post(url)

        if response.status_code == 200:
            data = response.json()
            return data["workflow_id"]
        else:
            print(f"请求失败，状态码：{response.status_code}")
            print("响应内容：", response.text)

    except requests.exceptions.RequestException as e:
        print("请求发生错误：", e)

def add_task(workflow_id,task_name):
    data = {
        'workflow_id': workflow_id,
        'task_type': 'code',
        'task_name': task_name,
    }

    url = f"http://{MAZE_SERVER_ADDR}/add_task"

    try:
        response = requests.post(url,json=data)
 
        if response.status_code == 200:
            data = response.json()
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

    url = f"http://{MAZE_SERVER_ADDR}/save_task"

    try:
        response = requests.post(url,json=data)

        if response.status_code == 200:
            data = response.json()
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

    url = f"http://{MAZE_SERVER_ADDR}/del_task" 

    try:
        response = requests.post(url,json=data)

        if response.status_code == 200:
            data = response.json()
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

    url = f"http://{MAZE_SERVER_ADDR}/add_edge" 

    try:
        response = requests.post(url,json=data)

        if response.status_code == 200:
            data = response.json()
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

    url = f"http://{MAZE_SERVER_ADDR}/del_edge"
    try:
        response = requests.post(url,json=data)

        if response.status_code == 200:
            data = response.json()
        else:
            print(f"请求失败，状态码：{response.status_code}")
            print("响应内容：", response.text)

    except requests.exceptions.RequestException as e:
        print("请求发生错误：", e)

def run_workflow(workflow_id):
    data = {
        'workflow_id': workflow_id,
    }

    
    url = f"http://{MAZE_SERVER_ADDR}/run_workflow" 

    try:
        response = requests.post(url,json=data)

        if response.status_code == 200:
            data = response.json()
        else:
            print(f"请求失败，状态码：{response.status_code}")
            print("响应内容：", response.text)

    except requests.exceptions.RequestException as e:
        print("请求发生错误：", e)

def get_workflow_status(workflow_id):
    url = f"ws://{MAZE_SERVER_ADDR}/get_workflow_res/{workflow_id}"  
   
    def on_message(ws, message):
        print(f"Received: {message}")

    def on_error(ws, error):
        error_code = int.from_bytes(error.data, 'big')
        if error_code == 1000:
            return
        else:
            print("Error:", error)

    def on_close(ws, close_status_code, close_msg):
        print("Connection closed")

    def on_open(ws):
        print(f"Connected to {url}")

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    ws.run_forever()

class TestWorkflowSystem(unittest.TestCase):
    def test_full_workflow_execution(self):
        workflow_id = create_workflow()

        task_id1 = add_task(workflow_id, task_name="task1")
        task_id2 = add_task(workflow_id, task_name="task2")

        code_str = """
from datetime import datetime
import time

def task1(params):
    task_input = params.get("task1_input")
    time.sleep(2)
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

    time.sleep(2)
    
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
                "key": "task2_input", 
                "input_schema":"from_task", 
                "data_type":"str",
                "value" : f"{task_id1}.output.task1_output",
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

        add_edge(workflow_id=workflow_id,source_task_id=task_id1,target_task_id=task_id2)

        run_workflow(workflow_id=workflow_id)

        get_workflow_status(workflow_id=workflow_id)

if __name__ == '__main__':
    unittest.main()