"""Stage recording: measurement, no-op safety, disabled config."""

from __future__ import annotations

import asyncio

from tracing.config import TracingConfig, configure_tracing
from tracing.context import TraceStatus, new_trace, set_current_trace
from tracing.stages import record_stage, stage


async def test_stage_context_manager_measures():
    trace = new_trace("s-1")
    set_current_trace(trace)
    async with stage("db"):
        await asyncio.sleep(0.02)
    assert len(trace.stages) == 1
    assert trace.stages[0].name == "db"
    assert trace.stages[0].elapsed_ms >= 15.0


async def test_stage_records_even_on_exception():
    trace = new_trace("s-2")
    set_current_trace(trace)
    with __import__("pytest").raises(RuntimeError):
        async with stage("failing"):
            raise RuntimeError("boom")
    assert [s.name for s in trace.stages] == ["failing"]


def test_record_stage_without_trace_is_noop():
    record_stage("orphan", 1.0)  # must not raise


def test_record_stage_after_finalize_is_ignored():
    trace = new_trace("s-3")
    set_current_trace(trace)
    trace.finalize(TraceStatus.OK)
    record_stage("late", 1.0)
    assert trace.stages == []


async def test_disabled_tracing_records_nothing(tracing_state):
    configure_tracing(TracingConfig(enabled=False))
    trace = new_trace("s-4")
    set_current_trace(trace)
    async with stage("db"):
        await asyncio.sleep(0)
    record_stage("manual", 1.0)
    assert trace.stages == []
