from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from media_platform.xhs.core import XiaoHongShuCrawler
from media_platform.xhs.login import XiaoHongShuLogin


class FakePage:
    def __init__(
        self,
        name: str,
        events: list[tuple[str, object]],
        *,
        visible_states: list[str] | None = None,
    ):
        self.name = name
        self.url = f"https://www.xiaohongshu.com/{name}"
        self.closed = False
        self.events = events
        self.visible_states = list(visible_states or [""])

    def is_closed(self) -> bool:
        return self.closed

    async def bring_to_front(self) -> None:
        self.events.append(("front", self.name))

    async def content(self) -> str:
        state = self.visible_states[0]
        if len(self.visible_states) > 1:
            self.visible_states.pop(0)
        return state

    async def close(self) -> None:
        self.events.append(("close", self.name))
        self.closed = True


class FakeContext:
    def __init__(self, pages: list[FakePage], events: list[tuple[str, object]]):
        self.pages = pages
        self.events = events
        self.page_handler = None
        self.closed = False

    def on(self, event: str, handler) -> None:
        assert event == "page"
        self.page_handler = handler

    def emit_page(self, page: FakePage) -> None:
        self.pages.append(page)
        assert self.page_handler is not None
        self.page_handler(page)

    async def new_page(self) -> FakePage:
        page = FakePage("crawler-owned", self.events)
        self.emit_page(page)
        return page

    async def close(self) -> None:
        self.events.append(("context_close", "context"))
        self.closed = True


def install_fake_clock(crawler: XiaoHongShuCrawler, events: list[tuple[str, object]]):
    clock = {"now": 100.0}

    def monotonic() -> float:
        return clock["now"]

    async def sleep(seconds: float) -> None:
        events.append(("sleep", seconds))
        clock["now"] += seconds

    crawler._popup_monotonic = monotonic
    crawler._popup_sleep = sleep


@pytest.mark.asyncio
async def test_unexpected_xhs_tab_waits_30_seconds_before_login_cleanup() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("search", events)
    popup = FakePage("security-popup", events)
    context = FakeContext([primary], events)
    crawler = XiaoHongShuCrawler()
    install_fake_clock(crawler, events)
    crawler.browser_context = context
    crawler.context_page = primary
    crawler._install_new_page_guard()

    context.emit_page(popup)
    selected = await crawler._single_page_for_login()

    assert selected is primary
    assert popup.closed is True
    assert events[0] == ("front", "security-popup")
    assert events[1][0] == "sleep"
    assert events[1][1] == pytest.approx(30.0)
    assert events[2] == ("close", "security-popup")


@pytest.mark.asyncio
async def test_extra_tab_present_at_guard_install_is_also_protected() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("search", events)
    popup = FakePage("startup-security-popup", events)
    context = FakeContext([primary, popup], events)
    crawler = XiaoHongShuCrawler()
    install_fake_clock(crawler, events)
    crawler.browser_context = context
    crawler.context_page = primary

    crawler._install_new_page_guard()
    await crawler._single_page_for_login()

    assert crawler._new_pages[id(popup)][2] == "preexisting_extra"
    assert events == [
        ("front", "startup-security-popup"),
        ("sleep", pytest.approx(30.0)),
        ("close", "startup-security-popup"),
    ]


@pytest.mark.asyncio
async def test_crawler_opened_xhs_tab_waits_30_seconds_after_failed_scroll() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("search", events)
    context = FakeContext([primary], events)
    crawler = XiaoHongShuCrawler()
    install_fake_clock(crawler, events)
    crawler.browser_context = context
    crawler.context_page = primary
    crawler._install_new_page_guard()

    page = await crawler._new_guarded_page()
    events.append(("scroll_effect", False))
    await crawler._close_page_with_deadline(page, reason="creator_profile_cleanup")

    assert crawler._new_pages[id(page)][2] == "crawler_opened"
    scroll_index = events.index(("scroll_effect", False))
    sleep_index = next(index for index, event in enumerate(events) if event[0] == "sleep")
    close_index = events.index(("close", "crawler-owned"))
    assert events[sleep_index][1] == pytest.approx(30.0)
    assert scroll_index < sleep_index < close_index
    assert page.closed is True


