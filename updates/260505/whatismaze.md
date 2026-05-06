# Maze 是什么

整理时间：2026-05-06  
阅读范围：`README.md`、`pyproject.toml`、`maze/`、`examples/`、`web/`、`test/`

## 一句话概括

Maze 是一个面向 LLM Agent / 多任务 AI 工作流的分布式运行框架。它把一个 Agent 流程拆成多个带输入、输出和资源需求的任务节点，用 DAG 表示依赖关系，再通过 FastAPI + Scheduler + Ray 把任务调度到本机或多机 worker 上执行，从而实现任务级并行、资源隔离、GPU/CPU 异构调度和工作流结果追踪。

项目的 PyPI 包名是 `maze-agent`，命令行入口是 `maze` 和 `maze-sandbox`。

## Maze 解决什么问题

传统 Agent 或 LangGraph 工作流通常以函数调用或图执行为中心，但对“每个节点需要多少 CPU、多少 GPU、能不能并行、哪个 worker 有资源”管理较弱。Maze 的核心定位是：

- 任务级编排：把函数封装成 `@task` 任务，声明输入、输出、资源需求。
- DAG 依赖管理：上游任务完成后，下游任务自动获得输入并进入 ready 状态。
- 并行执行：无依赖的任务可以同时提交到 Ray 执行。
- 资源管理：按 `cpu`、`cpu_mem`、`gpu`、`gpu_mem` 选择可用节点，避免并发任务抢同一份资源。
- 分布式部署：一个 head 节点接收工作流和调度任务，多个 worker 可加入 Ray 集群。
- LangGraph 后端：可以用 `LanggraphClient.task` 把 LangGraph 节点远程交给 Maze 执行。
- 可视化：Playground 支持拖拽式构建工作流；Board 偏监控面板。
- 运行时扩展：包含 vLLM 实例管理、代码沙箱、Agent/ReAct、MCP 工具接入等实验模块。

## 顶层目录作用

| 路径 | 作用 |
| --- | --- |
| `README.md` | 项目介绍、安装、启动和最小示例。 |
| `pyproject.toml` | Python 包元数据、依赖、命令行入口。 |
| `maze/` | Maze 核心 Python 包，包含客户端、服务端、调度器、Agent、MCP、沙箱、工具等。 |
| `examples/` | 端到端示例：多模态内容生成、金融风险 LangGraph 工作流。 |
| `web/` | 前端/可视化相关代码，包括 Playground 和 Board。 |
| `test/` | pytest 测试，覆盖客户端、HTTP API、资源调度、LangGraph、缓存、异常、沙箱等。 |
| `assets/` | README 等文档使用的静态图片。 |
| `docs/` | 本地整理文档，本文件位于 `docs/260506/whatismaze.md`。 |
| `LICENSE` | MIT License。 |

## `maze/` 内部结构

### `maze/__init__.py`

对外暴露主 API：

- `MaClient`
- `MaWorkflow`
- `MaTask`
- `TaskOutput`
- `TaskOutputs`
- `task`
- `get_task_metadata`
- `LanggraphClient`

所以用户通常这样使用：

```python
from maze import MaClient, task
```

### `maze/cli/`

命令行入口。

- `cli.py`：实现 `maze start` 和 `maze stop`。
- `sandbox_cli.py`：实现 `maze-sandbox start`。

当前代码中的主命令：

```bash
maze start --head --port 8000 --ray-head-port 6379
maze start --worker --addr HEAD_IP:HEAD_PORT
maze stop
```

可选参数：

- `--strategy`：调度策略，默认 `Default`，代码中还处理 `DAPS`、`HACS`、`ATLAS`。
- `--playground`：只对 head 生效，会同时启动 Playground 后端和前端。
- `--log-level` / `--log-file`：日志配置。

注意：README 里有些旧命令如 `maze server`，但当前 CLI 代码实际是 `maze start --head`。

### `maze/client/maze/`

这是主客户端 API，也是 `from maze import MaClient, task` 实际使用的模块。

| 文件 | 作用 |
| --- | --- |
| `client.py` | `MaClient`，负责连接 Maze server、创建工作流、管理 LLM 实例。 |
| `workflow.py` | `MaWorkflow`，负责添加任务、添加边、运行工作流、读取结果、可视化工作流。 |
| `decorator.py` | `@task` 装饰器，提取函数源码、cloudpickle 序列化函数、记录输入输出和资源。 |
| `models.py` | `MaTask`、`TaskOutput`、`TaskOutputs`，用于任务对象和任务输出引用。 |
| `builtin/simpleTask.py` | 简单内置任务示例 `task1`、`task2`。 |

