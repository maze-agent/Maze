# Maze v2

用 Python 实现静态 Agent DAG 运行时：Maze 描述图与管理调用，Ray 负责依赖、
资源放置和执行。目标范围见原实施计划；目前是已通过本机测试的 CPU 开发版，
已实现 attached LLM HTTP 适配；managed LLM、GPU、多机和全部可靠性要求尚未完成。

## 本机运行

本项目固定使用 `/opt/anaconda3/envs/maze-v2`，Python 3.11.16、Ray 2.58.0。
在仓库根目录运行；下面的 `python` 均指激活该环境后的解释器。

```bash
conda activate /opt/anaconda3/envs/maze-v2
python -m examples.arithmetic
python -m pytest -q
```

前台启动 Engine，先生成一个管理 token；其他终端调用时使用同一个 token：

```bash
export MAZE_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m maze start --config examples/engine.yaml
```

在另一终端激活同一环境、设置相同 token 后：

```bash
python -m maze status
python -m maze deploy examples/arithmetic.yaml
python -m maze deployments list
# 用部署响应的实际 deployment_id 替换下方占位符。
python -m maze invoke <deployment_id> --input examples/input.json --metrics basic
python -m maze undeploy <deployment_id>
python -m maze stop
```

默认只绑定 `127.0.0.1:8000`，2 个逻辑 CPU、256 MiB 对象存储。
Maze 启动和关闭自己的 Ray 实例；发现已有 Ray 进程时拒绝 managed-local 启动，
不会用 `pkill`、端口号或 `ray stop --force` 清理其他进程。
导入 `maze` 不启动集群。对象存储额度不是进程树的硬内存上限。

## 核心代码

| 文件 | 职责 |
|---|---|
| `maze/graph.py` | Python 图、拓扑验证与不可变执行步骤 |
| `maze/spec.py` | 有版本的 JSON/YAML 协议，转换到同一个执行图 |
| `maze/runtime.py` | Task、Async Actor、多端口、完成信号、取消 |
| `maze/services.py` | attached chat completion、异步连接池与 usage |
| `maze/engine.py` | 当前部署、请求额度、等待队列、deadline、drain |
| `maze/api.py` | HTTP 和 OpenAPI；无第二套业务状态 |
| `maze/client.py`、`maze/cli.py` | 薄客户端和本地管理入口 |

每次调用创建独立 ObjectRefs；上游引用作为 Ray 顶层参数传给下游。
入口只等待最终输出与小型完成信号，不转发中间业务结果。
默认业务任务不重试。异步节点的 Actor 在部署内复用，取消单次调用不杀共享 Actor。

Python 图定义见 `examples/arithmetic.py`，直接构建与
`examples/arithmetic.yaml` 相同的 `DagSpec`；运行示例使用 Engine 部署与调用。
底层 `DAG` / `Runtime` 保留为执行接口，正式部署使用标准模型。
默认值、常量和输入采用受限 JSON；内部节点间仍可以传递 Ray 支持的对象。

Python 客户端连接已经启动的 Engine：

```python
import os
from examples.arithmetic import spec
from maze.client import MazeClient
from maze.spec import InvokeRequest

with MazeClient("http://127.0.0.1:8000", os.environ["MAZE_API_TOKEN"]) as client:
    deployment_id = client.deploy(spec)["deployment_id"]
    try:
        print(client.invoke(deployment_id, InvokeRequest(input={"x": 3}))["output"])
    finally:
        client.undeploy(deployment_id)
```

`spec.document()` 导出标准 JSON 字典，保留显式 `null` 与未声明默认值的区别。
Engine 部署时重新校验并复制模型；之后修改调用方的字典不会改变已部署的图。

## 绑定现有模型服务

`examples/attached_chat.yaml` 是非流式文本 chat DAG；
`examples/attached_service.yaml` 配置固定 endpoint、模型名和调用上限。
先修改服务配置中的占位模型名和地址；凭据环境变量需要在启动 Ray 前设置，
或由集群管理员预置到 Worker。Maze 不会为这个示例下载或启动模型。

```bash
python -m maze deploy examples/attached_chat.yaml --deployment-config examples/attached_service.yaml
python -m maze invoke <deployment_id> --input examples/chat_input.json
```

部署时从 Worker 请求 `/models` 并检查模型 ID；调用 `/chat/completions`，
返回 `text` 和原始 `response`。basic/detailed 指标中的
`nodes.<node_id>.metadata.llm` 包含模型标识和实际返回的 usage，缺失 usage 不补零。
这里只完成本地 HTTP fixture 验证；真实 Serve LLM / vLLM 推理尚未验收。
取消后的 `cleanup` 描述调用侧清理，`backend_cleanup: unconfirmed` 表示无法确认后端推理停止。

## 依赖与验证

基础：Ray、Pydantic、PyYAML。HTTP 层使用可选的 FastAPI、Uvicorn、HTTPX；
`llm` extra 仅需要 HTTPX（本机已安装），不安装 Torch、vLLM、Transformers。
项目当前可构建 wheel，尚未发布。

```bash
python -m ruff check .
python -m build --wheel --no-isolation
python -m benchmarks.arithmetic --samples 20
```

[开发进度](docs/v2/IMPLEMENTATION_STATUS.md)与[测试报告](docs/v2/TEST_REPORT.md)
列出实际验证和剩余缺口，不能将 mock、本机或导入 smoke 当作 GPU/多机验收。
[接口及运维边界](docs/v2/OPERATIONS.md)包含调用、取消与停止语义。

旧研究版本保留在 `main` 和历史提交 `e7b90f5`；v2 不承诺旧工作流 API 兼容。
原许可证与归属文件已恢复到当前开发版本。业务运行历史不保存；Ray 自身仍有运维日志。
