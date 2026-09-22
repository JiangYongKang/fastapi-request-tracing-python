# FastAPI 请求追踪与慢请求诊断（本地、无外部依赖）

为每个请求建立可贯穿整条调用链的追踪信息，按阶段记录耗时、识别慢请求并指出
主导阶段；区分未处理异常 / 超时 / 客户端中断三种失败；采样结论与聚合结果
确定性、可复现；**追踪组件自身故障不改变业务结果与状态码**。

仅依赖 FastAPI/Starlette 与标准库（`contextvars`、`asyncio`、`uuid`、
`zlib`、`logging`），不需要任何外部服务（无 Jaeger/Zipkin/OTLP）。

---

## 1. 快速开始

```bash
uv sync                                   # 安装依赖（含 pytest）
.venv/bin/uvicorn main:app --port 8000    # 启动演示应用
.venv/bin/python -m pytest -q             # 运行全部 21 个测试
```

演示端点：

| 端点 | 说明 | 业务状态码 |
| --- | --- | --- |
| `GET /` | 原始骨架接口，行为保持不变 | 200 |
| `GET /work` | 正常多阶段请求（fetch + compute） | 200 |
| `GET /slow` | 超过阈值的慢请求，主导阶段为 `db` | 200 |
| `GET /error` | 未处理异常（RuntimeError） | 500 |
| `GET /timeout` | 超时（TimeoutError） | 500 |
| `GET /spawn?wait_ms=20` | 派生并发任务 + 延迟后台任务，均继承同一链路 | 200 |
| `GET /trace-id` | 返回当前请求的 trace_id | 200 |
| `GET /metrics` | 本地聚合器快照（JSON，确定性） | 200 |

> `/error` 与 `/timeout` 对外都是 500（FastAPI 默认行为不变），
> 但追踪链路的 `status` 分别为 `error` 与 `timeout`，可明确区分。

---

## 2. 追踪模型

```
Trace（一条请求链路）
├── trace_id   : 128bit 随机 hex（uuid4），整条链路唯一
├── span_id    : 当前处理阶段标识
├── method/path/status_code
├── status     : ok | slow | error | timeout | cancelled
├── spans[]    : 多个命名阶段，单调时钟计时，可标记 error
└── dominant_span : 耗时最长的阶段（并列取最先开始，结论稳定）
```

### 2.1 链路传播（派生 / 延迟任务不串链路）

* 链路保存在 `contextvars.ContextVar` 中。`asyncio.create_task`、
  `asyncio.gather`、`TaskGroup` 创建任务时会自动拷贝当前上下文快照，
  因此请求处理中**派生的并发任务**与 Starlette 的 **BackgroundTasks
  延迟任务**都继承同一个 `trace_id`。
* 不同并发请求运行在不同任务、持有独立的 Context 副本，**互不串扰**。
* 线程池等不自动传播上下文的场景，可用
  `rtrace.inherited_trace(trace)` 显式恢复绑定。
* 中间件在 `finally` 中重置上下文，请求结束后当前任务不残留链路绑定。

业务代码中记录分阶段耗时：

```python
from rtrace import timed_operation, current_trace

async def handler():
    async with timed_operation("db"):   # 自动挂到当前请求的链路上
        await db.query(...)
    trace = current_trace()             # 无活动链路时返回 None，安全空转
```

### 2.2 五种链路状态

| 状态 | 判定依据 |
| --- | --- |
| `ok` | 正常完成且总耗时 < 阈值 |
| `slow` | 正常完成（含 2xx/3xx/4xx）但总耗时 **≥ 阈值** |
| `error` | 未处理异常（或内层异常处理中间件产出 5xx 响应） |
| `timeout` | 捕获到 `TimeoutError` / `asyncio.TimeoutError` |
| `cancelled` | 收到 ASGI `http.disconnect`（响应发出前客户端中断）或任务收到 `CancelledError` |

优先级：`cancelled` / `timeout` 一旦确定即为终局，不会被随后的 5xx
状态码覆盖；`finish()` 幂等，链路恰好结束一次。

> 为什么除了 `CancelledError` 还要监听 `http.disconnect`：
> 生产服务器（uvicorn）在客户端断开时首先通过 `receive` 投递
> `http.disconnect`；而关闭时才取消请求任务，且事件循环随即停止，
> `finally` 不一定来得及执行。以 `http.disconnect` 作为主信号，
> 能在客户端中断的第一时间确定链路结论并完成收尾。

### 2.3 收尾保证

纯 ASGI 中间件（非 BaseHTTPMiddleware）在 `try/except/finally` 中：

1. 捕获状态码（包装 `send`）与断连（包装 `receive`）；
2. 异常时按类型标记 error/timeout/cancelled 并**原样抛出**
   （不吞 `CancelledError`，保证服务器正确取消）；
3. `finally` 中结束链路、判定采样、写日志、导出，最后重置上下文。

---

## 3. 慢请求与主导阶段

* 计时使用 `time.perf_counter()` 单调时钟，不受系统时间回拨影响。
* 阈值默认 **200ms**，`总耗时 >= 阈值` 判为慢请求（边界包含，稳定）。
* `dominant_span` 为耗时最长的阶段；耗时相同时取最先开始的阶段，
  保证重复运行结论一致。
* 日志与 `/metrics` 明细中给出每个阶段的耗时，慢在哪一目了然。

---

## 4. 采样规则（确定性、可复现）

采样不使用随机数或时钟，**同一 trace_id 的结论永远一致**：

