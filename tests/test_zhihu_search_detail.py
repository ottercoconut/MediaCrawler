from __future__ import annotations

import asyncio

import config
import pytest
from constant import zhihu as constant
from media_platform.zhihu.core import ZhihuCrawler
from media_platform.zhihu.help import merge_search_content_detail
from model.m_zhihu import ZhihuContent
from store import zhihu as zhihu_store


def test_merge_search_detail_keeps_search_author_evidence() -> None:
    search_content = ZhihuContent(
        content_id="answer-1",
        content_type="answer",
        content_text="search excerpt",
        creator_hash="search-author",
        followers_count=123,
        followers_observed=True,
        author_followers_source="search_author",
    )
    detail_content = ZhihuContent(
        content_id="answer-1",
        content_type="answer",
        content_text="full detail",
        image_list=["https://example.test/content.jpg"],
        creator_hash="detail-author",
        followers_count=0,
        followers_observed=False,
    )

    merged = merge_search_content_detail(search_content, detail_content)

    assert merged.content_text == "full detail"
    assert merged.image_list == ["https://example.test/content.jpg"]
    assert merged.image_count == 1
    assert merged.content_detail_status == "detail_observed"
    assert merged.content_detail_source == "answer_detail"
    assert merged.creator_hash == "search-author"
    assert merged.followers_count == 123
    assert merged.followers_observed is True
    assert merged.author_followers_source == "search_author"


@pytest.mark.asyncio
async def test_detail_mode_marks_success_and_parse_failure(monkeypatch) -> None:
    crawler = ZhihuCrawler.__new__(ZhihuCrawler)

    class Client:
        async def get_answer_info(self, question_id: str, answer_id: str):
            if answer_id == "missing":
                return None
            return ZhihuContent(
                content_id=answer_id,
                question_id=question_id,
                content_type=constant.ANSWER_NAME,
                image_list=[],
            )

    crawler.zhihu_client = Client()

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    observed = await crawler.get_note_detail(
        "https://www.zhihu.com/question/123/answer/observed",
        asyncio.Semaphore(1),
    )
    failed = await crawler.get_note_detail(
        "https://www.zhihu.com/question/123/answer/missing",
        asyncio.Semaphore(1),
    )

    assert observed is not None
    assert observed.content_detail_status == "detail_observed"
    assert observed.content_detail_source == "answer_detail"
    assert failed is not None
    assert failed.content_detail_status == "parse_failed"


@pytest.mark.asyncio
async def test_specified_detail_mode_reuses_one_semaphore(monkeypatch) -> None:
    crawler = ZhihuCrawler.__new__(ZhihuCrawler)
    semaphore_ids: list[int] = []

    async def fake_detail(full_note_url: str, semaphore: asyncio.Semaphore):
        semaphore_ids.append(id(semaphore))
        return ZhihuContent(
            content_id=full_note_url.rsplit("/", 1)[-1],
            content_type=constant.ARTICLE_NAME,
            content_detail_status="detail_observed",
        )

    stored: list[str] = []

    async def fake_store(content: ZhihuContent):
        stored.append(content.content_id)

    async def no_comments(_: list[ZhihuContent]) -> None:
        return None

    monkeypatch.setattr(config, "ZHIHU_SPECIFIED_ID_LIST", [
        "https://zhuanlan.zhihu.com/p/1",
        "https://zhuanlan.zhihu.com/p/2",
    ])
    monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 1)
    monkeypatch.setattr(crawler, "get_note_detail", fake_detail)
    monkeypatch.setattr(crawler, "batch_get_content_comments", no_comments)
    monkeypatch.setattr(zhihu_store, "update_zhihu_content", fake_store)

    await crawler.get_specified_notes()

    assert len(set(semaphore_ids)) == 1
    assert stored == ["1", "2"]
