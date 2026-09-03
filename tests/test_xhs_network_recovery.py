from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from playwright.async_api import Error as PlaywrightError
from tenacity import Future, RetryError

from media_platform.xhs.core import (
    XiaoHongShuCrawler,
    XHSNetworkRecoveryTimeout,
)
from media_platform.xhs.exception import DataFetchError


def _retry_error(error: BaseException) -> RetryError:
    return RetryError(Future.construct(3, error, has_exception=True))


def _crawler_with_clock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wait_seconds: float = 10.0,
    minimum_delay: float = 2.0,
    maximum_delay: float = 4.0,
) -> tuple[XiaoHongShuCrawler, object, object, list[float]]:
    crawler = XiaoHongShuCrawler()
    page = SimpleNamespace(
        is_closed=lambda: False,
        url="https://www.xiaohongshu.com/search_result",
    )
    context = SimpleNamespace(pages=[page], new_page=AsyncMock())
    crawler.context_page = page
    crawler.browser_context = context
    crawler._record_network_recovery_event = AsyncMock()

    clock = {"now": 20.0}
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock["now"]

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    crawler._popup_monotonic = monotonic
    crawler._popup_sleep = sleep
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_XHS_NETWORK_WAIT_SECONDS",
        str(wait_seconds),
    )
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MIN_SECONDS",
        str(minimum_delay),
    )
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MAX_SECONDS",
        str(maximum_delay),
    )
    return crawler, context, page, sleeps


@pytest.mark.asyncio
async def test_transport_outage_retries_operation_without_replacing_browser_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, context, page, sleeps = _crawler_with_clock(monkeypatch)
    request = httpx.Request("GET", "https://edith.xiaohongshu.com/api/search")
    calls: list[tuple[object, object]] = []

    async def operation() -> str:
        calls.append((crawler.browser_context, crawler.context_page))
        if len(calls) == 1:
            raise httpx.ReadTimeout("offline", request=request)
        return "restored"

    result = await crawler._run_with_network_recovery(
        operation,
        stage="search:frontier:page=3",
    )

    assert result == "restored"
    assert calls == [(context, page), (context, page)]
    assert sleeps == [2.0]
    context.new_page.assert_not_awaited()
    assert [
        call.kwargs["outcome"]
        for call in crawler._record_network_recovery_event.await_args_list
    ] == ["network_paused", "network_recovered"]


@pytest.mark.asyncio
async def test_retry_error_is_unwrapped_before_same_operation_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, _context, _page, sleeps = _crawler_with_clock(monkeypatch)
    request = httpx.Request("GET", "https://edith.xiaohongshu.com/api/search")
    operation = AsyncMock(
        side_effect=[
            _retry_error(httpx.ConnectError("offline", request=request)),
            {"items": []},
        ]
    )

    result = await crawler._run_with_network_recovery(
        operation,
        stage="search:refresh:page=1",
    )

    assert result == {"items": []}
    assert operation.await_count == 2
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_network_budget_exhaustion_has_exact_backoff_and_keeps_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, context, page, sleeps = _crawler_with_clock(
        monkeypatch,
        wait_seconds=5.0,
    )
    request = httpx.Request("GET", "https://edith.xiaohongshu.com/api/search")
    operation = AsyncMock(
        side_effect=httpx.ConnectError("still offline", request=request)
    )

    with pytest.raises(XHSNetworkRecoveryTimeout) as exc_info:
        await crawler._run_with_network_recovery(
            operation,
            stage="search:frontier:page=9",
        )

    assert exc_info.value.stage == "search:frontier:page=9"
    assert exc_info.value.elapsed_seconds == pytest.approx(5.0)
    assert operation.await_count == 3
    assert sleeps == [2.0, 3.0]
    assert crawler.browser_context is context
    assert crawler.context_page is page
    context.new_page.assert_not_awaited()
    assert [
        call.kwargs["outcome"]
        for call in crawler._record_network_recovery_event.await_args_list
    ] == ["network_paused", "network_paused", "network_recovery_timeout"]


