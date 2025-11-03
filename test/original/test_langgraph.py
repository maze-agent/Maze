from langgraph.graph import StateGraph, END
from typing import TypedDict, List
from langgraph.graph import StateGraph, START, END
from typing import Any
from maze import LanggraphClient

MAZE_SERVER_ADDR = "localhost:8000"

client = LanggraphClient(addr=MAZE_SERVER_ADDR)  
 
class GraphState(TypedDict):
    result1: str
    result2: str
    result3: str
    
@client.task(resources={"cpu": 1,"gpu": 1,"cpu_mem": 31,"gpu_mem": 12})
def cpu_tool_1(state: GraphState) -> GraphState:
    result = "CPU Tool 1 done"
    return {"result1":result}

def cpu_tool_2(state: GraphState) -> GraphState:
    result = "CPU Tool 2 done"
    return {"result2":result}

def cpu_tool_3(state: GraphState) -> GraphState:
    result = "CPU Tool 3 done"
    return {"result3":result}

def start_node(state: GraphState) -> GraphState:
    print("Start node executed")
    return state

builder = StateGraph(GraphState)

builder.add_node("start", start_node)
builder.add_node("tool1", cpu_tool_1)
builder.add_node("tool2", cpu_tool_2)
builder.add_node("tool3", cpu_tool_3)
 

builder.add_edge(START, "start")
builder.add_edge("start", "tool1")
builder.add_edge("start", "tool2")
builder.add_edge("start", "tool3")

builder.add_edge("tool1", END)
builder.add_edge("tool2", END)
builder.add_edge("tool3", END)

graph = builder.compile()



initial_state: dict[str, list[Any]] = {"results": []}
result = graph.invoke(initial_state)
print("Final state:", result)