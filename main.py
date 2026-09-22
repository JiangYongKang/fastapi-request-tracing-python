"""演示应用：挂载追踪中间件并提供多种可验证场景的端点。

端点一览（对外接口行为与状态码均为标准 FastAPI 行为）：

* ``GET /``            原始骨架接口，行为不变
* ``GET /work``        正常多阶段请求（fetch + compute）
* ``GET /slow``        超过慢请求阈值的请求，主导阶段为 db
* ``GET /error``       抛出未处理异常，返回 500
* ``GET /timeout``     模拟 asyncio 超时（TimeoutError），返回 500
* ``GET /spawn``       派生/延迟异步任务（create_task + 后台任务），
                       这些任务继承同一条 trace_id 并追加阶段
* ``GET /trace-id``    返回当前链路标识，供测试断言
* ``GET /metrics``     返回本地聚合器快照（确定性、可复现）
"""

from __future__ import annotations

import asyncio

from fastapi import BackgroundTasks, FastAPI

from rtrace import (
    Sampler,
    TraceAggregator,
    TracingConfig,
    TracingMiddleware,
    configure_logging,
    current_trace,
    timed_operation,
)

configure_logging()

app = FastAPI(title="fastapi-request-tracing-demo")

config = TracingConfig.from_env()
aggregator = TraceAggregator()
app.add_middleware(
    TracingMiddleware,
    config=config,
    exporter=aggregator.record,
    sampler=Sampler(rate=config.sample_rate, error_rate=config.sample_error_rate),
)


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/work")
async def do_work():
    """正常多阶段请求，用于阶段耗时与主导阶段识别。"""

    async with timed_operation("fetch"):
        await asyncio.sleep(0.02)
    async with timed_operation("compute"):
        await asyncio.sleep(0.01)
    return {"ok": True}


@app.get("/slow")
async def slow_request():
    """慢请求：db 阶段占主导。"""

    async with timed_operation("auth"):
        await asyncio.sleep(0.01)
    async with timed_operation("db"):
        await asyncio.sleep(0.3)
    return {"ok": True}


@app.get("/error")
async def failing_request():
    """未处理业务异常，FastAPI 默认返回 500。"""

    async with timed_operation("risky"):
        raise RuntimeError("boom: simulated unhandled error")


@app.get("/timeout")
async def timeout_request():
    """模拟下游超时：TimeoutError 未处理，FastAPI 默认返回 500。"""

    async with timed_operation("downstream"):
        raise TimeoutError("downstream did not respond in time")


async def _background_job(delay: float) -> None:
    # 延迟执行的后台任务：必须继承请求的同一条链路
    async with timed_operation("background"):
        await asyncio.sleep(delay)


@app.get("/spawn")
async def spawn_tasks(background: BackgroundTasks, wait_ms: int = 20):
    """派生并发子任务 + 延迟后台任务，均继承同一条 trace_id。"""

    trace = current_trace()
    trace_id = trace.trace_id if trace else None
    seen: list[str | None] = []

    async def child(index: int) -> None:
        async with timed_operation(f"child_{index}"):
            child_trace = current_trace()
            seen.append(child_trace.trace_id if child_trace else None)
            await asyncio.sleep(wait_ms / 1000.0)

    await asyncio.gather(child(1), child(2))
    background.add_task(_background_job, wait_ms / 1000.0)
    return {"trace_id": trace_id, "children_seen": seen}


@app.get("/trace-id")
async def get_trace_id():
    trace = current_trace()
    return {"trace_id": trace.trace_id if trace else None}


@app.get("/metrics")
async def metrics():
    """本地聚合快照（仅包含被采样的链路）。"""

    return aggregator.snapshot()