1. `bucket = crc32(trace_id) mod 10000`，取值 `[0, 9999]`；
2. 普通请求：`bucket < sample_rate × 10000` 时采样；
3. 失败请求（error/timeout/cancelled）使用独立的
   `sample_error_rate`，可做到"错误全采、正常按比例采"。

因此：

* 同一条链路无论判定多少次，采样结论不变；
* 同一批请求重复跑（trace_id 输入相同），`/metrics` 聚合快照逐字段一致；
* 聚合明细按 `trace_id` 排序、主导阶段分布按 `(计数降序, 名称)` 排序，
  输出顺序也稳定。

聚合快照字段：`total`、`status_counts`、`status_ratio`、`slow_count`、
`failure_count`、`dominant_span_counts`、`details`（慢/失败明细）。

---

## 5. 开关与配置

配置通过环境变量覆盖（前缀 `RTRACE_`），见 `rtrace/config.py`：

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `RTRACE_ENABLED` | `true` | 追踪总开关，`false` 时中间件完全透传、不建链路 |
| `RTRACE_SLOW_THRESHOLD_MS` | `200` | 慢请求阈值（毫秒） |
| `RTRACE_SAMPLE_RATE` | `1.0` | 普通请求采样比例 `[0,1]`（越界自动夹紧） |
| `RTRACE_SAMPLE_ERROR_RATE` | `1.0` | 失败/超时/取消请求采样比例 |
| `RTRACE_RECORD_SPAN_THRESHOLD_MS` | `0` | 短于该值的阶段不进入明细 |

代码中显式配置：

```python
from rtrace import TracingConfig, TracingMiddleware, Sampler, TraceAggregator

aggregator = TraceAggregator()
config = TracingConfig(slow_threshold_ms=150, sample_rate=0.1, sample_error_rate=1.0)
app.add_middleware(
    TracingMiddleware,
    config=config,
    sampler=Sampler(rate=config.sample_rate, error_rate=config.sample_error_rate),
    exporter=aggregator.record,   # 任意可调用对象；抛异常也不影响业务
)
```

`exporter` 接收已结束的 `Trace`（可写入内存聚合器、文件等），
可为同步或异步可调用对象；其内部异常仅记录一条内部日志并被吞掉。

---

## 6. 日志

每个请求结束输出一行 JSON 到 stderr（`rtrace` logger），无论是否采样
都包含 `sampled` 字段，便于本地用 trace_id 关联整条链路：

```json
{"event":"trace_end","sampled":true,"trace_id":"...","status":"slow",
 "status_code":200,"total_ms":311.4,"dominant_span":"db","dominant_ms":300.4,
 "spans":[{"name":"auth","duration_ms":10.6,"error":false},
          {"name":"db","duration_ms":300.4,"error":false}]}
```

失败链路以 ERROR 级别输出，携带 `error_type` 与截断后的 `error_message`。

---

## 7. 本地验证方法

```bash
# 全部单测（含并发、异常、超时、取消、采样可复现、故障隔离、真实断连集成）
.venv/bin/python -m pytest -q

# 手工端到端
.venv/bin/uvicorn main:app --port 8000 &
curl -s localhost:8000/work | jq .
curl -s localhost:8000/slow > /dev/null      # stderr 可见 status=slow, dominant=db
curl -s localhost:8000/error > /dev/null     # status=error
curl -s localhost:8000/timeout > /dev/null   # status=timeout
curl -s localhost:8000/metrics | jq .

# 阈值与采样通过环境变量即时调整
RTRACE_SLOW_THRESHOLD_MS=50 RTRACE_SAMPLE_RATE=0.1 \
  .venv/bin/uvicorn main:app --port 8000
```

### 测试覆盖说明

| 测试文件 | 覆盖点 |
| --- | --- |
| `tests/test_smoke.py` | 对外接口 `/` 行为与状态码不变 |
| `tests/test_propagation.py` | 派生/后台任务继承同一 trace_id；30 并发互不串扰；日志含 trace_id 与各阶段耗时 |
| `tests/test_cancellation.py` | 任务取消：CANCELLED 可区分、CancelledError 原样抛出、链路收尾、上下文清理；ASGI `http.disconnect` |
| `tests/test_client_disconnect_integration.py` | 真实 uvicorn + 原始 TCP 连接，客户端中断后链路为 cancelled |
| `tests/test_scenarios.py` | 慢请求判定与主导阶段、阈值边界幂等、error/timeout 区分、采样确定性 |
| `tests/test_aggregation.py` | 采样+聚合管线重复运行快照逐字段一致 |
| `tests/test_resilience.py` | exporter/sampler 故障不影响响应与状态码；关闭开关完全透传 |
| `tests/test_sampling_http.py` | 环境变量采样率在 HTTP 链路上生效（正常零采、失败全采） |

测试代码结构：

```
rtrace/
├── config.py       # 开关/阈值/采样率（环境变量）
├── context.py      # ContextVar 链路绑定、ID 生成、跨任务传播
├── span.py         # 阶段计时（单调时钟、幂等结束）
├── trace.py        # 链路聚合根：状态机、慢判定、主导阶段、收尾
├── timing.py       # timed_operation 业务侧计时助手
├── sampling.py     # 基于 crc32(trace_id) 的确定性采样器
├── aggregator.py   # 线程安全的本地聚合器与稳定快照
├── logging.py      # 结构化 JSON 日志
└── middleware.py   # 纯 ASGI 中间件：传播、断连感知、收尾、故障隔离
```
