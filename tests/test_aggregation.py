"""Aggregation conclusions must be stable and reproducible."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from main import app
from tracing import aggregate
from tracing.context import TraceStatus, new_trace


def _finished_trace(trace_id: str, status: TraceStatus, stages: dict[str, float]):
    trace = new_trace(trace_id)
    for name, ms in stages.items():
        trace.add_stage(name, ms)
    trace.finalize(status)
    return trace


def test_summarize_is_pure_and_reproducible():
    for trace in [
        _finished_trace("a", TraceStatus.OK, {"db": 10.0, "compute": 30.0}),
        _finished_trace("b", TraceStatus.ERROR, {"db": 20.0}),
        _finished_trace("c", TraceStatus.TIMEOUT, {"io": 50.0}),
        _finished_trace("d", TraceStatus.CANCELLED, {}),
    ]:
        aggregate.register(trace)

    first = aggregate.summarize()
    second = aggregate.summarize()
    assert first == second, "same inputs must yield identical summaries"
    assert first["total"] == 4
    assert first["by_status"] == {"ok": 1, "error": 1, "timeout": 1, "cancelled": 1}
    assert first["stage_avg_ms"] == {"compute": 30.0, "db": 15.0, "io": 50.0}
    assert first["dominant_stage"] == "io"


def test_slow_judgement_is_stable_across_repeated_runs(tracing_state):
    """Same rule + same workload -> same slow/fast verdicts every run."""
    client = TestClient(app)
    verdicts_per_run = []
    for _ in range(3):
        aggregate.reset_registry()
        client.get("/slow", params={"ms": 300})   # threshold is 200ms
        client.get("/slow", params={"ms": 5})
        verdicts = [t.is_slow for t in aggregate.all_traces()]
        verdicts_per_run.append(verdicts)
    assert verdicts_per_run == [[True, False]] * 3


async def test_same_rule_repeated_load_gives_same_conclusions():
    """Repeated identical concurrent load -> identical status/slow counts."""
    summaries = []
    for _ in range(2):
        aggregate.reset_registry()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            import asyncio

            await asyncio.gather(
                client.get("/"),
                client.get("/error"),
                client.get("/timeout"),
                client.get("/slow", params={"ms": 300}),
                client.get("/work", params={"ms": 20}),
            )
        summary = aggregate.summarize()
        # compare only timing-independent conclusions
        summaries.append(
            (summary["total"], summary["by_status"], summary["slow"])
        )
    assert summaries[0] == summaries[1]
    total, by_status, slow = summaries[0]
    assert total == 5
    assert by_status == {"ok": 3, "error": 1, "timeout": 1, "cancelled": 0}
    assert slow == 1
