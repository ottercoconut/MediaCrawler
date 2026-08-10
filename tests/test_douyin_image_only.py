from __future__ import annotations

from io import BytesIO
import json
from unittest.mock import AsyncMock

from PIL import Image
import pytest

import config
from media_platform.douyin import core as douyin_core
from media_platform.douyin.core import DouYinCrawler, DouyinImageDownloadError
from store import douyin as douyin_store


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (9, 6), color="orange").save(output, format="PNG")
    return output.getvalue()


def image_aweme(aweme_id: str = "dy-note") -> dict:
    return {
        "aweme_id": aweme_id,
        "aweme_type": 68,
        "desc": "image note",
        "create_time": 1_700_000_000,
        "author": {"uid": "author", "nickname": "name", "follower_count": 5},
        "statistics": {
            "digg_count": 1,
            "collect_count": 2,
            "comment_count": 3,
            "share_count": 4,
        },
        "images": [
            {
                "uri": "uri-body-one",
                "url_list": [
                    "https://p3.test/body-one.jpeg?signature=old",
                    "https://p9.test/body-one.jpeg?signature=fresh",
                ],
            }
        ],
        "video": {
            "raw_cover": {"url_list": ["", "https://media.test/cover.jpg"]},
            "play_addr": {"url_list": ["", "https://media.test/video.mp4"]},
        },
        "music": {"play_url": {"uri": "https://media.test/music.mp3"}},
    }


