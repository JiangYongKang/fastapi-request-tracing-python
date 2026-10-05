"""Request tracing and slow-request diagnostics (skeleton)."""

from . import records
from .config import TracingConfig, configure_tracing, get_config
from .context import TraceContext, get_current_trace, new_trace
from .records import DiagnosticRecord, DiagnosticStore
from .stages import record_stage, stage
from .tasks import trace_as_current, traced_background, traced_task
from .middleware import TraceMiddleware
from .sampling import Sampler

__all__ = [
    "TracingConfig",
    "configure_tracing",
    "get_config",
    "TraceContext",
    "get_current_trace",
    "new_trace",
    "record_stage",
    "stage",
    "trace_as_current",
    "traced_background",
    "traced_task",
    "TraceMiddleware",
    "Sampler",
    "DiagnosticRecord",
    "DiagnosticStore",
    "records",
]
