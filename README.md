<h2 align="center"><img src="./assets/imgs/image.png" style="height:1em; width:auto; vertical-align:middle"/> Maze:A Task-Level Distributed Agent Workflow Framework</h2>


[**Documentation**](https://maze-doc-new.readthedocs.io/en/latest/)

 
<br />

## 🌟Why  Maze？
- **Task-level**

  Maze enables fine-grained, task-level management, enhancing system flexibility and composability while supporting task parallelism to significantly improve the end-to-end performance of agent workflows.

- **Resource Management**

  Maze supports resource allocation for workflow tasks, effectively preventing resource contention both among parallel tasks within a single workflow and across multiple concurrently executing workflows.

- **Distributed Deployment**

  Maze supports not only standalone but also distributed deployment, allowing you to build highly available and scalable Maze clusters to meet the demands of large-scale concurrency and high-performance computing.

- **Multi-Agent Support**

  Maze can serve as a runtime backend for other agent frameworks.For example, it allows LangGraph to be seamlessly migrated to Maze and automatically gain task-level parallelism without modifying original logic. [**Example**](https://github.com/QinbinLi/Maze/tree/develop/examples/financial_risk_workflow)

<br>


## 🚀Quick Start

## 1. Install

**From PyPI (Recommended)**

   ```bash
   pip install maze-agent
   ```

**From source**

   ```bash
   git clone https://github.com/QinbinLi/Maze.git
   cd Maze
   pip install -e .
   ```
## 2. Launch Maze
   Launch Maze Head.

   ```
   maze start --head --port HEAD_PORT
   ```
   If there are multiple machines, you can start multiple Maze workers.
   ```
   maze start --worker --addr HEAD_IP:HEAD_PORT
   ```
## 3. Example

```python
from typing import Any
from maze import MaClient,task

#1.Define your task functions using the @task decorator
@task(
    inputs=["text"],
    outputs=["result"],
)
def my_task(params):
    text: Any = params.get("text")
    return {"result": f"Hello {text}"}

#2.Create the maze client
client = MaClient("http://localhost:8000")

#3.Create the workflow
workflow = client.create_workflow()
task1 = workflow.add_task(
    my_task,
    inputs={"text": "Maze"}
)

#4.Submit the workflow and get results
workflow.run() 
workflow.show_results()
```
<br>



## 🖥️ Maze Playground
We support building workflows through a drag-and-drop interface on the Maze Playground.Here are two pages for reference. For detailed usage instructions, please refer to the [**Maze Playground**](https://maze-doc-new.readthedocs.io/en/latest/playground.html).


### Design Workflow
![Design Workflow Screenshot](https://meeting-agent1.oss-cn-beijing.aliyuncs.com/create_workflow.png)  
[Design Workflow Video](https://meeting-agent1.oss-cn-beijing.aliyuncs.com/create_workflow.mp4)

### Check Result
![Check Result Screenshot](https://meeting-agent1.oss-cn-beijing.aliyuncs.com/check_result.png)  
[Check Result Video](https://meeting-agent1.oss-cn-beijing.aliyuncs.com/check_result.mp4)


