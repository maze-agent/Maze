# Maze

[**Documentation**](https://maze-doc-new.readthedocs.io/en/latest/)
<p align="center">
  <img
    src="./assets/imgs/maze-logo-icon.svg"
    alt="AgentScope Logo"
    width="200"
  />
</p>
<br>
 



# 🌟Why  Maze？
- Resource Management:

  When multiple tasks run in parallel within a single workflow—or when multiple workflows execute concurrently—resource contention can occur. Without proper coordination, this may lead to severe resource overloads, such as GPU out-of-memory (OOM) errors.
- Task Parallelism:

  Maze enables true parallel execution of ready-to-run tasks. In contrast, the LangGraph framework is constrained by Python’s Global Interpreter Lock (GIL), which prevents genuine parallelism. Maze supports seamless migration from LangGraph-based workflows while unlocking true parallel execution for ready tasks.

- Distributed Deployment:

  Maze natively supports distributed deployment, allowing you to build highly available and scalable Maze clusters to meet the demands of large-scale concurrency and high-performance computing.

<br>


# 🚀Quick Start

## 1. Install

**From source**

   ```
   git clone https://github.com/QinbinLi/Maze.git
   cd Maze
   pip install -e .
   ```
## 2. Launch Maze

   ```
   mazea start --head --port HEAD_PORT
   ```
   If there are multiple machines, you can start multiple Maze workers.
   ```
   mazea start --worker --addr HEAD_IP:HEAD_PORT
   ```
## 3. Example

```
   
```
<br>



# 🖥️ Front-end
We support  building workflows through a drag-and-drop interface on the front-end.


### Design Workflow
![Design Workflow Screenshot](https://meeting-agent1.oss-cn-beijing.aliyuncs.com/create_workflow.png)  
[Design Workflow Video](https://meeting-agent1.oss-cn-beijing.aliyuncs.com/create_workflow.mp4)

### Check Result
![Check Result Screenshot](https://meeting-agent1.oss-cn-beijing.aliyuncs.com/check_result.png)  
[Check Result Video](https://meeting-agent1.oss-cn-beijing.aliyuncs.com/check_result.mp4)


