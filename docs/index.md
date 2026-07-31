---
hide:
  - navigation
  - toc
---

<div class="hero-block" markdown>

# Maze 文档

**分布式智能体 Workflow Runtime**

[快速开始 →](#30-秒上手){ .md-button .md-button--primary }
[Maze 边界 →](maze_boundary.md){ .md-button }
[Server Route 边界 →](server_route_boundary.md){ .md-button }
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

Server route 边界见：[server_route_boundary.md](server_route_boundary.md)。Workbench 后端公开主线只保留 workflow、run、artifact、cluster/resource/queue、task authoring 相关 route；Skills/MCP/Workspace Agent routes 不属于主线 public boundary。

## 文档导航

- [Maze 边界](maze_boundary.md)：Core Runtime、Workflow Agent 和 Workflow Workbench 的主线职责。
- [Server Route 边界](server_route_boundary.md)：Head 与 Workbench 后端保留的公开路由范围。
- [GitHub README](https://github.com/maze-agent/Maze#readme)：安装、运行和 API 使用示例。

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

更多用法请参阅 [GitHub README](https://github.com/maze-agent/Maze#readme)。
