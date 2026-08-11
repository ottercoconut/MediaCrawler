from unittest.mock import AsyncMock

import pytest

from media_platform.xhs.core import XiaoHongShuCrawler, XHSCreatorProfileUnavailable
from media_platform.xhs.exception import IPBlockError
from media_platform.xhs.extractor import XiaoHongShuExtractor


class _CreatorClient:
    def __init__(self, creator_info):
        self.creator_info = creator_info
        self.calls = []

    async def get_creator_info(self, **kwargs):
        self.calls.append(kwargs)
        return self.creator_info


@pytest.fixture
def crawler(monkeypatch):
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_ENRICH_CREATORS", "1")
    instance = XiaoHongShuCrawler()
    instance._guarded_pause = AsyncMock(return_value=0.0)
    return instance


@pytest.mark.asyncio
async def test_creator_enrichment_uses_signed_in_profile_without_note_token(crawler):
    creator = {"interactions": [{"type": "fans", "count": "123"}]}
    crawler.xhs_client = _CreatorClient(creator)
    crawler._get_creator_info_from_browser = AsyncMock()
    note = {
        "user": {"user_id": "author-1"},
        "xsec_token": "note-token-must-not-be-used-for-author-profile",
        "xsec_source": "pc_search",
    }

    await crawler.enrich_note_creator(note)

    assert crawler.xhs_client.calls == [{"user_id": "author-1"}]
    assert note["creator_profile"] == creator
    crawler._get_creator_info_from_browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_creator_enrichment_falls_back_to_signed_in_browser(crawler):
    creator = {"interactions": [{"type": "fans", "count": "456"}]}
    crawler.xhs_client = _CreatorClient(None)
    crawler._get_creator_info_from_browser = AsyncMock(return_value=creator)
    note = {"user": {"user_id": "author-2"}, "xsec_token": "note-token"}

    await crawler.enrich_note_creator(note)

    crawler._get_creator_info_from_browser.assert_awaited_once_with("author-2")
    assert note["creator_profile"] == creator


@pytest.mark.asyncio
async def test_creator_enrichment_reports_empty_profile_after_all_fallbacks(crawler):
    crawler.xhs_client = _CreatorClient(None)
    crawler._get_creator_info_from_browser = AsyncMock(return_value=None)
    note = {"user": {"user_id": "author-empty"}}

    with pytest.raises(XHSCreatorProfileUnavailable) as exc_info:
        await crawler.enrich_note_creator(note)

    assert exc_info.value.attempts == 2
    crawler._get_creator_info_from_browser.assert_awaited_once_with("author-empty")


@pytest.mark.asyncio
async def test_creator_enrichment_does_not_hide_ip_block(crawler):
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_creator_info.side_effect = IPBlockError(
        "Network connection error, code 300012"
    )
    crawler._get_creator_info_from_browser = AsyncMock()
    note = {"user": {"user_id": "author-blocked"}}

    with pytest.raises(IPBlockError):
        await crawler.enrich_note_creator(note)

    crawler._get_creator_info_from_browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_creator_enrichment_does_not_hide_visible_browser_block(crawler):
    crawler.xhs_client = _CreatorClient(None)
    crawler._get_creator_info_from_browser = AsyncMock(
        side_effect=RuntimeError("xhs_creator_profile_visible_block:captcha_or_verify")
    )
    note = {"user": {"user_id": "author-3"}}

    with pytest.raises(RuntimeError, match="xhs_creator_profile_visible_block"):
        await crawler.enrich_note_creator(note)


