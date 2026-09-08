# 当前 CPU 开发版：接口和运维边界

## 生命周期

一个前台 `maze start` 进程拥有单个 Engine 和新建的本地 Ray 实例。
启动前检查 token、现有 Ray 进程及 API 端口；先绑定 API socket，再启动 Ray。
API 由单个 Uvicorn worker 提供，不支持多 worker 或控制面 HA。
启动失败或正常退出时关闭本次 Ray 实例；没有后台 PID 管理文件。
预检和 API 端口冲突均在尝试 Ray init 前结束，不调用 Ray shutdown。
一旦开始尝试创建 Ray，即使初始化抛异常或 KeyboardInterrupt 也执行 shutdown。
HTTP 应用/Server 构造和运行失败均关闭 Engine，再回收 Ray 与 API socket。
shutdown 自身失败时仍关闭 socket，并以异常退出，不能据此推断 Ray 已全部退出。
这些回滚依赖 Python 清理代码可以运行；尚不保证卡死初始化有界返回或 SIGKILL 后自动恢复。

部署生成新的 ID，验证并复制协议、编译图，进行 worker 代码反序列化探测、
准备专属 Async Actor 后才 ready。探测不证明代码在所有未来节点都可运行。
输入、代码与资源不能在 invoke 中修改；变更需要新建部署。
准备任务归部署所有；准备期间卸载会先取消并等待准备清理，避免提前移除或错误 ready。

卸载先进入 draining，拒绝新调用并唤醒等待请求，在有限时间内等待或取消在途工作。
并发卸载共享同一清理任务；Engine 停止会尝试清理所有部署，再报告失败。
停止 Engine 后不会继续接纳新部署/调用。不能确认清理完成时返回 503/incomplete，
保留部署和容量记录，不伪报卸载完成。重复删除已移除 ID 返回 404。

## 请求与容量

EngineConfig 集中限制活跃调用、等待请求、节点调用总数、每图节点数及 JSON 大小。
取得整张图的节点预算后才提交，避免半图占用额度后阻塞下游。
队列为全引擎 FIFO；可能存在队首阻塞，尚未做公平性/吞吐优化。
Async Actor 并发还受部署级调用额度和 Ray max_pending_calls 限制。

deadline 包含 Engine 接收并验证后的排队和执行时间；不含 HTTP 上传及代理时间。
HTTP 断开、主动 cancel、deadline 和卸载都会取消本次引用清单中的调用。
普通 Task 先协作取消，grace 后可 force；Actor 方法不 force、不为取消单请求杀 Actor。
完成引用终结是当前清理确认依据，不承诺物理副作用回滚或 LLM 后端即时终止。
未终结调用仍计入容量；下一次接纳或等待唤醒时重新检查，记录量受总容量限制。

没有业务历史查询或持久化幂等性。活动 ID 冲突返回 409，完成后复用不保证去重。
默认业务重试为 0，不提供 exactly-once。用户函数自行负责外部副作用的幂等性。
只有显式声明为纯计算的普通 Task 可配置 `retry`，最多重试 3 次：

```yaml
retry:
  pure: true
  max_retries: 2
  retry_exceptions: true
```

重试交给 Ray；Maze 不套第二层重试循环。`retry_exceptions` 默认为 false，
只设置 max_retries 不会自动重试业务异常。Async Actor 不支持此重试配置。
重复取消不会中断已启动的有界清理；SIGTERM 的 HTTP 请求等待也受 shutdown grace 限制。

## HTTP

