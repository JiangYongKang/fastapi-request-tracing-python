"""供客户端中断集成测试使用的独立应用（由子进程 exec）。

调用前需在 exec 命名空间中定义 PORT 与 RESULT_FILE。

应用在 receive 到 http.disconnect 后结束请求处理；追踪中间件已在
receive 包装层把链路标记为 CANCELLED，请求处理返回后立即收尾，
exporter 据此把最终状态写入 RESULT_FILE。
"""

import uvicorn

from rtrace.config import TracingConfig
from rtrace.middleware import TracingMiddleware


def _write_status(trace) -> None:
    with open(RESULT_FILE, "w") as f:  # noqa: F821 - exec 注入
        f.write(trace.status.value)


async def hang_app(scope, receive, send):
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return  # 客户端已中断，结束处理；中间件以 CANCELLED 收尾


app = TracingMiddleware(
    hang_app,
    config=TracingConfig(slow_threshold_ms=60_000),
    exporter=_write_status,
)

config = uvicorn.Config(
    app,
    host="127.0.0.1",
    port=PORT,  # noqa: F821 - exec 注入
    log_level="error",
    lifespan="off",
)
uvicorn.Server(config).run()
