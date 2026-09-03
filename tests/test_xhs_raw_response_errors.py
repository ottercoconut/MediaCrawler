# -*- coding: utf-8 -*-

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from tenacity import Future, RetryError

from media_platform.xhs.client import (
    XiaoHongShuClient,
    is_recoverable_xhs_transport_failure,
)
from media_platform.xhs.exception import (
    DataFetchError,
    IPBlockError,
    PlatformRuntimeError,
)


class FakeAsyncClient:
    def __init__(self, request_impl):
        self.request_impl = request_impl

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def request(self, *args, **kwargs):
        return await self.request_impl(*args, **kwargs)

    async def get(self, *args, **kwargs):
        return await self.request_impl("GET", *args, **kwargs)


def make_client():
    client = XiaoHongShuClient(
        headers={"Cookie": "web_session=test; a1=test"},
        playwright_page=object(),
        cookie_dict={},
    )
    client._refresh_proxy_if_expired = AsyncMock()
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "failure_code"),
    [(401, "login_required"), (403, "login_required"), (429, "rate_limited")],
)
async def test_raw_response_rejects_access_http_status(
    monkeypatch, status_code, failure_code
):
    calls = 0

    async def request_impl(method, url, **kwargs):
        nonlocal calls
        calls += 1
        return httpx.Response(
            status_code,
            text="blocked",
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(PlatformRuntimeError) as exc_info:
        await make_client().request(
            "GET", "https://www.xiaohongshu.com/user/profile/test", return_response=True
        )

    assert exc_info.value.code == failure_code
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_exception", "failure_code", "return_response"),
    [
        (300011, PlatformRuntimeError, "platform_security_limit_300011", True),
        ("300011", PlatformRuntimeError, "platform_security_limit_300011", False),
        (300012, IPBlockError, None, True),
        ("300012", IPBlockError, None, False),
    ],
)
async def test_raw_response_rejects_known_business_block(
    monkeypatch, code, expected_exception, failure_code, return_response
):
    calls = 0

    async def request_impl(method, url, **kwargs):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"success": False, "code": code, "msg": "blocked"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(expected_exception) as exc_info:
        await make_client().request(
            "GET",
            "https://www.xiaohongshu.com/explore/test",
            return_response=return_response,
        )

    if failure_code is not None:
        assert exc_info.value.code == failure_code
    assert calls == 1


@pytest.mark.asyncio
async def test_raw_response_keeps_successful_html(monkeypatch):
    async def request_impl(method, url, **kwargs):
        return httpx.Response(
            200,
            text="<html>ok</html>",
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    result = await make_client().request(
        "GET", "https://www.xiaohongshu.com/explore/test", return_response=True
    )

    assert result == "<html>ok</html>"


@pytest.mark.asyncio
async def test_successful_json_keeps_raw_and_parsed_return_modes(monkeypatch):
    calls = 0

    async def request_impl(method, url, **kwargs):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"success": True, "data": {"id": "test"}},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )
    client = make_client()

    raw_result = await client.request(
        "GET", "https://www.xiaohongshu.com/explore/test", return_response=True
    )
    parsed_result = await client.request(
        "GET", "https://edith.xiaohongshu.com/api/test"
    )

    assert json.loads(raw_result) == {"success": True, "data": {"id": "test"}}
    assert parsed_result == {"id": "test"}
    assert calls == 2


@pytest.mark.asyncio
async def test_html_detail_does_not_multiply_transport_retries(monkeypatch):
    calls = 0

    async def request_impl(method, url, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=httpx.Request(method, url))

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(RetryError):
        await make_client().get_note_by_id_from_html(
            "test", xsec_source="pc_search", xsec_token="token"
        )

    assert calls == 3


@pytest.mark.asyncio
async def test_html_detail_still_retries_parse_failures():
    client = make_client()
    client.request = AsyncMock(return_value="<html>incomplete</html>")
    client._extractor.extract_note_detail_from_html = Mock(
        side_effect=ValueError("incomplete initial state")
    )

    with pytest.raises(RetryError):
        await client.get_note_by_id_from_html(
            "test", xsec_source="pc_search", xsec_token="token"
        )

    assert client.request.await_count == 3
    assert client._extractor.extract_note_detail_from_html.call_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_pong_returns_false_only_for_explicit_unauthenticated_status(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    async def request_impl(method, url, **kwargs):
        return httpx.Response(
            status_code,
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    assert await make_client().pong() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
async def test_pong_propagates_temporary_transport_outage(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[httpx.RequestError],
) -> None:
    async def request_impl(method, url, **kwargs):
        raise error_type("network unavailable", request=httpx.Request(method, url))

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(error_type, match="network unavailable"):
        await make_client().pong()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (429, "rate_limited"),
        (461, "verification_required"),
        (471, "verification_required"),
    ],
)
async def test_pong_does_not_turn_platform_checkpoint_into_logged_out(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_code: str,
) -> None:
    async def request_impl(method, url, **kwargs):
        return httpx.Response(
            status_code,
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(PlatformRuntimeError) as exc_info:
        await make_client().pong()

    assert exc_info.value.code == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body_code", "error_type"),
    [(300011, PlatformRuntimeError), (300012, IPBlockError)],
)
async def test_pong_preserves_business_block_from_successful_http_response(
    monkeypatch: pytest.MonkeyPatch,
    body_code: int,
    error_type: type[Exception],
) -> None:
    async def request_impl(method, url, **kwargs):
        return httpx.Response(
            200,
            json={"success": False, "code": body_code},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(error_type):
        await make_client().pong()


def test_transport_classifier_unwraps_retry_error_without_broad_request_errors() -> None:
    request = httpx.Request("GET", "https://edith.xiaohongshu.com/api/test")
    retry_timeout = RetryError(
        Future.construct(
            3,
            httpx.ReadTimeout("offline", request=request),
            has_exception=True,
        )
    )

    assert is_recoverable_xhs_transport_failure(retry_timeout) is True
    assert is_recoverable_xhs_transport_failure(
        httpx.RemoteProtocolError("peer reset", request=request)
    ) is True
    assert is_recoverable_xhs_transport_failure(
        httpx.LocalProtocolError("invalid request", request=request)
    ) is False
    assert is_recoverable_xhs_transport_failure(DataFetchError("bad payload")) is False
    assert is_recoverable_xhs_transport_failure(
        PlatformRuntimeError("HTTP 429", code="rate_limited")
    ) is False


@pytest.mark.asyncio
async def test_pong_does_not_swallow_cancellation() -> None:
    client = make_client()
    client.query_self = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await client.pong()
