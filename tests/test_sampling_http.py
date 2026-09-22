"""通过环境变量配置采样率，验证开关与确定性采样在 HTTP 链路上生效。"""

from __future__ import annotations

import importlib
import os

import httpx
import pytest


@pytest.fixture()
def low_rate_app(monkeypatch):
    # 正常请求零采样、失败请求全采样：确定性且易于断言
    monkeypatch.setenv("RTRACE_SAMPLE_RATE", "0.0")
    monkeypatch.setenv("RTRACE_SAMPLE_ERROR_RATE", "1.0")
    monkeypatch.setenv("RTRACE_SLOW_THRESHOLD_MS", "100")
    import main as main_module
    importlib.reload(main_module)
    yield main_module
    monkeypatch.delenv("RTRACE_SAMPLE_RATE", raising=False)
    monkeypatch.delenv("RTRACE_SAMPLE_ERROR_RATE", raising=False)
    monkeypatch.delenv("RTRACE_SLOW_THRESHOLD_MS", raising=False)
    importlib.reload(main_module)  # 还原默认配置，避免污染其他测试


@pytest.mark.asyncio
async def test_normal_requests_not_sampled_failures_are(low_rate_app):
    transport = httpx.ASGITransport(
        app=low_rate_app.app, raise_app_exceptions=False
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        await c.get("/work")
        await c.get("/work")
        await c.get("/error")
        snap_resp = await c.get("/metrics")
    snap = snap_resp.json()
    # /metrics 自身也是正常请求，不被采样；两条 /work 同样不采样
    # 仅失败的 /error 被采样
    paths = {d["path"] for d in snap["details"]}
    assert paths == {"/error"}
    assert snap["total"] == 1
    assert snap["status_counts"]["error"] == 1
    # 关闭开关的配置解析正确（不直接发请求，断言配置值）
    os.environ["RTRACE_ENABLED"] = "false"
    from rtrace.config import TracingConfig
    try:
        assert TracingConfig.from_env().enabled is False
    finally:
        del os.environ["RTRACE_ENABLED"]
