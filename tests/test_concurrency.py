"""Concurrency: no cross-talk between requests; cancellation is distinct."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from main import app
from tracing import aggregate
from tracing.context import TraceStatus


async def test_concurrent_requests_do_not_cross_talk():
    n = 20
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        responses = await asyncio.gather(
            *(client.get("/work", params={"ms": 20 + i}) for i in range(n))
        )

    assert all(r.status_code == 200 for r in responses)
    header_ids = [r.headers["x-trace-id"] for r in responses]
    assert len(set(header_ids)) == n, "every request needs a distinct trace id"

    for resp in responses:
        body = resp.json()
        # body, header and spawned child task all agree on one trace id
        assert body["trace_id"] == resp.headers["x-trace-id"]
        assert body["child_trace_id"] == body["trace_id"]

    traces = {t.trace_id: t for t in aggregate.all_traces()}
    assert set(traces) == set(header_ids)
    for trace_id in header_ids:
        trace = traces[trace_id]
        # each trace holds exactly its own stages: no leakage between requests
        assert sorted(s.name for s in trace.stages) == ["child", "compute", "db"]
        assert trace.status is TraceStatus.OK
        assert trace.finished


async def test_concurrent_mixed_outcomes_stay_distinguishable():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        results = await asyncio.gather(
            client.get("/"),
            client.get("/error"),
            client.get("/timeout"),
            client.get("/slow", params={"ms": 300}),
            client.get("/work", params={"ms": 30}),
        )
    statuses = [r.status_code for r in results]
    assert statuses == [200, 500, 504, 200, 200]

    by_id = {t.trace_id: t for t in aggregate.all_traces()}
    assert len(by_id) == 5
    trace_by_resp = [by_id[r.headers["x-trace-id"]] for r in results]
    assert trace_by_resp[0].status is TraceStatus.OK
    assert trace_by_resp[1].status is TraceStatus.ERROR
    assert trace_by_resp[2].status is TraceStatus.TIMEOUT
    assert trace_by_resp[3].is_slow and trace_by_resp[3].status is TraceStatus.OK
    assert not trace_by_resp[4].is_slow
    assert all(t.finished for t in trace_by_resp)


async def test_client_cancellation_finalizes_trace_as_cancelled():
    """Cancelling the ASGI task (client disconnect) ends the trace cleanly."""
    messages: list[dict] = []
    request_sent = False

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/cancel",
        "raw_path": b"/cancel",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(30)
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.sleep(0.1)  # let the request reach the endpoint
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    traces = aggregate.all_traces()
    assert len(traces) == 1
    trace = traces[0]
    assert trace.status is TraceStatus.CANCELLED
    assert trace.finished, "cancelled trace must still be finalized"


async def test_no_unfinished_traces_after_mixed_load():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        await asyncio.gather(
            client.get("/"),
            client.get("/error"),
            client.get("/timeout"),
            client.get("/work", params={"ms": 10}),
        )
    traces = aggregate.all_traces()
    assert len(traces) == 4
    assert all(t.finished for t in traces), "no trace may be left unclosed"
