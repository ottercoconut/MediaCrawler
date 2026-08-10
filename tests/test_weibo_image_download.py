from __future__ import annotations

from io import BytesIO
import json
import sqlite3
from unittest.mock import AsyncMock

from PIL import Image
import pytest

import config
from media_platform.weibo import core as weibo_core
from media_platform.weibo.core import (
    WeiboCrawler,
    WeiboFullTextFetchError,
    WeiboImageDownloadError,
)
from media_platform.weibo.exception import DataFetchError


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 3), color="blue").save(output, format="PNG")
    return output.getvalue()


def valid_mblog(note_id: str) -> dict:
    return {
        "id": note_id,
        "text": "valid body",
        "content_detail_status": "detail_observed",
        "content_detail_source": "search_mblog_complete",
        "created_at": "Sat Jun 14 12:00:00 +0800 2025",
        "attitudes_count": 1,
        "comments_count": 2,
        "reposts_count": 3,
        "pics": [{"pid": f"pid-{note_id}", "url": f"https://wx.test/{note_id}.jpg"}],
        "user": {"id": "author", "screen_name": "name", "followers_count": 4},
    }


@pytest.mark.asyncio
async def test_get_note_images_passes_note_pid_order_and_url(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    crawler = WeiboCrawler()
    crawler.wb_client = AsyncMock()
    crawler.wb_client.get_note_image.side_effect = [png_bytes(), png_bytes()]
    mblog = valid_mblog("note-download")
    mblog["pics"] = [
        {"pid": "pid-first", "url": "https://wx.test/one.jpg?x=1"},
        {"pid": "pid-second", "large": {"url": "https://wx.test/two.webp?x=2"}},
    ]

    await crawler.get_note_images(mblog)

    rows = [
        json.loads(line)
        for line in (tmp_path / "weibo" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [(row["platform_post_id"], row["source_index"]) for row in rows] == [
        ("note-download", 0),
        ("note-download", 1),
    ]
    assert [row["source_asset_key"] for row in rows] == [
        "weibo:pid:pid-first",
        "weibo:pid:pid-second",
    ]
    assert all(row["mime_type"] == "image/png" for row in rows)
    assert all(row["staging_path"].endswith(".png") for row in rows)


@pytest.mark.asyncio
async def test_transient_fetch_recovers_and_records_attempts(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(
        "tools.image_download_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS",
        (0.0, 0.0),
    )
    crawler = WeiboCrawler()
    crawler.wb_client = AsyncMock()
    crawler.wb_client.get_note_image.side_effect = [None, png_bytes()]

    await crawler.get_note_images(valid_mblog("recovered-note"))

    rows = [
        json.loads(line)
        for line in (tmp_path / "weibo" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [(row["fetch_status"], row["attempts"]) for row in rows] == [
        ("downloaded", 2)
    ]
    assert crawler.wb_client.get_note_image.await_count == 2


@pytest.mark.asyncio
async def test_failed_fetch_writes_only_failed_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(
        "tools.image_download_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS",
        (0.0, 0.0),
    )
    crawler = WeiboCrawler()
    crawler.wb_client = AsyncMock()
    crawler.wb_client.get_note_image.return_value = None

    with pytest.raises(WeiboImageDownloadError):
        await crawler.get_note_images(valid_mblog("failed-note"))

    rows = [
        json.loads(line)
        for line in (tmp_path / "weibo" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["fetch_status"] == "failed"
    assert rows[0]["attempts"] == 3
    assert rows[0]["staging_path"] is None
    assert crawler.wb_client.get_note_image.await_count == 3
    assert not list((tmp_path / "weibo").rglob("*.png"))


class SearchClient:
    def __init__(self, cards):
        self.cards = cards
        self.calls = 0

    async def get_note_by_keyword(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {"cards": self.cards}
        return {"cards": []}


def prepare_search(monkeypatch, tmp_path, cards):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_TARGET_NEW_POSTS", "5")
    monkeypatch.setenv("TRIPPOSTCOLLECT_COMPLETION_MODE", "source-exhausted")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "KEYWORDS", "test")
    monkeypatch.setattr(config, "WEIBO_SEARCH_TYPE", "default")
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 20)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = WeiboCrawler()
    crawler.wb_client = SearchClient(cards)
    crawler.batch_get_notes_full_text = AsyncMock(
        side_effect=lambda notes: notes
    )
    crawler.batch_get_notes_comments = AsyncMock(return_value=None)
    crawler.get_note_images = AsyncMock(return_value=None)
    monkeypatch.setattr(weibo_core.weibo_store, "update_weibo_note", AsyncMock(return_value=None))
    return crawler, state_path


@pytest.mark.asyncio
async def test_known_id_and_invalid_note_are_skipped_before_media(monkeypatch, tmp_path):
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        connection.execute("INSERT INTO web_posts VALUES ('weibo', 'known', NULL)")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(db_path))
    invalid = valid_mblog("invalid")
    invalid["user"].pop("followers_count")
    crawler, _ = prepare_search(
        monkeypatch,
        tmp_path,
        [
            {"card_type": 9, "mblog": valid_mblog("known")},
            {"card_type": 9, "mblog": invalid},
            {"card_type": 9, "mblog": valid_mblog("new")},
        ],
    )

    await crawler.search()

    crawler.get_note_images.assert_awaited_once()
    assert crawler.get_note_images.await_args.args[0]["id"] == "new"


@pytest.mark.asyncio
async def test_image_failure_is_deferred_and_later_candidate_continues(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, state_path = prepare_search(
        monkeypatch,
        tmp_path,
        [
            {"card_type": 9, "mblog": valid_mblog("retry-note")},
            {"card_type": 9, "mblog": valid_mblog("success-note")},
        ],
    )
    crawler.get_note_images.side_effect = [
        WeiboImageDownloadError(
            "retry-note", 0, "image_download_retryable", attempts=3
        ),
        None,
    ]

    await crawler.search()

    weibo_core.weibo_store.update_weibo_note.assert_awaited_once()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    deferred = [event for event in events if event["type"] == "candidate_deferred"]
    assert deferred[0]["details"]["identity"] == "retry-note"
    assert deferred[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "deferred_retry_pending"
    assert stopped["details"]["stop_detail"] == "retryable_candidate_failures"
    assert stopped["details"]["resume_page"] == 1
    assert stopped["details"]["candidate_count"] == 2
    assert stopped["details"]["candidate_identities"] == ["success-note"]


@pytest.mark.asyncio
async def test_terminal_image_failure_stops_page_without_deferral(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, state_path = prepare_search(
        monkeypatch,
        tmp_path,
        [
            {"card_type": 9, "mblog": valid_mblog("terminal-note")},
            {"card_type": 9, "mblog": valid_mblog("must-not-store")},
        ],
    )
    crawler.get_note_images.side_effect = WeiboImageDownloadError(
        "terminal-note", 0, "image_decode_failed", attempts=1
    )

    with pytest.raises(WeiboImageDownloadError):
        await crawler.search()

    weibo_core.weibo_store.update_weibo_note.assert_not_awaited()
    assert crawler.get_note_images.await_count == 1
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    assert not [event for event in events if event["type"] == "candidate_deferred"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == (
        "image_materialization_terminal:image_decode_failed"
    )
    assert stopped["details"]["resume_page"] == 1
    assert stopped["details"]["candidate_identities"] == []


@pytest.mark.asyncio
async def test_short_and_long_posts_record_authoritative_full_text_sources(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_WEIBO_FULL_TEXT", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = WeiboCrawler()
    crawler.wb_client = AsyncMock()
    short = {"mblog": {"id": "short", "text": "complete", "isLongText": False}}
    long = {"mblog": {"id": "long", "text": "truncated", "isLongText": True}}
    crawler.wb_client.get_note_info_by_id.return_value = {
        "mblog": {"id": "long", "text": "complete long body", "isLongText": True}
    }

    short_result = await crawler.get_note_full_text(short)
    long_result = await crawler.get_note_full_text(long)

    assert short_result["mblog"]["content_detail_status"] == "detail_observed"
    assert short_result["mblog"]["content_detail_source"] == "search_mblog_complete"
    assert long_result["mblog"]["text"] == "complete long body"
    assert long_result["mblog"]["content_detail_status"] == "detail_observed"
    assert long_result["mblog"]["content_detail_source"] == "mobile_detail"


@pytest.mark.asyncio
async def test_long_text_detail_failure_blocks_batch_and_keeps_candidate_unseen(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    long_mblog = valid_mblog("long-retry")
    long_mblog.update({"isLongText": True, "text": "truncated...全文"})
    crawler, state_path = prepare_search(
        monkeypatch,
        tmp_path,
        [{"card_type": 9, "mblog": long_mblog}],
    )
    crawler.batch_get_notes_full_text = AsyncMock(
        side_effect=WeiboFullTextFetchError("long-retry", "detail_request_failed")
    )

    with pytest.raises(WeiboFullTextFetchError):
        await crawler.search()

    weibo_core.weibo_store.update_weibo_note.assert_not_awaited()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_detail"] == "full_text_request_failed"
    assert stopped["details"]["resume_page"] == 1
    assert stopped["details"]["candidate_identities"] == []


@pytest.mark.asyncio
async def test_long_text_detail_request_error_never_returns_search_excerpt(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_WEIBO_FULL_TEXT", True)
    crawler = WeiboCrawler()
    crawler.wb_client = AsyncMock()
    crawler.wb_client.get_note_info_by_id.side_effect = DataFetchError("temporary")
    note = {"mblog": {"id": "long", "text": "truncated...全文", "isLongText": True}}

    with pytest.raises(WeiboFullTextFetchError) as exc_info:
        await crawler.get_note_full_text(note)

    assert exc_info.value.code == "detail_request_failed"
    assert "content_detail_status" not in note["mblog"]
