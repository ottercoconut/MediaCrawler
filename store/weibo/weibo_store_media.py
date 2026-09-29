# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/weibo/weibo_store_media.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

# -*- coding: utf-8 -*-
# @Author  : Erm
# @Time    : 2024/4/9 17:35
# @Desc    : Weibo media storage
from trippostcollect.core.paths import MEDIACRAWLER_DIR

from pathlib import Path
from typing import Dict, List

from base.base_crawler import AbstractStoreImage
from tools import utils
from tools.image_manifest import (
    ImageAsset,
    failed_manifest_row,
    stage_post_images,
    upsert_manifest_rows_atomic,
    weibo_source_asset_key,
)
import config


class WeiboStoreImage(AbstractStoreImage):
    def __init__(self):
        if config.SAVE_DATA_PATH:
            self.save_data_root = Path(config.SAVE_DATA_PATH)
        else:
            self.save_data_root = (MEDIACRAWLER_DIR / "data")
        self.platform_root = self.save_data_root / "weibo"
        self.image_store_path = self.platform_root / "images"
        self.manifest_path = self.platform_root / "image_manifest.jsonl"

    async def store_post_images(self, note_id: str, image_content_items: List[Dict]):
        assets = [
            ImageAsset(
                source_index=int(item["source_index"]),
                source_asset_key=weibo_source_asset_key(item.get("pid"), item["url"]),
                source_url=item["url"],
                content=item["content"],
                attempts=int(item.get("attempts") or 1),
                http_status=int(item.get("http_status") or 200),
            )
            for item in image_content_items
        ]
        rows = stage_post_images(
            save_data_root=self.save_data_root,
            platform_storage_key="weibo",
            platform_key="weibo",
            platform_post_id=note_id,
            source_key="image_list",
            assets=assets,
        )
        utils.logger.info(
            f"[WeiboImageStoreImplement.store_post_images] saved {len(rows)} "
            f"body images for note {note_id}"
        )
        return rows

    async def record_failure(self, note_id: str, image_content_item: Dict):
        row = failed_manifest_row(
            platform_key="weibo",
            platform_post_id=note_id,
            source_key="image_list",
            source_index=int(image_content_item["source_index"]),
            source_asset_key=weibo_source_asset_key(
                image_content_item.get("pid"), image_content_item["url"]
            ),
            source_url=image_content_item["url"],
            attempts=int(image_content_item.get("attempts") or 1),
            error_code=str(
                image_content_item.get("error_code") or "image_download_retryable"
            ),
            http_status=image_content_item.get("http_status"),
        )
        upsert_manifest_rows_atomic(self.manifest_path, [row])
        return row
