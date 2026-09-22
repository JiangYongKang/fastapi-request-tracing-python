"""并发请求的链路传播与隔离：

* 派生（create_task / TaskGroup）与延迟（BackgroundTasks）执行的任务
  必须继承同一条 trace_id；
* 并发请求之间上下文不得串扰；
* 日志中必须能通过 trace_id 关联同一请求的全部阶段。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from main import app


@pytest.mark.asyncio
async def test_spawned_and_background_tasks_inherit_trace():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/spawn", params={"wait_ms": 10})
    assert resp.status_code == 200
    data = resp.json()
    trace_id = data["trace_id"]
    # 两个并发子任务都看到同一个 trace_id
    assert data["children_seen"] == [trace_id, trace_id]
    assert trace_id is not None and len(trace_id) == 32


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_cross_talk():
    """高并发下每个响应只携带自己的 trace_id，聚合阶段也归属各自链路。"""

    transport = httpx.ASGITransport(app=app)
    n = 30

    async def one(i: int) -> tuple[str, list[str]]:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            resp = await c.get("/spawn", params={"wait_ms": 5})
        body = resp.json()
        return body["trace_id"], body["children_seen"]

    results = await asyncio.gather(*(one(i) for i in range(n)))
    trace_ids = [r[0] for r in results]
    # 每个请求链路标识唯一
    assert len(set(trace_ids)) == n
    for trace_id, children in results:
        assert children == [trace_id, trace_id]


@pytest.mark.asyncio
async def test_trace_id_present_in_logs_and_correlates_spans(caplog):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/work")
    assert resp.status_code == 200

    # 找到 /work 的结束日志，验证 trace_id 与各阶段耗时依据都在日志里
    entries = [
        getattr(r, "rtrace_payload", None)
        for r in caplog.records
        if r.name.startswith("rtrace")
    ]
    work_logs = [m for m in entries if isinstance(m, dict) and m.get("path") == "/work"]
    assert work_logs, entries
    entry = work_logs[-1]
    assert len(entry["trace_id"]) == 32
    span_names = {s["name"] for s in entry["spans"]}
    assert {"fetch", "compute"} <= span_names
    assert entry["dominant_span"] == "fetch"
    for span in entry["spans"]:
        assert span["duration_ms"] >= 0.0
    print("\n[log-evidence] trace_id=%s spans=%s total_ms=%s" % (
        entry["trace_id"],
        [(s["name"], s["duration_ms"]) for s in entry["spans"]],
        entry["total_ms"],
    ))
