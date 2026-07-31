from __future__ import annotations

import pytest

from media_platform.xhs.core import XiaoHongShuCrawler
from media_platform.xhs.login import XiaoHongShuLogin


class FakePage:
    def __init__(self, name: str, events: list[tuple[str, object]]):
        self.name = name
        self.url = f"https://www.xiaohongshu.com/{name}"
        self.closed = False
        self.events = events

    def is_closed(self) -> bool:
        return self.closed

    async def bring_to_front(self) -> None:
        self.events.append(("front", self.name))

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
    crawler._install_unexpected_page_guard()

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

    crawler._install_unexpected_page_guard()
    await crawler._single_page_for_login()

    assert id(popup) in crawler._unexpected_pages
    assert events == [
        ("front", "startup-security-popup"),
        ("sleep", pytest.approx(30.0)),
        ("close", "startup-security-popup"),
    ]


@pytest.mark.asyncio
async def test_crawler_owned_xhs_tab_does_not_use_popup_hold() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("search", events)
    context = FakeContext([primary], events)
    crawler = XiaoHongShuCrawler()
    install_fake_clock(crawler, events)
    crawler.browser_context = context
    crawler.context_page = primary
    crawler._install_unexpected_page_guard()

    page = await crawler._new_owned_page()
    await crawler._close_page_with_deadline(page, reason="test_cleanup")

    assert crawler._owned_pages[id(page)] is page
    assert id(page) not in crawler._unexpected_pages
    assert ("sleep", 30.0) not in events
    assert page.closed is True


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
    crawler._install_unexpected_page_guard()

    context.emit_page(popup)
    await crawler.close()

    sleep_index = next(index for index, event in enumerate(events) if event[0] == "sleep")
    close_index = events.index(("context_close", "context"))
    assert events[sleep_index][1] == pytest.approx(30.0)
    assert sleep_index < close_index
    assert events.index(("close", "captcha-popup")) < close_index


@pytest.mark.asyncio
async def test_standalone_xhs_login_waits_before_closing_extra_tab(
    monkeypatch,
) -> None:
    events: list[tuple[str, object]] = []

    async def sleep(seconds: float) -> None:
        events.append(("sleep", seconds))

    monkeypatch.setattr("media_platform.xhs.login.asyncio.sleep", sleep)
    primary = FakePage("login", events)
    popup = FakePage("verification", events)
    context = FakeContext([primary, popup], events)
    login = XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=context,
        context_page=primary,
    )

    selected = await login._single_login_page()

    assert selected is primary
    assert events == [
        ("front", "verification"),
        ("sleep", 30),
        ("close", "verification"),
    ]
