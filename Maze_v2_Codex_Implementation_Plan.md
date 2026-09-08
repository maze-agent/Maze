# Maze v2：交给 Codex 的完整重构与实施计划

**文档日期：2026-09-08**  
**目标仓库：`maze-agent/Maze`**  
**本次核查的 main：`e7b90f5c4cd1ad7cc1cfb5926d853ffedd2b40b1`**  
**性质：实施任务书，不是现有功能说明，也不是已经通过测试的实现。**

> 核心目标：把 Maze 重构为一个尽量复用 Ray 的静态 Agent DAG 分布式运行时。Maze 包办运行环境启动、DAG 部署和调用入口；Ray 承担依赖等待、对象传输、资源放置和分布式执行；Ray Serve LLM 承担模型服务。一次调用完成后返回输出与可选指标，Maze 不保存业务执行历史。

## 0. 给 Codex 的执行要求

请实际实施本计划，而不是只复述架构或再生成一份计划。按阶段推进，每个阶段留下可运行代码、测试和简短执行记录。先建立可工作的 v2 路径，再切换默认入口，最后删除旧实现；不要第一步删除整个旧 Core。

### 0.1 决策优先级

1. 第 1 节是用户已明确确认的产品边界，不得自行改变。
2. 其他章节给出了为实施而提出的工程默认方案。默认按这些方案执行；出现经源码、版本兼容性或实验确认的问题，可以采取更小、更可靠的等价实现，在决策记录中说明。
3. 不得因为一个局部问题重新引入自研节点调度、GPU 显存余额表、业务数据库或完整模型进程管理器。
4. 不要就已确认的五项产品边界重复询问。只在缺少真实凭据、外部机器权限，或需要超出授权的高成本/破坏性操作时停在该操作边界；其他不依赖它的工作继续。
5. 代码和测试是最终依据。文档声明、Mock 通过、语法检查，均不能代替真实 Ray 集成测试；CPU 测试不能代替真实 GPU/多机验证。

### 0.2 仓库和资源安全

开始先查看 `git status`、当前分支、HEAD、项目指令及未提交文件。上述 SHA 只是审查基线，不要求回滚或重置到它。若当前工作分支已包含更新，以实际仓库为准，记录差异。适用时创建独立本地工作分支，例如 `codex/maze-v2-static-runtime`；不得破坏用户已有改动。

不要执行 `git reset --hard`、强推、重写历史、删除未知未跟踪文件。默认不推送、不创建 PR、不发布 PyPI、不部署到未授权的真实集群。保留论文复现版本的历史、LICENSE、第三方归属和引用信息。没有自动提交权限时保留本地 diff；不要为制造提交而更改 Git 身份。

真实 GPU 测试使用用户已提供的模型路径或明确授权下载的测试模型。不擅自下载大模型、不使用收费 API、不修改系统驱动，不关闭不属于本次任务的 Ray 服务。外部依赖不足时记录 `BLOCKED` 或带原因的 `SKIPPED`，不能伪造 PASS。

### 0.3 交付方式

维护 `docs/v2/IMPLEMENTATION_STATUS.md`，逐阶段记录：完成项、文件变更、执行命令、实际结果、跳过原因和剩余阻塞。每个阶段完成后继续后续可执行阶段，不把“需要一个真实 workload”作为停止基础重构的理由。用户稍后提供业务 workload；现在先构建合成测试和可接入的基准接口。

文中新的 CLI、HTTP、Python API 和 YAML 均为**目标契约**，不是当前仓库已经可用的命令。

---

## 1. 用户已确认的产品边界

| 事项 | 确定要求 |
|---|---|
| 产品定位 | 在 Ray Core 之上构建高级库：静态 Agent DAG 分布式运行时，不再做大而全的 Agent 平台。 |
| 代码来源 | 节点代码由开发者提前编写；不运行 LLM 临时生成的 Python/Shell 代码。 |
| 图语义 | 静态 DAG，没有条件分支、循环或运行时增删节点。固定的并行分叉/汇合仍支持；“无分支”不是“所有节点串行”。 |
| 使用方式 | 接收一个 DAG，准备它需要的模型服务并暴露 API；部署一次，后续接收大量不同输入。 |
| 输出方式 | 非流式；每次调用一次性返回输出及可选指标。不同请求可以并发。 |
| 启动责任 | **Maze 负责全部运行组件的启动。不得把“用户先手动搭好 Ray 集群”作为唯一或默认使用方式。** |
| 底层机制 | 尽可能利用 Ray 的 Task、Actor、ObjectRef、资源调度、依赖和失败传播；模型服务利用 Ray Serve LLM。 |
| 持久化 | 不保存业务请求、运行历史、中间结果、长期指标；上层应用可保存返回值。 |
| 文件沙盒 | 用户决定稍后单独讨论。本次不实现文件沙盒、跨节点共享工作区或文件历史系统。 |
| 优化方法 | 先建立正确、可测量的基础，再以未来的真实 workload 持续优化。 |

成功标准不是“少了多少行代码”，而是职责清晰、重复执行正确、错误边界可预测、能够实际部署并测量。

## 2. 本次不做的内容

不做 HACS、自研 CPU/GPU/I/O 调度队列、自研节点选择、自研可用资源余额/lease、自研 GPU ID/实时显存分配、Maze Worker 二次注册和心跳、论文式故障诊断与提示词自动修复。

不做动态 DAG、ReAct/规划/记忆/RAG 应用框架、通用工具市场、Dify/LangGraph 完整兼容层、可视化 DAG 编辑器、Workspace、业务 Run 数据库、历史日志查询平台、审计计费系统、对象存储产品、沙盒产品和全功能 RBAC。

不以 Ray Compiled Graph、Ray 私有 API、多执行后端插件平台为首版基础。不为未来优化先建立庞大的插件系统。

“不做业务持久化”不禁止：原始部署配置文件、依赖锁文件、少量用于安全启动/停止的本地管理信息、必要运维日志。不得在这些管理文件中塞入请求正文、prompt、中间输出、ObjectRef 或业务历史。Ray/Serve 自身可能产生缓存、日志和对象溢写；不承诺整个软件栈零落盘或响应返回后物理擦除所有副本。[R1]

## 3. 目标架构与默认工程选择

### 3.1 三种生命周期

| 生命周期 | 内容 | 结束方式 |
|---|---|---|
| Engine | 已启动的 Ray 集群、调用入口、当前部署清单、管理权限和资源归属 | `maze stop`；只停止自己拥有的资源 |
| Deployment | 不可变 DagSpec、ExecutionPlan、已准备的服务/Actor、有限并发配置 | 先 DRAINING，再清理并卸载 |
| Invocation | 本次输入、所有调用引用、deadline、有限指标和错误 | 成功/失败/取消后清理，不留业务历史 |

重复使用的是定义、执行计划、不可变代码以及显式共享的服务。**每次 Invocation 必须创建新的一组 ObjectRefs；禁止复用上一次请求的输出引用。**

### 3.2 首版默认结构

建议先用一个 `EngineHost` 管理进程，运行轻量异步 HTTP API、内存部署注册表和 Invocation 协调器；真正的业务节点都交给 Ray。默认单个 API worker/单个状态所有者，不宣传控制面 HA，也不悄悄启动多个各自拥有独立注册表的 Uvicorn worker。

这一选择是工程默认，不是用户新增限制。若采用单副本 Ray Serve ingress 更简单，也可以用 ADR 说明后替换入口实现，但不能同时建立两套部署真相源。核心 `DagRuntime` 不得依赖 FastAPI 类型，应可嵌入 Python 程序。

LLM 使用 Ray Serve LLM；普通 CPU/GPU 函数使用 Ray Task；异步 I/O 和 LLM 调用适配器按需要使用 Async Actor。不要把所有业务节点无差别转换为常驻 Actor。

```text
用户的 DAG + 部署配置
          |
      EngineHost
          |
  验证 -> ExecutionPlan -> ServiceBindings
          |
     InvocationScope（每次独立）
          |
  Ray Task / Async Actor + 顶层 ObjectRef 参数
          |                         |
      普通计算                 OpenAI-compatible 调用
                                    |
                              Ray Serve LLM
          |                         |
          +---- 输出 / CompletionSignal / 指标 ----+
                              |
                        返回并清理本次状态
```

