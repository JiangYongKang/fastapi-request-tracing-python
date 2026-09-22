"""骨架冒烟测试：应用可导入、接口行为保持原样。"""

import httpx
import pytest

from main import app


@pytest.mark.asyncio
async def test_root_unchanged():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"Hello": "World"}
