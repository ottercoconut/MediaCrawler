from unittest.mock import AsyncMock, MagicMock

import pytest

from media_platform.weibo import client as weibo_client
from media_platform.weibo.client import WeiboClient


class _Response:
    def json(self):
        return {
            "ok": 0,
            "msg": "这里还没有内容",
            "data": {"cards": []},
        }


class _AsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, *args, **kwargs):
        return _Response()


@pytest.mark.asyncio
async def test_search_empty_message_is_returned_as_empty_page(monkeypatch):
    monkeypatch.setattr(
        weibo_client,
        "make_async_client",
        lambda **kwargs: _AsyncClient(),
    )
    crawler_client = WeiboClient(
        headers={},
        playwright_page=MagicMock(),
        cookie_dict={},
    )
    crawler_client._refresh_proxy_if_expired = AsyncMock(return_value=None)

    result = await crawler_client.request(
        method="GET",
        url="https://m.weibo.cn/api/container/getIndex",
        allow_empty_search=True,
    )

    assert result == {"cards": []}
