from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

import media_platform.xhs.core as xhs_core
from media_platform.xhs.core import XiaoHongShuCrawler
from media_platform.xhs.login import XiaoHongShuLogin
from media_platform.xhs.manual_wait import XHSManualWaitBudget


_REMOVED_LOGIN_ENV_VARS = (
    "TRIPPOSTCOLLECT_XHS_RUN_SCOPED_LOGIN",
    "TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH",
    "TRIPPOSTCOLLECT_COOKIES",
)


@pytest.fixture(autouse=True)
def clean_removed_login_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_name in _REMOVED_LOGIN_ENV_VARS:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(xhs_core.config, "LOGIN_TYPE", "qrcode")
    monkeypatch.setattr(xhs_core.config, "COOKIES", "")
    monkeypatch.setattr(xhs_core.config, "ENABLE_IP_PROXY", False)


@pytest.mark.parametrize("login_type", ["phone", "cookie", "unknown", ""])
def test_low_level_xhs_login_rejects_every_non_qrcode_type(
    login_type: str,
) -> None:
    with pytest.raises(ValueError, match="^xhs_login_type_must_be_qrcode$"):
        XiaoHongShuLogin(
            login_type=login_type,
            browser_context=SimpleNamespace(),
            context_page=SimpleNamespace(),
        )


@pytest.mark.parametrize("removed_parameter", ["login_phone", "cookie_str"])
def test_removed_login_constructor_parameters_fail_closed(
    removed_parameter: str,
) -> None:
    kwargs = {
        "login_type": "qrcode",
        "browser_context": SimpleNamespace(),
        "context_page": SimpleNamespace(),
        removed_parameter: "legacy-value",
    }

    with pytest.raises(TypeError, match=f"unexpected keyword argument '{removed_parameter}'"):
        XiaoHongShuLogin(**kwargs)


@pytest.mark.asyncio
async def test_xhs_login_begin_has_only_the_qrcode_branch() -> None:
    login = XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=SimpleNamespace(),
        context_page=SimpleNamespace(),
    )
    login._single_login_page = AsyncMock()
    login.login_by_qrcode = AsyncMock()

    await login.begin()

    login._single_login_page.assert_awaited_once_with()
    login.login_by_qrcode.assert_awaited_once_with()


def test_xhs_login_has_no_mobile_cookie_or_redis_automation_surface() -> None:
    signature = inspect.signature(XiaoHongShuLogin)
    source = inspect.getsource(XiaoHongShuLogin)

    assert "login_phone" not in signature.parameters
    assert "cookie_str" not in signature.parameters
    assert not hasattr(XiaoHongShuLogin, "login_by_mobile")
    assert not hasattr(XiaoHongShuLogin, "login_by_cookies")
    assert not hasattr(XiaoHongShuLogin, "check_login_state")
    assert not hasattr(XiaoHongShuLogin, "wait_login_state")
    assert "CacheFactory" not in source
    assert "send_btn_ele" not in source
    assert "sms_code_key" not in source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("TRIPPOSTCOLLECT_XHS_RUN_SCOPED_LOGIN", ""),
        ("TRIPPOSTCOLLECT_XHS_RUN_SCOPED_LOGIN", "0"),
        ("TRIPPOSTCOLLECT_XHS_RUN_SCOPED_LOGIN", "1"),
        ("TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH", ""),
        ("TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH", "/tmp/legacy.json"),
        ("TRIPPOSTCOLLECT_COOKIES", ""),
        ("TRIPPOSTCOLLECT_COOKIES", "web_session=legacy"),
    ],
)
async def test_removed_login_environment_fails_before_browser_start(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    env_value: str,
) -> None:
    monkeypatch.setenv(env_name, env_value)
    async_playwright = MagicMock(
        side_effect=AssertionError("deprecated input must fail before browser start")
    )
    monkeypatch.setattr(xhs_core, "async_playwright", async_playwright)

    with pytest.raises(
        RuntimeError,
        match=f"^xhs_deprecated_login_input:{env_name}$",
    ):
        await XiaoHongShuCrawler().start()

    async_playwright.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("login_type", ["phone", "cookie", "unknown"])
async def test_removed_xhs_login_type_fails_before_browser_start(
    monkeypatch: pytest.MonkeyPatch,
    login_type: str,
) -> None:
    monkeypatch.setattr(xhs_core.config, "LOGIN_TYPE", login_type)
    async_playwright = MagicMock(
        side_effect=AssertionError("invalid login type must fail before browser start")
    )
    monkeypatch.setattr(xhs_core, "async_playwright", async_playwright)

    with pytest.raises(RuntimeError, match="^xhs_login_type_must_be_qrcode$"):
        await XiaoHongShuCrawler().start()

    async_playwright.assert_not_called()


