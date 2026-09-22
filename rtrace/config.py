"""追踪组件配置（可通过环境变量覆盖）。

环境变量（前缀 RTRACE_ 可自定义）：

* ``RTRACE_ENABLED``            : 总开关，1/0/true/false/yes/no
* ``RTRACE_SLOW_THRESHOLD_MS``  : 慢请求阈值（毫秒），总耗时 >= 阈值判为慢
* ``RTRACE_SAMPLE_RATE``        : 普通请求采样比例 [0,1]
* ``RTRACE_SAMPLE_ERROR_RATE``  : 失败/超时/取消请求的采样比例 [0,1]
* ``RTRACE_RECORD_SPAN_THRESHOLD_MS`` : 短于此阈值的阶段不进入聚合明细
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _as_bool(raw: str, default: bool) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return default


def _as_float(raw: str, default: float) -> float:
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class TracingConfig:
    """追踪开关、阈值与采样规则配置。"""

    enabled: bool = True
    slow_threshold_ms: float = 200.0
    sample_rate: float = 1.0
    sample_error_rate: float = 1.0
    record_span_threshold_ms: float = 0.0

    @staticmethod
    def from_env(prefix: str = "RTRACE_") -> "TracingConfig":
        base = TracingConfig()
        get = os.environ.get

        enabled = base.enabled
        if (raw := get(f"{prefix}ENABLED")) is not None:
            enabled = _as_bool(raw, base.enabled)

        slow = base.slow_threshold_ms
        if (raw := get(f"{prefix}SLOW_THRESHOLD_MS")) is not None:
            slow = _as_float(raw, slow)

        rate = base.sample_rate
        if (raw := get(f"{prefix}SAMPLE_RATE")) is not None:
            rate = _clamp(_as_float(raw, rate))

        error_rate = base.sample_error_rate
        if (raw := get(f"{prefix}SAMPLE_ERROR_RATE")) is not None:
            error_rate = _clamp(_as_float(raw, error_rate))

        span_threshold = base.record_span_threshold_ms
        if (raw := get(f"{prefix}RECORD_SPAN_THRESHOLD_MS")) is not None:
            span_threshold = max(0.0, _as_float(raw, span_threshold))

        return TracingConfig(
            enabled=enabled,
            slow_threshold_ms=slow,
            sample_rate=rate,
            sample_error_rate=error_rate,
            record_span_threshold_ms=span_threshold,
        )


def _clamp(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value
