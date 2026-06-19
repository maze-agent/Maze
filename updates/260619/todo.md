# Maze Core Purification Phase 1: Public Boundary Reset

日期：2026-06-19
目录：`/root/data/Maze/updates/260619/`
环境：`conda activate maze`

## 0. 本轮目标

本轮不是大规模重构，也不是一次性物理删除所有历史代码。

本轮目标是恢复 Maze 的 public boundary，让默认安装、默认文档、默认 import、默认 CLI、默认 Workbench 呈现出的 Maze 回到：

```text
Maze = Core Runtime + Workflow Agent + Workflow Workbench
```

Phase 1 的名字：

```text
Maze Core Purification Phase 1: Public Boundary Reset
```

核心判断标准：

```text
Agent proposes.
Core validates.
Scheduler dispatches.
Worker executes.
Workbench observes.
```

任何保留下来的 Agent 能力，都只能生成 Maze-native 的 `WorkflowSpec` / `WorkflowPatch` / `TaskSpec` / `ResourceSpec`，不能直接执行工具、不能绕过 scheduler、不能变成通用聊天 Agent。

## 0.1 当前执行进度快照

截至 2026-06-19，本轮已按小步 commit 完成一批 Phase 1 public boundary reset：

- [x] Python public API 已移除 Skills / MCP / generic Agent / ReAct 主线导出。
- [x] `MaClient.create_agent_run` / `MaClient.create_react_workflow` 已移除。
- [x] legacy ReAct runtime、agent tool registry、agent sandbox、agent permissions 已物理删除。
- [x] `maze/tool/**` tool zoo 与 `maze/sandbox/**` 通用 sandbox service 已物理删除。
- [x] `pyproject.toml` 已移除 `maze-sandbox` console entry 与 `mcp>=1.25.0` 默认依赖。
- [x] Workbench 的 ReAct run modal / launch path 已删除。
- [x] Core `dynamic_store.py` 已移除 ReAct/agent-run 特殊事件推断。
- [x] 已运行 `python -m py_compile $(find maze -name '*.py' -print) web/maze_playground/backend/maze_bridge.py`，通过。
- [x] 已运行 public API smoke，确认 `maze` / `maze.client.maze` / `MaClient` 默认不暴露 `skill` / `mcp` / `react` / `agent` / `tool` 主线符号。
- [ ] frontend build 未验证：当前环境没有 `node` / `npm`。

当前未提交工作区里还有一个正在处理的小步：

- [x] `web/maze_playground/frontend/src/App.tsx` 已从默认 Workbench 挂载中移除 `WorkspaceAgentPanel`。
- [x] `web/maze_playground/frontend/src/components/WorkspaceAgentPanel.tsx` 删除方向已确认：不恢复，不 legacy 化为主线 UI。
- [x] `web/maze_playground/frontend/src/api/client.ts` 已移除 Workspace Agent frontend API/type wrappers。
- [x] `web/maze_playground/frontend/src/index.css` 已移除 `.workspace-agent-*` 专属样式残留。
- [x] README / docs / `docs/maze_boundary.md` 已补充边界修正：删除的是 Workspace Agent，不是 Workflow Agent。
- [x] 已新增 `updates/260619/core_boundary_smoke.py` 覆盖 Core boundary smoke。
- [x] 已运行 `python updates/260619/core_boundary_smoke.py`，通过。
- [x] `919c8ae chore: remove backend mcp public routes` 已删除 `/api/mcp/*` backend public routes。
- [x] `68cd292 chore: remove workspace agent public routes` 已删除 `/api/agent/*` backend public routes。
- [x] `71a95ec docs: add server route boundary` 已新增 `docs/server_route_boundary.md`。
- [x] `updates/260619/core_boundary_smoke.py` 已补充 dynamic append edge（通过 `parents`）和 LLM instance lifecycle mock 覆盖。
- [x] `e34bb84 chore: remove backend mcp legacy helpers` 已删除 backend MCP profile / discovery / validation helper 链。
- [x] `66f2a83 chore: remove workspace agent backend helpers` 已删除 backend Workspace Agent session/draft/run/tool-loop helper 链。
- [x] 最新后端边界扫描确认 `web/maze_playground/backend/src/server.js` 不再包含 `/api/agent` / `/api/mcp` public routes、Workspace Agent helper、MCP helper。
- [x] 最新验证已运行：

```bash
python -m py_compile $(find maze -name '*.py' -print) web/maze_playground/backend/maze_bridge.py updates/260619/core_boundary_smoke.py
python updates/260619/core_boundary_smoke.py
```

结果：通过，`core boundary smoke passed`。

### 0.2 变更账本：todo 原文未逐项点名的改动

下面记录 Phase 1 过程中已经删掉、移动或修改，但最初 todo 没有逐文件列出的内容，方便后续审计。

已提交改动：

- [x] `204086a chore: move worker capability detection to core`
  - [x] 新增 `maze/core/worker/capabilities.py`。
  - [x] 修改 `maze/core/scheduler/resource.py`。
  - [x] 修改 `maze/core/worker/worker.py`。
  - [x] 修改 `maze/client/maze/agent_sandbox.py`，为后续删除 generic agent sandbox 做准备。
  - 说明：这是为了保留 Core worker capability / execution isolation 语义，避免删除 agent sandbox 时误伤 worker 能力检测。

- [x] `343c75d chore: mark legacy agent client methods`
  - [x] 修改 `maze/client/maze/client.py`，先标记 legacy agent client methods。
  - 说明：随后这些方法已在后续 commit 中删除。

