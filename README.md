<h2 align="center"><img src="./assets/imgs/image.png" style="height:1em; width:auto; vertical-align:middle"/> Maze: A Distributed Framework for LLM Agents</h2>

<p align="center">
    <a href="https://mazeagent.net/">
        <img src="https://img.shields.io/badge/Website-mazeagent.net-blue?style=for-the-badge&logo=google-chrome&logoColor=white" alt="Website">
    </a>
    <a href="https://maze-doc-new.readthedocs.io/en/latest/">
        <img src="https://img.shields.io/badge/Docs-ReadTheDocs-black?style=for-the-badge&logo=readthedocs&logoColor=white" alt="Documentation">
    </a>
</p>

## 🌟 Why Maze?
- **Task-level**

  Maze enables fine-grained, task-level management, enhancing system flexibility and composability while supporting task parallelism to significantly improve the end-to-end performance of agent workflows.

- **Resource Management**

  Maze supports resource allocation for workflow tasks, effectively preventing resource contention both among parallel tasks within a single workflow and across multiple concurrently executing workflows.

- **Distributed Deployment**

  Maze supports not only standalone but also distributed deployment, allowing you to build highly available and scalable Maze clusters to meet the demands of large-scale concurrency and high-performance computing.

- **Multi-Agent Support**

  Maze can serve as a runtime backend for other agent frameworks. For example, it allows LangGraph to be seamlessly migrated to Maze and automatically gain task-level parallelism without modifying original logic. [**Example**](https://github.com/QinbinLi/Maze/tree/develop/examples/financial_risk_workflow)

<br>


## 📰 News


- **2026-05**: We support dynamic workflows with runtime `append_task`, lifecycle events, persisted run history, and developer inspection.
- **2026-05**: Maze Playground now supports user workspaces, including file upload, download, preview, task-side file processing, and run artifact downloads.
- **2026-05**: Maze Playground can generate workspace tasks from natural-language prompts through OpenAI-compatible LLM APIs.
- **2026-01**: We support the sandbox feature! [**Docs**](https://github.com/QinbinLi/Maze/tree/develop/examples/sandbox)

<br>

## 🚀 Quick Start

## 1. Install

**From PyPI (Recommended)**

   ```bash
   pip install maze-agent
   ```

**From source**

   ```bash
   git clone https://github.com/maze-agent/Maze.git
   cd Maze
   pip install -e .
   ```
## 2. Launch Maze
   Launch Maze Head as maze server. The maze server can receive the workflow of the agent.

   ```
   maze start --head --port HEAD_PORT
   ```
   If there are multiple machines, you can connect other machines as maze workers to the maze head.
   ```
   maze start --worker --addr HEAD_IP:HEAD_PORT
   ```
## 3. Example

### Static Workflow

```python
from maze import MaClient, task

# 1. Define task functions using the @task decorator.
@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def greet(text: str = ""):
    return {"result": f"Hello {text}"}


@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def uppercase(result: str = ""):
    return {"upper": result.upper()}


# 2. Create the Maze client.
client = MaClient("http://localhost:8000")

# 3. Create a workflow and wire task outputs into downstream inputs.
workflow = client.create_workflow()
greeting = workflow.add_task(
    greet,
    inputs={"text": "Maze"}
)
upper = workflow.add_task(
    uppercase,
    inputs={"result": greeting.outputs["result"]}
)

# 4. Submit the workflow and get results.
run_id = workflow.run()
workflow.show_results(run_id)
```

### Dynamic Workflow

```python
from maze import MaClient, task


@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def summarize(topic: str = ""):
    return {"summary": f"Maze can build workflows dynamically for {topic}."}


client = MaClient("http://localhost:8000")

run = client.create_dynamic_run(max_tasks=10)
summary = run.append_task(
    summarize,
    inputs={"topic": "agent runtime"}
)
run.wait_for_task(summary)
run.finalize({"status": "done"})
print(run.status())
```

In Maze Playground, files uploaded under `workspace/files` are staged into each task sandbox. Task code should read and write files with relative paths such as `Path("input.csv")`, `Path("folder/data.json")`, or `Path(".")`; it should not hard-code `workspace/files/...`.
<br>



## 🖥️ Maze Playground
Maze Playground supports building workflows through a drag-and-drop interface, managing workspace files, and generating workspace tasks from prompts. You can start the playground with the following command option.
```
maze start --head --port HEAD_PORT --playground
```
Here are two videos showing the process of using built-in tasks and uploading user-defined tasks in Maze Playground. For detailed usage instructions, please refer to the [**Maze Playground**](https://maze-doc-new.readthedocs.io/en/latest/playground.html).


### Builtin Task Workflow
![Design Workflow Screenshot](https://meeting-agent1.oss-cn-beijing.aliyuncs.com/builtin_task.png)  
[Design Workflow Video](https://meeting-agent1.oss-cn-beijing.aliyuncs.com/builtin_task.mp4)

### User Defined Task Workflow
![Check Result Screenshot](https://meeting-agent1.oss-cn-beijing.aliyuncs.com/userdef_task.png)  
[Check Result Video](https://meeting-agent1.oss-cn-beijing.aliyuncs.com/userdef_task.mp4)

## Acknowledgement
We thank contributors from Huazhong University of Science and Technology, Huawei, and other institutions for their support and contributions to this project.