@pytest.mark.asyncio
async def test_context_replacement_during_pause_is_terminal_not_a_second_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, _context, _page, sleeps = _crawler_with_clock(monkeypatch)
    replacement_context = SimpleNamespace(pages=[])
    original_sleep = crawler._popup_sleep

    async def replace_context(seconds: float) -> None:
        await original_sleep(seconds)
        crawler.browser_context = replacement_context

    crawler._popup_sleep = replace_context
    request = httpx.Request("GET", "https://edith.xiaohongshu.com/api/search")
    operation = AsyncMock(
        side_effect=httpx.ReadTimeout("offline", request=request)
    )

    with pytest.raises(PlaywrightError, match="context_replaced"):
        await crawler._run_with_network_recovery(
            operation,
            stage="search:frontier:page=3",
        )

    assert operation.await_count == 1
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_non_transport_request_error_is_never_retried_as_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, _context, _page, sleeps = _crawler_with_clock(monkeypatch)
    operation = AsyncMock(side_effect=DataFetchError("invalid response schema"))

    with pytest.raises(DataFetchError, match="invalid response schema"):
        await crawler._run_with_network_recovery(
            operation,
            stage="search:frontier:page=3",
        )

    assert operation.await_count == 1
    assert sleeps == []
    crawler._record_network_recovery_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_network_wait_does_not_swallow_task_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, _context, _page, sleeps = _crawler_with_clock(monkeypatch)
    operation = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await crawler._run_with_network_recovery(
            operation,
            stage="search:frontier:page=3",
        )

    assert operation.await_count == 1
    assert sleeps == []
    crawler._record_network_recovery_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_network_wait_fails_without_hidden_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, _context, _page, sleeps = _crawler_with_clock(
        monkeypatch,
        wait_seconds=0.0,
    )
    request = httpx.Request("GET", "https://edith.xiaohongshu.com/api/search")
    operation = AsyncMock(
        side_effect=httpx.ReadTimeout("offline", request=request)
    )

    with pytest.raises(XHSNetworkRecoveryTimeout) as exc_info:
        await crawler._run_with_network_recovery(
            operation,
            stage="search:frontier:page=3",
        )

    assert exc_info.value.elapsed_seconds == 0.0
    assert operation.await_count == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_concurrent_recovery_stages_keep_independent_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = XiaoHongShuCrawler()
    page = SimpleNamespace(is_closed=lambda: False)
    crawler.context_page = page
    crawler.browser_context = SimpleNamespace(pages=[page])
    crawler._record_network_recovery_event = AsyncMock()
    crawler._popup_monotonic = time.monotonic
    crawler._popup_sleep = asyncio.sleep
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_WAIT_SECONDS", "0.03")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MIN_SECONDS", "0.01")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MAX_SECONDS", "0.01")
    request = httpx.Request("GET", "https://edith.xiaohongshu.com/api/test")
    calls = {"a": 0, "b": 0}

    async def fail(name: str) -> None:
        calls[name] += 1
        raise httpx.ConnectError(f"offline-{name}", request=request)

    results = await asyncio.wait_for(
        asyncio.gather(
            crawler._run_with_network_recovery(
                lambda: fail("a"),
                stage="note_detail_api:note=a",
            ),
            crawler._run_with_network_recovery(
                lambda: fail("b"),
                stage="note_detail_api:note=b",
            ),
            return_exceptions=True,
        ),
        timeout=0.5,
    )

    assert all(isinstance(result, XHSNetworkRecoveryTimeout) for result in results)
    assert calls == {"a": 2, "b": 2}
    assert all(result.elapsed_seconds >= 0.03 for result in results)


@pytest.mark.asyncio
async def test_transport_then_nontransport_error_is_aborted_not_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, _context, _page, sleeps = _crawler_with_clock(monkeypatch)
    request = httpx.Request("GET", "https://edith.xiaohongshu.com/api/search")
    operation = AsyncMock(
        side_effect=[
            httpx.ReadTimeout("offline", request=request),
            DataFetchError("bad response after reconnect"),
        ]
    )

    with pytest.raises(DataFetchError, match="bad response after reconnect"):
        await crawler._run_with_network_recovery(
            operation,
            stage="search:frontier:page=3",
        )

    assert operation.await_count == 2
    assert sleeps == [2.0]
    assert [
        call.kwargs["outcome"]
        for call in crawler._record_network_recovery_event.await_args_list
    ] == ["network_paused", "network_recovery_aborted"]


@pytest.mark.asyncio
async def test_missing_browser_context_is_terminal_before_network_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = XiaoHongShuCrawler()
    crawler._record_network_recovery_event = AsyncMock()
    crawler._popup_sleep = AsyncMock()
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_WAIT_SECONDS", "10")
    request = httpx.Request("GET", "https://edith.xiaohongshu.com/api/search")
    operation = AsyncMock(
        side_effect=httpx.ConnectError("offline", request=request)
    )

    with pytest.raises(PlaywrightError, match="context_missing"):
        await crawler._run_with_network_recovery(
            operation,
            stage="search:frontier:page=3",
        )

    assert operation.await_count == 1
    crawler._popup_sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("session_fault", ["closed", "removed", "foreign"])
async def test_invalid_operation_page_is_terminal_before_retry(
    monkeypatch: pytest.MonkeyPatch,
    session_fault: str,
) -> None:
    crawler, context, page, sleeps = _crawler_with_clock(monkeypatch)
    if session_fault == "closed":
        page.is_closed = lambda: True
    elif session_fault == "removed":
        context.pages.clear()
    else:
        page.context = SimpleNamespace(pages=[page])
    request = httpx.Request("GET", "https://edith.xiaohongshu.com/api/search")
    operation = AsyncMock(
        side_effect=httpx.ReadTimeout("offline", request=request)
    )

    with pytest.raises(PlaywrightError, match="xhs_network_recovery"):
        await crawler._run_with_network_recovery(
            operation,
            stage="search:frontier:page=3",
        )

    assert operation.await_count == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_creator_network_timeout_never_opens_browser_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = XiaoHongShuCrawler()
    crawler._run_with_network_recovery = AsyncMock(
        side_effect=XHSNetworkRecoveryTimeout("creator_profile_api:user=author", 600)
    )
    crawler._get_creator_info_from_browser = AsyncMock()
    crawler._guarded_pause = AsyncMock()
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_ENRICH_CREATORS", "1")
    note = {"user": {"user_id": "author"}}

    with pytest.raises(XHSNetworkRecoveryTimeout):
        await crawler.enrich_note_creator(note)

    crawler._get_creator_info_from_browser.assert_not_awaited()
    crawler._guarded_pause.assert_not_awaited()
