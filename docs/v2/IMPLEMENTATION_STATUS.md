# Maze v2 开发记录

## 当前状态：按完整文档范围推进

### 有界事件缓存验证（仍基于 `5dfce8f`）

继续开启事件记录、把 GCS 保留上限设为 1000，同负载运行 120 秒。
GCS 日志显示累计报告 49,321 条事件、保留 1000 条；RSS 增加 5.14 MiB，
对比默认上限组增加 87.75 MiB。14,376 次调用、1,797 批清理检查、11 次 Actor 重建均正常。
Driver 仍增加 8.38 MiB，整体内存收敛尚未验证。
压测工具增加 `--task-event-capacity`、范围/冲突检查与实际容量记录；新增参数真实 Ray smoke 通过。
Ray `_system_config` 标注测试用途，因此仅用于此诊断工具，没有加入正式 Engine 配置或改变默认行为。
本轮未提交推送，未新增依赖，仍限定 v2 与原 Conda 环境。

### GCS 内存增长定位（仍基于 `5dfce8f`）

压测增加独立 GCS RSS、脚本 SHA256、任务事件配置记录。
相同代码/环境下顺序执行两次 120 秒对照，100 批预热，只改变任务事件报告开关。
开启时 GCS 增加 87.75 MiB、关闭时 1.33 MiB；自有 GCS 日志记录的事件数分别为 24,772 与 0。
两组无意外错误，清理检查全部通过，各重建 Actor 11 次。证据支持任务事件保留是本次 GCS 增长的主要因素。
Driver 在关闭组仍增加 5.83 MiB，未证明整个系统内存收敛，也未验证达到默认事件缓存上限后的表现。
未修改生产默认配置；后续需比较较小缓存上限及更长时间/引用趋势。原始对照数据已存入测试报告目录。
未安装依赖、未提交推送，保持 v2 与指定 Conda 环境。

### 可配置持续压测（基线 `5dfce8f`）

新增 `benchmarks/soak.py`：预热后混合 CPU 算术图、Async Actor 等待、主动取消和 deadline，
每十秒左右卸载重建一个 Actor 部署；每批检查 Engine 请求、队列和在途额度归零。
记录成功/取消/超时计数、分类延迟、吞吐、Driver/子进程 RSS 与内存线性趋势。
延迟保留最近最多 2048 个样本/类别；只有显式 output 路径才写结果，无 Engine 持久化指标。
本机五分钟基线仅用于初步观察，不等于小时级压力或泄漏自由验收。
当前不测 HTTP 连接、LLM、对象存储字节和 Ray 全局 ObjectRef 总量，后续仍需扩展。
本轮未提交推送，未安装依赖，仍仅使用原 Conda 环境与 v2 分支。
实测 300 秒、36,320 次调用，0 意外错误，4,540 批清理检查通过，28 次 Actor 重建。
Driver / 子进程合计 RSS 分别增加约 15 / 162 MiB，分进程快照显示主要增长位于 GCS。
因此 T31 仍为部分完成；尚未证明内存收敛。原始数据和分析见测试报告。

### managed-local 启动回滚（基线 `500654d`）

启动预检或端口绑定失败时不调用 Ray init/shutdown；只有尝试创建本地 Ray 后才进入回滚。
HTTP 应用与 Server 构造纳入 Engine 清理范围，Ray shutdown 失败仍释放 API socket，
同时保留异常链，不把清理失败报告为成功。
新增启动故障测试：token/已有 Ray/地址/进程检查、实际端口冲突、部分初始化和中断、
HTTP 构造与运行故障、清理自身失败；真实子进程启动 Ray 后注入异常或 KeyboardInterrupt，
验证 Ray 进程退出和端口可重新绑定。已有 API stop / SIGTERM 在途请求测试继续回归。
本轮只修改 v2，使用原 maze-v2 环境，无新依赖；后续已提交推送为 `5dfce8f`。
最终验证：**92 passed in 53.48s**；Ruff 与 wheel 构建通过。
仍未覆盖 ray.init 内部任意时刻卡死、SIGKILL 遗留恢复或真实多机 SSH 回滚。

### Attached LLM 与异步连接池（基线 `dbf3713`）

