# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/zhihu/core.py
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


# -*- coding: utf-8 -*-
import asyncio
import os
# import random  # Removed as we now use fixed config.CRAWLER_MAX_SLEEP_SEC intervals
from asyncio import Task
from typing import Dict, List, Optional, cast
from urllib.parse import quote

from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Page,
    Playwright,
    async_playwright,
)

import config
from constant import zhihu as constant
from base.base_crawler import AbstractCrawler
from model.m_zhihu import ZhihuContent, ZhihuCreator
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import zhihu as zhihu_store
from tools import utils
from tools.trippostcollect_behavior import project_browser_args, run_required_human_behavior
from tools.trippostcollect_adaptive import AdaptiveAccumulator, env_int
from tools.cdp_browser import CDPBrowserManager
from var import crawler_type_var, source_keyword_var

from .client import ZhiHuClient
from .exception import DataFetchError
from .help import ZhihuExtractor, judge_zhihu_url, merge_search_content_detail
from .login import ZhiHuLogin


class ZhihuCrawler(AbstractCrawler):
    context_page: Page
    zhihu_client: ZhiHuClient
    browser_context: BrowserContext
    cdp_manager: Optional[CDPBrowserManager]

    def __init__(self) -> None:
        self.index_url = "https://www.zhihu.com"
        self.cookie_urls = [self.index_url]
        # self.user_agent = utils.get_user_agent()
        self.user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        self._extractor = ZhihuExtractor()
        self.cdp_manager = None
        self.ip_proxy_pool = None  # Proxy IP pool for automatic proxy refresh

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        value = os.environ.get(name, "").strip()
        if not value:
            return default
        try:
            return float(value)
        except ValueError:
            return default

    async def _activate_latest_zhihu_page(self) -> None:
        try:
            pages = [page for page in self.browser_context.pages if not page.is_closed()]
        except Exception:
            return
        for page in reversed(pages):
            if "zhihu.com" in (page.url or ""):
                self.context_page = page
                return

    async def _close_stale_pages(self, keep_page: Page) -> None:
        try:
            pages = [page for page in self.browser_context.pages if not page.is_closed()]
        except Exception:
            return

        closed_count = 0
        for page in pages:
            if page is keep_page:
                continue
            try:
                await page.close(run_before_unload=False)
                closed_count += 1
            except Exception as exc:
                utils.logger.warning(
                    f"[ZhihuCrawler] Failed to close stale browser tab {page.url}: {exc}"
                )

        if closed_count:
            utils.logger.info(f"[ZhihuCrawler] Closed {closed_count} stale browser tabs")

    async def _wait_for_initial_login_settle(self) -> None:
        settle_seconds = self._env_float("TRIPPOSTCOLLECT_ZHIHU_INITIAL_SETTLE_SECONDS", 0.0)
        await self._activate_latest_zhihu_page()
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
                f"[ZhihuCrawler] Waiting {settle_seconds:.1f}s for Zhihu login state settle ..."
            )
            await asyncio.sleep(settle_seconds)
        await self._activate_latest_zhihu_page()

    async def enrich_search_content_detail(
        self,
        content: ZhihuContent,
    ) -> ZhihuContent:
        """Fetch the full answer/article when search HTML has no content image."""
        if content.image_list or content.content_type not in {
            constant.ANSWER_NAME,
            constant.ARTICLE_NAME,
        }:
            return content

        try:
            if content.content_type == constant.ANSWER_NAME:
                detail = await self.zhihu_client.get_answer_info(
                    content.question_id,
                    content.content_id,
                )
            else:
                detail = await self.zhihu_client.get_article_info(content.content_id)
        except DataFetchError as exc:
            content.content_detail_status = "request_failed"
            utils.logger.warning(
                "[ZhihuCrawler.enrich_search_content_detail] Detail request failed "
                f"for {content.content_type}:{content.content_id}: {exc}"
            )
        else:
            if detail is None:
                content.content_detail_status = "parse_failed"
                utils.logger.warning(
                    "[ZhihuCrawler.enrich_search_content_detail] Detail parse failed "
                    f"for {content.content_type}:{content.content_id}"
                )
            else:
                merge_search_content_detail(content, detail)

        await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
        return content

    async def start(self) -> None:
        """
        Start the crawler
        Returns:

        """
        playwright_proxy_format, httpx_proxy_format = None, None
        if config.ENABLE_IP_PROXY:
            self.ip_proxy_pool = await create_ip_pool(
                config.IP_PROXY_POOL_COUNT, enable_validate_ip=True
            )
            ip_proxy_info: IpInfoModel = await self.ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = utils.format_proxy_info(
                ip_proxy_info
            )

        async with async_playwright() as playwright:
            # Choose launch mode based on configuration
            if config.ENABLE_CDP_MODE:
                utils.logger.info("[ZhihuCrawler] Launching browser in CDP mode")
                self.browser_context = await self.launch_browser_with_cdp(
                    playwright,
                    playwright_proxy_format,
                    self.user_agent,
                    headless=config.CDP_HEADLESS,
                )
            else:
                utils.logger.info("[ZhihuCrawler] Launching browser in standard mode")
                # Launch a browser context.
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(
                    chromium, None, self.user_agent, headless=config.HEADLESS
                )
                # stealth.min.js is a js script to prevent the website from detecting the crawler.
                await self.browser_context.add_init_script(path="libs/stealth.min.js")

            self.context_page = await self.browser_context.new_page()
            await self._close_stale_pages(self.context_page)
            try:
                await self.context_page.goto(
                    self.index_url,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            except Exception as exc:
                utils.logger.warning(
                    f"[ZhihuCrawler.start] Initial homepage navigation did not settle: {exc}"
                )
            if config.LOGIN_TYPE == "cookie" and config.COOKIES:
                for key, value in utils.convert_str_cookie_to_dict(config.COOKIES).items():
                    await self.browser_context.add_cookies(
                        [{"name": key, "value": value, "domain": ".zhihu.com", "path": "/"}]
                    )
                try:
                    await self.context_page.reload(
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                except Exception as exc:
                    utils.logger.warning(
                        f"[ZhihuCrawler.start] Cookie reload did not settle: {exc}"
                    )
            await self._wait_for_initial_login_settle()
            await self._close_stale_pages(self.context_page)

            # Create a client to interact with the zhihu website.
            self.zhihu_client = await self.create_zhihu_client(httpx_proxy_format)
            if not await self.zhihu_client.pong():
                login_obj = ZhiHuLogin(
                    login_type=config.LOGIN_TYPE,
                    login_phone="",  # input your phone number
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=config.COOKIES,
                )
                await login_obj.begin()
                await self.zhihu_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )

            crawler_type_var.set(config.CRAWLER_TYPE)
            search_cookie_keyword = config.KEYWORDS.split(",", maxsplit=1)[0].strip()
            # Zhihu's search API requires opening the search page first to access cookies, homepage alone won't work
            utils.logger.info(
                "[ZhihuCrawler.start] Zhihu navigating to search page to get search page cookies, this process takes about 5 seconds"
            )
            try:
                await self.context_page.goto(
                    f"{self.index_url}/search?q={quote(search_cookie_keyword)}&type=content",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            except Exception as exc:
                utils.logger.warning(
                    f"[ZhihuCrawler.start] Search-page navigation did not settle: {exc}"
                )
            await asyncio.sleep(5)
            await self.zhihu_client.update_cookies(
                browser_context=self.browser_context,
                urls=self.cookie_urls,
            )

            await run_required_human_behavior(self.context_page, "zhihu")
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

            utils.logger.info("[ZhihuCrawler.start] Zhihu Crawler finished ...")

    async def search(self) -> None:
        """Search for notes and retrieve their comment information."""
        utils.logger.info("[ZhihuCrawler.search] Begin search zhihu keywords")
        start_page = config.START_PAGE
        accumulator = AdaptiveAccumulator.from_environment("zhihu", config.CRAWLER_MAX_NOTES_COUNT)
        for keyword in config.KEYWORDS.split(","):
            source_keyword_var.set(keyword)
            utils.logger.info(
                f"[ZhihuCrawler.search] Current search keyword: {keyword}"
            )
            phases: list[tuple[str, int, int | None]] = []
            refresh_max_pages = env_int("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", 0)
            if start_page > 1 and refresh_max_pages > 0:
                phases.append(("refresh", 1, min(start_page - 1, refresh_max_pages)))
            source_exhausted = os.environ.get("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED") == "1"
            if not source_exhausted:
                phases.append(("frontier", start_page, None))
            for discovery_phase, phase_start, phase_end in phases:
                page = phase_start
                while (
                    accumulator.can_continue
                    and (phase_end is None or page <= phase_end)
                ):
                    try:
                        requested_page = page
                        utils.logger.info(
                            f"[ZhihuCrawler.search] search zhihu keyword: {keyword}, "
                            f"page: {requested_page}"
                        )
                        content_list: List[ZhihuContent] = (
                            await self.zhihu_client.get_note_by_keyword(
                                keyword=keyword,
                                page=requested_page,
                            )
                        )
                        if not content_list:
                            utils.logger.info("No more content!")
                            if discovery_phase == "frontier":
                                accumulator.mark_source_exhausted(
                                    "empty_page",
                                    source_page=requested_page,
                                    raw_batch_count=0,
                                    resume_page=requested_page,
                                    source_has_more=False,
                                    discovery_phase=discovery_phase,
                                )
                            break

                        raw_batch_count = len(content_list)
                        pending_content_ids: set[str] = set()
                        unknown_contents: List[ZhihuContent] = []
                        for content in content_list:
                            content_id = str(content.content_id or "")
                            if accumulator.is_known(content_id) or (
                                content_id and content_id in pending_content_ids
                            ):
                                continue
                            if content_id:
                                pending_content_ids.add(content_id)
                            unknown_contents.append(content)
                        content_list = unknown_contents
                        await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                        utils.logger.info(
                            f"[ZhihuCrawler.search] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} "
                            f"seconds after page {requested_page}"
                        )

                        page += 1
                        accumulator.begin_batch()
                        processed_count = 0
                        stored_contents: List[ZhihuContent] = []
                        for content in content_list:
                            content = await self.enrich_search_content_detail(content)
                            valid = bool(
                                content.content_id
                                and (content.content_text or content.title)
                                and content.created_time
                                and content.creator_hash
                                and content.user_nickname
                                and content.image_list
                                and content.followers_observed
                            )
                            should_stop = accumulator.consider(
                                str(content.content_id or ""),
                                valid=valid,
                            )
                            processed_count += 1
                            stored_contents.append(content)
                            await zhihu_store.update_zhihu_content(content)
                            if should_stop:
                                break

                        await self.batch_get_content_comments(stored_contents)
                        batch_complete = processed_count >= len(content_list)
                        if accumulator.finish_batch(
                            source_page=requested_page,
                            raw_batch_count=raw_batch_count,
                            resume_page=page if batch_complete else requested_page,
                            batch_complete=batch_complete,
                            discovery_phase=discovery_phase,
                            count_stagnation=discovery_phase == "frontier",
                        ):
                            break
                    except DataFetchError:
                        utils.logger.error("[ZhihuCrawler.search] Search content error")
                        accumulator.mark_runtime_failed(
                            "search_request_failed",
                            source_page=page,
                            resume_page=page,
                            discovery_phase=discovery_phase,
                        )
                        return
            if source_exhausted and not accumulator.stop_reason:
                accumulator.mark_source_exhausted(
                    "saved_source_exhausted",
                    source_page=start_page,
                    resume_page=start_page,
                    source_has_more=False,
                    raw_batch_count=0,
                    discovery_phase="frontier",
                )

    async def batch_get_content_comments(self, content_list: List[ZhihuContent]):
        """
        Batch get content comments
        Args:
            content_list:

        Returns:

        """
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.info(
                "[ZhihuCrawler.batch_get_content_comments] Crawling comment mode is not enabled"
            )
            return

        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        for content_item in content_list:
            task = asyncio.create_task(
                self.get_comments(content_item, semaphore), name=content_item.content_id
            )
            task_list.append(task)
        await asyncio.gather(*task_list)

    async def get_comments(
        self, content_item: ZhihuContent, semaphore: asyncio.Semaphore
    ):
        """
        Get note comments with keyword filtering and quantity limitation
        Args:
            content_item:
            semaphore:

        Returns:

        """
        async with semaphore:
            utils.logger.info(
                f"[ZhihuCrawler.get_comments] Begin get note id comments {content_item.content_id}"
            )

            # Sleep before fetching comments
            await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
            utils.logger.info(f"[ZhihuCrawler.get_comments] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds before fetching comments for content {content_item.content_id}")

            await self.zhihu_client.get_note_all_comments(
                content=content_item,
                crawl_interval=config.CRAWLER_MAX_SLEEP_SEC,
                callback=zhihu_store.batch_update_zhihu_note_comments,
            )

    async def get_creators_and_notes(self) -> None:
        """
        Get creator's information and their notes and comments
        Returns:

        """
        utils.logger.info(
            "[ZhihuCrawler.get_creators_and_notes] Begin get xiaohongshu creators"
        )
        for user_link in config.ZHIHU_CREATOR_URL_LIST:
            utils.logger.info(
                f"[ZhihuCrawler.get_creators_and_notes] Begin get creator {user_link}"
            )
            user_url_token = user_link.split("/")[-1]
            # get creator detail info from web html content
            createor_info: ZhihuCreator = await self.zhihu_client.get_creator_info(
                url_token=user_url_token
            )
            if not createor_info:
                utils.logger.info(
                    f"[ZhihuCrawler.get_creators_and_notes] Creator {user_url_token} not found"
                )
                continue

            utils.logger.info(
                f"[ZhihuCrawler.get_creators_and_notes] Creator info: {createor_info}"
            )

            # By default, only answer information is extracted, uncomment below if articles and videos are needed

            # Get all anwser information of the creator
            all_content_list = await self.zhihu_client.get_all_anwser_by_creator(
                url_token=user_url_token,
                crawl_interval=config.CRAWLER_MAX_SLEEP_SEC,
                callback=zhihu_store.batch_update_zhihu_contents,
            )

            # Get all articles of the creator's contents
            # all_content_list = await self.zhihu_client.get_all_articles_by_creator(
            #     url_token=user_url_token,
            #     crawl_interval=config.CRAWLER_MAX_SLEEP_SEC,
            #     callback=zhihu_store.batch_update_zhihu_contents
            # )

            # Get all videos of the creator's contents
            # all_content_list = await self.zhihu_client.get_all_videos_by_creator(
            #     url_token=user_url_token,
            #     crawl_interval=config.CRAWLER_MAX_SLEEP_SEC,
            #     callback=zhihu_store.batch_update_zhihu_contents
            # )

            # Get all comments of the creator's contents
            await self.batch_get_content_comments(all_content_list)

    async def get_note_detail(
        self, full_note_url: str, semaphore: asyncio.Semaphore
    ) -> Optional[ZhihuContent]:
        """
        Get note detail
        Args:
            full_note_url: str
            semaphore:

        Returns:

        """
        async with semaphore:
            utils.logger.info(
                f"[ZhihuCrawler.get_specified_notes] Begin get specified note {full_note_url}"
            )
            # Judge note type
            note_type: str = judge_zhihu_url(full_note_url)
            if note_type == constant.ANSWER_NAME:
                question_id = full_note_url.split("/")[-3]
                answer_id = full_note_url.split("/")[-1]
                utils.logger.info(
                    f"[ZhihuCrawler.get_specified_notes] Get answer info, question_id: {question_id}, answer_id: {answer_id}"
                )
                try:
                    result = await self.zhihu_client.get_answer_info(question_id, answer_id)
                except DataFetchError as exc:
                    utils.logger.warning(
                        "[ZhihuCrawler.get_note_detail] Answer detail request failed "
                        f"for {answer_id}: {exc}"
                    )
                    return ZhihuContent(
                        content_id=answer_id,
                        question_id=question_id,
                        content_type=constant.ANSWER_NAME,
                        content_url=full_note_url,
                        content_detail_status="request_failed",
                    )

                # Sleep after fetching answer details
                await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                utils.logger.info(f"[ZhihuCrawler.get_note_detail] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching answer details {answer_id}")

                if result is None:
                    return ZhihuContent(
                        content_id=answer_id,
                        question_id=question_id,
                        content_type=constant.ANSWER_NAME,
                        content_url=full_note_url,
                        content_detail_status="parse_failed",
                    )
                result.content_detail_status = "detail_observed"
                return result

            elif note_type == constant.ARTICLE_NAME:
                article_id = full_note_url.split("/")[-1]
                utils.logger.info(
                    f"[ZhihuCrawler.get_specified_notes] Get article info, article_id: {article_id}"
                )
                try:
                    result = await self.zhihu_client.get_article_info(article_id)
                except DataFetchError as exc:
                    utils.logger.warning(
                        "[ZhihuCrawler.get_note_detail] Article detail request failed "
                        f"for {article_id}: {exc}"
                    )
                    return ZhihuContent(
                        content_id=article_id,
                        content_type=constant.ARTICLE_NAME,
                        content_url=full_note_url,
                        content_detail_status="request_failed",
                    )

                # Sleep after fetching article details
                await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                utils.logger.info(f"[ZhihuCrawler.get_note_detail] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching article details {article_id}")

                if result is None:
                    return ZhihuContent(
                        content_id=article_id,
                        content_type=constant.ARTICLE_NAME,
                        content_url=full_note_url,
                        content_detail_status="parse_failed",
                    )
                result.content_detail_status = "detail_observed"
                return result

            elif note_type == constant.VIDEO_NAME:
                video_id = full_note_url.split("/")[-1]
                utils.logger.info(
                    f"[ZhihuCrawler.get_specified_notes] Get video info, video_id: {video_id}"
                )
                result = await self.zhihu_client.get_video_info(video_id)

                # Sleep after fetching video details
                await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                utils.logger.info(f"[ZhihuCrawler.get_note_detail] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching video details {video_id}")

                return result

    async def get_specified_notes(self):
        """
        Get the information and comments of the specified post
        Returns:

        """
        get_note_detail_task_list = []
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        for full_note_url in config.ZHIHU_SPECIFIED_ID_LIST:
            # remove query params
            full_note_url = full_note_url.split("?")[0]
            crawler_task = self.get_note_detail(
                full_note_url=full_note_url,
                semaphore=semaphore,
            )
            get_note_detail_task_list.append(crawler_task)

        need_get_comment_notes: List[ZhihuContent] = []
        note_details = await asyncio.gather(*get_note_detail_task_list)
        for index, note_detail in enumerate(note_details):
            if not note_detail:
                utils.logger.info(
                    f"[ZhihuCrawler.get_specified_notes] Note {config.ZHIHU_SPECIFIED_ID_LIST[index]} not found"
                )
                continue

            note_detail = cast(ZhihuContent, note_detail)  # only for type check
            need_get_comment_notes.append(note_detail)
            await zhihu_store.update_zhihu_content(note_detail)

        await self.batch_get_content_comments(need_get_comment_notes)

    async def create_zhihu_client(self, httpx_proxy: Optional[str]) -> ZhiHuClient:
        """Create zhihu client"""
        utils.logger.info(
            "[ZhihuCrawler.create_zhihu_client] Begin create zhihu API client ..."
        )
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            self.browser_context,
            urls=self.cookie_urls,
        )
        zhihu_client_obj = ZhiHuClient(
            proxy=httpx_proxy,
            headers={
                "accept": "*/*",
                "accept-language": "zh-CN,zh;q=0.9",
                "cookie": cookie_str,
                "priority": "u=1, i",
                "referer": "https://www.zhihu.com/search?q=python&time_interval=a_year&type=content",
                "user-agent": self.user_agent,
                "x-api-version": "3.0.91",
                "x-app-za": "OS=Web",
                "x-requested-with": "fetch",
                "x-zse-93": "101_3_3.0",
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
            proxy_ip_pool=self.ip_proxy_pool,  # Pass proxy pool for automatic refresh
        )
        return zhihu_client_obj

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        utils.logger.info(
            "[ZhihuCrawler.launch_browser] Begin create browser context ..."
        )
        if config.SAVE_LOGIN_STATE:
            # feat issue #14
            # we will save login state to avoid login every time
            user_data_dir = os.path.join(
                os.getcwd(), "browser_data", config.USER_DATA_DIR % config.PLATFORM
            )  # type: ignore
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=headless,
                proxy=playwright_proxy,  # type: ignore
                viewport={"width": 1920, "height": 1080},
                user_agent=user_agent,
                args=project_browser_args(),
            )
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy)  # type: ignore
            browser_context = await browser.new_context(
                viewport={"width": 1920, "height": 1080}, user_agent=user_agent
            )
            return browser_context

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """
        Launch browser using CDP mode
        """
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
            utils.logger.info(f"[ZhihuCrawler] CDP browser info: {browser_info}")

            return browser_context

        except Exception as e:
            utils.logger.error(f"[ZhihuCrawler] CDP mode launch failed, falling back to standard mode: {e}")
            # Fall back to standard mode
            chromium = playwright.chromium
            return await self.launch_browser(
                chromium, playwright_proxy, user_agent, headless
            )

    async def close(self):
        """Close browser context"""
        # Special handling if using CDP mode
        if self.cdp_manager:
            await self.cdp_manager.cleanup()
            self.cdp_manager = None
        else:
            await self.browser_context.close()
        utils.logger.info("[ZhihuCrawler.close] Browser context closed ...")