主客户端支持：

- `client.create_workflow()`
- `workflow.add_task(func, inputs={...})`
- `workflow.add_edge(task1, task2)`
- `workflow.run()`
- `workflow.get_results(run_id)`
- `workflow.show_results(run_id)`
- `workflow.get_task_result(run_id, task_id)`
- `workflow.print_graph()`
- `workflow.get_graph_mermaid()`
- `workflow.draw_graph(...)`
- `client.start_llm_instance(model)`
- `client.query_llm_instance(query, instance_id)`
- `client.stop_llm_instance(instance_id)`

### `maze/client/front/`

这是 Playground / 前端实验用客户端，和主客户端很像，但多了前端场景需要的能力。

| 文件 | 作用 |
| --- | --- |
| `client.py` | front 版 `MaClient`，还提供 `create_server_workflow`。 |
| `workflow.py` | front 版 `MaWorkflow`，支持文件型输入、结果文件下载等逻辑。 |
| `decorator.py` | front 版 `@task` 和 `@tool`，`tool` 用于轻量节点。 |
| `file_utils.py` | `FileInput`、文件类型判断。 |
| `server_workflow.py` | 将工作流部署成一个 FastAPI Agent 服务的实验实现。 |
| `builtin/` | 前端可用的内置任务，包括 simple、file、health。 |

注意：`client/front` 中有上传/下载/注册 Agent 的客户端逻辑，但当前 `maze/core/server.py` 没有实现所有对应端点，例如 `upload_file`、`download_file`、`cleanup_workflow`、`register_agent`。Playground 目前对这些能力做了降级处理。

### `maze/client/langgraph/`

LangGraph 适配层。

- `client.py`：`LanggraphClient`。
- `client.task` 装饰器会把函数注册成 Maze 的 `langgraph` 类型任务。
- 当 LangGraph 执行到这个函数时，wrapper 会把 `args`、`kwargs` 用 cloudpickle 序列化后请求 `/run_langgraph_task`，由 Maze 后端调度执行。

金融风险示例就在 `examples/financial_risk_workflow/langgraph_maze.py`。

### `maze/core/`

核心运行时。

| 路径 | 作用 |
| --- | --- |
| `server.py` | FastAPI 服务端，提供工作流、任务、边、运行、WebSocket 结果、worker 注册、LLM 实例接口。 |
| `path/path.py` | `MaPath`，Head 进程内的工作流管理器，连接 API 层和 scheduler 子进程。 |
| `workflow/workflow.py` | 工作流 DAG 对象，基于 NetworkX，负责添加任务、添加边、找起点、完成任务后解锁下游。 |
| `workflow/task.py` | `CodeTask`、`LangGraphTask` 数据模型。 |
| `scheduler/scheduler.py` | 独立 scheduler 子进程，包含接收线程、提交线程、监督线程、LLM 实例线程。 |
| `scheduler/runtime.py` | 运行期任务对象、Ray ObjectRef 和任务结果管理。 |
| `scheduler/resource.py` | Worker/Node 资源管理，按资源选择节点、占用和释放资源。 |
| `scheduler/runner.py` | Ray remote 函数，真正执行用户代码或反序列化后的函数。 |
| `scheduler/llm_instance.py` | 基于 Ray Actor 启动/停止 vLLM OpenAI API server。 |
| `worker/worker.py` | Worker 节点启动逻辑：加入 Ray 集群并向 head 注册资源。 |
| `predictor/` | DAPS 调度策略使用的执行时间预测服务和模型。 |

### `maze/tool/`

一组用 `@task` 包装的通用工具任务，可以放进工作流中使用。包括：

- 文件类：`file_manager`、`file_writer`、`text_reader`
- 文档/媒体读取：`pdf_reader`、`doc_reader`、`csv_reader`、`xlsx_reader`、`mp3_reader`、`video_reader`、`figure_reader`
- 网络类：`http_request`、`google_search`、`weather`
- 通用处理：`calculator`、`date_time`、`json_parser`、`string_processor`、`hash_generator`、`system_info`
- 邮件：`email_sender`