新增 attached ServiceSpec、LLM 节点与部署服务绑定，限定文本非流式 chat。
Worker 内复用 HTTPX 客户端，准备时检查模型声明；配置限定连接、timeout、token 与响应大小。
显式 AsyncOperation 生命周期支持准备/关闭，卸载先关闭客户端再终止自有 Actor。
usage 进入独立节点 metadata；取消错误区分调用侧 cleanup 与未确认的 backend_cleanup。
新增真实 TCP + Ray 测试：连接复用/上限、双请求重叠、usage 缺失、取消/超时隔离、
HTTP 失败零重试、响应超限、准备失败、凭据环境变量、生成参数和服务协议边界。
没有下载模型、安装新依赖或调用外部服务；HTTPX 已在原环境中。
LLM extra 仅声明现有 HTTPX。只修改 v2；后续已提交推送为 `500654d`。
这不是 managed Serve LLM / vLLM 或 GPU 推理验收，相关开发与实测仍待完成。
完整回归 **77 passed in 40.32s**；Ruff 与 wheel 构建通过。

### Python / JSON / YAML 协议统一（基线 `630f5f6`）

Python arithmetic 示例直接构建 `DagSpec`，与 YAML 模型等价，并通过 Engine 执行。
客户端新增类型化 deploy / invoke / undeploy；`document()` 输出统一协议，保留
未声明默认值与显式 null 的区别，不遗漏默认容器字段的修改。
部署前检查默认值、最终输出类型、重复节点和 JSON 常量；Engine 重新校验模型并复制，
避免 Python `model_copy` 绕过协议以及调用方后续修改污染已有部署。
JSON 边界拒绝非字符串键、元组、非有限数值和过深嵌套；内部 Ray 对象传递不受此限制。
新增协议回归、默认值隔离、部署快照隔离，真实 CLI 测试使用类型化客户端。
本轮仅使用原 Conda 环境，未新增依赖；后续已提交推送为 `dbf3713`。
最终验证：**62 passed in 27.88s**；Ruff、wheel 构建通过，导出的 JSON Schema / OpenAPI 与现有文件一致。

### 后续本机可靠性迭代（基线 `74bcc61`）

修复部署准备/卸载竞态、并发卸载、单个清理失败阻断 Engine 后续清理的问题。
清理任务抵御重复取消，未确认停止的调用继续占容量；普通纯 Task 增加有限 Ray 重试。
标准协议现在将端口检查带入每个远端 runner，错误响应增加调用/部署/节点标识。

真实 TCP 测试发现旧 HTTP 中间件使排队断开未及时被观察；改为透传 receive 的 ASGI
认证中间件后，运行中/排队中断开均能清理，且共享 Actor 可以继续接新请求。
Uvicorn 增加有界优雅停止，覆盖在途请求时 API stop 和 SIGTERM，并确认 Ray 进程退出。

新增 `tests/test_reliability.py`：部分提交故障、输出与异常序列化故障、有限重试、
重复取消、准备/卸载竞态、清理失败隔离、JSON 大小与 drain 用例。
生命周期钩子的人工故障注入用于确定性重现竞态；Task/Actor 和序列化路径使用真实 Ray。
此次没有新增依赖、没有改其他环境或分支；后续已提交并推送为 `630f5f6`。
最终验证：`python -m pytest -q` 为 **44 passed in 26.88s**；Ruff 和 wheel 构建通过。

用户已确认以完整实施计划为目标，同时继续使用 Python、类型标注和直接易读的代码。
暂无 GPU/多机服务器，先完成本机可验证的部分。以下是当前进展，后面的最小原型记录
仅代表较早的开发检查点。

本轮新增统一 JSON/YAML 协议及 JSON Schema、Async Actor、每节点完成信号、
异步全图等待和基础指标；增加 Engine 的有界请求队列/节点预算、deadline、取消、
drain，以及单状态所有者的 HTTP API、客户端、前台 managed-local CLI。
恢复原许可证和第三方归属文件，新增 Linux CPU CI（尚未远程执行）。

较早检查点：28 项本机测试通过、Ruff 通过、开发版 wheel 构建与源码外导入通过。
完整命令、测试矩阵及小规模 Ray/Maze 对照数据见 `TEST_REPORT.md`。
所有安装仅发生在 `/opt/anaconda3/envs/maze-v2`：新增 HTTP/API 和 build 依赖，
没有修改其他环境、其他分支，没有提交或推送。wheel 导入测试仅在 `/private/tmp`
的包目录中进行，仍使用同一个 Conda 解释器。

