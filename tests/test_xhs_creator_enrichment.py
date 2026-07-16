from unittest.mock import AsyncMock

import pytest

from media_platform.xhs.core import XiaoHongShuCrawler


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
async def test_creator_enrichment_does_not_hide_visible_browser_block(crawler):
    crawler.xhs_client = _CreatorClient(None)
    crawler._get_creator_info_from_browser = AsyncMock(
        side_effect=RuntimeError("xhs_creator_profile_visible_block:captcha_or_verify")
    )
    note = {"user": {"user_id": "author-3"}}

    with pytest.raises(RuntimeError, match="xhs_creator_profile_visible_block"):
        await crawler.enrich_note_creator(note)


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
