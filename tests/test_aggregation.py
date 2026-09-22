"""聚合结果在同一规则下重复运行可复现。"""

from __future__ import annotations

from rtrace.aggregator import TraceAggregator
from rtrace.config import TracingConfig
from rtrace.sampling import Sampler
from rtrace.trace import Trace, TraceStatus


def _make_trace(tid: str, status: TraceStatus, dominant: str, ms: int, code: int):
    cfg = TracingConfig(slow_threshold_ms=100.0)
    trace = Trace(trace_id=tid, config=cfg, start_perf=0.0, method="GET", path="/p")
    span = trace.start_span(dominant)
    span.start_perf = 0.0
    span.end_perf = ms / 1000.0
    if status is not TraceStatus.OK:
        trace.status = status
    trace.finish(code, end_perf=ms / 1000.0)
    return trace


def _build():
    """按相同输入构建一次"采样 + 聚合"管线。"""

    sampler = Sampler(rate=0.5, error_rate=1.0)
    aggregator = TraceAggregator()
    rows = [
        ("01", TraceStatus.OK, "cache", 10, 200),
        ("02", TraceStatus.SLOW, "db", 250, 200),
        ("03", TraceStatus.ERROR, "db", 30, 500),
        ("04", TraceStatus.TIMEOUT, "downstream", 300, 500),
        ("05", TraceStatus.OK, "cache", 5, 200),
        ("06", TraceStatus.CANCELLED, "client", 12, None),
        ("07", TraceStatus.SLOW, "db", 300, 200),
        ("08", TraceStatus.OK, "cache", 2, 200),
    ]
    for tid, status, dom, ms, code in rows:
        trace = _make_trace(tid * 16, status, dom, ms, code)
        if sampler.should_sample(trace.trace_id, is_error=status.is_failure):
            aggregator.record(trace)
    return aggregator.snapshot()


def test_pipeline_snapshot_is_reproducible():
    first = _build()
    for _ in range(5):
        assert _build() == first

    # 期望值用同一个确定性采样器推导，而不是依赖固定 ID 的运气
    sampler = Sampler(rate=0.5, error_rate=1.0)
    rows = [
        ("01", False), ("02", False), ("03", True), ("04", True),
        ("05", False), ("06", True), ("07", False), ("08", False),
    ]
    expected_total = sum(
        sampler.should_sample(tid * 16, is_error=is_err) for tid, is_err in rows
    )
    assert first["total"] == expected_total
    assert first["failure_count"] == 3  # 失败请求 error_rate=1 全采
    expected_slow = sum(
        sampler.should_sample(tid * 16)
        for tid in ("02", "07")
    )
    assert first["slow_count"] == expected_slow
    # 明细按 trace_id 排序
    ids = [d["trace_id"] for d in first["details"]]
    assert ids == sorted(ids)
    # 每种失败状态都在快照中可区分（失败全采，必然存在）
    statuses = {d["status"] for d in first["details"]}
    assert {"error", "timeout", "cancelled"} <= statuses
    if expected_slow:
        assert "slow" in statuses
