"""阶段计时辅助工具。

业务代码用它为当前请求链路增加分阶段耗时；无活动链路时安全空转，
绝不能因为追踪本身影响业务逻辑。
"""

from __future__ import annotations

import contextlib
from typing import AsyncIterator, Optional

from rtrace.context import current_trace
from rtrace.span import Span


@contextlib.asynccontextmanager
async def timed_operation(name: str) -> AsyncIterator[Optional[Span]]:
    """为当前链路记录一个命名阶段的耗时。

    阶段内抛出异常时该阶段会被标记为错误，异常原样向上抛出。
    若当前上下文没有活动链路（如离线脚本），则什么也不做。
    """

    trace = current_trace()
    if trace is None:
        yield None
        return
    span = trace.start_span(name)
    try:
        yield span
    except BaseException:
        span.mark_error()
        raise
    finally:
        span.finish()
