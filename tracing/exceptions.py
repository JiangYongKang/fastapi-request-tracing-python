"""Exception classification for trace finalization.

Distinguishes the three abnormal endings:

* ``asyncio.CancelledError``      -> client disconnected / task cancelled
* ``asyncio.TimeoutError``        -> request exceeded its time budget
* any other exception             -> unhandled error
"""

from __future__ import annotations

import asyncio

from .context import TraceStatus


def classify_exception(exc: BaseException) -> TraceStatus:
    if isinstance(exc, asyncio.CancelledError):
        return TraceStatus.CANCELLED
    if isinstance(exc, asyncio.TimeoutError):
        return TraceStatus.TIMEOUT
    return TraceStatus.ERROR
