# Maze API Reference

> Version: `maze-agent` 1.0.2, Python >= 3.10
> Scope: task-level distributed Agent / Workflow development and integration.
> Last updated: 2026-06-20. This version includes AppSpec, unified Run/Artifact APIs, Workbench System Catalog, and artifact open/download routes.

This document summarizes the public Maze APIs across four layers:

1. [Python SDK](#1-python-sdk-api): define tasks, build workflows, and run agents.
2. [Head HTTP and WebSocket API](#2-head-http-and-websocket-api): FastAPI endpoints exposed by the Maze Head service.
3. [CLI Commands](#3-cli-commands): `maze start`, `maze stop`, `maze status`, `maze doctor`, and the retired `maze-sandbox` compatibility command.
4. [Maze Workbench Backend REST API](#4-maze-workbench-backend-rest-api): APIs used by the visual Workbench.

Additional sections cover the [event protocol](#5-event-protocol), [resource configuration](#6-resource-configuration), [error handling](#7-error-handling), [examples](#8-complete-examples), and [observability APIs](#9-run-status-metrics-and-observability).

---

## 1. Python SDK API

Main imports:

```python
from maze import (
    MaClient, MaWorkflow, MaTask, TaskOutput, TaskOutputs,
    DynamicRun, DynamicTaskSpec, DynamicTaskInvocation,
    LanggraphClient,
    task, get_task_metadata,
)
```

### 1.1 `@task` decorator

```python
from maze import task
```

#### Signature

```python
task(func: Callable = None, *,
     resources: Dict[str, Any] | None = None,
     data_types: Dict[str, str] | None = None,
     max_retries: int | None = None,
     retry_backoff_seconds: float = 0,
     retry_on: list[str] | None = None,
     timeout_seconds: float | None = None) -> Callable
```

#### Behavior

- Marks a normal Python function as a Maze task and attaches `TaskMetadata`.
- Inputs are inferred from the function signature.
- Outputs are inferred from string keys in `return {...}` dict literals.
- The task must return a dict at runtime, otherwise Maze raises `TypeError`.
- Default resources are `{"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}`.
- If GPU fields are not explicitly declared, Maze may infer GPU usage from imports and function body.
- `*args`, `**kwargs`, and positional-only parameters are not supported. Use explicit named parameters.

#### Parameters

| Parameter | Type | Description |
|---|---|---|
| `resources` | `dict` | Resource requirements. Keys: `cpu`, `cpu_mem`, `gpu`, `gpu_mem`. |
| `data_types` | `dict[str, str]` | Overrides inferred input/output type strings. |
| `max_retries` | `int | None` | Maximum retries for this task. `None` means server default behavior. |
| `retry_backoff_seconds` | `float` | Delay before retrying a failed task. |
| `retry_on` | `list[str] | None` | Optional allowlist of exception/error names that trigger retry. |
| `timeout_seconds` | `float | None` | Per-task execution timeout. |

#### Example

```python
@task(resources={"cpu": 1, "cpu_mem": 128})
def greet(text: str = ""):
    return {"result": f"Hello {text}"}
```

#### Helper

```python
get_task_metadata(func) -> TaskMetadata
```

Main metadata fields:

```text
func_name / code_str / code_ser / inputs / outputs / resources / data_types /
max_retries / retry_backoff_seconds / retry_on / timeout_seconds
```

`code_ser` is a base64-encoded `cloudpickle` payload.

---

### 1.2 `MaClient`

```python
class MaClient:
    def __init__(self, server_url: str = "http://localhost:8000")
```

| Method | Returns | Description |
|---|---|---|
| `create_workflow()` | `MaWorkflow` | Create a local static DAG draft. |
| `create_workflow_from(workflow_def, inputs=None)` | `MaWorkflow` | Build a static workflow from a `@workflow` definition. |
| `run_app(spec, workspace_dir=None, artifact_mode=True, timeout_seconds=None, tags=None, metadata=None)` | `dict` | Submit an AppSpec/RunSpec through `/apps/run`. |
| `validate_workflow_spec(spec)` | `dict` | Validate an external DAG spec through `/workflows/validate`. |
| `submit_workflow(spec, artifact_mode=True, tags=None, metadata=None)` | `dict` | Submit an external DAG spec and return `workflow_id` and `run_id`. |
| `create_dynamic_run(max_tasks=100, timeout_seconds=None, file_context=None, workspace_dir=None, artifact_mode=False, metadata=None)` | `DynamicRun` | Create a dynamic run. |
| `get_dynamic_run(run_id)` | `DynamicRun` | Attach to an existing dynamic run. |
| `list_dynamic_runs(status=None, limit=None, detail=False)` | `list[dict]` | List dynamic runs, optionally filtered by status. |
| `delete_dynamic_run(run_id)` | `dict` | Delete one dynamic run and its stored history. |
| `cleanup_dynamic_runs(statuses=None, older_than_days=None, dry_run=True)` | `dict` | Bulk cleanup. `dry_run=True` only reports candidates. |
| `list_runs(status=None, kind=None, limit=None, detail=False)` | `list[dict]` | Unified listing for static, dynamic, and app runs. |
| `get_run(run_id)` | `dict` | Get a unified run snapshot. |
| `get_run_tasks(run_id)` / `get_run_task(run_id, task_id)` | `list[dict]` / `dict` | Inspect run tasks. |
| `get_run_artifacts(run_id)` / `get_run_task_artifacts(run_id, task_id)` | `list[dict]` | Inspect run or task artifacts. |
| `get_run_events(run_id, after=None)` | `list[dict]` | Read run events, optionally after a sequence number. |
| `get_run_logs(run_id, tail=500, task_id=None)` | `dict` | Read run or task log tail. |
| `cancel_run(run_id, reason=None)` | `dict` | Cancel a static or dynamic run. |
| `retry_run(run_id, ...)` | `dict` | Retry an AppSpec run. Only AppSpec runs are supported. |
| `wait_run(run_id, timeout=None, poll_interval=0.5)` | `dict` | Poll until the run reaches a terminal state. |
| `stream_run(run_id, poll_interval=0.2)` | `Iterator[dict]` | Yield events until a terminal event/state appears. |
| `get_ray_head_port()` | `dict` | Return Ray Head port information for worker connection. |
| `get_cluster_resources()` | `dict` | Inspect scheduler-registered nodes, resources, GPUs, and unregistered Ray nodes. |
| `get_cluster_queues()` | `dict` | Inspect scheduling queues and running task diagnostics. |
| `start_llm_instance(model)` | `str` | Start an LLM serving instance on the cluster. |
| `stop_llm_instance(instance_id)` | `dict` | Stop an LLM serving instance. |
| `query_llm_instance(query, instance_id)` | `str` | Query an instance through an OpenAI-compatible client. |

---

### 1.3 `MaWorkflow`

`MaWorkflow` is a local DAG draft. Adding tasks does not contact Core.
`run()` submits the complete graph and run configuration once through
`POST /workflows/submit`.

| Method | Description |
|---|---|
| `add_task(task_func, inputs=None, task_name=None)` | Add a decorated task; `TaskOutput` inputs create dependency edges automatically. |
| `get_tasks() -> list[dict]` | List tasks in the local draft. |
| `run(file_context=None, workspace_dir=None, artifact_mode=False, timeout_seconds=None, tags=None, metadata=None, inputs=None, run_id=None) -> str` | Atomically submit the DAG and return the Core `run_id`. Reusing a `run_id` replays only an identical submission. |

Use `MaClient.get_run()`, `wait_run()`, `stream_run()`, and `cancel_run()`
for run state, events, results, and cancellation.

### 1.4 `MaTask`, `TaskOutput`, and `TaskOutputs`

`MaTask` is a local node handle. `task.outputs["name"]` returns a
`TaskOutput` that can be passed directly to a downstream task input. Tasks,
deletions, and edges are no longer persisted through separate HTTP calls.
### 1.5 `DynamicRun`

Dynamic runs allow the graph to grow at runtime. Create them through `MaClient.create_dynamic_run()` or attach through `MaClient.get_dynamic_run()`.

#### Registering and appending tasks

| Method | Description |
|---|---|
| `register_task_spec(spec)` | Register a reusable task specification. |
| `append_task(invocation)` | Append one task invocation at runtime. |
| `append_task(task_func, inputs=None, parents=None, request_id=None)` | Inline-register and append a `@task` function. |
| `wait_for_task(task_id, timeout=None)` | Wait until one dynamic task completes. |

Inputs may contain normal JSON-like values or output references:

```python
{"__maze_output_ref__": True, "task_id": "<parent-task>", "output_key": "result"}
```

#### Waiting and streaming

| Method | Description |
|---|---|
| `get_snapshot()` | Return the current dynamic run snapshot. |
| `events(after=None)` | Return events after a sequence number. |
| `stream_events(after=None)` | Stream/poll events. |
| `wait(timeout=None)` | Wait until the dynamic run reaches a terminal state. |

#### Lifecycle

| Method | Description |
|---|---|
| `finalize(result=None)` | Mark that no more tasks will be appended. |
| `cancel(reason=None)` | Cancel the dynamic run. |
| `delete()` | Delete persisted state. |

Common statuses:

```text
created / running / finalizing / succeeded / failed / canceled / timed_out / interrupted
```

#### Data classes

```python
DynamicTaskSpec(
    task_spec_id: str | None = None,
    task_name: str | None = None,
    code_str: str | None = None,
    code_ser: str | None = None,
    inputs: list[dict] = ...,
    outputs: list[dict] = ...,
    resources: dict = ...
)

DynamicTaskInvocation(
    task_spec_id: str | None = None,
    task_spec: DynamicTaskSpec | None = None,
    inputs: dict = ...,
    parents: list[str] = ...,
    request_id: str | None = None
)
```

#### Example

```python
from maze import MaClient, task

@task(resources={"cpu": 1, "cpu_mem": 128})
def double(x: int = 1):
    return {"y": x * 2}

client = MaClient("http://localhost:8000")
run = client.create_dynamic_run(max_tasks=10)
task_id = run.append_task(double, inputs={"x": 21})
print(run.wait_for_task(task_id))
run.finalize({"done": True})
print(run.wait())
```

---

### 1.6 `LanggraphClient`

Maze can run LangGraph node functions through the standard static Run API.
Decorating a function creates a local one-node DAG template. Calling it submits
that DAG through `POST /workflows/submit`, waits on `GET /runs/{run_id}`, and
returns the decoded Python value.

| Method | Description |
|---|---|
| `LanggraphClient(...).task(...)` | Decorate a graph node as a Maze task. |

Each invocation is an ordinary Core Run tagged `langgraph`, so the existing
Run events, cancellation, retry, and inspection APIs apply without a separate
LangGraph control plane. Supported resource keys are `cpu_num`, `gpu_mem`,
`io_num`, and the legacy `cpu` alias.

---

## 2. Head HTTP and WebSocket API

Start the Head service with:

```bash
maze start --head --port 8000
```

Default base URL:

```text
http://localhost:8000
```

Implementation: `maze/core/server.py`.

Most responses follow:

```json
{"status": "success", "...": "..."}
```

Errors usually return non-2xx with `detail`.

---

### 2.1 Static Workflow

#### `POST /apps/validate`

Validate an AppSpec/RunSpec without executing it.

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

Response:

```json
{"status": "success", "spec": {"...": "normalized app spec"}}
```

#### `POST /apps/run`

Validate, build, and execute an AppSpec/RunSpec.

| Field | Description |
|---|---|
| `spec` | AppSpec/RunSpec payload. The spec may also be sent at the request root. |
| `source_path` | Optional source path used for relative path resolution. |
| `workspace_dir` | Optional workspace override. |
| `artifact_mode` | Defaults to `true`; uses the Head artifact store. |
| `timeout_seconds` | Optional run timeout override. |
| `tags` / `metadata` | Additional run tags and metadata. |

Response:

```json
{
  "status": "success",
  "run_id": "<uuid>",
  "workflow_id": "<uuid>",
  "spec": {"...": "normalized app spec"}
}
```

---

### 2.3 External DAG WorkflowSpec

#### `POST /workflows/validate`

Validate a complete external DAG spec without running it.

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

Response:

```json
{"status": "success", "spec": {"...": "normalized spec"}}
```

#### `POST /workflows/submit`

Recommended stable endpoint for external visual DAG builders. Maze validates the spec, builds a static workflow, and submits a run.

The body is the same as `/workflows/validate`. You may additionally pass `tags`, `metadata`, and `artifact_mode`.
Python workflows include a stable `workflow_id`, `input_contract`, and
`final_output_refs` in the spec. Per-run inputs, timeout, and idempotency fields
live under `spec.run`.

Response:

```json
{
  "status": "success",
  "workflow_id": "<uuid>",
  "run_id": "<uuid>",
  "spec": {"...": "normalized spec"}
}
```

Use the unified run API after submission:

```text
GET /runs/{run_id}
GET /runs/{run_id}/tasks
GET /runs/{run_id}/events?after=<seq>
GET /runs/{run_id}/artifacts
```

---

### 2.4 Unified Run, Artifact, and Cluster API

The unified Run API covers static workflows, AppSpec runs, and dynamic runs. Workbench uses these endpoints for the Runs panel.

| Method | Path | Description |
|---|---|---|
| GET | `/runs?status=&kind=&limit=&detail=` | List runs. `kind` can distinguish static, dynamic, app, and other run types. |
| GET | `/runs/{run_id}` | Get one run snapshot. |
| GET | `/runs/{run_id}/tasks` | Get all tasks in a run. |
| GET | `/runs/{run_id}/tasks/{task_id}` | Get one task snapshot. |
| GET | `/runs/{run_id}/events?after=<seq>` | Read run events incrementally. |
| GET | `/runs/{run_id}/logs?tail=500&task_id=` | Read log tail for a run or task. |
| GET | `/runs/{run_id}/artifacts` | List run artifacts. |
| GET | `/runs/{run_id}/tasks/{task_id}/artifacts` | List artifacts for one task. |
| POST | `/runs/{run_id}/cancel` | Cancel a static or dynamic run. Body may include `{"reason": "..."}`. |
| POST | `/runs/{run_id}/retry` | Retry an AppSpec run. Normal DAG/static runs are not supported by this endpoint. |

Artifact store endpoints:

| Method | Path | Description |
|---|---|---|
| PUT | `/artifacts/sha256/{sha256}` | Upload a blob. The server validates the request body sha256. |
| HEAD | `/artifacts/sha256/{sha256}` | Check existence and metadata. |
| GET | `/artifacts/sha256/{sha256}/metadata` | Get metadata. |
| GET | `/artifacts/sha256/{sha256}` | Download the blob. |

Cluster diagnostics:

| Method | Path | Description |
|---|---|---|
| GET | `/cluster/resources` | Scheduler view of nodes, resources, GPUs, and unregistered Ray nodes. |
| GET | `/cluster/queues` | Waiting queues, running tasks, and scheduling diagnostics. |
| GET | `/cluster/join_command?host=` | Generate recommended `maze start --worker` commands. |
| POST | `/cluster/reconcile_workers` | Return unregistered Ray nodes and recommended commands. Does not execute them. |

---

### 2.5 Dynamic Run

#### `POST /dynamic_runs`

Create a dynamic run.

```json
{"max_tasks": 100, "timeout_seconds": null}
```

Response:

```json
{"status": "success", "run_id": "..."}
```

#### `GET /dynamic_runs?status=&limit=`

List dynamic runs.

```json
{"status": "success", "runs": []}
```

#### `POST /dynamic_runs/cleanup`

Bulk cleanup terminal dynamic runs.

```json
{
  "statuses": ["failed", "canceled"],
  "older_than_days": 7,
  "dry_run": true
}
```

#### `GET /dynamic_runs/{run_id}`

Get a full snapshot, including task specs, tasks, status, metadata, and event sequence.

#### `DELETE /dynamic_runs/{run_id}`

Delete one dynamic run.

#### `POST /dynamic_runs/{run_id}/task_specs`

Register a task spec.

```json
{
  "task_spec_id": "<optional>",
  "task_name": "<optional>",
  "code_str": "<optional>",
  "code_ser": "<base64-cloudpickle>",
  "inputs": [{"name": "x", "data_type": "str"}],
  "outputs": [{"name": "y", "data_type": "any"}],
  "resources": {"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}
}
```

#### `POST /dynamic_runs/{run_id}/append_task`

Append a task at runtime.

```json
{
  "task_spec_id": "<optional registered spec id>",
  "task_spec": {"...": "inline spec if not registered"},
  "inputs": {
    "x": 1,
    "y": {"__maze_output_ref__": true, "task_id": "<parent-task>", "output_key": "out"}
  },
  "parents": ["<extra-parent-task-id>"],
  "request_id": "<optional idempotency key>"
}
```

#### `POST /dynamic_runs/{run_id}/finalize`

```json
{"result": {}}
```

#### `POST /dynamic_runs/{run_id}/cancel`

```json
{"reason": "user_cancel"}
```

#### `GET /dynamic_runs/{run_id}/events?after=<seq>`

Read events where `seq > after`.

#### `POST /dynamic_runs/{run_id}/events`

Write a custom event.

```json
{"type": "domain_progress", "data": {"completed": 3, "total": 10}}
```

#### `PATCH /dynamic_runs/{run_id}/metadata`

Merge-update dynamic run metadata.

```json
{"metadata": {"key": "value"}}
```

#### `WS /dynamic_runs/{run_id}/events`

Real-time dynamic run event stream. Common event types include:

```text
register_task_spec / append_task / task_ready / start_task / finish_task /
task_exception / finish_workflow / cancel_dynamic_run /
timeout_dynamic_run / interrupt_dynamic_run
```

---

### 2.6 LangGraph Adapter

LangGraph has no dedicated Head endpoint. `LanggraphClient` serializes the
callable and invocation values into a standard one-node DAG, submits it with
`POST /workflows/submit`, and reads the result from `GET /runs/{run_id}`.

---

### 2.7 Worker and LLM Compatibility APIs

#### `POST /get_head_ray_port`

```json
{"status": "success", "port": 6379}
```

#### `POST /start_worker`

```json
{
  "node_ip": "192.168.x.x",
  "node_id": "worker-1",
  "resources": {"cpu": 8, "gpu": 1}
}
```

#### `POST /start_llm_instance`

```json
{
  "model": "Qwen2.5-7B",
  "cpu_nums": 5,
  "gpu_nums": 1,
  "memory": 1024,
  "gpu_mem": 16000
}
```

Response:

```json
{
  "status": "success",
  "host": "...",
  "port": 12345,
  "instance_id": "<uuid>"
}
```

The instance exposes an OpenAI-compatible endpoint at:

```text
http://<host>:<port>/v1
```

#### `POST /stop_llm_instance`

```json
{"instance_id": "..."}
```

---

## 3. CLI Commands

Registered scripts in `pyproject.toml`:

```toml
[project.scripts]
maze = "maze.cli.cli:main"
maze-sandbox = "maze.cli.sandbox_cli:main"
```

### 3.1 `maze start`

```bash
maze start --head | --worker [options]
```

#### Head mode

```bash
maze start --head \
           --port 8000 \
           --ray-head-port 6379 \
           --strategy least-loaded \
           [--playground] \
           [--playground-port 5173] \
           [--playground-backend-port 3001] \
           [--detach] \
           [--log-level INFO] [--log-file /path/to/log]
```

| Option | Default | Description |
|---|---|---|
| `--port` | `8000` | Maze Head FastAPI port. |
| `--ray-head-port` | `6379` | Ray Head GCS port. |
| `--strategy` | `least-loaded` | Scheduling strategy. Common values: `least-loaded`, `Default`, `HACS`, `ATLAS`. |
| `--playground` | off | Start the Workbench UI and its Node.js backend together with the head node. |
| `--playground-port` | `5173` | Workbench web UI port. |
| `--playground-backend-port` | `3001` | Workbench backend API port. If omitted and `--playground-port` is changed, defaults to `--playground-port + 1`. |
| `--detach` | off | Run the same Maze Head parent in the background and print its PID and log path. |
| `--log-level` | `INFO` | Logging level. |
| `--log-file` | unset | Write logs to a file. |

Examples:

```bash
# Default one-line startup.
maze start --head --port 8000 --ray-head-port 6379 --playground

# Background startup. Foreground heads use the same runtime state and can also
# be stopped from another terminal with `maze stop`.
maze start --head --port 8000 --detach

# Custom ports. The CLI wires the Workbench backend to the selected Maze Head
# and wires the frontend proxy to the selected Workbench backend.
maze start --head \
           --port 9000 \
           --ray-head-port 6380 \
           --playground \
           --playground-port 5174
```

With `--playground-port 5174`, the Workbench backend defaults to `5175`.
Use `--playground-backend-port` only when that backend port must be fixed.
Maze checks configured ports before startup and prints a clear error when a
port is already in use or two services are configured to share the same port.

#### Worker mode

```bash
maze start --worker --addr <HEAD_IP>:<HEAD_PORT>
```

### 3.2 `maze stop`

```bash
maze stop [--timeout 90] [--force]
maze stop --worker
```

By default, stops only the recorded local Maze Head parent. Maze validates the
PID start identity and command before sending `SIGTERM`; `--force` escalates to
`SIGKILL` only if that same verified process exceeds the graceful timeout. Use
`maze stop --worker` for the previous local Ray worker shutdown behavior.

### 3.3 `maze doctor`

```bash
maze doctor [--server-url http://127.0.0.1:8000] [--json] [--strict]
```

Checks the active runtime record, Python and required binaries, package and
Workbench directories, configured ports, and Core/Workbench HTTP health. It
does not depend on legacy development PID files, workspace nesting, or
`PYTHONPATH` checks.

### 3.4 `maze-sandbox` (retired)

The legacy remote sandbox service has been removed. The command remains as a
compatibility tombstone and exits with migration guidance. Use
`maze start --head --playground` for the maintained workflow editor.

---

## 4. Maze Workbench Backend REST API

Workbench is started by:

```bash
maze start --head --playground
```

Default ports:

| Service | Default |
|---|---|
| Maze Head API | `8000` |
| Ray Head GCS | `6379` |
| Workbench frontend | `5173` |
| Workbench backend | `3001` |

The CLI automatically sets `MAZE_CORE_URL` for the Workbench backend and
`VITE_MAZE_BACKEND_URL` for the frontend when custom ports are used, so users
normally do not need to export these variables manually.

Implementation:

```text
web/maze_playground/backend/src/server.js
```

The frontend calls this Node.js backend, which proxies or bridges to the Maze Head and Python bridge.

Environment variables:

| Variable | Description |
|---|---|
| `MAZE_WORKSPACE_ROOT_DIR` | Workspace root. Defaults to `<project>/workspaces`. |
| `MAZE_WORKSPACES_DIR` | Directory containing multiple workspaces. Defaults to `MAZE_WORKSPACE_ROOT_DIR`. |
| `MAZE_WORKSPACE_DIR` | Backward-compatible workspace root input. |
| `MAZE_DEFAULT_WORKSPACE_ID` | Default workspace id. Defaults to `default`. |
| `MAZE_SYSTEM_CATALOG_DIR` | System tasks/workflows catalog. Defaults to `<project>/system_catalog`. |
| `MAZE_CORE_URL` | Maze Head URL. Defaults to `http://localhost:8000`. |
| `PYTHON_BIN` / `MAZE_CONDA_PREFIX` / `CONDA_PREFIX` | Python interpreter selection for the Python bridge. |

---

### 4.1 Workspaces and System Catalog

| Method | Path | Description |
|---|---|---|
| POST | `/api/workspaces` | Create a workspace. Body may include `workspaceId`, `name`, and `mode`. |
| GET | `/api/workspaces/current?workspaceId=&workspaceDir=` | Get current workspace manifest and directory. |
| GET | `/api/workspaces/:workspaceId` | Get workspace by id. |
| GET | `/api/system-catalog?type=workflows|tasks` | List system workflow/task templates. |
| POST | `/api/system-catalog/import` | Copy a system task or workflow JSON into a workspace. |
| POST | `/api/system-catalog/workflows/load` | Load a system workflow template onto the canvas and import bundled task definitions. |

`/api/system-catalog/workflows/load` request:

```json
{
  "workspaceId": "default",
  "workspaceDir": "/optional/workspace/path",
  "sourceId": "resource_mix_demo.json"
}
```

Response includes `workflow` and `importedTaskDefinitions`. The endpoint imports dependent task definitions into workspace tasks, but the workflow itself is loaded as an unsaved draft. It only becomes a workspace workflow after the user saves it.

---

### 4.2 Task Management

| Method | Path | Description |
|---|---|---|
| GET | `/api/system-catalog?type=tasks` | List canonical built-in task sources. |
| GET | `/api/workspace-tasks` | List Python tasks under `workspace/tasks/`. |
| POST | `/api/workspace-tasks` | Create or overwrite one workspace task source file. |
| DELETE | `/api/workspace-tasks` | Delete one workspace task. |
| PATCH | `/api/workspace-tasks/rename` | Rename a workspace task. |

Task paths in requests are relative to `workspace/tasks/`.

---

### 4.3 Workspace File Management

| Method | Path | Description |
|---|---|---|
| GET | `/api/workspace-files?path=` | List directory contents. |
| POST | `/api/workspace-files/upload` | Upload a file through multipart or base64 payload. |
| POST | `/api/workspace-files/mkdir` | Create a directory. |
| DELETE | `/api/workspace-files` | Delete a file or directory. |
| GET | `/api/workspace-files/preview?path=` | Preview text, image, or table-like files. |
| GET | `/api/workspace-files/download?path=` | Download binary content. |
| PUT | `/api/local-workspaces/:workspaceId/manifest` | Write a local workspace manifest. |
| GET | `/api/local-workspaces/:workspaceId/manifest` | Read a local workspace manifest. |
| POST | `/api/workspace-files/missing` | Check whether paths are missing. |
| POST | `/api/artifacts/promote` | Copy a Core SHA-256 artifact into Workspace Files. |

---

### 4.4 LLM Integration

| Method | Path | Description |
|---|---|---|
| POST | `/api/llm/test` | Test an OpenAI-compatible endpoint. |
| POST | `/api/llm/generate-task` | Generate Python workspace task source from a prompt. |

Example:

```json
{
  "prompt": "Generate a task to count CSV rows",
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-4o-mini",
  "api_key": "<optional if server reads env>"
}
```

---

### 4.5 Workspace Workflows

| Method | Path | Description |
|---|---|---|
| GET | `/api/workspace-workflows` | List saved workflow blueprints. |
| DELETE | `/api/workspace-workflows` | Delete a workflow blueprint. |
| PATCH | `/api/workspace-workflows/rename` | Rename a workflow blueprint. |
| POST | `/api/workspace-workflows/save` | Save frontend JSON blueprint. |
| POST | `/api/workspace-workflows/load` | Load a saved blueprint onto the canvas. |
| POST | `/api/workspace-workflows/import` | Import a workflow from an uploaded JSON file. |

---

### 4.6 Runs, Cluster, and Artifacts View

| Method | Path | Description |
|---|---|---|
| GET | `/api/runs?status=&kind=&limit=&detail=` | Proxy Head unified `/runs`. |
| GET | `/api/runs/:runId` | Unified run detail. |
| GET | `/api/runs/:runId/tasks` | Run task list. |
| GET | `/api/runs/:runId/tasks/:taskId` | Single task snapshot. |
| GET | `/api/runs/:runId/events?after=` | Run events. |
| GET | `/api/runs/:runId/logs?tail=&taskId=` | Run/task logs. |
| GET | `/api/runs/:runId/artifacts` | Run artifact list. |
| GET | `/api/runs/:runId/tasks/:taskId/artifacts` | Task artifact list. |
| POST | `/api/runs/:runId/cancel` | Cancel a run. |
| POST | `/api/runs/:runId/retry` | Retry an AppSpec run. |
| GET | `/api/cluster/resources` | Proxy Head `/cluster/resources`. |
| GET | `/api/cluster/queues` | Proxy Head `/cluster/queues`. |
| GET | `/api/artifacts/sha256/:sha256/metadata` | Proxy Head artifact metadata. |
| GET | `/api/artifacts/sha256/:sha256?disposition=inline|attachment` | Open inline or download a sha256 artifact. |
| GET | `/api/dynamic-runs` | List dynamic runs. |
| GET | `/api/dynamic-runs/:runId` | Dynamic run detail. |
| GET | `/api/dynamic-runs/:runId/events` | Dynamic run events over HTTP. |
| POST | `/api/dynamic-runs/:runId/events` | Write an event through Maze Head. |
| DELETE | `/api/dynamic-runs/:runId` | Delete a dynamic run. |
| POST | `/api/dynamic-runs/cleanup` | Bulk cleanup dynamic runs. |
---

### 4.7 Editor APIs

| Method | Path | Description |
|---|---|---|
| POST | `/api/parse-custom-function` | Parse uploaded/pasted Python source and extract `@task` metadata. |
| POST | `/api/workflows/:id/run` | Compile the workflow supplied in the request and submit it to Maze Core. Returns the Core `run_id`. |

### 4.8 Health

```text
GET /health -> {"status": "ok"}
```

---

## 5. Event Protocol

Events are JSON objects with at least:

```json
{
  "seq": 1,
  "timestamp": "2026-06-20T00:00:00Z",
  "type": "start_task",
  "data": {}
}
```

### 5.1 Scheduler and task events

Common static/dynamic workflow events:

| Type | Key data fields |
|---|---|
| `start_workflow` | `workflow_id`, `run_id` |
| `start_task` | `task_id`, `task_name`, `node_id` |
| `finish_task` | `task_id`, `result`, `metrics` |
| `task_exception` | `task_id`, `error`, `traceback` |
| `finish_workflow` | `workflow_id`, `run_id` |
| `cancel_workflow` | `run_id`, `reason` |
| `run_interrupted` | `run_id`, `reason` |
| `register_task_spec` | dynamic run task spec metadata |
| `append_task` | dynamic run invocation metadata |
| `finish_dynamic_run` | dynamic run result |

## 6. Resource Configuration

Common resource fields:

| Field | Unit | Default | Description |
|---|---|---|---|
| `cpu` | cores | `1` | Minimum normalized value is 1. |
| `cpu_mem` | MB | `0` | `0` means unspecified/unlimited. |
| `gpu` | count | `0` | If `gpu_mem > 0`, Maze normalizes `gpu` to at least 1. |
| `gpu_mem` | MB | `0` | If omitted, Maze may infer GPU memory needs from task source. |

Normalization rules:

- Start from `{cpu: 1, cpu_mem: 0, gpu: 0, gpu_mem: 0}`.
- User-provided fields override defaults.
- If neither `gpu` nor `gpu_mem` is explicitly declared, Maze may infer GPU usage from imports and source code.
- `cpu < 1` is clamped to 1.
- `gpu_mem > 0` forces `gpu >= 1`.

---

## 7. Error Handling

### 7.1 SDK exceptions

| Error | Typical cause |
|---|---|
| `Exception("Failed to ...")` | HTTP non-200 or response `status != "success"`. |
| `RuntimeError("Dynamic run ended before task finished: ...")` | Waiting for a task while the dynamic run entered a terminal state. |
| `RuntimeError("Dynamic task failed: ...")` | A dynamic task emitted `task_exception`. |
| `RuntimeError("Failed to execute remote task ...")` | A LangGraph adapter Run failed or returned an invalid result. |
| `TimeoutError` | Wait operation timed out. |
| `TaskOutputInferenceError` | A task does not return a dict literal with string keys. |
| `TypeError("Task ... must return a dict")` | Task returned a non-dict value at runtime. |
| `TypeError("@task no longer accepts ...")` | Deprecated decorator options were passed. |
| `ValueError("...")` | Argument validation failed. |

### 7.2 Server conventions

Successful responses usually contain:

```json
{"status": "success"}
```

Server-side failures return HTTP 4xx/5xx with:

```json
{"detail": "..."}
```

Workbench backend failures usually return:

```json
{"error": "..."}
```

---

## 8. Complete Examples

### 8.1 Static workflow

```python
from maze import MaClient, task

@task(resources={"cpu": 1, "cpu_mem": 128})
def produce(name: str = "Maze"):
    return {"message": f"Hello {name}"}

@task(resources={"cpu": 1, "cpu_mem": 128})
def consume(message: str = ""):
    return {"upper": message.upper()}

client = MaClient("http://localhost:8000")
wf = client.create_workflow()
a = wf.add_task(produce, inputs={"name": "Maze"})
b = wf.add_task(consume, inputs={"message": a.outputs["message"]})
run_id = wf.run(artifact_mode=True, tags=["example"])
print(wf.wait(run_id))
```

### 8.2 Dynamic workflow

```python
from maze import MaClient, task

@task
def add_one(x: int = 0):
    return {"y": x + 1}

client = MaClient("http://localhost:8000")
run = client.create_dynamic_run(max_tasks=5)
t1 = run.append_task(add_one, inputs={"x": 1})
t2 = run.append_task(add_one, inputs={
    "x": {"__maze_output_ref__": True, "task_id": t1, "output_key": "y"}
})
print(run.wait_for_task(t2))
run.finalize({"done": True})
```

### 8.3 Direct HTTP DAG submission

```bash
curl -sS -X POST http://localhost:8000/workflows/submit \
  -H 'Content-Type: application/json' \
  -d '{
    "spec": {
      "schema": "maze.workflow/v1",
      "name": "hello-dag",
      "nodes": [
        {
          "id": "produce",
          "task_name": "produce",
          "code": "def produce(name=\"Maze\"):\n    return {\"message\": f\"Hello {name}\"}",
          "inputs": {"name": "Maze"},
          "outputs": ["message"],
          "resources": {"cpu": 1, "cpu_mem": 128}
        }
      ],
      "edges": [],
      "run": {"artifact_mode": true}
    }
  }' | python -m json.tool
```

---

## 9. Run Status, Metrics, and Observability

Use the unified `/runs/*` API first. It covers static workflows, AppSpec runs, and dynamic runs. The `/v1/*` endpoints are static workflow observability helpers for global metrics, current-task snapshots, and backward-compatible monitoring scripts.

### 9.1 State model

Run states:

```text
Active: created / running
Terminal: succeeded / failed / cancelled / timed_out / interrupted
```

Task states:

```text
pending / queued / running / succeeded / failed / cancelled / timed_out
```

### 9.2 Persistence

Maze Core exclusively owns static run persistence under its workspace:

```text
workspace/workflow_runs/static_runs/{run_id}/
  run.json
  events.jsonl
```

`MAZE_WORKSPACE_DIR` can override the default workspace path for compatible flows.

### 9.3 HTTP endpoints

#### `GET /v1/metrics`

Cluster-level aggregate metrics.

```bash
curl http://localhost:8000/v1/metrics
```

Example response:

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
    "by_status": {
      "submitted": 0,
      "running": 1,
      "succeeded": 8,
      "failed": 1,
      "canceled": 0,
      "interrupted": 0
    }
  },
  "tokens": {
    "in": 12345,
    "out": 6789,
    "cost_usd": 0.054321,
    "by_model": {
      "qwen3-30b": {"tokens_in": 12345, "tokens_out": 6789, "calls": 8}
    }
  }
}
```

#### `GET /v1/runs?status=running&limit=20&offset=0`

List static runs, newest first.

#### `GET /v1/runs/{run_id}/snapshot`

Return a full static run snapshot. The unified equivalent is:

```bash
curl http://localhost:8000/runs/<run_id>
```

#### `GET /v1/runs/{run_id}/current-task`

Quickly answer "what is this DAG running now?"

#### `GET /v1/runs/{run_id}/tasks`

List task states and metrics. The compatibility response keeps the legacy
`task_total` and `tasks` keys, mapped from `task_counts.total` and `task_nodes`.
Unified equivalent:

```bash
curl http://localhost:8000/runs/<run_id>/tasks
```

#### `GET /v1/runs/{run_id}/timeline?after=10`

Read static run events by sequence number. Unified equivalent:

```bash
curl "http://localhost:8000/runs/<run_id>/events?after=10"
```

### 9.4 CLI

```bash
maze status
maze status --watch
maze status --status running
maze status --run-id <run_id>
maze status --addr http://10.0.0.1:8000
```

### 9.5 Token and metrics reporting

Maze does not call LLMs by itself, so token usage must be reported by task code.

#### Channel A: `maze.metrics.report()`

```python
from maze import task, metrics

@task
def call_llm(prompt: str = ""):
    response = openai_client.chat.completions.create(...)
    metrics.report(
        tokens_in=response.usage.prompt_tokens,
        tokens_out=response.usage.completion_tokens,
        model=response.model,
        cost_usd=0.012,
    )
    return {"answer": response.choices[0].message.content}
```

#### Channel B: `__maze_metrics__` in task result

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

Maze strips `__maze_metrics__` from downstream task inputs and merges it into task metrics.

Metric field conventions:

| Field | Type | Merge behavior |
|---|---|---|
| `tokens_in` | int | Sum. |
| `tokens_out` | int | Sum. |
| `cost_usd` | float | Sum. |
| `model` | str | Enables `by_model` bucket. |
| Other keys | any | Numeric values sum; non-numeric values overwrite. |

### 9.6 Structured logs

Set:

```bash
MAZE_LOG_FORMAT=json maze start --head --port 8000
```

Each line becomes JSON, suitable for Loki or ELK:

```json
{"ts": "2026-05-23T10:00:00Z", "level": "INFO", "logger": "maze.core.path.path", "msg": "..."}
```

### 9.7 End-to-end validation commands

Terminal A:

```bash
conda activate maze
python -m maze.cli.cli start --head --port 8000 --ray-head-port 6379
```

Terminal B:

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

curl -sS "http://localhost:8000/runs/$RUN_ID" | python -m json.tool
curl -sS "http://localhost:8000/runs/$RUN_ID/tasks" | python -m json.tool
curl -sS "http://localhost:8000/runs/$RUN_ID/events" | python -m json.tool
curl -sS "http://localhost:8000/v1/metrics" | python -m json.tool
conda run -n maze python -m maze.cli.cli status --addr http://localhost:8000
```

Expected result:

- The run eventually has `status=succeeded`.
- `task_counts.total=2` and `task_counts.succeeded=2`.
- `produce.metrics.tokens_in=11` and `consume.metrics.tokens_in=3`.
- Global `tokens.in=14`, `tokens.out=9`, and `test-model.calls=2`.

### 9.8 Compatibility and boundaries

- Dynamic runs can be inspected through unified `/runs/*`.
- Use `DynamicRun` SDK and `/dynamic_runs/*` for task appending and detailed events.
- Token metrics depend on user reporting. Maze does not intercept LLM traffic.
- If the Head process crashes or restarts, running static runs are marked `interrupted` on the next startup and written to `events.jsonl`.

---

## 10. References

- Main repository: <https://github.com/QinbinLi/Maze>
- Online docs: <https://maze-doc-new.readthedocs.io/>
- Website: <https://mazeagent.net/>
- Key source files:
  - Decorator: `maze/client/maze/decorator.py`
  - Static workflow SDK: `maze/client/maze/workflow.py`
  - Dynamic run SDK: `maze/client/maze/dynamic.py`
  - LangGraph bridge: `maze/client/langgraph/client.py`
  - Head service: `maze/core/server.py`
  - Scheduler: `maze/core/scheduler/`
  - Static run persistence and events: `maze/core/workflow/static_run.py`
  - Dynamic run model and events: `maze/core/workflow/dynamic.py`, `maze/core/workflow/dynamic_store.py`
  - Metrics reporting: `maze/metrics/`
  - CLI: `maze/cli/cli.py`, `maze/cli/sandbox_cli.py`
  - Workbench backend: `web/maze_playground/backend/src/server.js`
