from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import config
from media_platform.zhihu.core import ZhihuCrawler
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
        image_list=["https://pic1.zhimg.com/v2-search_r.jpg"],
    )

    result = await crawler.enrich_search_content_detail(content)

    assert result.content_detail_status == "detail_observed"
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

    result = await crawler.enrich_search_content_detail(content)
    await crawler.get_content_images(result)

    assert result.content_detail_status == mode
    crawler.zhihu_client.get_content_image.assert_not_awaited()
    assert zhihu_store.zhihu_content_image_assets(result) == []