### 3.3 建议模块

以下是职责划分，不要求为了名字创建空类或一文件一类。可以合并小模块。

```text
maze/
  spec/           # DagSpec、部署/服务/资源协议、验证
  compiler/       # 不可变 ExecutionPlan、端口到参数/引用映射
  runtime/        # Invocation、Ray runner、异步 I/O、错误和指标
  services/       # Ray Serve LLM 部署和调用适配
  deployment/     # 当前部署生命周期、归属、回滚、drain
  cluster/        # 封装官方 Ray 启停/cluster launcher
  api/            # 薄 HTTP 入口、协议错误转换
  cli/            # start/deploy/invoke/status/undeploy/stop
  sdk/            # 轻量客户端和可嵌入接口
```

过渡期可以放在隔离的 `maze/v2/` 下，但最终默认入口不得依赖旧 Scheduler/ResourceManager。不要长期维持两套完整产品路径。

## 4. 版本、平台与现有仓库审计

本次读取到旧 `pyproject.toml` 固定 `ray==2.50.1`，同时把 Transformers、Accelerate 等放在基础依赖中，vLLM extra 固定 `0.26.0`。这只能说明现有配置，不能当作已验证的兼容组合。[C2]

文档检查时 Ray stable 页面标识为 2.58.0。以该版本作为**候选基线**，实施开始时再核对官方支持组合。先锁一组实际可安装、可运行的 Ray/Serve/vLLM/Python/CUDA 组合，不要盲目追 nightly，也不要直接升级所有依赖。记录测试环境、版本、wheel/镜像与 CUDA 条件。[R8][R14]

首版参考环境建议 Linux + Python 3.11；最终以验证后的组合为准。普通 CPU 路径应能在无 GPU 环境安装和测试。真实 GPU 基线为已授权的 NVIDIA/CUDA 环境；不承诺 Windows/macOS/MPS 全功能兼容。

基础安装与 LLM 重依赖分离：基础路径不应仅为执行 CPU DAG 而加载 vLLM、Torch、Transformers。优先使用 Ray 官方 extras/兼容组合，在项目约束文件中锁住实际测试版本。保留包名 `maze-agent` 和 import 名 `maze`，重大接口变化用开发版版本号表达，不擅自正式发布。

现有 `.gitignore` 忽略 `/tests/` 和 `/benchmarks/`。必须让新的测试与基准**源码**进入版本控制，继续忽略结果、权重、日志和临时环境。在移除忽略前先检查本地被忽略文件；不要使用不加审查的 `git add -A` 把私密数据提交进来。[C3]

## 5. DAG、部署、调用协议

### 5.1 三份逻辑协议

- `maze.dag/v1`：逻辑图、节点、输入输出端口和数据边。
- `maze.deployment/v1`：代码环境、服务绑定、资源预算、调用限制。
- `maze.engine/v1`：集群启动模式、网络/认证、Engine 级限制。

允许合并成一个 YAML bundle；内部仍保持这些职责独立。JSON 与 YAML 都转成相同的类型模型。Python SDK 只是构建同一模型，不维护第二套语义。输出 JSON Schema，支持明确版本检查和未知字段拒绝。YAML 使用安全加载，并限制文档大小、嵌套复杂度；不做 `eval` 或任意表达式语言。

### 5.2 节点与边

第一版节点种类：`python`、`llm`。`python.execution` 区分 `task` 与 `async_actor`；I/O 密集是执行方式/资源需求，不是第三套调度系统。

Python 节点使用可导入的 `package.module:function`，函数由已准备的代码制品提供。不从业务调用请求接受代码或依赖安装指令。普通函数接收命名参数，返回声明输出端口对应的 Mapping；可额外支持显式 `NodeResult(outputs=..., metrics=...)`，不靠删除用户字典中特殊键收集指标。

每个输入端口在以下来源中必须唯一确定：数据边、声明的常量、声明的默认值。`edges` 是数据依赖的唯一权威来源；不再另设一套重复的 `depends_on`。常量不是隐式依赖，不从 prompt 字符串或任意表达式中推导边。

每条边使用结构化端点，避免用点号字符串模糊解析节点/字段。首版不做通用字段查询语言；复杂字段选择由显式输出端口或下游函数实现。

### 5.3 最小、可验证的 CPU DAG 样例

下面是**目标格式示例**。Codex 需要同时实现对应 schema 与示例模块，而不是只把 YAML 放进仓库。

```yaml
schema: maze.dag/v1
name: arithmetic
inputs:
  x:
    type: number
nodes:
  - id: double
    kind: python
    callable: maze_examples.arithmetic:double
    execution: task
    resources:
      num_cpus: 1
      num_gpus: 0
    inputs:
      x: {type: number}
    outputs:
      value: {type: number}
  - id: offset
    kind: python
    callable: maze_examples.arithmetic:offset
    execution: task
    resources:
      num_cpus: 1
      num_gpus: 0
    inputs:
      x: {type: number}
    outputs:
      value: {type: number}
  - id: merge
    kind: python
    callable: maze_examples.arithmetic:merge
    execution: task
    resources:
      num_cpus: 1
      num_gpus: 0
    inputs:
      a: {type: number}
      b: {type: number}
    outputs:
      total: {type: number}
edges:
  - from: {input: x}
    to: {node: double, port: x}
  - from: {input: x}
    to: {node: offset, port: x}
  - from: {node: double, port: value}
    to: {node: merge, port: a}
  - from: {node: offset, port: value}
    to: {node: merge, port: b}
outputs:
  result:
    from: {node: merge, port: total}
    type: number
```

对应函数的预期行为：`double(x)` 返回 `{"value": 2*x}`；`offset(x)` 返回 `{"value": x+1}`；`merge(a,b)` 返回 `{"total": a+b}`。输入 `x=3`，最终 `result=10`。两条上游路径可以并行。

### 5.4 部署验证必须覆盖

节点/端口重复、未知节点/端口、成环、自环、目标输入多重绑定、缺失必需输入、不可调用代码引用、不支持的 execution、非法资源值、LLM 服务引用不存在、未知协议版本、图或配置超限，都必须在接收业务调用前报错。

首版要求每个节点都能到达某个显式最终输出；拒绝无用途的悬空节点，避免悄悄忽略副作用。每个 Python 节点至少声明一个输出；纯副作用工作也必须显式返回状态并进入图的最终完成条件。

不强迫每个节点依赖全局输入；无入边的固定常量节点可以在每次调用时执行。所有节点都属于本次必需工作，只有全部成功才整体成功。首次失败后允许取消尚未完成的其他节点。

输入输出 schema 首版只做明确支持的类型子集与边界检查；不要声称能静态证明任意 Python 对象的类型。HTTP 输入/最终输出必须为受限 JSON 数据；内部 Ray 节点之间可以传输 Ray 支持的对象，例如数组。不得通过 HTTP 接收 pickle/cloudpickle。

### 5.5 不可变部署

部署完成后 DagSpec/ExecutionPlan 不变；调用请求只提供输入和允许的调用选项，不能改代码、拓扑、资源或模型部署配置。修改图需要新 Deployment ID，再卸载旧部署。首版不做无缝热更新、路由别名或多版本流量切分。

## 6. Ray 编译与数据流：必须遵守的规则

### 6.1 首版使用 Ray Core，而不是先绑定 DAG API

部署阶段验证 DAG，计算拓扑顺序和输出端口顺序，构建 Ray 函数/Actor 调用模板。调用阶段按顺序提交各节点，把上游 ObjectRefs 交给下游。

`.remote()` 的提交顺序不代表同步执行；下游可以在上游尚未完成时提交，Ray 负责等待。不要在编译器中重新实现 `ready -> 拿回上游值 -> 提交下游` 的调度循环。[R1][R2]

Ray DAG API/Compiled Graph 仅为后续可替换实现；不让它们的节点类型进入公共 DSL。首版无需通用 Backend 插件框架。

