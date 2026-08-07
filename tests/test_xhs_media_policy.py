from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import config
from media_platform.xhs.core import XiaoHongShuCrawler
from store import xhs as xhs_store


@pytest.mark.asyncio
async def test_image_mode_never_reaches_video_method_or_store(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    crawler = XiaoHongShuCrawler()
    crawler.get_note_images = AsyncMock(return_value=None)
    crawler.get_notice_video = AsyncMock(return_value=None)
    video_store = AsyncMock(return_value=None)
    monkeypatch.setattr(xhs_store, "update_xhs_note_video", video_store)

    await crawler.get_notice_media({"note_id": "image-only"})

    crawler.get_note_images.assert_awaited_once_with({"note_id": "image-only"})
    crawler.get_notice_video.assert_not_awaited()
    video_store.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_media_mode_reaches_no_media_method(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", False)
    crawler = XiaoHongShuCrawler()
    crawler.get_note_images = AsyncMock(return_value=None)
    crawler.get_notice_video = AsyncMock(return_value=None)

    await crawler.get_notice_media({"note_id": "disabled"})

    crawler.get_note_images.assert_not_awaited()
    crawler.get_notice_video.assert_not_awaited()


def test_xhs_asset_projection_excludes_avatar_and_chooses_one_url():
    assets = xhs_store._xhs_image_assets(
        {
            "image_list": [
                {
                    "url_default": "https://img.test/notes/a?format=webp",
                    "url_pre": "https://img.test/notes/a?format=jpg",
                }
            ],
            "user": {"avatar": "https://avatar.test/profile.jpg"},
        }
    )

    assert assets == [
        {
            "url": "https://img.test/notes/a?format=webp",
            "source_index": 0,
            "source_asset_key": "xhs:path:/notes/a",
        }
    ]
    assert "avatar" not in str(assets).lower()