@pytest.mark.asyncio
async def test_creator_browser_fallback_stops_on_platform_security_limit(crawler, monkeypatch):
    class SecurityLimitPage:
        async def wait_for_timeout(self, milliseconds):
            assert milliseconds >= 0

    page = SecurityLimitPage()
    crawler._new_guarded_page = AsyncMock(return_value=page)
    crawler._goto_with_deadline = AsyncMock()
    crawler._close_page_with_deadline = AsyncMock()
    crawler._wait_for_creator_profile_verification = AsyncMock()
    record_limit = AsyncMock(return_value={"observed_error_code": "300011"})

    async def inspect_state(current_page):
        assert current_page is page
        return "安全限制 Account exception, please retry later 300011", {
            "platform_security_limit": True,
            "captcha_or_verify": True,
            "rate_limited": False,
            "blocked": False,
            "login_required": False,
        }

    monkeypatch.setattr(
        "media_platform.xhs.core.inspect_visible_page_state",
        inspect_state,
    )
    monkeypatch.setattr(
        "media_platform.xhs.core.record_platform_security_limit",
        record_limit,
    )

    with pytest.raises(
        RuntimeError,
        match="xhs_creator_profile_visible_block:platform_security_limit",
    ):
        await crawler._get_creator_info_from_browser("author-security-limit")

    crawler._close_page_with_deadline.assert_awaited_once_with(
        page,
        reason="creator_profile_cleanup",
    )
    crawler._wait_for_creator_profile_verification.assert_not_awaited()
    record_limit.assert_awaited_once_with(
        page,
        stage="creator_profile:author-security-limit:arrival",
        visible_text_sample="安全限制 Account exception, please retry later 300011",
        visible_markers={
            "platform_security_limit": True,
            "captcha_or_verify": True,
            "rate_limited": False,
            "blocked": False,
            "login_required": False,
        },
    )


@pytest.mark.asyncio
async def test_startup_checkpoint_wait_stops_on_platform_security_limit(crawler, monkeypatch):
    monkeypatch.setattr(crawler, "_env_int", lambda name, default: 600)
    crawler._single_page_for_login = AsyncMock()
    crawler._cookie_markers = AsyncMock(return_value={"web_session": True})
    crawler._profile_ui_visible = AsyncMock(return_value=True)
    crawler._visible_checkpoint_markers = AsyncMock(
        return_value={
            "security": ["website-login/error"],
            "login_or_qr": [],
            "pages": [],
        }
    )

    with pytest.raises(RuntimeError, match="xhs_platform_security_limit_300011"):
        await crawler._wait_for_manual_checkpoint_if_needed()


@pytest.mark.asyncio
async def test_creator_browser_fallback_keeps_qr_page_open_until_verified(
    crawler,
    monkeypatch,
):
    creator = {"interactions": [{"type": "fans", "count": "654"}]}

    class Mouse:
        move_calls = 0
        wheel_calls = 0

        async def move(self, *args, **kwargs):
            self.move_calls += 1

        async def wheel(self, *args, **kwargs):
            self.wheel_calls += 1

    class VerificationPage:
        viewport_size = {"width": 1280, "height": 800}
        mouse = Mouse()

        def __init__(self):
            self.brought_to_front = False
            self.closed = False
            self.content_calls = 0
            self.inspection_calls = 0

        async def wait_for_timeout(self, milliseconds):
            assert not self.closed

        async def bring_to_front(self):
            self.brought_to_front = True

        async def content(self):
            self.content_calls += 1
            assert self.inspection_calls >= 3
            return "creator"

        async def close(self):
            self.closed = True

    class BrowserContext:
        async def new_page(self):
            return page

    class CreatorHtmlClient:
        @staticmethod
        def extract_creator_info_from_html(html):
            return creator if html == "creator" else None

    page = VerificationPage()
    marker_sequence = iter(
        [
            {"captcha_or_verify": True},
            {"captcha_or_verify": True},
            {"captcha_or_verify": False},
        ]
    )

    async def inspect_state(current_page):
        assert current_page is page
        assert not page.closed
        page.inspection_calls += 1
        return "", next(marker_sequence)

    crawler.browser_context = BrowserContext()
    crawler.xhs_client = CreatorHtmlClient()
    crawler._goto_with_deadline = AsyncMock()
    monkeypatch.setattr(
        "media_platform.xhs.core.inspect_visible_page_state",
        inspect_state,
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_WAIT_SECONDS", "30")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_POLL_SECONDS", "0")

    result = await crawler._get_creator_info_from_browser("author-qr")

    assert result == creator
    assert page.brought_to_front is True
    assert page.closed is True
    assert page.content_calls == 1
    assert page.mouse.move_calls == 0
    assert page.mouse.wheel_calls == 0


