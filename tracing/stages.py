"""Stage recording helpers.

Two styles:

* ``record_stage(name, elapsed_ms)`` — record an already-measured duration.
* ``async with stage("db"):`` — measure the wrapped block automatically.

Both attach the measurement to the *current* trace (the one bound to the
running task).  If tracing is disabled or no trace is active, they are
no-ops and never raise.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from .config import get_config
from .context import get_current_trace


def record_stage(name: str, elapsed_ms: float) -> None:
    """Record a stage duration on the current trace. Never raises."""
    try:
        if not get_config().enabled:
            return
        trace = get_current_trace()
        if trace is None or trace.finished:
            return
        trace.add_stage(name, float(elapsed_ms))
    except Exception:  # tracing must never break business logic
        return


@asynccontextmanager
async def stage(name: str) -> AsyncIterator[None]:
    """Measure the wrapped async block as a stage on the current trace."""
    start = time.perf_counter()
    try:
        yield
    finally:
        record_stage(name, (time.perf_counter() - start) * 1000.0)
