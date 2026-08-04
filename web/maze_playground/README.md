# Maze Playground

Maze Playground 是 Maze 的 React 工作流编辑器。工作流 JSON 保存在 workspace，执行时由 Node 后端编译为 `maze.workflow/v1`，提交给 Maze Core。

## 架构

```text
React/Vite
    | workspace、catalog、agent API
Express backend
    | POST /workflows/submit；代理 /runs、events、logs、artifacts
Maze Core
```

普通静态工作流只使用 Core `run_id`。Node 不执行第二份 Python 工作流，也不保存运行结果镜像。`maze_bridge.py` 只承载任务解析、workspace 工具、ReAct、MCP 和 GAIA 私有流程。

## 启动

从仓库根目录启动完整服务：

```bash
conda activate maze
maze start --head --port 8000 --playground
```

默认地址：

- Maze Core: `http://localhost:8000`
- Playground backend: `http://localhost:3001`
- Playground frontend: `http://localhost:5173`

也可以分别运行：

```bash
cd web/maze_playground/backend
npm install
npm run dev

cd ../frontend
npm install
npm run dev
```

## 所有权

- workspace workflow 文件是编辑事实源。
- `system_catalog/tasks` 是内置任务事实源。
- Maze Core 是 Run、事件、日志和 artifact 事实源。
- GAIA 私有 trace 与旧 Playground JSON 只用于受控映射和历史只读访问。

## 验证

```bash
conda run -n maze node --test web/maze_playground/backend/test/workflow_dag_spec.test.js
conda run -n maze pytest -q tests/test_dag_submit_contract.py tests/test_playground_task_metadata.py
cd web/maze_playground/frontend && conda run -n maze npm run build
```
