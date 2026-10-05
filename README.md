# FastAPI 请求追踪与慢请求诊断（本地可运行）

为每个请求建立贯穿整条调用链的追踪信息，按阶段记录耗时、识别慢请求，并对
**未处理异常 / 超时 / 客户端中断**给出可区分、可收尾的链路结论。每个请求结束后
留下一份可按链路标识回查的诊断记录（有容量上限与留存策略）。全部能力只依赖
FastAPI 与标准库，无任何外部服务。

## 1. 快速开始

```bash
uv sync
uv run uvicorn main:app --port 8000
```

```bash
# 普通请求（响应头带回链路标识）
curl -i http://127.0.0.1:8000/
# 指定/关联上游链路标识（原样回显）
curl -H 'X-Trace-Id: my-correlation-id' http://127.0.0.1:8000/
# 分阶段 + 派生异步任务（子任务继承同一条链路）
curl 'http://127.0.0.1:8000/work?ms=100'
# 慢请求
curl 'http://127.0.0.1:8000/slow?ms=600'
# 三种异常结局
curl -i http://127.0.0.1:8000/error      # 500，链路状态 error
curl -i http://127.0.0.1:8000/timeout    # 504，链路状态 timeout
curl --max-time 0.3 http://127.0.0.1:8000/cancel  # 客户端中断，链路状态 cancelled
curl --max-time 0.3 http://127.0.0.1:8000/hang    # 端点不做断连检查，中间件兜底，同样 cancelled
```

运行测试（含并发、异常、取消、断连兜底、回查、容量留存、采样可复现性；
`-v -s` 下测试日志会逐条打印覆盖了哪些场景、回查到的记录内容）：

```bash
uv run pytest -v -s
```

## 2. 追踪模型

| 概念 | 说明 |
| --- | --- |
| TraceContext | 每个 HTTP 请求一个，含 `trace_id`、服务名、阈值、状态、阶段列表、起止时间。`finalize()` 保证**恰好收尾一次**。 |
| 链路标识 | 入站请求头 `X-Trace-Id` 有值则沿用（可与上游/日志关联），否则生成 `uuid4().hex`；响应头原样回显，日志行携带同一标识。 |
| 上下文传播 | 链路存放在 `contextvars.ContextVar` 中。`asyncio.create_task` 自动拷贝当前上下文，因此请求处理过程中**派生或延迟执行的异步任务天然继承同一条链路**；线程池任务通过 `traced_background` 拷贝上下文。并发请求持有各自的 TraceContext 对象，互不串扰；阶段写入有锁保护。 |
| 阶段 | `async with stage("db"):` 或 `record_stage("db", 12.3)`，记录在当前链路上，无链路或追踪关闭时为 no-op。 |
| 主导阶段 | `dominant_stage` = 已记录阶段中耗时最大者，作为慢请求的耗时依据。 |

派生任务用法：

```python
from tracing import stage, traced_task, traced_background, get_current_trace

child = await traced_task(some_coro())          # 子任务继承当前 trace
await traced_background(sync_fn)                # 线程池同步函数同样继承
```

链路四种终态（互斥、可区分）：

| 状态 | 触发条件 |
| --- | --- |
| `ok` | 正常返回（含 4xx；业务上合法的响应） |
| `error` | 未处理异常（`Exception`），返回 500；或响应状态码 ≥ 500 |
| `timeout` | `asyncio.TimeoutError`，返回 504 |
| `cancelled` | 客户端断开 / 任务取消（`asyncio.CancelledError` 或收到 `http.disconnect`），只收尾，不伪造响应 |

中间件为纯 ASGI 实现，异常边界上无论走哪条分支都会在 `finally` 前完成收尾，
**不会遗留未结束的链路**。

### 客户端断连的兜底识别

uvicorn（HTTP/1.1）在客户端断开时**不会**取消正在执行的端点任务，因此只靠
`CancelledError` 是不够的。中间件在 `receive` 通道上架了一个泵任务：把真实
`receive` 的消息转发给应用，同时自己监听 `http.disconnect`：

* 端点**自己做**断连检查（如 `/cancel` 里 `request.is_disconnected()`）：断连消息
  照常转发给应用，端点抛出的 `CancelledError` 被记录为 `cancelled` 并透明上抛，
  行为与没有追踪时一致；
* 端点**不做**断连检查（如 `/hang`）：中间件收到 `http.disconnect` 后主动停掉
  已无法交付的处理任务，把链路按 `cancelled` 收尾后安静返回——既不会没有结论
  一直挂着，也不会被当成正常跑完；
* 只有**应用先正常完成**、断连才到达时，按应用自己的结局判定（正常完成的响应
  不会被追改判成中断）。

无论哪条路径，`cancelled` 都与 `ok` / `error` / `timeout` 清楚区分；客户端已断开，
不会向其伪造任何响应。

## 3. 慢请求与采样规则

### 慢请求阈值

* 环境变量 `TRACING_SLOW_THRESHOLD_MS`（默认 `500` 毫秒）；
* 判定：链路收尾后 `总耗时 >= 阈值` 即为慢请求（阈值本身包含在内）；
* 日志输出总耗时、阈值、`slow=True/False` 以及各阶段耗时与主导阶段。

### 采样（确定性、可复现）

1. 慢请求、失败请求（`error` / `timeout` / `cancelled`）**强制采样**；
2. 其余请求以 `TRACING_SAMPLE_RATE`（默认 `1.0`）为基准率，按
   `sha256(trace_id)` 的前 64 位映射到 `[0,1)` 区间，`bucket < rate` 即采样。
   同一 trace_id 在任何进程、任何一次运行中的结论完全一致，**无随机状态**。

