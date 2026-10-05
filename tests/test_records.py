"""Postmortem diagnostic records: lookup by trace id + bounded retention."""

from __future__ import annotations

import asyncio

import httpx
from fastapi.testclient import TestClient

from main import app
from tracing import records
from tracing.config import TracingConfig, configure_tracing
from tracing.context import TraceStatus
from tracing.records import DiagnosticRecord, DiagnosticStore, StageTiming

client = TestClient(app)


def _record(
    trace_id: str,
    status: TraceStatus = TraceStatus.OK,
    total_ms: float = 10.0,
    threshold: float = 200.0,
) -> DiagnosticRecord:
    return DiagnosticRecord(
        trace_id=trace_id,
        service_name="test",
        status=status,
        total_ms=total_ms,
        slow_threshold_ms=threshold,
        is_slow=total_ms >= threshold,
        stages=(StageTiming("db", total_ms),),
        dominant_stage=StageTiming("db", total_ms),
    )


# -- store unit behaviour ---------------------------------------------------


def test_store_lookup_roundtrip_and_miss():
    store = DiagnosticStore(capacity=4)
    store.store(_record("a"))
    found = store.lookup("a")
    assert found is not None and found.trace_id == "a"
    assert store.lookup("does-not-exist") is None


def test_capacity_caps_normal_records_fifo():
    store = DiagnosticStore(capacity=3)
    for i in range(6):
        store.store(_record(f"normal-{i}"))
    assert len(store) == 3
    # oldest normal records evicted first, newest retained
    assert store.lookup("normal-0") is None
    assert store.lookup("normal-2") is None
    assert [r.trace_id for r in store.all()] == ["normal-3", "normal-4", "normal-5"]


def test_interesting_records_evict_normals_first():
    store = DiagnosticStore(capacity=3)
    for i in range(3):
        store.store(_record(f"normal-{i}"))
    assert store.store(_record("slow-1", total_ms=999.0))  # slow -> interesting
    assert store.store(_record("err-1", status=TraceStatus.ERROR))
    assert store.store(_record("cxl-1", status=TraceStatus.CANCELLED))
    # every normal record was evicted before any interesting one
    assert [r.trace_id for r in store.all()] == ["slow-1", "err-1", "cxl-1"]


def test_normal_record_never_evicts_interesting_ones():
    store = DiagnosticStore(capacity=2)
    store.store(_record("err-1", status=TraceStatus.ERROR))
    store.store(_record("slow-1", total_ms=999.0))
    retained = store.store(_record("normal-new"))
    assert retained is False, "boring newcomer must be dropped, not evict interesting"
    assert [r.trace_id for r in store.all()] == ["err-1", "slow-1"]


def test_interesting_record_evicts_oldest_interesting_when_full():
    store = DiagnosticStore(capacity=2)
    store.store(_record("err-1", status=TraceStatus.ERROR))
    store.store(_record("err-2", status=TraceStatus.TIMEOUT))
    assert store.store(_record("err-3", status=TraceStatus.CANCELLED))
    assert [r.trace_id for r in store.all()] == ["err-2", "err-3"]


def test_zero_capacity_retains_nothing():
    store = DiagnosticStore(capacity=0)
    assert store.store(_record("a")) is False
    assert store.lookup("a") is None


# -- HTTP level: lookup by the response-header trace id ---------------------


def test_record_retrievable_by_response_header_id():
    resp = client.get("/work", params={"ms": 50})
    assert resp.status_code == 200
    trace_id = resp.headers["x-trace-id"]
    record = records.lookup(trace_id)
    assert record is not None, "record must be findable by the header trace id"
    print(f"\n[lookup] trace_id={trace_id} -> {record.as_dict()}")
    assert record.trace_id == trace_id
    assert record.status is TraceStatus.OK
    assert record.total_ms > 0.0
    stage_names = {s.name for s in record.stages}
    assert stage_names == {"db", "child", "compute"}
    assert record.dominant_stage is not None
    assert record.dominant_stage.name in stage_names
    # dominant stage is the most expensive recorded stage
    assert record.dominant_stage.elapsed_ms == max(s.elapsed_ms for s in record.stages)


def test_record_shows_final_status_for_each_ending():
    cases = [
        ("/", None, 200, TraceStatus.OK),
        ("/error", None, 500, TraceStatus.ERROR),
        ("/timeout", None, 504, TraceStatus.TIMEOUT),
    ]
    for path, params, expected_code, expected_status in cases:
        resp = client.get(path, params=params)
        assert resp.status_code == expected_code
        record = records.lookup(resp.headers["x-trace-id"])
        assert record is not None
        assert record.status is expected_status
        print(f"[ending] {path} -> http={expected_code} status={record.status.value}")


async def test_concurrent_records_are_isolated_per_trace_id():
    n = 12
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        responses = await asyncio.gather(
            *(c.get("/work", params={"ms": 20 + i}) for i in range(n))
        )
    ids = [r.headers["x-trace-id"] for r in responses]
    assert len(set(ids)) == n
    for resp in responses:
        record = records.lookup(resp.headers["x-trace-id"])
        assert record is not None
        # each record holds exactly its own request's stages: no cross-talk
        assert {s.name for s in record.stages} == {"db", "child", "compute"}
        assert record.status is TraceStatus.OK


def test_capacity_eviction_over_http(tracing_state):
    configure_tracing(
        TracingConfig(enabled=True, slow_threshold_ms=200.0, record_capacity=3)
    )
    normal_ids = [client.get("/").headers["x-trace-id"] for _ in range(3)]
    assert len(records.get_store()) == 3

    error_id = client.get("/error").headers["x-trace-id"]
    slow_id = client.get("/slow", params={"ms": 300}).headers["x-trace-id"]
    timeout_id = client.get("/timeout").headers["x-trace-id"]

    store = records.get_store()
    assert len(store) == 3, "store stays within capacity"
    kept = {r.trace_id for r in store.all()}
    # the interesting endings displaced every plain fast-ok record
    assert kept == {error_id, slow_id, timeout_id}
    for nid in normal_ids:
        assert records.lookup(nid) is None

    # a further boring request must not displace the interesting records
    boring_id = client.get("/").headers["x-trace-id"]
    assert records.lookup(boring_id) is None
    assert {r.trace_id for r in store.all()} == {error_id, slow_id, timeout_id}
    print(f"[retention] capacity=3 kept={sorted(r.status.value for r in store.all())}")