@pytest.mark.asyncio
async def test_qr_and_verification_popup_remains_open_until_stably_cleared() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("search", events)
    popup = FakePage(
        "login-popup",
        events,
        visible_states=[
            "扫码登录 二维码",
            "扫码登录 二维码",
            "请输入验证码",
            "首页 发现 消息 我",
            "首页 发现 消息 我",
        ],
    )
    context = FakeContext([primary], events)
    crawler = XiaoHongShuCrawler()
    install_fake_clock(crawler, events)
    crawler.browser_context = context
    crawler.context_page = primary
    crawler._install_new_page_guard()

    context.emit_page(popup)
    selected = await crawler._single_page_for_login()

    assert selected is primary
    assert crawler.browser_context is context
    assert popup.closed is True
    first_close = events.index(("close", "login-popup"))
    first_hold = events.index(("sleep", pytest.approx(30.0)))
    checkpoint_front = events.index(("front", "login-popup"), 1)
    checkpoint_polls = [
        index
        for index, event in enumerate(events)
        if event == ("sleep", pytest.approx(2.0))
    ]
    assert first_hold < checkpoint_front < checkpoint_polls[0]
    assert len(checkpoint_polls) == 3
    assert checkpoint_polls[-1] < first_close


@pytest.mark.asyncio
async def test_manual_checkpoint_timeout_is_600_seconds_then_fails_and_closes() -> None:
    events: list[tuple[str, object]] = []
    page = FakePage(
        "verification",
        events,
        visible_states=["请通过安全验证"],
    )
    context = FakeContext([page], events)
    crawler = XiaoHongShuCrawler()
    install_fake_clock(crawler, events)
    crawler.browser_context = context

    with pytest.raises(
        RuntimeError,
        match=r"xhs_manual_checkpoint_timeout:test_cleanup:600s",
    ):
        await crawler._close_page_with_deadline(page, reason="test_cleanup")

    assert page.closed is True
    assert sum(
        float(event[1]) for event in events if event[0] == "sleep"
    ) == pytest.approx(600.0)
    assert events[-1] == ("close", "verification")


@pytest.mark.asyncio
async def test_manual_timeout_does_not_skip_browser_context_cleanup() -> None:
    events: list[tuple[str, object]] = []
    page = FakePage(
        "verification",
        events,
        visible_states=["请通过安全验证"],
    )
    context = FakeContext([page], events)
    crawler = XiaoHongShuCrawler()
    install_fake_clock(crawler, events)
    crawler.browser_context = context
    crawler.context_page = page

    with pytest.raises(
        RuntimeError,
        match=r"xhs_manual_checkpoint_timeout:browser_shutdown:600s",
    ):
        await crawler.close(force=True)

    assert page.closed is True
    assert context.closed is True
    assert events[-2:] == [
        ("close", "verification"),
        ("context_close", "context"),
    ]


@pytest.mark.asyncio
async def test_rate_limit_popup_fails_immediately_without_30_second_hold() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("search", events)
    popup = FakePage(
        "rate-limit",
        events,
        visible_states=["请稍后重试，当前访问过于频繁"],
    )
    context = FakeContext([primary], events)
    crawler = XiaoHongShuCrawler()
    install_fake_clock(crawler, events)
    crawler.browser_context = context
    crawler.context_page = primary
    crawler._install_new_page_guard()

    context.emit_page(popup)
    with pytest.raises(
        RuntimeError,
        match=r"xhs_rate_limited_during_page_guard:login_tab_normalization",
    ):
        await crawler._single_page_for_login()

    assert popup.closed is True
    assert not [event for event in events if event[0] == "sleep"]


@pytest.mark.asyncio
async def test_browser_shutdown_cannot_bypass_primary_login_checkpoint() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage(
        "login",
        events,
        visible_states=[
            "手机号登录 请输入验证码",
            "手机号登录 请输入验证码",
            "首页 发现 消息 我",
            "首页 发现 消息 我",
        ],
    )
    context = FakeContext([primary], events)
    crawler = XiaoHongShuCrawler()
    install_fake_clock(crawler, events)
    crawler.browser_context = context
    crawler.context_page = primary

    await crawler.close()

    assert crawler.browser_context is context
    assert events[0] == ("front", "login")
    assert events.index(("close", "login")) < events.index(
        ("context_close", "context")
    )
    assert [event for event in events if event[0] == "sleep"] == [
        ("sleep", pytest.approx(2.0)),
        ("sleep", pytest.approx(2.0)),
    ]