聚合：`tracing.aggregate` 在进程内登记所有已收尾链路，`summarize()` 输出
按状态计数、慢请求数、各阶段平均耗时与主导阶段；同样的输入得到同样的汇总结果。

## 4. 事后回查（诊断记录）

每个请求收尾后，都会在进程内留下一份**不可变诊断记录**
（`tracing.records.DiagnosticRecord`），拿响应头里的 `X-Trace-Id` 即可回查：

```python
from tracing import records

record = records.lookup(resp.headers["x-trace-id"])
record.status           # 最终结论：ok / error / timeout / cancelled
record.total_ms         # 总耗时
record.stages           # 各阶段耗时 (StageTiming: name, elapsed_ms)
record.dominant_stage   # 最占时间的阶段
record.is_slow          # 是否慢请求
record.as_dict()        # 整体导出为 dict
```

* 记录是收尾时刻的快照，按 `trace_id` 索引；并发请求各存各的、各查各的，
  互不串扰（底层有锁，读写均线程安全，且任何失败都被吞掉，不影响请求）。
* **容量上限**：`TRACING_RECORD_CAPACITY`（默认 `256`，`0` 表示不保留）。
  记录不会无限攒。
* **留存策略**（容量打满时）：慢请求与失败/中断（`error` / `timeout` /
  `cancelled`）属于"值得回头看"的记录，优先留下——
  1. 先淘汰最老的普通记录（`ok` 且不慢）；
  2. 若存满的全是值得留的记录，新来的普通记录直接丢弃，**不会**挤掉它们；
  3. 若新记录同样值得留，则淘汰最老的一条（先进先出）。

## 5. 开关与配置

均为环境变量，进程启动时读取一次：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TRACING_ENABLED` | `true` | 总开关。关闭后中间件直接透传：不建链路、无 `X-Trace-Id` 头、无日志，接口行为/状态码完全不变。 |
| `TRACING_SLOW_THRESHOLD_MS` | `500` | 慢请求阈值（毫秒）。 |
| `TRACING_SAMPLE_RATE` | `1.0` | 基准采样率，范围 `[0,1]`，越界自动夹取；慢/失败请求不受其影响。 |
| `TRACING_SERVICE_NAME` | `local-service` | 日志中附带的服务名。 |
| `TRACING_HEADER_NAME` | `X-Trace-Id` | 链路标识请求/响应头名称。 |
| `TRACING_EMIT_LOGS` | `true` | 是否输出每条已采样链路的日志行。 |
| `TRACING_RECORD_CAPACITY` | `256` | 诊断记录容量上限；`0` 表示不保留。 |

也可在代码中配置：`configure_tracing(TracingConfig(...))`。

## 6. 接口行为保证与故障隔离

* 原有 `GET /` 的响应体与状态码保持不变（仅新增一个响应头）；
* **对外兼容边界**：业务自己产生的响应（状态码、响应体）追踪开/关完全一致，
  可逐字节对比（见
  `tests/test_disconnect.py::test_outward_contract_identical_with_tracing_on_and_off`）；
  业务失败的状态码（如 500）只被观察、不被改写。未处理异常与超时这两条
  边界路径由中间件按既定契约合成 `500` / `504` 响应（既有行为，保持不变）；
  客户端断开时不向已断开的连接伪造任何响应；
* 追踪代码的所有入口（建链、阶段记录、收尾、登记、回查存储、日志）均吞掉
  自身异常；即使追踪子系统崩溃，请求也会按无追踪方式照常执行，
  **不改变业务结果与状态码**
  （见 `tests/test_middleware.py::test_tracing_failure_does_not_change_business_result`）。

日志示例（慢请求，可与响应头中的 `X-Trace-Id` 直接关联）：

```
... WARNING tracing trace_id=36b121ce4108... status=ok total=299.8ms threshold=200.0ms slow=True sampled=True dominant=heavy-io=299.4ms stages=[heavy-io=299.4ms]
```

## 7. 代码结构

```
tracing/
  config.py          # 环境变量配置与全局开关
  context.py         # TraceContext、ContextVar、阶段与终态
  stages.py          # stage() / record_stage()
  tasks.py           # traced_task / traced_background / trace_as_current
  sampling.py        # 确定性采样（trace_id 哈希桶）
  exceptions.py      # 异常 -> 链路状态分类
  middleware.py      # 纯 ASGI 中间件（建链、断连监听、收尾、故障隔离）
  records.py         # 诊断记录：按 trace_id 回查 + 容量上限与留存策略
  logging_setup.py   # 含 trace_id 与阶段耗时的日志
  aggregate.py       # 进程内登记与可复现聚合
main.py              # 演示路由：/ /work /slow /error /timeout /cancel /hang
tests/               # 单元/并发/取消/断连/回查/容量/聚合测试
```

## 8. 本地验证

```bash
uv run pytest -v -s     # 全量：并发/异常/取消/采样/聚合 + 断连兜底/回查/容量留存
uv run pytest tests/test_disconnect.py -v -s   # 只看断连与对外兼容
uv run pytest tests/test_records.py -v -s      # 只看回查与留存策略
```

手动验证断连兜底：`uv run uvicorn main:app --port 8000` 后执行
`curl --max-time 0.3 http://127.0.0.1:8000/hang`，curl 因超时主动断开，
服务端日志会出现一条 `status=cancelled` 的链路记录（总耗时约 0.3s 而非
端点的 30s），进程不会留下悬而未完的链路。
