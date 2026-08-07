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
import re
import time
from asyncio import Task
from typing import Dict, List, Optional
from urllib.parse import quote

from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from tenacity import RetryError

import config
from base.base_crawler import AbstractCrawler
from model.m_xiaohongshu import NoteUrlInfo, CreatorUrlInfo
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import xhs as xhs_store
from tools import utils
from tools.image_manifest import ImageStagingError
from tools.trippostcollect_behavior import (
    inspect_visible_page_state,
    install_project_runtime_hints,
    project_browser_args,
    run_required_continuity_behavior,
    run_required_human_behavior,
    run_required_request_pause,
    run_requested_post_interaction,
)
from tools.trippostcollect_adaptive import AdaptiveAccumulator, env_int
from tools.cdp_browser import CDPBrowserManager
from var import crawler_type_var, source_keyword_var

from .client import XiaoHongShuClient
from .exception import DataFetchError, NoteNotFoundError
from .field import SearchSortType
from .help import parse_note_info_from_note_url, parse_creator_info_from_url, get_search_id
from .login import XiaoHongShuLogin


XHS_NEW_PAGE_MIN_HOLD_SECONDS = 30.0


class XHSImageDownloadError(RuntimeError):
    """An XHS post image failed before the candidate safe frontier."""

    def __init__(self, note_id: str, source_index: int, code: str):
        super().__init__(
            f"XHS image download failed: note_id={note_id}, "
            f"source_index={source_index}, code={code}"
        )
        self.note_id = note_id
        self.source_index = source_index
        self.code = code