@pytest.mark.asyncio
async def test_xhs_cookie_login_value_fails_before_browser_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xhs_core.config, "COOKIES", "web_session=legacy")
    async_playwright = MagicMock(
        side_effect=AssertionError("cookie input must fail before browser start")
    )
    monkeypatch.setattr(xhs_core, "async_playwright", async_playwright)

    with pytest.raises(RuntimeError, match="^xhs_cookie_login_input_removed$"):
        await XiaoHongShuCrawler().start()

    async_playwright.assert_not_called()


def test_current_qrcode_contract_and_temporary_profile_remain_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_PROFILE_DIR", str(profile_dir))

    XiaoHongShuCrawler._validate_login_contract()

    assert XiaoHongShuCrawler._profile_dir() == str(profile_dir)


@pytest.mark.asyncio
@pytest.mark.parametrize("save_login_state", [True, False])
async def test_standard_xhs_launch_always_uses_the_run_scoped_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    save_login_state: bool,
) -> None:
    profile_dir = tmp_path / "empty-run-profile"
    profile_dir.mkdir()
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_PROFILE_DIR", str(profile_dir))
    monkeypatch.setattr(
        xhs_core.config,
        "SAVE_LOGIN_STATE",
        save_login_state,
    )
    context = object()
    chromium = SimpleNamespace(
        launch_persistent_context=AsyncMock(return_value=context),
        launch=AsyncMock(
            side_effect=AssertionError("XHS must not launch a profile-less browser")
        ),
    )

    crawler = XiaoHongShuCrawler()
    result = await crawler.launch_browser(
        chromium,
        {"server": "http://proxy.invalid"},
        "XHS test agent",
        headless=False,
    )

    assert result is context
    chromium.launch_persistent_context.assert_awaited_once()
    launch_kwargs = chromium.launch_persistent_context.await_args.kwargs
    assert launch_kwargs["user_data_dir"] == str(profile_dir)
    assert launch_kwargs["headless"] is False
    assert launch_kwargs["proxy"] == {"server": "http://proxy.invalid"}
    assert launch_kwargs["user_agent"] == "XHS test agent"
    chromium.launch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("save_login_state", [True, False])
@pytest.mark.parametrize("profile_value", [None, "", " \t "])
async def test_standard_xhs_launch_rejects_missing_profile_before_chrome(
    monkeypatch: pytest.MonkeyPatch,
    save_login_state: bool,
    profile_value: str | None,
) -> None:
    if profile_value is None:
        monkeypatch.delenv("TRIPPOSTCOLLECT_XHS_PROFILE_DIR", raising=False)
    else:
        monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_PROFILE_DIR", profile_value)
    monkeypatch.setattr(
        xhs_core.config,
        "SAVE_LOGIN_STATE",
        save_login_state,
    )
    chromium = SimpleNamespace(
        launch_persistent_context=AsyncMock(
            side_effect=AssertionError("missing profile must fail before Chrome")
        ),
        launch=AsyncMock(
            side_effect=AssertionError("missing profile must fail before Chrome")
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="^XHS requires TRIPPOSTCOLLECT_XHS_PROFILE_DIR from xhs_runner.py$",
    ):
        await XiaoHongShuCrawler().launch_browser(
            chromium,
            None,
            None,
            headless=False,
        )

    chromium.launch_persistent_context.assert_not_awaited()
    chromium.launch.assert_not_awaited()


def test_xhs_crawler_has_no_storage_state_compatibility_surface() -> None:
    source = inspect.getsource(XiaoHongShuCrawler)

    assert not hasattr(XiaoHongShuCrawler, "_storage_state_path")
    assert not hasattr(XiaoHongShuCrawler, "_cookie_for_restore")
    assert not hasattr(XiaoHongShuCrawler, "_restore_storage_state")
    assert not hasattr(XiaoHongShuCrawler, "_write_storage_state")
    assert "TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH" not in source
    assert ".storage_state(" not in source


class _LoginFlowClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleep_calls: list[float] = []
        self.on_sleep: Callable[[float], None] | None = None

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.now)


