"""追踪组件自身故障时，不得改变业务请求的结果或状态码。"""

from __future__ import annotations

import asyncio

import pytest

from rtrace.aggregator import TraceAggregator
from rtrace.config import TracingConfig
from rtrace.context import current_trace
from rtrace.middleware import TracingMiddleware
from rtrace.trace import TraceStatus


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"hello"})


def _scope(path="/x"):
    return {"type": "http", "method": "GET", "path": path, "root_path": ""}


async def _receive():
    return {"type": "http.request"}


@pytest.mark.asyncio
async def test_broken_exporter_does_not_affect_response():
    def broken_exporter(trace):
        raise RuntimeError("collector down")

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    mw = TracingMiddleware(_ok_app, config=TracingConfig(), exporter=broken_exporter)
    # 不抛出 -> 业务响应保持 200
    await mw(_scope(), _receive, send)
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"hello"
    assert current_trace() is None  # 上下文已清理


@pytest.mark.asyncio
async def test_broken_exporter_does_not_mask_app_error():
    async def broken_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 503, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    def broken_exporter(trace):
        raise RuntimeError("collector down")

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    mw = TracingMiddleware(broken_app, config=TracingConfig(), exporter=broken_exporter)
    await mw(_scope(), _receive, send)
    assert sent[0]["status"] == 503  # 业务状态码原样保留


class _ExplodingSampler:
    def should_sample(self, trace_id, is_error=False):
        raise RuntimeError("sampler exploded")


@pytest.mark.asyncio
async def test_broken_sampler_does_not_affect_response():
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    mw = TracingMiddleware(
        _ok_app,
        config=TracingConfig(),
        exporter=lambda t: None,
        sampler=_ExplodingSampler(),
    )
    await mw(_scope(), _receive, send)
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_disabled_config_passes_through():
    aggregator = TraceAggregator()
    mw = TracingMiddleware(
        _ok_app,
        config=TracingConfig(enabled=False),
        exporter=aggregator.record,
    )
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await mw(_scope(), _receive, send)
    assert sent[0]["status"] == 200
    assert aggregator.snapshot()["total"] == 0  # 关闭后不追踪
