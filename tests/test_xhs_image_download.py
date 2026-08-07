from __future__ import annotations

from io import BytesIO
import json
from unittest.mock import AsyncMock

from PIL import Image
import pytest

import config
from media_platform.xhs.core import XiaoHongShuCrawler, XHSImageDownloadError


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (6, 4), color="green").save(output, format="PNG")
    return output.getvalue()


def image_note(note_id: str = "xhs-note") -> dict:
    return {
        "note_id": note_id,
        "image_list": [
            {
                "url_default": "https://sns-img-a.test/notes_pre_post/asset-one?format=jpg",
                "url": "https://sns-img-b.test/notes_pre_post/asset-one?format=webp",
                "url_pre": "https://sns-img-c.test/notes_pre_post/asset-one?format=avif",
            }
        ],
        "user": {
            "avatar": "https://sns-avatar.test/author.jpg",
            "user_id": "author",
        },
    }


@pytest.mark.asyncio
async def test_one_image_object_downloads_one_authoritative_url_with_true_format(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("media_platform.xhs.core.random.random", lambda: 0)
    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_note_media.return_value = png_bytes()

    await crawler.get_note_images(image_note())

    crawler.xhs_client.get_note_media.assert_awaited_once_with(
        "https://sns-img-a.test/notes_pre_post/asset-one?format=jpg"
    )
    assert (
        tmp_path / "xhs" / "images" / "xhs-note" / "000.png"
    ).read_bytes() == png_bytes()
    rows = [
        json.loads(line)
        for line in (tmp_path / "xhs" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["source_asset_key"] == "xhs:path:/notes_pre_post/asset-one"
    assert rows[0]["mime_type"] == "image/png"
    assert rows[0]["staging_path"] == "xhs/images/xhs-note/000.png"
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.asyncio
async def test_xhs_failed_image_writes_no_success_file_or_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("media_platform.xhs.core.random.random", lambda: 0)
    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_note_media.return_value = None

    with pytest.raises(XHSImageDownloadError):
        await crawler.get_note_images(image_note("xhs-failed"))

    rows = [
        json.loads(line)
        for line in (tmp_path / "xhs" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["fetch_status"] == "failed"
    assert rows[0]["staging_path"] is None
    assert not (tmp_path / "xhs" / "images" / "xhs-failed").exists()


@pytest.mark.asyncio
async def test_equivalent_xhs_asset_variants_are_deduplicated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("media_platform.xhs.core.random.random", lambda: 0)
    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_note_media.return_value = png_bytes()
    note = image_note("dedupe-note")
    note["image_list"].append(
        {"url_default": "http://sns-img-z.test/notes_pre_post/asset-one?format=png"}
    )

    await crawler.get_note_images(note)

    crawler.xhs_client.get_note_media.assert_awaited_once()
    files = list((tmp_path / "xhs" / "images" / "dedupe-note").iterdir())
    assert [path.name for path in files] == ["000.png"]
