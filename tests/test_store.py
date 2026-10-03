"""Diagnostic record store: lookup by trace id, capacity, retention policy."""

from __future__ import annotations

import asyncio

import httpx
from fastapi.testclient import TestClient

from main import app
from tracing import store
from tracing.config import TracingConfig, configure_tracing
from tracing.context import TraceStatus

client = TestClient(app)


def test_record_lookupable_by_response_header_trace_id():
    resp = client.get("/work", params={"ms": 30})
    assert resp.status_code == 200
    trace_id = resp.headers["x-trace-id"]

    record = store.get_record(trace_id)
    assert record is not None, "the trace id from the header must be queryable"
    print(f"\n[lookup] trace_id={trace_id} status={record.status.value} "
          f"total={record.total_ms:.1f}ms dominant={record.dominant_stage} "
          f"stages={[(s.name, round(s.elapsed_ms, 1)) for s in record.stages]}")
    assert record.trace_id == trace_id
    assert record.status is TraceStatus.OK
    assert record.total_ms > 0.0
    assert {s.name for s in record.stages} == {"db", "child", "compute"}
    assert record.dominant_stage in {"db", "compute"}


def test_record_captures_slow_flag_and_dominant_stage():
    resp = client.get("/slow", params={"ms": 300})
    record = store.get_record(resp.headers["x-trace-id"])
    print(f"\n[slow-record] status={record.status.value} slow={record.is_slow} "
          f"dominant={record.dominant_stage} total={record.total_ms:.1f}ms")
    assert record.is_slow
    assert record.dominant_stage == "heavy-io"


def test_record_captures_error_and_timeout_endings():
    err = client.get("/error")
    timeout = client.get("/timeout")
    err_rec = store.get_record(err.headers["x-trace-id"])
    to_rec = store.get_record(timeout.headers["x-trace-id"])
    print(f"\n[endings] error={err_rec.status.value} timeout={to_rec.status.value}")
    assert err_rec.status is TraceStatus.ERROR
    assert to_rec.status is TraceStatus.TIMEOUT


def test_unknown_trace_id_returns_none():
    assert store.get_record("no-such-trace-id") is None


def test_diagnostics_endpoint_returns_stored_record():
    resp = client.get("/slow", params={"ms": 250})
    trace_id = resp.headers["x-trace-id"]

    diag = client.get(f"/diagnostics/{trace_id}")
    assert diag.status_code == 200
    body = diag.json()
    print(f"\n[diagnostics] {body}")
    assert body["trace_id"] == trace_id
    assert body["status"] == "ok"
    assert body["is_slow"] is True
    assert body["dominant_stage"] == "heavy-io"
    assert body["total_ms"] >= 200.0

    missing = client.get("/diagnostics/no-such-trace-id")
    assert missing.status_code == 404


def test_capacity_evicts_boring_records_before_interesting_ones(tracing_state):
    configure_tracing(
        TracingConfig(enabled=True, slow_threshold_ms=200.0, record_capacity=4)
    )
    # 1 interesting record (slow), then a flood of boring ones
    slow_resp = client.get("/slow", params={"ms": 300})
    slow_id = slow_resp.headers["x-trace-id"]
    boring_ids = []
    for _ in range(6):
        boring_ids.append(client.get("/").headers["x-trace-id"])

    records = store.all_records()
    print(f"\n[retention] kept={[(r.trace_id[:8], r.status.value, r.is_slow) for r in records]}")
    assert store.capacity() <= 4, "store must never grow past its capacity"
    assert store.get_record(slow_id) is not None, (
        "a slow record must survive a flood of normal requests"
    )
    # the oldest boring records were evicted first
    assert store.get_record(boring_ids[0]) is None
    assert store.get_record(boring_ids[-1]) is not None


def test_error_records_are_retained_over_normal_ones(tracing_state):
    configure_tracing(
        TracingConfig(enabled=True, slow_threshold_ms=200.0, record_capacity=3)
    )
    err_id = client.get("/error").headers["x-trace-id"]
    for _ in range(5):
        client.get("/")
    assert store.get_record(err_id) is not None
    assert store.capacity() <= 3


async def test_concurrent_requests_each_get_their_own_record():
    n = 15
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        responses = await asyncio.gather(
            *(c.get("/work", params={"ms": 10 + i}) for i in range(n))
        )
    ids = [r.headers["x-trace-id"] for r in responses]
    assert len(set(ids)) == n
    for resp, trace_id in zip(responses, ids):
        record = store.get_record(trace_id)
        assert record is not None
        # no cross-talk: each record belongs to exactly its own request
        assert record.trace_id == trace_id
        assert record.status is TraceStatus.OK
        assert {s.name for s in record.stages} == {"db", "child", "compute"}
    print(f"\n[concurrency] {n} distinct records, no cross-talk")
