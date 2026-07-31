from __future__ import annotations

import json

import pytest

import config
from media_platform.douyin.core import DouYinCrawler
from media_platform.douyin.exception import SearchResponseError
from media_platform.douyin.search_safety import (
    classify_empty_first_page,
    validate_douyin_search_response,
)


class EmptySearchClient:
    async def search_info_by_keyword(self, **kwargs):
        return {
            "status_code": 0,
            "data": [],
            "has_more": 0,
            "extra": {"logid": "request-log-id"},
        }


class FakeLocator:
    def __init__(self, *, count: int = 0, text: str = ""):
        self._count = count
        self._text = text

    async def count(self) -> int:
        return self._count

    async def inner_text(self, *, timeout: int) -> str:
        return self._text


class FakeSearchPage:
    url = "https://www.douyin.com/search/test?type=general"

    def __init__(self, *, result_count: int, visible_text: str):
        self.result_count = result_count
        self.visible_text = visible_text

    def locator(self, selector: str) -> FakeLocator:
        if selector == "body":
            return FakeLocator(text=self.visible_text)
        return FakeLocator(count=self.result_count)


def prepare_empty_search(monkeypatch, tmp_path, *, result_count: int, visible_text: str):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_TARGET_NEW_POSTS", "50")
    monkeypatch.setenv("TRIPPOSTCOLLECT_MAX_STAGNANT_BATCHES", "3")
    monkeypatch.setenv("TRIPPOSTCOLLECT_COMPLETION_MODE", "source-exhausted")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR", raising=False)
    monkeypatch.delenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET", raising=False)
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 1000)
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "KEYWORDS", "青岛崂山旅游攻略")
    monkeypatch.setattr(config, "PUBLISH_TIME_TYPE", 0)

    crawler = DouYinCrawler()
    crawler.dy_client = EmptySearchClient()
    crawler.context_page = FakeSearchPage(
        result_count=result_count,
        visible_text=visible_text,
    )
    return crawler, state_path


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"status_code": 10000, "data": [], "has_more": 0}, "search_business_status_nonzero"),
        ({"status_code": 0, "has_more": 0}, "missing_data_field"),
        ({"status_code": 0, "data": [], "has_more": "0"}, "invalid_has_more_field"),
        ({"status_code": 0, "data": [], "has_more": 0, "extra": []}, "invalid_extra_field"),
        ({"status_code": 0, "data": [], "has_more": 1}, "missing_next_search_id"),
    ],
)
def test_invalid_search_response_is_rejected(payload, reason) -> None:
    with pytest.raises(SearchResponseError) as raised:
        validate_douyin_search_response(payload)
    assert raised.value.reason == reason


def test_empty_terminal_response_remains_available_for_page_verification() -> None:
    payload = {
        "status_code": 0,
        "data": [],
        "has_more": 0,
        "extra": {"logid": "request-log-id"},
    }
    assert validate_douyin_search_response(payload) is payload


def test_empty_first_page_classifier_prefers_visible_results() -> None:
    assert classify_empty_first_page(
        visible_result_count=4,
        visible_text="暂无搜索结果",
    ) == "visible_results"


@pytest.mark.asyncio
async def test_visible_results_turn_empty_first_api_page_into_runtime_failure(
    monkeypatch,
    tmp_path,
) -> None:
    crawler, state_path = prepare_empty_search(
        monkeypatch,
        tmp_path,
        result_count=4,
        visible_text="综合 视频 用户",
    )

    await crawler.search()

    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    checked = [event for event in events if event["type"] == "douyin_empty_first_page_checked"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"]
    assert checked[-1]["details"]["classification"] == "visible_results"
    assert stopped[-1]["details"]["stop_reason"] == "runtime_failed"
    assert stopped[-1]["details"]["stop_detail"] == "empty_api_response_with_visible_results"
    assert stopped[-1]["details"]["resume_page"] == 1
    assert stopped[-1]["details"]["resume_offset"] == 0


@pytest.mark.asyncio
async def test_explicit_visible_no_result_marker_can_exhaust_first_page(
    monkeypatch,
    tmp_path,
) -> None:
    crawler, state_path = prepare_empty_search(
        monkeypatch,
        tmp_path,
        result_count=0,
        visible_text="没有找到相关结果，换个关键词试试",
    )

    await crawler.search()

    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"]
    assert stopped[-1]["details"]["stop_reason"] == "source_exhausted"
    assert stopped[-1]["details"]["stop_detail"] == "verified_empty_first_page"


@pytest.mark.asyncio
async def test_ambiguous_empty_first_page_is_runtime_failure(monkeypatch, tmp_path) -> None:
    crawler, state_path = prepare_empty_search(
        monkeypatch,
        tmp_path,
        result_count=0,
        visible_text="综合 视频 用户",
    )

    await crawler.search()

    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"]
    assert stopped[-1]["details"]["stop_reason"] == "runtime_failed"
    assert stopped[-1]["details"]["stop_detail"] == "ambiguous_empty_first_page"