### 分阶段缺口

| 阶段 | 状态 |
|---|---|
| P0 | 部分：本机版本/依赖/测试/CI 基础已有；Linux 与 GPU 兼容组合待实测 |
| P1 | 部分：CPU/attached LLM 协议、schema、Python 模型及类型验证已有；managed ServiceSpec 和完整边界待补 |
| P2 | 部分：真实 Ray 数据流/多端口/完成信号已有；完整失败/序列化/重试矩阵待补 |
| P3 | 部分：本地 API/CLI/SDK 与 admission/deadline/drain 闭环已有；错误契约和故障边界待完善 |
| P4 | 部分：Async Actor、attached HTTP 适配、连接池、usage 已本机验证；managed Serve LLM/vLLM 未完成 |
| P5 | 部分：managed-local 启停、冲突及启动失败回滚已有；多机、强制崩溃恢复管理待补 |
| P6 | 部分：基础指标、状态释放与小型对照基准已有；压力/故障矩阵和详细指标待补 |
| P7 | 部分：v2 默认路径、wheel、许可证已有；完整打包 E2E、文档与最终审计待补 |

当前不能宣称已经完成原文档或生产级验收。P1/P2 的更全面协议边界、长期资源收敛、
强制崩溃恢复和 managed Serve LLM 配置/所有权适配仍是下一批本机工作。

## 2026-09-08：最小 Python DAG 原型

基线：`v2` 分支，HEAD `e114299`。本阶段不提交或推送。
使用已有 `/opt/anaconda3/envs/maze-v2` 环境：Python 3.11.16、Ray 2.58.0、
pytest 9.1.1。没有安装依赖或修改其他 Conda 环境。

根据用户最新要求，先用纯 Python 和类型标注表达核心逻辑，保持简洁。
这是原计划 P1/P2 的一个子集，不宣称完成原计划全部验收。

### 已实现

- `maze/graph.py`：DAG、节点、输入/输出/常量，结构验证和拓扑排序。
- `maze/runtime.py`：多端口 Task，顶层 ObjectRef 依赖，重复/并发调用。
- `examples/arithmetic.py`：固定分叉汇合，输入 3/7 输出 10/22。
- `tests/`：协议和真实 Ray worker 集成测试。
- `pyproject.toml`、`.gitignore`、README：最小依赖、源码忽略规则和运行说明。

### 验证结果

以下命令均在仓库根目录执行，前缀为
`conda run -p /opt/anaconda3/envs/maze-v2`：

| 命令 | 实际结果 |
|---|---|
| `python -m ruff format .` | 格式化完成 |
| `python -m ruff check .` | All checks passed，退出码 0 |
| `python -m pytest -q` | 13 passed in 7.42s，退出码 0 |
| `python -m examples.arithmetic` | 返回 `{'result': 10}`、`{'result': 22}`，退出码 0 |
| `python -m pip check` | No broken requirements found，退出码 0 |

测试环境为 macOS ARM64。首次沙箱内测试是 8 passed、5 errors，原因是 Ray
启动时读取进程信息被系统沙箱禁止；授权后在沙箱外运行真实本地 Ray 测试通过。
测试显式新建实例，使用 2 个逻辑 CPU、256 MiB 对象存储，完成后关闭。
并行测试使用双任务会合并核对不同 worker PID，不以耗时阈值冒充并行证据。
其他集成用例覆盖重复/并发输入、多输出绑定和上游异常不执行下游。

### 有意简化与待完成项

- 使用 Python 函数和 dataclass，暂不实现 YAML、Pydantic schema 或导入字符串协议。
- 输入绑定就是依赖边；图结构编译为 tuple，函数和常量需要调用方保持不变。
- 无指标需求时，不增加 CompletionSignal；所有节点必须能到达某个最终输出。
- 暂由示例启动 Ray；Maze 的 Engine 管理入口尚未实现。
- 当前是同步调用 API；并发由调用方线程发起，尚无 admission、deadline 或取消 API。
- 尚未验证大对象控制面流量、长期资源收敛、干净 wheel 安装。
- Async Actor、Serve LLM、GPU、真实多机尚未实现或测试。

下一步可以先评审 Python 图定义和调用接口，再加入 Async Actor 与异步调用。