安全注意：`calculator` 使用受限 `eval`；文件管理/写入、HTTP 请求、邮件发送等任务有副作用，真实部署时需要权限和输入校验。

### `maze/agent/`

实验性 Agent 层。

| 路径 | 作用 |
| --- | --- |
| `react_agent/react_agent.py` | `ReActAgent`，循环执行“LLM 推理 -> 工具调用 -> 写入记忆”。 |
| `memory/short_term_memory.py` | 短期对话记忆。 |
| `tool/toolkit.py` | 工具注册中心，可注册普通 async tool 或 MCP tool。 |
| `tool/calculator.py`、`tool/weather.py` | 示例工具。 |
| `model/openai_model.py` | OpenAI 模型包装。 |
| `model/dashscope_model.py` | 阿里 DashScope 模型包装。 |

### `maze/mcp/`

MCP 客户端支持。

- `BaseClient`：连接、关闭、列工具、调用工具的基类。
- `StdIOClient`：通过 stdio 连接 MCP server。
- `HttpClient`：通过 SSE 或 streamable HTTP 连接 MCP server。
- `McpTool`：把 MCP 工具包装成 Toolkit 可调用函数。

测试示例在 `test/mcp_test/mcp_test.py`。

### `maze/sandbox/`

代码沙箱模块。

| 文件 | 作用 |
| --- | --- |
| `code_sandbox.py` | `CodeSandbox`，用 Ray Actor 创建 Docker 容器执行 Python 代码。 |
| `launcher.py` | `SandboxActor`，启动 `pytorch/pytorch` 容器，复制代码到 `/tmp/tmp.py` 并执行。 |
| `server.py` | FastAPI 沙箱服务，提供创建 session、执行代码、关闭 session。 |
| `client.py` | `CodeSandboxClient`，远程调用沙箱服务。 |

启动命令：

```bash
maze-sandbox start --host 0.0.0.0 --port 8000
```

运行沙箱需要 Docker。GPU 沙箱依赖 Docker GPU runtime / NVIDIA 环境。

### `maze/config/`

- `logging_config.py`：统一日志配置。

### `maze/utils/`

- `utils.py`：GPU 信息采集、可用端口查找。
- `resource_infer.py`：从函数源码和模型名字符串估算 GPU 显存需求，例如检测 `7B`、`13B`、`float16` 等信息。

## `examples/` 示例

### `examples/multimodel_create_workflow/`

多模态创意内容生成工作流：

1. 用户输入一个主题。
2. 预处理任务生成文本、图像、音频提示词。
3. 文本生成、图像生成、音频生成三个任务并行执行。
4. 汇总任务生成 HTML 展示页。

它展示了 Maze 的核心价值：一个前置任务解锁多个并行任务，CPU 和 GPU 任务由资源调度器分别安排。

### `examples/financial_risk_workflow/`

金融风险评估 LangGraph 工作流：

1. LLM 从自然语言投资组合描述中提取参数。
2. 市场风险、信用风险、流动性风险三个计算任务并行执行。
3. 汇总报告。

其中 `langgraph_naive.py` 是普通 LangGraph 版本，`langgraph_maze.py` 使用 `LanggraphClient` 把 LangGraph 节点交给 Maze runtime 执行。示例文档称在特定机器上 e2e 延迟从约 3 分钟降到约 47 秒。

## `web/` 前端

### `web/maze_playground/`

拖拽式工作流设计器。

架构：

```text
浏览器 React/Vite 前端
  -> Node.js Express 后端
  -> maze_bridge.py
  -> maze.client.front.MaClient
  -> Maze FastAPI server
  -> Scheduler/Ray
```

关键文件：

- `frontend/src/App.tsx`：Playground 主界面。
- `frontend/src/components/WorkflowCanvas.tsx`：ReactFlow 画布。
- `frontend/src/components/BuiltinTasksSidebar.tsx`：内置任务侧栏。
- `frontend/src/components/CustomTaskEditor.tsx`：自定义任务编辑器。
- `frontend/src/stores/workflowStore.ts`：Zustand 状态管理。
- `backend/src/server.js`：Express API 和 WebSocket server。
- `backend/maze_bridge.py`：Node 调 Python 的桥接层。

推荐启动方式：

```bash
maze start --head --port 8000 --playground
```

这会尝试启动：

