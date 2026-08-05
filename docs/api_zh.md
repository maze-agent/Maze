# Maze API 文档（中文）

> 版本：`maze-agent` 1.0.2 · Python ≥ 3.10
> 适用场景：任务级分布式 Agent / Workflow 框架的开发与集成
> 最近更新：2026-06-20，补齐 AppSpec、统一 Run/Artifact API、Workbench System Catalog 与 Artifact 下载接口。

本文档汇总 Maze 项目对外提供的 **全部 API**，包含四层：

1. [Python SDK](#一python-sdk-api)：开发者最常用，定义任务 / 构建工作流 / 启动 Agent。
2. [Head HTTP & WebSocket API](#二head-服务-http--websocket-api)：FastAPI 提供的底层接口。
3. [CLI 命令](#三cli-命令)：`maze start`、`maze stop`，以及已退役的 `maze-sandbox` 兼容命令。
4. [Playground 后端 REST API](#四maze-playground-后端-rest-api)：可视化界面专用接口。

文末附 [事件协议](#五事件协议event-protocol)、[资源配置说明](#六资源配置resources)、[错误码与异常](#七错误处理) 与 [完整示例](#八完整示例)。

---

## 一、Python SDK API

入口模块：

```python
from maze import (
    MaClient, MaWorkflow, MaTask, TaskOutput, TaskOutputs,
    DynamicRun, DynamicTaskSpec, DynamicTaskInvocation,
    LanggraphClient,
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
     data_types: Dict[str, str] | None = None,
     max_retries: int | None = None,
     retry_backoff_seconds: float = 0,
     retry_on: list[str] | None = None,
     timeout_seconds: float | None = None) -> Callable
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
| `max_retries` | `int \| None` | 单个 task 失败后的最大重试次数；`None` 表示使用服务端默认行为 |
| `retry_backoff_seconds` | `float` | 每次重试前等待的秒数 |
| `retry_on` | `list[str] \| None` | 限定哪些异常/错误类型触发重试；为空时不做类型过滤 |
| `timeout_seconds` | `float \| None` | 单个 task 的运行超时时间 |

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
| `create_workflow()` | `MaWorkflow` | 创建一个本地静态 DAG 草稿 |
| `create_workflow_from(workflow_def, inputs=None)` | `MaWorkflow` | 从 `@workflow` 定义构建静态 workflow |
| `run_app(spec, workspace_dir=None, artifact_mode=True, timeout_seconds=None, tags=None, metadata=None)` | `dict` | 提交 AppSpec/RunSpec，走 `/apps/run` |
| `validate_workflow_spec(spec)` | `dict` | 校验外部 DAG spec，走 `/workflows/validate` |
| `submit_workflow(spec, artifact_mode=True, tags=None, metadata=None)` | `dict` | 提交外部 DAG spec，返回 `workflow_id/run_id` |
| `create_dynamic_run(max_tasks=100, timeout_seconds=None, file_context=None, workspace_dir=None, artifact_mode=False, metadata=None)` | `DynamicRun` | 创建动态 run |
| `get_dynamic_run(run_id)` | `DynamicRun` | 关联已有动态 run |
| `list_dynamic_runs(status=None, limit=None, detail=False)` | `list[dict]` | 列出动态 run（可按状态过滤） |
| `delete_dynamic_run(run_id)` | `dict` | 删除动态 run 及其历史 |
| `cleanup_dynamic_runs(statuses=None, older_than_days=None, dry_run=True)` | `dict` | 批量清理；`dry_run=True` 时只返回会被清理的列表 |
| `list_runs(status=None, kind=None, limit=None, detail=False)` | `list[dict]` | 统一列出静态 / 动态 / app run |
| `get_run(run_id)` | `dict` | 统一获取 run 快照 |
| `get_run_tasks(run_id)` / `get_run_task(run_id, task_id)` | `list[dict]` / `dict` | 查询 run 内 task 状态 |
| `get_run_artifacts(run_id)` / `get_run_task_artifacts(run_id, task_id)` | `list[dict]` | 查询 run 或 task 产物 |
| `get_run_events(run_id, after=None)` | `list[dict]` | 增量读取 run 事件 |
| `get_run_logs(run_id, tail=500, task_id=None)` | `dict` | 读取 run/task 日志尾部 |
| `cancel_run(run_id, reason=None)` | `dict` | 统一取消静态或动态 run |
| `retry_run(run_id, ...)` | `dict` | 重跑 AppSpec run（仅 AppSpec run 支持） |
| `wait_run(run_id, timeout=None, poll_interval=0.5)` | `dict` | 轮询等待 run 进入终态 |
| `stream_run(run_id, poll_interval=0.2)` | `Iterator[dict]` | 轮询事件流直到终态事件 |
| `get_ray_head_port()` | `dict` | 拿到 Ray Head 端口（外部 worker 接入用） |
| `get_cluster_resources()` | `dict` | 查询调度器注册的节点、资源与未注册 Ray 节点 |
| `get_cluster_queues()` | `dict` | 查询调度队列与运行中任务诊断信息 |
| `start_llm_instance(model: str)` | `instance_id (str)` | 在集群里拉起一个 LLM 推理实例 |
| `stop_llm_instance(instance_id)` | `dict` | 关闭推理实例 |
| `query_llm_instance(query, instance_id)` | `str` | 通过 OpenAI 客户端查询实例（completion） |

---

### 1.3 `MaWorkflow`（静态 DAG）

`MaWorkflow` 是本地 DAG 草稿。添加 task 时不访问 Core，`run()` 才通过
`POST /workflows/submit` 一次提交完整节点、边和运行配置。

| 方法 | 说明 |
|---|---|
| `add_task(task_func, inputs=None, task_name=None)` | 添加 `@task` 节点；`TaskOutput` 输入会自动生成依赖边 |
| `get_tasks() -> list[dict]` | 列出草稿中的任务 |
| `run(file_context=None, workspace_dir=None, artifact_mode=False, timeout_seconds=None, tags=None, metadata=None, inputs=None, run_id=None) -> str` | 原子提交 DAG 并返回 Core `run_id`；复用 `run_id` 时只接受完全相同的提交 |

执行状态、事件、结果和取消统一使用 `MaClient.get_run()`、`wait_run()`、
`stream_run()` 和 `cancel_run()`。

### 1.4 `MaTask` / `TaskOutput` / `TaskOutputs`

`MaTask` 只是本地节点句柄；`task.outputs["name"]` 返回 `TaskOutput`，可直接作为
下游 task 的输入。任务保存、删除和边创建不再通过独立 HTTP 接口。
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

### 1.6 `LanggraphClient`（LangGraph 桥）

```python
from maze import LanggraphClient

lc = LanggraphClient(addr="localhost:8000")

@lc.task(resources={"cpu_num": 1})
def node(state):
    ...
    return state
```

- 构造和装饰阶段只在本地生成稳定的一节点 DAG，不访问 Core。
- 调用被装饰函数时，框架通过 `POST /workflows/submit` 提交标准静态 Run，并通过 `GET /runs/{run_id}` 等待结果。
- 每次调用都会出现在统一 Runs、events、cancel、retry 和检查界面中，并带有 `langgraph` 标签。
- 资源支持 `cpu_num / gpu_mem / io_num` 和兼容别名 `cpu`，默认 `cpu_num=1`。

---

## 二、Head 服务 HTTP & WebSocket API

由 `maze start --head --port <PORT>` 启动，默认 `http://localhost:8000`，CORS 全开。
代码：`maze/core/server.py`。

> 所有响应都遵循 `{"status": "success" \| "error", ...}` 约定；错误时返回 HTTP 500 并附 `detail`。

### 2.1 静态 Workflow

#### `POST /apps/validate`
校验 AppSpec/RunSpec，不执行。

```json
{
  "spec": {
    "schema": "maze.app/v1",
    "name": "demo-app",
    "tasks": []
  },
  "source_path": "/optional/source/path.yaml",
  "workspace_dir": "/optional/workspace",
  "timeout_seconds": 300
}
```

**响应**：

```json
{"status": "success", "spec": { "...": "normalized app spec" }}
```

#### `POST /apps/run`
校验、构建并执行 AppSpec/RunSpec。

请求体字段：

| 字段 | 说明 |
|---|---|
| `spec` | AppSpec/RunSpec payload；也可以直接把 spec 放在请求顶层 |
| `source_path` | 可选，spec 来源路径，用于相对路径解析 |
| `workspace_dir` | 可选，覆盖 spec 中的 workspace |
| `artifact_mode` | 默认 `true`，自动使用 Head artifact store |
| `timeout_seconds` | 可选，覆盖 run 超时 |
| `tags` / `metadata` | 追加 run 标签和元数据 |

**响应**：

```json
{
  "status": "success",
  "run_id": "<uuid>",
  "workflow_id": "<uuid>",
  "spec": { "...": "normalized app spec" }
}
```

---

### 2.3 外部 DAG WorkflowSpec

#### `POST /workflows/validate`
校验外部可视化平台提交的完整 DAG spec，不执行。

```json
{
  "spec": {
    "schema": "maze.workflow/v1",
    "name": "hello-dag",
    "nodes": [
      {
        "id": "greet",
        "task_name": "greet",
        "code": "def greet(name='Maze'):\n    return {'message': f'Hello {name}'}",
        "inputs": {"name": "Maze"},
        "outputs": ["message"],
        "resources": {"cpu": 1, "cpu_mem": 128}
      },
      {
        "id": "upper",
        "task_name": "upper",
        "code": "def upper(message):\n    return {'upper': message.upper()}",
        "inputs": {"message": {"from": "greet.message"}},
        "outputs": ["upper"]
      }
    ],
    "edges": [{"from": "greet.message", "to": "upper.message"}],
    "run": {"artifact_mode": true}
  }
}
```

响应：

```json
{"status": "success", "spec": { "...": "normalized spec" }}
```

---

#### `POST /workflows/submit`
外部 DAG 平台推荐使用的稳定提交接口。Maze 会一次性校验、构建静态 workflow 并提交执行。

请求体同 `/workflows/validate`，也可额外传 `tags`、`metadata`、`artifact_mode`。
Python Workflow 会在 spec 中携带稳定的 `workflow_id`、`input_contract` 和
`final_output_refs`；每次执行的 `inputs`、超时和幂等字段位于 `spec.run`。

响应：

```json
{
  "status": "success",
  "workflow_id": "<uuid>",
  "run_id": "<uuid>",
  "spec": { "...": "normalized spec" }
}
```

提交后使用统一 run API 查询状态：`GET /runs/{run_id}`、`GET /runs/{run_id}/tasks`、`GET /runs/{run_id}/events?after=<seq>`、`GET /runs/{run_id}/artifacts`。

---

### 2.4 统一 Run / Artifact / Cluster API

统一 Run API 同时覆盖静态 workflow、AppSpec run 和 dynamic run；前端 Runs 面板优先使用这组接口。

| Method | Path | 说明 |
|---|---|---|
| GET | `/runs?status=&kind=&limit=&detail=` | 列出 run；`kind` 可用于区分 static / dynamic / app 等类型 |
| GET | `/runs/{run_id}` | 获取单个 run 快照 |
| GET | `/runs/{run_id}/tasks` | 获取 run 内所有 task |
| GET | `/runs/{run_id}/tasks/{task_id}` | 获取单个 task |
| GET | `/runs/{run_id}/events?after=<seq>` | 增量读取事件 |
| GET | `/runs/{run_id}/logs?tail=500&task_id=` | 读取 run 或指定 task 的日志尾部 |
| GET | `/runs/{run_id}/artifacts` | 获取 run 产物列表 |
| GET | `/runs/{run_id}/tasks/{task_id}/artifacts` | 获取某个 task 的产物列表 |
| POST | `/runs/{run_id}/cancel` | 取消静态或动态 run；请求体可传 `{"reason": "..."}` |
| POST | `/runs/{run_id}/retry` | 重跑 AppSpec run；普通 DAG/static run 不支持该端点重跑 |

Artifact store 按 sha256 存储二进制 blob：

| Method | Path | 说明 |
|---|---|---|
| PUT | `/artifacts/sha256/{sha256}` | 上传 blob；服务端会校验 body 的 sha256 |
| HEAD | `/artifacts/sha256/{sha256}` | 判断 blob 是否存在并返回 metadata header/body |
| GET | `/artifacts/sha256/{sha256}/metadata` | 获取 metadata |
| GET | `/artifacts/sha256/{sha256}` | 下载 blob |

集群诊断：

| Method | Path | 说明 |
|---|---|---|
| GET | `/cluster/resources` | 调度器视角下的节点、资源、GPU、未注册 Ray 节点 |
| GET | `/cluster/queues` | 等待队列、运行中任务、调度诊断 |
| GET | `/cluster/join_command?host=` | 为未注册 worker 生成推荐 `maze start --worker` 命令 |
| POST | `/cluster/reconcile_workers` | 返回未注册 Ray 节点及推荐接入命令，不会自动执行 |

---

### 2.5 Dynamic Run

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
{"type": "domain_progress", "data": {"completed": 3, "total": 10}}
```

---

#### `PATCH /dynamic_runs/{run_id}/metadata`
合并更新 dynamic run metadata。

```json
{"metadata": {"key": "value"}}
```

---

#### `POST /dynamic_runs/{run_id}/permission_requests`
创建一个动态 run 权限请求，通常由工具执行前发起。

```json
{
  "tool_name": "write_file",
  "action": "write",
  "payload": {"path": "outputs/report.txt"},
  "reason": "需要写入分析结果"
}
```

---

#### `GET /dynamic_runs/{run_id}/permission_requests/{request_id}`
查询单个权限请求。

---

#### `POST /dynamic_runs/{run_id}/permission_requests/{request_id}/decision`
对权限请求做决策。

```json
{
  "decision": {
    "action": "allow",
    "reason": "用户确认",
    "decided_by": "playground"
  }
}
```

`action` 取值：`allow` 或 `deny`。

---

#### `WS /dynamic_runs/{run_id}/events`
实时事件流（含 `register_task_spec`、`append_task`、`task_ready`、`start_task`、`finish_task`、`task_exception`、`finish_workflow`、`cancel_dynamic_run`、`timeout_dynamic_run`、`interrupt_dynamic_run` 等，参见 [事件协议](#五事件协议event-protocol)）。

---

### 2.6 LangGraph 适配器

LangGraph 不再拥有专用 Head 接口。`LanggraphClient` 将函数和调用参数编码成标准的一节点 DAG，通过 `POST /workflows/submit` 提交，并从 `GET /runs/{run_id}` 读取结果。

---

### 2.7 Worker / LLM 实例（兼容接口）

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
           --strategy least-loaded \
           [--playground] \
           [--playground-port 5173] \
           [--playground-backend-port 3001] \
           [--log-level INFO] [--log-file /path/to/log]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--port` | 8000 | Maze Head FastAPI 端口 |
| `--ray-head-port` | 6379 | Ray Head 的 GCS 端口 |
| `--strategy` | `least-loaded` | 调度策略；常用值包括 `least-loaded`、`Default`、`HACS`、`ATLAS` |
| `--playground` |  | 随 Head 一起拉起 Workbench 前端和 Node.js 后端 |
| `--playground-port` | 5173 | Workbench 页面入口端口 |
| `--playground-backend-port` | 3001 | Workbench 后端 API 端口；如果未设置且修改了 `--playground-port`，默认使用 `--playground-port + 1` |
| `--log-level` | `INFO` | `DEBUG/INFO/WARNING/ERROR/CRITICAL` |
| `--log-file` |  | 写入文件 |

示例：

```bash
# 默认一行启动。
maze start --head --port 8000 --ray-head-port 6379 --playground

# 自定义端口。CLI 会自动把 Workbench 后端连到所选 Maze Head，
# 也会自动把前端代理连到所选 Workbench 后端。
maze start --head \
           --port 9000 \
           --ray-head-port 6380 \
           --playground \
           --playground-port 5174
```

当使用 `--playground-port 5174` 时，Workbench 后端默认使用 `5175`。
只有需要固定后端端口时才需要显式设置 `--playground-backend-port`。
Maze 会在启动前检查端口；如果端口已被占用，或者两个服务配置到了同一个端口，会直接给出明确错误提示。

#### Worker 模式
```bash
maze start --worker --addr <HEAD_IP>:<HEAD_PORT>
```

### 3.2 `maze stop`

```bash
maze stop [--log-level INFO] [--log-file ...]
```

停止本机的 Worker。

### 3.3 `maze-sandbox`（已退役）

旧的远程 Sandbox 服务已经删除。该命令仅作为兼容提示保留，执行后会给出迁移说明并退出。
请改用 `maze start --head --playground` 启动维护中的工作流编辑器。

---

## 四、Maze Playground 后端 REST API

由 `maze start --head --playground` 拉起，运行在 Node.js，代码：`web/maze_playground/backend/src/server.js`。Workbench 前端调它，它再调用 Maze Head 和 Python 桥 `maze_bridge.py`。

默认端口：

| 服务 | 默认端口 |
|---|---|
| Maze Head API | 8000 |
| Ray Head GCS | 6379 |
| Workbench 前端 | 5173 |
| Workbench 后端 | 3001 |

使用自定义端口时，CLI 会自动为 Workbench 后端设置 `MAZE_CORE_URL`，并自动为前端设置 `VITE_MAZE_BACKEND_URL`。普通用户通常不需要手动 export 这些环境变量。

环境变量：

| 变量 | 说明 |
|---|---|
| `MAZE_WORKSPACE_ROOT_DIR` | workspace 根目录（默认项目根下 `workspaces/`） |
| `MAZE_WORKSPACES_DIR` | 多 workspace 存放目录；未设置时等于 `MAZE_WORKSPACE_ROOT_DIR` |
| `MAZE_WORKSPACE_DIR` | 兼容旧配置，也会参与 workspace root 推断 |
| `MAZE_DEFAULT_WORKSPACE_ID` | 默认 workspace id，默认 `default` |
| `MAZE_SYSTEM_CATALOG_DIR` | 系统任务 / workflow 模板目录，默认项目根下 `system_catalog/` |
| `MAZE_CORE_URL` | Maze Head 地址（默认 `http://localhost:8000`） |
| `PYTHON_BIN` / `MAZE_CONDA_PREFIX` / `CONDA_PREFIX` | Python 解释器（被 Python 桥调用时使用） |

---

### 4.1 Workspaces / System Catalog

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/workspaces` | 创建 workspace；请求体可传 `workspaceId`、`name`、`mode` |
| GET | `/api/workspaces/current?workspaceId=&workspaceDir=` | 获取当前 workspace manifest 与目录 |
| GET | `/api/workspaces/:workspaceId` | 按 id 获取 workspace |
| GET | `/api/system-catalog?type=workflows|tasks` | 列出系统目录中的 workflow/task 模板 |
| POST | `/api/system-catalog/import` | 把系统 task 或 workflow JSON 复制到 workspace |
| POST | `/api/system-catalog/workflows/load` | 加载系统 workflow 模板到画布，同时把随附 task definitions 导入 workspace tasks |
| GET | `/api/workspace-policy` | 读取 workspace sandbox policy |
| PUT | `/api/workspace-policy` | 更新 workspace sandbox policy |

`/api/system-catalog/workflows/load` 请求体：

```json
{
  "workspaceId": "default",
  "workspaceDir": "/optional/workspace/path",
  "sourceId": "resource_mix_demo.json"
}
```

该接口返回 `workflow` 和 `importedTaskDefinitions`。它会把模板依赖的 task 定义导入 workspace，但 workflow 本身作为未保存草稿加载到画布；用户保存后才进入 workspace workflows。

---

### 4.2 任务管理

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/system-catalog?type=tasks` | 列出 canonical 内置任务源码 |
| GET | `/api/workspace-tasks` | 列出 `workspace/tasks/` 下用户任务 |
| POST | `/api/workspace-tasks` | 保存（新增/覆盖）一个 workspace task 的 Python 源码 |
| DELETE | `/api/workspace-tasks` | 删除一个 workspace task |
| PATCH | `/api/workspace-tasks/rename` | 重命名 task |

请求体里 task 路径都使用相对于 `workspace/tasks/` 的路径。

---

### 4.3 Workspace 文件管理

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/workspace-files?path=` | 列目录 |
| POST | `/api/workspace-files/upload` | 上传文件（multipart 或 base64 字段） |
| POST | `/api/workspace-files/mkdir` | 创建目录 |
| DELETE | `/api/workspace-files` | 删除文件/目录 |
| GET | `/api/workspace-files/preview?path=` | 预览文件（自动识别文本 / 图片 / 表格） |
| GET | `/api/workspace-files/download?path=` | 下载（二进制） |
| PUT | `/api/local-workspaces/:workspaceId/manifest` | 写入本地 workspace manifest |
| GET | `/api/local-workspaces/:workspaceId/manifest` | 读取本地 workspace manifest |
| POST | `/api/workspace-files/missing` | 检查一组文件路径是否缺失 |
| POST | `/api/artifacts/promote` | 将 Core SHA-256 artifact 复制到 Workspace Files |

---

### 4.4 LLM 集成

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

### 4.5 Workspace Workflows

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/workspace-workflows` | 列出已保存 workflow |
| DELETE | `/api/workspace-workflows` | 删除 workflow |
| PATCH | `/api/workspace-workflows/rename` | 重命名 |
| POST | `/api/workspace-workflows/save` | 保存（前端 JSON 蓝图） |
| POST | `/api/workspace-workflows/load` | 加载到画布 |
| POST | `/api/workspace-workflows/import` | 从上传的 JSON 文件导入 |

---

### 4.6 Runs / Cluster / Artifacts 视图

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/runs?status=&kind=&limit=&detail=` | 代理 Head 统一 `/runs` |
| GET | `/api/runs/:runId` | 统一 run 详情 |
| GET | `/api/runs/:runId/tasks` | run 内 task 列表 |
| GET | `/api/runs/:runId/tasks/:taskId` | 单个 task 状态 |
| GET | `/api/runs/:runId/events?after=` | run 事件 |
| GET | `/api/runs/:runId/logs?tail=&taskId=` | run/task 日志 |
| GET | `/api/runs/:runId/artifacts` | run artifact 列表 |
| GET | `/api/runs/:runId/tasks/:taskId/artifacts` | task artifact 列表 |
| POST | `/api/runs/:runId/cancel` | 取消 run |
| POST | `/api/runs/:runId/retry` | 重跑 AppSpec run |
| GET | `/api/cluster/resources` | 代理 Head `/cluster/resources` |
| GET | `/api/cluster/queues` | 代理 Head `/cluster/queues` |
| GET | `/api/artifacts/sha256/:sha256/metadata` | 代理 Head artifact metadata |
| GET | `/api/artifacts/sha256/:sha256?disposition=inline|attachment` | 下载或内联打开 sha256 artifact |
| GET | `/api/dynamic-runs` | 列出所有 dynamic runs |
| GET | `/api/dynamic-runs/:runId` | 单个 run 详情 |
| GET | `/api/dynamic-runs/:runId/events` | 事件列表（HTTP） |
| POST | `/api/dynamic-runs/:runId/events` | 写入事件（透传给 Maze Head） |
| POST | `/api/dynamic-runs/:runId/permission-requests/:requestId/decision` | Workbench 对动态 run 权限请求做 allow/deny 决策 |
| DELETE | `/api/dynamic-runs/:runId` | 删除 |
| POST | `/api/dynamic-runs/cleanup` | 批量清理 |
---

### 4.7 编辑器

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/parse-custom-function` | 解析用户上传/粘贴的 Python 源码，抽取 `@task` 元数据 |
| POST | `/api/workflows/:id/run` | 编译请求中携带的 workflow 并提交 Maze Core，返回 Core `run_id` |

---

### 4.8 健康检查

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
out = client.wait_run(run_id)
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

### 8.3 LangGraph 迁移

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

### 8.4 直接调用 HTTP API（curl）

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

## 九、运行状态查询与指标上报

> 这一组接口和约定让你能从外部观察 run 的运行情况：当前有多少 DAG 在跑、每个 run 跑到哪一步、每个 task 的状态/耗时/节点/metrics、累计消耗了多少 token。推荐优先使用统一 `/runs/*` API，它覆盖静态 workflow、AppSpec run 和 dynamic run。`/v1/*` 这组接口是静态 workflow 的观测补充，主要服务全局 metrics、当前 task 快照和兼容旧监控脚本。

### 9.1 状态机

**Run 状态**（一次 `wf.run()` 提交后产生一个 run，run_id = submit_id）：

```
运行中：created / running
终态：succeeded / failed / cancelled / timed_out / interrupted
```

- `created`：已提交但尚未收到 task 开始事件。
- `running`：至少一个 task 收到了 `start_task` 事件。
- 终态：`succeeded` / `failed` / `cancelled` / `timed_out` / `interrupted`。

**Task 状态**：`pending` / `queued` / `running` / `succeeded` / `failed` / `cancelled` / `timed_out`。

### 9.2 持久化

静态 run 的持久化由 Maze Core 独占，每个 run 在 Core workspace 下有自己的目录：

```
workspace/workflow_runs/static_runs/{run_id}/
  ├── run.json          # 最新快照
  └── events.jsonl      # 事件流（append-only）
```

环境变量 `MAZE_WORKSPACE_DIR` 可覆盖默认 workspace 路径。

### 9.3 HTTP 接口

#### `GET /v1/metrics`

集群级聚合指标。

```bash
curl http://localhost:8000/v1/metrics
```

返回示例：

```json
{
  "uptime_sec": 3600,
  "started_at": 1716543000.0,
  "workflows": {
    "created_total": 12,
    "in_memory_not_submitted": 2
  },
  "static_runs": {
    "total": 10,
    "in_memory": 1,
    "by_status": {"submitted": 0, "running": 1, "succeeded": 8, "failed": 1, "canceled": 0, "interrupted": 0}
  },
  "tasks": {
    "total_finished": 47,
    "by_status": {"running": 2, "succeeded": 44, "failed": 1, "canceled": 0}
  },
  "tokens": {
    "in": 12345,
    "out": 6789,
    "cost_usd": 0.054321,
    "by_model": {"qwen3-30b": {"tokens_in": 12345, "tokens_out": 6789, "calls": 8}}
  }
}
```

#### `GET /v1/runs?status=running&limit=20&offset=0`

列出 run（按创建时间倒序）。`status` 可省略，可选值见状态机。

#### `GET /v1/runs/{run_id}/snapshot`

单个 run 的完整快照。快照包含 run 状态、时间、进度、task 节点、task 结果摘要、task metrics、错误摘要等。

统一 run API 也提供同类数据：

```bash
curl http://localhost:8000/runs/<run_id>
```

#### `GET /v1/runs/{run_id}/current-task`

最常用接口——快速回答"这个 DAG 现在跑到哪一步了"。

```json
{
  "run_id": "abc-123",
  "status": "running",
  "running": [
    {"task_id": "t1", "task_name": "summarize", "started_time": 1716543210.0,
     "node_id": "node-01"}
  ],
  "pending_count": 3,
  "done_count": 5,
  "task_total": 9
}
```

#### `GET /v1/runs/{run_id}/tasks`

所有 task 的状态 + metrics 字典。兼容响应继续使用旧字段名 `task_total` 和
`tasks`，其内容分别来自当前快照的 `task_counts.total` 和 `task_nodes`。统一 run
API 也支持：

```bash
curl http://localhost:8000/runs/<run_id>/tasks
```

单个 task 示例字段：

```json
{
  "task_id": "produce",
  "task_name": "produce",
  "status": "succeeded",
  "started_time": 1780547600.0,
  "finished_time": 1780547600.5,
  "duration_seconds": 0.5,
  "duration_ms": 500,
  "selected_node": {"node_id": "node-a", "node_ip": "127.0.0.1", "gpu_id": null},
  "result_summary": {"message": "Hello Maze"},
  "metrics": {
    "tokens_in": 11,
    "tokens_out": 7,
    "cost_usd": 0.123,
    "model": "test-model"
  }
}
```

#### `GET /v1/runs/{run_id}/timeline?after=10`

按 `seq` 排序的事件流。`after` 可选，用于增量拉取。统一 run API 也支持：

```bash
curl "http://localhost:8000/runs/<run_id>/events?after=10"
```

常见事件类型：`start_workflow` / `start_task` / `finish_task` / `task_exception` / `finish_workflow` / `cancel_workflow` / `run_interrupted`。

### 9.4 CLI

```bash
# 一次性查看
maze status

# 持续刷新
maze status --watch

# 只看运行中的 run
maze status --status running

# 查看某个具体 run 的细节
maze status --run-id <run_id>

# 指向远程 head
maze status --addr http://10.0.0.1:8000
```

### 9.5 Token / 指标上报（用户视角）

Maze 自身**不调用** LLM，因此 token 消耗只能由 task 函数主动上报。提供两条上报通道，二选一或并用：

#### 通道 A：`maze.metrics.report()` 函数

```python
from maze import task, metrics

@task
def call_llm(prompt: str = ""):
    response = openai_client.chat.completions.create(...)
    metrics.report(
        tokens_in=response.usage.prompt_tokens,
        tokens_out=response.usage.completion_tokens,
        model=response.model,
        cost_usd=0.012,         # 可选
    )
    return {"answer": response.choices[0].message.content}
```

同一 task 内可以多次调用，数值字段会累加；同一 `model` 还会在 `by_model` 桶里独立累加 `calls`。

#### 通道 B：`return` 字段塞 `__maze_metrics__`

```python
@task
def call_llm(prompt: str = ""):
    response = openai_client.chat.completions.create(...)
    return {
        "answer": response.choices[0].message.content,
        "__maze_metrics__": {
            "tokens_in": response.usage.prompt_tokens,
            "tokens_out": response.usage.completion_tokens,
            "model": response.model,
        }
    }
```

框架会把 `__maze_metrics__` 字段自动剥离（不会污染下游 task 的输入），并合并到 task 的 `metrics` 里。

#### 上报字段约定

| 字段 | 类型 | 说明 |
|---|---|---|
| `tokens_in` | int | 累加 |
| `tokens_out` | int | 累加 |
| `cost_usd` | float | 累加 |
| `model` | str | 触发 `by_model` 分桶 |
| 其它任意键 | any | 数值累加，非数值后写覆盖 |

### 9.6 结构化日志

设置 `MAZE_LOG_FORMAT=json`，日志将以 JSON 行格式输出，方便接 Loki / ELK：

```bash
MAZE_LOG_FORMAT=json maze start --head --port 8000
```

每行示例：

```json
{"ts":"2026-05-23T10:00:00Z","level":"INFO","logger":"maze.core.path.path","msg":"..."}
```

未设置时仍是默认人类可读格式。

### 9.7 端到端验证命令

下面命令可以完整验证静态 DAG 监控链路：DAG 提交、run 状态、task metrics、events、global metrics、CLI status。

终端 A 启动 Head：

```bash
conda activate maze
python -m maze.cli.cli start --head --port 8000 --ray-head-port 6379
```

终端 B 创建测试 DAG：

```bash
cat > /tmp/observability_dag.json <<'JSON'
{
  "spec": {
    "schema": "maze.workflow/v1",
    "name": "observability-dag",
    "nodes": [
      {
        "id": "produce",
        "task_name": "produce",
        "code": "def produce(name='Maze'):\n    return {'message': f'Hello {name}', '__maze_metrics__': {'tokens_in': 11, 'tokens_out': 7, 'cost_usd': 0.123, 'model': 'test-model'}}",
        "inputs": {"name": "Maze"},
        "outputs": ["message"],
        "resources": {"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0}
      },
      {
        "id": "consume",
        "task_name": "consume",
        "code": "def consume(message):\n    return {'upper': message.upper(), '__maze_metrics__': {'tokens_in': 3, 'tokens_out': 2, 'cost_usd': 0.01, 'model': 'test-model'}}",
        "inputs": {"message": {"from": "produce.message"}},
        "outputs": ["upper"],
        "resources": {"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0}
      }
    ],
    "edges": [{"from": "produce.message", "to": "consume.message"}],
    "run": {"artifact_mode": true, "timeout_seconds": 60},
    "tags": ["observability-test"],
    "metadata": {"purpose": "manual-observability-test"}
  }
}
JSON
```

校验并提交：

```bash
curl -sS -X POST http://localhost:8000/workflows/validate \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/observability_dag.json | python -m json.tool

curl -sS -X POST http://localhost:8000/workflows/submit \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/observability_dag.json \
  | tee /tmp/maze_submit.json | python -m json.tool

RUN_ID=$(python - <<'PY'
import json
print(json.load(open("/tmp/maze_submit.json"))["run_id"])
PY
)
echo "$RUN_ID"
```

查询 run、tasks、events、global metrics：

```bash
curl -sS "http://localhost:8000/runs/$RUN_ID" | python -m json.tool
curl -sS "http://localhost:8000/runs/$RUN_ID/tasks" | python -m json.tool
curl -sS "http://localhost:8000/runs/$RUN_ID/events" | python -m json.tool
curl -sS "http://localhost:8000/v1/metrics" | python -m json.tool
conda run -n maze python -m maze.cli.cli status --addr http://localhost:8000
```

预期结果：

- run 最终 `status=succeeded`
- `task_counts.total=2`，`task_counts.succeeded=2`
- `produce.metrics.tokens_in=11`，`consume.metrics.tokens_in=3`
- 全局 `tokens.in=14`，`tokens.out=9`，`test-model.calls=2`

### 9.8 兼容性与边界

- 动态 run 可以走统一 `/runs/*` 查询；需要动态追加任务、权限请求和详细事件时，仍使用 `DynamicRun` SDK 和 `/dynamic_runs/*`。
- 本节接口的 schema 字段名是稳定的，未来扩展会向后兼容。
- Token 数据可信度依赖用户上报；框架不做 LLM 流量拦截。
- Head 进程崩溃 / 重启时，正在跑的 run 会在下次启动时被标记为 `interrupted` 并写入 `events.jsonl`。

---

## 十、参考

- 项目主仓库：<https://github.com/QinbinLi/Maze>
- 在线文档：<https://maze-doc-new.readthedocs.io/>
- 官网：<https://mazeagent.net/>
- 关键源码：
  - 装饰器：`maze/client/maze/decorator.py`
  - 静态 workflow SDK：`maze/client/maze/workflow.py`
  - 动态 run SDK：`maze/client/maze/dynamic.py`
  - LangGraph 桥：`maze/client/langgraph/client.py`
  - Head 服务：`maze/core/server.py`
  - 调度器：`maze/core/scheduler/`
  - 静态 run 持久化与事件：`maze/core/workflow/static_run.py`
  - 动态运行模型与事件：`maze/core/workflow/dynamic.py`、`maze/core/workflow/dynamic_store.py`
  - 指标上报：`maze/metrics/`
  - CLI：`maze/cli/cli.py`、`maze/cli/sandbox_cli.py`
  - Workbench 后端：`web/maze_playground/backend/src/server.js`
