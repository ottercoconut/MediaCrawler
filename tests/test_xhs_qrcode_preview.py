from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from media_platform.xhs import login as login_module
from media_platform.xhs.login import XiaoHongShuLogin
from media_platform.xhs.manual_wait import XHSManualWaitBudgetExhausted


class FakeContext:
    async def cookies(self) -> list[dict[str, str]]:
        return [{"name": "web_session", "value": "before"}]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class ImmediateFuture:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error

    def add_done_callback(self, callback) -> None:
        callback(self)

    def result(self) -> None:
        if self.error is not None:
            raise self.error


def make_login() -> tuple[XiaoHongShuLogin, SimpleNamespace]:
    page = SimpleNamespace(reload=AsyncMock())
    login = XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=FakeContext(),
        context_page=page,
    )
    return login, page


def configure_qrcode_flow(
    monkeypatch: pytest.MonkeyPatch,
    login: XiaoHongShuLogin,
    *,
    wait_seconds: int,
) -> tuple[AsyncMock, MagicMock]:
    clock = FakeClock()
    find_qrcode = AsyncMock(return_value="qr-image")
    show_qrcode = MagicMock()
    monkeypatch.setattr(login_module.utils, "find_login_qrcode", find_qrcode)
    monkeypatch.setattr(login_module.utils, "show_qrcode", show_qrcode)
    monkeypatch.setattr(login_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(login_module.asyncio, "sleep", clock.sleep)
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS",
        str(wait_seconds),
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_QR_REFRESH_SECONDS", "180")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_LOGIN_POLL_SECONDS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_STABLE_LOGIN_SECONDS", "5")

    def observe_qr() -> dict[str, object]:
        return {
            "terminal_security": [],
            "terminal_login_error": [],
            "manual_in_progress": False,
            "profile_visible": False,
            "qr_visible": True,
            "qr_expired": False,
            "pages": [{"login_or_qr": ["扫码登录"]}],
        }

    async def check_login_state(_session: str) -> bool:
        login._last_login_observation = observe_qr()
        return False

    login._check_login_state_once = AsyncMock(side_effect=check_login_state)
    return find_qrcode, show_qrcode


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enable_cdp", "cdp_headless", "playwright_headless"),
    [
        (True, False, True),
        (False, True, False),
    ],
)
async def test_headed_qrcode_never_schedules_initial_or_refreshed_preview(
    monkeypatch: pytest.MonkeyPatch,
    enable_cdp: bool,
    cdp_headless: bool,
    playwright_headless: bool,
) -> None:
    login, page = make_login()
    find_qrcode, show_qrcode = configure_qrcode_flow(
        monkeypatch,
        login,
        wait_seconds=182,
    )
    monkeypatch.setattr(login_module.config, "ENABLE_CDP_MODE", enable_cdp)
    monkeypatch.setattr(login_module.config, "CDP_HEADLESS", cdp_headless)
    monkeypatch.setattr(login_module.config, "HEADLESS", playwright_headless)
    run_in_executor = MagicMock(
        side_effect=AssertionError("headed login must not open an OS preview")
    )
    monkeypatch.setattr(
        login_module.asyncio,
        "get_running_loop",
        lambda: SimpleNamespace(run_in_executor=run_in_executor),
    )

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    assert find_qrcode.await_count == 2
    assert page.reload.await_args_list == [
        call(wait_until="domcontentloaded", timeout=30_000)
    ]
    run_in_executor.assert_not_called()
    show_qrcode.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enable_cdp", "cdp_headless", "playwright_headless"),
    [
        (True, True, False),
        (False, False, True),
    ],
)
async def test_headless_qrcode_still_schedules_required_preview(
    monkeypatch: pytest.MonkeyPatch,
    enable_cdp: bool,
    cdp_headless: bool,
    playwright_headless: bool,
) -> None:
    login, page = make_login()
    find_qrcode, show_qrcode = configure_qrcode_flow(
        monkeypatch,
        login,
        wait_seconds=2,
    )

    def run_in_executor(*, executor, func):
        assert executor is None
        func()
        return ImmediateFuture()

    executor = MagicMock(side_effect=run_in_executor)
    monkeypatch.setattr(login_module.config, "ENABLE_CDP_MODE", enable_cdp)
    monkeypatch.setattr(login_module.config, "CDP_HEADLESS", cdp_headless)
    monkeypatch.setattr(login_module.config, "HEADLESS", playwright_headless)
    monkeypatch.setattr(
        login_module.asyncio,
        "get_running_loop",
        lambda: SimpleNamespace(run_in_executor=executor),
    )

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    find_qrcode.assert_awaited_once()
    page.reload.assert_not_awaited()
    executor.assert_called_once()
    show_qrcode.assert_called_once_with("qr-image")


@pytest.mark.asyncio
async def test_headless_preview_failure_does_not_restart_or_abort_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login, page = make_login()
    find_qrcode, show_qrcode = configure_qrcode_flow(
        monkeypatch,
        login,
        wait_seconds=2,
    )
    show_qrcode.side_effect = RuntimeError("preview unavailable")

    def run_in_executor(*, executor, func):
        assert executor is None
        try:
            func()
        except Exception as exc:
            return ImmediateFuture(exc)
        return ImmediateFuture()

    executor = MagicMock(side_effect=run_in_executor)
    monkeypatch.setattr(login_module.config, "ENABLE_CDP_MODE", True)
    monkeypatch.setattr(login_module.config, "CDP_HEADLESS", True)
    monkeypatch.setattr(login_module.config, "HEADLESS", False)
    monkeypatch.setattr(
        login_module.asyncio,
        "get_running_loop",
        lambda: SimpleNamespace(run_in_executor=executor),
    )

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    find_qrcode.assert_awaited_once()
    show_qrcode.assert_called_once_with("qr-image")
    page.reload.assert_not_awaited()
