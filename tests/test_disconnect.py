"""Client disconnect: the tracing layer itself must close the trace.

Covers:
* a real ``http.disconnect`` mid-request is finalised as ``cancelled`` by the
  middleware alone — even when the endpoint never checks for disconnects;
* endpoints with their own disconnect check keep working (same conclusion);
* ``cancelled`` stays clearly distinct from ok / error / timeout;
* the outward contract (status code + body) is identical with tracing on/off.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from main import app
from tracing import aggregate, records
from tracing.config import TracingConfig, configure_tracing
from tracing.context import TraceStatus

client = TestClient(app)


def _scope(path: str, trace_id: str | None = None) -> dict:
    headers = [(b"x-trace-id", trace_id.encode("latin-1"))] if trace_id else []
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


def _receive_disconnecting_after(delay: float):
    """receive() that delivers the request, then a client disconnect."""
    state = {"sent_request": False}

    async def receive() -> dict:
        if not state["sent_request"]:
            state["sent_request"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(delay)
        return {"type": "http.disconnect"}

    return receive


async def test_disconnect_without_endpoint_check_finalizes_as_cancelled():
    """Endpoint /hang never looks at the receive channel; the middleware
    alone must spot the disconnect, stop the work and close the trace."""
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    scope = _scope("/hang", trace_id="disconnect-no-check")
    task = asyncio.create_task(
        app(scope, _receive_disconnecting_after(0.05), send)
    )
    # must finish promptly: the endpoint itself would sleep 30s
    await asyncio.wait_for(task, timeout=5.0)

    assert not any(
        m.get("type") == "http.response.start" for m in sent
    ), "a gone client must not receive a synthesized response"

    traces = aggregate.all_traces()
    assert len(traces) == 1
    trace = traces[0]
    assert trace.status is TraceStatus.CANCELLED
    assert trace.finished, "interrupted trace must be finalized, not left hanging"
    assert trace.total_ms < 5_000.0, "trace closed at disconnect, not at endpoint end"

    record = records.lookup("disconnect-no-check")
    assert record is not None, "interrupted request still leaves a lookup record"
    assert record.status is TraceStatus.CANCELLED
    print(f"\n[disconnect] /hang -> status={record.status.value} "
          f"total={record.total_ms:.1f}ms")


async def test_disconnect_with_endpoint_check_still_cancelled():
    """Endpoint /cancel raises CancelledError itself on disconnect; the
    conclusion must be the same single 'cancelled' ending."""
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    scope = _scope("/cancel", trace_id="disconnect-with-check")
    task = asyncio.create_task(
        app(scope, _receive_disconnecting_after(0.05), send)
    )
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except asyncio.CancelledError:
        pass  # endpoint-surfaced cancellation may propagate; both are fine

    traces = aggregate.all_traces()
    assert len(traces) == 1
    assert traces[0].status is TraceStatus.CANCELLED
    assert traces[0].finished
    record = records.lookup("disconnect-with-check")
    assert record is not None and record.status is TraceStatus.CANCELLED


async def test_all_four_endings_stay_distinct():
    """ok / error / timeout / cancelled are four separate conclusions."""
    client.get("/")
    client.get("/error")
    client.get("/timeout")

    async def send(message: dict) -> None:
        return None

    task = asyncio.create_task(
        app(_scope("/hang"), _receive_disconnecting_after(0.05), send)
    )
    await asyncio.wait_for(task, timeout=5.0)

    statuses = {t.status for t in aggregate.all_traces()}
    assert statuses == {
        TraceStatus.OK,
        TraceStatus.ERROR,
        TraceStatus.TIMEOUT,
        TraceStatus.CANCELLED,
    }
    by_id = {t.trace_id: t.status for t in aggregate.all_traces()}
    print(f"\n[endings] {sorted(s.value for s in by_id.values())}")


def test_outward_contract_identical_with_tracing_on_and_off(tracing_state):
    """App-produced responses are byte-identical with tracing on or off."""
    # raise_server_exceptions=False: compare exactly what the *client* sees
    probe = TestClient(app, raise_server_exceptions=False)

    configure_tracing(TracingConfig(enabled=True, slow_threshold_ms=200.0))
    on_root = probe.get("/")
    on_404 = probe.get("/no-such-route")
    on_error = probe.get("/error")
    on_timeout = probe.get("/timeout")
    assert "x-trace-id" in on_root.headers

    configure_tracing(TracingConfig(enabled=False))
    off_root = probe.get("/")
    off_404 = probe.get("/no-such-route")
    off_error = probe.get("/error")
    assert "x-trace-id" not in off_root.headers

    # business responses: status AND body untouched by tracing
    assert (on_root.status_code, on_root.json()) == (200, {"Hello": "World"})
    assert on_root.status_code == off_root.status_code
    assert on_root.content == off_root.content
    assert on_404.status_code == off_404.status_code == 404
    assert on_404.content == off_404.content
    # business failure keeps its own status code: tracing only observes
    assert on_error.status_code == off_error.status_code == 500
    # timeout boundary keeps its documented code (existing contract)
    assert on_timeout.status_code == 504
    print("\n[compat] root/404 bodies identical on/off; "
          "error=500 timeout=504 preserved")


def test_business_failure_status_is_not_rewritten(tracing_state):
    """A failing endpoint keeps its own status code; tracing only observes."""
    resp = client.get("/error")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal Server Error"}
    record = records.lookup(resp.headers["x-trace-id"])
    assert record is not None and record.status is TraceStatus.ERROR
