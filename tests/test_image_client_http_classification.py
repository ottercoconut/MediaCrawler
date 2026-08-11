from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from media_platform.douyin import client as douyin_client
from media_platform.weibo import client as weibo_client
from media_platform.xhs import client as xhs_client
from media_platform.xhs.exception import IPBlockError, PlatformRuntimeError
from media_platform.zhihu import client as zhihu_client
from tools.image_download_retry import ImageDownloadFetchError


def async_client_factory(status_code: int, content: bytes = b""):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=content, request=request)

    return lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_douyin_http_404_preserves_terminal_status(monkeypatch) -> None:
    monkeypatch.setattr(
        douyin_client, "make_async_client", async_client_factory(404)
    )
    client = object.__new__(douyin_client.DouYinClient)
    client.proxy = None
    client.timeout = 1

    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_aweme_media("https://example.test/missing.jpg")

    assert exc_info.value.code == "image_source_unavailable"
    assert exc_info.value.http_status == 404


@pytest.mark.asyncio
async def test_weibo_http_404_preserves_terminal_status(monkeypatch) -> None:
    monkeypatch.setattr(weibo_client, "make_async_client", async_client_factory(404))
    client = object.__new__(weibo_client.WeiboClient)
    client.proxy = None
    client.timeout = 1
    client._image_agent_host = "https://i1.wp.com/"

    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_note_image("https://wx.test/missing.jpg")

    assert exc_info.value.code == "image_source_unavailable"
    assert exc_info.value.http_status == 404


@pytest.mark.asyncio
async def test_xhs_http_404_preserves_terminal_status(monkeypatch) -> None:
    monkeypatch.setattr(xhs_client, "make_async_client", async_client_factory(404))
    client = object.__new__(xhs_client.XiaoHongShuClient)
    client.proxy = None
    client.timeout = 1
    client._refresh_proxy_if_expired = AsyncMock(return_value=None)

    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_note_media("https://example.test/missing.jpg")

    assert exc_info.value.code == "image_source_unavailable"
    assert exc_info.value.http_status == 404


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "login_required"), (403, "login_required"), (429, "rate_limited")],
)
@pytest.mark.asyncio
async def test_xhs_html_response_preserves_run_level_http_status(
    monkeypatch,
    status,
    code,
) -> None:
    monkeypatch.setattr(xhs_client, "make_async_client", async_client_factory(status))
    client = object.__new__(xhs_client.XiaoHongShuClient)
    client.proxy = None
    client.timeout = 1
    client._refresh_proxy_if_expired = AsyncMock(return_value=None)

    with pytest.raises(PlatformRuntimeError) as exc_info:
        await client.request(
            "GET",
            "https://www.xiaohongshu.com/user/profile/test",
            return_response=True,
        )

    assert exc_info.value.code == code


@pytest.mark.asyncio
async def test_xhs_html_response_preserves_ip_block_business_code(monkeypatch) -> None:
    payload = b'{"success":false,"code":300012,"msg":"blocked"}'
    monkeypatch.setattr(
        xhs_client,
        "make_async_client",
        async_client_factory(200, payload),
    )
    client = object.__new__(xhs_client.XiaoHongShuClient)
    client.proxy = None
    client.timeout = 1
    client._refresh_proxy_if_expired = AsyncMock(return_value=None)
    client.IP_ERROR_CODE = 300012
    client.IP_ERROR_STR = "Network connection error, code 300012"

    with pytest.raises(IPBlockError):
        await client.request(
            "GET",
            "https://www.xiaohongshu.com/explore/test",
            return_response=True,
        )


@pytest.mark.asyncio
async def test_zhihu_http_404_preserves_terminal_status(monkeypatch) -> None:
    monkeypatch.setattr(zhihu_client, "make_async_client", async_client_factory(404))
    client = object.__new__(zhihu_client.ZhiHuClient)
    client.proxy = None
    client.timeout = 1
    client.default_headers = {}

    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_content_image(
            "https://example.test/missing.jpg",
            referer="https://www.zhihu.com/question/1",
        )

    assert exc_info.value.code == "image_source_unavailable"
    assert exc_info.value.http_status == 404


@pytest.mark.asyncio
async def test_zhihu_oversized_response_is_terminal(monkeypatch) -> None:
    monkeypatch.setattr(zhihu_client, "IMAGE_DOWNLOAD_MAX_BYTES", 3)
    monkeypatch.setattr(
        zhihu_client,
        "make_async_client",
        async_client_factory(200, content=b"four"),
    )
    client = object.__new__(zhihu_client.ZhiHuClient)
    client.proxy = None
    client.timeout = 1
    client.default_headers = {}

    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_content_image(
            "https://example.test/large.jpg",
            referer="https://www.zhihu.com/question/1",
        )

    assert exc_info.value.code == "image_too_large"
    assert exc_info.value.retryable is False
    assert exc_info.value.http_status == 200