- [x] `d7ec6e4 chore: remove bundled skills catalog`
  - [x] 删除整个 `system_catalog/skills/`。
  - 删除内容包括但不限于：`algorithmic-art`、`json-canvas`、`mcp-builder`、`obsidian-markdown`、`slack-gif-creator`、`systematic-debugging`、`test-driven-development`、`webapp-testing`。

- [x] `30ebb23 chore: remove legacy skills runtime`
  - [x] 删除 `maze/client/maze/skills.py`。
  - [x] 删除 `maze/client/maze/agent_skills.py`。
  - [x] 修改 `maze/client/maze/__init__.py`。
  - [x] 修改 `maze/client/maze/agent.py`。
  - [x] 修改 `maze/client/maze/agent_tools.py`。
  - [x] 修改 `maze/client/maze/client.py`。
  - [x] 修改 `maze/client/maze/react.py`。
  - [x] 修改 `web/maze_playground/backend/maze_bridge.py`。

- [x] `b10f9c5 chore: remove legacy mcp runtime`
  - [x] 删除 `maze/mcp/__init__.py`。
  - [x] 删除 `maze/mcp/base_client.py`。
  - [x] 删除 `maze/mcp/http_client.py`。
  - [x] 删除 `maze/mcp/mcp_tool_wrapper.py`。
  - [x] 删除 `maze/mcp/stdio_client.py`。
  - [x] 删除 `maze/client/maze/agent_mcp.py`。
  - [x] 删除旧 `maze/agent/**` package：memory/model/react_agent/tool 等。
  - [x] 修改 `maze/client/maze/__init__.py`。
  - [x] 修改 `maze/client/maze/agent.py`。
  - [x] 修改 `maze/client/maze/agent_tools.py`。
  - [x] 修改 `maze/client/maze/client.py`。
  - [x] 修改 `maze/client/maze/react.py`。
  - [x] 修改 `web/maze_playground/backend/maze_bridge.py`。

- [x] `41147ea chore: remove legacy skills and mcp leftovers`
  - [x] 修改 `maze/client/maze/agent_permissions.py`。
  - [x] 修改 `maze/client/maze/agent_tools.py`。
  - [x] 修改 `maze/client/maze/react_llm.py`。
  - [x] 修改 `web/maze_playground/backend/maze_bridge.py`。

- [x] `55ff309 chore: remove legacy react agent runtime`
  - [x] 删除 `maze/client/maze/agent.py`。
  - [x] 删除 `maze/client/maze/agent_tools.py`。
  - [x] 删除 `maze/client/maze/react.py`。
  - [x] 删除 `maze/client/maze/react_llm.py`。
  - [x] 修改 `maze/client/maze/__init__.py`。
  - [x] 修改 `maze/client/maze/client.py`。
  - [x] 修改 `web/maze_playground/backend/maze_bridge.py`。

- [x] `123ad8a chore: drop react mode inference from dynamic runs`
  - [x] 修改 `maze/core/workflow/dynamic_store.py`，移除 `react_llm_decision` / `agent_run_started` 特殊推断。

- [x] `f45f7bc chore: remove legacy workspace agent tools`
  - [x] 删除 `maze/client/front/builtin/agentTools.py`。
  - [x] 删除 `maze/client/maze/agent_exec.py`。
  - [x] 删除 `maze/client/maze/agent_permissions.py`。
  - [x] 删除 `maze/client/maze/agent_sandbox.py`。
  - [x] 修改 `maze/client/front/builtin/__init__.py`。
  - [x] 修改 `maze/client/maze/__init__.py`。
  - [x] 修改 `web/maze_playground/backend/maze_bridge.py`。

- [x] `f1fb8f3 chore: remove legacy agent run filters`
  - [x] 修改 `maze/core/path/path.py`，移除 `kind=react/agent` 运行过滤语义。
  - [x] 修改 `web/maze_playground/backend/maze_bridge.py`，移除 MCP/skill 默认 policy 项。

- [x] `8d1420e chore: remove tool zoo and sandbox service`
  - [x] 删除 `maze/tool/__init__.py`。
  - [x] 删除 `maze/tool/calculator.py`、`weather.py`、`search.py`、`http_request.py`、`email_sender.py`。
  - [x] 删除 `maze/tool/pdf_reader.py`、`xlsx_reader.py`、`doc_reader.py`、`csv_reader.py`、`text_reader.py`。
  - [x] 删除 `maze/tool/video_reader.py`、`mp3_reader.py`、`figure_reader.py`。
  - [x] 删除 `maze/tool/file_manager.py`、`file_writer.py`、`json_parser.py`、`hash_generator.py`、`string_processor.py`、`system_info.py`、`date_time.py`。
  - [x] 删除 `maze/sandbox/__init__.py`、`client.py`、`code_sandbox.py`、`launcher.py`、`server.py`。
  - [x] 删除 `maze/cli/sandbox_cli.py`。
  - [x] 修改 `pyproject.toml`，移除 `maze-sandbox` console entry 与 `mcp>=1.25.0` dependency。
  - [x] 修改 `docs/index.md`，把主线描述收敛回 runtime / workbench。

- [x] `0bce346 chore: remove react workbench launch path`
  - [x] 删除 `web/maze_playground/frontend/src/components/ReActRunModal.tsx`。
  - [x] 修改 `web/maze_playground/frontend/src/api/client.ts`，删除 `startReactRun` API。
  - [x] 修改 `web/maze_playground/frontend/src/App.tsx`，删除 ReAct modal state / launch wiring。
  - [x] 修改 `web/maze_playground/frontend/src/components/Toolbar.tsx`，删除 ReAct launch button path。
  - [x] 修改 `web/maze_playground/backend/src/server.js`，删除 `/api/react-runs/start` 与对应 process launcher。
  - [x] 修改 `web/maze_playground/frontend/src/components/RunsInspector.tsx` 的 ReAct 相关入口/展示路径。

