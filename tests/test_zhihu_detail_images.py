from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

import config
from media_platform.zhihu import core as zhihu_core
from media_platform.zhihu.core import ZhihuCrawler, ZhihuDetailFetchError
from media_platform.zhihu.exception import DataFetchError
from model.m_zhihu import ZhihuContent
from store import zhihu as zhihu_store


@pytest.mark.asyncio
async def test_search_payload_with_body_images_becomes_observed_without_detail_request(
    monkeypatch,
):
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    content = ZhihuContent(
        content_id="search-image",
        content_type="answer",
        content_text="complete search body",
        image_list=["https://pic1.zhimg.com/v2-search_r.jpg"],
    )

    result = await crawler.enrich_search_content_detail(content)

    assert result.content_detail_status == "detail_observed"
    assert result.content_detail_source == "search_content"
    crawler.zhihu_client.get_answer_info.assert_not_awaited()
    assert [asset["url"] for asset in zhihu_store.zhihu_content_image_assets(result)] == [
        "https://pic1.zhimg.com/v2-search_r.jpg"
    ]


@pytest.mark.asyncio
async def test_search_without_images_uses_detail_body_and_filters_formula(monkeypatch):
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    crawler.zhihu_client.get_answer_info.return_value = ZhihuContent(
        content_id="detail-image",
        content_type="answer",
        content_text="full body",
        image_list=[
            "https://pic1.zhimg.com/v2-detail_b.webp",
            "https://www.zhihu.com/equation?tex=y",
        ],
    )
    search = ZhihuContent(
        content_id="detail-image",
        question_id="question-1",
        content_type="answer",
        creator_hash="author",
        followers_observed=True,
    )

    result = await crawler.enrich_search_content_detail(search)

    assert result.content_detail_status == "detail_observed"
    assert result.content_detail_source == "answer_detail"
    assert result.content_text == "full body"
    assert [asset["url"] for asset in zhihu_store.zhihu_content_image_assets(result)] == [
        "https://pic1.zhimg.com/v2-detail_b.webp"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["request_failed", "parse_failed"])
async def test_failed_or_unparsed_detail_cannot_enter_image_success(monkeypatch, mode):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    if mode == "request_failed":
        crawler.zhihu_client.get_answer_info.side_effect = DataFetchError("failed")
    else:
        crawler.zhihu_client.get_answer_info.return_value = None
    content = ZhihuContent(
        content_id=mode,
        question_id="question-1",
        content_type="answer",
        image_list=[],
    )

    with pytest.raises(ZhihuDetailFetchError) as exc_info:
        await crawler.enrich_search_content_detail(content)

    assert exc_info.value.code == f"detail_{mode}"
    crawler.zhihu_client.get_content_image.assert_not_awaited()
    assert zhihu_store.zhihu_content_image_assets(content) == []


@pytest.mark.asyncio
async def test_recoverable_detail_failure_keeps_zhihu_candidate_unseen(
    monkeypatch,
    tmp_path,
):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_TARGET_NEW_POSTS", "5")
    monkeypatch.setenv("TRIPPOSTCOLLECT_COMPLETION_MODE", "source-exhausted")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "START_PAGE", 4)
    monkeypatch.setattr(config, "KEYWORDS", "test")
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 20)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    crawler.zhihu_client.get_note_by_keyword.return_value = [
        ZhihuContent(
            content_id="retry-detail",
            question_id="question-1",
            content_type="answer",
            content_text="search excerpt",
        )
    ]
    crawler.zhihu_client.get_answer_info.side_effect = DataFetchError("temporary")
    store = AsyncMock(return_value=None)
    monkeypatch.setattr(zhihu_core.zhihu_store, "update_zhihu_content", store)

    await crawler.search()

    store.assert_not_awaited()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == "content_detail_failed"
    assert stopped["details"]["resume_page"] == 4
    assert stopped["details"]["candidate_identities"] == []
