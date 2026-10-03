# FastAPI 请求追踪与慢请求诊断（本地可运行）

为每个请求建立贯穿整条调用链的追踪信息，按阶段记录耗时、识别慢请求，并对
**未处理异常 / 超时 / 客户端中断**给出可区分、可收尾的链路结论。全部能力只依赖
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
# 事后回查：拿响应头里的 X-Trace-Id 查这条链路的诊断记录
curl -i http://127.0.0.1:8000/diagnostics/<trace-id>
```

运行测试（含并发、异常、取消、采样可复现性，以及新增的按标识回查、容量留存、
真断连识别、对外兼容性用例；测试日志会打印链路标识与各阶段耗时依据）：

```bash
uv run pytest -q -s
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
| `cancelled` | `asyncio.CancelledError`（客户端断开/任务取消），只收尾并重新抛出，不伪造响应 |

中间件为纯 ASGI 实现，异常边界上无论走哪条分支都会在 `finally` 前完成收尾，
**不会遗留未结束的链路**。

## 2.1 事后回查：诊断记录存储（tracing.store）

每条请求收尾后，都会在本地留一份不可变的诊断记录（`TraceRecord`），可拿响应头
里的 `X-Trace-Id` 事后回查，并发请求各查各的、互不串扰：

* 记录内容：最终结论（`ok` / `error` / `timeout` / `cancelled`）、总耗时、
  慢请求标记、各阶段耗时、占主导的阶段名；
* 查询方式：代码里 `tracing.store.get_record(trace_id)`，或 HTTP 接口
  `GET /diagnostics/{trace_id}`（查不到返回 404）；
* 容量上限：`TRACING_RECORD_CAPACITY`（默认 1024），存满后**不会无限增长**；
* 留存规则：淘汰时先丢"没看头"的记录（正常且非慢请求，最旧的先丢）；
  慢请求、失败、超时、中断这类值得排查的记录优先保留，只有当库里全是这类
  记录时才按最旧先出。正常流量再大也挤不掉值得看的记录。

## 2.2 客户端断连识别

中间件独占消费 ASGI `receive` 通道：请求体消息照常转发给应用，`http.disconnect`
则置位断连事件并与应用任务竞速。因此**不管业务接口有没有自己做断连检查**，
只要客户端在响应完成前真断开：

* 应用任务被及时取消，链路以 `cancelled` 正常收尾（恰好收尾一次），
  不会既没结论又一直挂着，也不会被当成正常跑完；
* 该结论与正常结束（`ok`）、超时（`timeout`）、未处理异常（`error`）清楚区分；
* 断连记录同样进入诊断存储，可事后按标识回查。

适用范围：识别的是 ASGI 层面的 `http.disconnect`（uvicorn 在客户端断开时下发）。
响应已正常完成后到达的 disconnect 属于正常收尾，不影响结论。

## 2.3 对外兼容边界

* 追踪开/关、内部判成哪种结局（含超时），对外响应体与状态码与未接追踪时一致；
* 业务自己设置的状态码（含 4xx）原样透传，追踪不会改写；
* 追踪子系统自身任何异常都被吞掉，请求按无追踪方式照常执行。

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

## 4. 开关与配置

均为环境变量，进程启动时读取一次：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TRACING_ENABLED` | `true` | 总开关。关闭后中间件直接透传：不建链路、无 `X-Trace-Id` 头、无日志，接口行为/状态码完全不变。 |
| `TRACING_SLOW_THRESHOLD_MS` | `500` | 慢请求阈值（毫秒）。 |
| `TRACING_SAMPLE_RATE` | `1.0` | 基准采样率，范围 `[0,1]`，越界自动夹取；慢/失败请求不受其影响。 |
| `TRACING_SERVICE_NAME` | `local-service` | 日志中附带的服务名。 |
| `TRACING_HEADER_NAME` | `X-Trace-Id` | 链路标识请求/响应头名称。 |
| `TRACING_EMIT_LOGS` | `true` | 是否输出每条已采样链路的日志行。 |
| `TRACING_RECORD_CAPACITY` | `1024` | 诊断记录存储容量上限；满后按留存规则淘汰（见 2.1）。 |

也可在代码中配置：`configure_tracing(TracingConfig(...))`。

## 5. 接口行为保证与故障隔离

* 原有 `GET /` 的响应体与状态码保持不变（仅新增一个响应头）；
* 追踪代码的所有入口（建链、阶段记录、收尾、登记、日志）均吞掉自身异常；
  即使追踪子系统崩溃，请求也会按无追踪方式照常执行，**不改变业务结果与状态码**
  （见 `tests/test_middleware.py::test_tracing_failure_does_not_change_business_result`）。

日志示例（慢请求，可与响应头中的 `X-Trace-Id` 直接关联）：

```
... WARNING tracing trace_id=36b121ce4108... status=ok total=299.8ms threshold=200.0ms slow=True sampled=True dominant=heavy-io=299.4ms stages=[heavy-io=299.4ms]
```

## 6. 代码结构

```
tracing/
  config.py          # 环境变量配置与全局开关
  context.py         # TraceContext、ContextVar、阶段与终态
  stages.py          # stage() / record_stage()
  tasks.py           # traced_task / traced_background / trace_as_current
  sampling.py        # 确定性采样（trace_id 哈希桶）
  exceptions.py      # 异常 -> 链路状态分类
  middleware.py      # 纯 ASGI 中间件（建链、透传、断连识别、收尾、故障隔离）
  store.py           # 有容量上限的诊断记录存储（按标识回查、留存优先淘汰）
  logging_setup.py   # 含 trace_id 与阶段耗时的日志
  aggregate.py       # 进程内登记与可复现聚合
main.py              # 演示路由：/ /work /slow /error /timeout /cancel
tests/               # 单元/并发/取消/聚合测试
```
