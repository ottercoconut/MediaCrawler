# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/easing.py
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


#!/usr/bin/env python
# -*- coding: utf-8 -*-
# copy from https://github.com/aneasystone/selenium-test/blob/master/12-slider-captcha.py
# thanks to aneasystone for his great work
# TripPostCollect：T06 改为根实现重导出或依赖装配；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。

from trippostcollect.platforms.douyin.login_support import (
    ease_in_quad as ease_in_quad, ease_out_quad as ease_out_quad,
    ease_out_quart as ease_out_quart, ease_out_expo as ease_out_expo,
    ease_out_bounce as ease_out_bounce, ease_out_elastic as ease_out_elastic,
    get_easing_tracks as get_tracks,
)

__all__ = [
    "ease_in_quad", "ease_out_quad", "ease_out_quart", "ease_out_expo",
    "ease_out_bounce", "ease_out_elastic", "get_tracks",
]
