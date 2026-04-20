from core.memory import TemporaryMemory,Message,Role
from core.prompt import DEFAULT_PROMPT
import re
from typing import List
import time
import requests
import json
import os
import networkx as nx
import matplotlib.pyplot as plt
import json
 
            
# class ReactAgent:
#     def __init__(
#         self,
#         model,
#         tools:List[List],
#         server_addr:str,
#         name:str=None,
#         prompt:str=None
#     ):
#         self.server_addr = server_addr
#         self.name = name
   
#         self.model = model
         

#         def extract_info(docs):
#             # Split the string into lines
#             lines = docs.strip().split('\n')
            
#             # Extract the description (first line after the initial empty one)
#             desc = lines[0].strip()
            
#             # Initialize parameters dictionary
#             parameters = {
#                 "type": "object",
#                 "properties": {},
#                 "required": []
#             }
            
#             # Process each line to extract parameter details
#             for line in lines[1:]:
#                 match = re.match(r'\s*(\w+)\(([\w\s]+)\):\s*(.+)', line)
#                 if match:
#                     param_name, param_type, param_desc = match.groups()
#                     parameters["properties"][param_name] = {
#                         "type": param_type.strip(),
#                         "description": param_desc.strip()
#                     }
#                     parameters["required"].append(param_name)
            
#             return desc, parameters

#         def extract_name(full_string):
#             parts = full_string.split('.')
#             name = parts[-1]
#             return name
        
#         self.tools_map = {}
#         self.tools=[]
#         for obj in tools:
#             if hasattr(obj[0], '__name__'):
#                 tool_name = obj[0].__name__
#             else:
#                 tool_name = extract_name(obj[0]._function_name)
#             tool_desc, parameters = extract_info(obj[1])
            
#             info = {
#                 "type": "function",
#                 "function": {
#                     "name": tool_name, 
#                     "description": tool_desc,  
#                     "parameters": parameters,
#                 }
#             }
#             self.tools.append(info)
#             self.tools_map[tool_name] = obj[0]
       
    
#         self.memory = TemporaryMemory()
#         if prompt:
#             self.memory.add_memory(Message(Role.SYSTEM,prompt))
#         else:
#             self.memory.add_memory(Message(Role.SYSTEM,DEFAULT_PROMPT))
        
     
#         #print(self.memory.memory[0]['content'])

#     def submit_task(
#         self,
#         task_name:str,
#         **args,
#     ):  
         
#         url = f"http://{self.server_addr}/task/"
#         payload = {
#             "task_name": task_name,
#             **args,
#         }

#         response = requests.post(url, json=payload)
#         if(task_name=="query_api_model"):
#             return response.json()

#         return response.json()['res']
  
     
#     def run(
#         self,
#         task:str,
#         max_turn:int = 10
#     ):
        
#         self.memory.add_memory(Message(Role.USER,task))
        
#         total_start_time = time.time()

#         if isinstance(self.model, LocalModel):
#             response = self.submit_task(
#                 task_name="query_local_model",
#                 model_id=self.model.model_id,
#                 messages=self.memory.memory,
#                 tools=self.tools,  # 
#                 name=self.name 
#             )

#             #  utils.py  call_local_model 
#             try:
#                 #  API 
#                 if isinstance(response, dict) and "output" in response:
#                     # 
#                     message = response["output"]["choices"][0]["message"]
                    
#                     # 
#                     if "tool_calls" in message:
#                         print("call tool:")
#                         call_res = ""
#                         for tool in message["tool_calls"]:
#                             tool_name = tool['function']['name']
#                             tool_args = json.loads(tool['function']['arguments'])
#                             print(tool_name)
                            
#                             call_tool_res = self.submit_task(
#                                 task_name=tool_name,
#                                 name=self.name,
#                                 **tool_args,
#                             )
                            
#                             call_res = call_res + "Action:" + tool_name + "\n"
#                             call_res = call_res + "Observation:" + call_tool_res + "\n"
                        
#                         self.memory.add_memory(Message(Role.USER, call_res))
#                         print("----------------------------------------------------------------------------------------")
                        
#                         # 
#                         # 
#                         return self.run(call_res, max_turn=max_turn-1)
#                     else:
#                         # 
#                         answer = message["content"]
#                         self.memory.add_memory(Message(Role.ASSISTANT, answer))
#                         print(answer)
#                         return answer
#                 else:
#                     # 
#                     answer = str(response)
#                     self.memory.add_memory(Message(Role.ASSISTANT, answer))
#                     print(answer)
#                     return answer
#             except Exception as e:
#                 error_msg = f"Error processing local model response: {str(e)}\nResponse: {response}"
#                 print(error_msg)
#                 return error_msg
#         else:
#             while(max_turn):
#                 #start_time = time.time()

