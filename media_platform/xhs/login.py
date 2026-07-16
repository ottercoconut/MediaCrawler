# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/login.py
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


import asyncio
import functools
import os
import sys
from typing import Optional

from playwright.async_api import BrowserContext, Page
from tenacity import (RetryError, retry, retry_if_result, stop_after_attempt,
                      wait_fixed)

import config
from base.base_crawler import AbstractLogin
from cache.cache_factory import CacheFactory
from tools import utils


class XiaoHongShuLogin(AbstractLogin):

    def __init__(self,
                 login_type: str,
                 browser_context: BrowserContext,
                 context_page: Page,
                 login_phone: Optional[str] = "",
                 cookie_str: str = ""
                 ):
        config.LOGIN_TYPE = login_type
        self.browser_context = browser_context
        self.context_page = context_page
        self.login_phone = login_phone
        self.cookie_str = cookie_str

    async def _single_login_page(self) -> Page:
        """Close stale tabs while login is pending, retaining the login page."""
        try:
            pages = [page for page in self.browser_context.pages if not page.is_closed()]
        except Exception:
            pages = []

        page = self.context_page if self.context_page in pages else (pages[0] if pages else await self.browser_context.new_page())
        closed_count = 0
        for other_page in pages:
            if other_page is page:
                continue
            await other_page.close()
            closed_count += 1
        if closed_count:
            utils.logger.info(
                "[XiaoHongShuLogin] Retained one login tab and closed "
                f"{closed_count} stale tab(s)."
            )
        self.context_page = page
        return page

    async def _check_login_state_once(self, no_logged_in_session: str) -> bool:
        """
        Verify login status using dual-check: UI elements and Cookies.
        """
        # 1. Priority check: Check if the "Me" (Profile) node appears in the sidebar
        page = await self._single_login_page()

        user_profile_selector = "xpath=//a[contains(@href, '/user/profile/')]//span[text()='我']"
        security_texts = ("请通过验证", "安全验证", "验证码", "身份验证", "操作频繁", "环境异常", "风险")
        login_texts = ("扫码登录", "二维码", "打开小红书扫一扫", "确认登录", "登录确认", "手机号登录")
        try:
            # Selector for elements containing "Me" text with a link pointing to the profile.
            is_visible = await page.is_visible(user_profile_selector, timeout=500)
            if is_visible:
                utils.logger.info("[XiaoHongShuLogin.check_login_state] Login status confirmed by UI element ('Me' button).")
                return True
        except Exception:
            pass
        try:
            content = await page.content()
        except Exception:
            content = ""
        markers = sorted({text for text in (*security_texts, *login_texts) if text in content})

        # 2. Check for CAPTCHA/security/login prompts on the single login page.
        if markers:
            utils.logger.info(
                "[XiaoHongShuLogin.check_login_state] Visible login/security checkpoint, "
                f"please verify manually: {{'url': {page.url}, 'markers': {markers}}}"
            )

        # 3. Compatibility fallback: Original Cookie-based change detection
        current_cookie = await self.browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(current_cookie)
        current_web_session = cookie_dict.get("web_session")
        
        # web_session may change after a security verification while the web UI is
        # still logged out. Treat it as evidence only; the logged-in sidebar is
        # the success condition that prevents premature browser shutdown.
        if no_logged_in_session and current_web_session and current_web_session != no_logged_in_session:
            utils.logger.info(
                "[XiaoHongShuLogin.check_login_state] web_session changed, "
                "waiting for logged-in UI before confirming login."
            )

        return False

    @retry(stop=stop_after_attempt(600), wait=wait_fixed(1), retry=retry_if_result(lambda value: value is False))
    async def check_login_state(self, no_logged_in_session: str) -> bool:
        return await self._check_login_state_once(no_logged_in_session)

    async def wait_login_state(self, no_logged_in_session: str, seconds: int) -> bool:
        for _ in range(max(1, seconds)):
            if await self._check_login_state_once(no_logged_in_session):
                return True
            await asyncio.sleep(1)
        return False

    async def begin(self):
        """Start login xiaohongshu"""
        utils.logger.info("[XiaoHongShuLogin.begin] Begin login xiaohongshu ...")
        await self._single_login_page()
        if config.LOGIN_TYPE == "qrcode":
            await self.login_by_qrcode()
        elif config.LOGIN_TYPE == "phone":
            await self.login_by_mobile()
        elif config.LOGIN_TYPE == "cookie":
            await self.login_by_cookies()
        else:
            raise ValueError("[XiaoHongShuLogin.begin]I nvalid Login Type Currently only supported qrcode or phone or cookies ...")

    async def login_by_mobile(self):
        """Login xiaohongshu by mobile"""
        utils.logger.info("[XiaoHongShuLogin.login_by_mobile] Begin login xiaohongshu by mobile ...")
        await asyncio.sleep(1)
        try:
            # After entering Xiaohongshu homepage, the login dialog may not pop up automatically, need to manually click login button
            login_button_ele = await self.context_page.wait_for_selector(
                selector="xpath=//*[@id='app']/div[1]/div[2]/div[1]/ul/div[1]/button",
                timeout=5000
            )
            await login_button_ele.click()
            # The login dialog has two forms: one shows phone number and verification code directly
            # The other requires clicking to switch to phone login
            element = await self.context_page.wait_for_selector(
                selector='xpath=//div[@class="login-container"]//div[@class="other-method"]/div[1]',
                timeout=5000
            )
            await element.click()
        except Exception:
            utils.logger.info("[XiaoHongShuLogin.login_by_mobile] have not found mobile button icon and keep going ...")

        await asyncio.sleep(1)
        login_container_ele = await self.context_page.wait_for_selector("div.login-container")
        input_ele = await login_container_ele.query_selector("label.phone > input")
        await input_ele.fill(self.login_phone)
        await asyncio.sleep(0.5)

        send_btn_ele = await login_container_ele.query_selector("label.auth-code > span")
        await send_btn_ele.click()  # Click to send verification code
        sms_code_input_ele = await login_container_ele.query_selector("label.auth-code > input")
        submit_btn_ele = await login_container_ele.query_selector("div.input-container > button")
        cache_client = CacheFactory.create_cache(config.CACHE_TYPE_MEMORY)
        max_get_sms_code_time = 60 * 2  # Maximum time to get verification code is 2 minutes
        no_logged_in_session = ""
        while max_get_sms_code_time > 0:
            utils.logger.info(f"[XiaoHongShuLogin.login_by_mobile] get sms code from redis remaining time {max_get_sms_code_time}s ...")
            await asyncio.sleep(1)
            sms_code_key = f"xhs_{self.login_phone}"
            sms_code_value = cache_client.get(sms_code_key)
            if not sms_code_value:
                max_get_sms_code_time -= 1
                continue

            current_cookie = await self.browser_context.cookies()
            _, cookie_dict = utils.convert_cookies(current_cookie)
            no_logged_in_session = cookie_dict.get("web_session")

            await sms_code_input_ele.fill(value=sms_code_value.decode())  # Enter SMS verification code
            await asyncio.sleep(0.5)
            agree_privacy_ele = self.context_page.locator("xpath=//div[@class='agreements']//*[local-name()='svg']")
            await agree_privacy_ele.click()  # Click to agree to privacy policy
            await asyncio.sleep(0.5)

            await submit_btn_ele.click()  # Click login

            # TODO: Should also check if the verification code is correct, as it may be incorrect
            break

        try:
            await self.check_login_state(no_logged_in_session)
        except RetryError:
            utils.logger.info("[XiaoHongShuLogin.login_by_mobile] Login xiaohongshu failed by mobile login method ...")
            sys.exit()

        wait_redirect_seconds = 5
        utils.logger.info(f"[XiaoHongShuLogin.login_by_mobile] Login successful then wait for {wait_redirect_seconds} seconds redirect ...")
        await asyncio.sleep(wait_redirect_seconds)

    async def login_by_qrcode(self):
        """login xiaohongshu website and keep webdriver login state"""
        utils.logger.info("[XiaoHongShuLogin.login_by_qrcode] Begin login xiaohongshu by qrcode ...")
        qrcode_img_selector = "xpath=//img[@class='qrcode-img']"
        refresh_seconds = int(os.environ.get("TRIPPOSTCOLLECT_XHS_QR_REFRESH_SECONDS", "90"))
        max_attempts = int(os.environ.get("TRIPPOSTCOLLECT_XHS_QR_ATTEMPTS", "5"))

        for attempt_no in range(1, max_attempts + 1):
            if attempt_no > 1:
                utils.logger.info(
                    f"[XiaoHongShuLogin.login_by_qrcode] QR code expired or not confirmed, refreshing ({attempt_no}/{max_attempts}) ..."
                )
                try:
                    await self.context_page.reload(wait_until="domcontentloaded", timeout=30_000)
                    await asyncio.sleep(1)
                except Exception as exc:
                    utils.logger.warning(f"[XiaoHongShuLogin.login_by_qrcode] page reload failed: {exc}")

            base64_qrcode_img = await utils.find_login_qrcode(
                self.context_page,
                selector=qrcode_img_selector
            )
            if not base64_qrcode_img:
                utils.logger.info("[XiaoHongShuLogin.login_by_qrcode] QR code not found, trying to open login dialog ...")
                try:
                    login_button_ele = self.context_page.locator("xpath=//*[@id='app']/div[1]/div[2]/div[1]/ul/div[1]/button")
                    await login_button_ele.click(timeout=5_000)
                    await asyncio.sleep(0.5)
                except Exception as exc:
                    utils.logger.warning(f"[XiaoHongShuLogin.login_by_qrcode] open login dialog failed: {exc}")
                base64_qrcode_img = await utils.find_login_qrcode(
                    self.context_page,
                    selector=qrcode_img_selector
                )
                if not base64_qrcode_img:
                    if await self._check_login_state_once(""):
                        break
                    continue

            current_cookie = await self.browser_context.cookies()
            _, cookie_dict = utils.convert_cookies(current_cookie)
            no_logged_in_session = cookie_dict.get("web_session")

            partial_show_qrcode = functools.partial(utils.show_qrcode, base64_qrcode_img)
            asyncio.get_running_loop().run_in_executor(executor=None, func=partial_show_qrcode)

            utils.logger.info(
                f"[XiaoHongShuLogin.login_by_qrcode] waiting for scan code login, "
                f"attempt {attempt_no}/{max_attempts}, refresh in {refresh_seconds}s"
            )
            if await self.wait_login_state(no_logged_in_session, refresh_seconds):
                break
        else:
            utils.logger.info("[XiaoHongShuLogin.login_by_qrcode] Login xiaohongshu failed by qrcode login method ...")
            sys.exit()

        wait_redirect_seconds = 5
        utils.logger.info(f"[XiaoHongShuLogin.login_by_qrcode] Login successful then wait for {wait_redirect_seconds} seconds redirect ...")
        await asyncio.sleep(wait_redirect_seconds)

    async def login_by_cookies(self):
        """login xiaohongshu website by cookies"""
        utils.logger.info("[XiaoHongShuLogin.login_by_cookies] Begin login xiaohongshu by cookie ...")
        for key, value in utils.convert_str_cookie_to_dict(self.cookie_str).items():
            if key != "web_session":  # Only set web_session cookie attribute
                continue
            await self.browser_context.add_cookies([{
                'name': key,
                'value': value,
                'domain': ".rednote.com" if config.XHS_INTERNATIONAL else ".xiaohongshu.com",
                'path': "/"
            }])
