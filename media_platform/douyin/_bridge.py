"""抖音旧导入路径的依赖装配；业务方法全部继承根实现。"""

import config
from trippostcollect.platforms import entry
from trippostcollect.platforms.douyin.client import DouYinClient as RootDouYinClient
from trippostcollect.platforms.douyin.login import DouYinLogin as RootDouYinLogin


class DouYinClient(RootDouYinClient):
    def __init__(self, timeout=60, proxy=None, *, headers, playwright_page,
                 cookie_dict, proxy_ip_pool=None):
        dependencies = entry.douyin_dependencies(config)
        super().__init__(
            timeout=timeout, proxy=proxy, headers=headers,
            playwright_page=playwright_page, cookie_dict=cookie_dict,
            ports=dependencies["ports"].client,
            browser_detail_fallback=dependencies["ports"].browser_detail_fallback,
            browser_detail_timeout=dependencies["inputs"].browser_detail_timeout,
        )


class DouYinLogin(RootDouYinLogin):
    def __init__(self, login_type, browser_context, context_page,
                 login_phone="", cookie_str=""):
        super().__init__(
            login_type=login_type, browser_context=browser_context,
            context_page=context_page, login_phone=login_phone,
            cookie_str=cookie_str, ports=entry.douyin_dependencies(config)["ports"].login,
        )
