from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import media_platform.xhs.core as xhs_core
from media_platform.xhs.core import XiaoHongShuCrawler
from media_platform.xhs.login import XiaoHongShuLogin


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


def test_xhs_crawler_has_no_storage_state_compatibility_surface() -> None:
    source = inspect.getsource(XiaoHongShuCrawler)

    assert not hasattr(XiaoHongShuCrawler, "_storage_state_path")
    assert not hasattr(XiaoHongShuCrawler, "_cookie_for_restore")
    assert not hasattr(XiaoHongShuCrawler, "_restore_storage_state")
    assert not hasattr(XiaoHongShuCrawler, "_write_storage_state")
    assert "TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH" not in source
    assert ".storage_state(" not in source


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
