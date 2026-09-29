"""知乎旧桥的图片暂存装配；算法统一由根共享暂存器提供。"""

# TripPostCollect：T07 薄装配；来源 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可见仓库 LICENSE。

from __future__ import annotations

from trippostcollect.core.paths import MEDIACRAWLER_DIR

from pathlib import Path
from typing import Dict, List

import config
from base.base_crawler import AbstractStoreImage
from tools import utils
from trippostcollect.platforms.zhihu.parser import zhihu_source_asset_key
from trippostcollect.artifacts.image_staging import PostImageStager


class ZhihuStoreImage(PostImageStager, AbstractStoreImage):
    def __init__(self):
        super().__init__(
            save_data_root=Path(config.SAVE_DATA_PATH) if config.SAVE_DATA_PATH else MEDIACRAWLER_DIR / "data",
            platform="zhihu",
            source_key="image_list",
            source_asset_key=lambda item: zhihu_source_asset_key(item["url"]),
            log_saved=lambda count, content_id: utils.logger.info(
                f"[ZhihuStoreImage.store_post_images] saved {count} "
                f"body images for content {content_id}"
            ),
        )

    async def store_post_images(self, content_id: str, image_content_items: List[Dict]):
        return await PostImageStager.store_post_images(self, content_id, image_content_items)

    async def record_failure(self, content_id: str, image_content_item: Dict):
        return await PostImageStager.record_failure(self, content_id, image_content_item)
