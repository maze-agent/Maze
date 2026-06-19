---
hide:
  - navigation
  - toc
---

<div class="hero-block" markdown>

![Maze](assets/logo.png)

# Maze 文档

**分布式智能体 Workflow Runtime**

[开始阅读 API 文档 →](api_zh.md){ .md-button .md-button--primary }
[Maze 边界 →](maze_boundary.md){ .md-button }
[GitHub 仓库](https://github.com/maze-agent/Maze){ .md-button }

</div>

## 这是什么？

[Maze](https://github.com/maze-agent/Maze) 是一个面向 LLM Agent 应用的 **分布式 workflow runtime**。
它让你用一个 `@task` 装饰器声明 Python 函数，自动调度到 Ray 集群上并行执行；
同时提供静态 DAG、动态运行时追加、资源感知调度、Worker 执行、run/task 状态、日志、artifact 和集群观测能力。

Maze 的主线边界是：

```text
Maze = Core Runtime + Workflow Agent + Workflow Workbench
```

- **Core Runtime**：负责静态/动态 DAG 的校验、调度、执行、资源管理、Worker、日志、artifact 和失败恢复。
- **Workflow Agent / Workflow Planner**：只负责生成或修复 `WorkflowSpec` / `WorkflowPatch` / `TaskSpec` / `ResourceSpec`，不直接执行工具，不调 MCP，不加载 skills，不做 workspace chat。
- **Workflow Workbench**：负责 DAG 可视化、人工编辑、task placement、资源/队列/日志/artifact 可视化。

Phase 1 删除的是旧 `WorkspaceAgentPanel`，不是 Workflow Agent。未来可以新增 `WorkflowPlannerPanel`，但它只能输出 Maze-native workflow/patch/spec 结构，不能恢复通用 Workspace Agent。

本站点提供 Maze 的 **完整中文 API 文档**，按以下层级组织：

<div class="grid cards" markdown>

-   :material-language-python:{ .lg .middle } **Python SDK**

    ---
    `@task` 装饰器、`MaClient`、静态 / 动态 workflow、WorkflowSpec…

    [→ 跳转](api_zh.md#一python-sdk-api)

-   :material-api:{ .lg .middle } **HTTP & WebSocket API**

    ---
    Head 服务的 30+ FastAPI 端点，含完整请求 / 响应 JSON 示例。

    [→ 跳转](api_zh.md#二head-服务-http--websocket-api)

-   :material-console:{ .lg .middle } **CLI**

    ---
    `maze start --head | --worker`、`maze stop` 全部参数。

    [→ 跳转](api_zh.md#三cli-命令)

-   :material-puzzle:{ .lg .middle } **Playground 后端**

    ---
    可视化界面的 40+ Node.js REST 端点。

    [→ 跳转](api_zh.md#四maze-playground-后端-rest-api)

-   :material-broadcast:{ .lg .middle } **事件协议**

    ---
    调度器、run/task 状态、日志、artifact 和资源观测事件。

    [→ 跳转](api_zh.md#五事件协议event-protocol)

-   :material-book-open-page-variant:{ .lg .middle } **完整示例**

    ---
    静态 / 动态 / app spec / curl 等 Maze-native 使用形态。

    [→ 跳转](api_zh.md#八完整示例)

</div>

## 30 秒上手

```bash
pip install maze-agent
maze start --head --port 8000
```

```python
from maze import MaClient, task

@task(resources={"cpu": 1, "cpu_mem": 128})
def greet(text: str = ""):
    return {"result": f"Hello {text}"}

client = MaClient("http://localhost:8000")
wf = client.create_workflow()
g = wf.add_task(greet, inputs={"text": "Maze"})
run_id = wf.run()
wf.show_results(run_id)
```

更多示例与完整 API：[API 文档](api_zh.md)
