"""客户端中断 / 任务取消场景：

* 链路状态必须判定为 CANCELLED，且与 ERROR / TIMEOUT 可区分；
* CancelledError 必须被原样向上抛出（不能被追踪中间件吞掉）；
* 链路必须完成收尾（finished=True），上下文必须被清理（无残留绑定）；
* exporter 必须能收到这条被取消的链路。
"""

from __future__ import annotations

import asyncio

import pytest

from rtrace.aggregator import TraceAggregator
from rtrace.config import TracingConfig
from rtrace.context import current_trace
from rtrace.middleware import TracingMiddleware
from rtrace.trace import TraceStatus


async def _hanging_app(scope, receive, send):
    # 模拟一个长时间等待、最终被取消的请求
    request = asyncio.Event()
    await request.wait()


@pytest.mark.asyncio
async def test_cancelled_request_finalizes_and_reraises():
    aggregator = TraceAggregator()
    middleware = TracingMiddleware(
        _hanging_app,
        config=TracingConfig(slow_threshold_ms=10_000),
        exporter=aggregator.record,
    )
    scope = {"type": "http", "method": "GET", "path": "/hang", "root_path": ""}

    async def receive():
        return {"type": "http.request"}

    async def send(message):  # pragma: no cover - 不会发送任何响应
        raise AssertionError("cancelled request should not send a response")

    task = asyncio.create_task(middleware(scope, receive, send))
    await asyncio.sleep(0.02)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    records = aggregator.records()
    assert len(records) == 1
    trace = records[0]
    assert trace.status is TraceStatus.CANCELLED
    assert trace.finished is True
    assert trace.error_type == "CancelledError"
    # 中间件返回后当前上下文不得残留链路绑定
    assert current_trace() is None


@pytest.mark.asyncio
async def test_cancelled_span_recorded_and_cleanup_on_same_task():
    """在执行被取消的同一任务内，已开启的阶段应保留在链路上。"""

    captured: dict = {}

    async def app(scope, receive, send):
        from rtrace.timing import timed_operation

        async with timed_operation("long_wait"):
            event = asyncio.Event()
            await event.wait()  # 被取消点

    aggregator = TraceAggregator()
    middleware = TracingMiddleware(
        app, config=TracingConfig(), exporter=aggregator.record
    )
    scope = {"type": "http", "method": "GET", "path": "/hang2", "root_path": ""}

    async def receive():
        return {"type": "http.request"}

    async def send(message):  # pragma: no cover
        captured["sent"] = message

    task = asyncio.create_task(middleware(scope, receive, send))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    trace = aggregator.records()[0]
    assert trace.status is TraceStatus.CANCELLED
    span_names = [s.name for s in trace.spans]
    assert "long_wait" in span_names
    assert trace.spans[0].error is True
    assert "sent" not in captured


@pytest.mark.asyncio
async def test_http_disconnect_message_marks_cancelled():
    """ASGI 层：应用在 receive 到 http.disconnect 后正常结束 -> CANCELLED。"""

    async def app(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return

    aggregator = TraceAggregator()
    middleware = TracingMiddleware(
        app, config=TracingConfig(slow_threshold_ms=10_000),
        exporter=aggregator.record,
    )
    scope = {"type": "http", "method": "GET", "path": "/drop", "root_path": ""}
    events = [
        {"type": "http.request", "body": b"", "more_body": False},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return events.pop(0)

    async def send(message):  # pragma: no cover - 断开后不应有响应
        raise AssertionError("no response after disconnect")

    await middleware(scope, receive, send)
    trace = aggregator.records()[0]
    assert trace.status is TraceStatus.CANCELLED
    assert trace.error_type == "ClientDisconnect"
    assert trace.finished is True
