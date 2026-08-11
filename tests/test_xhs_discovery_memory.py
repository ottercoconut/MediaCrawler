from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

import config
from playwright.async_api import Error as PlaywrightError
from tenacity import Future, RetryError
from media_platform.xhs import core as xhs_core
from media_platform.xhs.core import (
    XiaoHongShuCrawler,
    XHSImageDownloadError,
    XHSNoteDetailUnavailable,
)
from media_platform.xhs.exception import DataFetchError


class SearchClient:
    def __init__(self, items):
        self.items = items
        self.calls = []

    async def get_note_by_keyword(self, **kwargs):
        self.calls.append(kwargs)
        return {"items": self.items, "has_more": True}


class LoginExpiredSearchClient:
    def __init__(self, *, recover: bool):
        self.recover = recover
        self.calls = []

    async def get_note_by_keyword(self, **kwargs):
        self.calls.append(kwargs)
        if not self.recover or len(self.calls) == 1:
            failure = DataFetchError("登录已过期")
            raise RetryError(Future.construct(3, failure, has_exception=True))
        return {"items": [], "has_more": False}


def valid_note(note_id: str) -> dict:
    return {
        "note_id": note_id,
        "title": f"note {note_id}",
        "desc": "body",
        "content_detail_status": "detail_observed",
        "content_detail_source": "note_detail",
        "time": 1_700_000_000_000,
        "user": {"user_id": f"author-{note_id}", "nickname": "author"},
        "image_list": [{"url_default": "https://example.test/image.jpg"}],
        "creator_profile": {"fans_count": 100},
        "interact_info": {
            "liked_count": 1,
            "collected_count": 2,
            "comment_count": 3,
            "share_count": 4,
        },
        "xsec_token": "token",
    }


def prepare_crawler(monkeypatch, tmp_path, *, items, start_page=3, hard_limit=10, target=1):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_TARGET_NEW_POSTS", str(target))
    monkeypatch.setenv("TRIPPOSTCOLLECT_MAX_STAGNANT_BATCHES", "3")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR", "saved-search-id")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", hard_limit)
    monkeypatch.setattr(config, "START_PAGE", start_page)
    monkeypatch.setattr(config, "KEYWORDS", "青岛旅游")
    monkeypatch.setattr(config, "SORT_TYPE", "")
    monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 1)

    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = SearchClient(items)
    detail_ids = []

    async def fetch_detail(*, note_id, **kwargs):
        detail_ids.append(note_id)
        return valid_note(note_id)

    crawler.get_note_detail_async_task = fetch_detail
    crawler._guarded_pause = AsyncMock(return_value=0.0)
    crawler._maybe_run_post_interaction = AsyncMock(return_value=None)
    crawler.enrich_note_creator = AsyncMock(return_value=None)
    crawler.get_notice_media = AsyncMock(return_value=None)
    crawler.batch_get_note_comments = AsyncMock(return_value=None)
    crawler.is_video_note = lambda note: False
    crawler.note_detail_summaries = lambda notes: [{"note_id": note["note_id"]} for note in notes]
    monkeypatch.setattr(xhs_core.xhs_store, "update_xhs_note", AsyncMock(return_value=None))
    monkeypatch.setattr(
        xhs_core.xhs_store,
        "_normalized_creator_item",
        lambda user_id, profile: {"fans_count": profile["fans_count"]},
    )
    return crawler, detail_ids, state_path


@pytest.mark.asyncio
async def test_known_note_is_skipped_before_detail_request(monkeypatch, tmp_path):
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        conn.execute("INSERT INTO web_posts VALUES ('xhs', 'known-note', NULL)")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(db_path))
    crawler, detail_ids, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "known-note"}, {"id": "new-note"}],
    )

    await crawler.search()

    assert detail_ids == ["new-note"]
    assert crawler.xhs_client.calls[0]["page"] == 3
    assert crawler.xhs_client.calls[0]["search_id"] == "saved-search-id"
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["resume_page"] == 4
    assert stopped["details"]["batch_complete"] is True


@pytest.mark.asyncio
async def test_incomplete_boundary_page_is_repeated(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "first"}, {"id": "second"}],
        hard_limit=1,
        target=5,
    )

    await crawler.search()

    assert detail_ids == ["first"]
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["resume_page"] == 3
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["batch_complete"] is False
    assert stopped["details"]["candidate_identities"] == ["first"]


@pytest.mark.asyncio
async def test_top_refresh_uses_fresh_search_id_before_saved_frontier(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "top-new"}],
        start_page=1,
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "2")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "1")
    monkeypatch.setattr(xhs_core, "get_search_id", lambda: "fresh-search-id")

    await crawler.search()

    assert detail_ids == ["top-new"]
    assert [(call["page"], call["search_id"]) for call in crawler.xhs_client.calls] == [
        (1, "fresh-search-id")
    ]
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["discovery_phase"] == "refresh"