- Maze server：`http://localhost:8000`
- Playground backend：`http://localhost:3001`
- Playground frontend：`http://localhost:5173`

### `web/maze_board/`

监控/看板前端，React + Vite。它支持 Mock 模式和 API 模式：

- Dashboard：集群和工作流概览。
- Workers：worker 资源列表。
- Workflows：工作流列表、运行详情、服务控制 UI。
- `src/services/api.js`：期望后端提供 `/api/workers`、`/api/workflows`、`/api/dashboard/summary` 等接口。

注意：这些 `/api/...` 监控接口是 Board 的期望接口，当前核心 `maze/core/server.py` 主要提供工作流执行 API，还没有完整实现 Board 所需的监控 API。

## `test/` 测试覆盖

主要测试点：

- `test_maclient_builtin.py`：内置任务工作流。
- `test_maclient_userdef.py`：用户自定义 `@task` 工作流。
- `test_http_api.py`：直接调用 HTTP API 和 WebSocket。
- `test_cluster_resources.py`：CPU/GPU 资源约束和多次运行。
- `test_multiple_runs.py`：同一 workflow 多次/并发运行。
- `test_results_cache.py`：客户端结果缓存和按任务取结果。
- `test_task_exception.py`：任务异常传播。
- `test_visualization.py`：ASCII、Mermaid、Graphviz/Matplotlib 可视化。
- `test_langgraph.py`：LangGraph 适配。
- `test_llm_instance.py`：vLLM 实例启动、查询、停止。
- `test_daps.py`：DAPS 相关预测特征流转。
- `sandbox_test/test_sandbox.py`：远程代码沙箱。
- `mcp_test/mcp_test.py`：ReActAgent + MCP 工具。

## 如何使用

### 1. 安装

从 PyPI：

```bash
pip install maze-agent
```

从源码：

```bash
git clone https://github.com/QinbinLi/Maze.git
cd Maze
pip install -e .
```

开发测试依赖：

```bash
pip install -e ".[dev]"
```

### 2. 启动 Head

```bash
maze start --head --port 8000
```

常用参数：

```bash
maze start --head --port 8000 --ray-head-port 6379
maze start --head --port 8000 --strategy HACS
maze start --head --port 8000 --strategy ATLAS
maze start --head --port 8000 --strategy DAPS
maze start --head --port 8000 --playground
```

`--strategy DAPS` 会额外启动 predictor app，端口为 `port + 1`。代码里的 DAPS 请求目前写死访问 `127.0.0.1:8001`，所以最稳妥是让 head 使用默认 `8000` 端口。

### 3. 启动 Worker

单机可以只启动 head，head 节点也会作为 Ray 节点参与资源管理。多机时，在其他机器上运行：

```bash
maze start --worker --addr HEAD_IP:8000
```

Worker 启动流程：

1. 请求 head 的 `/get_head_ray_port` 获取 Ray head 端口。
2. 执行 `ray start --address HEAD_IP:RAY_HEAD_PORT` 加入 Ray 集群。
3. 采集本节点 CPU、内存、GPU、显存信息。
4. 请求 head 的 `/start_worker` 注册资源。

### 4. 编写普通 Maze 工作流

```python
from maze import MaClient, task


@task(
    inputs=["text"],
    outputs=["result"],
    resources={"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0},
)
def hello_task(params):
    text = params.get("text")
    return {"result": f"Hello {text}"}


@task(
    inputs=["message"],
    outputs=["final"],
)
def suffix_task(params):
    message = params.get("message")
    return {"final": message + "!"}


client = MaClient("http://localhost:8000")
workflow = client.create_workflow()

t1 = workflow.add_task(hello_task, inputs={"text": "Maze"})
t2 = workflow.add_task(suffix_task, inputs={"message": t1.outputs["result"]})

# 使用 TaskOutput 作为输入时，主客户端会通过 save_task_and_add_edge 自动添加依赖边。
# 显式 add_edge 也可用，尤其适合手动 API 或旧式写法。
workflow.add_edge(t1, t2)

run_id = workflow.run()
result = workflow.show_results(run_id)
print(result)
```

### 5. 获取结果和可视化

```python
messages = workflow.get_results(run_id, verbose=False)
task_result = workflow.get_task_result(run_id, t2.task_id)

workflow.print_graph()
print(workflow.get_graph_mermaid())
workflow.draw_graph("workflow.png", method="auto")
```