当前未提交改动，需与用户改动区分，不要盲目 stage：

- [ ] `.gitignore` 增加 `.codegraph/`。这是 CodeGraph 索引目录保护，不属于 Maze runtime 清理本身。
- [ ] `docs/requirements.txt` pin 了 mkdocs 相关依赖：`mkdocs==1.6.1`、`mkdocs-material==9.5.49`、`pymdown-extensions==10.16.1`、`Pygments==2.18.0`。这不是 Phase 1 core cleanup。
- [x] `web/maze_playground/backend/src/server.js` 已删除 Workspace Agent live activity / session / draft / run / tool-loop helper 链，并在 `66f2a83` 提交。
- [x] `web/maze_playground/backend/src/server.js` 已删除 MCP profile / discovery / validation helper 链，并在 `e34bb84` 提交。
- [x] `web/maze_playground/frontend/src/App.tsx` 当前已删除 `WorkspaceAgentPanel` 默认挂载、`VITE_ENABLE_LEGACY_AGENT_UI` gate、相关 focus state。
- [x] `web/maze_playground/frontend/src/components/WorkspaceAgentPanel.tsx` 当前工作区为删除状态，删除了旧 workspace chat / draft / tool-call 面板实现。
- [x] `web/maze_playground/frontend/src/api/client.ts` 当前已删除 Workspace Agent sessions/runs/drafts frontend wrappers。
- [x] `web/maze_playground/frontend/src/index.css` 当前已删除 Workspace Agent 面板、消息、live activity、draft、input 专属样式。
- [x] `updates/260619/core_boundary_smoke.py` 新增为本轮 Core smoke。

当前未跟踪内容，也不是 Phase 1 cleanup 已提交结果：

- [ ] `docs/api_zh.md`
- [ ] `docs/frontend_platform_api_zh.md`
- [ ] `docs/assets/`
- [ ] `docs/papers/`
- [ ] `updates/`

重要边界判断：

```text
Workspace Agent 是 Workflow Agent 的前身，但不是 Phase 1 主线形态。
```

后续应保留/重建的是窄版 `Workflow Agent / Workflow Planner`：

- 只生成 `WorkflowSpec` / `TaskSpec` / `WorkflowPatch`。
- 只修复 validation error。
- 只建议 dependency / resource / artifact / retry / timeout。
- 不直接执行工具。
- 不调用 MCP。
- 不加载 skills。
- 不做 workspace chat。
- 不绕过 Core validate / submit / append / scheduler。

因此 Phase 1 的处理原则是：

```text
移除 Workspace Agent 的默认主线入口。
保留 Workflow Agent 这个产品方向。
从旧 Workspace Agent 中只回收 workflow authoring / draft / validation 的必要经验。
```

## 1. 边界定义

### 1.1 Maze Core Runtime

Core Runtime 负责“跑”：

- 静态 DAG / 动态 DAG 的表达、校验、调度和执行。
- task-level scheduling。
- resource-aware placement。
- Worker 执行、失败重试、取消、日志、artifact、run state。
- 动态 workflow：运行过程中基于中间结果 append task / append sub-DAG / append edge。
- 动态扩展必须通过 `WorkflowPatch` 或类似结构校验后进入 scheduler。
- 本地 LLM / 本地推理引擎部署，例如 vLLM。
- 推理实例扩缩容、缩容、evict、资源回收等 runtime 能力。
- cluster resources、queue diagnostics、worker health、task placement 观测。

### 1.2 Maze Workflow Agent

Workflow Agent 负责“写”：

- 生成 Task / TaskSpec。
- 生成静态 `WorkflowSpec` / DAG。
- 基于运行结果生成 `WorkflowPatch`，用于动态 workflow 扩展。
- 根据 validation error 修复 `WorkflowSpec` / `WorkflowPatch`。
- 建议 task/resource/dependency/artifact/retry/timeout 配置。

Workflow Agent 不允许：

- 直接执行工具。
- 直接调用 MCP。
- 加载 skills。
- 做通用聊天。
- 做长期记忆。
- 绕过 scheduler。
- 自己维护开放式 agent loop。

### 1.3 Maze Workflow Workbench

Workflow Workbench 负责“看和改”：

- DAG 可视化。
- 人工拖拽编辑 DAG。
- `WorkflowSpec` / `WorkflowPatch` validate。
- submit / retry / cancel。
- task placement。
- worker/node 状态。
- CPU/GPU/I/O 资源视图。
- queue reason。
- run timeline。
- logs。
- artifacts。
- runtime-expanded dynamic DAG 展示。

Workbench 不应该继续朝通用 Agent Playground、Skills Playground、MCP Playground、workspace chat 或 Manus/Codex 风格 workspace assistant 发展。

### 1.4 本轮未完成项为什么还保留

todo 中仍有未完成项，不是因为忘记，而是出于 Phase 1 的小步边界和验证原则：

