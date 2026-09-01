from __future__ import annotations

import json
from unittest.mock import AsyncMock

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
    crawler._write_storage_state = AsyncMock()

    evidence = await crawler._run_human_behavior_with_page_recovery("青岛旅游")

    assert evidence["status"] == "completed"
    assert calls == [original, replacement]
    assert crawler.context_page is replacement
    assert crawler.xhs_client.playwright_page is replacement
    crawler._open_behavior_search_page.assert_awaited_once_with("青岛旅游")
    crawler._write_storage_state.assert_not_awaited()


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
async def test_xhs_shutdown_snapshots_device_state_before_closing_pages() -> None:
    events: list[tuple[str, object]] = []
    page = FakePage("search", events)
    context = FakeContext([page], events)
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = context

    async def snapshot() -> None:
        events.append(("snapshot", "state"))

    crawler._write_storage_state = snapshot
    await crawler._prepare_browser_shutdown()

    assert events[0] == ("snapshot", "state")
    assert events[1] == ("close", "search")
    assert crawler._shutdown_storage_state_written is True


@pytest.mark.asyncio
async def test_xhs_restore_does_not_overwrite_newer_profile_cookie(tmp_path) -> None:
    snapshot_path = tmp_path / "storage-state.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "web_session",
                        "value": "stale",
                        "domain": ".xiaohongshu.com",
                        "path": "/",
                    },
                    {
                        "name": "a1",
                        "value": "fallback",
                        "domain": ".xiaohongshu.com",
                        "path": "/",
                    },
                ],
                "origins": [],
                "trippostcollect": {
                    "runtime_storage": [
                        {
                            "origin": "https://www.xiaohongshu.com",
                            "page_role": "primary",
                            "localStorage": {},
                            "sessionStorage": {"XHS_TAB_DEVICE_ID": "stable-device"},
                        },
                        {
                            "origin": "https://www.xiaohongshu.com",
                            "page_role": "secondary",
                            "localStorage": {},
                            "sessionStorage": {"XHS_TAB_DEVICE_ID": "other-device"},
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    class RestoreContext:
        def __init__(self) -> None:
            self.added_cookies = []
            self.script = ""

        async def cookies(self):
            return [{"name": "web_session", "domain": ".xiaohongshu.com", "path": "/"}]

        async def add_cookies(self, cookies):
            self.added_cookies = cookies

        async def add_init_script(self, *, script):
            self.script = script

    class RestorePage:
        url = "about:blank"

        def __init__(self) -> None:
            self.script = ""

        async def add_init_script(self, *, script):
            self.script = script

    crawler = XiaoHongShuCrawler()
    crawler.browser_context = RestoreContext()
    crawler._storage_state_path = lambda: str(snapshot_path)
    page = RestorePage()

    assert await crawler._restore_storage_state(primary_page=page) is True
    assert [cookie["name"] for cookie in crawler.browser_context.added_cookies] == ["a1"]
    assert "XHS_TAB_DEVICE_ID" not in crawler.browser_context.script
    assert "XHS_TAB_DEVICE_ID" in page.script
    assert "other-device" not in page.script
    assert "sessionStorage.getItem(key) === null" in page.script


@pytest.mark.asyncio
async def test_xhs_snapshot_preserves_account_binding_and_session_device_id(
    tmp_path,
    monkeypatch,
) -> None:
    snapshot_path = tmp_path / "storage-state.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "cookies": [],
                "origins": [],
                "trippostcollect": {
                    "schema_version": 3,
                    "account_id": "xhs-a02",
                    "identity_hash": "identity-2",
                },
            }
        ),
        encoding="utf-8",
    )

    class StoragePage:
        url = "https://www.xiaohongshu.com/explore"

        def is_closed(self):
            return False

        async def evaluate(self, script):
            assert "sessionStorage" in script
            return {
                "origin": "https://www.xiaohongshu.com",
                "url": self.url,
                "localStorage": {"b1": "browser"},
                "sessionStorage": {"XHS_TAB_DEVICE_ID": "stable-device"},
            }

    class StorageContext:
        pages = [StoragePage()]

        async def storage_state(self):
            return {"cookies": [], "origins": []}

    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_ACCOUNT_ID", "xhs-a02")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_RUN_ID", "run-verified")
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = StorageContext()
    crawler.context_page = crawler.browser_context.pages[0]
    crawler._storage_state_path = lambda: str(snapshot_path)

    await crawler._write_storage_state(session_verified=True)

    saved = json.loads(snapshot_path.read_text(encoding="utf-8"))
    metadata = saved["trippostcollect"]
    assert metadata["schema_version"] == 3
    assert metadata["account_id"] == "xhs-a02"
    assert metadata["identity_hash"] == "identity-2"
    assert metadata["runtime_storage"][0]["sessionStorage"] == {
        "XHS_TAB_DEVICE_ID": "stable-device"
    }
    assert metadata["runtime_storage"][0]["page_role"] == "primary"
    assert metadata["session_verification"] == {
        "status": "verified",
        "run_id": "run-verified",
        "source": "xhs_selfinfo",
        "verified_at": metadata["session_verification"]["verified_at"],
    }