`get_results` 通过 WebSocket 读取执行过程消息，消息类型包括：

- `start_task`
- `finish_task`
- `task_exception`
- `finish_workflow`

### 6. 直接调用 HTTP API

核心 API 在 `maze/core/server.py`：

| API | 作用 |
| --- | --- |
| `POST /create_workflow` | 创建 workflow。 |
| `POST /add_task` | 添加 code task。 |
| `GET /get_workflow_tasks/{workflow_id}` | 查询任务列表。 |
| `POST /save_task` | 保存任务代码、输入、输出、资源。 |
| `POST /save_task_and_add_edge` | 保存任务，并根据 `from_task` 输入自动加边。 |
| `POST /add_edge` | 添加依赖边。 |
| `POST /del_edge` | 删除依赖边。 |
| `POST /run_workflow` | 提交执行，返回 `run_id`。 |
| `WS /get_workflow_res/{workflow_id}/{run_id}` | 获取执行过程和结果。 |
| `POST /add_langgraph_task` | 注册 LangGraph 任务。 |
| `POST /run_langgraph_task` | 执行 LangGraph 任务。 |
| `POST /get_head_ray_port` | 查询 Ray head 端口。 |
| `POST /start_worker` | 注册 worker 资源。 |
| `POST /start_llm_instance` | 启动 vLLM 实例。 |
| `POST /stop_llm_instance` | 停止 vLLM 实例。 |

### 7. LangGraph 接入

```python
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from maze import LanggraphClient

client = LanggraphClient("localhost:8000")


class GraphState(TypedDict):
    text: str
    result: str


@client.task(resources={"cpu": 1, "gpu": 0, "cpu_mem": 0, "gpu_mem": 0})
def remote_node(state: GraphState):
    return {"result": state["text"].upper()}


builder = StateGraph(GraphState)
builder.add_node("remote", remote_node)
builder.add_edge(START, "remote")
builder.add_edge("remote", END)

graph = builder.compile()
print(graph.invoke({"text": "maze", "result": ""}))
```

### 8. 启动 Playground

```bash
maze start --head --port 8000 --playground
```

打开：

```text
http://localhost:5173
```

Playground 的基本使用流程：

1. 从左侧选择内置任务，或写一个自定义 `@task` 函数。
2. 拖拽节点到画布。
3. 配置输入参数，参数可以来自用户输入或上游任务输出。
4. 连线表达依赖关系。
5. 点击运行，前端通过 Node 后端和 `maze_bridge.py` 创建并执行 Maze 工作流。

### 9. 使用代码沙箱

启动服务：

```bash
maze-sandbox start --host 0.0.0.0 --port 8000
```

客户端示例：

```python
import asyncio
from maze.sandbox import CodeSandboxClient


async def main():
    sandbox = CodeSandboxClient("http://localhost:8000", cpu_nums=1, memory_mb=512)
    try:
        result = await sandbox.run_code("print('hello from sandbox')", timeout=10)
        print(result)
    finally:
        await sandbox.close()


asyncio.run(main())
```

## 核心原理

### 1. 任务定义：`@task`

`@task` 做了几件事：

1. 读取函数源码，剥掉装饰器，只保留函数定义。
2. 用 cloudpickle 序列化整个函数，得到 `code_ser`。
3. 记录 `inputs`、`outputs`、`resources`、`data_types`。
4. 把这些元数据挂到函数 `_maze_task_metadata` 上。

任务函数约定接收一个 `params` 字典，返回一个字典：

```python
@task(inputs=["x"], outputs=["y"])
def f(params):
    return {"y": params["x"] + 1}
```

### 2. 客户端构图

`workflow.add_task(func, inputs={...})` 时：

1. 客户端读取函数上的 task metadata。
2. 请求 `/add_task` 在服务端创建任务 ID。
3. 把普通输入转成 `from_user`。
4. 把 `task.outputs["key"]` 这样的引用转成 `from_task`，格式是：

```text
{task_id}.output.{output_key}
```

5. 请求 `/save_task_and_add_edge` 保存任务配置。
6. 服务端发现输入来自上游任务时，自动添加 DAG 边。

### 3. 服务端保存 DAG

`maze/core/workflow/workflow.py` 使用 NetworkX `DiGraph` 保存任务图：

