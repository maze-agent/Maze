# Maze Playground 更新记录

更新时间：2026-05-06  
工作目录：`/home/gujing/code/maze/Maze`

## 背景

这轮更新主要围绕 Maze Playground 的本地可用性、workspace 化、workflow/task 管理体验和导入导出能力展开。目标是让用户在 WSL 中启动 Maze，在 Windows 浏览器里直接使用前端，并能用 workspace 组织自己的 tasks 与 workflows。

## 运行与启动

本地已有 conda 环境 `maze`，启动时需要先进入该环境：

```bash
source /home/gujing/miniconda3/etc/profile.d/conda.sh
conda activate maze
```

当前 Playground 涉及三个服务：

```bash
# Maze API
python -m maze.server

# Playground backend
cd /home/gujing/code/maze/Maze/web/maze_playground/backend
node src/server.js

# Playground frontend
cd /home/gujing/code/maze/Maze/web/maze_playground/frontend
./node_modules/.bin/vite --host 0.0.0.0
```

Windows 侧访问前端，优先打开：

```text
http://localhost:5173
```

如果打开的是 `:3001/api` 或 `:8000`，看到的是后端/API 服务，不是前端页面。

## 文档

新增 Maze 说明文档，整理了 Maze 是什么、主要功能、目录结构、使用方式和基本原理：

```text
/home/gujing/code/maze/Maze/docs/260506/whatismaze.md
```

同时也保留了一份更新目录下的说明材料：

```text
/home/gujing/code/maze/Maze/updates/260505/whatismaze.md
```

## 后端更新

后端主要文件：

```text
web/maze_playground/backend/src/server.js
web/maze_playground/backend/maze_bridge.py
```

主要变化：

- 增加默认 workspace 目录：

```text
/home/gujing/code/maze/Maze/workspace
```

- workspace 下固定使用两个子目录：

```text
workspace/tasks
workspace/workflows
```

- 新增 workspace tasks API：

```text
GET    /api/workspace-tasks
POST   /api/workspace-tasks
DELETE /api/workspace-tasks
PATCH  /api/workspace-tasks/rename
```

- 新增 workspace workflows API：

```text
GET    /api/workspace-workflows
POST   /api/workspace-workflows/save
POST   /api/workspace-workflows/load
POST   /api/workspace-workflows/import
DELETE /api/workspace-workflows
PATCH  /api/workspace-workflows/rename
```

- Python bridge 支持扫描 `tasks/**/*.py`，自动提取被 `@task` 装饰的 workspace task。
- Python bridge 支持运行 workflow 时加载 workspace task 文件。
- 修复运行时缺少 `time` 导入导致的潜在错误。
- 增加任务运行进度事件，将 Python 侧进度通过 stderr 传给 Node，再通过 WebSocket 推给前端。
- 设置 `sys.dont_write_bytecode = True`，减少 workspace 下生成 `__pycache__` 的干扰。

## 前端更新

主要文件：

```text
web/maze_playground/frontend/src/App.tsx
web/maze_playground/frontend/src/api/client.ts
web/maze_playground/frontend/src/components/BuiltinTasksSidebar.tsx
web/maze_playground/frontend/src/components/Toolbar.tsx
web/maze_playground/frontend/src/components/WorkflowCanvas.tsx
web/maze_playground/frontend/src/components/NodePanel.tsx
web/maze_playground/frontend/src/components/CustomNode.tsx
web/maze_playground/frontend/src/components/CustomTaskEditor.tsx
web/maze_playground/frontend/src/stores/workflowStore.ts
web/maze_playground/frontend/src/types/workflow.ts
```

主要变化：

- 侧边栏支持折叠/展开。
- 侧边栏结构调整为 workspace 优先：

```text
Workspace
Workflows
Tasks
Workspace Tasks
Builtin Tasks
```

- Builtin Tasks 展示更友好的任务描述。
- Workspace Tasks 支持从当前 workspace 自动加载。
- Workspace Workflows 支持从当前 workspace 自动加载。
- 顶部 workflow 名称支持点击后就地编辑，失焦自动保存。
- 左侧 workflow 卡片名称支持点击编辑，失焦自动更新。
- 左侧 workspace task 卡片名称支持点击编辑，失焦自动更新。
- 画布上的节点支持 `Delete` / `Backspace` 删除。
- 全局支持 `Ctrl+S` / `Cmd+S` 保存当前 workflow 到 workspace。
- workflow 运行时前端可以看到更明确的运行状态和任务进度。
- Custom Task / Workspace Task 的编辑逻辑调整为右侧 Drawer，避免误把更新操作当成添加节点。

## Workspace Tasks 体验

Workspace task 的设计更新为：

