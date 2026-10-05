"""Postmortem diagnostic records with lookup by trace id.

After every request is finalized, an immutable :class:`DiagnosticRecord`
snapshot is stored locally.  Given the trace id echoed in the response
header, the record for exactly that request can be retrieved later — it
carries the final status, total duration, per-stage durations and the
dominant (most expensive) stage.

The store is bounded: at most ``capacity`` records are retained.  When the
store is full, eviction favours keeping records that are worth revisiting —
slow requests and non-ok endings (error / timeout / cancelled) — over plain
fast ok records:

1. evict the oldest *uninteresting* (ok and not slow) record first;
2. if every retained record is interesting and the newcomer is not, the
   newcomer is dropped instead of evicting an interesting record;
3. otherwise (all interesting, newcomer interesting) evict the oldest
   record overall.

Records of concurrent requests are independent snapshots keyed by trace id,
so concurrent lookups never cross-talk.  All operations are thread-safe and
never raise — diagnostics must not affect request handling.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from .context import TraceContext, TraceStatus


@dataclass(frozen=True)
class StageTiming:
    name: str
    elapsed_ms: float


@dataclass(frozen=True)
class DiagnosticRecord:
    """Immutable postmortem snapshot of one finalized trace."""

    trace_id: str
    service_name: str
    status: TraceStatus
    total_ms: float
    slow_threshold_ms: float
    is_slow: bool
    stages: tuple[StageTiming, ...] = field(default_factory=tuple)
    dominant_stage: StageTiming | None = None

    @property
    def interesting(self) -> bool:
        """Worth revisiting: slow, or ended abnormally (not plain ok)."""
        return self.is_slow or self.status is not TraceStatus.OK

    def as_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "service_name": self.service_name,
            "status": self.status.value,
            "total_ms": self.total_ms,
            "slow_threshold_ms": self.slow_threshold_ms,
            "is_slow": self.is_slow,
            "stages": [
                {"name": s.name, "elapsed_ms": s.elapsed_ms} for s in self.stages
            ],
            "dominant_stage": (
                {
                    "name": self.dominant_stage.name,
                    "elapsed_ms": self.dominant_stage.elapsed_ms,
                }
                if self.dominant_stage
                else None
            ),
        }


def record_from_trace(trace: TraceContext) -> DiagnosticRecord:
    """Snapshot a finalized trace into an immutable record."""
    with trace._lock:
        stages = tuple(
            StageTiming(name=s.name, elapsed_ms=s.elapsed_ms) for s in trace.stages
        )
    dominant = max(stages, key=lambda s: s.elapsed_ms) if stages else None
    return DiagnosticRecord(
        trace_id=trace.trace_id,
        service_name=trace.service_name,
        status=trace.status,
        total_ms=trace.total_ms,
        slow_threshold_ms=trace.slow_threshold_ms,
        is_slow=trace.is_slow,
        stages=stages,
        dominant_stage=dominant,
    )


class DiagnosticStore:
    """Bounded, thread-safe store of diagnostic records keyed by trace id."""

    def __init__(self, capacity: int = 256) -> None:
        self._capacity = max(int(capacity), 0)
        self._records: "OrderedDict[str, DiagnosticRecord]" = OrderedDict()
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def set_capacity(self, capacity: int) -> None:
        """Resize; if shrinking, evict by the same retention policy."""
        with self._lock:
            self._capacity = max(int(capacity), 0)
            self._evict_until_fits()

    def store(self, record: DiagnosticRecord) -> bool:
        """Insert a record. Returns True if it was retained. Never raises."""
        try:
            with self._lock:
                if self._capacity <= 0:
                    return False
                if record.trace_id in self._records:
                    # re-insertion replaces and refreshes recency
                    del self._records[record.trace_id]
                    self._records[record.trace_id] = record
                    return True
                if len(self._records) < self._capacity:
                    self._records[record.trace_id] = record
                    return True
                # full: try to evict the oldest uninteresting record
                victim = self._oldest(interesting=False)
                if victim is None:
                    if not record.interesting:
                        return False  # never evict interesting for boring
                    victim = self._oldest(interesting=True)
                if victim is None:
                    return False
                del self._records[victim]
                self._records[record.trace_id] = record
                return True
        except Exception:
            return False

    def lookup(self, trace_id: str) -> DiagnosticRecord | None:
        """Find the record for a trace id (from the response header)."""
        with self._lock:
            return self._records.get(trace_id)

    def all(self) -> list[DiagnosticRecord]:
        with self._lock:
            return list(self._records.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def reset(self) -> None:
        with self._lock:
            self._records.clear()

    # -- internals ---------------------------------------------------------

    def _oldest(self, *, interesting: bool) -> str | None:
        for trace_id, record in self._records.items():  # insertion order
            if record.interesting == interesting:
                return trace_id
        return None

    def _evict_until_fits(self) -> None:
        while len(self._records) > self._capacity:
            victim = self._oldest(interesting=False)
            if victim is None:
                victim = next(iter(self._records), None)
            if victim is None:
                return
            del self._records[victim]


# Process-wide default store.  Capacity follows the active config on every
# insert, so ``configure_tracing(TracingConfig(record_capacity=N))`` and
# ``TRACING_RECORD_CAPACITY`` both take effect without extra wiring.
_default_store = DiagnosticStore()


def get_store() -> DiagnosticStore:
    return _default_store


def store_trace(trace: TraceContext) -> DiagnosticRecord | None:
    """Snapshot ``trace`` into the default store. Never raises."""
    try:
        from .config import get_config

        store = get_store()
        store.set_capacity(get_config().record_capacity)
        record = record_from_trace(trace)
        store.store(record)
        return record
    except Exception:
        return None


def lookup(trace_id: str) -> DiagnosticRecord | None:
    """Look up the diagnostic record for a response-header trace id."""
    try:
        return get_store().lookup(trace_id)
    except Exception:
        return None


def reset_store() -> None:
    try:
        get_store().reset()
    except Exception:
        return
