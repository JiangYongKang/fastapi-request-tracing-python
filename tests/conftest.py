"""Shared fixtures: deterministic config + clean trace registry per test."""

from __future__ import annotations

import pytest

from tracing import aggregate, records
from tracing.config import TracingConfig, configure_tracing
from tracing.context import set_current_trace


@pytest.fixture(autouse=True)
def tracing_state():
    # guarantee each test starts and ends with no leaked current trace
    token = set_current_trace(None)
    cfg = configure_tracing(
        TracingConfig(
            enabled=True,
            slow_threshold_ms=200.0,
            sample_rate=1.0,
            service_name="test-service",
            emit_logs=True,
        )
    )
    aggregate.reset_registry()
    records.reset()
    yield cfg
    aggregate.reset_registry()
    records.reset()
    configure_tracing(TracingConfig())
    set_current_trace(None)