### 6.2 顶层 ObjectRef

所有用来表达执行依赖的上游 ObjectRefs，都必须作为 Ray 调用的顶层位置参数或关键字参数。若用户函数需要一个输入字典，应由远端 runner 在收到已经解引用的数据后重建它。[R1]

正确的内部形式类似：`runner.remote(blueprint, invocation_metadata, upstream_ref_a, upstream_ref_b)`。`blueprint` 中可以有字面常量和绑定说明，但不能把实际依赖引用塞入它的嵌套字典，再假设 Ray 自动等待。

禁止由入口进程 `ray.get()` 所有中间结果后再转发；最终输出和小型完成/指标信息可以返回入口。大输入被多节点复用时可使用一次 `ray.put()`；小输入不必为了形式统一强制 put。不要从业务任务内部返回新 `ray.put()` 的引用代替正常任务输出。

### 6.3 输出端口和 CompletionSignal

把固定业务输出端口展平为 Ray 多返回值，再附加一个小型 CompletionSignal/metadata 返回值。用固定端口顺序建立 `(node_id, port) -> ObjectRef` 映射。Ray Task 支持 `num_returns`；Actor 方法对应支持需在选定版本实测并纳入适配层。[R2]

例如 A 有 `text`、`score` 两个端口，可生成 `text_ref`、`score_ref`、`signal_ref`。B 只接收 `text_ref`。业务输出不必因指标采集而被整体传回控制面。

即使 `metrics=none`，仍保留用于完成与失败判断的最小信号；不要用指标系统承担执行正确性。若选定 Actor API 的多返回语义不满足需求，应在适配层做小而可测的等价转换，不要破坏顶层依赖原则，也不要不经测量为每条边新增提取任务。

### 6.4 失败必须抛异常

当前旧 runner 捕获异常并返回错误 envelope；新版不能照搬这种成功返回路径。[C4]

远程包装器可以捕获异常以补充节点 ID、错误分类和已知耗时，然后抛出可序列化的 `NodeExecutionError`，保留原始原因。只有 HTTP/SDK 边界才转换成错误响应。不得返回普通 `{"error": ...}` 后让 Ray 当成功结果继续执行下游。[R3]

### 6.5 全图完成与尽早失败

Invocation 注册本次所有业务调用的代表引用、各节点 CompletionSignal、最终输出引用。异步等待，任一节点明确失败时停止未完成工作；所有必需节点成功且最终输出校验通过才返回成功。

不得只等待某一个 sink，漏掉其他输出分支。取消和完成逻辑不要反过来成为一个数据 ready scheduler：节点可预提交，观察器只管本次调用生命周期。

释放 Python 引用、future、回调和结果缓存，避免将 invocation 数据捕获在全局闭包中。Ray 的分布式引用计数不负责清除 Maze 自己仍持有的引用。[R1]

## 7. CPU、异步 I/O、GPU 和 LLM 节点

### 7.1 资源声明

公开字段尽量对齐 Ray：`num_cpus`、`num_gpus`、`memory_bytes`（在编译层转为 Ray `memory`），以及经过版本验证的可选硬件 label/custom resource 约束。未知 Ray 参数不能未经审查任意透传。`num_returns`、内部命名、服务归属等由编译器控制，不能被用户资源配置覆盖。

普通业务 Task 不默认指定 `node_id`，不默认使用硬 NodeAffinity，不手工设置或删除 `CUDA_VISIBLE_DEVICES`。Ray 负责选择节点和 GPU。CPU/GPU 是逻辑调度资源，不是线程数或物理隔离承诺；小数 GPU 不承诺显存隔离。[R4]

`num_gpus=N` 表达单 Task 在一个节点上的资源需求，不自动变成跨机器模型并行。LLM 多 GPU 配置交给 Serve LLM/推理引擎；首版不另造通用 gang-scheduling DSL。硬件 label 是节点约束；混合不同 GPU 型号的单节点不能被简单的节点 label 误认为逐卡精确选择。

不要让框架自动检测“这是 I/O 函数”后修改资源；由节点声明 execution 和资源。默认 `task` 使用 1 个逻辑 CPU；低 CPU/零 CPU 必须显式配置并受全局在途数量限制。

### 7.2 同步计算与异步 I/O

同步 CPU/GPU 函数走 Ray Task。简单阻塞 I/O 可以显式用低 CPU Task，但不宣传它能无限扩展。

真正的 `async def` I/O 函数走 Async Actor。首版可以按一个部署中的 async 节点创建一个 Actor，不做动态 Actor 池或自研按节点负载路由；配置明确的 `max_concurrency`、Actor CPU 需求和排队上限。并发不够时先测量，再决定是否扩展副本。

Ray 普通 remote function 不原生接受 coroutine 作为异步 Task；Async Actor 内不能阻塞 `ray.get()`/`ray.wait()`，也不能把 CPU 密集函数假装成异步函数。[R5]

Actor 可以共享连接池等明确的服务状态；每次调用的输入、输出和指标必须放在局部变量中。不要用共享 `self.current_request`、进程 `cwd` 或临时环境变量保存请求上下文，不要把一个请求的取消传播成杀死整个共享 Actor。

### 7.3 LLM 的 GPU 属于模型服务

LLM 调用适配器是 I/O 工作，`num_gpus` 必须为 0。GPU 在 Ray Serve LLM 的模型副本配置中申请。禁止模型占着 GPU，而发送请求的 Task 又等待 GPU，造成死等。

普通 GPU Task 和常驻模型共享集群时，部署文档必须说明资源预算：模型副本可能长期占住 GPU，不能承诺它们会自动腾出设备让任意 CUDA Task 执行。两个都必须常驻且各需 1 GPU 的模型，不应在仅 1 GPU 的环境中宣称部署成功。

可以读取 Ray 资源快照作部署诊断与静态可行性提示，但该信息不是资源预留或实时显存账本。真正的分配仍交给 Ray/Serve，最终以就绪状态和部署 deadline 为边界。

## 8. LLM 服务准备与调用

### 8.1 首版支持范围

必须实现一个真实可用的 Ray Serve LLM + 经验证 vLLM 组合，首版 LLM 节点执行非流式 chat completion，输入为规范 messages/已声明请求参数，输出至少包含 `text` 和原始结构化 `response`。token usage 进入独立 metadata。

嵌入、语音、其他推理引擎、跨节点超大模型优化不作为首版全部交付承诺。服务配置可以保留明确的引擎参数区域，但只对实际验证过的接口声明支持。未知能力应拒绝或标为未验证，不能靠宽泛字典透传假装兼容。

### 8.2 ServiceSpec

服务配置至少包含：`service_id`、模式、模型公开名称、固定权重来源/版本、必要引擎配置、模型副本配置和启动超时。模型大小/型号只是用户提供的配置，不根据模型名称猜测足够的 GPU/显存。

模式包括：

- `managed`：Maze 通过 Ray Serve LLM 创建并拥有服务，是首版主要工作方式。
- `attached`：显式绑定既有 OpenAI-compatible endpoint；作为薄适配和测试能力，不替代 managed 交付。

一个 DAG 中多个 LLM 节点引用同一 `service_id`，复用一个服务。首版不做全局跨 DAG 的自动模型去重/内存引用计数。跨 DAG 共享使用显式 attached 引用；将来再设计统一所有者。

Managed 服务使用包含 Engine/Deployment 标识的稳定命名。记录谁创建、谁可删除；不要仅凭模型名推断归属。不同权重 revision、量化和并行配置不能视为同一模型实例。

### 8.3 准备服务

把 ServiceSpec 转为选定 Ray 版本公开支持的 LLM 配置，使用 Ray Serve LLM 应用构建与部署 API；不要从 Maze 启动或监控 vLLM/Transformers subprocess。不要自己分配模型进程端口、轮询进程组、维护 GPU leases 或 LRU 换模型。[R8]

默认配置先用固定、有限的 replica 数建立基线；可选自动扩缩容只映射到 Serve 的有界配置，不实现第二个 autoscaler。模型 ready 前 DAG 不接业务请求。部署失败只回滚本次创建的应用/Actor，不影响同一 Engine 的其他 DAG 或 attached 服务。

