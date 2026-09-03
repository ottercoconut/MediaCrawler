from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from media_platform.xhs import login as login_module
from media_platform.xhs.login import XiaoHongShuLogin


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep: Callable[[float], None] | None = None

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.now)


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector

    async def count(self) -> int:
        return 1 if self.selector == "body" else int(
            self.selector in self.page.visible_selectors
        )

    async def inner_text(self, timeout: int) -> str:
        assert timeout > 0
        if self.page.inner_text_hook is not None:
            self.page.inner_text_hook(self.page)
        return self.page.visible_text

    async def is_visible(self, timeout: int) -> bool:
        assert timeout > 0
        return self.selector in self.page.visible_selectors

    async def click(self, timeout: int) -> None:
        assert timeout > 0
        self.page.events.append(("click", self.selector))
        self.page.click_times.append(self.page.clock.now)
        if self.page.click_hook is not None:
            self.page.click_hook(self.page, self.selector)


class FakePage:
    def __init__(self, clock: FakeClock, *, name: str = "login") -> None:
        self.clock = clock
        self.name = name
        self.url = f"https://www.xiaohongshu.com/{name}"
        self.visible_text = ""
        self.hidden_html = ""
        self.visible_selectors: set[str] = set()
        self.closed = False
        self.reload_times: list[float] = []
        self.click_times: list[float] = []
        self.events: list[tuple[str, str]] = []
        self.inner_text_hook: Callable[[FakePage], None] | None = None
        self.click_hook: Callable[[FakePage, str], None] | None = None

    @property
    def frames(self) -> list[FakePage]:
        return [self]

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def is_visible(self, selector: str, timeout: int) -> bool:
        assert timeout > 0
        return selector in self.visible_selectors

    async def content(self) -> str:
        return self.hidden_html or self.visible_text

    async def reload(self, *, wait_until: str, timeout: int) -> None:
        assert wait_until == "domcontentloaded"
        assert timeout == 30_000
        self.reload_times.append(self.clock.now)

    async def bring_to_front(self) -> None:
        self.events.append(("front", self.name))

    async def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


class FakeContext:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages

    async def cookies(self) -> list[dict[str, str]]:
        return [{"name": "web_session", "value": "before"}]

    async def new_page(self) -> FakePage:
        page = FakePage(self.pages[0].clock, name="new-login")
        self.pages.append(page)
        return page


