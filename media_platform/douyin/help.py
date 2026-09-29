# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/douyin/help.py
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
# @Name: Programmer Ajiang-Relakkes
# @Time    : 2024/6/10 02:24
# @Desc    : Get a_bogus parameter, for learning and communication only, do not use for commercial purposes, contact author to delete if infringement

# TripPostCollect：T06 改为根实现重导出或依赖装配；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。
from functools import partial
from trippostcollect.platforms.douyin.client import get_web_id as _get_web_id
from trippostcollect.platforms.douyin.parser import parse_video_info_from_url as parse_video_info_from_url
from trippostcollect.platforms.douyin.signer import (
    get_a_bogus as get_a_bogus, get_a_bogus_from_js as get_a_bogus_from_js,
    douyin_sign_obj as douyin_sign_obj,
)

import random
import re
from playwright.async_api import Page
from model.m_douyin import CreatorUrlInfo

get_web_id = partial(_get_web_id, random_source=lambda: random.random())


async def get_a_bogus_from_playwright(params: str, post_data: dict, user_agent: str, page: Page):
    """
    Get a_bogus parameter through playwright
    playwright version is deprecated
    Returns:

    """
    if not post_data:
        post_data = ""
    a_bogus = await page.evaluate(
        "([params, post_data, ua]) => window.bdms.init._v[2].p[42].apply(null, [0, 1, 8, params, post_data, ua])",
        [params, post_data, user_agent])

    return a_bogus


def parse_creator_info_from_url(url: str) -> CreatorUrlInfo:
    """
    Parse creator ID (sec_user_id) from Douyin creator homepage URL
    Supports the following formats:
    1. Creator homepage: https://www.douyin.com/user/MS4wLjABAAAATJPY7LAlaa5X-c8uNdWkvz0jUGgpw4eeXIwu_8BhvqE?from_tab_name=main
    2. Pure ID: MS4wLjABAAAATJPY7LAlaa5X-c8uNdWkvz0jUGgpw4eeXIwu_8BhvqE

    Args:
        url: Douyin creator homepage link or sec_user_id
    Returns:
        CreatorUrlInfo: Object containing creator ID
    """
    # If it's a pure ID format (usually starts with MS4wLjABAAAA), return directly
    if url.startswith("MS4wLjABAAAA") or (not url.startswith("http") and "douyin.com" not in url):
        return CreatorUrlInfo(sec_user_id=url)

    # Extract sec_user_id from creator homepage URL: /user/xxx
    user_pattern = r'/user/([^/?]+)'
    match = re.search(user_pattern, url)
    if match:
        sec_user_id = match.group(1)
        return CreatorUrlInfo(sec_user_id=sec_user_id)

    raise ValueError(f"Unable to parse creator ID from URL: {url}")


if __name__ == '__main__':
    # Test video URL parsing
    print("=== Video URL Parsing Test ===")
    test_urls = [
        "https://www.douyin.com/video/7525082444551310602",
        "https://www.douyin.com/user/MS4wLjABAAAATJPY7LAlaa5X-c8uNdWkvz0jUGgpw4eeXIwu_8BhvqE?from_tab_name=main&modal_id=7525082444551310602",
        "https://www.douyin.com/root/search/python?aid=b733a3b0-4662-4639-9a72-c2318fba9f3f&modal_id=7471165520058862848&type=general",
        "7525082444551310602",
    ]
    for url in test_urls:
        try:
            result = parse_video_info_from_url(url)
            print(f"✓ URL: {url[:80]}...")
            print(f"  Result: {result}\n")
        except Exception as e:
            print(f"✗ URL: {url}")
            print(f"  Error: {e}\n")

    # Test creator URL parsing
    print("=== Creator URL Parsing Test ===")
    test_creator_urls = [
        "https://www.douyin.com/user/MS4wLjABAAAATJPY7LAlaa5X-c8uNdWkvz0jUGgpw4eeXIwu_8BhvqE?from_tab_name=main",
        "MS4wLjABAAAATJPY7LAlaa5X-c8uNdWkvz0jUGgpw4eeXIwu_8BhvqE",
    ]
    for url in test_creator_urls:
        try:
            result = parse_creator_info_from_url(url)
            print(f"✓ URL: {url[:80]}...")
            print(f"  Result: {result}\n")
        except Exception as e:
            print(f"✗ URL: {url}")
            print(f"  Error: {e}\n")