权重与代码必须对实际执行节点可访问。本地路径模式需明确共享挂载/一致镜像/预置条件，不能只检查 Head 上存在某路径。生产制品应有稳定版本或摘要；不要每次请求临时扫描 `model_cache`。

### 8.4 调用适配器

首版优先通过 OpenAI-compatible HTTP 接口调用 Serve LLM。使用可复用的异步客户端和 Async Actor 包装，让适配器输出正常 Ray ObjectRefs，继续参与下游依赖。Actor 调用接口与 Task 使用相同的端口和完成信号契约。

不要把 Serve `DeploymentResponse` 不加转换地当作普通 ObjectRef。若后续改用 Serve Handle/native composition，转换逻辑必须隔离并有版本测试；不得让 DeveloperAPI 类型流入公共 DagSpec。[R9]

多机环境的 endpoint 必须是 Worker 可达地址，而非默认 `localhost`。启动时验证服务地址绑定、advertised address、API token/环境变量注入和健康状态。普通 API 入口与内部模型 endpoint 不必暴露到公网。

LLM 请求设置有限 timeout、连接池与输出 token 上限。显式关闭 HTTP SDK 隐式重试，默认 Ray actor method 也不重试业务调用；用户显式允许重试时，说明重复推理/费用风险。不改变用户的 prompt 或采样参数去“自动修复”失败。

HTTP 请求取消只能保证调用侧按已验证路径释放资源；不能未经测试承诺 Serve/vLLM 已即时中止 GPU 推理。必须测试实际取消行为；无法确认时报告 best-effort/incomplete，并依靠有限请求 timeout/生成上限控制残留工作，绝不为一个请求终止共享模型。

## 9. Invocation：并发、deadline、失败和清理

### 9.1 InvocationScope

内存中至少包含：唯一 invocation_id、deployment_id/revision、本地单调时钟起点和 deadline、已提交调用的代表引用、完成信号/最终输出引用、当前状态、取消标记、可选指标和清理结果。

同一部署每次请求都创建独立 Scope。不提供历史查询；取消 API 只操作当前活动请求。允许调用方提供格式受限的 invocation_id 以便主动取消；活动 ID 冲突返回 409。ID 不是持久幂等键：完成后重用 ID 不承诺去重。

### 9.2 有界 admission，不重新调度节点

至少限制：Engine 级活动 invocation、有限等待队列、DAG 节点数、输入/输出大小、在途节点调用总预算、异步 Actor 并发与服务排队。

在提交该次图之前，为它的节点调用数获得预算。图超过硬上限直接拒绝；暂时无容量则进入有限等待队列或按配置拒绝。不要提交半张图后让上游占住全部额度、下游永远拿不到额度。

首版可先使用单 EngineHost 的轻量 admission gate；它计数的是请求和待完成调用，不是 CPU/GPU/节点资源。若入口采用 Serve，其 `max_ongoing_requests` 是每副本限制，`max_queued_requests` 是每调用方限制，不能冒充全局引擎额度。[R6][R7]

建议起始配置（只为建立可测试边界，非性能承诺）：

```yaml
schema: maze.engine/v1
cluster:
  mode: managed-local
api:
  host: 127.0.0.1
  port: 8000
limits:
  max_active_invocations: 32
  max_waiting_invocations: 64
  max_inflight_node_calls: 8192
  max_nodes_per_dag: 256
  max_input_bytes: 1048576
  max_output_bytes: 8388608
timeouts:
  default_invocation_s: 120
  max_invocation_s: 600
  deployment_s: 900
  shutdown_grace_s: 30
  cancellation_grace_s: 5
```

上述数值必须集中配置并验证，不散落为魔法常量。实际 workload 到来后再调整。队列满返回明确 429；部署未 ready/引擎不可用返回 503。不要接受请求后默默丢弃。

### 9.3 Deadline 与取消

必需支持一次 Invocation 的整体 deadline，包括 Engine 已接收后的等待时间。起点和计时口径写入 API 文档。远端节点不能直接比较来自其他机器的 `perf_counter`/单调时钟值。

协调进程以本地 deadline 控制整个请求，超时后取消本次所有未完成调用；I/O 和 LLM 客户端另有有限的操作 timeout。`ray.get(timeout=...)` 或超时一个 Python await 只停止等待，不等于取消远端计算。

取消普通 Ray Task 可先 cooperative，必要时在 grace 后 force；取消 Actor 方法必须按 Ray 允许的方式处理，不能 `force=True`，更不能杀共享 Actor。`recursive=True` 主要处理由某个 Task 提交的子任务，不是自动遍历整张 ObjectRef DAG；必须依靠 Scope 的引用清单。[R10]

超时、HTTP 断开、SDK task 被取消、用户显式取消、卸载部署，统一进入同一套取消/清理逻辑。函数有外部副作用时，取消不等于回滚。首版不承诺任意 Python 函数的逐节点硬实时 timeout；不要把普通等待 timeout 命名成这种保证。

清理有独立有限 grace。响应结束但远端终止尚未确认时，可以保留**有界、只用于终止和额度核算**的 cleanup 记录；仍在运行的调用不得因响应已返回就被从容量预算中完全抹掉。残留超过上限要降级/停止接纳并报错，不得静默累积后台任务。

### 9.4 重试契约

默认所有业务节点 `max_retries=0`、`retry_exceptions=False`，LLM/HTTP SDK 隐式重试关闭。这是保守工程默认，不代表 Ray 没有重试能力。

允许明确声明为纯计算/幂等的 Task 启用有限 Ray 重试；不要在 Maze 再套自己的 task 重提交流程。actor restart 是基础设施生命周期策略，与是否重放业务方法分开。用户代码异常默认直接失败。

Ray Task 重试和对象 lineage reconstruction 不提供业务 exactly-once；即使设为零重试，也不对“调用方重发/执行后响应丢失”等情况提供恰好一次保证。文档需明确副作用任务由应用负责幂等或去重。[R3]

### 9.5 错误类型和状态

建议核心错误码：`DAG_INVALID`、`DEPLOYMENT_NOT_READY`、`SERVICE_START_FAILED`、`RESOURCE_UNAVAILABLE`、`OVERLOADED`、`INVOCATION_TIMEOUT`、`INVOCATION_CANCELLED`、`NODE_FAILED`、`UPSTREAM_FAILED`、`RESULT_INVALID`、`RESULT_TOO_LARGE`、`ENGINE_UNAVAILABLE`。

错误响应包含 request/deployment/node 标识、错误码、脱敏消息、有限的 cause 信息、retryable 提示以及 cleanup 状态。`retryable` 是建议，不代表系统已自动重试。默认不向 HTTP 返回原始完整 traceback、环境变量、prompt 或路径中的秘密。

首版 HTTP 默认：schema 422、ID/版本冲突 409、不存在 404、过载 429、未就绪/运行基础设施不可用 503、请求 deadline 504、节点执行或最终结果契约失败 500。成功为 200；异步准备部署为 202；具体枚举落实到 OpenAPI 和测试，避免错误也返回 200。

## 10. 指标：随调用返回，不依赖监控系统保证正确性

### 10.1 核心来源

- Engine 本地计时：排队等待、执行阶段、清理阶段，明确 `engine_elapsed_ms` 不含客户端网络和未知上游代理等待。
- 节点包装器计时：同一进程 `perf_counter_ns()` 的差值；异步 I/O elapsed 包括等待，不能叫纯 CPU/GPU compute time。
- LLM 响应 usage：prompt/completion/total tokens，模型 ID；无 usage 就缺失，不能填 0。
- 用户自定义小型数值 metadata：通过显式 NodeResult 返回，做键数、类型和总大小限制。

不要把 State API 放在正常响应必经路径。Ray 官方明确不保证它对完成资源始终给出完整、一致快照。它只能作为可选诊断来源，失败不改变成功的业务响应。[R11]

### 10.2 Metrics 模式

`none`：只返回业务输出、标识和必要状态；仍有最小完成信号。

