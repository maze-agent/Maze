from langgraph.graph import StateGraph, END
from typing import TypedDict, List
from langgraph.graph import StateGraph, START, END
from typing import Any
from maze import LanggraphClient
import threading

MAZE_SERVER_ADDR = "localhost:8000"

client = LanggraphClient(addr=MAZE_SERVER_ADDR)  
 
class GraphState(TypedDict):
    result1: str
    result2: str
    result3: str
    
@client.task
def cpu_tool_1(state: GraphState) -> GraphState:
    result = "CPU Tool 1 done"
    for i in range(50000000):
        pass
    return {"result1":result}

@client.task
def cpu_tool_2(state: GraphState) -> GraphState:
    result = "CPU Tool 2 done"
    for i in range(50000000):
        pass
    return {"result2":result}

@client.task
def cpu_tool_3(state: GraphState) -> GraphState:
    result = "CPU Tool 3 done"
    for i in range(50000000):
        pass
    return {"result3":result}

def start_node(state: GraphState) -> GraphState:
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
assert(result['result1']=='CPU Tool 1 done')
assert(result['result2']=='CPU Tool 2 done')
assert(result['result3']=='CPU Tool 3 done')
 