所有接口（包括 health/openapi）要求 `Authorization: Bearer <token>`。
管理者可部署预先准备的 Python callable，因此 token 只交给可信部署者。
当前没有单独的业务调用 token/RBAC，不把此入口暴露给不可信用户。

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/healthz`、`/readyz` | 存活与接纳状态 |
| POST | `/v1/deployments` | `{dag, deployment?}`，同步准备后返回部署 |
| GET | `/v1/deployments`、`/v1/deployments/{id}` | 仅当前部署摘要 |
| POST | `/v1/deployments/{id}/invoke` | `{input, invocation_id?, timeout_s?, metrics?}` |
| POST | `/v1/invocations/{id}/cancel` | 取消活动调用 |
| DELETE | `/v1/deployments/{id}` | 有界 drain 和卸载 |
| POST | `/v1/stop` | drain 并停止前台 Engine |

输出：`{invocation_id, deployment_id, status, output, metrics?}`。
错误：`{error: {code, message, cleanup, backend_cleanup, invocation_id, deployment_id, node_id, retryable}}`。
无法确定的标识为 null；已注册调用的错误带调用和部署 ID，包装器错误还带节点 ID。
retryable 仅为提示，不代表已重试。422 无效协议/输入，404 不存在，
409 ID 冲突/取消，413 输入过大，429 队列满，503 未就绪/清理未完成，
504 deadline，500 节点或结果错误。默认 HTTP 错误不返回原始输入或 traceback。
HTTP 默认不公开原始 cause；Python/Ray 异常链保留原因供本机排查。

## 指标与类型

`none` 不返回指标，但仍等待各节点小型完成信号。
`basic` 返回入口排队/耗时、节点包装器 elapsed 和显式 NodeResult 数值指标。
`detailed` 当前等于 basic 加 `missing_fields=["ray_events"]`，不是完整详细诊断。
节点 elapsed 含等待，不代表纯 CPU 时间；并行耗时不可直接相加当整体耗时。
没有 State API 请求，因而监控不可用不会阻断基础结果。

Port 支持 any、number、integer、string、boolean、array、object、null；
验证图输入、节点输入/输出和最终输出的已声明顶层类型及边声明兼容。
声明为 number/integer 的端口不接受 bool，数值拒绝 NaN/Infinity。
any 仍用于 Ray 可序列化的任意内部对象，不声称能证明任意 Python 类型。
JSON/YAML 拒绝未知字段、重复 key、YAML alias、非 JSON 常量和过深配置。
嵌套 schema、数值范围、数组长度等完整端口边界约束仍待补齐。

## Attached LLM 服务

DeploymentSpec.services 定义 ServiceSpec，LLM 节点通过 service_id 引用。
目前仅接受 `mode: attached`，managed 配置会拒绝；Maze 不拥有或删除 attached 服务。
模型服务本身可以被多个节点和部署引用；每个 LLM 节点拥有自己的 Async Actor 和连接池。
`max_connections` 限制每个适配器的连接数，不是模型服务的全局并发额度。
Actor 并发还受节点 max_concurrency 及 Engine admission 限制。

LLM 节点必须明确配置 async_actor、零 GPU、messages 输入以及 text/response 输出。
可声明额外输入 max_tokens、temperature；其余生成选项暂不支持。
max_tokens 在 1 到 ServiceSpec.max_output_tokens 之间；未提供时使用部署上限。
temperature 若提供，范围为 0 到 2；不提供时不替用户修改服务默认采样参数。
messages 目前仅支持 system/user/assistant 的字符串 content，不支持多模态、工具和流式输出。

endpoint 是固定 HTTP(S) base URL，例如 `http://worker-reachable-host:8001/v1`。
禁止 URL 内嵌凭据、query 和 fragment；不跟随重定向，不使用代理环境变量。
api_key_env 只记录环境变量名，密钥在 Worker 创建客户端时读取，不放入 DAG 或调用输入。
多机时地址必须从 Worker 可达；示例 loopback 仅用于单机。当前没有跨机密钥分发机制。

准备时从 Worker 检查 `/models` 中的模型 ID，失败则部署不能 ready。
这个探测确认 HTTP 可达和模型声明，不证明后端推理健康或 GPU 容量足够。
HTTPX 与 Ray Actor 业务重试均关闭；总 timeout 包含连接池等待、网络和响应读取，
同时限制响应字节数。卸载先 drain，再有界关闭客户端连接，最后终止自有 Actor。
API stop 和其他 DAG 卸载不会删除 attached endpoint。

LLM usage 放入 `metrics.nodes.<id>.metadata.llm`，原始结构仍保留在 response 输出。
不把 HTTP 首字节当 TTFT，不返回推测的 TPOT；metrics=none 时不返回 metadata。
取消、deadline 或节点失败时，含 LLM 的 DAG 报告 `backend_cleanup: unconfirmed`：
即使 `cleanup: complete`，也仅说明调用侧引用已终结，不能确认模型后端停止。

实现依据：[HTTPX async 客户端生命周期](https://www.python-httpx.org/async/)、
[HTTPX 显式 transport/retries](https://www.python-httpx.org/advanced/transports/)。

## 环境和剩余能力

当前仅 managed-local，CPU 路径在 macOS ARM64 实测。
还没有 managed-onprem、managed Serve LLM、runtime_env 制品配置、配置恢复、
GPU worker 就绪验证或多机 advertised endpoint。不要将这些视为可用功能。