`basic`：Engine/节点 elapsed、成功节点状态、可获取的执行位置、LLM usage、自定义小型指标。基础时间直接来自包装器，不等待 Ray State。

`detailed`：在 basic 上，按能力附加 Ray 事件诊断或后端实际返回的请求级推理数据；允许缺失，并给出来源/完整性说明。该模式不能强制拖延业务响应直到所有监控事件到齐；查询有单独很小且可配置的预算，也不能留下无界查询线程。

首版对非流式接口，不把第一个 HTTP 字节时间当 TTFT，不用数据块间隔猜 TPOT。没有请求级字段就不返回该数值。服务级 GPU/KV cache/直方图不属于本次调用指标。若后续采用 vLLM 特殊请求级字段，验证 Serve 是否透传，不能只因 vLLM 文档出现就宣称支持。

### 10.3 正确性口径

并行节点 elapsed 总和可以大于 DAG elapsed；不直接相加冒充整体耗时。创建到开始之间包含多种等待，不一律叫 scheduling time。重试后返回的 metadata 默认代表最后可观察的尝试；不能宣称是所有尝试的精确计费总量。

需要时包含 `source`、`complete`、`missing_fields`、`attempt_scope`。失败响应只收集已可获取的少量指标，不为补齐失败任务 metadata 无限等待。Ray task ID/attempt 等字段仅在已验证公开接口能提供时填写，否则为缺失，而不是使用私有字段凑数。

示例响应中的数值仅为格式示意，不是实测：

```json
{
  "invocation_id": "example-invocation",
  "deployment_id": "arithmetic-example",
  "status": "succeeded",
  "output": {"result": 10},
  "metrics": {
    "engine_elapsed_ms": 24.8,
    "admission_wait_ms": 0.2,
    "nodes": {
      "double": {"execution_ms": 1.1, "source": "node_wrapper"},
      "offset": {"execution_ms": 1.0, "source": "node_wrapper"},
      "merge": {"execution_ms": 0.6, "source": "node_wrapper"}
    },
    "complete": true,
    "missing_fields": [],
    "attempt_scope": "last_observed_attempt"
  }
}
```

## 11. Maze 包办启动与关闭

### 11.1 必须有两条 managed 路径

`managed-local`：Maze 启动单机 Ray、模型服务和 API。适用于开发/单机部署。

`managed-onprem`：用户提供现成机器清单、私网地址、SSH 权限和运行环境配置，Maze 封装 Ray 官方 on-prem cluster launcher 或其官方启动流程，自动启动 Head/Workers，再在 Head 上运行 Engine/API。不能仅输出“请逐台手工执行 ray start”作为完成。[R12]

“包办启动”不是购买机器、安装驱动或绕过 SSH 授权。首次安装可以有明确前置条件；用户不必预先手动启动 Ray。默认不做云机器采购、Kubernetes operator 或跨网络自动打洞。

### 11.2 生命周期步骤

`maze start`：校验配置和所有权 -> 检查端口/环境 -> 启动并连接 Ray -> 确认所需节点/能力可用 -> 启动 API -> Engine Ready。

`maze deploy`：解析/验证 DAG -> 准备固定代码环境 -> 编译 -> 创建 managed 服务和必要异步 Actor -> 确认就绪 -> Deployment Ready。不能在异步环境初始化失败前就宣称 Ready。

`maze undeploy`：原子进入 DRAINING -> 拒绝该部署新请求 -> 在 deadline 内等待/取消当前请求 -> 关闭客户端/专属 Actor -> 删除该部署 owned Serve 应用 -> 清理内存配置。

`maze stop`：停止接受新部署和调用 -> 对所有部署 drain -> 清理本 Engine 拥有的服务 -> 停止本 Engine 拥有的 Ray 节点 -> 结束 API/管理进程。

以上步骤应可重复调用，具有明确的 no-op/冲突行为。启动失败有反向回滚；停止未完全确认时输出未清理资源，不伪报成功。

### 11.3 所有权和安全停止

可以保存很小的本地 Engine 管理文件，包含 owner_id、受限访问的管理 endpoint、集群配置标识、启动 PID/必要进程身份、拥有的节点范围与启动状态。权限受限、原子写入，不保存业务数据。记录不是执行恢复数据库。

不使用模糊 `pgrep`/`pkill` 猜测所有权，不因发现占用端口就杀进程，不在共享宿主机上盲目执行 `ray stop --force`。Ray namespace/应用名不是物理进程隔离保证。首版 managed 模式要求授权的专用 Ray 运行边界；发现已有未知 Ray 实例应拒绝启动并说明冲突。

`ray.shutdown()`、删除某个 Serve 应用、整体 Serve shutdown、停止 Ray 集群作用域不同；必须按所有权选择正确 API。单个 DAG 卸载不停止其他 DAG、attached 服务或整个集群。

进程崩溃后不承诺旧 invocation 恢复。重启检测遗留 managed 资源时应明确报告、按已验证归属清理或重新部署，不把旧服务自动当作新进程的业务运行历史。

### 11.4 网络与最低安全措施

默认 API 绑定 loopback；远程使用需显式设置 bind 和 advertised address，并启用最小 token 保护或声明由受信任网关保护。管理接口只给可信部署者，业务 invoke 不允许提交代码。Ray 的内部端口和 Serve 管理面不能直接开放到不受信任网络。

保留 secrets 的环境/受保护文件引用，不写进 DagSpec 的可返回字段，不打印完整 SSH key、API key 或模型凭据。不实现用户体系和 RBAC，但也不以此为理由裸露任意代码部署接口。

文件沙盒本轮未实现，所以不能承诺宿主文件访问隔离。用户代码可能自行写日志；Maze 不保存业务历史不等于保证所有下层日志都没有业务内容，需在文档说明。

## 12. 代码环境与依赖分发

以已构建 wheel/镜像或受信任固定版本代码目录作为节点实现来源。利用 Ray `runtime_env` 等官方机制分发代码/依赖，在部署时准备，不在每次调用时安装包。[R13]

包导入在正确的执行环境中验证；Head 本地 import 成功不等于 Worker 可执行。缺包、不同节点版本不一致、模型文件不可见均须有清楚的部署/执行错误。

固定 `working_dir` 仅作为代码制品环境，不用它冒充每次请求可变文件沙盒。不要修改共享 Actor 的 cwd，也不要自动同步任意用户工作目录、凭据或模型缓存。

依赖准备缓存允许跨请求复用；Invocation 输入/输出不进入部署缓存。CPU 示例依赖必须足够轻，打包后在仓库外的干净环境也能运行，而不是靠当前目录隐式导入成功。

## 13. 对外接口与内存状态

### 13.1 CLI 目标

```text
maze start --config engine.yaml
maze status
maze deploy dag.yaml --deployment-config deployment.yaml
maze deployments list
maze deployments show <deployment_id>
maze invoke <deployment_id> --input input.json --metrics basic
maze cancel <invocation_id>
maze undeploy <deployment_id>
maze stop
```

命令使用同一个 Engine API/同一语义，不把 CLI 和 HTTP 各自实现成独立执行器。前台启动优先；后台模式若实现，必须可安全 status/stop，不再次造复杂进程侦测平台。

### 13.2 HTTP 目标

| 方法与路径 | 用途 |
|---|---|
| `GET /healthz` | 管理进程基本存活 |
| `GET /readyz` | Engine 接受操作所需条件是否满足；与单个 DAG 就绪分开 |
| `POST /v1/deployments` | 提交 DagSpec + 部署配置，返回 ID 和 preparing/ready 状态 |
| `GET /v1/deployments` | 仅当前部署，不是业务运行历史 |
| `GET /v1/deployments/{id}` | 当前部署状态、脱敏摘要、必要错误 |
| `POST /v1/deployments/{id}/invoke` | 非流式调用 |
| `POST /v1/invocations/{id}/cancel` | 仅取消当前活动请求；结束后通常 404 |
| `DELETE /v1/deployments/{id}` | 有界 drain + 卸载 |

不提供 `/runs/history`、历史 task/events/artifacts 等接口。当前 preparing/failed 部署状态允许保留到用户删除；它是部署管理，不是业务请求历史。

