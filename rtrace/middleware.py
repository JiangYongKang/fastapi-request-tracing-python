"""ASGI 追踪中间件：贯穿请求全生命周期并保证收尾。

设计要点：

* 纯 ASGI 中间件（不依赖 BaseHTTPMiddleware 的子任务结构），
  追踪逻辑与下游应用运行在同一个任务里，``CancelledError`` 能在本层
  直接被感知，客户端中断时也能以 CANCELLED 状态可靠收尾；
* 链路通过 ``ContextVar`` 绑定，下游 ``asyncio.create_task`` /
  TaskGroup 派生及 BackgroundTasks 延迟执行的任务自动继承同一条链路；
* 追踪组件自身（采样器、exporter、日志）的任何异常都会被吞并，
  绝不改变业务响应体或状态码；
* 无论成功、业务异常、超时还是取消，链路恰好结束一次，且中间件返回后
  当前任务的上下文恢复原状（不遗留未收尾/未清理的链路）。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rtrace.config import TracingConfig
from rtrace.context import bind_trace, reset_trace
from rtrace.sampling import Sampler
from rtrace.trace import Trace, TraceStatus
from rtrace.logging import get_logger, log_trace_summary

Exporter = Callable[[Trace], Any]


class TracingMiddleware:
    """请求追踪中间件。"""

    def __init__(
        self,
        app: ASGIApp,
        config: Optional[TracingConfig] = None,
        exporter: Optional[Exporter] = None,
        sampler: Optional[Sampler] = None,
    ) -> None:
        self.app = app
        self.config = config or TracingConfig()
        # sampler 优先使用显式注入；否则按配置构建，保证可复现
        self.sampler = sampler or Sampler(
            rate=self.config.sample_rate,
            error_rate=self.config.sample_error_rate,
        )
        self.exporter = exporter
        self.logger = get_logger("middleware")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.config.enabled:
            await self.app(scope, receive, send)
            return

        trace = Trace(
            method=scope.get("method", ""),
            path=_path_of(scope),
            config=self.config,
        )
        token = bind_trace(trace)
        status_code: Optional[int] = None
        app_error: Optional[BaseException] = None
        response_started = False

        async def receive_wrapper() -> Message:
            # 客户端在响应发出前断开：这是生产中"客户端中断"最可靠的信号。
            # 服务器关闭时任务被 cancel() 往往来不及执行 finally，
            # 因此在收到 http.disconnect 的第一时间就标记 CANCELLED 终局。
            message = await receive()
            if (
                message.get("type") == "http.disconnect"
                and not response_started
                and trace.status is not TraceStatus.CANCELLED
            ):
                trace.status = TraceStatus.CANCELLED
                if trace.error_type is None:
                    trace.error_type = "ClientDisconnect"
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message.get("status")
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except BaseException as exc:  # noqa: BLE001 - 必须区分取消并保证收尾
            app_error = exc
            try:
                trace.mark_error(exc)
            except Exception:  # pragma: no cover - 防御性
                self.logger.exception({"event": "trace_mark_error_failed"})
            raise
        finally:
            # Starlette 的 ExceptionMiddleware 已把未处理异常转换为 5xx
            # 响应（异常不会抛出本层），因此据状态码补齐 ERROR 结论。
            # 取消/超时已经是终局，不允许被 5xx 覆盖。
            if (
                app_error is None
                and status_code is not None
                and status_code >= 500
                and trace.status not in (TraceStatus.CANCELLED, TraceStatus.TIMEOUT)
            ):
                trace.status = TraceStatus.ERROR
                if trace.error_type is None:
                    trace.error_type = "UnhandledServerError"
            final_status = self._finalize(trace, status_code, app_error)
            sampled = self._decide_sampling(trace, final_status)
            await self._emit(trace, sampled)
            try:
                reset_trace(token)
            except Exception:  # pragma: no cover - 防御性
                self.logger.exception({"event": "trace_context_reset_failed"})

    # ---- 内部步骤（单独拆开便于单测与隔离故障） ------------------------

    def _finalize(
        self,
        trace: Trace,
        status_code: Optional[int],
        app_error: Optional[BaseException],
    ) -> TraceStatus:
        try:
            return trace.finish(status_code)
        except Exception:  # pragma: no cover - 防御性
            self.logger.exception({"event": "trace_finalize_failed"})
            return trace.status

    def _decide_sampling(self, trace: Trace, status: TraceStatus) -> bool:
        try:
            return self.sampler.should_sample(
                trace.trace_id, is_error=status.is_failure
            )
        except Exception:  # pragma: no cover - 防御性
            return True  # 采样器故障时宁多勿漏，但不能影响业务

    async def _emit(self, trace: Trace, sampled: bool) -> None:
        # 先写日志：单测/本地排障时无论是否采样都能看到链路结论与耗时依据
        try:
            log_trace_summary(trace, sampled, logger=self.logger)
        except Exception:  # pragma: no cover - 防御性
            pass
        if not sampled or self.exporter is None:
            return
        try:
            result = self.exporter(trace)
            if hasattr(result, "__await__"):
                await result  # type: ignore[misc]
        except Exception:
            # 追踪组件自身故障绝不能改变业务结果
            self.logger.exception(
                {"event": "trace_exporter_failed", "trace_id": trace.trace_id}
            )


def _path_of(scope: Scope) -> str:
    path = scope.get("path", "")
    root_path = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        path = path[len(root_path) :]
    return path