class XiaoHongShuCrawler(AbstractCrawler):
    context_page: Page
    xhs_client: XiaoHongShuClient
    browser_context: BrowserContext
    cdp_manager: Optional[CDPBrowserManager]

    def __init__(self) -> None:
        self.index_url = "https://www.rednote.com" if config.XHS_INTERNATIONAL else "https://www.xiaohongshu.com"
        self.cookie_urls = [self.index_url]
        self.user_agent: Optional[str] = None
        self.cdp_manager = None
        self.ip_proxy_pool = None  # Proxy IP pool for automatic proxy refresh
        self.post_interaction_mode = os.environ.get("TRIPPOSTCOLLECT_XHS_POST_INTERACTION", "none").strip()
        self.post_interaction_attempted = False
        self.creator_profile_cache: Dict[str, Dict] = {}
        self._crawler_page_open_depth = 0
        self._initial_pages: Dict[int, Page] = {}
        self._new_pages: Dict[int, tuple[Page, float, str]] = {}
        self._new_page_guard_tasks: Dict[int, Task[None]] = {}

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

    @staticmethod
    def _page_is_closed(page: Page) -> bool:
        try:
            return page.is_closed()
        except Exception:
            return False

    @staticmethod
    def _popup_monotonic() -> float:
        return time.monotonic()

    @staticmethod
    async def _popup_sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    def _install_new_page_guard(self) -> None:
        """Protect every page appearing after browser launch for at least 30 seconds."""
        try:
            pages = list(self.browser_context.pages)
        except Exception:
            pages = []
        self.browser_context.on("page", self._on_browser_page)
        if pages:
            primary_page = pages[0]
            self._initial_pages[id(primary_page)] = primary_page
            for extra_page in pages[1:]:
                self._register_new_page(extra_page, source="preexisting_extra")

    def _on_browser_page(self, page: Page) -> None:
        source = "crawler_opened" if self._crawler_page_open_depth > 0 else "platform_opened"
        self._register_new_page(page, source=source)

    def _register_new_page(self, page: Page, *, source: str) -> None:
        page_id = id(page)
        if self._initial_pages.get(page_id) is page:
            return
        if page_id in self._new_pages:
            return
        opened_at = self._popup_monotonic()
        self._new_pages[page_id] = (page, opened_at, source)
        task = asyncio.create_task(self._hold_new_page(page, opened_at, source))
        self._new_page_guard_tasks[page_id] = task

    async def _hold_new_page(self, page: Page, opened_at: float, source: str) -> None:
        """Surface any new tab and keep it alive without trying to classify it."""
        try:
            await page.bring_to_front()
        except Exception as exc:
            utils.logger.warning(
                "[XiaoHongShuCrawler] Could not bring new tab to front: "
                f"{type(exc).__name__}: {exc}"
            )
        remaining = max(
            0.0,
            XHS_NEW_PAGE_MIN_HOLD_SECONDS
            - (self._popup_monotonic() - opened_at),
        )
        utils.logger.warning(
            "[XiaoHongShuCrawler] New browser tab opened; keeping it visible "
            f"for at least {XHS_NEW_PAGE_MIN_HOLD_SECONDS:.0f}s before any "
            f"crawler-initiated close: source={source}, url={getattr(page, 'url', '')}"
        )
        if remaining > 0:
            await self._popup_sleep(remaining)

    async def _new_guarded_page(self) -> Page:
        """Open a crawler-requested page while retaining the universal new-tab guard."""
        self._crawler_page_open_depth += 1
        try:
            page = await self.browser_context.new_page()
        finally:
            self._crawler_page_open_depth -= 1
        # Playwright normally emits ``page`` before ``new_page`` returns. Registering
        # explicitly as well keeps the guard correct if that event is delayed.
        self._register_new_page(page, source="crawler_opened")
        return page

    async def _wait_before_page_close(self, page: Page, *, reason: str) -> None:
        page_id = id(page)
        if page_id not in self._new_pages:
            return
        task = self._new_page_guard_tasks.get(page_id)
        if task and not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                utils.logger.warning(
                    "[XiaoHongShuCrawler] New-tab guard task failed; "
                    f"enforcing remaining hold directly: {type(exc).__name__}: {exc}"
                )
        _, opened_at, source = self._new_pages[page_id]
        remaining = max(
            0.0,
            XHS_NEW_PAGE_MIN_HOLD_SECONDS
            - (self._popup_monotonic() - opened_at),
        )
        if remaining > 0:
            await self._popup_sleep(remaining)
        utils.logger.info(
            "[XiaoHongShuCrawler] New-tab minimum hold completed; "
            f"close may proceed: source={source}, reason={reason}, "
            f"url={getattr(page, 'url', '')}"
        )

    async def _wait_for_all_new_page_guards(self) -> None:
        """Drain all new-tab hold periods before Playwright or the context can close them."""
        while True:
            guarded_pages = [
                page
                for page, _, _ in self._new_pages.values()
                if not self._page_is_closed(page)
            ]
            pending = [
                page
                for page in guarded_pages
                if self._popup_monotonic() - self._new_pages[id(page)][1]
                < XHS_NEW_PAGE_MIN_HOLD_SECONDS
            ]
            if not pending:
                return
            await asyncio.gather(
                *(
                    self._wait_before_page_close(page, reason="browser_context_cleanup")
                    for page in pending
                )
            )

    async def _prepare_browser_shutdown(self) -> None:
        """Close pages through the guard before context or Playwright teardown."""
        while True:
            await self._wait_for_all_new_page_guards()
            try:
                pages = [
                    page
                    for page in self.browser_context.pages
                    if not self._page_is_closed(page)
                ]
            except Exception:
                return
            if not pages:
                return
            page_ids_before = {id(page) for page in pages}
            for page in pages:
                await self._close_page_with_deadline(
                    page,
                    reason="browser_shutdown",
                )
            await asyncio.sleep(0)
            try:
                remaining_ids = {
                    id(page)
                    for page in self.browser_context.pages
                    if not self._page_is_closed(page)
                }
            except Exception:
                return
            if not remaining_ids or remaining_ids == page_ids_before:
                return

    async def _guarded_pause(self, stage: str, minimum: float, maximum: float) -> float:
        event = await run_required_request_pause(stage, minimum, maximum)
        seconds = float(event["seconds"])
        utils.logger.info(f"[XiaoHongShuCrawler] Guarded pause stage={stage} seconds={seconds:.3f}")
        if stage in {"search_results", "search_page"}:
            continuity = await run_required_continuity_behavior(self.context_page, stage)
            utils.logger.info(
                "[XiaoHongShuCrawler] Continuity behavior "
                f"stage={stage} status={continuity.get('status')}"
            )
        return seconds

    async def _goto_with_deadline(
        self,
        page: Page,
        url: str,
        *,
        stage: str,
        wait_until: str = "commit",
    ) -> None:
        timeout_seconds = self._env_float("TRIPPOSTCOLLECT_XHS_NAVIGATION_DEADLINE_SECONDS", 60.0)
        timeout_seconds = max(5.0, timeout_seconds)
        try:
            async with asyncio.timeout(timeout_seconds + 2.0):
                await page.goto(
                    url,
                    wait_until=wait_until,
                    timeout=int(timeout_seconds * 1000),
                )
        except (PlaywrightTimeoutError, TimeoutError) as exc:
            current_url = page.url or ""
            if "/search_result" in url and "/search_result" in current_url:
                utils.logger.warning(
                    "[XiaoHongShuCrawler] Navigation event timed out after URL commit; "
                    f"continuing with visible readiness gate: stage={stage}, url={current_url}"
                )
                return
            raise RuntimeError(f"xhs_navigation_timeout:{stage}:{current_url}") from exc

    async def _close_page_with_deadline(
        self,
        page: Page,
        *,
        reason: str = "crawler_page_cleanup",
    ) -> None:
        await self._wait_before_page_close(page, reason=reason)
        if self._page_is_closed(page):
            return
        try:
            async with asyncio.timeout(10):
                await page.close()
        except Exception as exc:
            utils.logger.warning(f"[XiaoHongShuCrawler] Page close did not finish cleanly: {exc}")

    async def _browser_identity_headers(self) -> Dict[str, str]:
        try:
            async with asyncio.timeout(10):
                identity = await self.context_page.evaluate(
                    """() => ({
                        webdriver: navigator.webdriver,
                        user_agent: navigator.userAgent || '',
                        language: navigator.language || '',
                        platform: navigator.userAgentData
                            ? navigator.userAgentData.platform
                            : (navigator.platform || ''),
                        mobile: navigator.userAgentData
                            ? navigator.userAgentData.mobile
                            : false,
                        brands: navigator.userAgentData
                            ? Array.from(navigator.userAgentData.brands || [])
                            : [],
                    })"""
                )
        except Exception as exc:
            raise RuntimeError(f"xhs_browser_identity_unavailable:{type(exc).__name__}") from exc

        user_agent = str((identity or {}).get("user_agent") or "").strip()
        language = str((identity or {}).get("language") or "").strip()
        platform = str((identity or {}).get("platform") or "").strip()
        brands = (identity or {}).get("brands") or []
        if (identity or {}).get("webdriver") is not None:
            raise RuntimeError("xhs_browser_identity_webdriver_exposed")
        if not user_agent or not language or not platform or not isinstance(brands, list) or not brands:
            raise RuntimeError("xhs_browser_identity_incomplete")

        ua_match = re.search(r"(?:Chrome|Chromium)/(\d+)", user_agent)
        ua_major = ua_match.group(1) if ua_match else ""
        normalized_brands: List[str] = []
        chromium_major = ""
        for item in brands:
            if not isinstance(item, dict):
                continue
            brand = str(item.get("brand") or "").replace("\\", "").replace('"', "").strip()
            version = str(item.get("version") or "").split(".", 1)[0].strip()
            if not brand or not version:
                continue
            normalized_brands.append(f'"{brand}";v="{version}"')
            if brand in {"Chromium", "Google Chrome"}:
                chromium_major = version
        if not normalized_brands or not ua_major or chromium_major != ua_major:
            raise RuntimeError(
                f"xhs_browser_identity_version_mismatch:ua={ua_major},client_hints={chromium_major}"
            )

        self.user_agent = user_agent
        return {
            "accept-language": language,
            "sec-ch-ua": ", ".join(normalized_brands),
            "sec-ch-ua-mobile": "?1" if bool((identity or {}).get("mobile")) else "?0",
            "sec-ch-ua-platform": f'"{platform}"',
            "user-agent": user_agent,
        }

    async def _maybe_run_post_interaction(self, note_detail: Dict) -> None:
        if self.post_interaction_mode == "none" or self.post_interaction_attempted:
            return
        self.post_interaction_attempted = True
        note_id = str(note_detail.get("note_id") or "").strip()
        if not note_id:
            utils.logger.warning("[XiaoHongShuCrawler] Skip requested post interaction: missing note_id")
            return
        query = (
            f"xsec_token={quote(str(note_detail.get('xsec_token') or ''))}"
            f"&xsec_source={quote(str(note_detail.get('xsec_source') or 'pc_search'))}"
        )
        interaction_url = f"{self.index_url}/explore/{quote(note_id)}?{query}"
        page = await self._new_guarded_page()
        try:
            await self._goto_with_deadline(
                page,
                interaction_url,
                stage="post_interaction",
            )
            interaction = await run_requested_post_interaction(
                page,
                platform_key="xhs",
                requested_mode=self.post_interaction_mode,
                note_id=note_id,
            )
            utils.logger.info(
                "[XiaoHongShuCrawler] Post interaction "
                f"mode={self.post_interaction_mode} note_id={note_id} status={interaction.get('status')}"
            )
            blocked_markers = {
                key
                for markers_key in ("initial_visible_markers", "visible_markers")
                for key, present in (interaction.get(markers_key) or {}).items()
                if present
            }
            if blocked_markers:
                raise RuntimeError(f"xhs_post_interaction_visible_block:{','.join(sorted(blocked_markers))}")
        except RuntimeError as exc:
            if str(exc).startswith("xhs_post_interaction_visible_block:"):
                raise
            utils.logger.warning(
                f"[XiaoHongShuCrawler] Requested post interaction failed without stopping crawl: {type(exc).__name__}: {exc}"
            )
        except Exception as exc:
            utils.logger.warning(
                f"[XiaoHongShuCrawler] Requested post interaction failed without stopping crawl: {type(exc).__name__}: {exc}"
            )
        finally:
            await self._close_page_with_deadline(
                page,
                reason="post_interaction_cleanup",
            )

    def _storage_state_path(self) -> str:
        explicit_path = os.environ.get("TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH", "").strip()
        if not explicit_path:
            raise RuntimeError("XHS requires TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH from xhs_runner.py")
        return os.path.abspath(os.path.expanduser(explicit_path))

    @staticmethod
    def _profile_dir() -> str:
        explicit_path = os.environ.get("TRIPPOSTCOLLECT_XHS_PROFILE_DIR", "").strip()
        if not explicit_path:
            raise RuntimeError("XHS requires TRIPPOSTCOLLECT_XHS_PROFILE_DIR from xhs_runner.py")
        return os.path.abspath(os.path.expanduser(explicit_path))

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

    async def _single_page_for_login(self) -> Page:
        """Keep exactly one tab open until the login checkpoint has succeeded."""
        try:
            pages = [page for page in self.browser_context.pages if not page.is_closed()]
        except Exception:
            pages = []

        current_page = getattr(self, "context_page", None)
        page = (
            current_page
            if current_page in pages
            else (pages[0] if pages else await self._new_guarded_page())
        )
        closed_count = 0
        for other_page in pages:
            if other_page is page:
                continue
            await self._close_page_with_deadline(
                other_page,
                reason="login_tab_normalization",
            )
            closed_count += 1
        if closed_count:
            utils.logger.info(
                "[XiaoHongShuCrawler] Login stage retained one tab and closed "
                f"{closed_count} stale tab(s)."
            )
        self.context_page = page
        return page

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
            await self._single_page_for_login()
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

    @staticmethod
    def _request_failure_exception(exc: BaseException) -> BaseException:
        """Return the request exception hidden by tenacity, when available."""
        if not isinstance(exc, RetryError):
            return exc
        try:
            nested = exc.last_attempt.exception()
        except Exception:
            nested = None
        return nested if isinstance(nested, BaseException) else exc

    @classmethod
    def _is_login_expired_failure(cls, exc: BaseException) -> bool:
        nested = cls._request_failure_exception(exc)
        text = str(nested).lower()
        return isinstance(nested, DataFetchError) and any(
            marker in text
            for marker in (
                "登录已过期",
                "登录状态已失效",
                "登录失效",
                "login expired",
                "login has expired",
                "session expired",
            )
        )

    async def _wait_for_midrun_login_recovery(self, keyword: str) -> bool:
        """Keep the headed browser open while the operator restores an expired login."""
        wait_seconds = self._env_int("TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS", 0)
        if wait_seconds <= 0:
            return False

        utils.logger.warning(
            "[XiaoHongShuCrawler] Xiaohongshu login expired during search; "
            f"keeping every browser tab open for up to {wait_seconds}s so the "
            "operator can complete login/security verification."
        )
        await self._activate_latest_xhs_page()
        try:
            await self.context_page.bring_to_front()
        except Exception:
            pass
        try:
            await self.context_page.reload(wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            utils.logger.warning(
                "[XiaoHongShuCrawler] Could not refresh the visible page before "
                f"manual login recovery; leaving it open: {type(exc).__name__}: {exc}"
            )

        started = time.monotonic()
        last_print = 0.0
        last_pong = -10.0
        while time.monotonic() - started < wait_seconds:
            await self._activate_latest_xhs_page()
            try:
                await self.context_page.bring_to_front()
            except Exception:
                pass

            elapsed = time.monotonic() - started
            profile_ui = await self._profile_ui_visible()
            if profile_ui and elapsed - last_pong >= 5.0:
                last_pong = elapsed
                await self.xhs_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )
                if await self.xhs_client.pong():
                    search_url = f"{self.index_url}/search_result?keyword={quote(keyword)}"
                    await self._goto_with_deadline(
                        self.context_page,
                        search_url,
                        stage="midrun_login_recovered",
                    )
                    await self.xhs_client.update_cookies(
                        browser_context=self.browser_context,
                        urls=self.cookie_urls,
                    )
                    await self._write_storage_state()
                    utils.logger.info(
                        "[XiaoHongShuCrawler] Mid-run login recovery confirmed; "
                        "retrying the same search page."
                    )
                    return True

            if elapsed - last_print >= 10.0:
                checkpoint_markers = await self._visible_checkpoint_markers()
                utils.logger.info(
                    "[XiaoHongShuCrawler] Waiting for mid-run Xiaohongshu login "
                    f"recovery: profile_ui={profile_ui}, visible={checkpoint_markers}"
                )
                last_print = elapsed
            await asyncio.sleep(2)

        utils.logger.error(
            "[XiaoHongShuCrawler] Mid-run login/security verification timed out; "
            "the current source page will remain the recovery frontier."
        )
        return False

    async def start(self) -> None:
        playwright_proxy_format, httpx_proxy_format = None, None
        if config.ENABLE_IP_PROXY:
            self.ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
            ip_proxy_info: IpInfoModel = await self.ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = utils.format_proxy_info(ip_proxy_info)

        async with async_playwright() as playwright:
            try:
                await self._run_browser_session(
                    playwright,
                    playwright_proxy_format,
                    httpx_proxy_format,
                )
            finally:
                await self._prepare_browser_shutdown()

    async def _run_browser_session(
        self,
        playwright: Playwright,
        playwright_proxy_format: Optional[Dict],
        httpx_proxy_format: Optional[str],
    ) -> None:
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
            chromium = playwright.chromium
            self.browser_context = await self.launch_browser(
                chromium,
                playwright_proxy_format,
                self.user_agent,
                headless=config.HEADLESS,
            )
            await self.browser_context.add_init_script(path="libs/stealth.min.js")

        self._install_new_page_guard()
        await install_project_runtime_hints(self.browser_context)
        await self._restore_storage_state()
        self.context_page = await self._single_page_for_login()
        await self._goto_with_deadline(
            self.context_page,
            self.index_url,
            stage="initial_home",
        )
        await self._wait_for_initial_page_settle()

        self.xhs_client = await self.create_xhs_client(httpx_proxy_format)
        if not await self.xhs_client.pong():
            await self._single_page_for_login()
            checkpoint_ready = False
            if await self._wait_for_manual_checkpoint_if_needed():
                await self.xhs_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )
                checkpoint_ready = await self.xhs_client.pong()
            if not checkpoint_ready:
                await self._single_page_for_login()
                login_obj = XiaoHongShuLogin(
                    login_type=config.LOGIN_TYPE,
                    login_phone="",  # input your phone number
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=config.COOKIES,
                    close_page=self._close_page_with_deadline,
                    new_page=self._new_guarded_page,
                )
                await login_obj.begin()
                await self.xhs_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )
                if not await self.xhs_client.pong():
                    raise RuntimeError(
                        "[XiaoHongShuCrawler] Xiaohongshu login state not confirmed "
                        "after login flow"
                    )

        await self._write_storage_state()
        behavior_keyword = next(
            (item.strip() for item in config.KEYWORDS.split(",") if item.strip()),
            "",
        )
        if behavior_keyword:
            await self._goto_with_deadline(
                self.context_page,
                f"https://www.xiaohongshu.com/search_result?keyword={quote(behavior_keyword)}",
                stage="behavior_search",
            )
        behavior_evidence = await run_required_human_behavior(self.context_page, "xhs")
        if behavior_evidence.get("status") != "completed":
            raise RuntimeError("XHS required human behavior stage did not complete")
        crawler_type_var.set(config.CRAWLER_TYPE)
        if config.CRAWLER_TYPE == "search":
            await self.search()
        elif config.CRAWLER_TYPE == "detail":
            await self.get_specified_notes()
        elif config.CRAWLER_TYPE == "creator":
            await self.get_creators_and_notes()

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
            refresh_max_pages = env_int(
                "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES",
                0,
            )
            source_exhausted = (
                os.environ.get("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED") == "1"
            )
            frontier_search_id = os.environ.get(
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR",
                "",
            ) or get_search_id()
            phases: list[tuple[str, int, int | None, str]] = []
            if refresh_max_pages > 0:
                phases.append(("refresh", 1, refresh_max_pages, get_search_id()))
            if not source_exhausted:
                phases.append(("frontier", start_page, None, frontier_search_id))

            for discovery_phase, phase_start, phase_limit, search_id in phases:
                page = phase_start
                phase_batches = 0
                while (
                    accumulator.can_continue
                    and (phase_limit is None or phase_batches < phase_limit)
                ):
                    requested_page = page
                    try:
                        utils.logger.info(
                            "[XiaoHongShuCrawler.search] search Xiaohongshu "
                            f"keyword: {keyword}, page: {requested_page}, phase: {discovery_phase}"
                        )
                        notes_res = await self.xhs_client.get_note_by_keyword(
                            keyword=keyword,
                            search_id=search_id,
                            page=requested_page,
                            sort=(
                                SearchSortType(config.SORT_TYPE)
                                if config.SORT_TYPE != ""
                                else SearchSortType.GENERAL
                            ),
                        )
                        if not notes_res:
                            utils.logger.info("[XiaoHongShuCrawler.search] No more content!")
                            if discovery_phase == "frontier":
                                accumulator.mark_source_exhausted(
                                    "empty_response",
                                    source_page=requested_page,
                                    source_cursor=search_id,
                                    source_has_more=False,
                                    raw_batch_count=0,
                                    resume_page=requested_page,
                                    resume_cursor=search_id,
                                    discovery_phase=discovery_phase,
                                )
                            break

                        raw_items = list(notes_res.get("items") or [])
                        source_has_more = (
                            notes_res.get("has_more")
                            if "has_more" in notes_res
                            else None
                        )
                        pending_ids: set[str] = set()
                        unknown_items: List[Dict] = []
                        for post_item in raw_items:
                            if post_item.get("model_type") in ("rec_query", "hot_query"):
                                continue
                            identity = str(post_item.get("id") or "")
                            if accumulator.is_known(identity) or (
                                identity and identity in pending_ids
                            ):
                                continue
                            if identity:
                                pending_ids.add(identity)
                            unknown_items.append(post_item)

                        accumulator.begin_batch()
                        if accumulator.exhaustion_mode:
                            selected_items = unknown_items
                        else:
                            remaining = max(
                                0,
                                accumulator.hard_limit - accumulator.candidate_count,
                            )
                            selected_items = unknown_items[:remaining]
                        if not selected_items:
                            resume_page = requested_page + 1
                            if accumulator.finish_batch(
                                source_page=requested_page,
                                source_cursor=search_id,
                                source_has_more=source_has_more,
                                raw_batch_count=len(raw_items),
                                resume_page=resume_page,
                                resume_cursor=search_id,
                                batch_complete=True,
                                discovery_phase=discovery_phase,
                                count_stagnation=discovery_phase == "frontier",
                            ):
                                break
                            if source_has_more in (False, 0):
                                if discovery_phase == "frontier":
                                    accumulator.mark_source_exhausted(
                                        "has_more_false",
                                        source_page=requested_page,
                                        source_cursor=search_id,
                                        source_has_more=False,
                                        raw_batch_count=len(raw_items),
                                        resume_page=resume_page,
                                        resume_cursor=search_id,
                                        batch_complete=True,
                                        discovery_phase=discovery_phase,
                                    )
                                break
                            page = resume_page
                            phase_batches += 1
                            await self._guarded_pause("search_page", 12.0, 30.0)
                            continue

                        await self._guarded_pause("search_results", 6.0, 14.0)
                        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
                        task_list = [
                            self.get_note_detail_async_task(
                                note_id=post_item.get("id"),
                                xsec_source=post_item.get("xsec_source"),
                                xsec_token=post_item.get("xsec_token"),
                                semaphore=semaphore,
                            )
                            for post_item in selected_items
                        ]
                        note_details = await asyncio.gather(*task_list)
                        note_ids: List[str] = []
                        xsec_tokens: List[str] = []
                        processed_count = 0
                        for post_item, note_detail in zip(selected_items, note_details):
                            identity = str(
                                (note_detail or {}).get("note_id")
                                or post_item.get("id")
                                or ""
                            )
                            processed_count += 1
                            if note_detail:
                                if self.is_video_note(note_detail):
                                    utils.logger.info(
                                        "[XiaoHongShuCrawler.search] Skip video note, "
                                        f"note_id: {note_detail.get('note_id')}"
                                    )
                                    if accumulator.consider(identity, valid=False):
                                        break
                                    continue
                                await self._maybe_run_post_interaction(note_detail)
                                await self.enrich_note_creator(note_detail)
                                creator_profile = note_detail.get("creator_profile") or {}
                                creator_item = (
                                    xhs_store._normalized_creator_item(
                                        (note_detail.get("user") or {}).get("user_id", ""),
                                        creator_profile,
                                    )
                                    if creator_profile
                                    else {}
                                )
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
                                    and all(
                                        interact_info.get(key) not in (None, "")
                                        for key in (
                                            "liked_count",
                                            "collected_count",
                                            "comment_count",
                                            "share_count",
                                        )
                                    )
                                )
                                if valid:
                                    await self.get_notice_media(note_detail)
                                await xhs_store.update_xhs_note(note_detail)
                                note_ids.append(note_detail.get("note_id"))
                                xsec_tokens.append(note_detail.get("xsec_token"))
                                should_stop = accumulator.consider(identity, valid=valid)
                                if should_stop:
                                    break
                            elif accumulator.consider(identity, valid=False):
                                break

                        utils.logger.info(
                            "[XiaoHongShuCrawler.search] Note detail summaries: "
                            f"{self.note_detail_summaries(note_details)}"
                        )
                        await self.batch_get_note_comments(note_ids, xsec_tokens)
                        batch_complete = processed_count >= len(unknown_items)
                        resume_page = (
                            requested_page + 1 if batch_complete else requested_page
                        )
                        if accumulator.finish_batch(
                            source_page=requested_page,
                            source_cursor=search_id,
                            source_has_more=source_has_more,
                            raw_batch_count=len(raw_items),
                            resume_page=resume_page,
                            resume_cursor=search_id,
                            batch_complete=batch_complete,
                            discovery_phase=discovery_phase,
                            count_stagnation=discovery_phase == "frontier",
                        ):
                            break
                        if source_has_more in (False, 0):
                            if discovery_phase == "frontier":
                                accumulator.mark_source_exhausted(
                                    "has_more_false",
                                    source_page=requested_page,
                                    source_cursor=search_id,
                                    source_has_more=False,
                                    raw_batch_count=len(raw_items),
                                    resume_page=resume_page,
                                    resume_cursor=search_id,
                                    batch_complete=batch_complete,
                                    discovery_phase=discovery_phase,
                                )
                            break
                        page = requested_page + 1
                        phase_batches += 1
                        await self._guarded_pause("search_page", 12.0, 30.0)
                    except XHSImageDownloadError as exc:
                        utils.logger.error(
                            "[XiaoHongShuCrawler.search] Image materialization failed "
                            f"on page {requested_page}: {exc!r}"
                        )
                        accumulator.mark_runtime_failed(
                            "image_download_failed",
                            source_page=requested_page,
                            source_cursor=search_id,
                            resume_page=requested_page,
                            resume_cursor=search_id,
                            discovery_phase=discovery_phase,
                        )
                        break
                    except (DataFetchError, RetryError) as exc:
                        if self._is_login_expired_failure(exc):
                            if await self._wait_for_midrun_login_recovery(keyword):
                                continue
                            utils.logger.error(
                                "[XiaoHongShuCrawler.search] Login remained expired "
                                f"on page {requested_page}."
                            )
                            accumulator.mark_runtime_failed(
                                "login_required",
                                source_page=requested_page,
                                source_cursor=search_id,
                                resume_page=requested_page,
                                resume_cursor=search_id,
                                discovery_phase=discovery_phase,
                            )
                            break
                        utils.logger.error(
                            "[XiaoHongShuCrawler.search] Search or note detail "
                            f"request failed: {self._request_failure_exception(exc)!r}"
                        )
                        accumulator.mark_runtime_failed(
                            "search_or_detail_request_failed",
                            source_page=requested_page,
                            source_cursor=search_id,
                            resume_page=requested_page,
                            resume_cursor=search_id,
                            discovery_phase=discovery_phase,
                        )
                        break
                    except PlaywrightError as exc:
                        detail = (
                            "browser_context_closed"
                            if (
                                exc.__class__.__name__ == "TargetClosedError"
                                or "context or browser has been closed" in str(exc).lower()
                            )
                            else "browser_runtime_failed"
                        )
                        utils.logger.error(
                            "[XiaoHongShuCrawler.search] Browser runtime error on "
                            f"page {requested_page}: {exc!r}"
                        )
                        accumulator.mark_runtime_failed(
                            detail,
                            source_page=requested_page,
                            source_cursor=search_id,
                            resume_page=requested_page,
                            resume_cursor=search_id,
                            discovery_phase=discovery_phase,
                        )
                        break

            if source_exhausted and not accumulator.stop_reason:
                accumulator.mark_source_exhausted(
                    "saved_source_exhausted",
                    source_page=start_page,
                    source_cursor=frontier_search_id,
                    source_has_more=False,
                    raw_batch_count=0,
                    resume_page=start_page,
                    resume_cursor=frontier_search_id,
                    discovery_phase="frontier",
                )

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
        cached_creator = self.creator_profile_cache.get(str(user_id))
        if cached_creator:
            note_detail["creator_profile"] = cached_creator
            return
        creator_info = None
        try:
            creator_info = await self.xhs_client.get_creator_info(user_id=user_id)
        except Exception as exc:
            utils.logger.warning(
                "[XiaoHongShuCrawler.enrich_note_creator] "
                f"session profile request failed, using browser fallback: {user_id}, {exc}"
            )

        if not creator_info:
            await self._guarded_pause("creator_profile_browser_fallback", 12.0, 30.0)
            creator_info = await self._get_creator_info_from_browser(str(user_id))
        if creator_info:
            self.creator_profile_cache[str(user_id)] = creator_info
            note_detail["creator_profile"] = creator_info
            await self._guarded_pause("creator_profile", 8.0, 18.0)
        else:
            utils.logger.warning(
                f"[XiaoHongShuCrawler.enrich_note_creator] creator profile empty after browser fallback: {user_id}"
            )

    async def _get_creator_info_from_browser(self, user_id: str) -> Optional[Dict]:
        """Load an author homepage in the signed-in context when the direct request is empty."""
        page = await self._new_guarded_page()
        try:
            await self._goto_with_deadline(
                page,
                f"{self.index_url}/user/profile/{quote(user_id)}",
                stage="creator_profile_browser",
            )
            await page.wait_for_timeout(random.randint(1_200, 3_000))

            _, markers = await inspect_visible_page_state(page)
            if markers.get("captcha_or_verify"):
                return await self._wait_for_creator_profile_verification(page, user_id)
            challenge = next(
                (
                    key
                    for key in ("rate_limited", "blocked")
                    if markers.get(key)
                ),
                "",
            )
            if challenge:
                raise RuntimeError(f"xhs_creator_profile_visible_block:{challenge}")
            if markers.get("login_required"):
                raise RuntimeError("xhs_creator_profile_visible_block:login_required")

            viewport = page.viewport_size or {"width": 1280, "height": 800}
            await page.mouse.move(
                random.randint(80, max(81, viewport["width"] - 80)),
                random.randint(80, max(81, viewport["height"] - 80)),
                steps=random.randint(6, 14),
            )
            await page.mouse.wheel(0, random.randint(180, 460))
            await page.wait_for_timeout(random.randint(500, 1_500))

            _, markers = await inspect_visible_page_state(page)
            if markers.get("captcha_or_verify"):
                return await self._wait_for_creator_profile_verification(page, user_id)
            challenge = next(
                (
                    key
                    for key in ("rate_limited", "blocked")
                    if markers.get(key)
                ),
                "",
            )
            if challenge:
                raise RuntimeError(f"xhs_creator_profile_visible_block:{challenge}")
            if markers.get("login_required"):
                raise RuntimeError("xhs_creator_profile_visible_block:login_required")

            html_content = await page.content()
            return self.xhs_client.extract_creator_info_from_html(html_content)
        finally:
            await self._close_page_with_deadline(
                page,
                reason="creator_profile_cleanup",
            )

    async def _wait_for_creator_profile_verification(
        self,
        page: Page,
        user_id: str,
    ) -> Optional[Dict]:
        """Keep a QR security-check page open until the operator completes it."""
        timeout_seconds = max(
            30.0,
            self._env_float("TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_WAIT_SECONDS", 600.0),
        )
        poll_seconds = max(
            1.0,
            self._env_float("TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_POLL_SECONDS", 2.0),
        )
        deadline = time.monotonic() + timeout_seconds
        await page.bring_to_front()
        utils.logger.warning(
            "[XiaoHongShuCrawler] Manual QR security verification required for creator profile; "
            f"keeping page open for up to {timeout_seconds:.0f}s: {user_id}"
        )

        while True:
            if time.monotonic() >= deadline:
                raise RuntimeError("xhs_creator_profile_verification_timeout")

            _, markers = await inspect_visible_page_state(page)
            challenge = next(
                (
                    key
                    for key in ("rate_limited", "blocked")
                    if markers.get(key)
                ),
                "",
            )
            if challenge:
                raise RuntimeError(f"xhs_creator_profile_visible_block:{challenge}")
            if markers.get("login_required") and not markers.get("captcha_or_verify"):
                raise RuntimeError("xhs_creator_profile_visible_block:login_required")

            if not markers.get("captcha_or_verify"):
                html_content = await page.content()
                creator_info = self.xhs_client.extract_creator_info_from_html(html_content)
                if creator_info:
                    utils.logger.info(
                        "[XiaoHongShuCrawler] Manual creator-profile verification completed: "
                        f"{user_id}"
                    )
                    return creator_info

            remaining_seconds = deadline - time.monotonic()
            await page.wait_for_timeout(
                int(min(poll_seconds, max(0.1, remaining_seconds)) * 1000)
            )

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

                await self._guarded_pause("note_detail", 4.0, 10.0)

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
            utils.logger.info("[XiaoHongShuCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
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
        identity_headers = await self._browser_identity_headers()
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            self.browser_context,
            urls=self.cookie_urls,
        )
        xhs_client_obj = XiaoHongShuClient(
            proxy=httpx_proxy,
            headers={
                "accept": "application/json, text/plain, */*",
                "cache-control": "no-cache",
                "content-type": "application/json;charset=UTF-8",
                "origin": self.index_url,
                "pragma": "no-cache",
                "priority": "u=1, i",
                "referer": f"{self.index_url}/",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
                **identity_headers,
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
            user_data_dir = self._profile_dir()
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
                args=project_browser_args(),
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

    async def close(self, *, force: bool = False):
        """Close browser context"""
        await self._prepare_browser_shutdown()
        try:
            async with asyncio.timeout(20):
                # Special handling if using CDP mode
                if self.cdp_manager:
                    await self.cdp_manager.cleanup(force=force)
                    self.cdp_manager = None
                else:
                    await self.browser_context.close()
        except Exception as exc:
            utils.logger.warning(f"[XiaoHongShuCrawler.close] Browser cleanup timed out or failed: {exc}")
        utils.logger.info("[XiaoHongShuCrawler.close] Browser context closed ...")

    async def get_notice_media(self, note_detail: Dict):
        if not config.ENABLE_GET_MEIDAS:
            utils.logger.info("[XiaoHongShuCrawler.get_notice_media] Crawling image mode is not enabled")
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
        note_id = str(note_item.get("note_id") or "")
        image_assets = xhs_store._xhs_image_assets(note_item)
        if not image_assets:
            return
        fetched_assets: List[Dict] = []
        for asset in image_assets:
            content = await self.xhs_client.get_note_media(asset["url"])
            await asyncio.sleep(random.random())
            if content is None:
                await xhs_store.record_xhs_note_image_failure(
                    note_id,
                    {
                        **asset,
                        "attempts": 1,
                        "http_status": None,
                        "error_code": "image_download_retryable",
                    },
                )
                raise XHSImageDownloadError(
                    note_id,
                    int(asset["source_index"]),
                    "image_download_retryable",
                )
            fetched_assets.append(
                {**asset, "content": content, "attempts": 1, "http_status": 200}
            )
        try:
            await xhs_store.update_xhs_note_images(note_id, fetched_assets)
        except ImageStagingError as exc:
            source_index = int(exc.source_index or 0)
            failed_asset = next(
                (
                    asset
                    for asset in image_assets
                    if int(asset["source_index"]) == source_index
                ),
                image_assets[0],
            )
            await xhs_store.record_xhs_note_image_failure(
                note_id,
                {
                    **failed_asset,
                    "attempts": 1,
                    "http_status": 200,
                    "error_code": exc.code,
                },
            )
            raise XHSImageDownloadError(note_id, source_index, exc.code) from exc

    async def get_notice_video(self, note_item: Dict):
        """Get note videos. Please use get_notice_media

        Args:
            note_item: Note item dictionary
        """
        utils.logger.info("[XiaoHongShuCrawler.get_notice_video] Video media crawling is disabled by TripPostCollect policy")
        return
