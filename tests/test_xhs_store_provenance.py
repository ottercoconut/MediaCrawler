from __future__ import annotations

import config
import pytest
from store import xhs as xhs_store


@pytest.mark.asyncio
async def test_jsonl_store_preserves_note_detail_provenance(monkeypatch) -> None:
    captured: dict = {}

    class Store:
        async def store_content(self, content_item):
            captured.update(content_item)

    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "jsonl")
    monkeypatch.setattr(
        xhs_store.XhsStoreFactory,
        "create_store",
        staticmethod(lambda: Store()),
    )
    await xhs_store.update_xhs_note(
        {
            "note_id": "note-1",
            "type": "normal",
            "title": "title",
            "desc": "complete body",
            "content_detail_status": "detail_observed",
            "content_detail_source": "note_detail",
            "user": {"user_id": "author-1", "nickname": "author"},
            "creator_profile": {"fans_count": 10},
            "interact_info": {},
            "image_list": [{"url_default": "https://sns-img.test/body.jpg"}],
        }
    )

    assert captured["content_detail_status"] == "detail_observed"
    assert captured["content_detail_source"] == "note_detail"
    assert captured["desc"] == "complete body"
