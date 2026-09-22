"""Trace：单条请求链路的聚合根。

状态机（只允许异常类状态优先，结束后冻结）::

    OK ──(总耗时 >= 阈值)──> SLOW
    OK ──(未处理异常)──────> ERROR
    OK ──(asyncio 超时)────> TIMEOUT
    *  ──(客户端中断/取消)──> CANCELLED   （优先级最高）
"""

from __future__ import annotations

import asyncio
import enum
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Optional

from rtrace.context import new_span_id, new_trace_id
from rtrace.span import Span

if TYPE_CHECKING:
    from rtrace.config import TracingConfig


class TraceStatus(enum.Enum):
    """链路结束状态，四种可区分结论。"""

    OK = "ok"
    SLOW = "slow"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"

    @property
    def is_failure(self) -> bool:
        return self in (TraceStatus.ERROR, TraceStatus.TIMEOUT, TraceStatus.CANCELLED)


def classify_exception(exc: BaseException) -> TraceStatus:
    """根据异常类型给出链路状态。

    * :class:`asyncio.CancelledError`  -> CANCELLED（客户端中断/任务取消）
    * :class:`TimeoutError`（含 asyncio.TimeoutError，3.11+ 为其别名）-> TIMEOUT
    * 其它异常                          -> ERROR
    """

    if isinstance(exc, asyncio.CancelledError):
        return TraceStatus.CANCELLED
    if isinstance(exc, TimeoutError):
        return TraceStatus.TIMEOUT
    return TraceStatus.ERROR


class Trace:
    """单条请求的追踪记录。"""

    def __init__(
        self,
        trace_id: str | None = None,
        span_id: str | None = None,
        method: str = "",
        path: str = "",
        config: "Optional[TracingConfig]" = None,
        start_perf: float | None = None,
    ) -> None:
        self.trace_id = trace_id or new_trace_id()
        self.span_id = span_id or new_span_id()
        self.method = method
        self.path = path
        self.config = config
        self.status: TraceStatus = TraceStatus.OK
        self.error_type: Optional[str] = None
        self.error_message: Optional[str] = None
        self.status_code: Optional[int] = None
        self.start_perf: float = (
            start_perf if start_perf is not None else time.perf_counter()
        )
        self.end_perf: Optional[float] = None
        self._spans: list[Span] = []
        self._lock = threading.Lock()
        self._finished = False

    # ---- 阶段记录 -----------------------------------------------------

    def start_span(self, name: str) -> Span:
        """开启一个命名阶段。"""

        span = Span(name=name)
        with self._lock:
            self._spans.append(span)
        return span

    def record_span(self, span: Span) -> None:
        """登记一个外部创建的阶段（尚未结束的保持实时计算）。"""

        with self._lock:
            if span not in self._spans:
                self._spans.append(span)

    @contextmanager
    def span(self, name: str):
        """同步上下文管理器：with trace.span('x'): ..."""

        span = self.start_span(name)
        try:
            yield span
        except BaseException:
            span.mark_error()
            raise
        finally:
            span.finish()

    # ---- 状态标记 -----------------------------------------------------

    def mark_error(self, exc: BaseException) -> None:
        """记录异常；CancelledError 优先级最高且不可被覆盖。"""

        new_status = classify_exception(exc)
        # 取消是终局信号：一旦发生不允许被其它结论覆盖
        if self.status is TraceStatus.CANCELLED:
            return
        self.status = new_status
        self.error_type = type(exc).__name__
        message = str(exc) or repr(exc)
        self.error_message = message[:500]

    # ---- 收尾 ---------------------------------------------------------

    def finish(
        self,
        status_code: int | None = None,
        end_perf: float | None = None,
    ) -> TraceStatus:
        """结束链路并给出最终状态；幂等，重复调用返回同一结论。"""

        if self._finished:
            return self.status
        self.end_perf = end_perf if end_perf is not None else time.perf_counter()
        if status_code is not None:
            self.status_code = status_code

        if self.status is TraceStatus.OK:
            threshold = self.config.slow_threshold_ms if self.config else 200.0
            if self.total_ms >= threshold:
                self.status = TraceStatus.SLOW

        self._finished = True
        return self.status

    # ---- 只读视图 -----------------------------------------------------

    @property
    def spans(self) -> list[Span]:
        with self._lock:
            return list(self._spans)

    @property
    def total_ms(self) -> float:
        end = self.end_perf if self.end_perf is not None else time.perf_counter()
        return max(0.0, (end - self.start_perf) * 1000.0)

    @property
    def dominant_span(self) -> Optional[Span]:
        """耗时占主导的阶段（并列时取最先开始的，保证结论稳定）。"""

        with self._lock:
            spans = list(self._spans)
        if not spans:
            return None
        return max(spans, key=lambda s: (s.duration_ms, -s.start_perf))

    @property
    def finished(self) -> bool:
        return self._finished

    def to_summary(self) -> dict:
        """输出可写入日志/聚合器的稳定字典（耗时保留 3 位小数）。"""

        dominant = self.dominant_span
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "method": self.method,
            "path": self.path,
            "status": self.status.value,
            "status_code": self.status_code,
            "total_ms": round(self.total_ms, 3),
            "dominant_span": dominant.name if dominant else None,
            "dominant_ms": round(dominant.duration_ms, 3) if dominant else None,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "spans": [
                {
                    "name": s.name,
                    "duration_ms": round(s.duration_ms, 3),
                    "error": s.error,
                }
                for s in sorted(self.spans, key=lambda s: s.start_perf)
            ],
        }
