"""pytest 公共夹具与日志捕获（单测日志打印链路标识与阶段耗时依据）。"""

from __future__ import annotations

import logging

import pytest

from rtrace.logging import configure_logging


@pytest.fixture(autouse=True)
def _rtrace_log_capture(caplog):
    """让所有测试都捕获 rtrace 日志，便于断言 trace_id 与阶段耗时。"""

    configure_logging()
    caplog.set_level(logging.DEBUG, logger="rtrace")
    yield
