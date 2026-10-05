"""Post-hoc diagnostic records: lookup by trace id, capacity & retention."""

from __future__ import annotations

import asyncio

import httpx
from fastapi.testclient import TestClient

from main import app
from tracing import records
from tracing.config import TracingConfig, configure_tracing
from tracing.context import TraceStatus, new_trace

client = TestClient(app)


def _finished(trace_id: str, status: TraceStatus, stages: dict[str, float] | None = None,
              slow_threshold_ms: float = 500.0):
    trace = new_trace(trace_id)
    trace.slow_threshold_ms = slow_threshold_ms
    for name, ms in (stages or {}).items():
        trace.add_stage(name, ms)
    trace.finalize(status)
    return trace


def test_record_lookupable_by_response_header_trace_id():
    print("\n[scenario] 按响应头标识回查单条请求的诊断记录")
    resp = client.get("/work", params={"ms": 30})
    assert resp.status_code == 200
    trace_id = resp.headers["x-trace-id"]

    rec = records.lookup(trace_id)
    assert rec is not None, "record must be retrievable by the response trace id"
    print(f"  trace_id={trace_id} status={rec.status} total={rec.total_ms:.1f}ms "
          f"dominant={rec.dominant_stage}")
    assert rec.status == "ok"
    assert rec.total_ms > 0.0
    assert sorted(name for name, _ in rec.stages) == ["child", "compute", "db"]
    # dominant stage is the slowest recorded stage
    expected = max(rec.stages, key=lambda s: s[1])
    assert rec.dominant_stage == expected[0]
    assert rec.dominant_stage_ms == expected[1]


async def test_concurrent_records_do_not_cross_talk():
    print("\n[scenario] 并发请求各自回查各自的记录，互不串")
    n = 15
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        responses = await asyncio.gather(
            *(c.get("/work", params={"ms": 10 + i}) for i in range(n))
        )
    ids = [r.headers["x-trace-id"] for r in responses]
    assert len(set(ids)) == n
    for trace_id in ids:
        rec = records.lookup(trace_id)
        assert rec is not None
        assert rec.trace_id == trace_id
        # each record carries exactly its own stages
        assert sorted(name for name, _ in rec.stages) == ["child", "compute", "db"]
        assert rec.status == "ok"


def test_capacity_evicts_normal_records_before_interesting_ones():
    print("\n[scenario] 容量打满：普通记录先被淘汰，失败/慢/中断优先留下")
    cap = 4
    for i in range(cap):
        records.record(_finished(f"normal-{i}", TraceStatus.OK, {"db": 1.0}), capacity=cap)
    assert records.size() == cap

    # a failed request must be retained, evicting the oldest normal record
    records.record(_finished("err-1", TraceStatus.ERROR), capacity=cap)
    assert records.lookup("err-1") is not None
    assert records.lookup("normal-0") is None
    assert records.lookup("normal-3") is not None

    # a cancelled request is interesting too
    records.record(_finished("cancelled-1", TraceStatus.CANCELLED), capacity=cap)
    assert records.lookup("cancelled-1") is not None
    assert records.lookup("normal-1") is None

    # a slow (but ok) request is interesting as well
    records.record(
        _finished("slow-1", TraceStatus.OK, {"io": 900.0}, slow_threshold_ms=0.0),
        capacity=cap,
    )
    assert records.lookup("slow-1") is not None
    assert records.lookup("normal-2") is None

    # one normal record (normal-3) is still present; a new normal record
    # evicts it (normal records always lose to normal records first)
    records.record(_finished("normal-late", TraceStatus.OK), capacity=cap)
    assert records.lookup("normal-late") is not None
    assert records.lookup("normal-3") is None

    # push the last normal record out with an interesting one: the store is
    # now entirely interesting, so a further normal record is dropped
    records.record(_finished("err-2", TraceStatus.ERROR), capacity=cap)
    assert records.lookup("normal-late") is None
    assert records.lookup("err-2") is not None
    records.record(_finished("normal-dropped", TraceStatus.OK), capacity=cap)
    assert records.lookup("normal-dropped") is None
    assert records.size() == cap
    kept = {r.trace_id for r in records.all_records()}
    print(f"  kept={sorted(kept)}")
    assert kept == {"err-1", "cancelled-1", "slow-1", "err-2"}

    # a new interesting record evicts the oldest interesting one
    records.record(_finished("err-3", TraceStatus.ERROR), capacity=cap)
    assert records.lookup("err-3") is not None
    assert records.lookup("err-1") is None
    assert records.size() == cap


def test_normal_flood_cannot_flush_out_slow_or_failed_records(tracing_state):
    print("\n[scenario] 大量正常请求冲刷后，慢请求与失败请求的记录仍可回查")
    configure_tracing(TracingConfig(enabled=True, slow_threshold_ms=200.0,
                                    sample_rate=1.0, records_capacity=8,
                                    emit_logs=False))
    slow_id = client.get("/slow", params={"ms": 300}).headers["x-trace-id"]
    err_id = client.get("/error").headers["x-trace-id"]
    for _ in range(30):
        client.get("/")
    assert records.size() <= 8
    slow_rec = records.lookup(slow_id)
    err_rec = records.lookup(err_id)
    assert slow_rec is not None and slow_rec.slow and slow_rec.status == "ok"
    assert err_rec is not None and err_rec.status == "error"
    print(f"  slow kept: {slow_id} ({slow_rec.total_ms:.0f}ms), "
          f"error kept: {err_id}")


def test_capacity_zero_disables_recording(tracing_state):
    print("\n[scenario] 容量为 0 时完全不留记录，请求本身不受影响")
    configure_tracing(TracingConfig(enabled=True, records_capacity=0, emit_logs=False))
    resp = client.get("/")
    assert resp.status_code == 200
    assert records.lookup(resp.headers["x-trace-id"]) is None
    assert records.size() == 0
