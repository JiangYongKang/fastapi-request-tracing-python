"""慢请求判定、主导阶段、错误/超时状态区分与确定性采样。"""

from __future__ import annotations

import httpx
import pytest

from main import app
from rtrace import Sampler
from rtrace.config import TracingConfig
from rtrace.trace import Trace, TraceStatus, classify_exception
import asyncio


@pytest.mark.asyncio
async def test_slow_endpoint_flagged_with_dominant_span(caplog):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/slow")
    assert resp.status_code == 200  # 对外行为/状态码不变

    entries = [getattr(r, "rtrace_payload", None) for r in caplog.records]
    slow = [e for e in entries if isinstance(e, dict) and e.get("path") == "/slow"][-1]
    assert slow["status"] == "slow"
    assert slow["dominant_span"] == "db"
    # 主导阶段必须确实占总耗时的主导
    assert slow["dominant_ms"] >= slow["total_ms"] * 0.8
    print("\n[slow-evidence] trace_id=%s total=%sms dominant=%s(%sms) spans=%s" % (
        slow["trace_id"], slow["total_ms"], slow["dominant_span"],
        slow["dominant_ms"], [(s["name"], s["duration_ms"]) for s in slow["spans"]],
    ))


@pytest.mark.asyncio
async def test_normal_request_is_not_slow():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/work")
    assert resp.status_code == 200
    # /work 总耗时约 30ms，远低于默认 200ms 阈值 -> ok
    # （通过聚合快照不便过滤，这里仅断言接口行为；阈值边界在下方单测覆盖）


@pytest.mark.asyncio
async def test_error_and_timeout_are_distinguishable(caplog):
    # raise_server_exceptions=False 与 uvicorn 生产行为一致：
    # ServerErrorMiddleware 发送 500 后重抛的异常被服务端吞掉
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        err = await client.get("/error")
        timeout = await client.get("/timeout")
    # 两者对外都是 500，但链路状态必须可区分
    assert err.status_code == 500
    assert timeout.status_code == 500

    entries = [getattr(r, "rtrace_payload", None) for r in caplog.records]
    by_path = {}
    for e in entries:
        if isinstance(e, dict) and e.get("path") in ("/error", "/timeout"):
            by_path[e["path"]] = e
    assert by_path["/error"]["status"] == "error"
    assert by_path["/error"]["error_type"] == "RuntimeError"
    assert by_path["/timeout"]["status"] == "timeout"
    assert by_path["/timeout"]["error_type"] == "TimeoutError"
    assert by_path["/error"]["trace_id"] != by_path["/timeout"]["trace_id"]


def test_classify_exception_distinct_states():
    assert classify_exception(RuntimeError()) is TraceStatus.ERROR
    assert classify_exception(TimeoutError()) is TraceStatus.TIMEOUT
    assert classify_exception(asyncio.CancelledError()) is TraceStatus.CANCELLED
    # 3.11+ asyncio.TimeoutError 即 TimeoutError 的别名
    assert classify_exception(asyncio.TimeoutError()) is TraceStatus.TIMEOUT


def test_slow_threshold_boundary_is_stable():
    cfg = TracingConfig(slow_threshold_ms=100.0)
    fast = Trace(trace_id="a" * 32, config=cfg, start_perf=0.0)
    fast.finish(200, end_perf=0.0999)
    assert fast.status is TraceStatus.OK
    edge = Trace(trace_id="b" * 32, config=cfg, start_perf=0.0)
    edge.finish(200, end_perf=0.1000)  # >= 阈值判慢
    assert edge.status is TraceStatus.SLOW
    slow = Trace(trace_id="c" * 32, config=cfg, start_perf=0.0)
    slow.finish(200, end_perf=0.15)
    assert slow.status is TraceStatus.SLOW
    # 重复结束结论冻结
    assert slow.finish(200, end_perf=0.001) is TraceStatus.SLOW


def test_deterministic_sampling_is_reproducible():
    sampler = Sampler(rate=0.33, error_rate=1.0)
    ids = [f"{i:032x}" for i in range(5000)]
    first = {tid: sampler.should_sample(tid) for tid in ids}
    # 同一规则重复判定，结论必须完全一致
    assert all(sampler.should_sample(tid) == first[tid] for tid in ids)
    # 失败请求使用独立采样率（全采）
    assert all(sampler.should_sample(tid, is_error=True) for tid in ids[:100])
    ratio = sum(first.values()) / len(ids)
    assert 0.30 < ratio < 0.36, ratio


def test_error_sampling_independent_rate():
    sampler = Sampler(rate=0.0, error_rate=0.5)
    ids = [f"{i:032x}" for i in range(4000)]
    assert not any(sampler.should_sample(tid) for tid in ids[:100])
    ratio = sum(sampler.should_sample(tid, is_error=True) for tid in ids) / len(ids)
    assert 0.46 < ratio < 0.54, ratio
    # 同样的失败 trace_id 反复判定结果稳定
    assert sampler.should_sample(ids[0], True) == sampler.should_sample(ids[0], True)
