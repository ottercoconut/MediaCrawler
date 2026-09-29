"""未迁平台的浏览器装配；配置在 manager 构造时快照，行为参数仍在使用时读取。"""

import config
from trippostcollect.runtime.browser import CDPBrowserManager as RuntimeCDPBrowserManager, CDPBrowserSettings
from trippostcollect.runtime.browser_launcher import BrowserLauncher as RuntimeBrowserLauncher
from .trippostcollect_behavior import project_browser_args


class BrowserLauncher(RuntimeBrowserLauncher):
    def __init__(self):
        super().__init__(project_browser_args=project_browser_args)


class CDPBrowserManager(RuntimeCDPBrowserManager):
    def __init__(self):
        super().__init__(
            CDPBrowserSettings(
                PLATFORM=config.PLATFORM,
                CDP_CONNECT_EXISTING=config.CDP_CONNECT_EXISTING,
                CDP_DEBUG_PORT=config.CDP_DEBUG_PORT,
                BROWSER_LAUNCH_TIMEOUT=config.BROWSER_LAUNCH_TIMEOUT,
                CUSTOM_BROWSER_PATH=config.CUSTOM_BROWSER_PATH,
                SAVE_LOGIN_STATE=config.SAVE_LOGIN_STATE,
                USER_DATA_DIR=config.USER_DATA_DIR,
                AUTO_CLOSE_BROWSER=config.AUTO_CLOSE_BROWSER,
            ),
            project_browser_args=project_browser_args,
        )