#                 response = self.submit_task(
#                     task_name="query_api_model",
#                     model_source=self.model.__class__.__name__,
#                     model_id=self.model.model_id,
#                     api_key=self.model.api_key,
#                     messages=self.memory.memory,
#                     tools = self.tools,
#                     name = self.name 
#                 )
                
                
#                 #print(response)
#                 message = response["output"]["choices"][0]["message"]
                
                 
#                 if("tool_calls" in message):
#                     #start_time = time.time()
#                     print("call tool:")
#                     call_res = ""
#                     for tool in response["output"]["choices"][0]["message"]["tool_calls"]:
#                         tool_name = tool['function']['name']
#                         tool_args = json.loads(tool['function']['arguments'])
#                         print(tool_name)
                        
#                         call_tool_res = self.submit_task(
#                             task_name=tool_name,
#                             name = self.name,
#                             **tool_args,
#                         )
                         
                    
#                         call_res = call_res + "Action:" + tool_name + "\n"
#                         call_res = call_res + "Observation:" + call_tool_res + "\n"
#                     self.memory.add_memory(Message(Role.USER,call_res))
                    
#                     print("----------------------------------------------------------------------------------------")
#                 else:        
#                     answer = response["output"]["choices"][0]["message"]["content"]
#                     self.memory.add_memory(Message(Role.ASSISTANT,answer))
#                     print(answer)
#                     break

#                 max_turn = max_turn-1


 
class Agent:
    def __init__(
        self,
        workflow:str, #dag workflow folder
        port:str="5001", #scheduler port
    ) -> None:
        self.addr = "127.0.0.1:"+port
        self.workflow = workflow
    
    def show(self):
        """
        draw agent workflow
        """
        dag_json_file = os.path.join(self.workflow, 'dag.json')
        with open(dag_json_file, 'r') as file:
            dag_data = json.load(file) 
        nodes = dag_data.get('nodes', [])
        edges = dag_data.get('edges', [])
         
        
        G = nx.DiGraph()
        for node in nodes:
            G.add_node(node['task'])
        for edge in edges:
            G.add_edge(edge['source'], edge['target'])

        #topological_order = list(nx.topological_sort(G))

        pos = {node: (i, -j) for j, layer in enumerate(nx.topological_generations(G)) for i, node in enumerate(layer)}

        plt.figure(figsize=(3, 3))
        nx.draw(G, pos, with_labels=True, node_size=1000, node_color='lightblue', font_size=15, arrowsize=20)
        plt.title("Agent Workflow")
        plt.show()    
      
    def run(self,wait=False):    
        payload = {
            "dag_folder":self.workflow 
        } 
        response = requests.post(f"http://{self.addr}/dag/", json=payload)
        res = response.json()
        
        if res["status"]=="error":
            print(res)
        else:
            data = res["data"]
            dag_id = data["dag_id"]
            task_ids = data["task_ids"]

            print(f"🎁 Agent workflow submit to scheduler")
            print(dag_id)
            print(task_ids)

            if wait:
                #workflow
                start_time = time.time()
                while True:
                    time.sleep(0.5)
                    payload = {
                        "dag_id":dag_id 
                    }
                    response = requests.post(f"http://{self.addr}/status/", json=payload)
                    res = response.json()
                    
                    finish = True
                    for task,status in res["data"]["dag_status"].items():
                        if status == "unfinished":
                            finish = False
                            break
                        
                    if finish:
                        print("✅ Agent workflow finish")
                        break              
                print(f"{time.time()-start_time}")
                
                #return
                for task_name,task_id in task_ids.items():
                    payload = {
                        "dag_id":dag_id,
                        "func_name":task_name,
                        "task_id":task_id, 
                    }
                    response = requests.post(f"http://{self.addr}/get/", json=payload)
                    res = response.json()
                    if res["status"]=="error":
                        print(res["msg"])
                    else:
                        print(f"{task_name}:{res['data']['task_ret_data']}")
                            
                
                #

                payload = {
                    "dag_id":dag_id,
                    "task_ids":task_ids
                }
                response = requests.post(f"http://{self.addr}/release/", json=payload)
                res = response.json()
                if res["status"]=="error":
                    print(res["msg"])
             
              





