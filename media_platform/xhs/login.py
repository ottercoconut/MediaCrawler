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
import time
from typing import Optional

from playwright.async_api import BrowserContext, Page
from tenacity import (RetryError, retry, retry_if_result, stop_after_attempt,
                      wait_fixed)

import config
from base.base_crawler import AbstractLogin
from cache.cache_factory import CacheFactory
from tools import utils


class XiaoHongShuLogin(AbstractLogin):

    _MIN_QR_REFRESH_SECONDS = 180
    _DEFAULT_MANUAL_LOGIN_SECONDS = 600
    _DEFAULT_LOGIN_POLL_SECONDS = 1.0
    _DEFAULT_STABLE_LOGIN_SECONDS = 5.0
    _QR_COMPONENT_REFRESH_RETRY_SECONDS = 5.0
    _TERMINAL_SECURITY_MARKERS = frozenset({
        "操作频繁",
        "安全限制",
        "账号异常",
        "Account exception",
        "300011",
        "website-login/error",
    })
    _LOGIN_OR_QR_TEXTS = (
        "扫码登录",
        "二维码",
        "打开小红书扫一扫",
        "手机号登录",
    )
    _STRONG_MANUAL_PROGRESS_TEXTS = (
        "已扫码",
        "请在手机上确认",
        "请在小红书App确认",
        "请在小红书 APP 确认",
        "手机确认",
        "等待确认",
        "请通过验证",
        "安全验证",
        "身份验证",
        "滑块验证",
        "拖动滑块",
        "SMS Verification",
        "Parameter error",
    )
    _CONDITIONAL_MANUAL_PROGRESS_TEXTS = (
        "确认登录",
        "登录确认",
        "验证码",
    )
    _QR_EXPIRED_TEXTS = (
        "二维码已失效",
        "二维码已过期",
        "点击刷新",
        "重新获取二维码",
    )
    _CONDITIONAL_MANUAL_PROGRESS_SELECTORS = (
        "input[autocomplete='one-time-code']",
        "input[placeholder*='验证码']",
    )
    _STRONG_MANUAL_PROGRESS_SELECTORS = (
        "input[placeholder*='安全验证']",
        "[class*='captcha'] input",
        "[class*='verify'] input",
        "[class*='verification'] input",
        "[class*='slider']",
        "[class*='captcha'] canvas",
    )
    _MANUAL_PROGRESS_SELECTORS = (
        _CONDITIONAL_MANUAL_PROGRESS_SELECTORS
        + _STRONG_MANUAL_PROGRESS_SELECTORS
    )
    _QR_COMPONENT_REFRESH_SELECTORS = (
        "xpath=//*[normalize-space()='点击刷新']",
        "xpath=//*[normalize-space()='重新获取二维码']",
    )
    _PROFILE_SELECTORS = (
        "xpath=//a[contains(@href, '/user/profile/')]"
        "[.//*[normalize-space()='我'] or normalize-space()='我']",
        "xpath=//*[self::a or self::button]"
        "[.//*[normalize-space()='我'] or normalize-space()='我']",
        "xpath=//*[normalize-space()='我']",
    )
    _QRCODE_SELECTOR = "xpath=//img[contains(concat(' ', normalize-space(@class), ' '), ' qrcode-img ')]"

    def __init__(self,
                 login_type: str,
                 browser_context: BrowserContext,
                 context_page: Page,
                 login_phone: Optional[str] = "",
                 cookie_str: str = "",
                 ):
        config.LOGIN_TYPE = login_type
        self.browser_context = browser_context
        self.context_page = context_page
        self.login_phone = login_phone
        self.cookie_str = cookie_str
        self._last_login_observation: dict[str, object] = {}

    async def _single_login_page(self) -> Page:
        """Return the active login tab without closing verification popups.

        Login/security tabs are owned by the current BrowserContext and may be
        required for an operator to finish the same login.  Cleanup belongs to
        the crawler's guarded shutdown, never to the login polling loop.
        """
        try:
            pages = [page for page in self.browser_context.pages if not page.is_closed()]
        except Exception:
            pages = []

        page = (
            self.context_page
            if self.context_page in pages
            else (pages[-1] if pages else await self.browser_context.new_page())
        )
        if len(pages) > 1:
            utils.logger.info(
                "[XiaoHongShuLogin] Preserving all login/verification tabs "
                f"until login completes: {len(pages)} open tab(s)."
            )
        self.context_page = page
        return page

    async def _login_pages(self) -> list[Page]:
        try:
            pages = [page for page in self.browser_context.pages if not page.is_closed()]
        except Exception:
            pages = []
        if not pages:
            pages = [await self._single_login_page()]
        return pages

    @staticmethod
    async def _selector_is_visible(page: Page, selector: str) -> bool:
        frames = getattr(page, "frames", None)
        targets = list(frames) if frames else [page]
        for target in targets:
            try:
                if await target.locator(selector).is_visible(timeout=500):
                    return True
            except Exception:
                continue
        try:
            return bool(await page.is_visible(selector, timeout=500))
        except Exception:
            return False

    @classmethod
    async def _any_selector_is_visible(
        cls,
        page: Page,
        selectors: tuple[str, ...],
    ) -> bool:
        for selector in selectors:
            if await cls._selector_is_visible(page, selector):
                return True
        return False

    @staticmethod
    async def _visible_page_text(page: Page) -> str:
        """Read rendered text, excluding hidden fallback DOM in real pages."""
        parts: list[str] = []
        frames = getattr(page, "frames", None)
        targets = list(frames) if frames else [page]
        locator_supported = False
        for target in targets:
            locator = getattr(target, "locator", None)
            if not callable(locator):
                continue
            locator_supported = True
            try:
                body = locator("body")
                if await body.count() > 0:
                    parts.append(await body.inner_text(timeout=750))
            except Exception:
                continue
        if locator_supported:
            return "\n".join(parts)

        # Lightweight test doubles and older Page shims may not implement
        # Locator. Playwright pages always use the rendered-text branch above.
        try:
            return await page.content()
        except Exception:
            return ""

    async def _page_login_observation(self, page: Page) -> dict[str, object]:
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        text = await self._visible_page_text(page)
        terminal_markers = {
            marker for marker in self._TERMINAL_SECURITY_MARKERS
            if marker != "website-login/error" and marker in text
        }
        if "/website-login/error" in url:
            terminal_markers.add("website-login/error")

        qr_visible = await self._selector_is_visible(page, self._QRCODE_SELECTOR)
        conditional_progress_control_visible = await self._any_selector_is_visible(
            page,
            self._CONDITIONAL_MANUAL_PROGRESS_SELECTORS,
        )
        strong_progress_control_visible = await self._any_selector_is_visible(
            page,
            self._STRONG_MANUAL_PROGRESS_SELECTORS,
        )
        progress_control_visible = bool(
            conditional_progress_control_visible
            or strong_progress_control_visible
        )
        strong_progress = {
            marker for marker in self._STRONG_MANUAL_PROGRESS_TEXTS if marker in text
        }
        conditional_progress = {
            marker for marker in self._CONDITIONAL_MANUAL_PROGRESS_TEXTS if marker in text
        }
        qr_expired = {marker for marker in self._QR_EXPIRED_TEXTS if marker in text}
        # Generic words such as "验证码" may be present as an alternative login
        # method on the untouched or expired QR page. A generic code input has
        # the same ambiguity; it becomes progress only after the QR checkpoint
        # has left the visible UI. CAPTCHA/slider controls remain strong proof.
        manual_in_progress = bool(
            strong_progress
            or strong_progress_control_visible
            or (
                (conditional_progress or conditional_progress_control_visible)
                and not qr_visible
                and not qr_expired
            )
        )
        login_or_qr = {
            marker for marker in self._LOGIN_OR_QR_TEXTS if marker in text
        }
        profile_visible = await self._any_selector_is_visible(
            page,
            self._PROFILE_SELECTORS,
        )
        return {
            "page": page,
            "url": url,
            "terminal": sorted(terminal_markers),
            "manual_progress": sorted(strong_progress | conditional_progress),
            "manual_control_visible": progress_control_visible,
            "manual_in_progress": manual_in_progress,
            "login_or_qr": sorted(login_or_qr),
            "qr_visible": qr_visible,
            "qr_expired": sorted(qr_expired),
            "profile_visible": profile_visible,
        }

    async def _login_observation(self) -> dict[str, object]:
        page_observations = [
            await self._page_login_observation(page)
            for page in await self._login_pages()
        ]
        terminal = sorted({
            marker
            for item in page_observations
            for marker in item["terminal"]
        })
        manual_in_progress = any(
            bool(item["manual_in_progress"]) for item in page_observations
        )
        profile_pages = [
            item for item in page_observations if item["profile_visible"]
        ]
        progress_pages = [
            item for item in page_observations if item["manual_in_progress"]
        ]
        initial_checkpoint_pages = [
            item
            for item in page_observations
            if item["login_or_qr"] or item["qr_visible"]
        ]
        # An obsolete QR-only tab may remain open after the active page signs
        # in. It must not block success forever. Manual/verification progress on
        # any tab still wins, and a login overlay on the same profile page wins
        # over the stale profile shell beneath it.
        profile_page_checkpoint = any(
            bool(item["login_or_qr"] or item["qr_visible"])
            for item in profile_pages
        )
        visible_checkpoint = bool(
            progress_pages
            or profile_page_checkpoint
            or (not profile_pages and initial_checkpoint_pages)
        )
        active = (progress_pages or profile_pages or page_observations)[-1]
        active_page = active["page"]
        if manual_in_progress and active_page is not self.context_page:
            try:
                await active_page.bring_to_front()
            except Exception:
                pass
            self.context_page = active_page
        return {
            "terminal": terminal,
            "manual_in_progress": manual_in_progress,
            "visible_checkpoint": visible_checkpoint,
            "profile_visible": bool(profile_pages),
            "qr_visible": any(bool(item["qr_visible"]) for item in page_observations),
            "qr_expired": any(bool(item["qr_expired"]) for item in page_observations),
            "pages": page_observations,
        }

    @staticmethod
    def _is_pure_qr_observation(observation: dict[str, object]) -> bool:
        if (
            observation.get("terminal")
            or observation.get("manual_in_progress")
            or observation.get("profile_visible")
        ):
            return False
        pages = observation.get("pages") or []
        return bool(
            observation.get("qr_visible")
            or any(item.get("login_or_qr") for item in pages)
        )

    @classmethod
    def _is_pure_expired_qr_observation(
        cls,
        observation: dict[str, object],
    ) -> bool:
        if not cls._is_pure_qr_observation(observation):
            return False
        return bool(
            observation.get("qr_expired")
            or any(item.get("qr_expired") for item in observation.get("pages") or [])
        )

    async def _click_expired_qr_component_refresh(
        self,
        observation: dict[str, object],
    ) -> bool:
        """Click only an explicit refresh control on the observed expired QR page."""
        expired_pages = [
            item["page"]
            for item in observation.get("pages") or []
            if item.get("qr_expired")
        ]
        for page in reversed(expired_pages):
            frames = getattr(page, "frames", None)
            targets = list(frames) if frames else [page]
            for target in targets:
                locator_factory = getattr(target, "locator", None)
                if not callable(locator_factory):
                    continue
                for selector in self._QR_COMPONENT_REFRESH_SELECTORS:
                    try:
                        candidate = locator_factory(selector)
                        locator = getattr(candidate, "first", candidate)
                        if not await locator.is_visible(timeout=500):
                            continue
                        await locator.click(timeout=5_000)
                        self.context_page = page
                        utils.logger.info(
                            "[XiaoHongShuLogin.login_by_qrcode] Expired pure QR "
                            "confirmed twice; clicked its component refresh control."
                        )
                        return True
                    except Exception:
                        continue
        return False

    async def _check_login_state_once(self, no_logged_in_session: str) -> bool:
        """
        Verify login status using dual-check: UI elements and Cookies.
        """
        observation = await self._login_observation()
        self._last_login_observation = observation
        terminal_markers = set(observation["terminal"])
        if terminal_markers:
            raise RuntimeError("xhs_platform_security_limit_300011")

        # A stale signed-in shell can remain behind an active login or security
        # overlay. Every visible checkpoint wins over every visible "我" entry.
        if observation["visible_checkpoint"]:
            page_summaries = [
                {
                    "url": item["url"],
                    "manual_progress": item["manual_progress"],
                    "manual_control_visible": item["manual_control_visible"],
                    "login_or_qr": item["login_or_qr"],
                    "qr_visible": item["qr_visible"],
                }
                for item in observation["pages"]
            ]
            utils.logger.info(
                "[XiaoHongShuLogin.check_login_state] Visible login/security checkpoint, "
                f"please verify manually: {page_summaries}"
            )
        elif observation["profile_visible"]:
            utils.logger.info(
                "[XiaoHongShuLogin.check_login_state] Login status confirmed by "
                "visible profile UI without a login/security checkpoint."
            )
            return True

        # Cookie changes are diagnostic evidence only. They cannot close the
        # browser before the visible page has reached a stable signed-in state.
        current_cookie = await self.browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(current_cookie)
        current_web_session = cookie_dict.get("web_session")
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
        except RetryError as exc:
            utils.logger.info("[XiaoHongShuLogin.login_by_mobile] Login xiaohongshu failed by mobile login method ...")
            raise RuntimeError("xhs_mobile_login_timeout") from exc

        wait_redirect_seconds = 5
        utils.logger.info(f"[XiaoHongShuLogin.login_by_mobile] Login successful then wait for {wait_redirect_seconds} seconds redirect ...")
        await asyncio.sleep(wait_redirect_seconds)

    async def login_by_qrcode(self):
        """login xiaohongshu website and keep webdriver login state"""
        utils.logger.info("[XiaoHongShuLogin.login_by_qrcode] Begin login xiaohongshu by qrcode ...")
        requested_refresh_seconds = int(
            os.environ.get(
                "TRIPPOSTCOLLECT_XHS_QR_REFRESH_SECONDS",
                str(self._MIN_QR_REFRESH_SECONDS),
            )
        )
        refresh_seconds = max(
            self._MIN_QR_REFRESH_SECONDS,
            requested_refresh_seconds,
        )
        if refresh_seconds != requested_refresh_seconds:
            utils.logger.warning(
                "[XiaoHongShuLogin.login_by_qrcode] QR refresh interval "
                f"{requested_refresh_seconds}s is unsafe; clamped to {refresh_seconds}s."
            )
        manual_wait_seconds = int(
            os.environ.get(
                "TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS",
                str(self._DEFAULT_MANUAL_LOGIN_SECONDS),
            )
        )
        if manual_wait_seconds <= 0:
            raise RuntimeError("xhs_qrcode_login_wait_disabled")
        poll_seconds = max(
            0.2,
            float(
                os.environ.get(
                    "TRIPPOSTCOLLECT_XHS_LOGIN_POLL_SECONDS",
                    str(self._DEFAULT_LOGIN_POLL_SECONDS),
                )
            ),
        )
        stable_seconds = max(
            poll_seconds,
            float(
                os.environ.get(
                    "TRIPPOSTCOLLECT_XHS_STABLE_LOGIN_SECONDS",
                    str(self._DEFAULT_STABLE_LOGIN_SECONDS),
                )
            ),
        )

        current_cookie = await self.browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(current_cookie)
        no_logged_in_session = cookie_dict.get("web_session")

        started_at = time.monotonic()
        deadline = started_at + manual_wait_seconds
        next_refresh_at: Optional[float] = None
        progress_latched = False
        qrcode_displayed = False
        refresh_count = 0
        stable_started_at: Optional[float] = None
        component_refresh_pending = False
        next_component_refresh_retry_at: Optional[float] = None

        utils.logger.info(
            "[XiaoHongShuLogin.login_by_qrcode] Waiting for operator login: "
            f"manual_deadline={manual_wait_seconds}s, qr_refresh_interval={refresh_seconds}s."
        )
        while time.monotonic() < deadline:
            logged_in = await self._check_login_state_once(no_logged_in_session)
            observation = self._last_login_observation
            if observation.get("manual_in_progress") and not progress_latched:
                progress_latched = True
                utils.logger.info(
                    "[XiaoHongShuLogin.login_by_qrcode] Manual login/verification "
                    "progress observed; disabling QR reload for the remainder of "
                    "this bounded login window."
                )
            if logged_in and not progress_latched:
                progress_latched = True
                utils.logger.info(
                    "[XiaoHongShuLogin.login_by_qrcode] Signed-in UI observed; "
                    "disabling every QR refresh path while stability is confirmed."
                )

            now = time.monotonic()
            if now >= deadline:
                break
            if logged_in:
                if stable_started_at is None:
                    stable_started_at = now
                    utils.logger.info(
                        "[XiaoHongShuLogin.login_by_qrcode] Signed-in UI observed; "
                        f"requiring {stable_seconds:.1f}s stable confirmation."
                    )
                elif now - stable_started_at >= stable_seconds:
                    utils.logger.info(
                        "[XiaoHongShuLogin.login_by_qrcode] Login confirmed stable "
                        f"for {now - stable_started_at:.1f}s."
                    )
                    return
            else:
                stable_started_at = None

            if (
                component_refresh_pending
                and self._is_pure_qr_observation(observation)
                and not self._is_pure_expired_qr_observation(observation)
            ):
                # A component click produced a fresh QR. Start the full-page
                # reload floor from this new ready observation, not from the
                # expired image's original lifetime.
                component_refresh_pending = False
                next_component_refresh_retry_at = None
                qrcode_displayed = False
                next_refresh_at = None

            if not qrcode_displayed and not progress_latched and not logged_in:
                base64_qrcode_img = await utils.find_login_qrcode(
                    self.context_page,
                    selector=self._QRCODE_SELECTOR,
                )
                if not base64_qrcode_img:
                    utils.logger.info(
                        "[XiaoHongShuLogin.login_by_qrcode] QR code not found, "
                        "trying to open login dialog ..."
                    )
                    try:
                        login_button_ele = self.context_page.locator(
                            "xpath=//*[@id='app']/div[1]/div[2]/div[1]/ul/div[1]/button"
                        )
                        await login_button_ele.click(timeout=5_000)
                        await asyncio.sleep(0.5)
                    except Exception as exc:
                        utils.logger.warning(
                            "[XiaoHongShuLogin.login_by_qrcode] open login dialog "
                            f"failed: {exc}"
                        )
                    base64_qrcode_img = await utils.find_login_qrcode(
                        self.context_page,
                        selector=self._QRCODE_SELECTOR,
                    )
                if base64_qrcode_img and not observation.get("qr_expired"):
                    browser_headless = bool(
                        config.CDP_HEADLESS
                        if config.ENABLE_CDP_MODE
                        else config.HEADLESS
                    )
                    if browser_headless:
                        partial_show_qrcode = functools.partial(
                            utils.show_qrcode,
                            base64_qrcode_img,
                        )
                        asyncio.get_running_loop().run_in_executor(
                            executor=None,
                            func=partial_show_qrcode,
                        )
                    qrcode_displayed = True
                    next_refresh_at = time.monotonic() + refresh_seconds
                    utils.logger.info(
                        "[XiaoHongShuLogin.login_by_qrcode] QR code ready in "
                        f"{'headless preview' if browser_headless else 'browser'}; "
                        f"automatic page reload is not allowed before {refresh_seconds}s."
                    )

            now = time.monotonic()
            if (
                not progress_latched
                and next_refresh_at is None
                and observation.get("qr_visible")
                and not observation.get("qr_expired")
            ):
                # The page may render a valid QR even when extracting its bytes
                # for the terminal preview fails. Start its lifetime from this
                # first visible observation, never from process startup.
                next_refresh_at = now + refresh_seconds

            if (
                not progress_latched
                and not logged_in
                and self._is_pure_expired_qr_observation(observation)
                and (
                    not component_refresh_pending
                    or next_component_refresh_retry_at is None
                    or now >= next_component_refresh_retry_at
                )
            ):
                # An expired QR can appear before the 180-second full-page
                # floor. Confirm the same fail-safe state twice, then operate
                # only the explicit control inside that QR component.
                confirmed_logged_in = await self._check_login_state_once(
                    no_logged_in_session
                )
                confirmed_observation = self._last_login_observation
                observation = confirmed_observation
                if confirmed_observation.get("manual_in_progress"):
                    progress_latched = True
                    utils.logger.info(
                        "[XiaoHongShuLogin.login_by_qrcode] Manual progress "
                        "appeared while confirming QR expiry; component refresh "
                        "cancelled and permanently disabled."
                    )
                elif confirmed_logged_in:
                    progress_latched = True
                    utils.logger.info(
                        "[XiaoHongShuLogin.login_by_qrcode] Signed-in UI appeared "
                        "while confirming QR expiry; component refresh cancelled."
                    )
                elif self._is_pure_expired_qr_observation(
                    confirmed_observation
                ):
                    component_clicked = (
                        await self._click_expired_qr_component_refresh(
                            confirmed_observation
                        )
                    )
                    if component_clicked:
                        component_refresh_pending = True
                        next_component_refresh_retry_at = (
                            time.monotonic()
                            + self._QR_COMPONENT_REFRESH_RETRY_SECONDS
                        )
                        qrcode_displayed = False

            if (
                not progress_latched
                and not component_refresh_pending
                and next_refresh_at is not None
                and now >= next_refresh_at
                and now < deadline
            ):
                # Close the scan-at-expiry race: the normal poll above and this
                # immediate second observation must both still be a pure QR
                # state. A scan/verification transition between them latches the
                # manual flow and permanently disables periodic reload.
                refresh_allowed = self._is_pure_qr_observation(observation)
                if refresh_allowed:
                    confirmed_logged_in = await self._check_login_state_once(
                        no_logged_in_session
                    )
                    confirmed_observation = self._last_login_observation
                    if confirmed_observation.get("manual_in_progress"):
                        progress_latched = True
                        utils.logger.info(
                            "[XiaoHongShuLogin.login_by_qrcode] Manual progress "
                            "appeared at the QR refresh boundary; reload cancelled."
                        )
                    refresh_allowed = bool(
                        not confirmed_logged_in
                        and not progress_latched
                        and self._is_pure_qr_observation(confirmed_observation)
                    )
                if refresh_allowed and time.monotonic() < deadline:
                    refresh_count += 1
                    utils.logger.info(
                        "[XiaoHongShuLogin.login_by_qrcode] QR code not confirmed; "
                        f"refreshing after at least {refresh_seconds}s "
                        f"(refresh {refresh_count})."
                    )
                    try:
                        await self.context_page.reload(
                            wait_until="domcontentloaded",
                            timeout=30_000,
                        )
                    except Exception as exc:
                        utils.logger.warning(
                            "[XiaoHongShuLogin.login_by_qrcode] page reload failed: "
                            f"{exc}"
                        )
                    qrcode_displayed = False
                    next_refresh_at = None
                    component_refresh_pending = False
                    next_component_refresh_retry_at = None
                elif not progress_latched:
                    next_refresh_at = time.monotonic() + poll_seconds
                    utils.logger.info(
                        "[XiaoHongShuLogin.login_by_qrcode] QR refresh deferred: "
                        "two consecutive pure-QR observations were not available."
                    )

            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(poll_seconds, remaining))

        raise RuntimeError(
            "xhs_qrcode_login_timeout: "
            f"operator login did not stabilize within {manual_wait_seconds}s"
        )

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