Invoke 输入包含 `input`、可选 invocation_id、`timeout_s`、`metrics`；每项在服务器端限制。输入不能覆盖代码来源、任意 URL、模型 endpoint、资源或引擎管理权限。LLM 允许的生成参数必须在部署时声明边界。

### 13.3 Python API

提供薄 HTTP `MazeClient` 和独立于 HTTP 的可嵌入运行接口，两者共用协议模型/核心实现。同步 SDK 可以封装异步接口，但不能在事件循环中错误嵌套 `asyncio.run()`。

HTTP 最终输出为受大小限制的 JSON；不能把 Ray ObjectRef 序列化成字符串给远程用户当作可持久化结果。Python 嵌入接口可返回正常 Python 值；原始引用透传不作为首版公共契约。

## 14. 旧代码迁移与删除策略

先审计实际引用，不以文件名机械决定去留。下表是目标方向，不是立即执行的删除命令。

| 旧模块/能力 | v2 处理 | 删除/迁移条件 |
|---|---|---|
| `maze/core/scheduler/scheduler.py` | 不进入新执行路径 | 新 Core 编译、失败传播、取消与全图完成通过测试后移除旧默认路径 |
| `scheduler/resource.py`、节点选择/lease | 删除自研资源调度 | CPU/GPU 需求成功映射到 Ray，跨节点测试通过 |
| `scheduler/strategy.py`、`queues.py`、HACS/估时 | 从产品主线移除 | 新 admission 只管请求预算，不再依赖旧 task queues |
| `scheduler/llm_instance.py` | 用 Serve LLM 部署适配取代 | 至少一个真实 managed LLM 能部署、复用、卸载；未实测不得宣称替换已验证 |
| `scheduler/standby_worker.py` | 删除通用预热池 | 普通 Task/明确 Async Actor 路径可工作，不保留同名第二套资源池 |
| `scheduler/runner.py` | 选择性复用纯函数思想，重写边界 | 去掉源码 exec、全局环境修改和成功返回错误 envelope，支持端口与 metadata |
| `scheduler/runtime.py` | 由 InvocationScope/ExecutionPlan 替代 | 不再把全部中间结果 materialize 回协调器，不复用跨请求状态 |
| `maze/core/worker/worker.py` | 官方 Ray 集群流程替代 | managed-local 和 managed-onprem 启动都有实现，非仅打印命令 |
| `workflow/dynamic.py`、`dynamic_store.py`、Run 持久化 | 移出首版产品路径 | 不再有业务恢复/动态节点接口；保留迁移说明 |
| 旧文件沙盒/artifact subsystem | 不迁入新 Core | 明确标注延期；不做删除用户实际文件/权重/历史数据的迁移脚本 |
| 旧 Python DAG builder/validator | 可复用纯逻辑 | 必须输出同一 v2 DagSpec，不维持另一套执行机制 |
| CLI | 保留易用入口，重写内部 | 包办启动且安全停止，不把用户赶回手工搭环境 |
| Workbench、GAIA、旧应用示例 | 从默认安装/测试/导入依赖中解耦 | 历史实现可在旧提交查阅；无需为旧 UI 保持全套兼容 |
| README、文档、许可证 | 重写产品说明，保留归属 | 清楚区分论文版本与 v2，不改写论文所对应的事实 |

不承诺兼容旧 `maze.workflow/v1` 或旧 Run HTTP API。可写小型迁移指南，不能为了兼容旧研究功能再拉入整套旧运行时。是否提供旧 Python 装饰器到 v2 的薄转换由实际复用成本决定，非首版验收前置。

## 15. 分阶段执行计划

各阶段可分成小提交/本地补丁，但不得跳过验收直接宣布整体完成。下列路径可按项目布局调整，职责和证据必须保留。

### P0 — 审计、版本基线、测试基础

**实施：**记录真实 HEAD/工作区；梳理旧入口、依赖和测试；检查新路径能否隔离旧 import 副作用。移除测试/benchmark 源码的 ignore，保留结果与秘密的 ignore。确定 CPU 与 GPU 两组约束/镜像基线，设置 pytest markers、格式/静态检查。

**交付：**`docs/v2/BASELINE.md`、`docs/v2/IMPLEMENTATION_STATUS.md`、依赖约束、CI 骨架、旧到新迁移清单。

**验收：**干净 CPU 环境可安装最小依赖，`import maze` 不启动集群、不强制 import vLLM。现有不能运行的测试明确记录原因，未跟踪敏感文件未被加入变更。

### P1 — 协议与验证器

**实施：**类型化 DagSpec、NodeSpec、端口/边、DeploymentSpec、ServiceSpec、EngineConfig；JSON/YAML 同一路径；导出 JSON Schema；实施第 5 节的验证及大小约束；提供 arithmetic 示例及对应 Python 包。

**交付：**`maze/spec/`、`tests/v2/unit/`、`examples/v2/arithmetic/`、`docs/v2/SPEC.md`。

**验收：**合法链/并行汇合/多输出通过，非法图逐项拒绝；输入顺序变化不能改变输出端口绑定。纯 schema 测试不需要 Ray/GPU。示例 YAML 与源码真实匹配。

### P2 — 最小 Ray Core 编译与调用

**实施：**不可变 ExecutionPlan、同步 Task runner、固定输出端口、多返回 CompletionSignal；ObjectRef 作为顶层参数；独立 Invocation；异常传播；异步等待全图完成；基本耗时随 metadata 返回。

**交付：**`maze/compiler/`、最小 `maze/runtime/`、真实单机 Ray CPU 集成测试。

**验收：**arithmetic 输入 3 返回 10；链、菱形、多输出能重复运行；上游异常时下游用户函数不执行；多请求不串结果。测试使用真实 worker 进程，不以 `local_mode=True` 或完全 Mock 代替。证明中间大结果不必回流协调器。

### P3 — Engine/API/SDK、admission 与请求生命周期

**实施：**单状态所有者 EngineHost、当前部署内存注册表、轻量 HTTP/SDK、调用 ID、有限等待/活动预算、整体 deadline、取消、drain；先接入最小的安全 managed-local 启动，支持用户通过 Maze 命令跑 CPU DAG。

**交付：**API、CLI/SDK 初版、`tests/v2/integration_cpu/test_api_*.py`、OpenAPI。

**验收：**部署/查询当前状态/调用/卸载闭环；过载 429、未准备 503、超时 504；客户端断开和取消后不会无限留着调用；命令行与 Python/HTTP 输出契约一致。业务历史不落盘。

### P4 — 异步 I/O 与 managed LLM 服务

**实施：**明确的 Async Actor 执行适配、连接池、固定并发；LLM ServiceSpec -> Serve LLM；managed/attached 所有权；启动 deadline、就绪检查；异步非流式 chat 调用；token usage；LLM 调用不申请 GPU。

**交付：**`maze/services/`、异步 runtime、Mock HTTP fixture、真实 GPU smoke 脚本和小模型示例配置。

**验收：**无 GPU 环境下，Mock 只证明适配器和协议；有授权 GPU 环境下，实际 managed 部署一个模型，至少连续/并发调用数次，模型未按请求反复加载，usage 能对应请求，卸载仅清理 owned 服务。请求 A 取消不影响请求 B。没有真实 GPU 时该部分保持待验证，不能被 attached endpoint 测试冒充。

### P5 — 完整的一键启动、多机管理和关闭安全

**实施：**完善 managed-local；封装官方 on-prem launcher，自动启动指定 Head/Worker、在 Head 上运行 Engine、准备代码并输出实际可达 API；实现有限管理状态文件、安全 status/stop、归属验证、启动回滚和有界关闭。对模型路径/endpoint 的多机可见性作检查。

**交付：**`maze/cluster/`、本地/多机配置样例、启动 E2E、`docs/v2/OPERATIONS.md`。

**验收：**单机从未启动 Ray 开始，仅用 Maze 命令完成启停。多机在至少两台已授权机器上证明不同 Ray 节点间的任务/对象传输。两进程模拟节点只能作为本地集成证据，不能标成真实多机。端口冲突、SSH 失败、模型启动失败、重复 stop 不误杀外部进程。

