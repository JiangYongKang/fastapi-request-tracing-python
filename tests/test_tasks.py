"""Spawned / delayed tasks must inherit the same trace."""

from __future__ import annotations

import asyncio

from tracing.context import get_current_trace, new_trace, set_current_trace
from tracing.stages import record_stage
from tracing.tasks import trace_as_current, traced_background, traced_task


async def test_traced_task_inherits_trace():
    trace = new_trace("task-1")
    set_current_trace(trace)

    async def child():
        return get_current_trace().trace_id

    child_trace_id = await traced_task(child())
    assert child_trace_id == "task-1"


async def test_traced_task_stage_lands_on_parent_trace():
    trace = new_trace("task-2")
    set_current_trace(trace)

    async def child():
        record_stage("child-stage", 7.5)

    await traced_task(child())
    assert [(s.name, s.elapsed_ms) for s in trace.stages] == [("child-stage", 7.5)]


async def test_many_concurrent_children_share_one_trace():
    trace = new_trace("task-3")
    set_current_trace(trace)

    async def child(i: int):
        await asyncio.sleep(0.001 * (i % 3))
        assert get_current_trace().trace_id == "task-3"
        record_stage(f"child-{i}", float(i))

    await asyncio.gather(*(traced_task(child(i)) for i in range(10)))
    assert len(trace.stages) == 10
    assert {s.name for s in trace.stages} == {f"child-{i}" for i in range(10)}


async def test_traced_background_sync_function_inherits_trace():
    trace = new_trace("task-4")
    set_current_trace(trace)

    def sync_work():
        return get_current_trace().trace_id if get_current_trace() else None

    result = await traced_background(sync_work)
    assert result == "task-4"


async def test_trace_as_current_binds_explicit_trace():
    other = new_trace("task-5")
    assert get_current_trace() is None
    with trace_as_current(other):
        assert get_current_trace() is other
    assert get_current_trace() is None


async def test_delayed_task_still_inherits():
    """A task created after an await (delayed execution) keeps the trace."""
    trace = new_trace("task-6")
    set_current_trace(trace)

    async def child():
        return get_current_trace().trace_id

    await asyncio.sleep(0.01)
    task = traced_task(child())
    await asyncio.sleep(0.01)
    assert await task == "task-6"
