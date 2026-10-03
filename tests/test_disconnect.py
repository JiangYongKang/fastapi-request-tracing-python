"""Client-disconnect handling and external-behavior compatibility.

The disconnect tests drive the app at the raw ASGI level so a genuine
mid-request ``http.disconnect`` can be delivered while the handler is still
running — something TestClient cannot do (it only reports disconnects after
the response completes).
"""

from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from main import app
from tracing import aggregate, store
from tracing.config import TracingConfig, configure_tracing
from tracing.context import TraceStatus


def _scope(path: str, query: str = "") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


async def _run_with_disconnect(path: str, query: str = "", disconnect_after: float = 0.1):
    """Drive the app; the client disconnects ``disconnect_after`` seconds in."""
    sent: list[dict] = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(disconnect_after)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    started = time.perf_counter()
    await app(_scope(path, query), receive, send)
    elapsed = time.perf_counter() - started
    return sent, elapsed


async def test_real_disconnect_marks_trace_cancelled_and_closes_it():
    """Handler never checks for disconnects; tracing must still cope."""
    # /slow sleeps 5s without any disconnect check of its own
    sent, elapsed = await _run_with_disconnect("/slow", "ms=5000", disconnect_after=0.1)

    traces = aggregate.all_traces()
    assert len(traces) == 1
    trace = traces[0]
    print(f"\n[disconnect] status={trace.status.value} finished={trace.finished} "
          f"elapsed={elapsed * 1000:.0f}ms")
    assert trace.status is TraceStatus.CANCELLED
    assert trace.finished, "a disconnected trace must still be finalized"
    assert elapsed < 2.0, "the request must be closed promptly, not left hanging"
    assert not sent, "no response can be sent to a disconnected client"

    # the conclusion is queryable afterwards via the store
    record = store.get_record(trace.trace_id)
    assert record is not None
    assert record.status is TraceStatus.CANCELLED


async def test_disconnect_is_distinct_from_ok_timeout_and_error():
    """Same app, four different endings -> four distinguishable conclusions."""
    await _run_with_disconnect("/slow", "ms=5000")          # client goes away
    client = TestClient(app)
    ok = client.get("/")
    err = client.get("/error")
    timeout = client.get("/timeout")

    statuses = [t.status for t in aggregate.all_traces()]
    print(f"\n[endings] {[s.value for s in statuses]}")
    assert statuses[0] is TraceStatus.CANCELLED
    by_id = {
        r.headers["x-trace-id"]: store.get_record(r.headers["x-trace-id"])
        for r in (ok, err, timeout)
    }
    assert by_id[ok.headers["x-trace-id"]].status is TraceStatus.OK
    assert by_id[err.headers["x-trace-id"]].status is TraceStatus.ERROR
    assert by_id[timeout.headers["x-trace-id"]].status is TraceStatus.TIMEOUT
    # cancelled must not collapse into any of the other endings
    assert len({s.value for s in statuses}) == 4


async def test_disconnect_does_not_disturb_concurrent_normal_request():
    """A disconnecting client and a healthy client stay cleanly separated."""
    client = TestClient(app)

    async def normal_request():
        # run the sync TestClient off the event loop to allow true overlap
        return await asyncio.to_thread(client.get, "/work", params={"ms": 50})

    disconnected, normal = await asyncio.gather(
        _run_with_disconnect("/slow", "ms=5000"), normal_request()
    )
    assert normal.status_code == 200
    traces = {t.trace_id: t for t in aggregate.all_traces()}
    assert len(traces) == 2
    normal_trace = traces[normal.headers["x-trace-id"]]
    cancelled = [t for t in traces.values() if t.status is TraceStatus.CANCELLED]
    assert len(cancelled) == 1
    assert normal_trace.status is TraceStatus.OK
    assert normal_trace.trace_id != cancelled[0].trace_id


# -- external behavior must be identical to the untraced baseline -----------

def test_status_codes_and_bodies_unchanged_with_tracing_on():
    client = TestClient(app)
    cases = [
        ("/", 200, {"Hello": "World"}),
        ("/error", 500, {"detail": "Internal Server Error"}),
        ("/timeout", 504, {"detail": "Request timeout"}),
        ("/no-such-route", 404, {"detail": "Not Found"}),
    ]
    for path, expected_status, expected_body in cases:
        resp = client.get(path)
        print(f"\n[compat:on] {path} -> {resp.status_code} {resp.json()}")
        assert resp.status_code == expected_status
        assert resp.json() == expected_body


def test_normal_routes_identical_with_tracing_off(tracing_state):
    client = TestClient(app)
    configure_tracing(TracingConfig(enabled=True))
    on_root = client.get("/")
    on_404 = client.get("/no-such-route")

    configure_tracing(TracingConfig(enabled=False))
    off_root = client.get("/")
    off_404 = client.get("/no-such-route")

    for on, off in ((on_root, off_root), (on_404, off_404)):
        assert on.status_code == off.status_code
        assert on.json() == off.json()
    assert "x-trace-id" not in off_root.headers
    assert store.all_records() == [] or all(
        r.trace_id != off_root.headers.get("x-trace-id") for r in store.all_records()
    )


def test_tracing_does_not_rewrite_business_status_codes():
    """Business-set codes (404, 422) pass through untouched by tracing."""
    client = TestClient(app)
    assert client.get("/no-such-route").status_code == 404
    bad_params = client.get("/work", params={"ms": "not-a-number"})
    assert bad_params.status_code == 422
    trace = aggregate.all_traces()[-1]
    # a 4xx is a normal ending for tracing, not an error
    assert trace.status is TraceStatus.OK
