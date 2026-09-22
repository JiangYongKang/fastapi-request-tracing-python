"""阶段（Span）：记录单个处理阶段的耗时。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from rtrace.context import new_span_id


@dataclass
class Span:
    """一个处理阶段的计时记录。

    计时基于 ``time.perf_counter``（单调时钟，不受系统时间回拨影响）。
    """

    name: str
    span_id: str = field(default_factory=new_span_id)
    start_perf: float | None = None
    end_perf: float | None = None
    error: bool = False

    def __post_init__(self) -> None:
        if self.start_perf is None:
            self.start_perf = time.perf_counter()

    def finish(self) -> None:
        """结束阶段计时；重复调用保持幂等（以第一次结束为准）。"""

        if self.end_perf is None:
            self.end_perf = time.perf_counter()

    def mark_error(self) -> None:
        self.error = True

    @property
    def duration_ms(self) -> float:
        """阶段耗时（毫秒）；未结束时按当前时刻实时计算。"""

        end = self.end_perf if self.end_perf is not None else time.perf_counter()
        return max(0.0, (end - self.start_perf) * 1000.0)
