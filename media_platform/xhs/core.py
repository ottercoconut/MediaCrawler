# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/core.py
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
import json
import os
import random
import time
from asyncio import Task
from typing import Dict, List, Optional

from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Page,
    Playwright,
    async_playwright,
)
from tenacity import RetryError

import config
from base.base_crawler import AbstractCrawler
from model.m_xiaohongshu import NoteUrlInfo, CreatorUrlInfo
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import xhs as xhs_store
from tools import utils
from tools.trippostcollect_adaptive import AdaptiveAccumulator
from tools.cdp_browser import CDPBrowserManager
from var import crawler_type_var, source_keyword_var

from .client import XiaoHongShuClient
from .exception import DataFetchError, NoteNotFoundError
from .field import SearchSortType
from .help import parse_note_info_from_note_url, parse_creator_info_from_url, get_search_id
from .login import XiaoHongShuLogin


class XiaoHongShuCrawler(AbstractCrawler):
    context_page: Page
    xhs_client: XiaoHongShuClient
    browser_context: BrowserContext
    cdp_manager: Optional[CDPBrowserManager]

    def __init__(self) -> None:
        self.index_url = "https://www.rednote.com" if config.XHS_INTERNATIONAL else "https://www.xiaohongshu.com"
        self.cookie_urls = [self.index_url]
        # self.user_agent = utils.get_user_agent()
        self.user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        self.cdp_manager = None
        self.ip_proxy_pool = None  # Proxy IP pool for automatic proxy refresh

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        try:
            return max(0.0, float(os.environ.get(name, str(default))))
        except ValueError:
            return default

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return max(0, int(os.environ.get(name, str(default))))
        except ValueError:
            return default

    def _storage_state_path(self) -> str:
        explicit_path = os.environ.get("TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH", "").strip()
        if explicit_path:
            return explicit_path
        profile_name = config.USER_DATA_DIR % config.PLATFORM
        return os.path.join(os.getcwd(), "browser_data", profile_name, "trippostcollect_storage_state.json")

    @staticmethod
    def _cookie_for_restore(cookie: Dict) -> Dict:
        allowed_keys = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
        restored = {key: value for key, value in cookie.items() if key in allowed_keys and value is not None}
        if restored.get("expires") == -1:
            restored.pop("expires", None)
        return restored

    async def _restore_storage_state(self) -> bool:
        snapshot_path = self._storage_state_path()
        if not snapshot_path or not os.path.isfile(snapshot_path):
            return False
        try:
            with open(snapshot_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            utils.logger.warning(f"[XiaoHongShuCrawler] Failed to load storage state {snapshot_path}: {exc}")
            return False

        cookies = [
            self._cookie_for_restore(cookie)
            for cookie in state.get("cookies", [])
            if isinstance(cookie, dict) and cookie.get("name") and cookie.get("value")
        ]
        if cookies:
            try:
                await self.browser_context.add_cookies(cookies)
                utils.logger.info(
                    f"[XiaoHongShuCrawler] Restored {len(cookies)} cookies from storage state: {snapshot_path}"
                )
            except Exception as exc:
                utils.logger.warning(f"[XiaoHongShuCrawler] Restore cookies failed: {exc}")

        storage_by_origin: Dict[str, Dict[str, Dict[str, str]]] = {}
        for origin_item in state.get("origins", []):
            if not isinstance(origin_item, dict):
                continue
            origin = origin_item.get("origin")
            if not origin:
                continue
            bucket = storage_by_origin.setdefault(origin, {"localStorage": {}, "sessionStorage": {}})
            for item in origin_item.get("localStorage", []):
                if isinstance(item, dict) and item.get("name") is not None and item.get("value") is not None:
                    bucket["localStorage"][str(item["name"])] = str(item["value"])

        for item in state.get("trippostcollect", {}).get("runtime_storage", []):
            if not isinstance(item, dict):
                continue
            origin = item.get("origin")
            if not origin:
                continue
            bucket = storage_by_origin.setdefault(origin, {"localStorage": {}, "sessionStorage": {}})
            for storage_key in ("localStorage", "sessionStorage"):
                values = item.get(storage_key)
                if not isinstance(values, dict):
                    continue
                for key, value in values.items():
                    if value is not None:
                        bucket[storage_key][str(key)] = str(value)

        if storage_by_origin:
            script_payload = json.dumps(storage_by_origin, ensure_ascii=False)
            await self.browser_context.add_init_script(
                script=f"""
                (() => {{
                  const storageByOrigin = {script_payload};
                  const state = storageByOrigin[location.origin];
                  if (!state) return;
                  for (const [key, value] of Object.entries(state.localStorage || {{}})) {{
                    try {{ window.localStorage.setItem(key, value); }} catch (_) {{}}
                  }}
                  for (const [key, value] of Object.entries(state.sessionStorage || {{}})) {{
                    try {{ window.sessionStorage.setItem(key, value); }} catch (_) {{}}
                  }}
                }})();
                """
            )
            utils.logger.info(
                f"[XiaoHongShuCrawler] Installed storage restore init script for {len(storage_by_origin)} origins"
            )
        return bool(cookies or storage_by_origin)

    async def _write_storage_state(self) -> None:
        snapshot_path = self._storage_state_path()
        if not snapshot_path:
            return
        try:
            state = await self.browser_context.storage_state()
            runtime_storage: List[Dict] = []
            for page in [page for page in self.browser_context.pages if not page.is_closed()]:
                url = page.url or ""
                if "xiaohongshu.com" not in url and "rednote.com" not in url:
                    continue
                try:
                    storage = await page.evaluate(
                        """
                        () => ({
                          origin: location.origin,
                          url: location.href,
                          localStorage: Object.fromEntries(Object.entries(window.localStorage || {})),
                          sessionStorage: Object.fromEntries(Object.entries(window.sessionStorage || {}))
                        })
                        """
                    )
                except Exception as exc:
                    storage = {"url": url, "error": f"{type(exc).__name__}: {exc}"}
                runtime_storage.append(storage)
            state["trippostcollect"] = {
                "platform": "xhs",
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "runtime_storage": runtime_storage,
            }
            os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
            with open(snapshot_path, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
            try:
                os.chmod(snapshot_path, 0o600)
            except OSError:
                pass
            utils.logger.info(f"[XiaoHongShuCrawler] Wrote storage state snapshot: {snapshot_path}")
        except Exception as exc:
            utils.logger.warning(f"[XiaoHongShuCrawler] Write storage state failed: {exc}")

    async def _activate_latest_xhs_page(self) -> None:
        """Use the newest Xiaohongshu/Rednote page when login opens an extra tab/window."""
        try:
            pages = [page for page in self.browser_context.pages if not page.is_closed()]
        except Exception:
            return
        for page in reversed(pages):
            url = page.url or ""
            if "xiaohongshu.com" in url or "rednote.com" in url:
                self.context_page = page
                return

    async def _profile_ui_visible(self) -> bool:
        try:
            await self._activate_latest_xhs_page()
            selector = "xpath=//a[contains(@href, '/user/profile/')]//span[text()='我']"
            return await self.context_page.locator(selector).count() > 0
        except Exception:
            return False

    async def _cookie_markers(self) -> Dict[str, bool]:
        try:
            current_cookie = await self.browser_context.cookies(self.cookie_urls)
            _, cookie_dict = utils.convert_cookies(current_cookie)
        except Exception:
            cookie_dict = {}
        return {
            "web_session": bool(cookie_dict.get("web_session")),
            "a1": bool(cookie_dict.get("a1")),
            "webId": bool(cookie_dict.get("webId")),
            "gid": bool(cookie_dict.get("gid")),
        }

    async def _visible_checkpoint_markers(self) -> Dict[str, object]:
        markers = {
            "security": [],
            "login_or_qr": [],
            "pages": [],
        }
        security_texts = ("请通过验证", "安全验证", "验证码", "身份验证", "操作频繁", "环境异常", "风险")
        login_texts = ("扫码登录", "二维码", "打开小红书扫一扫", "确认登录", "登录确认", "手机号登录")
        try:
            pages = [page for page in self.browser_context.pages if not page.is_closed()]
        except Exception:
            pages = [self.context_page]
        for page in pages:
            page_info: Dict[str, object] = {"url": page.url}
            try:
                content = await page.content()
            except Exception:
                content = ""
            security = sorted({text for text in security_texts if text in content})
            login_or_qr = sorted({text for text in login_texts if text in content})
            if security:
                markers["security"] = sorted(set(markers["security"]) | set(security))  # type: ignore[arg-type]
            if login_or_qr:
                markers["login_or_qr"] = sorted(set(markers["login_or_qr"]) | set(login_or_qr))  # type: ignore[arg-type]
            page_info["security"] = security
            page_info["login_or_qr"] = login_or_qr
            markers["pages"].append(page_info)  # type: ignore[union-attr]
        return markers

    async def _wait_for_initial_page_settle(self) -> None:
        settle_seconds = self._env_float("TRIPPOSTCOLLECT_XHS_INITIAL_SETTLE_SECONDS", 12.0)
        try:
            await self.context_page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            pass
        try:
            await self.context_page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        if settle_seconds > 0:
            utils.logger.info(
                f"[XiaoHongShuCrawler] Waiting {settle_seconds:.1f}s for Xiaohongshu web startup settle ..."
            )
            await asyncio.sleep(settle_seconds)
        await self._activate_latest_xhs_page()

    async def _wait_for_manual_checkpoint_if_needed(self) -> bool:
        wait_seconds = self._env_int("TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS", 0)
        if wait_seconds <= 0:
            return False

        utils.logger.info(
            f"[XiaoHongShuCrawler] Login state is not ready; waiting up to {wait_seconds}s "
            "for visible security/login confirmation ..."
        )
        started = time.monotonic()
        last_print = 0.0
        while time.monotonic() - started < wait_seconds:
            await self._activate_latest_xhs_page()
            cookie_markers = await self._cookie_markers()
            profile_ui = await self._profile_ui_visible()
            if profile_ui:
                utils.logger.info(
                    "[XiaoHongShuCrawler] Login/session markers became ready after manual checkpoint wait: "
                    f"profile_ui={profile_ui}, cookies={cookie_markers}"
                )
                return True

            now = time.monotonic()
            if now - last_print >= 10:
                checkpoint_markers = await self._visible_checkpoint_markers()
                utils.logger.info(
                    "[XiaoHongShuCrawler] Waiting for Xiaohongshu checkpoint: "
                    f"profile_ui={profile_ui}, cookies={cookie_markers}, visible={checkpoint_markers}"
                )
                last_print = now
            await asyncio.sleep(2)
        return False

    async def start(self) -> None:
        playwright_proxy_format, httpx_proxy_format = None, None
        if config.ENABLE_IP_PROXY:
            self.ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
            ip_proxy_info: IpInfoModel = await self.ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = utils.format_proxy_info(ip_proxy_info)

        async with async_playwright() as playwright:
            # Choose launch mode based on configuration
            if config.ENABLE_CDP_MODE:
                utils.logger.info("[XiaoHongShuCrawler] Launching browser using CDP mode")
                self.browser_context = await self.launch_browser_with_cdp(
                    playwright,
                    playwright_proxy_format,
                    self.user_agent,
                    headless=config.CDP_HEADLESS,
                )
                if self.cdp_manager:
                    await self.cdp_manager.add_stealth_script()
            else:
                utils.logger.info("[XiaoHongShuCrawler] Launching browser using standard mode")
                # Launch a browser context.
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(
                    chromium,
                    playwright_proxy_format,
                    self.user_agent,
                    headless=config.HEADLESS,
                )
                # stealth.min.js is a js script to prevent the website from detecting the crawler.
                await self.browser_context.add_init_script(path="libs/stealth.min.js")

            await self._restore_storage_state()
            self.context_page = await self.browser_context.new_page()
            await self.context_page.goto(self.index_url, wait_until="domcontentloaded", timeout=60_000)
            await self._wait_for_initial_page_settle()

            # Create a client to interact with the Xiaohongshu website.
            self.xhs_client = await self.create_xhs_client(httpx_proxy_format)
            if not await self.xhs_client.pong():
                checkpoint_ready = False
                if await self._wait_for_manual_checkpoint_if_needed():
                    await self.xhs_client.update_cookies(
                        browser_context=self.browser_context,
                        urls=self.cookie_urls,
                    )
                    checkpoint_ready = await self.xhs_client.pong()
                if not checkpoint_ready:
                    login_obj = XiaoHongShuLogin(
                        login_type=config.LOGIN_TYPE,
                        login_phone="",  # input your phone number
                        browser_context=self.browser_context,
                        context_page=self.context_page,
                        cookie_str=config.COOKIES,
                    )
                    await login_obj.begin()
                    await self.xhs_client.update_cookies(
                        browser_context=self.browser_context,
                        urls=self.cookie_urls,
                    )
                    if not await self.xhs_client.pong():
                        raise RuntimeError("[XiaoHongShuCrawler] Xiaohongshu login state not confirmed after login flow")

            await self._write_storage_state()
            crawler_type_var.set(config.CRAWLER_TYPE)
            if config.CRAWLER_TYPE == "search":
                # Search for notes and retrieve their comment information.
                await self.search()
            elif config.CRAWLER_TYPE == "detail":
                # Get the information and comments of the specified post
                await self.get_specified_notes()
            elif config.CRAWLER_TYPE == "creator":
                # Get creator's information and their notes and comments
                await self.get_creators_and_notes()
            else:
                pass

            utils.logger.info("[XiaoHongShuCrawler.start] Xhs Crawler finished ...")

    async def search(self) -> None:
        """Search for notes and retrieve their comment information."""
        utils.logger.info("[XiaoHongShuCrawler.search] Begin search Xiaohongshu keywords")
        candidate_hard_limit = max(1, int(config.CRAWLER_MAX_NOTES_COUNT or 1))
        accumulator = AdaptiveAccumulator.from_environment("xhs", candidate_hard_limit)
        start_page = config.START_PAGE
        for keyword in config.KEYWORDS.split(","):
            source_keyword_var.set(keyword)
            utils.logger.info(f"[XiaoHongShuCrawler.search] Current search keyword: {keyword}")
            page = 1
            search_id = get_search_id()
            while accumulator.candidate_count < accumulator.hard_limit and not accumulator.stop_reason:
                if page < start_page:
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Skip page {page}")
                    page += 1
                    continue

                try:
                    utils.logger.info(f"[XiaoHongShuCrawler.search] search Xiaohongshu keyword: {keyword}, page: {page}")
                    requested_page = page
                    note_ids: List[str] = []
                    xsec_tokens: List[str] = []
                    notes_res = await self.xhs_client.get_note_by_keyword(
                        keyword=keyword,
                        search_id=search_id,
                        page=page,
                        sort=(SearchSortType(config.SORT_TYPE) if config.SORT_TYPE != "" else SearchSortType.GENERAL),
                    )
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Search notes response: {notes_res}")
                    if not notes_res:
                        utils.logger.info("[XiaoHongShuCrawler.search] No more content!")
                        accumulator.mark_source_exhausted(
                            "empty_response",
                            source_page=requested_page,
                            source_cursor=search_id,
                            raw_batch_count=0,
                        )
                        break
                    semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
                    remaining = max(0, accumulator.hard_limit - accumulator.candidate_count)
                    raw_items = list(notes_res.get("items") or [])
                    source_has_more = notes_res.get("has_more") if "has_more" in notes_res else None
                    post_items = [
                        post_item
                        for post_item in raw_items
                        if post_item.get("model_type") not in ("rec_query", "hot_query")
                    ][:remaining]
                    if not post_items:
                        accumulator.begin_batch()
                        if accumulator.finish_batch(
                            source_page=requested_page,
                            source_cursor=search_id,
                            source_has_more=source_has_more,
                            raw_batch_count=len(raw_items),
                        ):
                            break
                        if source_has_more in (False, 0):
                            accumulator.mark_source_exhausted(
                                "has_more_false",
                                source_page=requested_page,
                                source_cursor=search_id,
                                source_has_more=source_has_more,
                                raw_batch_count=len(raw_items),
                            )
                            break
                        page += 1
                        await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                        continue
                    task_list = [
                        self.get_note_detail_async_task(
                            note_id=post_item.get("id"),
                            xsec_source=post_item.get("xsec_source"),
                            xsec_token=post_item.get("xsec_token"),
                            semaphore=semaphore,
                        ) for post_item in post_items
                    ]
                    note_details = await asyncio.gather(*task_list)
                    accumulator.begin_batch()
                    for post_item, note_detail in zip(post_items, note_details):
                        identity = str((note_detail or {}).get("note_id") or post_item.get("id") or "")
                        if note_detail:
                            if self.is_video_note(note_detail):
                                utils.logger.info(
                                    f"[XiaoHongShuCrawler.search] Skip video note, note_id: {note_detail.get('note_id')}"
                                )
                                if accumulator.consider(identity, valid=False):
                                    break
                                continue
                            await self.enrich_note_creator(note_detail)
                            creator_profile = note_detail.get("creator_profile") or {}
                            creator_item = xhs_store._normalized_creator_item(
                                (note_detail.get("user") or {}).get("user_id", ""),
                                creator_profile,
                            ) if creator_profile else {}
                            followers_observed = any(
                                creator_item.get(key) not in (None, "")
                                for key in ("fans_count", "fans")
                            )
                            interact_info = note_detail.get("interact_info") or {}
                            valid = bool(
                                identity
                                and (note_detail.get("title") or note_detail.get("desc"))
                                and note_detail.get("time")
                                and (note_detail.get("user") or {}).get("user_id")
                                and (note_detail.get("user") or {}).get("nickname")
                                and note_detail.get("image_list")
                                and followers_observed
                                and all(key in interact_info for key in ("liked_count", "collected_count", "comment_count", "share_count"))
                            )
                            should_stop = accumulator.consider(identity, valid=valid)
                            await xhs_store.update_xhs_note(note_detail)
                            await self.get_notice_media(note_detail)
                            note_ids.append(note_detail.get("note_id"))
                            xsec_tokens.append(note_detail.get("xsec_token"))
                            if should_stop:
                                break
                        elif accumulator.consider(identity, valid=False):
                            break
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Note detail summaries: {self.note_detail_summaries(note_details)}")
                    await self.batch_get_note_comments(note_ids, xsec_tokens)
                    if accumulator.finish_batch(
                        source_page=requested_page,
                        source_cursor=search_id,
                        source_has_more=source_has_more,
                        raw_batch_count=len(raw_items),
                    ):
                        break
                    if source_has_more in (False, 0):
                        accumulator.mark_source_exhausted(
                            "has_more_false",
                            source_page=requested_page,
                            source_cursor=search_id,
                            source_has_more=source_has_more,
                            raw_batch_count=len(raw_items),
                        )
                        break
                    page += 1

                    # Sleep after each page navigation
                    await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after page {page-1}")
                except DataFetchError:
                    utils.logger.error("[XiaoHongShuCrawler.search] Get note detail error")
                    accumulator.mark_runtime_failed(
                        "search_or_detail_request_failed",
                        source_page=page,
                        source_cursor=search_id,
                    )
                    break

    async def get_creators_and_notes(self) -> None:
        """Get creator's notes and retrieve their comment information."""
        utils.logger.info("[XiaoHongShuCrawler.get_creators_and_notes] Begin get Xiaohongshu creators")
        for creator_url in config.XHS_CREATOR_ID_LIST:
            try:
                # Parse creator URL to get user_id and security tokens
                creator_info: CreatorUrlInfo = parse_creator_info_from_url(creator_url)
                utils.logger.info(f"[XiaoHongShuCrawler.get_creators_and_notes] Parse creator URL info: {creator_info}")
                user_id = creator_info.user_id

                # get creator detail info from web html content
                createor_info: Dict = await self.xhs_client.get_creator_info(
                    user_id=user_id,
                    xsec_token=creator_info.xsec_token,
                    xsec_source=creator_info.xsec_source
                )
                if createor_info:
                    await xhs_store.save_creator(user_id, creator=createor_info)
            except ValueError as e:
                utils.logger.error(f"[XiaoHongShuCrawler.get_creators_and_notes] Failed to parse creator URL: {e}")
                continue

            # Use fixed crawling interval
            crawl_interval = config.CRAWLER_MAX_SLEEP_SEC
            # Get all note information of the creator
            all_notes_list = await self.xhs_client.get_all_notes_by_creator(
                user_id=user_id,
                crawl_interval=crawl_interval,
                callback=self.fetch_creator_notes_detail,
                xsec_token=creator_info.xsec_token,
                xsec_source=creator_info.xsec_source,
            )

            note_ids = []
            xsec_tokens = []
            for note_item in all_notes_list:
                note_ids.append(note_item.get("note_id"))
                xsec_tokens.append(note_item.get("xsec_token"))
            await self.batch_get_note_comments(note_ids, xsec_tokens)

    async def fetch_creator_notes_detail(self, note_list: List[Dict]):
        """Concurrently obtain the specified post list and save the data"""
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [
            self.get_note_detail_async_task(
                note_id=post_item.get("note_id"),
                xsec_source=post_item.get("xsec_source"),
                xsec_token=post_item.get("xsec_token"),
                semaphore=semaphore,
            ) for post_item in note_list
        ]

        note_details = await asyncio.gather(*task_list)
        for note_detail in note_details:
            if note_detail:
                if self.is_video_note(note_detail):
                    utils.logger.info(
                        f"[XiaoHongShuCrawler.fetch_creator_notes_detail] Skip video note, note_id: {note_detail.get('note_id')}"
                    )
                    continue
                await self.enrich_note_creator(note_detail)
                await xhs_store.update_xhs_note(note_detail)
                await self.get_notice_media(note_detail)

    async def get_specified_notes(self):
        """Get the information and comments of the specified post

        Note: Must specify note_id, xsec_source, xsec_token
        """
        get_note_detail_task_list = []
        for full_note_url in config.XHS_SPECIFIED_NOTE_URL_LIST:
            note_url_info: NoteUrlInfo = parse_note_info_from_note_url(full_note_url)
            utils.logger.info(f"[XiaoHongShuCrawler.get_specified_notes] Parse note url info: {note_url_info}")
            crawler_task = self.get_note_detail_async_task(
                note_id=note_url_info.note_id,
                xsec_source=note_url_info.xsec_source,
                xsec_token=note_url_info.xsec_token,
                semaphore=asyncio.Semaphore(config.MAX_CONCURRENCY_NUM),
            )
            get_note_detail_task_list.append(crawler_task)

        need_get_comment_note_ids = []
        xsec_tokens = []
        note_details = await asyncio.gather(*get_note_detail_task_list)
        for note_detail in note_details:
            if note_detail:
                if self.is_video_note(note_detail):
                    utils.logger.info(
                        f"[XiaoHongShuCrawler.get_specified_notes] Skip video note, note_id: {note_detail.get('note_id')}"
                    )
                    continue
                need_get_comment_note_ids.append(note_detail.get("note_id", ""))
                xsec_tokens.append(note_detail.get("xsec_token", ""))
                await self.enrich_note_creator(note_detail)
                await xhs_store.update_xhs_note(note_detail)
                await self.get_notice_media(note_detail)
        await self.batch_get_note_comments(need_get_comment_note_ids, xsec_tokens)

    async def enrich_note_creator(self, note_detail: Dict) -> None:
        """Attach creator homepage metrics when TripPostCollect requests author enrichment."""
        if os.environ.get("TRIPPOSTCOLLECT_XHS_ENRICH_CREATORS") != "1":
            return
        user_info = note_detail.get("user") or {}
        user_id = user_info.get("user_id")
        if not user_id:
            return
        try:
            creator_info = await self.xhs_client.get_creator_info(
                user_id=user_id,
                xsec_token=note_detail.get("xsec_token", ""),
                xsec_source=note_detail.get("xsec_source", "pc_search"),
            )
        except Exception as exc:
            utils.logger.warning(
                f"[XiaoHongShuCrawler.enrich_note_creator] creator profile fetch failed: {user_id}, {exc}"
            )
            return
        if creator_info:
            note_detail["creator_profile"] = creator_info
            await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)

    @staticmethod
    def is_video_note(note_detail: Dict) -> bool:
        note_type = str(note_detail.get("type") or "").strip().lower()
        return note_type in {"video", "视频"} or "video" in note_type

    @staticmethod
    def note_detail_summaries(note_details: List[Optional[Dict]]) -> List[Dict]:
        summaries: List[Dict] = []
        for note_detail in note_details:
            if not note_detail:
                continue
            user_info = note_detail.get("user") or {}
            interact_info = note_detail.get("interact_info") or {}
            creator_profile = note_detail.get("creator_profile") or {}
            summaries.append(
                {
                    "note_id": note_detail.get("note_id"),
                    "type": note_detail.get("type"),
                    "title": note_detail.get("title"),
                    "desc_preview": str(note_detail.get("desc") or "")[:80],
                    "image_count": len(note_detail.get("image_list") or []),
                    "user_id": user_info.get("user_id"),
                    "nickname": user_info.get("nickname"),
                    "liked_count": interact_info.get("liked_count"),
                    "collected_count": interact_info.get("collected_count"),
                    "comment_count": interact_info.get("comment_count"),
                    "share_count": interact_info.get("share_count"),
                    "creator_profile": bool(creator_profile),
                }
            )
        return summaries

    async def get_note_detail_async_task(
        self,
        note_id: str,
        xsec_source: str,
        xsec_token: str,
        semaphore: asyncio.Semaphore,
    ) -> Optional[Dict]:
        """Get note detail

        Args:
            note_id:
            xsec_source:
            xsec_token:
            semaphore:

        Returns:
            Dict: note detail
        """
        note_detail = None
        utils.logger.info(f"[get_note_detail_async_task] Begin get note detail, note_id: {note_id}")
        async with semaphore:
            try:
                try:
                    note_detail = await self.xhs_client.get_note_by_id(note_id, xsec_source, xsec_token)
                except RetryError:
                    pass

                if not note_detail:
                    note_detail = await self.xhs_client.get_note_by_id_from_html(note_id, xsec_source, xsec_token,
                                                                                 enable_cookie=True)
                    if not note_detail:
                        utils.logger.warning(f"[skip] Failed to get note detail, Id: {note_id}, 跳过继续")
                        return None

                note_detail.update({"xsec_token": xsec_token, "xsec_source": xsec_source})

                # Sleep after fetching note detail
                await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                utils.logger.info(f"[get_note_detail_async_task] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching note {note_id}")

                return note_detail

            except NoteNotFoundError as ex:
                utils.logger.warning(f"[XiaoHongShuCrawler.get_note_detail_async_task] Note not found: {note_id}, {ex}")
                return None
            except DataFetchError as ex:
                utils.logger.error(f"[XiaoHongShuCrawler.get_note_detail_async_task] Get note detail error: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(f"[XiaoHongShuCrawler.get_note_detail_async_task] have not fund note detail note_id:{note_id}, err: {ex}")
                return None

    async def batch_get_note_comments(self, note_list: List[str], xsec_tokens: List[str]):
        """Batch get note comments"""
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.info(f"[XiaoHongShuCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
            return

        utils.logger.info(f"[XiaoHongShuCrawler.batch_get_note_comments] Begin batch get note comments, note list: {note_list}")
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        for index, note_id in enumerate(note_list):
            task = asyncio.create_task(
                self.get_comments(note_id=note_id, xsec_token=xsec_tokens[index], semaphore=semaphore),
                name=note_id,
            )
            task_list.append(task)
        await asyncio.gather(*task_list)

    async def get_comments(self, note_id: str, xsec_token: str, semaphore: asyncio.Semaphore):
        """Get note comments with keyword filtering and quantity limitation"""
        async with semaphore:
            utils.logger.info(f"[XiaoHongShuCrawler.get_comments] Begin get note id comments {note_id}")
            # Use fixed crawling interval
            crawl_interval = config.CRAWLER_MAX_SLEEP_SEC
            await self.xhs_client.get_note_all_comments(
                note_id=note_id,
                xsec_token=xsec_token,
                crawl_interval=crawl_interval,
                callback=xhs_store.batch_update_xhs_note_comments,
                max_count=config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES,
            )

            # Sleep after fetching comments
            await asyncio.sleep(crawl_interval)
            utils.logger.info(f"[XiaoHongShuCrawler.get_comments] Sleeping for {crawl_interval} seconds after fetching comments for note {note_id}")

    async def create_xhs_client(self, httpx_proxy: Optional[str]) -> XiaoHongShuClient:
        """Create Xiaohongshu client"""
        utils.logger.info("[XiaoHongShuCrawler.create_xhs_client] Begin create Xiaohongshu API client ...")
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            self.browser_context,
            urls=self.cookie_urls,
        )
        xhs_client_obj = XiaoHongShuClient(
            proxy=httpx_proxy,
            headers={
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9",
                "cache-control": "no-cache",
                "content-type": "application/json;charset=UTF-8",
                "origin": self.index_url,
                "pragma": "no-cache",
                "priority": "u=1, i",
                "referer": f"{self.index_url}/",
                "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not.A/Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"macOS"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
                "user-agent": self.user_agent,
                "Cookie": cookie_str,
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
            proxy_ip_pool=self.ip_proxy_pool,  # Pass proxy pool for automatic refresh
        )
        return xhs_client_obj

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        utils.logger.info("[XiaoHongShuCrawler.launch_browser] Begin create browser context ...")
        if config.SAVE_LOGIN_STATE:
            # feat issue #14
            # we will save login state to avoid login every time
            user_data_dir = os.path.join(os.getcwd(), "browser_data", config.USER_DATA_DIR % config.PLATFORM)  # type: ignore
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=headless,
                proxy=playwright_proxy,  # type: ignore
                viewport={
                    "width": 1920,
                    "height": 1080
                },
                user_agent=user_agent,
            )
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy)  # type: ignore
            browser_context = await browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=user_agent)
            return browser_context

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser using CDP mode"""
        try:
            self.cdp_manager = CDPBrowserManager()
            browser_context = await self.cdp_manager.launch_and_connect(
                playwright=playwright,
                playwright_proxy=playwright_proxy,
                user_agent=user_agent,
                headless=headless,
            )

            # Display browser information
            browser_info = await self.cdp_manager.get_browser_info()
            utils.logger.info(f"[XiaoHongShuCrawler] CDP browser info: {browser_info}")

            return browser_context

        except Exception as e:
            utils.logger.error(f"[XiaoHongShuCrawler] CDP mode launch failed, falling back to standard mode: {e}")
            # Fall back to standard mode
            chromium = playwright.chromium
            return await self.launch_browser(chromium, playwright_proxy, user_agent, headless)

    async def close(self):
        """Close browser context"""
        # Special handling if using CDP mode
        if self.cdp_manager:
            await self.cdp_manager.cleanup()
            self.cdp_manager = None
        else:
            await self.browser_context.close()
        utils.logger.info("[XiaoHongShuCrawler.close] Browser context closed ...")

    async def get_notice_media(self, note_detail: Dict):
        if not config.ENABLE_GET_MEIDAS:
            utils.logger.info(f"[XiaoHongShuCrawler.get_notice_media] Crawling image mode is not enabled")
            return
        await self.get_note_images(note_detail)
        utils.logger.info("[XiaoHongShuCrawler.get_notice_media] Video media crawling is disabled by TripPostCollect policy")

    async def get_note_images(self, note_item: Dict):
        """Get note images. Please use get_notice_media

        Args:
            note_item: Note item dictionary
        """
        if not config.ENABLE_GET_MEIDAS:
            return
        note_id = note_item.get("note_id")
        image_list: List[Dict] = note_item.get("image_list", [])

        for img in image_list:
            if img.get("url_default") != "":
                img.update({"url": img.get("url_default")})

        if not image_list:
            return
        picNum = 0
        for pic in image_list:
            url = pic.get("url")
            if not url:
                continue
            content = await self.xhs_client.get_note_media(url)
            await asyncio.sleep(random.random())
            if content is None:
                continue
            extension_file_name = f"{picNum}.jpg"
            picNum += 1
            await xhs_store.update_xhs_note_image(note_id, content, extension_file_name)

    async def get_notice_video(self, note_item: Dict):
        """Get note videos. Please use get_notice_media

        Args:
            note_item: Note item dictionary
        """
        utils.logger.info("[XiaoHongShuCrawler.get_notice_video] Video media crawling is disabled by TripPostCollect policy")
        return