- backend `server.js` 的 `/api/mcp/*` 和 `/api/agent/*` public routes 已删除；MCP profile/discovery helper 与 Workspace Agent session/draft/run/tool-loop helper 已完成物理删除。
- `docs/api_zh.md` / `docs/frontend_platform_api_zh.md` 当前是未跟踪长文档，里面可能仍有历史 Skills/ReAct/Playground API 描述；未纳入本轮小步提交。
- `RunsInspector.tsx`、`WorkflowCanvas.tsx`、`BuiltinTasksSidebar.tsx`、`NodePanel.tsx`、`CustomNode.tsx`、`types/workflow.ts` 已在前序小步中清理过非主线入口，但后续仍应继续用 grep/build 复核，避免误伤 DAG editor / run console / cluster console。
- frontend build 未验证是因为当前环境没有 `node` / `npm`。
- 新 Workflow Planner UI 不属于 Phase 1 必做项；Phase 1 只要求保留 Workflow Agent / Workflow Planner 作为主线概念并明确边界。
- 因此，todo 中未勾选的项表示“仍需后续小步处理”，不是遗漏；尤其是未跟踪 docs、examples/tests、以及前端进程正在处理的 Workbench 残留。

## 2. 本轮总原则

- [x] 优先从 README / docs / public API / CLI / Workbench 主入口中移除非主线概念。
- [x] 如果某些非主线模块仍被 Core Runtime 间接依赖，Phase 1 不强删，先移动到 legacy 或 extension，并从 public API 中断开。
- [ ] 默认安装、默认 import、默认文档、默认 UI 只能呈现 Core Runtime + Workflow Agent + Workflow Workbench。
- [x] 所有 legacy/extension 模块默认不得被 README、`maze.__init__`、`maze.client.maze.__init__`、CLI help、Workbench 首屏引用。
- [ ] 如果 legacy 功能需要临时保留，必须默认关闭，并考虑通过显式 flag 开启，例如 `MAZE_ENABLE_LEGACY_AGENT=1`。
- [x] 不误删 Core Runtime 所需的 task execution isolation、timeout、logs、artifact capture、worker execution 相关能力。
- [x] 不新增复杂功能，不重写 scheduler、worker、dynamic workflow。

## 3. 执行前基线

在动代码前先记录当前状态，避免清理后分不清哪些问题本来就存在。

- [x] 记录 git 状态。

```bash
git status --short
```

- [x] 记录最近提交。

```bash
git log --oneline -8
```

- [x] 记录当前 public API。

```bash
python - <<'PY'
import maze
print("maze exports:", sorted([x for x in dir(maze) if not x.startswith("_")]))
from maze.client import maze as client_maze
print("maze.client.maze exports:", sorted([x for x in dir(client_maze) if not x.startswith("_")]))
PY
```

- [x] 运行当前已有测试或 smoke，并记录哪些失败是 baseline。

```bash
python -m pytest -q
```

当前已运行的最小 smoke：

```bash
python -m py_compile $(find maze -name '*.py' -print)
```

补充记录：

```bash
python -m py_compile $(find maze -name '*.py' -print) web/maze_playground/backend/maze_bridge.py
```

结果：通过。

## 4. 第一步：仓库扫描清单

目标：找出非主线能力的文件、入口、引用关系，先出清单，再动手。

### 4.1 Skills

- [x] 扫描 skills 文件。

```bash
find . -path '*skill*' -o -path '*skills*'
```

- [ ] 重点检查：
  - [x] `system_catalog/skills/`
  - [x] `maze/client/maze/skills.py`
  - [x] `maze/client/maze/agent_skills.py`
  - [ ] README 中的 skills 段落。
  - [ ] docs/examples/tests 中的 skills 内容。
  - [x] ReAct progressive skills 相关参数和注入逻辑。

### 4.2 MCP

- [x] 扫描 MCP 文件。

```bash
find . -iname '*mcp*' -print
```

- [ ] 重点检查：
  - [x] `maze/mcp/`
  - [x] `maze/client/maze/agent_mcp.py`
  - [x] MCP-enabled ReAct run。
  - [ ] Playground MCP 配置入口。
  - [ ] MCP profile。
  - [ ] MCP discovery/test API。
  - [ ] README/docs/examples/tests 中 MCP 内容。

### 4.3 Generic Agent / ReAct

- [x] 扫描 Agent/ReAct 文件。

```bash
find . \( -iname '*agent*' -o -iname '*react*' \) -print
```

- [ ] 重点检查：
  - [x] `maze/client/maze/agent.py`
  - [x] `maze/client/maze/react.py`
  - [x] `maze/client/maze/react_llm.py`
  - [x] `maze/client/maze/agent_tools.py`
  - [x] `maze/client/maze/agent_exec.py`
  - [x] `maze/client/maze/agent_permissions.py`
  - [x] `maze/client/maze/agent_sandbox.py`
  - [x] `maze/agent/`
  - [ ] Playground Workspace Agent UI/API。

### 4.4 Tool zoo

- [x] 扫描工具集合。

```bash
find maze/tool maze/client/front/builtin maze/client/maze/builtin -maxdepth 3 -type f 2>/dev/null
```

- [ ] 重点检查是否仍在 README/docs/public API 中作为主线能力出现：
  - [x] weather
  - [x] email
  - [x] search
  - [x] pdf_reader
  - [x] xlsx_reader
  - [x] video_reader
  - [x] mp3_reader
  - [x] http_request
  - [x] calculator

### 4.5 Workbench / Playground

- [x] 扫描 Workbench 主入口和非主线 UI。

```bash
find web/maze_playground -maxdepth 5 -type f | sort
```

- [x] 保留 workflow/DAG/runtime observability。
- [ ] 隐藏或移除 Skills/MCP/ReAct/workspace chat/general workspace assistant。

## 5. 第二步：README / docs 收敛

目标：用户打开 README 和 docs 时，只看到 Maze 是分布式智能体 workflow runtime。

### 5.1 README

- [x] 将 README 首页定位改成：

```text
Maze is a distributed workflow runtime for LLM agent applications.
Maze = Core Runtime + Workflow Agent + Workflow Workbench.
```

