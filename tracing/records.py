"""Bounded post-hoc diagnostic records, lookupable by trace id.

Every finalized trace leaves a compact :class:`TraceRecord` in an in-process
store, so a past request can be inspected afterwards using the trace id that
was echoed in the response header.

The store is bounded.  When it is full, eviction favours keeping the records
worth revisiting — slow, failed, timed-out or cancelled requests — over
ordinary fast/ok ones:

* a new *normal* record never evicts an *interesting* one (it evicts the
  oldest normal record, or is dropped itself when the store holds only
  interesting records);
* a new *interesting* record evicts the oldest normal record first, and only
  falls back to evicting the oldest interesting record when nothing normal
  remains.

Everything is thread-safe and never raises: recording must not affect the
request path.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TraceRecord:
    """Immutable post-hoc diagnosis of one finished request."""

    trace_id: str
    service_name: str
    status: str
    total_ms: float
    slow: bool
    stages: tuple[tuple[str, float], ...]
    dominant_stage: str | None
    dominant_stage_ms: float | None
    recorded_at: float = field(default_factory=time.time)

    @property
    def interesting(self) -> bool:
        """Slow or non-ok records are the ones worth revisiting."""
        return self.slow or self.status != "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "service_name": self.service_name,
            "status": self.status,
            "total_ms": round(self.total_ms, 3),
            "slow": self.slow,
            "stages": [
                {"name": name, "elapsed_ms": round(ms, 3)}
                for name, ms in self.stages
            ],
            "dominant_stage": self.dominant_stage,
            "dominant_stage_ms": (
                round(self.dominant_stage_ms, 3)
                if self.dominant_stage_ms is not None
                else None
            ),
            "recorded_at": self.recorded_at,
        }


_lock = threading.Lock()
_records: "OrderedDict[str, TraceRecord]" = OrderedDict()
_normal_ids: "deque[str]" = deque()  # insertion order of non-interesting ids


def reset() -> None:
    """Drop all records (test isolation / local debugging)."""
    with _lock:
        _records.clear()
        _normal_ids.clear()


def record(trace: Any, capacity: int = 256) -> None:
    """Snapshot a finalized trace into the store. Never raises."""
    try:
        _record(trace, capacity)
    except Exception:
        return


def _record(trace: Any, capacity: int) -> None:
    if capacity <= 0 or not getattr(trace, "finished", False):
        return
    dominant = trace.dominant_stage
    entry = TraceRecord(
        trace_id=trace.trace_id,
        service_name=trace.service_name,
        status=trace.status.value,
        total_ms=trace.total_ms,
        slow=trace.is_slow,
        stages=tuple((s.name, s.elapsed_ms) for s in trace.stages),
        dominant_stage=dominant.name if dominant else None,
        dominant_stage_ms=dominant.elapsed_ms if dominant else None,
    )
    with _lock:
        _insert(entry, capacity)


def _insert(entry: TraceRecord, capacity: int) -> None:
    old = _records.pop(entry.trace_id, None)
    if old is not None and not old.interesting:
        try:
            _normal_ids.remove(entry.trace_id)
        except ValueError:
            pass
    while len(_records) >= capacity:
        if _normal_ids:
            victim = _normal_ids.popleft()
            _records.pop(victim, None)
        elif entry.interesting:
            _records.popitem(last=False)  # oldest interesting record
        else:
            return  # store full of interesting records: drop the normal one
    _records[entry.trace_id] = entry
    if not entry.interesting:
        _normal_ids.append(entry.trace_id)


def lookup(trace_id: str) -> TraceRecord | None:
    """Find the diagnostic record for a trace id, if still retained."""
    with _lock:
        return _records.get(trace_id)


def all_records() -> list[TraceRecord]:
    with _lock:
        return list(_records.values())


def size() -> int:
    with _lock:
        return len(_records)