@pytest.mark.asyncio
async def test_xhs_context_cleanup_waits_for_unexpected_tab() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("search", events)
    popup = FakePage("captcha-popup", events)
    context = FakeContext([primary], events)
    crawler = XiaoHongShuCrawler()
    install_fake_clock(crawler, events)
    crawler.browser_context = context
    crawler.context_page = primary
    crawler._install_new_page_guard()

    context.emit_page(popup)
    await crawler.close()

    sleep_index = next(index for index, event in enumerate(events) if event[0] == "sleep")
    close_index = events.index(("context_close", "context"))
    assert events[sleep_index][1] == pytest.approx(30.0)
    assert sleep_index < close_index
    assert events.index(("close", "captcha-popup")) < close_index


@pytest.mark.asyncio
async def test_xhs_behavior_adopts_replacement_page_after_target_closed(
    monkeypatch,
) -> None:
    events: list[tuple[str, object]] = []
    original = FakePage("search", events)
    replacement = FakePage("replacement", events)
    context = FakeContext([original, replacement], events)
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = context
    crawler.context_page = original
    original.closed = True
    calls = []

    async def run_behavior(page, platform_key):
        calls.append(page)
        if page is original:
            raise RuntimeError(
                "human_behavior_failed:xhs:TargetClosedError: "
                "Target page, context or browser has been closed"
            )
        return {"status": "completed"}

    class FakeClient:
        def __init__(self) -> None:
            self.playwright_page = original
            self.updated = 0

        async def update_cookies(self, *, browser_context, urls) -> None:
            assert browser_context is context
            self.updated += 1

        async def pong(self) -> bool:
            return True

    monkeypatch.setattr(
        "media_platform.xhs.core.run_required_human_behavior",
        run_behavior,
    )
    crawler.xhs_client = FakeClient()
    crawler._open_behavior_search_page = AsyncMock()

    evidence = await crawler._run_human_behavior_with_page_recovery("青岛旅游")

    assert evidence["status"] == "completed"
    assert calls == [original, replacement]
    assert crawler.context_page is replacement
    assert crawler.xhs_client.playwright_page is replacement
    crawler._open_behavior_search_page.assert_awaited_once_with("青岛旅游")


@pytest.mark.asyncio
async def test_standalone_xhs_login_preserves_extra_verification_tab() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("login", events)
    popup = FakePage(
        "verification",
        events,
        visible_states=["请输入验证码"],
    )
    context = FakeContext([primary, popup], events)
    login = XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=context,
        context_page=primary,
    )

    selected = await login._single_login_page()

    assert selected is primary
    assert context.pages == [primary, popup]
    assert popup.closed is False
    assert events == []


@pytest.mark.asyncio
async def test_standalone_xhs_login_stops_on_platform_security_limit() -> None:
    class SecurityLimitPage:
        url = "https://www.xiaohongshu.com/website-login/error"

        async def is_visible(self, selector: str, timeout: int) -> bool:
            return True

        async def content(self) -> str:
            return ""

    login = XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=object(),
        context_page=SecurityLimitPage(),
    )
    login._single_login_page = AsyncMock(return_value=login.context_page)

    with pytest.raises(RuntimeError, match="xhs_platform_security_limit_300011"):
        await login._check_login_state_once("")


@pytest.mark.asyncio
async def test_crawler_checkpoint_markers_detect_url_only_security_limit() -> None:
    class SecurityLimitPage:
        url = "https://www.xiaohongshu.com/website-login/error?redirectPath=%2Fexplore"

        def is_closed(self) -> bool:
            return False

        async def content(self) -> str:
            return ""

    class Context:
        pages = [SecurityLimitPage()]

    crawler = XiaoHongShuCrawler()
    crawler.browser_context = Context()
    crawler.context_page = Context.pages[0]

    markers = await crawler._visible_checkpoint_markers()

    assert markers["security"] == ["website-login/error"]
    assert markers["pages"][0]["url"] == Context.pages[0].url