### P6 — 可靠性、指标和资源收敛

**实施：**完成 basic/detailed 指标口径、缺失标注；完善已完成/失败/取消 races、部分提交失败、异常序列化、Actor 中止、模型不可用、输出过大等情况；强化引用释放、bounded cleanup、服务调用超时；建立合成压测/benchmark。

**交付：**故障注入测试、生命周期压力测试、`benchmarks/v2/`、`docs/v2/METRICS.md` 和测试证据。

**验收：**Ray State 不可用不影响正常业务结果；持续多轮调用没有随完成请求数线性增长的活跃 Scope/引用；过载始终有界；引用、连接和 owned Actor/服务在卸载后收敛。GPU 终止无法确认时正确标注，而非宣称已完全取消。

### P7 — 默认路径切换、删旧、打包与交付

**实施：**将 CLI/import/README/示例切换到 v2；依据迁移表移除默认产品中的旧 scheduler、资源/模型进程管理、动态 Run 持久化和 UI 耦合；清理依赖；保留许可证与论文版本说明；完成文档、打包/干净安装 E2E、最终审计。

**交付：**可分发的开发版 wheel/源码、完整 v2 文档、CPU 和 GPU/多机验证结果表、兼容性/已知限制说明、简洁改动摘要。

**验收：**新默认执行链不再导入旧自研调度路径；一条官方文档流程能从安装、Maze 启动、部署、调用到停止；未实际执行的 GPU/多机/性能项目明确待验证。不得在替代实现未工作时只删除旧功能并宣称成功。

## 16. 必需测试矩阵

以下是最低测试范围，不是选择题。可组合用例，避免大量重复单测；关键语义必须有真实 Ray 测试。

| 编号 | 场景 | 必须证明的行为 | 环境 |
|---|---|---|---|
| T01 | schema/未知版本/非法资源 | 在调用前拒绝，不启动业务节点 | Unit |
| T02 | 链与 arithmetic | 正确值、输出端口不串位 | CPU Ray |
| T03 | 固定菱形 | 两条可并行路径被并行提交/运行，汇合正确 | CPU Ray |
| T04 | 嵌套参数构造 | 编译器把依赖展平为顶层引用，远端再组装 | CPU Ray |
| T05 | 多端口大对象 | 下游只接相应端口，协调器只读最终值和小 metadata | CPU Ray |
| T06 | 同 DAG 多请求 | 独立引用/输出/metrics，无共享 current_request 污染 | CPU Ray |
| T07 | 不同 DAG 同时存在 | ID、配置、取消和卸载互不干扰 | CPU Ray |
| T08 | 上游失败 | Ray 异常传播，下游用户代码不执行 | CPU Ray |
| T09 | 多 sink，一条失败 | 不因另一条成功就返回整体成功 | CPU Ray |
| T10 | 限量可重试纯 Task | 只按配置重试，不形成 Maze+SDK 多层重试 | CPU Ray |
| T11 | 队列满与 oversized DAG | 明确拒绝；不无限提交/保留输入 | CPU Ray/API |
| T12 | 等待中超时/断开 | 从有限队列移除，预算不泄漏 | CPU API |
| T13 | 运行中取消/timeout | 遍历本次调用，其他请求不受影响 | CPU Ray/API |
| T14 | Actor 取消 | 不用 force=True、不杀共享 Actor | CPU Ray |
| T15 | 提交一半时失败 | 已提交工作得到取消/清理，不留下半图 | CPU Ray |
| T16 | 结果或异常不可序列化 | 可识别失败，无无限悬挂/错误假成功 | CPU Ray |
| T17 | 输出 JSON 过大/无效 | 有界返回错误，不偷偷截断业务数据 | CPU API |
| T18 | basic/detailed/none | 执行结果不变；未知指标缺失而非 0 | CPU API |
| T19 | State API 故障 | basic 仍工作，detailed 降级 | CPU Ray |
| T20 | 真异步 I/O | await 重叠、并发有界、连接可清理 | CPU Ray + HTTP fixture |
| T21 | 普通 GPU Task | Ray 选择 GPU；Maze 不覆写设备环境 | GPU |
| T22 | managed 模型部署 | 真实 Serve LLM 启动，未 ready 不接请求 | GPU |
| T23 | 模型跨请求复用 | 多次调用不重复加载一份模型 | GPU |
| T24 | LLM usage/取消 | usage 对应请求；取消无外溢；不夸大后端中止保证 | GPU |
| T25 | GPU 资源不够 | 部署超时/错误有界，不能假 ready | GPU/受控资源 fixture |
| T26 | multi-node 执行 | 实际至少两台机器上的代码/数据可达与结果正确 | 真实多机 |
| T27 | 节点丢失 | 请求按策略失败/有限重试；不承诺恢复旧业务历史 | 多机/故障注入 |
| T28 | 启动冲突/回滚/重复停止 | 不误杀、不遗留未报告资源 | CPU + 多机 |
| T29 | 卸载和在途调用竞争 | 停止接新请求，按 drain 契约清理 | CPU/GPU |
| T30 | 控制进程死亡 | 明确调用失败，不伪称续跑；重启有遗留资源说明 | CPU Ray |
| T31 | 多轮压力/卸载重部署 | 活跃计数归零、引用/内存无持续线性增长 | CPU Ray，GPU 可选 |
| T32 | 干净 wheel 安装 | 不靠源码目录、未声明依赖或 GPU 才能 import | CPU |
| T33 | 不保存业务历史 | 没有自动 run.json/events.jsonl/业务数据库写入 | CPU |
| T34 | 密钥和日志 | 管理响应/默认日志不打印敏感配置和请求正文 | CPU |

T03 并行性尽量用事件/屏障证明而非容易波动的固定毫秒阈值。T05 不要求零序列化/零拷贝，只要求不强制通过 Engine 传输全部中间结果。T31 先预热再观察趋势；Ray worker/cache 保留内存不等于泄漏，禁止强行要求进程 RSS 每次回到初始值。

故障注入只对本次测试拥有的 Ray 实例执行。模拟 Ray 节点、Mock 模型、多机实测分别报告，不混用名称。

## 17. 合成基准与未来 workload 接口

先提供可重复、可配置的合成 workload：链形、并行汇合、宽图、多输出、大数组输入/输出、异步 HTTP 等待、CPU 计算、可选 GPU 运算、Mock LLM 和真实 LLM 小规模调用。

每个 benchmark 记录：版本、机器/资源、图参数、并发数、输入规模、预热、样本数、吞吐、P50/P95/P99、错误/拒绝率、Engine/worker 内存趋势和清理情况。结果只在 benchmark 显式要求时输出到调用方指定文件，不让引擎顺带长期存指标。

建立直接 Ray 基线与 Maze v2 的同图对照。条件允许时增加旧 Maze 基线；协议/实现已变化时解释不可比因素。不要混淆模型下载/首次加载时间与稳态 Invocation latency。

初始没有固定速度提升目标，不要求“比旧版快 X%”。必须报告测得的数值、环境和不确定性，不用单次样本宣传吞吐。性能不佳时，优先定位 I/O 方式、输入回流、过细任务、Actor 串行、模型容量、admission 和引用占用；只有证据充分才引入 Actor 副本、任务融合或 Compiled Graph。

建议提供简单 `BenchmarkCase` 接口/配置，未来用户给出一个 DAG、环境、输入生成器和期望输出验证函数，就能接入相同基准；不要建设独立 benchmark 平台或重做 GAIA 产品功能。

## 18. 文档、质量与最终完成定义

### 18.1 必需文档

- `docs/v2/ARCHITECTURE.md`：三种生命周期、Ray/Maze/Serve 分工、默认单控制进程限制。
- `docs/v2/SPEC.md`：DSL、端口绑定、资源、错误与协议版本。
- `docs/v2/QUICKSTART.md`：纯 CPU 从零启停、异步 I/O、真实 LLM、多机配置。
- `docs/v2/OPERATIONS.md`：归属、启动/回滚、ready、drain、stop、网络/凭据。
- `docs/v2/METRICS.md`：计时边界、usage 来源、缺失、重试口径。
- `docs/v2/MIGRATION.md`：论文版本与 v2 边界，不承诺的旧能力，不能删除用户数据。
- `docs/v2/TEST_REPORT.md`：实际 PASS/FAIL/SKIPPED/BLOCKED 矩阵与复现命令。