@pytest.mark.asyncio
async def test_strict_image_entry_requests_only_note_image_and_writes_true_format(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("media_platform.douyin.core.random.random", lambda: 0)
    crawler = DouYinCrawler()
    crawler.dy_client = AsyncMock()
    crawler.dy_client.get_aweme_media.return_value = png_bytes()
    crawler.get_aweme_video = AsyncMock(return_value=None)
    video_store = AsyncMock(return_value=None)
    monkeypatch.setattr(douyin_store, "update_dy_aweme_video", video_store)
    aweme = image_aweme()

    await crawler.get_aweme_media(aweme)

    crawler.dy_client.get_aweme_media.assert_awaited_once_with(
        "https://p9.test/body-one.jpeg?signature=fresh"
    )
    crawler.get_aweme_video.assert_not_awaited()
    video_store.assert_not_awaited()
    stored = tmp_path / "douyin" / "images" / "dy-note" / "000.png"
    assert stored.read_bytes() == png_bytes()
    rows = [
        json.loads(line)
        for line in (tmp_path / "douyin" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["source_asset_key"] == "douyin:uri:uri-body-one"
    assert rows[0]["mime_type"] == "image/png"
    serialized = json.dumps(rows)
    assert "cover.jpg" not in serialized
    assert "video.mp4" not in serialized
    assert "music.mp3" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("aweme", [{}, {"video": {"play_addr": {"url_list": ["v"]}}}])
async def test_empty_or_video_candidate_reaches_no_image_or_video_bytes(monkeypatch, aweme):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    crawler = DouYinCrawler()
    crawler.dy_client = AsyncMock()
    crawler.get_aweme_video = AsyncMock(return_value=None)

    await crawler.get_aweme_media(aweme)

    crawler.dy_client.get_aweme_media.assert_not_awaited()
    crawler.get_aweme_video.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_note_image_failure_recovers_and_records_attempts(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("media_platform.douyin.core.random.random", lambda: 0)
    monkeypatch.setattr(
        "tools.image_download_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS",
        (0.0, 0.0),
    )
    crawler = DouYinCrawler()
    crawler.dy_client = AsyncMock()
    crawler.dy_client.get_aweme_media.side_effect = [None, png_bytes()]

    await crawler.get_aweme_images(image_aweme("dy-recovered"))

    rows = [
        json.loads(line)
        for line in (tmp_path / "douyin" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [(row["fetch_status"], row["attempts"]) for row in rows] == [
        ("downloaded", 2)
    ]
    assert crawler.dy_client.get_aweme_media.await_count == 2


@pytest.mark.asyncio
async def test_failed_note_image_writes_failed_manifest_only(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("media_platform.douyin.core.random.random", lambda: 0)
    monkeypatch.setattr(
        "tools.image_download_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS",
        (0.0, 0.0),
    )
    crawler = DouYinCrawler()
    crawler.dy_client = AsyncMock()
    crawler.dy_client.get_aweme_media.return_value = None

    with pytest.raises(DouyinImageDownloadError):
        await crawler.get_aweme_images(image_aweme("dy-failed"))

    rows = [
        json.loads(line)
        for line in (tmp_path / "douyin" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[0]["fetch_status"] == "failed"
    assert rows[0]["attempts"] == 3
    assert rows[0]["staging_path"] is None
    assert crawler.dy_client.get_aweme_media.await_count == 3
    assert not (tmp_path / "douyin" / "images" / "dy-failed").exists()


class SearchClient:
    async def search_info_by_keyword(self, **kwargs):
        return {
            "status_code": 0,
            "data": [
                {"aweme_info": image_aweme("retry-aweme")},
                {"aweme_info": image_aweme("success-aweme")},
            ],
            "has_more": 1,
            "extra": {"logid": "fresh-search-id"},
        }


@pytest.mark.asyncio
async def test_image_failure_keeps_page_offset_cursor_and_candidate_unseen(
    monkeypatch, tmp_path
):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_TARGET_NEW_POSTS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_COMPLETION_MODE", "target-new-posts")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR", raising=False)
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 10)
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "KEYWORDS", "test")
    monkeypatch.setattr(config, "PUBLISH_TIME_TYPE", 0)
    crawler = DouYinCrawler()
    crawler.dy_client = SearchClient()
    crawler.enrich_aweme_creator = AsyncMock(side_effect=lambda aweme: aweme)
    crawler.get_aweme_images = AsyncMock(
        side_effect=[
            DouyinImageDownloadError(
                "retry-aweme", 0, "image_download_retryable", attempts=3
            ),
            None,
        ]
    )
    crawler.batch_get_note_comments = AsyncMock(return_value=None)
    store = AsyncMock(return_value=None)
    monkeypatch.setattr(douyin_core.douyin_store, "update_douyin_aweme", store)

    await crawler.search()

    store.assert_awaited_once()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    deferred = [event for event in events if event["type"] == "candidate_deferred"]
    assert deferred[0]["details"]["identity"] == "retry-aweme"
    assert deferred[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "target_new_met"
    assert stopped["details"]["resume_page"] == 1
    assert stopped["details"]["resume_offset"] == 0
    assert stopped["details"]["resume_cursor"] == ""
    assert stopped["details"]["candidate_count"] == 2
    assert stopped["details"]["candidate_identities"] == ["success-aweme"]


@pytest.mark.asyncio
async def test_terminal_image_failure_stops_cursor_without_deferral(
    monkeypatch, tmp_path
):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_TARGET_NEW_POSTS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_COMPLETION_MODE", "target-new-posts")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR", raising=False)
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 10)
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "KEYWORDS", "test")
    monkeypatch.setattr(config, "PUBLISH_TIME_TYPE", 0)
    crawler = DouYinCrawler()
    crawler.dy_client = SearchClient()
    crawler.enrich_aweme_creator = AsyncMock(side_effect=lambda aweme: aweme)
    crawler.get_aweme_images = AsyncMock(
        side_effect=DouyinImageDownloadError(
            "retry-aweme", 0, "image_too_large", attempts=1
        )
    )
    crawler.batch_get_note_comments = AsyncMock(return_value=None)
    store = AsyncMock(return_value=None)
    monkeypatch.setattr(douyin_core.douyin_store, "update_douyin_aweme", store)

    await crawler.search()

    store.assert_not_awaited()
    assert crawler.get_aweme_images.await_count == 1
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    assert not [event for event in events if event["type"] == "candidate_deferred"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == (
        "image_materialization_terminal:image_too_large"
    )
    assert stopped["details"]["resume_page"] == 1
    assert stopped["details"]["resume_offset"] == 0
    assert stopped["details"]["candidate_identities"] == []
