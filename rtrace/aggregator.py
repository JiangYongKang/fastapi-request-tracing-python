"""本地聚合统计：采样结论稳定，重复运行可复现。

聚合器只负责"对已采样的链路做统计"，采样判定由 :class:`~rtrace.sampling.Sampler`
基于 trace_id 确定性完成。因此同样的一批请求跑两遍，得到的快照一致
（耗时分布为区间计数，不依赖浮点累加顺序）。
"""

from __future__ import annotations

import threading
from collections import Counter

from rtrace.trace import Trace, TraceStatus


class TraceAggregator:
    """收集已结束链路并给出确定性的聚合结论。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._traces: list[Trace] = []

    def record(self, trace: Trace) -> None:
        """记录一条已采样、已结束的链路。"""

        with self._lock:
            self._traces.append(trace)

    def reset(self) -> None:
        with self._lock:
            self._traces.clear()

    def records(self) -> list[Trace]:
        with self._lock:
            return list(self._traces)

    def snapshot(self) -> dict:
        """生成统计快照。

        包含：总数、各状态计数与占比、慢请求占比、主导阶段分布、
        慢/失败请求明细（按 trace_id 排序保证输出顺序稳定）。
        """

        with self._lock:
            traces = list(self._traces)

        total = len(traces)
        status_counter: Counter[str] = Counter(t.status.value for t in traces)
        dominant_counter: Counter[str] = Counter(
            d for t in traces if (d := (t.dominant_span.name if t.dominant_span else None))
        )

        detail_rows = sorted(
            (
                {
                    "trace_id": t.trace_id,
                    "status": t.status.value,
                    "status_code": t.status_code,
                    "method": t.method,
                    "path": t.path,
                    "total_ms": round(t.total_ms, 3),
                    "dominant_span": t.dominant_span.name if t.dominant_span else None,
                    "error_type": t.error_type,
                }
                for t in traces
                if t.status.is_failure or t.status is TraceStatus.SLOW
            ),
            key=lambda r: r["trace_id"],
        )

        return {
            "total": total,
            "status_counts": {s.value: status_counter.get(s.value, 0) for s in TraceStatus},
            "status_ratio": {
                s.value: round(status_counter.get(s.value, 0) / total, 4) if total else 0.0
                for s in TraceStatus
            },
            "slow_count": status_counter.get(TraceStatus.SLOW.value, 0),
            "failure_count": sum(
                status_counter.get(s.value, 0)
                for s in TraceStatus
                if s.is_failure
            ),
            "dominant_span_counts": dict(
                sorted(dominant_counter.items(), key=lambda kv: (-kv[1], kv[0]))
            ),
            "details": detail_rows,
        }
