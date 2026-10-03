"""Bounded in-process store of per-request diagnostic records.

Every finalized trace leaves behind an immutable :class:`TraceRecord` that can
be looked up later by its trace id (the value echoed in the response header).

Retention policy: the store holds at most ``capacity`` records.  When full,
the *least interesting* record is evicted first — an ok, non-slow record
(oldest first).  Interesting records (slow, error, timeout, cancelled) are
only evicted (oldest first) when the store contains nothing else.  This keeps
the records worth investigating from being crowded out by normal traffic.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from .config import get_config
from .context import StageRecord, TraceContext, TraceStatus


@dataclass(frozen=True)
class TraceRecord:
    """Immutable diagnostic snapshot of one finalized trace."""

    trace_id: str
    service_name: str
    status: TraceStatus
    total_ms: float
    is_slow: bool
    slow_threshold_ms: float
    stages: tuple[StageRecord, ...]
    dominant_stage: str | None
    finished_monotonic: float

    @property
    def interesting(self) -> bool:
        """Slow or non-ok records are the ones worth keeping around."""
        return self.is_slow or self.status is not TraceStatus.OK


_lock = threading.Lock()
# trace_id -> TraceRecord, insertion-ordered (oldest first)
_records: "OrderedDict[str, TraceRecord]" = OrderedDict()


def _snapshot(trace: TraceContext) -> TraceRecord:
    dominant = trace.dominant_stage
    return TraceRecord(
        trace_id=trace.trace_id,
        service_name=trace.service_name,
        status=trace.status,
        total_ms=trace.total_ms,
        is_slow=trace.is_slow,
        slow_threshold_ms=trace.slow_threshold_ms,
        stages=tuple(trace.stages),
        dominant_stage=dominant.name if dominant else None,
        finished_monotonic=trace.finished_at,
    )


def _evict_one() -> None:
    """Drop one record: oldest boring record, else oldest overall."""
    for tid, record in _records.items():
        if not record.interesting:
            del _records[tid]
            return
    if _records:
        _records.popitem(last=False)


def reset_store() -> None:
    with _lock:
        _records.clear()


def store_trace(trace: TraceContext) -> None:
    """Snapshot ``trace`` into the store, evicting if over capacity."""
    try:
        record = _snapshot(trace)
        limit = max(int(get_config().record_capacity), 1)
        with _lock:
            _records.pop(record.trace_id, None)  # re-insert as newest
            while len(_records) >= limit:
                _evict_one()
            _records[record.trace_id] = record
    except Exception:  # the store must never break request handling
        return


def get_record(trace_id: str) -> TraceRecord | None:
    """Look up the diagnostic record for ``trace_id`` (None if absent)."""
    with _lock:
        return _records.get(trace_id)


def all_records() -> list[TraceRecord]:
    with _lock:
        return list(_records.values())


def capacity() -> int:
    with _lock:
        return len(_records)