@pytest.mark.asyncio
async def test_xhs_shutdown_does_not_write_independent_storage_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[tuple[str, object]] = []
    page = FakePage("search", events)
    context = FakeContext([page], events)
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = context
    legacy_snapshot = tmp_path / "storage-state.json"
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH",
        str(legacy_snapshot),
    )

    await crawler._prepare_browser_shutdown()

    assert events[0] == ("close", "search")
    assert not legacy_snapshot.exists()
    assert not hasattr(crawler, "_storage_state_path")
    assert not hasattr(crawler, "_restore_storage_state")
    assert not hasattr(crawler, "_write_storage_state")


@pytest.mark.asyncio
async def test_xhs_login_and_replacement_pages_share_one_browser_context(
) -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("login", events)
    replacement = FakePage("verification", events)
    context = FakeContext([primary, replacement], events)
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = context
    crawler.context_page = replacement
    login = XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=crawler.browser_context,
        context_page=crawler.context_page,
    )

    assert login.browser_context is context
    assert login.context_page is replacement
    assert crawler.context_page in crawler.browser_context.pages


@pytest.mark.asyncio
async def test_cdp_launch_failure_cleans_once_without_standard_fallback(
    monkeypatch,
) -> None:
    cleanup_calls: list[bool] = []

    class FailingManager:
        async def launch_and_connect(self, **kwargs):
            await self.cleanup(force=True)
            raise RuntimeError("cdp connect failed")

        async def cleanup(self, force: bool = False) -> None:
            cleanup_calls.append(force)

    chromium = SimpleNamespace(
        launch=AsyncMock(),
        launch_persistent_context=AsyncMock(),
    )
    playwright = SimpleNamespace(chromium=chromium)
    crawler = XiaoHongShuCrawler()
    crawler.launch_browser = AsyncMock()
    monkeypatch.setattr(
        "media_platform.xhs.core.CDPBrowserManager",
        FailingManager,
    )

    with pytest.raises(
        RuntimeError,
        match=r"^xhs_cdp_browser_launch_failed:RuntimeError: cdp connect failed$",
    ):
        await crawler.launch_browser_with_cdp(
            playwright,
            None,
            None,
            headless=False,
        )

    assert cleanup_calls == [True]
    assert crawler.cdp_manager is None
    crawler.launch_browser.assert_not_awaited()
    chromium.launch.assert_not_awaited()
    chromium.launch_persistent_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_session_cannot_be_started_twice_after_failed_attempt(
    monkeypatch,
) -> None:
    crawler = XiaoHongShuCrawler()
    crawler.launch_browser_with_cdp = AsyncMock(
        side_effect=RuntimeError("first launch failed")
    )
    monkeypatch.setattr("media_platform.xhs.core.config.ENABLE_CDP_MODE", True)
    playwright = SimpleNamespace(chromium=SimpleNamespace())

    with pytest.raises(RuntimeError, match="first launch failed"):
        await crawler._run_browser_session(playwright, None, None)

    with pytest.raises(RuntimeError, match="^xhs_browser_session_already_started$"):
        await crawler._run_browser_session(playwright, None, None)

    assert crawler._browser_session_started is True
    crawler.launch_browser_with_cdp.assert_awaited_once()


@pytest.mark.asyncio
async def test_replacement_page_from_another_context_is_rejected() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("search", events)
    replacement = FakePage("verification", events)
    active_context = FakeContext([primary, replacement], events)
    foreign_context = FakeContext([], events)
    replacement.context = foreign_context
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = active_context
    crawler._browser_session_context = active_context
    crawler.context_page = primary

    with pytest.raises(
        RuntimeError,
        match=r"^xhs_page_context_mismatch:replacement_page_adoption$",
    ):
        await crawler._activate_latest_xhs_page()

    assert crawler.context_page is primary
    assert active_context.pages == [primary, replacement]


def test_active_browser_context_cannot_be_replaced_within_session() -> None:
    events: list[tuple[str, object]] = []
    original_context = FakeContext([], events)
    replacement = FakePage("verification", events)
    replacement_context = FakeContext([replacement], events)
    crawler = XiaoHongShuCrawler()
    crawler._browser_session_context = original_context
    crawler.browser_context = replacement_context

    with pytest.raises(
        RuntimeError,
        match=r"^xhs_browser_context_replaced:test$",
    ):
        crawler._assert_page_in_active_browser_context(replacement, stage="test")
