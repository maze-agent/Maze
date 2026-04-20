import threading
import requests
import os
import networkx as nx
import matplotlib.pyplot as plt
import json
import logging

class Task():
    def __init__(self,dag_id,task_ids):
        self.dag_id = dag_id
        self.task_ids = task_ids
      
    def status(self):
        agentos = AgentOS_()
        
        payload = {
            "dag_id":self.dag_id 
        }
        response = requests.post(f"http://{agentos.task_center_addr}/status/", json=payload)
        res = response.json()
        if res["status"]=="error":
            print(res["msg"])
        else:
            print(res["data"])
    
    
    def get(self,func_name):
        agentos = AgentOS_()
        
        payload = {
            "dag_id":self.dag_id,
            "func_name":func_name,
            "task_id":self.task_ids[func_name],
            
        }
        response = requests.post(f"http://{agentos.task_center_addr}/get/", json=payload)
        res = response.json()
        if res["status"]=="error":
            print(res["msg"])
        else:
            return res["data"]["task_ret_data"]
    
       
    def release(self):
        agentos = AgentOS_()
         
        payload = {
            "dag_id":self.dag_id,
            "task_ids":self.task_ids
        }
        response = requests.post(f"http://{agentos.task_center_addr}/release/", json=payload)
        res = response.json()
        if res["status"]=="error":
            print(res["msg"])
        else:
            print(res["msg"])
        
       

class AgentOS_:
    _instance = None
    _lock = threading.Lock()
 
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(AgentOS_, cls).__new__(cls)
                    cls._initialize_instance(*args, **kwargs)
        return cls._instance


    @classmethod
    def _initialize_instance(cls, *args, **kwargs):
        cls.task_center_addr = kwargs['addr']
    
 


def init(addr:str=None):
    if addr is None:
        raise ValueError("Missing required parameters:addr(Task Center Addr)")
        
    agentos = AgentOS_(addr=addr)
    
 

def draw(folder:str):
    dag_json_file = os.path.join(folder, 'dag.json')
    with open(dag_json_file, 'r') as file:
        dag_data = json.load(file) 
    nodes = dag_data.get('nodes', [])
    edges = dag_data.get('edges', [])
    
    
    
    G = nx.DiGraph()
    for node in nodes:
        G.add_node(node['task'])
    for edge in edges:
        G.add_edge(edge['source'], edge['target'])

    #

    topological_order = list(nx.topological_sort(G))

    #

    pos = {node: (i, -j) for j, layer in enumerate(nx.topological_generations(G)) for i, node in enumerate(layer)}

    plt.figure(figsize=(8, 6))
    nx.draw(G, pos, with_labels=True, node_size=3000, node_color='lightblue', font_size=15, arrowsize=20)
    plt.title("DAG")
    plt.show()
    
    
def submit(folder:str):
    agentos = AgentOS_()
     
    payload = {
        "dag_folder":folder 
    }
    response = requests.post(f"http://{agentos.task_center_addr}/dag/", json=payload)
    res = response.json()
     
    if res["status"]=="error":
        print(res)
    else:
        data = res["data"]
        return Task(data["dag_id"],data["task_ids"])
        
       

    
      
    
