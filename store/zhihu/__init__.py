# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/zhihu/__init__.py
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
from typing import List
from functools import partial
from trippostcollect.platforms.zhihu.parser import zhihu_content_image_assets as zhihu_content_image_assets
from trippostcollect.platforms.zhihu.core import (
    store_zhihu_content,
    update_zhihu_content_images as _update_images,
    record_zhihu_content_image_failure as _record_image_failure,
)

import config
from base.base_crawler import AbstractStore
from model.m_zhihu import ZhihuComment, ZhihuContent, ZhihuCreator
from ._store_impl import (ZhihuCsvStoreImplement,
                                          ZhihuDbStoreImplement,
                                          ZhihuJsonStoreImplement,
                                          ZhihuJsonlStoreImplement,
                                          ZhihuSqliteStoreImplement,
                                          ZhihuMongoStoreImplement,
                                          ZhihuExcelStoreImplement)
from tools import utils
from var import source_keyword_var
from .zhihu_store_media import ZhihuStoreImage




class ZhihuStoreFactory:
    STORES = {
        "csv": ZhihuCsvStoreImplement,
        "db": ZhihuDbStoreImplement,
        "postgres": ZhihuDbStoreImplement,
        "json": ZhihuJsonStoreImplement,
        "jsonl": ZhihuJsonlStoreImplement,
        "sqlite": ZhihuSqliteStoreImplement,
        "mongodb": ZhihuMongoStoreImplement,
        "excel": ZhihuExcelStoreImplement,
    }

    @staticmethod
    def create_store() -> AbstractStore:
        store_class = ZhihuStoreFactory.STORES.get(config.SAVE_DATA_OPTION)
        if not store_class:
            raise ValueError("[ZhihuStoreFactory.create_store] Invalid save option only supported csv or db or json or sqlite or mongodb or excel ...")
        return store_class()

async def batch_update_zhihu_contents(contents: List[ZhihuContent]):
    """
    Batch update Zhihu contents
    Args:
        contents:

    Returns:

    """
    if not contents:
        return

    for content_item in contents:
        await update_zhihu_content(content_item)








async def batch_update_zhihu_note_comments(comments: List[ZhihuComment]):
    """
    Batch update Zhihu content comments
    Args:
        comments:

    Returns:

    """
    if not comments:
        return

    for comment_item in comments:
        await update_zhihu_content_comment(comment_item)


async def update_zhihu_content_comment(comment_item: ZhihuComment):
    """
    Update Zhihu content comment
    Args:
        comment_item:

    Returns:

    """
    local_db_item = comment_item.model_dump()
    local_db_item.update({"last_modify_ts": utils.get_current_timestamp()})
    utils.logger.info(f"[store.zhihu.update_zhihu_note_comment] zhihu content comment:{local_db_item}")
    await ZhihuStoreFactory.create_store().store_comment(local_db_item)


async def save_creator(creator: ZhihuCreator):
    """
    Save Zhihu creator information
    Args:
        creator:

    Returns:

    """
    if not creator:
        return
    local_db_item = creator.model_dump()
    local_db_item.update({"last_modify_ts": utils.get_current_timestamp()})
    await ZhihuStoreFactory.create_store().store_creator(local_db_item)

# TripPostCollect：T07 原位只绑定旧桥依赖；来源 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可见仓库 LICENSE。
update_zhihu_content = partial(
    store_zhihu_content, source_keyword=source_keyword_var.get,
    current_timestamp=utils.get_current_timestamp,
    content_sink_factory=ZhihuStoreFactory.create_store,
)
update_zhihu_content_images = partial(_update_images, image_stager_factory=ZhihuStoreImage)
record_zhihu_content_image_failure = partial(_record_image_failure, image_stager_factory=ZhihuStoreImage)
