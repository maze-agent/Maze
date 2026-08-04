# Maze Playground Backend

Express 后端负责 workspace、system catalog、Workspace Agent，以及 Maze Core API 代理。普通工作流在 Node 中编译后直接提交 Core；Python bridge 不执行普通工作流。

## 执行路径

```text
POST /api/workflows/:id/run
  -> compileWorkflowToDagSpec
  -> POST <MAZE_CORE_URL>/workflows/submit
  -> return Core run_id
```

运行详情统一来自：

- `GET /api/runs`
- `GET /api/runs/:runId`
- `GET /api/runs/:runId/events`
- `GET /api/runs/:runId/logs`
- `GET /api/runs/:runId/artifacts`
- `GET /api/runs/:runId/tasks/:taskId/artifacts`

GAIA Core Run 会从公开 `/api/runs*` 响应中过滤。GAIA 私有 trace 继续由 benchmark API 管理。

## Python Bridge

`maze_bridge.py` 保留以下职责：

- 解析 custom、workspace 和 catalog task
- workspace task/skill 管理
- Workspace Agent、ReAct 和 MCP 支持
- GAIA 私有流程

## 配置

```bash
PORT=3001
MAZE_CORE_URL=http://localhost:8000
MAZE_WORKSPACES_DIR=/path/to/workspaces
MAZE_SYSTEM_CATALOG_DIR=/path/to/system_catalog
PYTHON_BIN=/path/to/python
```

## 启动与验证

```bash
npm install
npm run dev

node --check src/server.js
node --test test/workflow_dag_spec.test.js
curl http://localhost:3001/health
```
