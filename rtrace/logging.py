"""结构化日志：每行带 trace_id 与阶段耗时依据。

输出示例（单行 JSON，便于本地 grep trace_id 关联整条链路）::

    {"event": "trace_end", "trace_id": "ab12...", "status": "slow",
     "total_ms": 231.4, "dominant_span": "db", "dominant_ms": 200.1,
     "spans": [{"name": "db", "duration_ms": 200.1, "error": false}]}
"""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from rtrace.trace import Trace

_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class _JsonFormatter(logging.Formatter):
    """把日志参数渲染为单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        # 业务侧若传入普通字符串消息则保持可读；dict 则序列化为 JSON
        payload = getattr(record, "rtrace_payload", None)
        if payload is None and isinstance(record.msg, dict):
            payload = record.msg
        if payload is not None:
            payload = dict(payload)
            payload.setdefault("level", record.levelname)
            payload.setdefault("logger", record.name)
            return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return super().format(record)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """配置 rtrace 命名空间下的日志输出到 stderr（幂等，不重复挂 handler）。"""

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger("rtrace")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False
    return root


def get_logger(name: str = "rtrace") -> logging.Logger:
    return logging.getLogger(name if name.startswith("rtrace") else f"rtrace.{name}")


def log_trace_summary(
    trace: "Trace",
    sampled: bool,
    logger: Optional[logging.Logger] = None,
) -> None:
    """打印链路结束日志。

    无论是否被采样都会输出一行（含 ``sampled`` 字段），便于在单测日志中
    直接看到链路标识、最终状态、总耗时、主导阶段及每个阶段的耗时依据。
    """

    logger = logger or get_logger()
    summary = trace.to_summary()
    payload = {"event": "trace_end", "sampled": sampled, **summary}
    # 结构化数据挂在 record 上（getMessage 保持简洁），formatter 与测试均可读取
    extra = {"rtrace_payload": payload}
    if trace.status.is_failure:
        logger.error("trace_end", extra=extra)
    else:
        logger.info("trace_end", extra=extra)