- [x] 主线介绍只保留：
  - [x] static DAG。
  - [x] dynamic append-only DAG expansion。
  - [x] resource-aware placement。
  - [x] worker execution。
  - [x] run/task state。
  - [x] logs/artifacts。
  - [x] cluster resources。
  - [x] queue diagnostics。
  - [x] local LLM / inference engine lifecycle。
  - [x] WorkflowSpec / WorkflowPatch。
  - [x] Workbench DAG / placement / resource observability。

- [x] 从主线移除：
  - [x] Skills。
  - [x] MCP。
  - [x] ReAct application host。
  - [x] generic Agent SDK。
  - [x] tool zoo。
  - [x] workspace chat。
  - [x] Manus/Codex 风格 workspace assistant。
  - [x] general-purpose agent playground。

- [x] 如果暂时保留历史说明，放到 `Legacy / Extensions / Examples`，并说明默认不属于 Maze 主线。

### 5.2 docs

- [x] 新增或更新 `docs/maze_boundary.md`。
- [x] 将 `/root/data/Maze/updates/boundary/maze_bounndary.md` 中的边界定义整理进 docs。
- [x] docs 首页改成 Core Runtime + Workflow Agent + Workflow Workbench。
- [ ] server routes 文档只突出 core APIs：
  - [ ] workflow validate/submit/run。
  - [ ] dynamic runs / append。
  - [ ] runs/tasks/events/logs/artifacts。
  - [ ] cluster resources / queues / workers。
  - [ ] LLM instance lifecycle。
- [x] 将 skills/MCP/generic agent 文档移到 legacy/extension 区，或删除。

## 6. 第三步：Public API 断开

目标：默认 import 不再暴露非主线能力。

### 6.1 `maze/__init__.py`

- [x] 移除 skill 相关 export。
- [x] 移除 MCP 相关 export。
- [x] 移除 generic Agent/ReAct export。
- [x] 只保留 Core/Workflow 主线 API，例如：
  - [x] `MaClient`
  - [x] `task`
  - [x] workflow authoring helper
  - [x] `WorkflowSpec` / `TaskSpec` / validation 相关内容，如果已有。

### 6.2 `maze/client/maze/__init__.py`

- [x] 移除：
  - [x] `load_skill`
  - [x] skills registry/export。
  - [x] MCP manager/export。
  - [x] `ReActWorkflow`
  - [x] `ReActStep`
  - [x] `AgentRun`
  - [x] `AgentContext`
  - [x] `AgentToolRegistry`
  - [x] `AgentPermissionPolicy`
  - [x] workspace/generic agent sandbox export。

- [x] 如暂时需要保留模块文件，不要在 `__init__` 中导出。

### 6.3 `MaClient`

- [x] 检查是否仍公开主线外方法：
  - [x] `create_agent_run`
  - [x] `create_react_workflow`
  - [x] MCP/skills 参数。

- [ ] Phase 1 可选做法：
  - [x] 从 README/docs 不再展示这些方法。
  - [x] 若兼容性需要，保留方法但标记 legacy，并默认不作为主线 API。
  - [ ] 后续 Phase 2 再拆出 extension client。

当前实际处理：`create_agent_run` / `create_react_workflow` 已删除，没有继续保留兼容方法。

### 6.4 CLI

- [x] 检查 `maze --help`。
- [x] CLI 主线只保留：
  - [x] `start`
  - [x] `stop`
  - [x] `cluster`
  - [x] `runs`
  - [x] `artifacts`
  - [x] `run`
  - [x] `app`
  - [x] `status`
  - [x] workflow validate/submit 相关命令，如果已有。
- [x] 移除或隐藏 skills/MCP/generic agent CLI。

当前实际处理：`maze-sandbox` console entry 已删除。

### 6.5 Server routes

- [x] 不优先删除 route，先检查是否被 Workbench/Core 使用。
- [x] 主线文档不再公开 MCP/skills/generic agent routes。
- [x] 已删除 `/api/mcp/*` 和 `/api/agent/*` public routes；如后续恢复 legacy route，必须使用 extension/legacy prefix 或 feature flag。

当前状态：`web/maze_playground/backend/src/server.js` 已删除 `/api/mcp/*`、`/api/agent/*` public routes，以及 MCP profile/discovery helper、Workspace Agent session/draft/run/tool-loop helper；后端 server 主线只保留 Workbench workflow/run/artifact/cluster 相关 API。

## 7. 第四步：Skills 激进清理

Skills 最偏离 Maze 主线，Phase 1 可以激进处理。

- [x] 删除或 legacy 化 `system_catalog/skills/`。
- [x] 删除或 legacy 化 `maze/client/maze/skills.py`。
- [x] 删除或 legacy 化 `maze/client/maze/agent_skills.py`。
- [x] 移除 progressive skills 参数。
- [x] 移除 skill prompt injection。
- [x] 移除 README 中 skills 主线描述。
- [ ] 删除或改写 skills docs/examples/tests。
- [ ] 如某个 skill 示例本质是 workflow 示例，改造成 `examples/workflows/`，不继续保留 skills 概念。
- [x] 确认默认 package/public API 不再出现 skills。

验收：

```bash
python - <<'PY'
import maze
print([x for x in dir(maze) if "skill" in x.lower()])
PY
```

输出应为空或仅有明确 legacy/internal 标记。

## 8. 第五步：MCP 激进清理

MCP 也偏离主线，Phase 1 优先从默认包和主线文档中移除。

