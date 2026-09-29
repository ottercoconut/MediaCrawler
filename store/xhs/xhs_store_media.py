# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/xhs/xhs_store_media.py
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
# @Author  : helloteemo
# @Time    : 2024/7/11 22:35
# @Desc    : Xiaohongshu media storage
from trippostcollect.core.paths import MEDIACRAWLER_DIR

import pathlib
from pathlib import Path
from typing import Dict, List

import aiofiles

from base.base_crawler import AbstractStoreImage, AbstractStoreVideo
from tools import utils
from tools.image_manifest import (
    ImageAsset,
    failed_manifest_row,
    stage_post_images,
    upsert_manifest_rows_atomic,
    xhs_source_asset_key,
)
import config


class XiaoHongShuImage(AbstractStoreImage):
    def __init__(self):
        if config.SAVE_DATA_PATH:
            self.save_data_root = Path(config.SAVE_DATA_PATH)
        else:
            self.save_data_root = (MEDIACRAWLER_DIR / "data")
        self.platform_root = self.save_data_root / "xhs"
        self.image_store_path = self.platform_root / "images"
        self.manifest_path = self.platform_root / "image_manifest.jsonl"

    async def store_post_images(self, note_id: str, image_content_items: List[Dict]):
        assets = [
            ImageAsset(
                source_index=int(item["source_index"]),
                source_asset_key=xhs_source_asset_key(item["url"]),
                source_url=item["url"],
                content=item["content"],
                attempts=int(item.get("attempts") or 1),
                http_status=int(item.get("http_status") or 200),
            )
            for item in image_content_items
        ]
        rows = stage_post_images(
            save_data_root=self.save_data_root,
            platform_storage_key="xhs",
            platform_key="xhs",
            platform_post_id=note_id,
            source_key="image_list",
            assets=assets,
        )
        utils.logger.info(
            f"[XiaoHongShuImage.store_post_images] saved {len(rows)} "
            f"body images for note {note_id}"
        )
        return rows

    async def record_failure(self, note_id: str, image_content_item: Dict):
        row = failed_manifest_row(
            platform_key="xhs",
            platform_post_id=note_id,
            source_key="image_list",
            source_index=int(image_content_item["source_index"]),
            source_asset_key=xhs_source_asset_key(image_content_item["url"]),
            source_url=image_content_item["url"],
            attempts=int(image_content_item.get("attempts") or 1),
            error_code=str(
                image_content_item.get("error_code") or "image_download_retryable"
            ),
            http_status=image_content_item.get("http_status"),
        )
        upsert_manifest_rows_atomic(self.manifest_path, [row])
        return row


class XiaoHongShuVideo(AbstractStoreVideo):
    def __init__(self):
        if config.SAVE_DATA_PATH:
            self.video_store_path = f"{config.SAVE_DATA_PATH}/xhs/videos"
        else:
            self.video_store_path = "data/xhs/videos"

    async def store_video(self, video_content_item: Dict):
        """
        store content

        Args:
            video_content_item:

        Returns:

        """
        await self.save_video(video_content_item.get("notice_id"), video_content_item.get("video_content"), video_content_item.get("extension_file_name"))

    def make_save_file_name(self, notice_id: str, extension_file_name: str) -> str:
        """
        make save file name by store type

        Args:
            notice_id: notice id
            extension_file_name: video filename with extension

        Returns:

        """
        return f"{self.video_store_path}/{notice_id}/{extension_file_name}"

    async def save_video(self, notice_id: str, video_content: str, extension_file_name):
        """
        save video to local

        Args:
            notice_id: notice id
            video_content: video content
            extension_file_name: video filename with extension

        Returns:

        """
        pathlib.Path(self.video_store_path + "/" + notice_id).mkdir(parents=True, exist_ok=True)
        save_file_name = self.make_save_file_name(notice_id, extension_file_name)
        async with aiofiles.open(save_file_name, 'wb') as f:
            await f.write(video_content)
            utils.logger.info(f"[XiaoHongShuVideoStoreImplement.save_video] save video {save_file_name} success ...")
