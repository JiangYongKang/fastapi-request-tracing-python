"""基于 contextvars 的链路上下文，跨 asyncio 任务安全传播。

* :data:`_current_trace` 是一个 ``ContextVar``，asyncio 在
  ``create_task`` / TaskGroup 派生任务时会自动拷贝当前上下文快照，
  因此请求处理中派生或延迟执行的任务天然继承同一条链路；
* 不同并发请求运行在不同任务、持有不同的 Context 副本，互不串扰；
* :func:`inherited_trace` 返回一个在新任务中"恢复并在退出时清理"
  链路绑定的上下文管理器，可用于线程池等需要显式传播的场景。
"""

from __future__ import annotations

import contextlib
import os
import uuid
from contextvars import ContextVar, Token
from typing import Iterator, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from rtrace.trace import Trace

_current_trace: ContextVar[Optional["Trace"]] = ContextVar(
    "rtrace_current_trace", default=None
)


def new_trace_id() -> str:
    """生成 128bit 的链路标识（32 个十六进制字符）。"""

    return uuid.uuid4().hex


def new_span_id() -> str:
    """生成阶段标识（16 个十六进制字符）。"""

    return uuid.uuid4().hex[:16]


def bind_trace(trace: "Trace") -> Token[Optional["Trace"]]:
    """把链路绑定到当前上下文，返回重置用的 Token。"""

    return _current_trace.set(trace)


def reset_trace(token: Token[Optional["Trace"]]) -> None:
    """根据 Token 恢复上下文绑定。"""

    _current_trace.reset(token)


def current_trace() -> Optional["Trace"]:
    """获取当前上下文绑定的链路；不存在时返回 None（不抛异常）。"""

    return _current_trace.get()


@contextlib.contextmanager
def inherited_trace(trace: Optional["Trace"] = None) -> Iterator[Optional["Trace"]]:
    """在当前执行上下文中恢复链路绑定，退出时恢复原状。

    用于 ``run_in_executor`` 等不自动拷贝 asyncio 上下文的场景。
    未传入 trace 时使用当前上下文里的链路；都没有则为空转。
    """

    target = trace if trace is not None else current_trace()
    token: Optional[Token[Optional["Trace"]]] = None
    if target is not None:
        token = _current_trace.set(target)
    try:
        yield target
    finally:
        if token is not None:
            _current_trace.reset(token)


def host_name() -> str:
    """返回主机标识，作为 trace_id 之外的辅助日志关联信息。"""

    return os.uname().nodename
