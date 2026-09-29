# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/weibo/__init__.py
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
# @Author  : relakkes@gmail.com
# @Time    : 2024/1/14 21:34
# @Desc    :

# TripPostCollect T05：原实现固定于 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303；原位仅重导出或注入根实现。
import re
from functools import partial
from types import SimpleNamespace

from trippostcollect.platforms.weibo.core import WeiboCrawler
from trippostcollect.platforms.weibo.parser import (
    _first_present as _first_present,
    _weibo_pic_url as _weibo_pic_url,
    _weibo_pic_urls as _weibo_pic_urls,
    _weibo_pic_assets as _weibo_pic_assets,
    persisted_weibo_content_text as persisted_weibo_content_text,
)
from typing import Dict, List

import config
from base.base_crawler import AbstractStore
from tools import utils
from tools.user_hash import anonymize_user_id, mask_nickname
from var import source_keyword_var

from .weibo_store_media import WeiboStoreImage
from ._store_impl import (
    WeiboCsvStoreImplement,
    WeiboDbStoreImplement,
    WeiboExcelStoreImplement,
    WeiboJsonlStoreImplement,
    WeiboJsonStoreImplement,
    WeiboMongoStoreImplement,
    WeiboSqliteStoreImplement,
)


class WeibostoreFactory:
    STORES = {
        "csv": WeiboCsvStoreImplement,
        "db": WeiboDbStoreImplement,
        "postgres": WeiboDbStoreImplement,
        "json": WeiboJsonStoreImplement,
        "jsonl": WeiboJsonlStoreImplement,
        "sqlite": WeiboSqliteStoreImplement,
        "mongodb": WeiboMongoStoreImplement,
        "excel": WeiboExcelStoreImplement,
    }

    @staticmethod
    def create_store() -> AbstractStore:
        store_class = WeibostoreFactory.STORES.get(config.SAVE_DATA_OPTION)
        if not store_class:
            raise ValueError("[WeibotoreFactory.create_store] Invalid save option only supported csv or db or json or sqlite or mongodb or excel ...")
        return store_class()


class _StoreContext:
    """给根写出方法注入旧 store 的配置、ContextVar 和工厂，不包含业务处理。"""

    config = config
    source_keyword = property(lambda self: source_keyword_var.get())
    ports = SimpleNamespace(
        current_timestamp=lambda: utils.get_current_timestamp(),
        store_factory=lambda: WeibostoreFactory.create_store(),
        image_stager=lambda: WeiboStoreImage(),
    )


update_weibo_note = partial(WeiboCrawler.update_weibo_note, _StoreContext())


async def batch_update_weibo_notes(note_list: List[Dict]):
    """
    Batch update weibo notes
    Args:
        note_list:

    Returns:

    """
    if not note_list:
        return
    for note_item in note_list:
        await update_weibo_note(note_item)


async def batch_update_weibo_note_comments(note_id: str, comments: List[Dict]):
    """
    Batch update weibo note comments
    Args:
        note_id:
        comments:

    Returns:

    """
    if not comments:
        return
    for comment_item in comments:
        await update_weibo_note_comment(note_id, comment_item)


async def update_weibo_note_comment(note_id: str, comment_item: Dict):
    """
    Update weibo note comment
    Args:
        note_id: weibo note id
        comment_item: weibo comment item

    Returns:

    """
    if not comment_item or not note_id:
        return
    comment_id = str(comment_item.get("id"))
    user_info: Dict = comment_item.get("user") or {}
    content_text = comment_item.get("text")
    clean_text = re.sub(r"<.*?>", "", content_text)
    # 教学版：原始 user_id 匿名化为 creator_hash，昵称脱敏；
    # 不采集头像/主页链接/性别/IP 归属地等可定位真人的信息。
    save_comment_item = {
        "comment_id": comment_id,
        "create_time": utils.rfc2822_to_timestamp(comment_item.get("created_at")),
        "create_date_time": str(utils.rfc2822_to_china_datetime(comment_item.get("created_at"))),
        "note_id": note_id,
        "content": clean_text,
        "sub_comment_count": str(comment_item.get("total_number", 0)),
        "comment_like_count": str(comment_item.get("like_count", 0)),
        "last_modify_ts": utils.get_current_timestamp(),
        "parent_comment_id": comment_item.get("rootid", ""),

        # 创作者信息（匿名化/脱敏，不含原始 user_id/avatar/gender/profile_url/ip_location）
        "creator_hash": anonymize_user_id(user_info.get("id")),
        "nickname": mask_nickname(user_info.get("screen_name", "")),
    }
    utils.logger.info(f"[store.weibo.update_weibo_note_comment] Weibo note comment: {comment_id}, content: {save_comment_item.get('content', '')[:24]} ...")
    await WeibostoreFactory.create_store().store_comment(comment_item=save_comment_item)


async def update_weibo_note_images(note_id: str, image_content_items: List[Dict]):
    return await WeiboCrawler.update_weibo_note_images(_StoreContext(), note_id, image_content_items)


async def record_weibo_note_image_failure(note_id: str, image_content_item: Dict):
    return await WeiboCrawler.record_weibo_note_image_failure(_StoreContext(), note_id, image_content_item)


async def save_creator(user_id: str, user_info: Dict):
    """
    Save creator information to local
    教学版：为防骚扰不再采集/持久化创作者个人信息（昵称/性别/头像/简介/IP/粉丝数等），
    此入口保留为空操作以兼容调用方。user_id 仅在调用方局部用于抓取该创作者的微博。
    Args:
        user_id:
        user_info:

    Returns:

    """
    # 教学版：创作者个人信息均不采集不持久化
    return