def configure_virtual_login(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    *,
    wait_seconds: int,
    refresh_seconds: int = 180,
    stable_seconds: int = 5,
) -> None:
    monkeypatch.setattr(login_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(login_module.asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(login_module.utils, "show_qrcode", lambda _: None)

    async def find_login_qrcode(page, selector: str):
        assert selector == XiaoHongShuLogin._QRCODE_SELECTOR
        return "qr-image" if selector in page.visible_selectors else None

    monkeypatch.setattr(login_module.utils, "find_login_qrcode", find_login_qrcode)
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS", str(wait_seconds))
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_QR_REFRESH_SECONDS", str(refresh_seconds))
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_LOGIN_POLL_SECONDS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_STABLE_LOGIN_SECONDS", str(stable_seconds))


def make_login(page: FakePage, *extra_pages: FakePage) -> XiaoHongShuLogin:
    return XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=FakeContext([page, *extra_pages]),
        context_page=page,
    )


@pytest.mark.asyncio
async def test_headed_qrcode_does_not_schedule_local_image_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录 打开小红书扫一扫"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=2)
    monkeypatch.setattr(login_module.config, "ENABLE_CDP_MODE", True)
    monkeypatch.setattr(login_module.config, "CDP_HEADLESS", False)
    monkeypatch.setattr(login_module.config, "HEADLESS", False)

    loop = asyncio.get_running_loop()
    preview_jobs: list[Callable[[], None]] = []

    def capture_executor_job(executor, func, *args):
        assert executor is None
        preview_jobs.append(lambda: func(*args))
        future = loop.create_future()
        future.set_result(None)
        return future

    monkeypatch.setattr(loop, "run_in_executor", capture_executor_job)

    with pytest.raises(RuntimeError, match="within 2s"):
        await login.login_by_qrcode()

    assert preview_jobs == []
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_qrcode_reload_is_clamped_to_at_least_180_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录 打开小红书扫一扫"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    login = make_login(page)
    configure_virtual_login(
        monkeypatch,
        clock,
        wait_seconds=181,
        refresh_seconds=90,
    )

    with pytest.raises(RuntimeError, match="xhs_qrcode_login_timeout"):
        await login.login_by_qrcode()

    assert page.reload_times == [pytest.approx(180.0)]


@pytest.mark.asyncio
async def test_visible_verification_latches_and_prevents_later_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=190)

    def advance_state(now: float) -> None:
        if now >= 179:
            page.visible_text = "请输入验证码"
            page.visible_selectors.discard(XiaoHongShuLogin._QRCODE_SELECTOR)
            page.visible_selectors.add("input[placeholder*='验证码']")

    clock.on_sleep = advance_state

    with pytest.raises(RuntimeError, match="within 190s"):
        await login.login_by_qrcode()

    assert login._last_login_observation["manual_in_progress"] is True
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_english_sms_error_latches_despite_background_qr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录 SMS Verification Parameter error Refresh"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=181)

    with pytest.raises(RuntimeError, match="within 181s"):
        await login.login_by_qrcode()

    assert login._last_login_observation["manual_in_progress"] is True
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_verification_appearing_at_180_seconds_cancels_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=185)
    observations_at_boundary = 0

    def on_inner_text(current_page: FakePage) -> None:
        nonlocal observations_at_boundary
        if clock.now == 180:
            observations_at_boundary += 1
            if observations_at_boundary == 2:
                current_page.visible_text = "请输入验证码"
                current_page.visible_selectors.discard(
                    XiaoHongShuLogin._QRCODE_SELECTOR
                )
                current_page.visible_selectors.add("input[placeholder*='验证码']")

    page.inner_text_hook = on_inner_text

    with pytest.raises(RuntimeError, match="xhs_qrcode_login_timeout"):
        await login.login_by_qrcode()

    assert observations_at_boundary >= 2
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_expired_pure_qr_uses_component_refresh_before_page_reload_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录 打开小红书扫一扫"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    refresh_selector = XiaoHongShuLogin._QR_COMPONENT_REFRESH_SELECTORS[0]
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=220)
    component_refreshed = False

    def advance_state(now: float) -> None:
        if now >= 60 and not component_refreshed:
            page.visible_text = "二维码已过期 点击刷新 验证码"
            page.visible_selectors.discard(XiaoHongShuLogin._QRCODE_SELECTOR)
            page.visible_selectors.update(
                {
                    refresh_selector,
                    "input[placeholder*='验证码']",
                }
            )

    def refresh_component(current_page: FakePage, selector: str) -> None:
        nonlocal component_refreshed
        assert selector == refresh_selector
        component_refreshed = True
        current_page.visible_text = "扫码登录 打开小红书扫一扫"
        current_page.visible_selectors.discard(refresh_selector)
        current_page.visible_selectors.discard("input[placeholder*='验证码']")
        current_page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)

    clock.on_sleep = advance_state
    page.click_hook = refresh_component

    with pytest.raises(RuntimeError, match="within 220s"):
        await login.login_by_qrcode()

    assert page.click_times == [pytest.approx(60.0)]
    assert page.events == [("click", refresh_selector)]
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_scan_transition_during_expiry_confirmation_cancels_component_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    refresh_selector = XiaoHongShuLogin._QR_COMPONENT_REFRESH_SELECTORS[0]
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=185)
    expiry_observations = 0
    manual_transitioned = False

    def advance_state(now: float) -> None:
        if now >= 60 and not manual_transitioned:
            page.visible_text = "二维码已过期 点击刷新"
            page.visible_selectors.discard(XiaoHongShuLogin._QRCODE_SELECTOR)
            page.visible_selectors.add(refresh_selector)

    def transition_on_second_observation(current_page: FakePage) -> None:
        nonlocal expiry_observations, manual_transitioned
        if clock.now != 60:
            return
        expiry_observations += 1
        if expiry_observations == 2:
            manual_transitioned = True
            current_page.visible_text = "已扫码 请在手机上确认"
            current_page.visible_selectors.discard(refresh_selector)

    clock.on_sleep = advance_state
    page.inner_text_hook = transition_on_second_observation

    with pytest.raises(RuntimeError, match="within 185s"):
        await login.login_by_qrcode()

    assert expiry_observations >= 2
    assert login._last_login_observation["manual_in_progress"] is True
    assert page.click_times == []
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_manual_progress_latch_blocks_later_expired_qr_component_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    refresh_selector = XiaoHongShuLogin._QR_COMPONENT_REFRESH_SELECTORS[0]
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=190)

    def advance_state(now: float) -> None:
        if 50 <= now < 70:
            page.visible_text = "请输入验证码"
            page.visible_selectors.discard(XiaoHongShuLogin._QRCODE_SELECTOR)
            page.visible_selectors.add("input[placeholder*='验证码']")
        elif now >= 70:
            page.visible_text = "二维码已过期 点击刷新"
            page.visible_selectors.discard("input[placeholder*='验证码']")
            page.visible_selectors.add(refresh_selector)

    clock.on_sleep = advance_state

    with pytest.raises(RuntimeError, match="within 190s"):
        await login.login_by_qrcode()

    assert page.click_times == []
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_expired_qr_without_component_button_never_reloads_before_180s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=181)

    def advance_state(now: float) -> None:
        if now >= 60:
            page.visible_text = "二维码已过期"
            page.visible_selectors.discard(XiaoHongShuLogin._QRCODE_SELECTOR)

    clock.on_sleep = advance_state

    with pytest.raises(RuntimeError, match="within 181s"):
        await login.login_by_qrcode()

    assert page.click_times == []
    assert page.reload_times == [pytest.approx(180.0)]


