"""Pure-ASGI tracing middleware.

Responsibilities:

* create one trace per HTTP request (honouring an inbound trace-id header),
  bind it to a ``ContextVar`` so the whole call chain — including spawned
  tasks — inherits it;
* distinguish endings: normal (status code), unhandled exception, timeout,
  client cancellation; the trace is always finalized exactly once;
* classify 5xx responses as error traces, everything else as ok;
* deterministic sampling + one structured log line per request;
* fault isolation: any failure inside the tracing machinery is swallowed and
  the request proceeds untouched — tracing never changes business results
  or status codes.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from . import aggregate
from .config import TracingConfig, get_config
from .context import (
    TraceContext,
    TraceStatus,
    new_trace,
    reset_current_trace,
    set_current_trace,
)
from .exceptions import classify_exception
from .logging_setup import log_trace
from .sampling import Sampler

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class TraceMiddleware:
    def __init__(self, app: Any, config: TracingConfig | None = None) -> None:
        self.app = app
        self.config = config  # None -> resolved per-request from get_config()
        self._sampler: Sampler | None = None

    # -- helpers ---------------------------------------------------------

    def _cfg(self) -> TracingConfig:
        return self.config if self.config is not None else get_config()

    def _sampler_for(self, cfg: TracingConfig) -> Sampler:
        if self.config is not None:
            if self._sampler is None or self._sampler.rate != cfg.sample_rate:
                self._sampler = Sampler(cfg.sample_rate)
            return self._sampler
        return Sampler(cfg.sample_rate)

    @staticmethod
    def _extract_trace_id(scope: Scope, header_name: str) -> str | None:
        wanted = header_name.lower().encode("latin-1")
        for name, value in scope.get("headers", []):
            if name.lower() == wanted:
                try:
                    candidate = value.decode("latin-1").strip()
                except Exception:
                    return None
                return candidate or None
        return None

    def _finalize_and_report(
        self, trace: TraceContext, status: TraceStatus, cfg: TracingConfig
    ) -> None:
        """Finalize once, then sample/register/log. Never raises."""
        try:
            trace.finalize(status)
            sampler = self._sampler_for(cfg)
            sampled = sampler.should_sample(
                trace.trace_id,
                is_slow=trace.is_slow,
                is_failed=trace.status is not TraceStatus.OK,
            )
            aggregate.register(trace)
            if cfg.emit_logs and sampled:
                log_trace(trace, sampled=sampled)
        except Exception:
            return

    async def _send_json(
        self,
        send: Send,
        status: int,
        payload: dict,
        header_name: str,
        trace_id: str,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    (header_name.lower().encode("latin-1"), trace_id.encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    # -- ASGI entrypoint ---------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        cfg = self._cfg()
        if scope.get("type") != "http" or not cfg.enabled:
            await self.app(scope, receive, send)
            return

        # Tracing setup is isolated: if it fails, the request runs untraced.
        try:
            trace_id = self._extract_trace_id(scope, cfg.header_name)
            trace = new_trace(trace_id, cfg)
        except Exception:
            await self.app(scope, receive, send)
            return

        status_code: list[int] = []
        response_started = False
        header_bytes = cfg.header_name.lower().encode("latin-1")
        trace_id_bytes = trace.trace_id.encode("latin-1")

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
                status_code.append(int(message.get("status", 0)))
                headers = list(message.get("headers", []))
                headers.append((header_bytes, trace_id_bytes))
                message = {**message, "headers": headers}
            await send(message)

        token = set_current_trace(trace)
        try:
            await self.app(scope, receive, send_wrapper)
        except asyncio.CancelledError:
            self._finalize_and_report(trace, TraceStatus.CANCELLED, cfg)
            raise
        except asyncio.TimeoutError:
            self._finalize_and_report(trace, TraceStatus.TIMEOUT, cfg)
            if not response_started:
                await self._send_json(
                    send, 504, {"detail": "Request timeout"}, cfg.header_name, trace.trace_id
                )
            # if the response already started, the timeout is only recorded
        except Exception as exc:  # noqa: BLE001 - boundary handler
            self._finalize_and_report(trace, classify_exception(exc), cfg)
            if response_started:
                raise
            await self._send_json(
                send, 500, {"detail": "Internal Server Error"}, cfg.header_name, trace.trace_id
            )
        else:
            code = status_code[0] if status_code else 200
            status = TraceStatus.ERROR if code >= 500 else TraceStatus.OK
            self._finalize_and_report(trace, status, cfg)
        finally:
            reset_current_trace(token)
