"""Helpers to propagate the trace into spawned / delayed async tasks.

``asyncio.create_task`` copies the current ``contextvars`` context, so tasks
created while a request is being handled already inherit the trace.  These
helpers make the inheritance explicit and robust:

* :func:`traced_task` binds the *currently active* trace inside the new task,
  even if the task is created after the caller's context was reset.
* :func:`traced_background` runs a sync callable in the default executor with
  the current context copied into the worker thread.

All helpers are no-ops with respect to business behavior: the callable always
runs, whether or not a trace is active.
"""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import contextmanager
from typing import Any, Awaitable, Callable, Iterator, TypeVar

from .context import (
    TraceContext,
    get_current_trace,
    reset_current_trace,
    set_current_trace,
)

T = TypeVar("T")


@contextmanager
def trace_as_current(trace: TraceContext | None = None) -> Iterator[TraceContext | None]:
    """Bind ``trace`` (default: the current one) for the enclosed block."""
    token = set_current_trace(trace if trace is not None else get_current_trace())
    try:
        yield get_current_trace()
    finally:
        reset_current_trace(token)


async def _run_with_trace(coro: Awaitable[T], trace: TraceContext | None) -> T:
    token = set_current_trace(trace)
    try:
        return await coro
    finally:
        reset_current_trace(token)


def traced_task(coro: Awaitable[T], *, name: str | None = None) -> "asyncio.Task[T]":
    """Create a task that runs ``coro`` with the current trace bound."""
    trace = get_current_trace()
    task_name = name or (f"trace-{trace.trace_id[:8]}" if trace else None)
    return asyncio.create_task(_run_with_trace(coro, trace), name=task_name)


def traced_background(
    func: Callable[..., T], *args: Any, **kwargs: Any
) -> "asyncio.Future[T]":
    """Run a sync callable in the default executor with the current context."""
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(None, lambda: ctx.run(func, *args, **kwargs))
