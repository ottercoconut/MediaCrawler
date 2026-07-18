from __future__ import annotations

from media_platform.zhihu.help import merge_search_content_detail
from model.m_zhihu import ZhihuContent


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
    assert merged.creator_hash == "search-author"
    assert merged.followers_count == 123
    assert merged.followers_observed is True
    assert merged.author_followers_source == "search_author"