@pytest.mark.asyncio
async def test_creator_enrichment_caches_successful_author_profile(crawler):
    creator = {"interactions": [{"type": "fans", "count": "789"}]}
    crawler.xhs_client = _CreatorClient(creator)
    first = {"user": {"user_id": "author-4"}}
    second = {"user": {"user_id": "author-4"}}

    await crawler.enrich_note_creator(first)
    await crawler.enrich_note_creator(second)

    assert crawler.xhs_client.calls == [{"user_id": "author-4"}]
    assert first["creator_profile"] == creator
    assert second["creator_profile"] == creator


@pytest.mark.asyncio
async def test_browser_identity_headers_follow_current_chromium_version(crawler):
    class IdentityPage:
        async def evaluate(self, script):
            return {
                "webdriver": None,
                "user_agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/149.0.0.0 Safari/537.36"
                ),
                "language": "zh-CN",
                "platform": "macOS",
                "mobile": False,
                "brands": [
                    {"brand": "Chromium", "version": "149"},
                    {"brand": "Not)A;Brand", "version": "24"},
                ],
            }

    crawler.context_page = IdentityPage()

    headers = await crawler._browser_identity_headers()

    assert "Chrome/149.0.0.0" in headers["user-agent"]
    assert '"Chromium";v="149"' in headers["sec-ch-ua"]
    assert "126" not in str(headers)
    assert headers["accept-language"] == "zh-CN"


@pytest.mark.asyncio
async def test_browser_identity_headers_reject_version_mismatch(crawler):
    class IdentityPage:
        async def evaluate(self, script):
            return {
                "webdriver": None,
                "user_agent": "Mozilla/5.0 Chrome/149.0.0.0 Safari/537.36",
                "language": "zh-CN",
                "platform": "macOS",
                "mobile": False,
                "brands": [{"brand": "Chromium", "version": "126"}],
            }

    crawler.context_page = IdentityPage()

    with pytest.raises(RuntimeError, match="version_mismatch"):
        await crawler._browser_identity_headers()


@pytest.mark.asyncio
async def test_search_navigation_timeout_can_defer_to_visible_readiness(crawler):
    class CommittedPage:
        url = "https://www.xiaohongshu.com/search_result?keyword=test"

        async def goto(self, *args, **kwargs):
            raise TimeoutError

    await crawler._goto_with_deadline(
        CommittedPage(),
        "https://www.xiaohongshu.com/search_result?keyword=test",
        stage="behavior_search",
    )


def test_creator_html_extractor_stops_at_end_of_initial_state_object():
    html = (
        '<script>window.__INITIAL_STATE__={"user":{"userPageData":'
        '{"userId":"author-8","interactions":[{"type":"fans","count":"88"}]}}}'
        '</script><script>window.__NEXT_STATE__={"extra":true}</script>'
    )

    creator = XiaoHongShuExtractor().extract_creator_info_from_html(html)

    assert creator == {
        "userId": "author-8",
        "interactions": [{"type": "fans", "count": "88"}],
    }


def test_creator_html_extractor_preserves_undefined_fallback():
    html = (
        '<script nonce="test"> window.__INITIAL_STATE__ = '
        '{"user":{"userPageData":{"userId":"author-9","ipLocation":undefined}}}'
        ";</script>"
    )

    creator = XiaoHongShuExtractor().extract_creator_info_from_html(html)

    assert creator == {"userId": "author-9", "ipLocation": None}
