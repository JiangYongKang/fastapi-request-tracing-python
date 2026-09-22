"""确定性采样：同一 trace_id 的采样结论稳定可复现。

规则（确定性、无随机数、无时钟依赖）：

1. 对 trace_id 做 CRC32，映射到 [0, 9999] 共 10000 个桶；
2. 普通请求：``bucket < sample_rate * 10000`` 时采样；
3. 失败/超时/取消请求：使用独立的 ``sample_error_rate``，
   便于"全量采错误、按比例采正常"；
4. 因为桶只由 trace_id 决定，同一条链路无论被判定多少次、
   聚合重复运行多少次，采样结论与聚合结果都一致可复现。
"""

from __future__ import annotations

import zlib

_BUCKETS = 10_000


class Sampler:
    """基于 trace_id 哈希的确定性采样器。"""

    def __init__(self, rate: float = 1.0, error_rate: float = 1.0) -> None:
        self.rate = _clamp(rate)
        self.error_rate = _clamp(error_rate)

    def should_sample(self, trace_id: str, is_error: bool = False) -> bool:
        rate = self.error_rate if is_error else self.rate
        if rate <= 0.0:
            return False
        if rate >= 1.0:
            return True
        return self.stable_bucket(trace_id) < int(round(rate * _BUCKETS))

    @staticmethod
    def stable_bucket(trace_id: str) -> int:
        """trace_id 的稳定桶号（0..9999），只依赖输入字符串。"""

        return zlib.crc32(trace_id.encode("utf-8")) % _BUCKETS


def _clamp(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value
