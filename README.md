# Maze v2

用 Python 实现静态 Agent DAG 运行时：Maze 描述图与管理调用，Ray 负责依赖、
资源放置和执行。目标范围见原实施计划；目前是已通过本机测试的 CPU 开发版，
尚未完成 LLM、GPU、多机和全部可靠性要求。

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
| `maze/engine.py` | 当前部署、请求额度、等待队列、deadline、drain |
| `maze/api.py` | HTTP 和 OpenAPI；无第二套业务状态 |
| `maze/client.py`、`maze/cli.py` | 薄客户端和本地管理入口 |

每次调用创建独立 ObjectRefs；上游引用作为 Ray 顶层参数传给下游。
入口只等待最终输出与小型完成信号，不转发中间业务结果。
默认业务任务不重试。异步节点的 Actor 在部署内复用，取消单次调用不杀共享 Actor。

Python 的简便图定义见 `examples/arithmetic.py`；标准协议见
`examples/arithmetic.yaml`。目前简便 Python API 的校验范围小于正式协议，
需要部署管理时请使用 `DagSpec` / Engine。

## 依赖与验证

基础：Ray、Pydantic、PyYAML。HTTP 层使用可选的 FastAPI、Uvicorn、HTTPX；
不安装 Torch、vLLM、Transformers。项目当前可构建 wheel，尚未发布。

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
