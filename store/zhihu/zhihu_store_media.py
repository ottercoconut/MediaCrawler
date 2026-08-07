"""TripPostCollect Zhihu body-image staging and manifest storage."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import config
from base.base_crawler import AbstractStoreImage
from tools import utils
from tools.image_manifest import (
    ImageAsset,
    failed_manifest_row,
    stage_post_images,
    upsert_manifest_rows_atomic,
    zhihu_source_asset_key,
)


class ZhihuStoreImage(AbstractStoreImage):
    def __init__(self):
        self.save_data_root = (
            Path(config.SAVE_DATA_PATH) if config.SAVE_DATA_PATH else Path("data")
        )
        self.platform_root = self.save_data_root / "zhihu"
        self.image_store_path = self.platform_root / "images"
        self.manifest_path = self.platform_root / "image_manifest.jsonl"

    async def store_post_images(self, content_id: str, image_content_items: List[Dict]):
        assets = [
            ImageAsset(
                source_index=int(item["source_index"]),
                source_asset_key=zhihu_source_asset_key(item["url"]),
                source_url=item["url"],
                content=item["content"],
                attempts=int(item.get("attempts") or 1),
                http_status=int(item.get("http_status") or 200),
            )
            for item in image_content_items
        ]
        rows = stage_post_images(
            save_data_root=self.save_data_root,
            platform_storage_key="zhihu",
            platform_key="zhihu",
            platform_post_id=content_id,
            source_key="image_list",
            assets=assets,
        )
        utils.logger.info(
            f"[ZhihuStoreImage.store_post_images] saved {len(rows)} "
            f"body images for content {content_id}"
        )
        return rows

    async def record_failure(self, content_id: str, image_content_item: Dict):
        row = failed_manifest_row(
            platform_key="zhihu",
            platform_post_id=content_id,
            source_key="image_list",
            source_index=int(image_content_item["source_index"]),
            source_asset_key=zhihu_source_asset_key(image_content_item["url"]),
            source_url=image_content_item["url"],
            attempts=int(image_content_item.get("attempts") or 1),
            error_code=str(
                image_content_item.get("error_code") or "image_download_retryable"
            ),
            http_status=image_content_item.get("http_status"),
        )
        upsert_manifest_rows_atomic(self.manifest_path, [row])
        return row
