"""Logging configuration for the tracing component.

Every trace log line carries the trace id, final status, total duration and
per-stage timings, so a log line can be correlated with the request that
produced it via the ``X-Trace-Id`` response header.
"""

from __future__ import annotations

import logging

from .context import TraceContext

LOGGER_NAME = "tracing"


def setup_tracing_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        setup_tracing_logging()
    return logger


def format_trace_line(trace: TraceContext, *, sampled: bool) -> str:
    stages = " ".join(
        f"{s.name}={s.elapsed_ms:.1f}ms" for s in trace.stages
    ) or "-"
    dominant = trace.dominant_stage
    dominant_txt = (
        f"{dominant.name}={dominant.elapsed_ms:.1f}ms" if dominant else "-"
    )
    return (
        f"trace_id={trace.trace_id} service={trace.service_name} "
        f"status={trace.status.value} total={trace.total_ms:.1f}ms "
        f"threshold={trace.slow_threshold_ms:.1f}ms slow={trace.is_slow} "
        f"sampled={sampled} dominant={dominant_txt} stages=[{stages}]"
    )


def log_trace(trace: TraceContext, *, sampled: bool) -> None:
    """Emit one trace line. Never raises — logging must not break requests."""
    try:
        logger = get_logger()
        line = format_trace_line(trace, sampled=sampled)
        if trace.is_slow or trace.status is not trace.status.OK:
            logger.warning(line)
        else:
            logger.info(line)
    except Exception:
        return