@pytest.mark.asyncio
async def test_browser_context_close_is_recorded_as_resumable_runtime_failure(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setenv("TRIPPOSTCOLLECT_COMPLETION_MODE", "source-exhausted")
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "new-note"}],
    )
    crawler.enrich_note_creator = AsyncMock(
        side_effect=PlaywrightError(
            "BrowserContext.new_page: Target page, context or browser has been closed"
        )
    )

    await crawler.search()

    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == "browser_context_closed"
    assert stopped["details"]["resume_page"] == 3
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["batch_complete"] is False


@pytest.mark.asyncio
async def test_detail_failure_is_recorded_seen_and_search_continues(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "retry-detail"}],
    )
    crawler.get_note_detail_async_task = AsyncMock(
        side_effect=XHSNoteDetailUnavailable("retry-detail", "api_and_html_empty")
    )
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_note_by_keyword.side_effect = [
        {"items": [{"id": "retry-detail"}], "has_more": True},
        {"items": [], "has_more": False},
    ]

    await crawler.search()

    xhs_core.xhs_store.update_xhs_note.assert_not_awaited()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert skipped[0]["details"]["identity"] == "retry-detail"
    assert skipped[0]["details"]["failure_scope"] == "post"
    assert skipped[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["resume_page"] == 5
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["candidate_identities"] == ["retry-detail"]


@pytest.mark.asyncio
async def test_image_failure_is_recorded_and_later_xhs_candidate_continues(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setenv("TRIPPOSTCOLLECT_COMPLETION_MODE", "target-new-posts")
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "retry-image"}, {"id": "success-image"}],
    )
    crawler.get_notice_media = AsyncMock(
        side_effect=[
            XHSImageDownloadError(
                "retry-image", 0, "image_download_retryable", attempts=3
            ),
            None,
        ]
    )

    await crawler.search()

    xhs_core.xhs_store.update_xhs_note.assert_awaited_once()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert skipped[0]["details"]["identity"] == "retry-image"
    assert skipped[0]["details"]["failure_scope"] == "image"
    assert skipped[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "target_new_met"
    assert stopped["details"]["resume_page"] == 4
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["candidate_identities"] == [
        "retry-image",
        "success-image",
    ]


@pytest.mark.asyncio
async def test_creator_failure_is_recorded_and_later_xhs_candidate_continues(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setenv("TRIPPOSTCOLLECT_COMPLETION_MODE", "target-new-posts")
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "retry-creator"}, {"id": "success-creator"}],
    )
    crawler.enrich_note_creator = AsyncMock(
        side_effect=[
            RuntimeError("creator_profile_unavailable_after_retry"),
            None,
        ]
    )

    await crawler.search()

    xhs_core.xhs_store.update_xhs_note.assert_awaited_once()
    assert crawler.get_notice_media.await_count == 1
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert skipped[0]["details"]["identity"] == "retry-creator"
    assert skipped[0]["details"]["failure_scope"] == "post"
    assert skipped[0]["details"]["error_code"] == "creator_profile_unavailable"
    assert skipped[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "target_new_met"
    assert stopped["details"]["candidate_identities"] == [
        "retry-creator",
        "success-creator",
    ]


@pytest.mark.asyncio
async def test_terminal_image_failure_is_recorded_and_later_candidate_continues(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setenv("TRIPPOSTCOLLECT_COMPLETION_MODE", "target-new-posts")
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "terminal-image"}, {"id": "must-not-store"}],
    )
    crawler.get_notice_media = AsyncMock(
        side_effect=[
            XHSImageDownloadError(
                "terminal-image", 0, "image_decode_failed", attempts=1
            ),
            None,
        ]
    )

    await crawler.search()

    xhs_core.xhs_store.update_xhs_note.assert_awaited_once()
    assert crawler.get_notice_media.await_count == 2
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    assert skipped[0]["details"]["identity"] == "terminal-image"
    assert skipped[0]["details"]["failure_scope"] == "image"
    assert skipped[0]["details"]["retryable"] is False
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "target_new_met"
    assert stopped["details"]["resume_page"] == 4
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["candidate_identities"] == [
        "must-not-store",
        "terminal-image",
    ]


@pytest.mark.asyncio
async def test_wrapped_login_expiry_waits_and_retries_same_search_page(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setenv("TRIPPOSTCOLLECT_COMPLETION_MODE", "source-exhausted")
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[],
    )
    crawler.xhs_client = LoginExpiredSearchClient(recover=True)
    crawler._wait_for_midrun_login_recovery = AsyncMock(return_value=True)

    await crawler.search()

    assert [call["page"] for call in crawler.xhs_client.calls] == [3, 3]
    crawler._wait_for_midrun_login_recovery.assert_awaited_once_with("青岛旅游")
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["stop_detail"] == "has_more_false"


@pytest.mark.asyncio
async def test_wrapped_login_expiry_timeout_keeps_current_page_as_frontier(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setenv("TRIPPOSTCOLLECT_COMPLETION_MODE", "source-exhausted")
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[],
    )
    crawler.xhs_client = LoginExpiredSearchClient(recover=False)
    crawler._wait_for_midrun_login_recovery = AsyncMock(return_value=False)

    await crawler.search()

    assert [call["page"] for call in crawler.xhs_client.calls] == [3]
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == "login_required"
    assert stopped["details"]["resume_page"] == 3
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["batch_complete"] is False
