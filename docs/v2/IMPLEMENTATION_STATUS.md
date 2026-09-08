# Maze v2 开发记录

## 当前状态：按完整文档范围推进

用户已确认以完整实施计划为目标，同时继续使用 Python、类型标注和直接易读的代码。
暂无 GPU/多机服务器，先完成本机可验证的部分。以下是当前进展，后面的最小原型记录
仅代表较早的开发检查点。

本轮新增统一 JSON/YAML 协议及 JSON Schema、Async Actor、每节点完成信号、
异步全图等待和基础指标；增加 Engine 的有界请求队列/节点预算、deadline、取消、
drain，以及单状态所有者的 HTTP API、客户端、前台 managed-local CLI。
恢复原许可证和第三方归属文件，新增 Linux CPU CI（尚未远程执行）。

验证：28 项本机测试通过、Ruff 通过、开发版 wheel 构建与源码外导入通过。
完整命令、测试矩阵及小规模 Ray/Maze 对照数据见 `TEST_REPORT.md`。
所有安装仅发生在 `/opt/anaconda3/envs/maze-v2`：新增 HTTP/API 和 build 依赖，
没有修改其他环境、其他分支，没有提交或推送。wheel 导入测试仅在 `/private/tmp`
的包目录中进行，仍使用同一个 Conda 解释器。

### 分阶段缺口

| 阶段 | 状态 |
|---|---|
| P0 | 部分：本机版本/依赖/测试/CI 基础已有；Linux 与 GPU 兼容组合待实测 |
| P1 | 部分：CPU 协议、schema、绑定/类型验证已有；ServiceSpec、完整边界及 Python builder 统一待补 |
| P2 | 部分：真实 Ray 数据流/多端口/完成信号已有；完整失败/序列化/重试矩阵待补 |
| P3 | 部分：本地 API/CLI/SDK 与 admission/deadline/drain 闭环已有；错误契约和故障边界待完善 |
| P4 | 部分：Async Actor 已验证；LLM 服务、连接池、usage 尚未实现 |
| P5 | 部分：正常 managed-local 启停已有；多机、启动故障回滚与恢复管理待补 |
| P6 | 部分：基础指标、状态释放与小型对照基准已有；压力/故障矩阵和详细指标待补 |
| P7 | 部分：v2 默认路径、wheel、许可证已有；完整打包 E2E、文档与最终审计待补 |

当前不能宣称已经完成原文档或生产级验收。下一批优先完成 P1/P2 边界测试、
P3 真实网络取消与 drain 竞争验证，然后加入 P4 的 HTTP/LLM 服务适配。

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