- 默认从 `workspace/tasks/**/*.py` 扫描。
- 卡片默认折叠，仅显示任务名、简单描述和少量重要信息。
- 点击卡片展开详情。
- 继续使用拖拽方式把 task 添加到画布。
- 去掉 task 卡片上的 Add 按钮，减少误操作。
- `Update` 按钮打开右侧编辑 Drawer，用于修改 workspace 下的 Python 文件。
- Update 不会把 task 自动添加到画布。
- 展开后的 workspace task 支持 Import / Export：
  - Import：导入 `.py` 内容并替换当前 workspace task 文件。
  - Export：下载当前 workspace task 的 `.py` 文件。
- 选中 workspace task 卡片后，按 `Delete` / `Backspace` 可删除对应 Python 文件。
- 删除 workspace task 时，会同步移除画布上引用该文件的节点。

## Workspace Workflows 体验

Workspace workflow 的设计更新为：

- 默认从 `workspace/workflows/**/*.json` 扫描。
- 点击 workflow 卡片会加载该 workflow 到当前画布。
- workflow 卡片支持点击名称编辑，失焦自动更新。
- 选中 workflow 卡片后，按 `Delete` / `Backspace` 可删除对应 JSON 文件。
- 左侧 Workflows 区域的 Save 会把当前画布保存到 workspace 下。
- 顶部 `Ctrl+S` 也会保存当前 workflow。
- 保存后的 workflow 会记住当前 workspace workflow 文件路径，后续保存会更新同一个文件。

## Workflow 导入导出

这是本轮最后一项重点修正。

旧问题：

- 导出的 workflow JSON 只有 nodes/edges。
- 如果 workflow 使用了 workspace task，导入到另一个 workspace 后，缺少对应 Python task 文件。
- 导入后节点虽然存在，但运行时找不到 task 定义。

现在的行为：

- 导出的 workflow JSON 升级为 `version: 2`。
- 导出的 JSON 会包含：

```text
workflow.nodes
workflow.edges
workflow.taskDefinitions
```

- `taskDefinitions` 中保存 workspace task 的关键信息：

```text
relativePath
functionName
displayName
code
inputs
outputs
resources
```

- 导入 workflow 时，后端会读取 `workflow.taskDefinitions`，并写入当前 workspace 的 `tasks/` 目录。
- 去重规则：按 Python 文件路径去重。
- 如果当前 workspace 已存在同路径 `.py` 文件，则跳过，不覆盖。
- 导入后，workflow 节点的 `workspaceDir` 会自动改成当前 workspace，而不是导出机器上的旧路径。
- 旧版没有 `taskDefinitions` 的 workflow JSON 仍然可以导入，只是不会自动补 task 文件。

## 关键实现点

后端新增/调整的核心逻辑：

- `normalizeTaskRelativePath`
- `resolveTaskDefinitionFile`
- `collectTaskDefinitions`
- `importTaskDefinitions`
- `applyWorkspaceToWorkflowNodes`

前端新增/调整的核心逻辑：

- `TaskDefinition` 类型。
- `api.importWorkspaceWorkflow`。
- 导出时从画布 workspace 节点收集 task definitions。
- 导入时调用后端接口，让后端负责把 task definitions 落盘。
- 导入完成后刷新 workspace tasks 和 workspace workflows。

## 验证

已执行并通过：

```bash
node --check web/maze_playground/backend/src/server.js
source /home/gujing/miniconda3/etc/profile.d/conda.sh && conda activate maze && python -m py_compile web/maze_playground/backend/maze_bridge.py
cd web/maze_playground/frontend && ./node_modules/.bin/tsc && ./node_modules/.bin/vite build
```

还做了导入烟测：

- 第一次导入带 `taskDefinitions` 的 workflow，会在 workspace 下写入对应 `.py` 文件。
- 第二次导入同一个 workflow，同路径 task 会跳过。
- 后端返回示例：

```json
{
  "imported": [],
  "skipped": [
    {
      "relativePath": "tasks/import_smoke_task.py",
      "reason": "exists"
    }
  ]
}
```

## 当前服务状态

更新后已重启 Playground backend。

当前监听端口：

```text
8000  Maze API
3001  Playground backend
5173  Playground frontend
```

Windows 浏览器刷新：

```text
http://localhost:5173
```

## 注意事项

- workspace task 导入 workflow 时按文件路径去重，不覆盖用户已有 `.py` 文件。
- workflow 保存和导出会尽量携带 workspace task 的源码，便于迁移。
- 如果导入的是旧版 workflow JSON，且没有 `taskDefinitions`，需要用户自己确保 workspace 下已有对应 task 文件。
- 当前工作区里有一些本轮产生或已有的未提交改动，没有执行任何 destructive git 操作。
