"""Tracing configuration loaded from environment variables.

Environment variables (all optional):
    TRACING_ENABLED          "1"/"true"/"yes" -> on, "0"/"false"/"no" -> off
    TRACING_SLOW_THRESHOLD_MS  slow-request threshold in milliseconds
    TRACING_SAMPLE_RATE      base sampling rate in [0.0, 1.0]
    TRACING_SERVICE_NAME     service name attached to every trace
    TRACING_HEADER_NAME      header used to carry/echo the trace id
    TRACING_EMIT_LOGS        "1"/"true"/"yes" -> emit per-request trace logs
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class TracingConfig:
    enabled: bool = True
    slow_threshold_ms: float = 500.0
    sample_rate: float = 1.0
    service_name: str = "local-service"
    header_name: str = "X-Trace-Id"
    emit_logs: bool = True
    records_capacity: int = 256

    @classmethod
    def from_env(cls) -> "TracingConfig":
        return cls(
            enabled=_env_bool("TRACING_ENABLED", True),
            slow_threshold_ms=_env_float("TRACING_SLOW_THRESHOLD_MS", 500.0),
            sample_rate=min(max(_env_float("TRACING_SAMPLE_RATE", 1.0), 0.0), 1.0),
            service_name=os.environ.get("TRACING_SERVICE_NAME", "local-service"),
            header_name=os.environ.get("TRACING_HEADER_NAME", "X-Trace-Id"),
            emit_logs=_env_bool("TRACING_EMIT_LOGS", True),
            records_capacity=max(_env_int("TRACING_RECORDS_CAPACITY", 256), 0),
        )

    @property
    def slow_threshold_seconds(self) -> float:
        return self.slow_threshold_ms / 1000.0


_current: TracingConfig = TracingConfig()


def configure_tracing(config: TracingConfig | None = None) -> TracingConfig:
    """Install the active config (defaults to env-derived). Returns it."""
    global _current
    _current = config if config is not None else TracingConfig.from_env()
    return _current


def get_config() -> TracingConfig:
    return _current
