"""Demo FastAPI app with request tracing.

The original ``GET /`` endpoint is unchanged.  Additional demo endpoints
exercise the tracing capabilities locally:

* ``GET /work?ms=N``    staged work (db/compute) + a traced child task
* ``GET /slow?ms=N``    defaults above the slow threshold
* ``GET /error``        raises an unhandled exception
* ``GET /timeout``      exceeds its time budget (asyncio.TimeoutError)
* ``GET /cancel``       long-running; meant to be cancelled by the client
* ``GET /hang``         long-running WITHOUT its own disconnect check —
                        the tracing middleware alone must close the trace
                        as cancelled when the client goes away
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Query, Request

from tracing import (
    TraceMiddleware,
    configure_tracing,
    get_current_trace,
    record_stage,
    stage,
    traced_task,
)
from tracing.config import TracingConfig

configure_tracing(TracingConfig.from_env())

app = FastAPI(title="fastapi-request-tracing-python")
app.add_middleware(TraceMiddleware)


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/work")
async def work(ms: int = Query(default=100, ge=0, le=10_000)):
    async with stage("db"):
        await asyncio.sleep(ms * 0.7 / 1000.0)

    async def child_job():
        # spawned task inherits the same trace automatically
        async with stage("child"):
            await asyncio.sleep(0.001)
        return get_current_trace().trace_id if get_current_trace() else None

    child_trace_id = await traced_task(child_job())

    async with stage("compute"):
        await asyncio.sleep(ms * 0.3 / 1000.0)

    trace = get_current_trace()
    return {
        "trace_id": trace.trace_id if trace else None,
        "child_trace_id": child_trace_id,
        "stages": [s.name for s in trace.stages] if trace else [],
    }


@app.get("/slow")
async def slow(ms: int = Query(default=600, ge=0, le=30_000)):
    async with stage("heavy-io"):
        await asyncio.sleep(ms / 1000.0)
    trace = get_current_trace()
    return {"trace_id": trace.trace_id if trace else None}


@app.get("/error")
async def boom():
    record_stage("before-crash", 1.0)
    raise RuntimeError("demo failure")


@app.get("/timeout")
async def timeout():
    async with stage("slow-dependency"):
        await asyncio.wait_for(asyncio.sleep(30), timeout=0.05)
    return {"unreachable": True}


@app.get("/cancel")
async def cancellable(request: Request):
    async with stage("long-work"):
        # Detect the client going away; uvicorn does not cancel the handler
        # task on disconnect for HTTP/1.1, so we raise CancelledError to
        # surface the interruption through the tracing middleware.
        while True:
            if await request.is_disconnected():
                raise asyncio.CancelledError("client disconnected")
            await asyncio.sleep(0.02)
    return {"unreachable": True}


@app.get("/hang")
async def hang():
    # Deliberately does NOT check for client disconnect: the tracing
    # middleware alone is responsible for spotting the disconnect, stopping the
    # work and closing the trace as cancelled.
    async with stage("oblivious-work"):
        await asyncio.sleep(30)
    return {"unreachable": True}
