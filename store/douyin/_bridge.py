"""抖音旧 store 函数的上下文装配；投影和写出只调用根 crawler。"""

from dataclasses import replace

import config
from var import crawler_type_var, source_keyword_var
from trippostcollect.platforms import entry
from trippostcollect.platforms.douyin.core import DouYinCrawler


async def update_douyin_aweme(aweme_item):
    from . import DouyinStoreFactory

    dependencies = entry.douyin_dependencies(config)
    dependencies["ports"] = replace(
        dependencies["ports"], content_sink=lambda _: DouyinStoreFactory.create_store(),
    )
    crawler = DouYinCrawler(**dependencies)
    crawler.source_keyword = source_keyword_var.get()
    crawler.crawler_type = crawler_type_var.get()
    return await crawler.update_douyin_aweme(aweme_item)