class _LoginFlowLocator:
    def __init__(self, page: "_LoginFlowPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    async def count(self) -> int:
        return 1 if self.selector == "body" else int(
            self.selector in self.page.visible_selectors
        )

    async def inner_text(self, timeout: int) -> str:
        assert timeout > 0
        return self.page.visible_text

    async def is_visible(self, timeout: int) -> bool:
        assert timeout > 0
        return self.selector in self.page.visible_selectors

    async def click(self, timeout: int) -> None:
        raise AssertionError(f"login flow must not click unexpected selector: {self.selector}")


class _LoginFlowPage:
    def __init__(self, *, visible_text: str = "") -> None:
        self.url = "https://www.xiaohongshu.com/explore"
        self.visible_text = visible_text
        self.visible_selectors: set[str] = set()
        self.closed = False
        self.front_count = 0

    @property
    def frames(self) -> list["_LoginFlowPage"]:
        return [self]

    def locator(self, selector: str) -> _LoginFlowLocator:
        return _LoginFlowLocator(self, selector)

    async def is_visible(self, selector: str, timeout: int) -> bool:
        assert timeout > 0
        return selector in self.visible_selectors

    async def bring_to_front(self) -> None:
        self.front_count += 1

    async def reload(self, **_kwargs: object) -> None:
        raise AssertionError("startup login must not reload after manual progress")

    def is_closed(self) -> bool:
        return self.closed


class _LoginFlowContext:
    def __init__(self, page: _LoginFlowPage) -> None:
        self.pages = [page]
        self.page_handler: object | None = None
        self.new_page_calls = 0

    def on(self, event: str, handler: object) -> None:
        assert event == "page"
        self.page_handler = handler

    async def cookies(self, *_args: object, **_kwargs: object) -> list[dict[str, str]]:
        return [{"name": "web_session", "value": "anonymous"}]

    async def new_page(self) -> _LoginFlowPage:
        self.new_page_calls += 1
        raise AssertionError("startup login must reuse the current BrowserContext page")


def _configure_browser_session_test(
    monkeypatch: pytest.MonkeyPatch,
    *,
    page: _LoginFlowPage,
    pong_results: list[bool],
    events: list[str],
) -> tuple[XiaoHongShuCrawler, _LoginFlowContext, SimpleNamespace]:
    context = _LoginFlowContext(page)
    crawler = XiaoHongShuCrawler()
    crawler.launch_browser_with_cdp = AsyncMock(return_value=context)
    crawler._goto_with_deadline = AsyncMock()
    crawler._wait_for_initial_page_settle = AsyncMock()
    crawler._wait_for_visible_page_shell = AsyncMock(return_value=True)
    crawler._open_behavior_search_page_with_recovery = AsyncMock()
    crawler._run_human_behavior_with_page_recovery = AsyncMock(
        return_value={"status": "completed"}
    )
    crawler.search = AsyncMock()

    async def update_cookies(**_kwargs: object) -> None:
        events.append("update_cookies")

    client = SimpleNamespace(
        playwright_page=page,
        update_cookies=AsyncMock(side_effect=update_cookies),
    )
    crawler.create_xhs_client = AsyncMock(return_value=client)

    outcomes = iter(pong_results)

    async def pong(*, stage: str) -> bool:
        events.append(f"pong:{stage}")
        return next(outcomes)

    crawler._pong_with_network_recovery = AsyncMock(side_effect=pong)
    monkeypatch.setattr(xhs_core, "install_project_runtime_hints", AsyncMock())
    monkeypatch.setattr(xhs_core.config, "ENABLE_CDP_MODE", True)
    monkeypatch.setattr(xhs_core.config, "CDP_HEADLESS", False)
    monkeypatch.setattr(xhs_core.config, "KEYWORDS", "青岛登录测试")
    monkeypatch.setattr(xhs_core.config, "CRAWLER_TYPE", "search")
    return crawler, context, client


def _install_observed_login_factory(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
) -> list[XiaoHongShuLogin]:
    instances: list[XiaoHongShuLogin] = []

    def factory(**kwargs: object) -> XiaoHongShuLogin:
        login = XiaoHongShuLogin(**kwargs)
        original_begin = login.begin

        async def observed_begin() -> None:
            events.append("login.begin")
            await original_begin()

        login.begin = observed_begin  # type: ignore[method-assign]
        instances.append(login)
        return login

    monkeypatch.setattr(xhs_core, "XiaoHongShuLogin", factory)
    return instances


@pytest.mark.asyncio
async def test_failed_startup_pong_enters_real_qrcode_state_machine_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _LoginFlowClock()
    page = _LoginFlowPage(visible_text="扫码登录 打开小红书扫一扫")
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    events: list[str] = []
    crawler, context, client = _configure_browser_session_test(
        monkeypatch,
        page=page,
        pong_results=[False, True, True],
        events=events,
    )
    budget = XHSManualWaitBudget(limit_seconds=600, monotonic=clock.monotonic)
    crawler._manual_wait_budget = budget
    instances = _install_observed_login_factory(monkeypatch, events)
    monkeypatch.setattr(xhs_core.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(xhs_core.asyncio, "sleep", clock.sleep)
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_LOGIN_POLL_SECONDS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_STABLE_LOGIN_SECONDS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_QR_REFRESH_SECONDS", "180")

    async def find_qrcode(current_page: _LoginFlowPage, *, selector: str) -> str | None:
        assert current_page is page
        return "qr" if selector in page.visible_selectors else None

    monkeypatch.setattr(xhs_core.utils, "find_login_qrcode", find_qrcode)

    def complete_scan(now: float) -> None:
        if now < 1:
            return
        page.visible_text = "首页 我"
        page.visible_selectors.discard(XiaoHongShuLogin._QRCODE_SELECTOR)
        page.visible_selectors.add(XiaoHongShuLogin._PROFILE_SELECTORS[0])

    clock.on_sleep = complete_scan

    await crawler._run_browser_session(object(), None, None)

    assert len(instances) == 1
    assert instances[0]._manual_wait_budget is budget
    assert crawler._manual_wait_budget is budget
    assert crawler.context_page is page
    assert client.playwright_page is page
    assert context.new_page_calls == 0
    assert crawler.launch_browser_with_cdp.await_count == 1
    assert crawler._pong_with_network_recovery.await_args_list == [
        call(stage="startup_login_probe"),
        call(stage="startup_post_login_probe"),
        call(stage="post_behavior_login_probe"),
    ]
    assert events.count("login.begin") == 1
    assert events.index("pong:startup_login_probe") < events.index("login.begin")
    assert events.index("login.begin") < events.index("update_cookies")
    assert events.index("update_cookies") < events.index(
        "pong:startup_post_login_probe"
    )
    crawler._run_human_behavior_with_page_recovery.assert_awaited_once_with(
        "青岛登录测试"
    )
    crawler.search.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("visible_text", "terminal_marker"),
    [
        ("SMS Verification\nParameter error", "Parameter error"),
        ("手机号登录\n今日短信验证码次数已达上限", "今日短信验证码次数已达上限"),
    ],
)
async def test_startup_sms_terminal_fails_immediately_without_retry_or_business_work(
    monkeypatch: pytest.MonkeyPatch,
    visible_text: str,
    terminal_marker: str,
) -> None:
    clock = _LoginFlowClock()
    page = _LoginFlowPage(visible_text=visible_text)
    events: list[str] = []
    crawler, context, client = _configure_browser_session_test(
        monkeypatch,
        page=page,
        pong_results=[False],
        events=events,
    )
    budget = XHSManualWaitBudget(limit_seconds=600, monotonic=clock.monotonic)
    crawler._manual_wait_budget = budget
    instances = _install_observed_login_factory(monkeypatch, events)
    monkeypatch.setattr(xhs_core.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(xhs_core.asyncio, "sleep", clock.sleep)

    with pytest.raises(
        RuntimeError,
        match=f"xhs_login_verification_terminal:.*{terminal_marker}",
    ):
        await crawler._run_browser_session(object(), None, None)

    assert len(instances) == 1
    assert instances[0]._manual_wait_budget is budget
    assert events == ["pong:startup_login_probe", "login.begin"]
    assert clock.sleep_calls == []
    assert context.new_page_calls == 0
    assert crawler.launch_browser_with_cdp.await_count == 1
    assert crawler._pong_with_network_recovery.await_args_list == [
        call(stage="startup_login_probe")
    ]
    client.update_cookies.assert_not_awaited()
    crawler._open_behavior_search_page_with_recovery.assert_not_awaited()
    crawler._run_human_behavior_with_page_recovery.assert_not_awaited()
    crawler.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_storage_state_file_is_not_read_or_rewritten(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot_path = tmp_path / "storage-state.json"
    snapshot_path.write_text("legacy-sentinel\n", encoding="utf-8")
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH",
        str(snapshot_path),
    )
    async_playwright = MagicMock(
        side_effect=AssertionError("legacy snapshot must fail before browser start")
    )
    monkeypatch.setattr(xhs_core, "async_playwright", async_playwright)

    with pytest.raises(
        RuntimeError,
        match=(
            "^xhs_deprecated_login_input:"
            "TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH$"
        ),
    ):
        await XiaoHongShuCrawler().start()

    assert snapshot_path.read_text(encoding="utf-8") == "legacy-sentinel\n"
    assert list(tmp_path.iterdir()) == [snapshot_path]
    async_playwright.assert_not_called()


@pytest.mark.asyncio
async def test_context_cookies_still_seed_the_api_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = SimpleNamespace()
    crawler.context_page = SimpleNamespace()
    crawler._browser_identity_headers = AsyncMock(
        return_value={"user-agent": "Chromium/140"}
    )
    convert_cookies = AsyncMock(
        return_value=(
            "web_session=current; a1=signed",
            {"web_session": "current", "a1": "signed"},
        )
    )
    monkeypatch.setattr(
        xhs_core.utils,
        "convert_browser_context_cookies",
        convert_cookies,
    )

    client = await crawler.create_xhs_client(None)

    convert_cookies.assert_awaited_once_with(
        crawler.browser_context,
        urls=crawler.cookie_urls,
    )
    assert client.headers["Cookie"] == "web_session=current; a1=signed"
    assert client.cookie_dict == {"web_session": "current", "a1": "signed"}
    assert client.playwright_page is crawler.context_page
