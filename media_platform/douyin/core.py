# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/douyin/core.py
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

# TripPostCollect：资源与 profile 基目录不再依赖进程 cwd。
# TripPostCollect：T06 改为根实现重导出或依赖装配；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。

import config
from trippostcollect.platforms import entry
from trippostcollect.platforms.douyin.core import DouYinCrawler as RootDouYinCrawler
from trippostcollect.platforms.douyin.models import DouyinImageDownloadError as DouyinImageDownloadError

# 与新入口共用装配函数；每次无参构造读取 fork 已解析的 config。
DouYinCrawler = RootDouYinCrawler.bind(lambda: entry.douyin_dependencies(config))
