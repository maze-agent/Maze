# Maze API 文档（中文）

> 版本：`maze-agent` 1.0.2 · Python ≥ 3.10
> 适用场景：任务级分布式 Agent / Workflow 框架的开发与集成

本文档汇总 Maze 项目对外提供的 **全部 API**，包含四层：

1. [Python SDK](#一python-sdk-api)：开发者最常用，定义任务 / 构建工作流 / 启动 Agent。
2. [Head HTTP & WebSocket API](#二head-服务-http--websocket-api)：FastAPI 提供的底层接口。
3. [CLI 命令](#三cli-命令)：`maze start`、`maze stop`、`maze-sandbox`。
4. [Playground 后端 REST API](#四maze-playground-后端-rest-api)：可视化界面专用接口。

文末附 [事件协议](#五事件协议event-protocol)、[资源配置说明](#六资源配置resources)、[错误码与异常](#七错误处理) 与 [完整示例](#八完整示例)。

---

## 一、Python SDK API

入口模块：

```python
from maze import (
    MaClient, MaWorkflow, MaTask, TaskOutput, TaskOutputs,
    DynamicRun, DynamicTaskSpec, DynamicTaskInvocation,
    AgentRun, AgentContext, AgentStep,
    ReActWorkflow, ReActStep,
    LanggraphClient,
    create_openai_react_llm_task,
    task, get_task_metadata,
)
```

### 1.1 `@task` 装饰器

```python
from maze import task
```

#### 签名

```python
task(func: Callable = None, *,
     resources: Dict[str, Any] | None = None,
     data_types: Dict[str, str] | None = None) -> Callable
```

#### 行为

- 把普通 Python 函数标记为 Maze 任务，并在函数对象上挂载 `TaskMetadata`。
- **inputs** 从函数签名自动推断。
- **outputs** 从函数体内 `return {...}` 字面量 key 自动推断（必须是 dict 字面量，否则抛 `TaskOutputInferenceError`）。
- **resources** 缺省值：`{"cpu":1,"cpu_mem":0,"gpu":0,"gpu_mem":0}`；如未显式声明 GPU，会通过 `infer_gpu_resources_from_function` 自动推断。
- 函数返回 **必须是 dict**，否则运行时抛 `TypeError`。

#### 参数

| 参数 | 类型 | 说明 |
|---|---|---|
| `resources` | `dict` | 资源配置，键：`cpu` / `cpu_mem` / `gpu` / `gpu_mem` |
| `data_types` | `dict[str, str]` | 显式覆盖某些 input/output 的类型字符串（默认从 type hint 推断） |

#### 示例

```python
@task(resources={"cpu": 1, "cpu_mem": 128})
def greet(text: str = ""):
    return {"result": f"Hello {text}"}
```

#### 配套函数

```python
get_task_metadata(func) -> TaskMetadata
```

返回字段：`func_name / code_str / code_ser (base64-cloudpickle) / inputs / outputs / resources / data_types`。

---

### 1.2 `MaClient`（客户端入口）

```python
class MaClient:
    def __init__(self, server_url: str = "http://localhost:8000")
```

| 方法 | 返回 | 说明 |
|---|---|---|
| `create_workflow()` | `MaWorkflow` | 创建一个静态 DAG workflow |
| `get_workflow(workflow_id)` | `MaWorkflow` | 关联已有 workflow（不校验存在性） |
| `create_dynamic_run(max_tasks=100, timeout_seconds=None)` | `DynamicRun` | 创建动态 run |
| `get_dynamic_run(run_id)` | `DynamicRun` | 关联已有动态 run |
| `list_dynamic_runs(status=None, limit=None)` | `list[dict]` | 列出动态 run（可按状态过滤） |
| `delete_dynamic_run(run_id)` | `dict` | 删除动态 run 及其历史 |
| `cleanup_dynamic_runs(statuses=None, older_than_days=None, dry_run=True)` | `dict` | 批量清理；`dry_run=True` 时只返回会被清理的列表 |
| `create_agent_run(tools, planner, max_steps=10, timeout_seconds=None, task_timeout=None)` | `AgentRun` | 通用 Agent loop（自定义 planner） |
| `create_react_workflow(llm_task, tools, max_steps=10, system_prompt=None, timeout_seconds=None, task_timeout=None)` | `ReActWorkflow` | ReAct 模板 |
| `get_ray_head_port()` | `dict` | 拿到 Ray Head 端口（外部 worker 接入用） |
| `start_llm_instance(model: str)` | `instance_id (str)` | 在集群里拉起一个 LLM 推理实例 |
| `stop_llm_instance(instance_id)` | `dict` | 关闭推理实例 |
| `query_llm_instance(query, instance_id)` | `str` | 通过 OpenAI 客户端查询实例（completion） |

---

### 1.3 `MaWorkflow`（静态 DAG）

```python
class MaWorkflow:
    workflow_id: str
    server_url: str
```

> 实例由 `MaClient.create_workflow()` 或 `get_workflow()` 返回，不要直接构造。

#### 任务与边

| 方法 | 说明 |
|---|---|
| `add_task(task_func, inputs=None, task_name=None)` | 添加 task；`inputs` 中允许填普通值或 `TaskOutput` 引用 |
| `add_task(task_type="code", task_name=...)` | 旧式手动添加（兼容） |
| `get_tasks() -> list[dict]` | 列出 workflow 中所有任务（`id`, `name`） |
| `add_edge(source_task, target_task)` | 增加依赖边 |
| `del_edge(source_task, target_task)` | 删除依赖边 |

`add_task` 接到 `@task` 函数后会自动：
1. 通过 `POST /add_task` 创建 task。
2. 用 `POST /save_task_and_add_edge` 保存代码、I/O、资源，并依据 `TaskOutput` 引用自动连边。
3. 返回 `MaTask`，其 `outputs["key"]` 是 `TaskOutput` 引用，可用于下游 `inputs`。

#### 执行与结果

| 方法 | 说明 |
|---|---|
| `run(file_context=None) -> run_id` | 提交执行，返回 `run_id`；`file_context` 用于挂载 workspace 文件 |
| `get_results(run_id, verbose=True) -> list[dict]` | 通过 WebSocket 拉取**全部原始消息**；结果会缓存 |
| `show_results(run_id) -> dict` | 格式化展示并返回 `{task_results, workflow_completed, has_exception, exception_tasks}` |
| `get_task_result(run_id, task_id) -> dict \| None` | 拿到单个 task 的结果 |
| `list_cached_runs() -> list[str]` | 列出已缓存的 run_id |
| `clear_cache(run_id=None)` | 清掉缓存 |

#### 可视化

| 方法 | 说明 |
|---|---|
| `get_graph_mermaid() -> str` | 生成 Mermaid 源码 |
| `get_graph_ascii() -> str` / `print_graph()` | ASCII 树状结构 |
| `get_graph_info() -> dict` | `{nodes, edges, stats}` |
| `draw_graph(output_path, engine="graphviz" \| "matplotlib", figsize, dpi)` | 渲染为图片 |

#### 示例

```python
from maze import MaClient, task

@task(resources={"cpu": 1, "cpu_mem": 128})
def greet(text: str = ""):
    return {"result": f"Hello {text}"}

@task(resources={"cpu": 1, "cpu_mem": 128})
def upper(result: str = ""):
    return {"upper": result.upper()}

client = MaClient("http://localhost:8000")
wf = client.create_workflow()
g  = wf.add_task(greet, inputs={"text": "Maze"})
u  = wf.add_task(upper, inputs={"result": g.outputs["result"]})
run_id = wf.run()
wf.show_results(run_id)
```

---

### 1.4 `MaTask` / `TaskOutput` / `TaskOutputs`

```python
class MaTask:
    task_id: str
    workflow_id: str
    task_name: str | None
    outputs: TaskOutputs  # 通过 outputs["key"] 拿到 TaskOutput
```

| 方法 | 说明 |
|---|---|
| `save(code_str, task_input, task_output, resources)` | 旧式 API：手动保存代码与 I/O |
| `delete()` | 从 workflow 删除该 task |

```python
class TaskOutput:
    task_id: str
    output_key: str
    def to_reference_string(self) -> str  # "<task_id>.output.<key>"

class TaskOutputs:
    def __getitem__(self, key) -> TaskOutput
    def keys() -> KeysView
```

`TaskOutput` 作为下游 task 的 `inputs[...]` 时，框架会在 server 端记录 `input_schema="from_task"` 并自动建立依赖边。

---

### 1.5 `DynamicRun`（动态工作流）

```python
class DynamicRun:
    run_id: str
    server_url: str
```

实例由 `MaClient.create_dynamic_run()` 或 `get_dynamic_run()` 返回。

#### 注册与追加任务

| 方法 | 说明 |
|---|---|
| `register_task_spec(task_func, task_spec_id=None, task_name=None) -> DynamicTaskSpec` | 注册一个可复用的任务规格 |
| `append_task(task, inputs=None, parents=None, request_id=None, task_name=None) -> DynamicTaskInvocation` | 运行时追加任务 |

`append_task` 的 `task` 参数支持三种：

- `DynamicTaskSpec`：使用已注册规格。
- `str`：用 `task_spec_id` 复用规格。
- `Callable`（`@task` 函数）：内联注册并提交。

`inputs` 中允许出现 `TaskOutput`（来自前序 task 的 `outputs[...]`），框架会自动转成跨任务引用。

`parents`：显式列出额外父任务（除 `inputs` 推导出的依赖外），元素可为 `DynamicTaskInvocation` 或 `task_id`。

`request_id`：幂等键。重复使用相同 `request_id` 不会重复创建任务，返回结果中 `idempotent=True`。

#### 等待与流

| 方法 | 说明 |
|---|---|
| `wait_for_task(task, timeout=None, poll_interval=0.2)` | 轮询事件直至任务完成；遇异常抛 `RuntimeError`，超时抛 `TimeoutError` |
| `stream_events() -> Iterator[dict]` | WebSocket 实时事件流 |
| `get_events(after=None) -> list[dict]` | HTTP 拉事件（增量） |
| `emit_event(event_type, data=None) -> dict` | 写入自定义事件，对调试 / agent trace 很有用 |
| `get_status() / status() -> dict` | 拿 run 当前快照 |

#### 生命周期

| 方法 | 说明 |
|---|---|
| `finalize(result=None)` | 标记 run 成功结束并返回结果 |
| `cancel(reason=None)` | 取消运行 |
| `delete()` | 从服务端删除 run 记录 |

#### 状态枚举

`created` / `running` / `finalized` / `failed` / `canceled` / `timed_out` / `interrupted`，
终态集合：`{finalized, failed, canceled, timed_out, interrupted}`（详见 `maze/core/workflow/dynamic.py:TERMINAL_DYNAMIC_RUN_STATUSES`）。

#### `DynamicTaskSpec` / `DynamicTaskInvocation`

```python
class DynamicTaskSpec:
    task_spec_id: str
    task_name: str
    output_keys: list[str]

class DynamicTaskInvocation:
    task_id: str
    task_name: str
    outputs: TaskOutputs | None
    idempotent: bool
```

#### 示例

```python
from maze import MaClient, task

@task(resources={"cpu": 1, "cpu_mem": 128})
def summarize(topic: str = ""):
    return {"summary": f"Maze can build workflows dynamically for {topic}."}

client = MaClient("http://localhost:8000")
run = client.create_dynamic_run(max_tasks=10)

spec = run.register_task_spec(summarize)
inv  = run.append_task(spec, inputs={"topic": "agent runtime"})
run.wait_for_task(inv)
run.finalize({"status": "done"})
```

---

### 1.6 `AgentRun`（通用 Agent 控制器）

```python
class AgentRun:
    def __init__(self,
                 dynamic_run: DynamicRun,
                 tools: list[Callable],
                 planner: Callable[[AgentContext], dict],
                 max_steps: int = 10,
                 task_timeout: float | None = None)

    def run(self, prompt: str) -> Any
    def status(self) -> dict
    def get_events(self, after=None) -> list[dict]
    def cancel(self, reason=None)
```

- `tools` 列表中的每个函数都必须是 `@task`。
- `planner(ctx: AgentContext) -> {"tool": "<name>", "args": {...}}` 或 `{"final": ...}`。
- 内部会 emit 事件：`agent_run_started` / `agent_action` / `agent_observation` / `agent_final` / `agent_error`。

#### `AgentContext` / `AgentStep`

```python
@dataclass
class AgentStep:
    index: int
    action: dict
    tool_name: str | None
    args: dict
    task_id: str | None
    observation: dict | None

@dataclass
class AgentContext:
    prompt: str
    tool_specs: dict[str, dict]
    steps: list[AgentStep]
    @property
    def observations(self) -> list[dict]
```

---

### 1.7 `ReActWorkflow`（ReAct 模板）

```python
class ReActWorkflow:
    def __init__(self,
                 dynamic_run: DynamicRun,
                 llm_task: Callable,
                 tools: list[Callable],
                 max_steps: int = 10,
                 system_prompt: str | None = None,
                 task_timeout: float | None = None)

    @property
    def run_id(self) -> str
    def run(self, prompt: str) -> Any
    def status(self) -> dict
    def get_events(self, after=None) -> list[dict]
    def cancel(self, reason=None)
```

#### LLM 决策 task 的契约

`llm_task` 是一个普通的 `@task`，函数签名可包含以下任意输入（按需声明，框架按签名传入）：

| 输入 | 类型 | 说明 |
|---|---|---|
| `prompt` | `str` | 用户原始问题 |
| `history` | `list[dict]` | 历史步骤（含 `tool`, `args`, `observation`） |
| `tools` | `dict[str, dict]` | 可用工具元信息（描述、入参、必填、出参等） |
| `step` | `int` | 当前步序号（从 1 起） |
| `system_prompt` | `str \| None` | 调用方注入的 system prompt |

返回值必须是 dict，并满足以下任一形式：

```json
{"action": {"tool": "<name>", "args": {...}}}
{"action": {"final": "<answer>"}}
// 兼容简写
{"tool": "...", "args": {...}}
{"final": "..."}
{"action": {"type": "final", "answer": "..."}}
```

#### 工具校验与修复

ReAct 会自动校验每一步决策，遇到下列错误会作为 `observation` 反馈给下一轮 LLM（允许“自我修复”）：

- `invalid_action`：JSON 不合法 / 缺字段
- `tool_not_allowed`：调用了未注册工具
- `missing_args`：必填入参缺失
- `unknown_args`：传了多余入参

#### `ReActStep`

```python
@dataclass
class ReActStep:
    index: int
    decision_task_id: str
    decision: dict
    tool_name: str | None
    args: dict
    tool_task_id: str | None
    observation: dict | None
```

#### OpenAI 兼容 LLM 决策 task 工厂

```python
from maze import create_openai_react_llm_task

llm_task = create_openai_react_llm_task(
    base_url="https://api.openai.com/v1",
    model="gpt-4o-mini",
    api_key_env="OPENAI_API_KEY",
    system_prompt=None,
    task_name="openai_react_decide",
    temperature=0,
    max_tokens=512,
    timeout=60,
    resources={"cpu": 1, "cpu_mem": 128},
)
```

参数：

| 参数 | 说明 |
|---|---|
| `base_url` | OpenAI 兼容端点（去掉末尾 `/`） |
| `model` | 模型名 |
| `api_key` / `api_key_env` / `config_path` | 三选一；`config_path` 指向 JSON，可包含 `url/base_url/model/key/api_key/key_env/api_key_env` |
| `system_prompt` | 自定义系统提示，默认会强调“仅返回 JSON” |
| `temperature` / `max_tokens` / `timeout` | 透传给 `/chat/completions` |
| `resources` | 该 task 的资源声明 |

返回值是一个 `@task` 函数，可直接传给 `client.create_react_workflow(llm_task=...)`。

---

### 1.8 `LanggraphClient`（LangGraph 桥）

```python
from maze import LanggraphClient

lc = LanggraphClient(addr="localhost:8000")

@lc.task(resources={"cpu": 1, "gpu": 0})
def node(state):
    ...
    return state
```

- 构造时自动 `POST /create_workflow`。
- `lc.task(...)` 装饰过的函数被调用时，框架会 cloudpickle args/kwargs，调用 `POST /run_langgraph_task` 在 Maze 集群中远程执行，返回值与本地调用一致。
- 资源支持 `cpu / gpu / cpu_mem / gpu_mem`，默认 `{cpu: 1, gpu: 0}`。

---

## 二、Head 服务 HTTP & WebSocket API

由 `maze start --head --port <PORT>` 启动，默认 `http://localhost:8000`，CORS 全开。
代码：`maze/core/server.py`。

> 所有响应都遵循 `{"status": "success" \| "error", ...}` 约定；错误时返回 HTTP 500 并附 `detail`。

### 2.1 静态 Workflow

#### `POST /create_workflow`
新建 workflow。

**请求体**：无
**响应**：
```json
{"status": "success", "workflow_id": "<uuid>"}
```

---

#### `POST /add_task`
向 workflow 加入一个任务（Code 类型）。

**请求体**：
```json
{
  "workflow_id": "<uuid>",
  "task_type":  "code",
  "task_name":  "<string>"
}
```
**响应**：
```json
{"status": "success", "task_id": "<uuid>"}
```

`task_type` 取值：`"code"` 或 `"langgraph"`（LangGraph 任务请用 `/add_langgraph_task`）。

---

#### `GET /get_workflow_tasks/{workflow_id}`
列出 workflow 内任务。

**响应**：
```json
{"status": "success", "tasks": [ {...任务序列化...} ]}
```

---

#### `POST /del_task`
删除任务。

```json
{"workflow_id": "...", "task_id": "..."}
```

---

#### `POST /save_task`
保存任务的代码、I/O、资源、文件上下文。

```json
{
  "workflow_id": "...",
  "task_id": "...",
  "task_input":  { "input_params":  { "1": { "key": "x", "input_schema": "from_user|from_task", "data_type": "str", "value": "...", "has_value": true } } },
  "task_output": { "output_params": { "1": { "key": "y", "data_type": "any" } } },
  "code_str":  "<可选>",
  "code_ser":  "<base64-cloudpickle 序列化函数，可选；code_str/code_ser 至少一项>",
  "resources": { "cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0 },
  "file_context": null
}
```

---

#### `POST /save_task_and_add_edge`
与 `/save_task` 相同，且会按 `task_input.input_params[].input_schema == "from_task"` 自动建立依赖边（`value` 形如 `"<source_task_id>.output.<key>"`）。

---

#### `POST /add_edge` / `POST /del_edge`
```json
{"workflow_id": "...", "source_task_id": "...", "target_task_id": "..."}
```

---

#### `POST /run_workflow`
触发执行。
```json
{"workflow_id": "...", "file_context": { ...可选 }}
```
**响应**：`{"status": "success", "run_id": "<uuid>"}`

---

#### `WS /get_workflow_res/{workflow_id}/{run_id}`
订阅静态 run 的实时事件流，消息体为 JSON。常见事件类型：

| `type` | 说明 |
|---|---|
| `start_task` | 任务开始 |
| `finish_task` | 任务完成（携带 `data.result`） |
| `task_exception` | 任务异常 |
| `finish_workflow` | 工作流完成 |

连接异常时服务端会自动调用 `stop_workflow`。

---

### 2.2 Dynamic Run

#### `POST /dynamic_runs`
创建动态 run。
```json
{"max_tasks": 100, "timeout_seconds": null}
```
**响应**：`{"status": "success", "run_id": "..."}`

---

#### `GET /dynamic_runs?status=&limit=`
列出动态 run，可按 `status` 过滤。
**响应**：
```json
{"status": "success", "runs": [ { "run_id": "...", "status": "running", ... } ]}
```

---

#### `POST /dynamic_runs/cleanup`
批量清理已终结的 run。
```json
{
  "statuses": ["failed", "canceled"],
  "older_than_days": 7,
  "dry_run": true
}
```

---

#### `GET /dynamic_runs/{run_id}`
拿 run 完整快照（task_specs / tasks / 状态 / 事件序号等）。

---

#### `DELETE /dynamic_runs/{run_id}`
删除一个 run。

---

#### `POST /dynamic_runs/{run_id}/task_specs`
注册任务规格。
```json
{
  "task_spec_id": "<可选>",
  "task_name":    "<可选>",
  "code_str":     "<可选>",
  "code_ser":     "<base64-cloudpickle, code_str/code_ser 二选一>",
  "inputs":  [{"name": "x", "data_type": "str"}],
  "outputs": [{"name": "y", "data_type": "any"}],
  "resources": {"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}
}
```
**响应**：
```json
{
  "status": "success",
  "run_id": "...",
  "task_spec_id": "...",
  "task_name": "...",
  "inputs":   [...],
  "outputs":  [...],
  "resources":{...}
}
```

---

#### `POST /dynamic_runs/{run_id}/append_task`
运行时追加任务。
```json
{
  "task_spec_id": "<可选，引用已注册规格>",
  "task_spec":    { ...如未注册，可以内联同 /task_specs 请求体... },
  "inputs": {
    "x": 1,
    "y": {"__maze_output_ref__": true, "task_id": "<父task>", "output_key": "out"}
  },
  "parents": ["<额外父task id>"],
  "request_id": "<可选幂等键>"
}
```
**响应**：
```json
{
  "status": "success",
  "run_id": "...",
  "task_id": "...",
  "task_name": "...",
  "outputs": [{"name": "y", "data_type": "any"}],
  "idempotent": false
}
```

---

#### `POST /dynamic_runs/{run_id}/finalize`
```json
{"result": { ... }}
```

---

#### `POST /dynamic_runs/{run_id}/cancel`
```json
{"reason": "user_cancel"}
```
**响应**：`{"status":"success","run_id":"...","run_status":"canceled"}`

---

#### `GET /dynamic_runs/{run_id}/events?after=<seq>`
拉取 `seq > after` 的事件列表。

---

#### `POST /dynamic_runs/{run_id}/events`
写入自定义事件。
```json
{"type": "agent_action", "data": {...}}
```

---

#### `WS /dynamic_runs/{run_id}/events`
实时事件流（含 `register_task_spec`、`append_task`、`task_ready`、`start_task`、`finish_task`、`task_exception`、`agent_*`、`react_*`、`finish_workflow`、`cancel_dynamic_run`、`timeout_dynamic_run`、`interrupt_dynamic_run` 等，参见 [事件协议](#五事件协议event-protocol)）。

---

### 2.3 LangGraph 桥

#### `POST /add_langgraph_task`
```json
{
  "workflow_id": "...",
  "task_type":   "langgraph",
  "task_name":   "...",
  "code_ser":    "<base64-cloudpickle>",
  "resources":   {"cpu": 1, "gpu": 0, "cpu_mem": 0, "gpu_mem": 0}
}
```

#### `POST /run_langgraph_task`
```json
{
  "workflow_id": "...",
  "task_id":     "...",
  "args":   "<base64-cloudpickle of tuple>",
  "kwargs": "<base64-cloudpickle of dict>"
}
```
**响应**：`{"status":"success","result": <pickle-back 后的返回值>}`

---

### 2.4 集群 / LLM 实例

#### `POST /get_head_ray_port`
**响应**：`{"status":"success","port": <int>}`

#### `POST /start_worker`
```json
{"node_ip": "192.168.x.x", "node_id": "worker-1", "resources": {"cpu": 8, "gpu": 1, ...}}
```

#### `POST /start_llm_instance`
```json
{"model": "Qwen2.5-7B", "cpu_nums": 5, "gpu_nums": 1, "memory": 1024, "gpu_mem": 16000}
```
**响应**：
```json
{"status":"success","host":"...","port":12345,"instance_id":"<uuid>"}
```
之后可通过 OpenAI 兼容协议直接访问 `http://host:port/v1/...`。

#### `POST /stop_llm_instance`
```json
{"instance_id": "..."}
```

---

### 2.5 资源预测器（`--strategy DAPS` 时启用）

DAPS 策略下，Head 会在 `port + 1` 上同时启动一个 FastAPI 服务（`maze/core/predictor/server.py`），用于做运行时间/资源预测；当前主要被调度器内部使用，未对外暴露稳定的 RESTful 接口。

---

## 三、CLI 命令

`pyproject.toml` 注册：

```toml
[project.scripts]
maze         = "maze.cli.cli:main"
maze-sandbox = "maze.cli.sandbox_cli:main"
```

### 3.1 `maze start`

```
maze start --head | --worker [其它选项]
```

#### Head 模式
```bash
maze start --head \
           --port 8000 \
           --ray-head-port 6379 \
           --strategy Default \
           [--playground] \
           [--log-level INFO] [--log-file /path/to/log]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--port` | 8000 | Maze Head FastAPI 端口 |
| `--ray-head-port` | 6379 | Ray Head 的 GCS 端口 |
| `--strategy` | `Default` | 调度策略：`Default` / `DAPS` / `HACS` / `ATLAS` |
| `--playground` |  | 同时拉起 Playground 后端 (3001) + 前端 (5173) |
| `--log-level` | `INFO` | `DEBUG/INFO/WARNING/ERROR/CRITICAL` |
| `--log-file` |  | 写入文件 |

#### Worker 模式
```bash
maze start --worker --addr <HEAD_IP>:<HEAD_PORT>
```

### 3.2 `maze stop`

```bash
maze stop [--log-level INFO] [--log-file ...]
```

停止本机的 Worker。

### 3.3 `maze-sandbox`

代码沙箱辅助命令，详见 `maze/cli/sandbox_cli.py`。

---

## 四、Maze Playground 后端 REST API

由 `maze start --head --playground` 拉起，运行在 Node.js（默认端口 **3001**），代码：`web/maze_playground/backend/src/server.js`。前端 (5173) 调它，它再调用 Maze Head (`http://localhost:8000`) 和 Python 桥 `maze_bridge.py`。

环境变量：

| 变量 | 说明 |
|---|---|
| `MAZE_WORKSPACE_DIR` | workspace 根目录（默认项目根下 `workspace/`） |
| `MAZE_CORE_URL` | Maze Head 地址（默认 `http://localhost:8000`） |
| `PYTHON_BIN` / `CONDA_PREFIX` | Python 解释器（被 Python 桥调用时使用） |

---

### 4.1 任务管理

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/builtin-tasks` | 列出内置任务（`Write File` / `Read File` / `Exec Code` 等） |
| GET | `/api/workspace-tasks` | 列出 `workspace/tasks/` 下用户任务 |
| POST | `/api/workspace-tasks` | 保存（新增/覆盖）一个 workspace task 的 Python 源码 |
| DELETE | `/api/workspace-tasks` | 删除一个 workspace task |
| PATCH | `/api/workspace-tasks/rename` | 重命名 task |

请求体里 task 路径都使用相对于 `workspace/tasks/` 的路径。

---

### 4.2 Workspace 文件管理

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/workspace-files?path=` | 列目录 |
| POST | `/api/workspace-files/upload` | 上传文件（multipart 或 base64 字段） |
| POST | `/api/workspace-files/mkdir` | 创建目录 |
| DELETE | `/api/workspace-files` | 删除文件/目录 |
| GET | `/api/workspace-files/preview?path=` | 预览文件（自动识别文本 / 图片 / 表格） |
| GET | `/api/workspace-files/download?path=` | 下载（二进制） |

---

### 4.3 LLM 集成

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/llm/test` | 测试 OpenAI 兼容端点连通性 |
| POST | `/api/llm/generate-task` | 用 LLM 生成 workspace 任务 Python 源码 |

`/api/llm/generate-task` 请求体示例：
```json
{
  "prompt": "Generate a task to count CSV rows",
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-4o-mini",
  "api_key": "<或在 server 端读 env>"
}
```

---

### 4.4 Workspace Workflows

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/workspace-workflows` | 列出已保存 workflow |
| DELETE | `/api/workspace-workflows` | 删除 workflow |
| PATCH | `/api/workspace-workflows/rename` | 重命名 |
| POST | `/api/workspace-workflows/save` | 保存（前端 JSON 蓝图） |
| POST | `/api/workspace-workflows/load` | 加载到画布 |
| POST | `/api/workspace-workflows/import` | 从上传的 JSON 文件导入 |

---

### 4.5 Runs 视图（统一查看静态 / 动态）

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/dynamic-runs` | 列出所有 dynamic runs |
| GET | `/api/dynamic-runs/:runId` | 单个 run 详情 |
| GET | `/api/dynamic-runs/:runId/events` | 事件列表（HTTP） |
| POST | `/api/dynamic-runs/:runId/events` | 写入事件（透传给 Maze Head） |
| DELETE | `/api/dynamic-runs/:runId` | 删除 |
| POST | `/api/dynamic-runs/cleanup` | 批量清理 |
| POST | `/api/react-runs/start` | 用 ReAct 模板启动一次 run（后端会 spawn Python 进程） |
| GET | `/api/workflow-runs/static` | 列出静态 run |
| GET | `/api/workflow-runs/static/:runId` | 静态 run 详情 |
| GET | `/api/workflow-runs/static/:runId/events` | 事件列表 |
| GET | `/api/workflow-runs/static/:runId/artifacts/download` | 下载 run 产物（zip） |
| DELETE | `/api/workflow-runs/static/:runId` | 删除 |
| POST | `/api/workflow-runs/static/cleanup` | 批量清理 |

---

### 4.6 编辑器

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/parse-custom-function` | 解析用户上传/粘贴的 Python 源码，抽取 `@task` 元数据 |
| POST | `/api/workflows` | 创建画布上的 workflow 草稿 |
| GET | `/api/workflows/:id` | 读取草稿 |
| PUT | `/api/workflows/:id` | 更新草稿 |
| POST | `/api/workflows/:id/run` | 把画布草稿编译成 Maze workflow 并执行 |
| GET | `/api/workflows/:id/results` | 拉取执行结果 |

---

### 4.7 健康检查

`GET /health` → `{"status":"ok"}`

---

## 五、事件协议（Event Protocol）

所有事件结构统一为：

```json
{
  "seq": <int>,           // 单调递增，可用于 ?after=
  "ts":  <unix-ms>,
  "type": "<event_type>",
  "data": { ... }
}
```

### 5.1 调度器与任务事件（来自 `maze/core/path/path.py` 与 `scheduler.py`）

| `type` | 触发时机 | `data` 关键字段 |
|---|---|---|
| `start_dynamic_run` | 动态 run 启动 | `run_id` |
| `register_task_spec` | 注册任务规格 | `task_spec_id`, `task_name`, `inputs`, `outputs`, `resources` |
| `append_task` | 追加任务 | `task_id`, `task_name`, `parents`, `inputs` |
| `task_ready` | 任务依赖就绪、进入调度 | `task_id` |
| `start_task` | 任务开始执行 | `task_id`, `node_id` |
| `finish_task` | 任务成功 | `task_id`, `result` |
| `task_exception` | 任务失败 | `task_id`, `error`, `traceback` |
| `finish_workflow` | 工作流结束 | `run_id`, `result` |
| `cancel_dynamic_run` | 被取消 | `reason` |
| `timeout_dynamic_run` | 超时 | `timeout_seconds` |
| `interrupt_dynamic_run` | 被打断 | — |
| `start_llm_instance` / `finish_llm_instance_launch` / `stop_llm_instance` | LLM 实例生命周期 | `instance_id`, `host`, `port` |

### 5.2 Agent / ReAct 事件（由 SDK 通过 `emit_event` 写入）

| `type` | `data` 关键字段 |
|---|---|
| `agent_run_started` | `mode (react/...) , prompt, max_steps, tools` |
| `agent_action` | `step, tool, args` |
| `agent_observation` | `step, tool, task_id, result` |
| `agent_repair_observation` | `step, tool, decision_task_id, result.error_type` |
| `react_llm_decision` | `step, task_id, decision, action` |
| `agent_final` | `mode, answer, step_count` |
| `agent_error` | `error` |

> 自定义事件类型：任何字符串都允许，建议使用 `snake_case` 并以业务前缀避免冲突。

---

## 六、资源配置（resources）

通用资源字段：

| 字段 | 单位 | 默认 | 说明 |
|---|---|---|---|
| `cpu` | 核 | 1 | 至少为 1 |
| `cpu_mem` | MB | 0 | 可为 0 表示不限定 |
| `gpu` | 张 | 0 | 若 `gpu_mem > 0` 自动至少 1 |
| `gpu_mem` | MB | 0 | 若未声明且 task 用到 GPU 库会自动推断 |

`_normalize_resources` 规则（`maze/client/maze/decorator.py`）：

- 默认 `{cpu:1, cpu_mem:0, gpu:0, gpu_mem:0}`。
- 用户显式声明字段会覆盖默认值。
- 若未显式声明 `gpu` 且未显式声明 `gpu_mem`，框架会调用 `infer_gpu_resources_from_function` 静态分析函数体（识别 torch/cuda 等）来推断 GPU 需求。
- `cpu < 1` 自动夹回 1。
- `gpu_mem > 0` 时 `gpu` 自动至少 1。

---

## 七、错误处理

### 7.1 SDK 异常

| 异常 | 触发条件 |
|---|---|
| `Exception("Failed to ...")` | HTTP 非 200，或 `status != "success"` |
| `RuntimeError("Dynamic run ended before task finished: ...")` | `wait_for_task` 时 run 进入终态 |
| `RuntimeError("Dynamic task failed: ...")` | `wait_for_task` 时任务 `task_exception` |
| `TimeoutError` | `wait_for_task(timeout=)` 超时 |
| `TaskOutputInferenceError` | `@task` 函数没有 `return {...}` 字面量 |
| `TypeError("Task ... must return a dict")` | task 运行时返回非 dict |
| `TypeError("@task no longer accepts ...")` | 用了已废弃参数 |
| `ValueError("...")` | 参数校验失败（max_steps、工具名重复等） |

### 7.2 服务端约定

所有 HTTP 接口在异常时返回：

```http
HTTP/1.1 500 Internal Server Error
Content-Type: application/json

{"detail": "<exception message>"}
```

WebSocket 出错时服务端会主动 close。

---

## 八、完整示例

### 8.1 静态 Workflow

```python
from maze import MaClient, task

@task(resources={"cpu": 1, "cpu_mem": 128})
def greet(text: str = ""):
    return {"result": f"Hello {text}"}

@task(resources={"cpu": 1, "cpu_mem": 128})
def upper(result: str = ""):
    return {"upper": result.upper()}

client = MaClient("http://localhost:8000")
wf = client.create_workflow()
g  = wf.add_task(greet, inputs={"text": "Maze"})
u  = wf.add_task(upper, inputs={"result": g.outputs["result"]})

run_id = wf.run()
out = wf.show_results(run_id)
print(out["task_results"])
```

### 8.2 动态 Workflow

```python
from maze import MaClient, task

@task
def summarize(topic: str = ""):
    return {"summary": f"Maze can build workflows dynamically for {topic}."}

client = MaClient("http://localhost:8000")
run = client.create_dynamic_run(max_tasks=10, timeout_seconds=60)

inv = run.append_task(summarize, inputs={"topic": "agent runtime"})
finish_event = run.wait_for_task(inv, timeout=30)
print(finish_event["data"]["result"])
run.finalize({"status": "done"})
```

### 8.3 ReAct Agent（自定义决策）

```python
from maze import MaClient, task

@task
def decide(prompt: str, history: list, tools: dict, step: int):
    if not history:
        return {"action": {"tool": "multiply", "args": {"a": 18, "b": 7}}}
    result = history[-1]["observation"]["result"]["result"]
    return {"action": {"final": f"The answer is {result}."}}

@task
def multiply(a: int, b: int):
    return {"result": a * b}

client = MaClient("http://localhost:8000")
react = client.create_react_workflow(
    llm_task=decide,
    tools=[multiply],
    max_steps=3,
)
answer = react.run("Compute 18 * 7 with the calculator.")
print(answer)
```

### 8.4 ReAct Agent（OpenAI 兼容 LLM）

```python
import os
from maze import MaClient, task, create_openai_react_llm_task

@task
def multiply(a: int, b: int):
    return {"result": a * b}

llm_task = create_openai_react_llm_task(
    base_url="https://api.openai.com/v1",
    model="gpt-4o-mini",
    api_key=os.environ["OPENAI_API_KEY"],
)

client = MaClient("http://localhost:8000")
react = client.create_react_workflow(
    llm_task=llm_task,
    tools=[multiply],
    max_steps=4,
    system_prompt="You can only call the multiply tool.",
)
print(react.run("Compute 23 * 17."))
```

### 8.5 LangGraph 迁移

```python
from langgraph.graph import StateGraph, START, END
from maze import LanggraphClient

lc = LanggraphClient(addr="localhost:8000")

@lc.task(resources={"cpu": 1})
def step_a(state):
    state["x"] += 1
    return state

@lc.task(resources={"cpu": 1})
def step_b(state):
    state["y"] = state["x"] * 2
    return state

graph = StateGraph(dict)
graph.add_node("a", step_a)
graph.add_node("b", step_b)
graph.add_edge(START, "a")
graph.add_edge("a", "b")
graph.add_edge("b", END)
app = graph.compile()

print(app.invoke({"x": 1}))
```

### 8.6 直接调用 HTTP API（curl）

```bash
# 1) 创建动态 run
curl -s -X POST http://localhost:8000/dynamic_runs \
     -H 'Content-Type: application/json' \
     -d '{"max_tasks": 10}'
# => {"status":"success","run_id":"..."}

# 2) 注册任务规格
curl -s -X POST http://localhost:8000/dynamic_runs/<run_id>/task_specs \
     -H 'Content-Type: application/json' \
     -d '{
       "task_spec_id": "summarize",
       "task_name":    "summarize",
       "code_ser":     "<base64-cloudpickle>",
       "inputs":  [{"name":"topic","data_type":"str"}],
       "outputs": [{"name":"summary","data_type":"any"}],
       "resources": {"cpu":1, "cpu_mem":128}
     }'

# 3) 追加任务
curl -s -X POST http://localhost:8000/dynamic_runs/<run_id>/append_task \
     -H 'Content-Type: application/json' \
     -d '{"task_spec_id":"summarize","inputs":{"topic":"agent runtime"}}'

# 4) 拉事件
curl -s "http://localhost:8000/dynamic_runs/<run_id>/events?after=0"

# 5) 结束
curl -s -X POST http://localhost:8000/dynamic_runs/<run_id>/finalize \
     -H 'Content-Type: application/json' \
     -d '{"result": {"status":"done"}}'
```

---

## 九、参考

- 项目主仓库：<https://github.com/QinbinLi/Maze>
- 在线文档：<https://maze-doc-new.readthedocs.io/>
- 官网：<https://mazeagent.net/>
- 关键源码：
  - 装饰器：`maze/client/maze/decorator.py`
  - 静态 workflow SDK：`maze/client/maze/workflow.py`
  - 动态 run SDK：`maze/client/maze/dynamic.py`
  - Agent / ReAct：`maze/client/maze/agent.py`、`maze/client/maze/react.py`、`maze/client/maze/react_llm.py`
  - LangGraph 桥：`maze/client/langgraph/client.py`
  - Head 服务：`maze/core/server.py`
  - 调度器：`maze/core/scheduler/`
  - 动态运行模型与事件：`maze/core/workflow/dynamic.py`、`maze/core/workflow/dynamic_store.py`
  - CLI：`maze/cli/cli.py`、`maze/cli/sandbox_cli.py`
  - Playground 后端：`web/maze_playground/backend/src/server.js`