@pytest.mark.asyncio
async def test_visible_checkpoint_wins_over_stale_profile_button() -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "请输入验证码"
    page.visible_selectors.update(
        {
            XiaoHongShuLogin._PROFILE_SELECTORS[0],
            "input[placeholder*='验证码']",
        }
    )
    login = make_login(page)

    assert await login._check_login_state_once("before") is False
    assert login._last_login_observation["profile_visible"] is True
    assert login._last_login_observation["visible_checkpoint"] is True


@pytest.mark.asyncio
async def test_hidden_checkpoint_html_does_not_mask_visible_signed_in_ui() -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "首页 我"
    page.hidden_html = "<div hidden>请输入验证码</div>"
    page.visible_selectors.add(XiaoHongShuLogin._PROFILE_SELECTORS[0])
    login = make_login(page)

    assert await login._check_login_state_once("before") is True
    assert login._last_login_observation["visible_checkpoint"] is False


@pytest.mark.asyncio
async def test_unrelated_retained_qr_tab_does_not_block_profile_success() -> None:
    clock = FakeClock()
    signed_in = FakePage(clock, name="signed-in")
    signed_in.visible_text = "首页 我"
    signed_in.visible_selectors.add(XiaoHongShuLogin._PROFILE_SELECTORS[0])
    stale_qr = FakePage(clock, name="stale-qr")
    stale_qr.visible_text = "扫码登录"
    stale_qr.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    login = make_login(signed_in, stale_qr)

    assert await login._check_login_state_once("before") is True
    assert login._last_login_observation["visible_checkpoint"] is False


@pytest.mark.asyncio
async def test_login_requires_stable_ui_after_transient_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "首页 我"
    page.visible_selectors.add(XiaoHongShuLogin._PROFILE_SELECTORS[0])
    login = make_login(page)
    configure_virtual_login(
        monkeypatch,
        clock,
        wait_seconds=20,
        stable_seconds=5,
    )

    def advance_state(now: float) -> None:
        if 2 <= now < 4:
            page.visible_text = "请输入验证码"
            page.visible_selectors.discard(XiaoHongShuLogin._PROFILE_SELECTORS[0])
            page.visible_selectors.add("input[placeholder*='验证码']")
        elif now >= 4:
            page.visible_text = "首页 我"
            page.visible_selectors.discard("input[placeholder*='验证码']")
            page.visible_selectors.add(XiaoHongShuLogin._PROFILE_SELECTORS[0])

    clock.on_sleep = advance_state

    await login.login_by_qrcode()

    assert clock.now >= 9
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_qrcode_timeout_and_security_limit_raise_runtime_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "安全限制"
    page.visible_selectors.add(XiaoHongShuLogin._PROFILE_SELECTORS[0])
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=600)

    with pytest.raises(RuntimeError, match="xhs_platform_security_limit_300011"):
        await login.login_by_qrcode()


@pytest.mark.asyncio
async def test_login_polling_preserves_verification_popup() -> None:
    clock = FakeClock()
    primary = FakePage(clock)
    popup = FakePage(clock, name="verification")
    login = make_login(primary, popup)

    assert await login._single_login_page() is primary
    assert popup.closed is False
    assert popup.events == []
