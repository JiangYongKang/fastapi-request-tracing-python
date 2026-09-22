"""Per-request trace context with task-safe propagation.

A :class:`contextvars.ContextVar` holds the current :class:`TraceContext`.
``asyncio.create_task`` copies the current context, so tasks spawned while a
request is being handled automatically inherit the same trace.  Child tasks
that only *read* the trace (or append stages) share the parent trace object;
stage appends are guarded by a lock so concurrent children cannot corrupt the
stage list.
"""

from __future__ import annotations

import contextvars
import enum
import threading
import time
import uuid
from dataclasses import dataclass, field


class TraceStatus(str, enum.Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class StageRecord:
    name: str
    elapsed_ms: float


def generate_trace_id() -> str:
    return uuid.uuid4().hex


@dataclass
class TraceContext:
    trace_id: str
    service_name: str = "local-service"
    slow_threshold_ms: float = 500.0
    status: TraceStatus = TraceStatus.OK
    parent_id: str | None = None
    stages: list[StageRecord] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    @property
    def finished(self) -> bool:
        return self.finished_at > 0.0

    @property
    def total_ms(self) -> float:
        if not self.finished:
            return 0.0
        return (self.finished_at - self.started_at) * 1000.0

    @property
    def is_slow(self) -> bool:
        return self.finished and self.total_ms >= self.slow_threshold_ms

    @property
    def dominant_stage(self) -> StageRecord | None:
        """The stage with the largest recorded elapsed time, if any."""
        with self._lock:
            if not self.stages:
                return None
            return max(self.stages, key=lambda s: s.elapsed_ms)

    def add_stage(self, name: str, elapsed_ms: float) -> None:
        with self._lock:
            self.stages.append(StageRecord(name=name, elapsed_ms=elapsed_ms))

    def finalize(self, status: TraceStatus) -> None:
        """Mark the trace finished exactly once; later calls are no-ops."""
        with self._lock:
            if self.finished_at > 0.0:
                return
            self.status = status
            self.finished_at = time.perf_counter()


_current_trace: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "current_trace", default=None
)


def new_trace(trace_id: str | None = None, config=None) -> TraceContext:
    """Create a trace context (not yet bound to the current task)."""
    from .config import get_config

    cfg = config if config is not None else get_config()
    return TraceContext(
        trace_id=trace_id or generate_trace_id(),
        service_name=cfg.service_name,
        slow_threshold_ms=cfg.slow_threshold_ms,
        started_at=time.perf_counter(),
    )


def set_current_trace(trace: TraceContext | None) -> contextvars.Token:
    return _current_trace.set(trace)


def reset_current_trace(token: contextvars.Token) -> None:
    _current_trace.reset(token)


def get_current_trace() -> TraceContext | None:
    return _current_trace.get()
