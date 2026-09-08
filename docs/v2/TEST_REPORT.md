# 本机验证报告 — 2026-09-08

环境：macOS 26.6.2 ARM64、48GB 内存、Python 3.11.16、Ray 2.58.0。
所有 Python 运行与依赖安装均使用 `/opt/anaconda3/envs/maze-v2`。
测试新建并关闭自己的本地 Ray 实例，2 个逻辑 CPU、256 MiB 对象存储。

## 实际执行

- 最新全量 `python -m pytest -q`：**92 passed in 53.48s**，退出码 0。
- 上一轮 attached 服务专项 `python -m pytest -q tests/test_services.py`：**15 passed in 13.36s**；本轮已纳入完整回归。
- Ruff 检查与格式化通过。
- `python -m examples.arithmetic`：通过 Engine 部署调用，输出 `{'result': 10}`、`{'result': 22}`，退出码 0。
- `python -m build --wheel --no-isolation`：成功生成开发版 wheel。
- 上一检查点 wheel 安装到 `/private/tmp/maze-wheel-j1sc4bcx`，在源码目录外以同一环境
  `python -I` 导入通过；未启动 Ray，未导入 Torch/vLLM/Transformers。
  这是 wheel 路径隔离 smoke，**不是另建干净环境的完整安装验收**。
- Linux CI 工作流已随之前提交推送；尚未核查远程执行结果。

真实集成覆盖：Task 多进程并行、并行汇合、多端口映射、多请求结果隔离、
上游失败不执行下游、8 MiB 中间对象不在入口反序列化、Async Actor 取消隔离、
队列过载、排队/运行 deadline、状态释放、部署隔离、HTTP token 与基本状态码，
以及前台 CLI 的 Ray 启动→HTTP 部署/调用→停止闭环。

本轮增加：有限重试、内部端口类型、不可序列化输出/异常、部分提交失败、重复取消、
准备中卸载、并发卸载、单部署清理失败隔离、输入/输出尺寸及 drain 竞争。
真实 TCP 关闭覆盖运行中和排队中请求，并检查额度释放与 Actor 后续可用。
在途请求期间 API stop / SIGTERM 均在测试上限内完成，进程退出后检查无 raylet/gcs_server。

网络测试曾真实失败：旧 BaseHTTPMiddleware 影响断开事件观察。修复为直接透传
ASGI receive 后，相同 TCP 测试通过。SIGTERM 在当前 Ray 环境可能返回退出码 15，
测试同时验证清理与进程退出，避免只用退出码推断清理成功。
确定性生命周期竞态和部分提交故障使用局部故障注入，其余执行依赖真实 Ray。

协议迭代增加 Python / YAML 模型等价、最终输出类型冲突、默认值与常量验证、
非 JSON 数据拒绝、显式 null 的序列化语义、可变默认值隔离、部署快照隔离。
真实前台 CLI 的 arithmetic 部署/调用/卸载现在通过类型化 `MazeClient` 完成。

Attached LLM 迭代使用真实本地 TCP HTTP/1.1 fixture + Ray Async Actor，未下载模型。
验证同一 TCP 连接跨请求复用、连接数上限、双请求会合（确证 await 重叠）、
usage 每请求隔离和缺失不补零；HTTP 503、响应超限和 timeout 均不自动重试。
取消和 deadline 用例故意让服务端工作继续，确认调用侧释放、兄弟请求与 Actor 后续可用，
同时返回 backend_cleanup=unconfirmed。卸载后服务仍存活，客户端连接已关闭。
还覆盖 Worker 环境变量凭据、另一个部署准备失败的隔离、生成上限、禁止输入覆盖 endpoint、
未知/重复 service_id、非法资源/模式/默认值。attached 示例编译通过，未连接示例地址。
这些证据验证 HTTP 协议适配与调用侧资源管理，不证明真实 Serve/vLLM 推理、GPU 取消或模型复用。

启动回滚迭代：预检、初始化失败与 HTTP 构造失败使用确定性故障注入；
端口冲突使用真实已监听 socket，并确认冲突后原 listener 仍接受连接。
另外三个独立子进程真实启动 Ray 后，分别注入初始化返回阶段异常、HTTP 构造异常和
KeyboardInterrupt；测试确认故障点确实到达、进程退出、无 raylet/gcs_server、API 端口可再绑定。
这不代表已经覆盖 Ray 初始化中间所有故障点、启动期间真实信号竞争或 SIGKILL 恢复。

## 原文档矩阵对照

“部分”表示已有对应证据，但尚未覆盖该条完整验收。

| 编号 | 状态与证据/缺口 |
|---|---|
| T01 | 部分：版本、资源、绑定、循环、默认值、最终输出类型和 Python/YAML 等价验证；含 LLM 的全协议矩阵未齐 |
| T02–T03 | 通过：arithmetic 与真实双 worker 会合测试 |
| T04 | 部分：顶层引用通过；复杂嵌套参数场景未全覆盖 |
| T05 | 通过当前用例：多端口与大对象不回流分别实测 |
| T06–T09 | 通过当前用例：重复/并发、部署隔离、失败传播和多最终输出 |
| T10 | 通过当前用例：默认不重试、纯 Task 最多 3 次、异常重试显式开启 |
| T11 | 部分：队列过载实测，完整尺寸/节点上限用例未齐 |
| T12–T13 | 通过当前用例：排队/运行超时、主动取消、真实 TCP 断开和额度释放 |
| T14 | 通过当前 Async Actor 单请求取消隔离用例 |
| T15–T17 | 通过当前用例：部分提交后清理、结果/异常序列化失败、JSON 尺寸/类型限制 |
| T18–T19 | 部分：none/basic/detailed 缺失标注；未接入 State 诊断 |
| T20 | 通过当前用例：真实 TCP await 会合、连接复用、并发/池上限和关闭；未作长期压力验收 |
| T21–T27 | 未执行：尚无 GPU/模型/多机环境，相关实现也未齐 |
| T28 | 部分：本地启停、端口冲突和预检隔离、真实 Ray 启动后故障回滚通过；多机 SSH 与完整重复停止矩阵待补 |
| T29 | 通过当前用例：准备/调用/排队/并发卸载竞争；GPU 尚未验证 |
| T30 | 部分：SIGTERM 有界关闭通过，非优雅崩溃/SIGKILL 恢复边界待补 |
| T31 | 部分：5 轮×8 请求后计数归零；长时间 RSS/引用趋势未测 |
| T32 | 部分：wheel 构建和源码外导入通过，完整干净安装待测 |
| T33–T34 | 部分：无业务存储实现，token/输入错误脱敏实测；全栈审计待补 |

## 小规模基准

以下是上一检查点（`74bcc61`）的历史基线，本轮未重复测量，不代表新增校验后的性能。

命令：`python -m benchmarks.arithmetic --samples 20`。
同一台机器、2 逻辑 CPU、并发 1，分别预热 3 次，统计 20 次调用，不含 Ray 启动。

| 路径 | 平均 ms | P50 ms | P95 ms | P99 ms |
|---|---:|---:|---:|---:|
| 直接 Ray | 1.078 | 1.037 | 1.328 | 1.328 |
| Maze Runtime | 1.449 | 1.413 | 1.712 | 1.712 |

两条路径均为相同算术语义的三个 Task；Maze 额外验证端口并返回完成信号。
样本少且图极小，P95/P99 在此样本量下落在同一观测值上，不代表稳态尾延迟。
该结果仅作为基线，不是 GPU、LLM、HTTP 或高并发吞吐结论，也不宣称速度提升。
