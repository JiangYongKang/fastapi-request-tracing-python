"""External behavior must be identical whether tracing is on or off:
same status codes, same bodies; tracing never rewrites business outcomes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app
from tracing.config import TracingConfig, configure_tracing

client = TestClient(app, raise_server_exceptions=False)


def _get(path: str) -> tuple[int, object, dict]:
    resp = client.get(path)
    return resp.status_code, resp.json(), dict(resp.headers)


def test_normal_endpoints_identical_with_tracing_on_and_off(tracing_state):
    print("\n[scenario] 正常接口：追踪开/关，状态码与响应体完全一致")
    # demo endpoints whose body introspects the current trace report
    # trace_id=None when tracing is off; that is the endpoint's own output,
    # not the tracing layer rewriting it -- exclude those self-report fields
    introspection_fields = {"trace_id", "child_trace_id", "stages"}
    for path in ["/", "/work?ms=20", "/slow?ms=20", "/no-such-route"]:
        configure_tracing(TracingConfig(enabled=True, emit_logs=False))
        on_status, on_body, on_headers = _get(path)
        configure_tracing(TracingConfig(enabled=False))
        off_status, off_body, off_headers = _get(path)

        assert on_status == off_status, path
        strip = lambda b: {k: v for k, v in b.items() if k not in introspection_fields}
        assert strip(on_body) == strip(off_body), path
        # the only allowed wire difference: the echoed trace id header
        assert "x-trace-id" in on_headers and "x-trace-id" not in off_headers
        print(f"  {path}: {on_status} == {off_status}, body identical")


def test_business_4xx_is_not_rewritten(tracing_state):
    print("\n[scenario] 业务自身的 4xx 不被追踪改写")
    configure_tracing(TracingConfig(enabled=True, emit_logs=False))
    status, body, _ = _get("/no-such-route")
    assert status == 404
    assert body == {"detail": "Not Found"}


def test_unhandled_exception_keeps_500_and_timeout_keeps_504(tracing_state):
    print("\n[scenario] 未处理异常仍是 500、超时仍是 504，不被改写")
    configure_tracing(TracingConfig(enabled=True, emit_logs=False))
    err_status, err_body, err_headers = _get("/error")
    assert err_status == 500
    assert err_body == {"detail": "Internal Server Error"}
    assert "x-trace-id" in err_headers

    timeout_status, timeout_body, timeout_headers = _get("/timeout")
    assert timeout_status == 504
    assert timeout_body == {"detail": "Request timeout"}
    assert "x-trace-id" in timeout_headers
    print(f"  /error -> {err_status}, /timeout -> {timeout_status} (with trace id)")


def test_disconnect_scenarios_do_not_change_other_requests(tracing_state):
    """After a disconnected request, subsequent requests behave normally."""
    print("\n[scenario] 发生过断连之后，后续请求对外表现不受影响")
    import asyncio

    from tests.test_disconnect import _scope

    async def disconnect_once():
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(0.02)
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        await app(_scope("/slow?ms=5000"), receive, send)

    import anyio

    anyio.run(disconnect_once)

    status, body, _ = _get("/")
    assert status == 200 and body == {"Hello": "World"}