- [x] 删除或 legacy 化 `maze/mcp/`。
- [x] 删除或 legacy 化 `maze/client/maze/agent_mcp.py`。
- [x] 移除 MCP-enabled ReAct run 的 public path。
- [x] 移除 Workbench/Playground 中 MCP 配置入口。
- [x] 移除 MCP profile UI/API 主入口。
- [x] 移除 MCP discovery/test API 的主线文档。
- [x] 移除 README 中 MCP 主线描述。
- [ ] 删除或改写 MCP docs/examples/tests。
- [ ] 如果暂时不能物理删除，移动到 `legacy/mcp` 或 `examples/extensions/mcp`。
- [x] 确保 Core Runtime 不依赖 MCP。

验收：

```bash
python - <<'PY'
import maze
print([x for x in dir(maze) if "mcp" in x.lower()])
PY
```

输出应为空或仅有明确 legacy/internal 标记。

## 9. 第六步：Generic Agent / ReAct 分阶段处理

Phase 1 不强行全部物理删除，因为这些模块可能和 DynamicRun、tool harness、artifact、sandbox 有耦合。

### 9.1 Phase 1 必须做到

- [x] 从 public API 移除。
- [x] 从 README 主线移除。
- [x] 从 CLI 主入口隐藏。
- [ ] 从 Workbench 主入口隐藏。
- [ ] 标记为 legacy 或 extension。
- [x] 不允许默认作为 Maze 主线概念出现。

当前实际处理：

- [x] 已删除 `maze/client/maze/agent.py`。
- [x] 已删除 `maze/client/maze/react.py`。
- [x] 已删除 `maze/client/maze/react_llm.py`。
- [x] 已删除 `maze/client/maze/agent_tools.py`。
- [x] 已删除 `maze/client/maze/agent_exec.py`。
- [x] 已删除 `maze/client/maze/agent_permissions.py`。
- [x] 已删除 `maze/client/maze/agent_sandbox.py`。
- [x] 已删除 Workbench `ReActRunModal.tsx` 和 `/api/react-runs/start` launch path。
- [x] Workbench `WorkspaceAgentPanel` 删除已确认：它属于 general-purpose workspace assistant，不恢复。
- [x] backend `server.js` 已删除 workspace agent session/draft/run public routes。
- [x] Workbench 默认入口已删除 Skills/MCP/ReAct/Workspace Agent 可见主线入口；后续仍需 build 验证。

### 9.2 Legacy 标记

- [x] backend / Python 默认包中已不再保留 generic Agent/ReAct/MCP/Skills 主线模块，因此无需给这些已删除模块添加 legacy 文件头。
- [ ] 如后续发现 docs/examples/tests 中仍保留历史 Agent/MCP/Skills 内容，移动到 extension/legacy 区时再添加 legacy 标记：

```text
Legacy application-level agent code.
Not part of Maze Core Runtime public boundary.
To be moved to extensions or removed in a later phase.
```

### 9.3 Workflow Agent 收敛方向

后续保留的最小 Workflow Agent 接口应命名为：

```text
generate_workflow_spec()
generate_workflow_patch()
repair_workflow_spec()
repair_workflow_patch()
suggest_task_resources()
```

避免继续使用容易滑向通用 Agent 的主线命名：

```text
chat()
run_agent()
run_react()
execute_tool()
load_skill()
call_mcp()
```

当前补充说明：

```text
Workspace Agent = 旧的通用工作区助手形态。
Workflow Agent = 后续要保留/重建的窄版 workflow authoring/planning 能力。
```

Phase 1 可以删除或隐藏 Workspace Agent 主线入口；不要把 Workflow Agent 方向一起删掉。

本轮边界修正：

- [x] 删除的是 `WorkspaceAgentPanel`，不是 Workflow Agent。
- [x] Workflow Agent / Workflow Planner 仍是 Maze 主线概念。
- [x] Workflow Agent 只能生成 `WorkflowSpec` / `WorkflowPatch` / `TaskSpec` / `ResourceSpec`。
- [x] Workflow Agent 不能执行工具、不能调 MCP、不能加载 skills、不能做 workspace chat。
- [x] Workbench 未来可以新增 `WorkflowPlannerPanel`，但不能恢复旧 `WorkspaceAgentPanel`。

## 10. 第七步：Tool zoo 分阶段处理

Phase 1 不直接全部删除，以免引发大量 import/test 修复。

- [x] 从默认 public API 移除。
- [x] 从 README 主线移除。
- [x] 标记为 legacy 或移动到 `examples/tools` / `legacy/tools`。
- [ ] 保留少量 Maze-native 示例，用于展示用户如何把自己的工具封装成 task：
  - [ ] CPU task example。
  - [ ] GPU task example。
  - [ ] I/O task example。
  - [ ] LLM task example。
- [x] 主线不再内置或宣传 weather/email/search/pdf/xlsx/video/mp3/http_request 等工具市场能力。

当前实际处理：`maze/tool/**` 已物理删除，未继续保留在默认 package。

## 11. 第八步：Sandbox 语义区分

要删除或 legacy 化的是：

- [x] Workspace Agent sandbox。
- [x] generic code execution agent sandbox。
- [x] 通用 Agent 自主执行代码的 sandbox。

不能误删的是 Core Runtime 需要的：

- [x] task execution isolation。
- [x] timeout。
- [x] stdout/stderr logs。
- [x] artifact capture。
- [x] worker-side execution control。
- [x] resource-aware execution。

验收问题：

```text
这个 sandbox 是为了 Core 调度后的 task 执行，还是为了通用 Agent 自己执行代码？
```

前者保留，后者 legacy/移除。

## 12. 第九步：Workbench / Playground 收敛

### 12.1 保留能力