- 节点：task id。
- 边：`source -> target`，表示 target 依赖 source。
- 添加边时检查 DAG，防止环。
- `get_start_task()` 找出入度为 0 的起始任务。
- `finish_task()` 标记任务完成，并检查所有后继节点是否所有前驱都完成，如果是，就返回新的 ready tasks。

### 4. 提交运行

`POST /run_workflow` 会调用 `MaPath.run_workflow`：

1. 为本次运行生成一个新的 `run_id`。
2. 深拷贝 workflow，避免一次运行影响下一次运行。
3. 找到所有起始任务。
4. 把这些任务发送到 scheduler 子进程的 ZeroMQ 队列。
5. 为本次 run 建立一个 asyncio queue，用来保存 scheduler 回传的状态消息。

### 5. Scheduler 子进程

Head 启动时会创建一个独立 scheduler 进程。scheduler 内部有四类线程：

- receive thread：接收 head 进程发来的 `run_task`、`clear_workflow`、`start_worker`、`start_llm_instance` 等消息。
- submit thread：从优先队列取 ready task，向 ResourceManager 申请资源，成功后提交 Ray remote task。
- supervisor thread：轮询 Ray ObjectRef，发现任务完成、失败或节点异常后回传消息，并释放资源。
- llm instance thread：为 vLLM 实例申请资源、启动或停止 Ray Actor。

Head 和 scheduler 之间用 ZeroMQ 通信；任务真正执行依赖 Ray。

### 6. Ray 执行用户任务

`maze/core/scheduler/runner.py` 中有两个 Ray remote 函数：

- `remote_task_runner`：执行普通 code task。
- `remote_lgraph_task_runner`：执行 LangGraph task。

普通任务执行时：

1. 如果有 `code_ser`，优先 cloudpickle 反序列化函数并调用。
2. 如果只有 `code_str`，则用 AST 提取 import 和第一个函数定义，`exec` 后调用。
3. 如果任务被分配了 GPU，设置 `CUDA_VISIBLE_DEVICES`。
4. 返回任务函数输出字典。

### 7. 输入传递

`WorkflowRuntime.run_task` 在提交 Ray 任务前会构造 `task_input_data`：

- `from_user`：直接取用户传入的 value。
- `from_task`：解析 `{task_id}.output.{key}`，从上游任务结果中取对应字段。

因此用户不需要手动在任务之间传结果，只要在构图时把输入指向上游输出即可。

### 8. 资源调度

`ResourceManager` 管理所有可用节点的资源：

- Head 启动时会调用 `ray.init(address='auto')` 并登记 head 节点资源。
- Worker 加入后通过 `/start_worker` 注册资源。
- GPU 信息来自 `nvidia-smi`。
- 每个任务声明 `resources={"cpu": ..., "cpu_mem": ..., "gpu": ..., "gpu_mem": ...}`。
- 选择节点时，先检查 CPU 和 CPU 内存，再检查 GPU 数量和 GPU 显存。
- 资源在任务提交时扣减，任务完成、取消或异常时释放。

注意：当前代码里 `gpu_mem` 来自 `nvidia-smi`，单位是 MB；`cpu_mem` 直接传给 Ray 的 `memory` 参数，代码中没有统一注释，使用时建议保持一致并按 Ray 的要求校准。

### 9. 调度策略

`--strategy` 影响 ready task 在优先队列里的优先级。

- `Default`：优先级为 0，基本按入队顺序执行。
- `DAPS`：对可预测任务调用 predictor 服务，根据任务剩余进度和预测耗时计算优先级；任务完成后收集真实执行时间用于训练。
- `HACS`：区分 CPU/GPU 任务，计算下游深度 `n_desc`、预测时间等，用于异构集群调度。
- `ATLAS`：维护 attained service 和提交时间，倾向公平/服务时间相关调度。

代码现状提醒：`predictor/server.py` 中 `/predict` 当前调用了 predictor 但返回值写成固定 `predict_time: 1`，所以 DAPS 的预测服务接口还带有实验/占位性质。

### 10. 结果返回

scheduler 任务状态通过 ZeroMQ 回到 `MaPath.monitor_coroutine`，再放入本次 run 的 asyncio queue。客户端通过：

```text
WS /get_workflow_res/{workflow_id}/{run_id}
```

持续接收消息。所有任务完成后，服务端发送：

