# -*- coding: utf-8 -*-
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from tools.cdp_browser import CDPBrowserManager
from tools.browser_launcher import BrowserLauncher


@pytest.mark.asyncio
async def test_existing_browser_connects_directly_to_devtools_browser(monkeypatch):
    monkeypatch.setattr(config, "CDP_CONNECT_EXISTING", True)
    monkeypatch.setattr(config, "BROWSER_LAUNCH_TIMEOUT", 60)

    manager = CDPBrowserManager()
    manager.debug_port = 9222
    manager._get_browser_websocket_url = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("existing browser mode must not call /json/version")
    )

    browser = MagicMock()
    browser.is_connected.return_value = True
    browser.contexts = []

    playwright = MagicMock()
    playwright.chromium.connect_over_cdp = AsyncMock(return_value=browser)

    await manager._connect_via_cdp(playwright)

    playwright.chromium.connect_over_cdp.assert_awaited_once_with(
        "ws://localhost:9222/devtools/browser",
        timeout=60000,
    )


@pytest.mark.asyncio
async def test_existing_browser_falls_back_to_discovered_websocket_url(monkeypatch):
    monkeypatch.setattr(config, "CDP_CONNECT_EXISTING", True)
    monkeypatch.setattr(config, "BROWSER_LAUNCH_TIMEOUT", 60)

    manager = CDPBrowserManager()
    manager.debug_port = 9222
    manager._get_browser_websocket_url = AsyncMock(  # type: ignore[method-assign]
        return_value="ws://localhost:9222/devtools/browser/generated-id"
    )

    browser = MagicMock()
    browser.is_connected.return_value = True
    browser.contexts = []

    playwright = MagicMock()
    playwright.chromium.connect_over_cdp = AsyncMock(
        side_effect=[RuntimeError("direct websocket failed"), browser]
    )

    await manager._connect_via_cdp(playwright)

    manager._get_browser_websocket_url.assert_awaited_once_with(9222)
    assert playwright.chromium.connect_over_cdp.await_args_list[0].args == (
        "ws://localhost:9222/devtools/browser",
    )
    assert playwright.chromium.connect_over_cdp.await_args_list[0].kwargs == {
        "timeout": 60000,
    }
    assert playwright.chromium.connect_over_cdp.await_args_list[1].args == (
        "ws://localhost:9222/devtools/browser/generated-id",
    )
    assert playwright.chromium.connect_over_cdp.await_args_list[1].kwargs == {
        "timeout": 60000,
    }


@pytest.mark.asyncio
async def test_launched_browser_uses_discovered_websocket_url(monkeypatch):
    monkeypatch.setattr(config, "CDP_CONNECT_EXISTING", False)

    manager = CDPBrowserManager()
    manager.debug_port = 9223
    manager._get_browser_websocket_url = AsyncMock(  # type: ignore[method-assign]
        return_value="ws://localhost:9223/devtools/browser/generated-id"
    )

    browser = MagicMock()
    browser.is_connected.return_value = True
    browser.contexts = []

    playwright = MagicMock()
    playwright.chromium.connect_over_cdp = AsyncMock(return_value=browser)

    await manager._connect_via_cdp(playwright)

    manager._get_browser_websocket_url.assert_awaited_once_with(9223)
    playwright.chromium.connect_over_cdp.assert_awaited_once_with(
        "ws://localhost:9223/devtools/browser/generated-id"
    )


def test_xhs_browser_uses_stable_native_window_instead_of_maximizing(monkeypatch):
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_WINDOW_SIZE", "1450,900")
    popen = MagicMock()
    monkeypatch.setattr("tools.browser_launcher.subprocess.Popen", popen)

    BrowserLauncher().launch_browser(
        browser_path="/Applications/Google Chrome",
        debug_port=9222,
        headless=False,
        user_data_dir="/tmp/xhs-profile",
    )

    arguments = popen.call_args.args[0]
    assert "--window-size=1450,900" in arguments
    assert "--start-maximized" not in arguments


def test_xhs_browser_rejects_invalid_window_size(monkeypatch):
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_WINDOW_SIZE", "invalid")

    with pytest.raises(RuntimeError, match="invalid TRIPPOSTCOLLECT_XHS_WINDOW_SIZE"):
        BrowserLauncher.xhs_window_size_argument()


@pytest.mark.asyncio
async def test_failed_cdp_launch_is_force_cleaned_once_by_owning_manager(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "CDP_CONNECT_EXISTING", False)
    manager = CDPBrowserManager()
    manager._get_browser_path = AsyncMock(return_value="/fake/chrome")
    manager.launcher.find_available_port = MagicMock(return_value=9444)
    manager._launch_browser = AsyncMock()
    manager._register_cleanup_handlers = MagicMock()
    manager._connect_via_cdp = AsyncMock(
        side_effect=RuntimeError("cdp handshake failed")
    )
    manager._create_browser_context = AsyncMock(
        side_effect=AssertionError("context must not be created after failed handshake")
    )
    manager.cleanup = AsyncMock()

    with pytest.raises(RuntimeError, match="cdp handshake failed"):
        await manager.launch_and_connect(playwright=MagicMock())

    manager._launch_browser.assert_awaited_once_with("/fake/chrome", False)
    manager._register_cleanup_handlers.assert_called_once_with()
    manager.cleanup.assert_awaited_once_with(force=True)
    manager._create_browser_context.assert_not_awaited()