- [x] DAG 可视化。
- [x] 人工拖拽编辑 DAG。
- [x] WorkflowSpec / WorkflowPatch validate。
- [x] submit / retry / cancel。
- [x] task placement。
- [x] worker/node 状态。
- [x] CPU/GPU/I/O 资源视图。
- [x] queue reason。
- [x] run timeline。
- [x] logs。
- [x] artifacts。
- [x] runtime-expanded dynamic DAG 展示。

### 12.2 移除或隐藏

- [x] Skills Playground。
- [x] MCP Playground。
- [x] generic ReAct Playground。
- [x] workspace chat sessions。
- [x] general-purpose workspace assistant。
- [x] permission decision UI for generic agent tools。
- [x] generic workspace file assistant。

当前实际处理：

- [x] ReAct launch path 已删除。
- [x] Backend `/api/mcp/*`、`/api/agent/*`、`/api/workspace-skills/*` public routes 已删除。
- [x] Backend MCP profile/discovery helper 和 Workspace Agent helper 已删除。
- [x] `App.tsx` 已从默认挂载中移除 `WorkspaceAgentPanel`。
- [x] `WorkspaceAgentPanel.tsx` 删除已确认：属于 general-purpose workspace assistant，不恢复。
- [x] `api/client.ts` 已删除 Workspace Agent frontend API/type wrappers。
- [x] `index.css` 已删除 Workspace Agent 专属样式。
- [x] `RunsInspector.tsx` / `WorkflowCanvas.tsx` / `BuiltinTasksSidebar.tsx` / `NodePanel.tsx` / `CustomNode.tsx` / `types/workflow.ts` 已完成一轮非主线入口清理。
- [ ] frontend build 未验证，原因是当前环境缺 `node` / `npm`。

### 12.3 命名收敛

- [x] 将 Playground 主线定位改为 `Workflow Workbench` 或 `Maze Console`。
- [x] 不再使用 `Agent Playground` 作为主线命名。
- [x] 如果 UI 中还有 `Workspace Agent`，Phase 1 至少隐藏入口或标记 legacy。

当前检查：`grep -RIn "WorkspaceAgent\|runWorkspaceAgent\|listAgentSessions\|getAgentDraft\|workspace-agent" web/maze_playground/frontend/src` 无结果。

边界说明：

- [x] 删除的是 Workspace Agent / `WorkspaceAgentPanel`，不是 Workflow Agent。
- [x] Workflow Agent / Workflow Planner 仍是 Maze 主线概念。
- [x] Workflow Agent 只能生成 `WorkflowSpec` / `WorkflowPatch` / `TaskSpec` / `ResourceSpec`。
- [x] Workflow Agent 不能执行工具、不能调 MCP、不能加载 skills、不能做 workspace chat。
- [x] Workbench 未来可以有 `WorkflowPlannerPanel`，但不能恢复旧 `WorkspaceAgentPanel`。
- [x] 当前 Workbench 至少保留 DAG editor、run console、cluster/resource/queue/task placement 可视化。

## 13. 第十步：Core smoke tests

删除非主线测试时，不能让 Core 测试变薄。

至少保证以下 smoke tests 存在并通过：

- [x] static workflow validate / submit / run。
- [x] dynamic run append task。
- [x] dynamic run append edge：当前 Core 通过 `append_dynamic_task(..., parents=[...])` 表达 runtime edge，已在 smoke 中覆盖。
- [x] task resource annotation。
- [x] cluster resources endpoint。
- [x] cluster queues endpoint。
- [x] worker registration / heartbeat / basic execution。
- [x] run state / task state / events。
- [x] logs / artifact capture。
- [x] local LLM / inference instance lifecycle：已用 mock actor 覆盖 `LlmInstanceManager.start_llm_instance()` / `stop_llm_instance()` 状态记录与释放。

当前已完成的最小验证：

- [x] Python compile smoke。
- [x] public API smoke。
- [ ] frontend build：未运行，环境缺少 `node` / `npm`。
- [x] Core workflow smoke：`python updates/260619/core_boundary_smoke.py` 通过。

已运行命令：

```bash
python -m py_compile $(find maze -name '*.py' -print) web/maze_playground/backend/maze_bridge.py updates/260619/core_boundary_smoke.py
python updates/260619/core_boundary_smoke.py
command -v node || true
command -v npm || true
grep -RIn "from maze\.sandbox\|import maze\.sandbox\|maze\.sandbox\|from maze\.tool\|import maze\.tool\|from maze\.mcp\|import maze\.mcp\|agent_sandbox\|WorkspaceSandbox" maze web/maze_playground/backend/maze_bridge.py 2>/dev/null || true
```

结果：

- Python compile：通过。
- Core boundary smoke：通过，输出 `core boundary smoke passed`。
- frontend build：未验证，`node` / `npm` 在当前环境中不可用。
- 删除 `maze/sandbox` 后的 import 检查：通过，Core/backend bridge 未发现 `maze.sandbox` / `maze.tool` / `maze.mcp` / `agent_sandbox` / `WorkspaceSandbox` 残留 import。

Core smoke 覆盖：

- static DAG spec validate / build / run snapshot。
- dynamic run append task / append edge / event / scheduler message。
- task resource annotation 和 timeout annotation。
- cluster resources / queues message surface。
- worker registration message surface。
- worker capability detection。
- run/task state snapshot。
- logs / artifact capture via worker file context manifest。
- local LLM / inference instance lifecycle mock。

删除 `maze/sandbox` 后的 worker-side 能力检查：

