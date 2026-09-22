"""Middleware integration: status distinction, header contract, isolation."""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from main import app
from tracing import aggregate
from tracing.config import TracingConfig, configure_tracing
from tracing.context import TraceStatus

client = TestClient(app)


def _only_trace():
    traces = aggregate.all_traces()
    assert len(traces) == 1
    return traces[0]


def test_root_endpoint_unchanged():
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"Hello": "World"}
    trace_id = resp.headers.get("x-trace-id")
    assert trace_id, "response must carry the trace id"
    trace = _only_trace()
    assert trace.trace_id == trace_id
    assert trace.status is TraceStatus.OK
    assert trace.finished, "no unfinished trace may be left behind"


def test_inbound_trace_id_is_honored_and_echoed():
    resp = client.get("/", headers={"X-Trace-Id": "client-supplied-id"})
    assert resp.status_code == 200
    assert resp.headers["x-trace-id"] == "client-supplied-id"
    assert _only_trace().trace_id == "client-supplied-id"


def test_unhandled_exception_is_error_and_keeps_500():
    resp = client.get("/error")
    assert resp.status_code == 500
    trace = _only_trace()
    assert trace.status is TraceStatus.ERROR
    assert trace.finished
    assert resp.headers["x-trace-id"] == trace.trace_id


def test_timeout_is_distinct_from_error():
    resp = client.get("/timeout")
    assert resp.status_code == 504
    trace = _only_trace()
    assert trace.status is TraceStatus.TIMEOUT
    assert trace.finished


def test_slow_request_flagged_with_dominant_stage(tracing_state):
    assert tracing_state.slow_threshold_ms == 200.0
    resp = client.get("/slow", params={"ms": 300})
    assert resp.status_code == 200
    trace = _only_trace()
    assert trace.is_slow
    assert trace.total_ms >= 200.0
    dominant = trace.dominant_stage
    assert dominant is not None and dominant.name == "heavy-io"


def test_fast_request_is_not_slow():
    resp = client.get("/slow", params={"ms": 10})
    assert resp.status_code == 200
    assert not _only_trace().is_slow


def test_4xx_is_not_an_error_trace():
    resp = client.get("/no-such-route")
    assert resp.status_code == 404
    assert _only_trace().status is TraceStatus.OK


def test_disabled_tracing_passes_through(tracing_state):
    configure_tracing(TracingConfig(enabled=False))
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"Hello": "World"}
    assert "x-trace-id" not in resp.headers
    assert aggregate.all_traces() == []


def test_tracing_failure_does_not_change_business_result(monkeypatch):
    """If the tracing machinery itself blows up, the request is unaffected."""
    import tracing.middleware as mw

    def broken_new_trace(*args, **kwargs):
        raise RuntimeError("tracing subsystem down")

    monkeypatch.setattr(mw, "new_trace", broken_new_trace)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"Hello": "World"}


def test_trace_log_line_contains_id_and_stage_evidence(caplog):
    with caplog.at_level(logging.INFO, logger="tracing"):
        resp = client.get("/work", params={"ms": 30})
    assert resp.status_code == 200
    trace = _only_trace()
    lines = [r.getMessage() for r in caplog.records if trace.trace_id in r.getMessage()]
    assert lines, "a log line must carry the trace id"
    line = lines[0]
    assert f"trace_id={trace.trace_id}" in line
    assert "db=" in line and "compute=" in line  # per-stage timing evidence
    assert "threshold=" in line and "slow=" in line