```json
{"type": "finish_workflow", "data": {"run_id": "..."}}
```

然后通知 scheduler 清理运行期 workflow。

### 11. LangGraph 为什么能加速

LangGraph 本身负责图语义，Maze 接管部分节点的执行。当多个 LangGraph 节点没有依赖关系时，LangGraph 会触发它们，Maze wrapper 会把这些函数调用变成远程任务。Maze 再通过 Ray 和资源调度并行执行，从而减少总耗时。

### 12. Playground 原理

Playground 不是直接调用 Python 包，而是：

1. React 前端保存节点、边、参数。
2. Express 后端接收 workflow JSON。
3. 后端通过子进程运行 `maze_bridge.py`。
4. `maze_bridge.py` 动态加载内置任务或用户自定义任务。
5. 用 `maze.client.front.MaClient` 创建 Maze workflow。
6. 等待 Maze 执行结果，再通过后端 WebSocket 推给前端。

这种结构隔离了浏览器、Node 后端、Python 运行时，也让 Playground 可以解析 Python 任务元数据。

### 13. 沙箱原理

沙箱服务用 Ray Actor 承载 Docker 容器：

1. 创建 session 时启动一个 `pytorch/pytorch` 容器。
2. 代码被写入容器 `/tmp/tmp.py`。
3. 容器中执行 `python /tmp/tmp.py`。
4. 返回 stdout、stderr、exit_code、timed_out。
5. 关闭 session 时停止并删除容器。

它主要用于远程执行不可信或隔离需求较高的代码，但真实生产仍需进一步限制网络、文件系统和镜像权限。

## 当前代码里值得注意的点

- 当前 CLI 真实命令是 `maze start --head` / `maze start --worker`，不是部分旧文档中的 `maze server`。
- `--playground` 只对 head 生效；传给 worker 会被忽略并打印 warning。
- `maze/core/path/path.py` 中使用了 `time.time()`，但文件顶部未导入 `time`；按当前代码阅读，`run_workflow` 处可能触发 `NameError`，需要补 `import time`。
- `client/front` 提到的一些文件上传、下载、cleanup、agent 注册端点，在核心 server 中尚未完整实现。
- `web/maze_board` 是看板前端，但它期望的 `/api/...` 监控接口尚未在核心 server 中完整接上。
- DAPS predictor 的 `/predict` 当前返回固定预测时间，算法类实现存在，但接口返回还未真正使用预测值。
- Agent/OpenAI/DashScope/MCP、vLLM 实例、沙箱都属于有用但相对独立的扩展模块，成熟度低于核心工作流调度链路。
- 工作流任务执行用户代码，且部分内置工具会访问文件、网络或执行表达式，生产环境需要权限隔离、鉴权和审计。

## 适合的使用场景

- 多步骤 AI Agent pipeline：检索、分析、生成、汇总。
- 多模态任务：文本、图像、音频任务混合，并行占用 CPU/GPU。
- LangGraph 中计算密集型节点的并行后端。
- 多用户或多请求同时提交工作流，需要资源隔离。
- 需要可视化编排的任务流原型。
- 需要按 GPU/显存调度模型推理任务的实验系统。

## 不太适合直接使用的场景

- 需要强安全隔离的生产环境，但还没有完善鉴权、租户隔离和任务沙箱策略。
- 只运行一个很小的同步函数，Ray/HTTP/scheduler 的开销可能大于收益。
- 需要完整监控 API 和持久化任务历史的场景，目前 Board 侧更多是前端框架和接口约定。
- 需要严格稳定的 DAPS 预测调度，当前预测接口仍有占位实现。

## 推荐阅读顺序

如果继续深入代码，可以按这个顺序读：

1. `README.md`
2. `maze/client/maze/decorator.py`
3. `maze/client/maze/workflow.py`
4. `maze/core/server.py`
5. `maze/core/path/path.py`
6. `maze/core/workflow/workflow.py`
7. `maze/core/scheduler/scheduler.py`
8. `maze/core/scheduler/runtime.py`
9. `maze/core/scheduler/resource.py`
10. `maze/core/scheduler/runner.py`
11. `examples/financial_risk_workflow/langgraph_maze.py`
12. `web/maze_playground/backend/maze_bridge.py`

这条线基本覆盖了“用户函数如何变成分布式任务并返回结果”的完整闭环。
