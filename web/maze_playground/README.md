# Maze Playground

Maze Playground 是 Maze 的 React 工作流编辑器。工作流 JSON 保存在 workspace，执行时由 Node 后端编译为 `maze.workflow/v1`，提交给 Maze Core。

## 架构

```text
React/Vite
    | workspace、catalog API
Express backend
    | POST /workflows/submit；代理 /runs、events、logs、artifacts
Maze Core
```

普通静态工作流只使用 Core `run_id`。Node 不执行第二份 Python 工作流，也不保存运行结果镜像。`maze_bridge.py` 只承载任务解析和 workspace task 文件操作。

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
- GAIA 直接创建私有 Core Run，并由本地 validation runner 评分。
- 旧 Playground JSON 保留在磁盘上，但不再读取或写入。

## 验证

```bash
cd web/maze_playground/frontend && conda run -n maze npm run build
```
