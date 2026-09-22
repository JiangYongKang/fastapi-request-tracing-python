"""Trace context lifecycle and propagation basics."""

from __future__ import annotations

import asyncio

from tracing.context import (
    TraceStatus,
    generate_trace_id,
    get_current_trace,
    new_trace,
    reset_current_trace,
    set_current_trace,
)


def test_trace_ids_are_unique():
    ids = {generate_trace_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_finalize_is_idempotent_and_sets_status():
    trace = new_trace("t-finalize")
    trace.finalize(TraceStatus.OK)
    first_finished_at = trace.finished_at
    trace.finalize(TraceStatus.ERROR)  # must be a no-op
    assert trace.status is TraceStatus.OK
    assert trace.finished_at == first_finished_at
    assert trace.finished


def test_dominant_stage_picks_largest():
    trace = new_trace("t-dom")
    trace.add_stage("db", 10.0)
    trace.add_stage("compute", 42.5)
    trace.add_stage("render", 3.0)
    dominant = trace.dominant_stage
    assert dominant is not None
    assert dominant.name == "compute"
    assert dominant.elapsed_ms == 42.5


def test_empty_trace_has_no_dominant_stage():
    assert new_trace("t-empty").dominant_stage is None


def test_slow_flag_uses_threshold():
    trace = new_trace("t-slow")
    trace.slow_threshold_ms = 0.0  # everything is slow
    trace.finalize(TraceStatus.OK)
    assert trace.is_slow


def test_contextvar_binds_and_resets():
    assert get_current_trace() is None
    trace = new_trace("t-bind")
    token = set_current_trace(trace)
    assert get_current_trace() is trace
    reset_current_trace(token)
    assert get_current_trace() is None


async def test_context_isolated_between_concurrent_tasks():
    """Two tasks with different traces must not see each other's trace."""
    seen: dict[str, str] = {}

    async def worker(name: str, trace_id: str):
        token = set_current_trace(new_trace(trace_id))
        try:
            await asyncio.sleep(0.01)  # interleave
            seen[name] = get_current_trace().trace_id
        finally:
            reset_current_trace(token)

    await asyncio.gather(worker("a", "trace-a"), worker("b", "trace-b"))
    assert seen == {"a": "trace-a", "b": "trace-b"}
