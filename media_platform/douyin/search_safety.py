# TripPostCollect：T06 改为根实现重导出或依赖装配；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。

from trippostcollect.platforms.douyin.parser import (
    decode_douyin_json_body as decode_douyin_json_body,
    validate_douyin_search_response as validate_douyin_search_response,
    classify_empty_first_page as classify_empty_first_page,
    DOUYIN_RESULT_LINK_SELECTOR as DOUYIN_RESULT_LINK_SELECTOR,
    DOUYIN_NO_RESULT_MARKERS as DOUYIN_NO_RESULT_MARKERS,
)
from trippostcollect.platforms.douyin.client import inspect_empty_first_page as inspect_empty_first_page
