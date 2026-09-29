# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/crawler_util.py
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
# @Time    : 2023/12/2 12:53
# @Desc    : Crawler utility functions

import json
import random
import re
import urllib
import urllib.parse
from typing import Dict, List, Optional, Tuple, cast

import httpx
from playwright.async_api import BrowserContext, Cookie, Page

from trippostcollect.runtime.login_helpers import (
    find_login_qrcode as _find_login_qrcode,
    find_qrcode_img_from_canvas as find_qrcode_img_from_canvas,
    show_qrcode as show_qrcode,
)
from . import utils
from .httpx_util import make_async_client

from trippostcollect.runtime.cookies import (
    convert_cookies as convert_cookies,
    convert_browser_context_cookies as convert_browser_context_cookies,
    convert_str_cookie_to_dict as convert_str_cookie_to_dict,
)
from trippostcollect.runtime.helpers import (
    get_user_agent as get_user_agent,
    get_mobile_user_agent as get_mobile_user_agent,
    extract_text_from_html as extract_text_from_html,
    extract_url_params_to_dict as extract_url_params_to_dict,
)


async def find_login_qrcode(page: Page, selector: str) -> str:
    return await _find_login_qrcode(
        page, selector, make_async_client=make_async_client, get_user_agent=get_user_agent,
    )


def match_interact_info_count(count_str: str) -> int:
    if not count_str:
        return 0

    match = re.search(r'\d+', count_str)
    if match:
        number = match.group()
        return int(number)
    else:
        return 0


def format_proxy_info(ip_proxy_info) -> Tuple[Optional[Dict], Optional[str]]:
    """format proxy info for playwright and httpx"""
    # fix circular import issue
    from proxy.proxy_ip_pool import IpInfoModel
    ip_proxy_info = cast(IpInfoModel, ip_proxy_info)

    # Playwright proxy server should be in format "host:port" without protocol prefix
    server = f"{ip_proxy_info.ip}:{ip_proxy_info.port}"
    
    playwright_proxy = {
        "server": server,
    }
    
    # Only add username and password if they are not empty
    if ip_proxy_info.user and ip_proxy_info.password:
        playwright_proxy["username"] = ip_proxy_info.user
        playwright_proxy["password"] = ip_proxy_info.password
    
    # httpx 0.28.1 requires passing proxy URL string directly, not a dictionary
    if ip_proxy_info.user and ip_proxy_info.password:
        httpx_proxy = f"http://{ip_proxy_info.user}:{ip_proxy_info.password}@{ip_proxy_info.ip}:{ip_proxy_info.port}"
    else:
        httpx_proxy = f"http://{ip_proxy_info.ip}:{ip_proxy_info.port}"
    return playwright_proxy, httpx_proxy
