"""Client disconnect: the tracing layer catches it even when the endpoint
never checks for it, and finalizes the trace as cancelled — distinctly from
ok / error / timeout."""

from __future__ import annotations

import asyncio

from main import app
from tracing import aggregate, records
from tracing.context import TraceStatus


def _scope(target: str) -> dict:
    path, _, query = target.partition("?")
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


async def test_mid_request_disconnect_marks_trace_cancelled_without_business_checks():
    """Endpoint /slow never polls for disconnect; tracing must still cope."""
    print("\n[scenario] 业务不做断连检查：客户端中途断开 -> 链路按中断收尾")
    sent: list[dict] = []
    request_delivered = False

    async def receive():
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(0.05)  # client goes away while the endpoint works
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    # the middleware returns cleanly; it must not hang and must not raise
    await asyncio.wait_for(app(_scope("/slow?ms=5000"), receive, send), timeout=5.0)

    assert not any(m["type"] == "http.response.start" for m in sent), \
        "no response may be fabricated for a gone client"
    traces = aggregate.all_traces()
    assert len(traces) == 1
    trace = traces[0]
    print(f"  status={trace.status} finished={trace.finished}")
    assert trace.status is TraceStatus.CANCELLED
    assert trace.finished, "a disconnected request must still be finalized"

    # ... and the outcome is lookupable afterwards via the records store
    rec = records.lookup(trace.trace_id)
    assert rec is not None and rec.status == "cancelled"


async def test_disconnect_after_complete_response_is_normal():
    """Servers signal disconnect after the final body chunk too: not cancelled."""
    print("\n[scenario] 响应发完后才断开 -> 仍按正常结束，不算中断")
    sent: list[dict] = []
    request_delivered = False
    response_finished = asyncio.Event()

    async def receive():
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await response_finished.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            response_finished.set()

    await asyncio.wait_for(app(_scope("/"), receive, send), timeout=5.0)

    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
    traces = aggregate.all_traces()
    assert len(traces) == 1
    assert traces[0].status is TraceStatus.OK
    assert traces[0].finished


async def test_disconnect_with_business_own_check_still_ends_cancelled():
    """Endpoint /cancel polls is_disconnected itself; either way -> cancelled."""
    print("\n[scenario] 业务自己做断连检查：同样判为中断，互不冲突")
    request_delivered = False

    async def receive():
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(0.05)
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    try:
        await asyncio.wait_for(app(_scope("/cancel"), receive, send), timeout=5.0)
    except asyncio.CancelledError:
        pass  # endpoint may surface its own CancelledError; both paths are ok

    traces = aggregate.all_traces()
    assert len(traces) == 1
    assert traces[0].status is TraceStatus.CANCELLED
    assert traces[0].finished


async def test_cancelled_is_distinct_from_ok_error_and_timeout():
    print("\n[scenario] 中断与正常/异常/超时三种结局清楚区分")
    request_delivered = False

    async def receive():
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(0.05)
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    await asyncio.wait_for(app(_scope("/slow?ms=5000"), receive, send), timeout=5.0)

    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r_ok = await c.get("/")
        r_err = await c.get("/error")
        r_timeout = await c.get("/timeout")

    by_id = {t.trace_id: t for t in aggregate.all_traces()}
    outcomes = {
        "cancelled": by_id[aggregate.all_traces()[0].trace_id].status,
        "ok": by_id[r_ok.headers["x-trace-id"]].status,
        "error": by_id[r_err.headers["x-trace-id"]].status,
        "timeout": by_id[r_timeout.headers["x-trace-id"]].status,
    }
    print(f"  outcomes={ {k: v.value for k, v in outcomes.items()} }")
    assert outcomes == {
        "cancelled": TraceStatus.CANCELLED,
        "ok": TraceStatus.OK,
        "error": TraceStatus.ERROR,
        "timeout": TraceStatus.TIMEOUT,
    }
    # four distinct final states, all finalized
    assert len({v for v in outcomes.values()}) == 4
    assert all(t.finished for t in by_id.values())