- timeout 仍在：`maze/core/scheduler/runtime.py` 的 `TaskRuntime.has_timed_out()`，`maze/core/scheduler/scheduler.py` 的 `_fail_timed_out_tasks()`。
- task execution isolation 仍在：`maze/core/files/lineage.py` 的 `run_task_with_file_context()` 会创建 per-run/per-task work dir，并设置 `MAZE_WORK_DIR` / `MAZE_INPUT_DIR` / `MAZE_OUTPUT_DIR` / `MAZE_RUN_ID` / `MAZE_TASK_ID`。
- artifact capture 仍在：`run_task_with_file_context()` 生成 `file_manifest`，`maze/core/path/path.py` 的 `get_run_artifacts()` / `get_run_task_artifacts()` 仍读取 manifest。
- logs 仍在：`maze/core/path/path.py` 的 `get_run_logs()` 从 `logs/maze-command*` artifact 读取 stdout/stderr。
- worker capability 仍在：`maze/core/worker/capabilities.py` 和 worker registration capabilities 保留 `workspace_sandbox` / `docker_sandbox` 状态。

建议新增或保留脚本：

```text
updates/260619/core_static_workflow_smoke.py
updates/260619/core_dynamic_append_smoke.py
updates/260619/core_cluster_endpoints_smoke.py
updates/260619/core_worker_execution_smoke.py
updates/260619/core_artifact_smoke.py
```

测试命令：

```bash
python -m pytest -q
python -m py_compile $(find maze -name '*.py' -print)
```

如 pytest 因 legacy 删除失败：

- [ ] 删除非主线 tests。
- [ ] 或将其移动到 legacy/extension tests。
- [ ] 不要为了保留 skills/MCP/generic agent 测试而继续暴露主线 API。

## 14. 禁止事项

- [x] 不重写 scheduler。
- [x] 不重写 worker。
- [x] 不重写 dynamic workflow。
- [x] 不引入新的 Agent framework。
- [x] 不新增复杂功能。
- [x] 不为了删除非主线代码而破坏 Core Runtime。
- [x] 不把 Workflow Agent 做成通用 Agent。
- [x] 不让 Workbench 继续朝通用 Agent Playground 发展。
- [x] 不删除 task execution isolation / logs / artifacts / timeout / worker execution 控制。

## 15. Phase 1 验收标准

完成后应满足：

- [x] README 首页只表达 Core Runtime + Workflow Agent + Workflow Workbench。
- [x] docs 中有清晰的 Maze boundary 文档。
- [x] `import maze` 不再暴露 skills/MCP/generic Agent/ReAct 主线符号。
- [x] `maze.client.maze` 不再暴露 skills/MCP/generic Agent/ReAct 主线符号。
- [x] CLI help 不再宣传 skills/MCP/generic Agent。
- [x] Workbench 默认入口已收敛：workspace chat / `WorkspaceAgentPanel` / ReAct launch / Skills/MCP/ReAct 可见主线入口已删除。
- [x] Skills 已删除或彻底 legacy 化。
- [x] MCP 已从默认 public boundary 删除；backend public routes、默认依赖、MCP profile/discovery helper 已移除。
- [x] Generic Agent/ReAct 已从主线隐藏。
- [x] Tool zoo 已从 README/public API 主线移除。
- [x] Core smoke tests 通过。
- [x] Core Runtime 能力未被破坏。

## 16. 完成后输出格式

执行完成后需要汇报：

### 16.1 Phase 1 修改摘要

简述本轮完成了哪些边界重置。

### 16.2 删除/移动/legacy 化的文件列表

按类别列出：

- Skills。
- MCP。
- Generic Agent / ReAct。
- Tool zoo。
- Workbench 非主线入口。
- Docs/README。

### 16.3 从 public API 移除的符号列表

包括：

- `maze.__init__`。
- `maze.client.maze.__init__`。
- `MaClient` 主线文档。
- CLI。

### 16.4 README/docs 的边界变化

说明 README/docs 如何改成：

```text
Core Runtime + Workflow Agent + Workflow Workbench
```

### 16.5 仍保留但已标记 legacy/extension 的模块

列出暂时无法物理删除的历史模块，并说明为什么 Phase 1 先保留。

### 16.6 Core Runtime 受影响情况

确认以下能力是否保持：

- static workflow。
- dynamic workflow。
- scheduler。
- resource manager。
- worker。
- run/task state。
- events/logs/artifacts。
- cluster resources/queues。
- LLM instance lifecycle。

### 16.7 Core smoke tests 运行结果

列出执行命令和结果。

### 16.8 下一阶段建议

重点说明：

- Generic Agent / ReAct 如何彻底拆分。
- Tool zoo 如何彻底移出默认包。
- legacy modules 何时物理删除。
- Workflow Agent 如何收敛成只输出 `WorkflowSpec` / `WorkflowPatch`。
- MCP/Skills 是否应独立为 external extension repo。

## 17. 下一阶段候选计划

Phase 2 可以考虑：

- [ ] 将 generic Agent/ReAct 移到 `extensions/agent_legacy` 或独立 repo。
- [ ] 将 MCP 移到 `extensions/mcp` 或独立 repo。
- [ ] 将 skills 移到独立 examples repo，或完全删除。
- [ ] 将 tool zoo 移到 `examples/tools`，不进入默认 package。
- [ ] 将 Workbench 里的 Workspace Agent 改造成 Workflow Planner，只输出 `WorkflowSpec` / `WorkflowPatch`。
- [ ] 引入明确的 `WorkflowPatch` schema 和 validate/append API。
- [ ] 将 DynamicRun append 行为对齐 append-only DAG expansion。
- [ ] 加强 HACS/MaLearn/MaPath 与默认 scheduler 策略的主线表达。
- [ ] 加强 LLM inference engine lifecycle：scale-in/out、draining、GPU memory reclaim、queue observability。
