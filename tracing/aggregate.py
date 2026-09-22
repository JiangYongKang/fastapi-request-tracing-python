"""Deterministic in-process registry of finalized traces.

Used by tests and local verification to aggregate outcomes.  Aggregation is
a pure function of the registered traces, so repeated runs over the same
inputs produce identical summaries.
"""

from __future__ import annotations

import threading
from typing import Any

from .context import TraceStatus

_lock = threading.Lock()
_traces: list[Any] = []


def reset_registry() -> None:
    with _lock:
        _traces.clear()


def register(trace: Any) -> None:
    with _lock:
        _traces.append(trace)


def all_traces() -> list[Any]:
    with _lock:
        return list(_traces)


def summarize() -> dict:
    """Aggregate registered traces into a stable, reproducible summary."""
    with _lock:
        traces = list(_traces)
    by_status: dict[str, int] = {s.value: 0 for s in TraceStatus}
    slow = 0
    stage_totals: dict[str, float] = {}
    stage_counts: dict[str, int] = {}
    for t in traces:
        by_status[t.status.value] += 1
        if t.is_slow:
            slow += 1
        for st in t.stages:
            stage_totals[st.name] = stage_totals.get(st.name, 0.0) + st.elapsed_ms
            stage_counts[st.name] = stage_counts.get(st.name, 0) + 1
    stage_avg = {
        name: stage_totals[name] / stage_counts[name]
        for name in sorted(stage_totals)
    }
    dominant = max(stage_totals, key=stage_totals.get) if stage_totals else None
    return {
        "total": len(traces),
        "by_status": by_status,
        "slow": slow,
        "stage_avg_ms": stage_avg,
        "dominant_stage": dominant,
    }