文件可适度合并，不需要为目录数量而写文档。代码中保留必要类型、异常上下文、docstring；避免不必要的抽象层和无使用方的接口。

### 18.2 CI 与构建

默认 CI 至少跑协议/单元测试、真实单机 Ray CPU 集成、格式/静态检查、打包及干净安装 smoke。GPU/多机为带明确 runner 条件的独立验证，缺环境要可见地跳过。

目标命令示例（按最终工具链落地，不得留下失效命令）：

```bash
python -m pytest tests/v2/unit -q
python -m pytest tests/v2/integration_cpu -q
python -m pytest tests/v2 -m gpu -q
python -m pytest tests/v2 -m multinode -q
python -m build
```

GPU/multinode marker 必须注册；没有环境时给明确 skip 原因。执行报告保存实际 stdout 摘要/退出码，不手工编造测试输出。构建产物不需要自动发布。

### 18.3 最终验收清单

- [ ] 只支持用户要求的预编写、静态、无条件分支、非流式 DAG；固定并行汇合可用。
- [ ] 一个部署可接受多个并发输入，请求之间结果和指标不混淆。
- [ ] 默认依赖与数据传递走 Ray 顶层 ObjectRef，不在入口串行拉回所有中间数据。
- [ ] 普通任务的节点/GPU 选择交给 Ray，没有自研余额表和默认硬 NodeAffinity。
- [ ] managed 模型服务由 Ray Serve LLM 准备与复用，不使用旧模型 subprocess 管理器。
- [ ] Maze 真正提供 managed-local 和 managed-onprem 的启动实现，而非只生成手工说明。
- [ ] 全图成功/失败、有限 admission、timeout、取消、drain 和清理都有测试。
- [ ] basic 时间/usage 随结果返回，State API 故障不破坏业务响应。
- [ ] 无业务 Run 持久化、旧 Workspace/历史数据库/文件沙盒的新实现。
- [ ] 测试/基准源码已进入版本控制，秘密/模型权重/结果文件没有误入。
- [ ] 默认 CLI/import/打包路径已经指向 v2，旧重型模块不再被无意加载。
- [ ] 文档能复现纯 CPU 闭环；GPU/真实多机的实际验证状态如实呈现。
- [ ] 清楚说明非 exactly-once、不保证控制进程故障后的旧请求恢复、资源是逻辑约束等边界。
- [ ] 没有未授权的远程部署、停止服务、下载大模型、发布或推送操作。

全部代码实现与全部环境验收是两种状态。若 CPU 部分通过、GPU/多机缺环境，最终报告必须写“相应实现已完成、真实验证待执行”，不能写“整个工业版本已经验证完成”。

## 19. Codex 最终应返回的内容

按实际结果给出：

1. 已实现的功能和刻意没有实现的非目标。
2. 关键新模块、旧模块移除/保留情况以及与本计划的有理由差异。
3. 从干净环境开始的安装、Maze 启动、部署、调用、取消、卸载、停止命令。
4. 测试矩阵：哪些实际执行、什么环境、什么结果；哪些缺 GPU/多机/模型权限。
5. 基准结果或当前只有基准工具的说明，不作未经测量的速度承诺。
6. 剩余阻塞与下一次可以直接继续的阶段。

报告应围绕证据，不只罗列修改文件。除外部资源授权问题外，不把已明确的产品选择再交回用户决定。

---

## 20. 实施依据与核查资料

以下链接用于 Codex 核对 API 和版本；日期为 2026-09-08。它们不是已经完成实验的证据。`latest` 会变化，实施时以选定版本文档及已安装源码为准；不要混用 `master`/nightly 的接口。

### 当前仓库依据

**[C1]** main 引用核查：`e7b90f5c4cd1ad7cc1cfb5926d853ffedd2b40b1`。  
`https://api.github.com/repos/maze-agent/Maze/git/ref/heads/main`

**[C2]** 旧依赖配置，含 Ray 2.50.1 与可选 vLLM 配置。  
`https://github.com/maze-agent/Maze/blob/e7b90f5c4cd1ad7cc1cfb5926d853ffedd2b40b1/pyproject.toml`

**[C3]** 旧 ignore 配置，忽略 tests/benchmarks 源目录。  
`https://github.com/maze-agent/Maze/blob/e7b90f5c4cd1ad7cc1cfb5926d853ffedd2b40b1/.gitignore`

**[C4]** 旧 runner 捕获异常返回 envelope、执行模型与环境处理。  
`https://github.com/maze-agent/Maze/blob/e7b90f5c4cd1ad7cc1cfb5926d853ffedd2b40b1/maze/core/scheduler/runner.py`

### Ray 官方文档

**[R1] Objects**：顶层/嵌套 ObjectRef、引用计数、对象生命周期。  
`https://docs.ray.io/en/latest/ray-core/objects.html`

**[R2] Tasks**：任务依赖、多个返回值、资源和失败机制。  
`https://docs.ray.io/en/latest/ray-core/tasks.html`

**[R3] Task Fault Tolerance**：异常传播、重试与非 exactly-once 边界。  
`https://docs.ray.io/en/latest/ray-core/fault_tolerance/tasks.html`

**[R4] Resources**：逻辑 CPU/GPU/memory、资源请求与限制。  
`https://docs.ray.io/en/latest/ray-core/scheduling/resources.html`

**[R5] AsyncIO / Concurrency for Actors**：async/threaded Actor、max_concurrency、不支持普通 async remote task。  
`https://docs.ray.io/en/latest/ray-core/actors/async_api.html`

**[R6] Limit pending tasks**：提交速度和资源调度不是同一个约束。  
`https://docs.ray.io/en/latest/ray-core/patterns/limit-pending-tasks.html`

**[R7] Serve deployment 配置**：每副本 ongoing 和每调用方 queued 限制。  
`https://docs.ray.io/en/latest/serve/api/doc/ray.serve.deployment_decorator.html`

**[R8] Ray Serve LLM Quickstart**：公开构建与部署方式、依赖和模型配置。  
`https://docs.ray.io/en/latest/serve/llm/quick-start.html`

**[R9] Serve model composition**：DeploymentHandle/Response 的组合方式；与 Ray Task/Actor 互操作时，文档将 ObjectRef 转换列为 DeveloperAPI，须在适配层隔离并实测。  
`https://docs.ray.io/en/latest/serve/model_composition.html`

**[R10] ray.cancel**：Task/Actor 区别、force 与 recursive 的边界。  
`https://docs.ray.io/en/latest/ray-core/api/doc/ray.cancel.html`

**[R11] Monitoring with CLI/SDK**：State API 不保证完整一致的完成资源快照。  
`https://docs.ray.io/en/latest/ray-observability/user-guides/cli-sdk.html`

**[R12] On-premise cluster**：已知机器与 SSH 条件下的官方 cluster launcher。  
`https://docs.ray.io/en/latest/cluster/vms/user-guides/launching-clusters/on-premises.html`

**[R13] Environment Dependencies**：runtime_env、working_dir 与部署依赖准备。  
`https://docs.ray.io/en/latest/ray-core/handling-dependencies.html`

**[R14] Serve LLM troubleshooting / compatibility**：实施时核对 Ray 与 vLLM 配对和 CUDA 条件；文档缓存可能滞后，不把旧配置视为实测组合。  
`https://docs.ray.io/en/latest/serve/llm/troubleshooting.html`

---

## 21. 实施原则的最后约束

**Maze 保存逻辑 DAG 和当前部署/请求的必要内存状态；Ray 管执行机制；Serve 管模型服务。Maze 包办启动不等于重新发明调度器，极简也不等于没有背压和清理。**

遇到两种可行实现，优先选择公开稳定 API、职责更少、可用真实测试证明的方案。不要为了“Agent”或“工业级”这两个词，擅自添加用户未要求的平台能力。
