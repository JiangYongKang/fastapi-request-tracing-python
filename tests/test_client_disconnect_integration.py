"""真实网络下的客户端中断集成测试（uvicorn 子进程 + 原始 TCP 连接）。

* 客户端在收到响应前断开连接；
* 服务器 receive 到 http.disconnect；
* 中间件必须把链路判定为 CANCELLED 并完成收尾（exporter 落盘结论）。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

APP_FILE = Path(__file__).parent / "_disconnect_app.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_file(path: Path, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.asyncio
async def test_real_client_disconnect_marks_cancelled(tmp_path):
    result_file = tmp_path / "result.txt"
    port = _free_port()
    code = (
        f"PORT = {port}\n"
        f"RESULT_FILE = {str(result_file)!r}\n"
        f"exec(open({str(APP_FILE)!r}).read())\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        ready = False
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError):
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    ready = True
                    break
            await asyncio.sleep(0.1)
        assert ready, "server did not start"

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /hang HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
        await writer.drain()
        await asyncio.sleep(0.3)
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()

        assert await _wait_file(result_file), "trace was not finalized in time"
        assert result_file.read_text().strip() == "cancelled"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
