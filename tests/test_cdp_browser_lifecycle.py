from __future__ import annotations

import signal
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import httpx
import pytest

from media_platform.xhs.core import XiaoHongShuCrawler
from tools.browser_launcher import BrowserLauncher
from tools.cdp_browser import CDPBrowserLifecycleError, CDPBrowserManager


class FakeProcess:
    def __init__(
        self,
        *,
        pid: int = 4242,
        returncode: int | None = None,
        wait_results: list[object] | None = None,
    ) -> None:
        self.pid = pid
        self.returncode = returncode
        self.wait_results = list(wait_results or [])
        self.terminate_calls = 0
        self.wait_calls: list[float] = []

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1

    def wait(self, timeout: float) -> int:
        self.wait_calls.append(timeout)
        if self.wait_results:
            result = self.wait_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            self.returncode = int(result)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class EventEmitter:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event: str) -> None:
        for handler in list(self.handlers.get(event, [])):
            handler(self)


class FakeBrowser(EventEmitter):
    def __init__(self) -> None:
        super().__init__()
        self.connected = True
        self.close_calls = 0
        self.contexts = []

    def is_connected(self) -> bool:
        return self.connected

    async def close(self) -> None:
        self.close_calls += 1
        self.connected = False
        self.emit("disconnected")


class FakeContext(EventEmitter):
    def __init__(self, pages: list[object] | None = None) -> None:
        super().__init__()
        self.pages = list(pages or [])
        self.closed = False
        self.close_calls = 0
        self.new_page = AsyncMock()

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self.emit("close")


def _connected_manager(
    *,
    process: FakeProcess | None = None,
) -> tuple[CDPBrowserManager, FakeBrowser, FakeContext]:
    manager = CDPBrowserManager()
    browser = FakeBrowser()
    context = FakeContext()
    manager.browser = browser
    manager.browser_context = context
    manager._connection_established = True
    manager._owns_browser_process = process is not None
    manager.launcher.browser_process = process
    manager.debug_port = 9444
    manager._observe_browser(browser)
    manager._observe_browser_context(context)
    return manager, browser, context


def test_launcher_rejects_overwriting_a_live_process(monkeypatch) -> None:
    process = FakeProcess(pid=5151)
    popen = MagicMock(return_value=process)
    monkeypatch.setattr("tools.browser_launcher.subprocess.Popen", popen)
    launcher = BrowserLauncher()

    first = launcher.launch_browser("/fake/chrome", 9444)
    with pytest.raises(
        RuntimeError,
        match=r"^browser_process_already_running:pid=5151$",
    ):
        launcher.launch_browser("/fake/chrome", 9555)

    assert first is process
    assert launcher.browser_process is process
    assert popen.call_count == 1


def test_launcher_rejects_a_second_call_while_popen_is_in_progress(
    monkeypatch,
) -> None:
    launcher = BrowserLauncher()
    process = FakeProcess(pid=5252)

    def launch_once(*_args, **_kwargs):
        with pytest.raises(
            RuntimeError,
            match=r"^browser_process_launch_in_progress$",
        ):
            launcher.launch_browser("/fake/chrome", 9555)
        return process

    popen = MagicMock(side_effect=launch_once)
    monkeypatch.setattr("tools.browser_launcher.subprocess.Popen", popen)

    assert launcher.launch_browser("/fake/chrome", 9444) is process
    assert launcher.browser_process is process
    assert popen.call_count == 1


def test_ready_wait_reports_early_process_exit_without_full_timeout() -> None:
    launcher = BrowserLauncher()
    process = FakeProcess(pid=6161, returncode=17)
    launcher.browser_process = process

    with pytest.raises(
        RuntimeError,
        match=r"browser_process_exited_before_cdp_ready:pid=6161:returncode=17",
    ):
        launcher.wait_for_browser_ready(9444, timeout=30)

    assert launcher.browser_process is process
    assert launcher.last_process_exit == {
        "event": "browser_process_exited",
        "reason": "browser_ready_wait",
        "pid": 6161,
        "returncode": 17,
        "observed_at": launcher.last_process_exit["observed_at"],
    }


def test_ready_wait_stops_when_running_process_exits_between_probes(
    monkeypatch,
) -> None:
    launcher = BrowserLauncher()
    process = FakeProcess(pid=6262)
    launcher.browser_process = process
    socket_instance = MagicMock()
    socket_instance.__enter__.return_value = socket_instance
    socket_instance.connect_ex.return_value = 1
    monkeypatch.setattr(
        "tools.browser_launcher.socket.socket",
        MagicMock(return_value=socket_instance),
    )
    monkeypatch.setattr(
        "tools.browser_launcher.time.sleep",
        lambda _seconds: setattr(process, "returncode", 19),
    )

    with pytest.raises(
        RuntimeError,
        match=r"browser_process_exited_before_cdp_ready:pid=6262:returncode=19",
    ):
        launcher.wait_for_browser_ready(9444, timeout=30)

    assert launcher.browser_process is process
    assert launcher.last_process_exit["reason"] == "browser_ready_wait"


def test_posix_cleanup_records_successful_term(monkeypatch) -> None:
    launcher = BrowserLauncher()
    launcher.system = "Darwin"
    process = FakeProcess(pid=7171, wait_results=[-15])
    launcher.browser_process = process
    killpg = MagicMock()
    monkeypatch.setattr("tools.browser_launcher.os.getpgid", lambda _pid: 7171)
    monkeypatch.setattr("tools.browser_launcher.os.killpg", killpg)

    result = launcher.cleanup(reason="normal_completion")

    assert result["status"] == "terminated"
    assert result["reason"] == "normal_completion"
    assert result["pid"] == 7171
    assert result["returncode"] == -15
    assert launcher.browser_process is None
    assert launcher.last_cleanup_result == result
    assert launcher.last_process_exit["reason"] == "planned_cleanup:normal_completion"
    killpg.assert_called_once_with(7171, signal.SIGTERM)


def test_posix_cleanup_escalates_term_to_kill(monkeypatch) -> None:
    launcher = BrowserLauncher()
    launcher.system = "Darwin"
    process = FakeProcess(
        pid=8181,
        wait_results=[subprocess.TimeoutExpired("chrome", 5), -9],
    )
    launcher.browser_process = process
    killpg = MagicMock()
    monkeypatch.setattr("tools.browser_launcher.os.getpgid", lambda _pid: 8181)
    monkeypatch.setattr("tools.browser_launcher.os.killpg", killpg)

    result = launcher.cleanup(reason="operator_interrupt")

    assert result["status"] == "killed"
    assert result["returncode"] == -9
    assert process.wait_calls == [5, 5]
    assert killpg.call_args_list == [
        call(8181, signal.SIGTERM),
        call(8181, signal.SIGKILL),
    ]


def test_cleanup_failure_retains_process_handle_and_evidence(monkeypatch) -> None:
    launcher = BrowserLauncher()
    launcher.system = "Darwin"
    process = FakeProcess(pid=9191)
    launcher.browser_process = process
    monkeypatch.setattr(
        "tools.browser_launcher.os.getpgid",
        MagicMock(side_effect=PermissionError("not allowed")),
    )

    result = launcher.cleanup(reason="failed_run")

    assert result == {
        "status": "failed",
        "reason": "failed_run",
        "pid": 9191,
        "returncode": None,
        "error": "PermissionError: not allowed",
    }
    assert launcher.browser_process is process
    assert launcher.last_cleanup_result == result
    assert launcher.cleanup_requested is True


def test_repeated_cleanup_does_not_signal_process_twice(monkeypatch) -> None:
    launcher = BrowserLauncher()
    launcher.system = "Darwin"
    process = FakeProcess(pid=1010, wait_results=[-15])
    launcher.browser_process = process
    killpg = MagicMock()
    monkeypatch.setattr("tools.browser_launcher.os.getpgid", lambda _pid: 1010)
    monkeypatch.setattr("tools.browser_launcher.os.killpg", killpg)

    first = launcher.cleanup(reason="normal_completion")
    second = launcher.cleanup(reason="duplicate_cleanup")

    assert second == first
    killpg.assert_called_once_with(1010, signal.SIGTERM)


def test_driver_disconnect_without_shutdown_latch_is_unexpected() -> None:
    process = FakeProcess(pid=1111)
    manager, browser, _context = _connected_manager(process=process)

    browser.connected = False
    browser.emit("disconnected")

    with pytest.raises(
        CDPBrowserLifecycleError,
        match=r"xhs_cdp_disconnected_unexpected:stage=search:pid=1111",
    ):
        manager.assert_alive("search")
    assert manager.lifecycle_snapshot()["unexpected"]["kind"] == "browser_disconnected"


def test_unexpected_context_close_is_latched_separately() -> None:
    process = FakeProcess(pid=1212)
    manager, _browser, context = _connected_manager(process=process)

    context.emit("close")

    with pytest.raises(
        CDPBrowserLifecycleError,
        match=r"xhs_browser_context_closed_unexpected:stage=detail:pid=1212",
    ):
        manager.assert_alive("detail")


@pytest.mark.asyncio
async def test_planned_cleanup_does_not_latch_close_or_disconnect() -> None:
    process = FakeProcess(pid=1313)
    manager, browser, context = _connected_manager(process=process)
    manager.launcher.cleanup = MagicMock(
        return_value={
            "status": "terminated",
            "reason": "normal_completion",
            "pid": 1313,
            "returncode": -15,
        }
    )

    first = await manager.cleanup(force=True, reason="normal_completion")
    second = await manager.cleanup(force=True, reason="duplicate_cleanup")

    assert first["status"] == "completed"
    assert second == first
    assert manager.lifecycle_snapshot()["unexpected"] == {}
    assert browser.close_calls == 1
    assert context.close_calls == 1
    manager.launcher.cleanup.assert_called_once_with(reason="normal_completion")


@pytest.mark.asyncio
async def test_start_latches_driver_disconnect_before_later_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    process = FakeProcess(pid=1353)
    manager, browser, context = _connected_manager(process=process)
    manager.launcher.cleanup = MagicMock(
        return_value={
            "status": "terminated",
            "reason": "playwright_context_exit",
            "pid": 1353,
            "returncode": -15,
        }
    )
    crawler = XiaoHongShuCrawler()
    crawler.cdp_manager = manager
    crawler.browser_context = context
    crawler._run_browser_session = AsyncMock()

    async def prepare_browser_shutdown() -> None:
        events.append("prepare")

    crawler._prepare_browser_shutdown = AsyncMock(
        side_effect=prepare_browser_shutdown
    )

    class DriverLifecycle:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args) -> None:
            events.append("driver_disconnect")
            browser.connected = False
            browser.emit("disconnected")

    monkeypatch.setattr(
        "media_platform.xhs.core.async_playwright",
        DriverLifecycle,
    )
    monkeypatch.setattr(
        "media_platform.xhs.core.config.ENABLE_IP_PROXY",
        False,
    )

    await crawler.start()

    assert events == ["prepare", "driver_disconnect"]
    assert manager.lifecycle_snapshot()["planned_cleanup_reason"] == (
        "playwright_context_exit"
    )
    assert manager.lifecycle_snapshot()["unexpected"] == {}
    assert manager.last_cleanup_result is None
    assert context.close_calls == 0
    assert browser.close_calls == 0
    assert manager.launcher.browser_process is process

    await crawler.close(force=True)

    assert events == ["prepare", "driver_disconnect", "prepare"]
    assert manager.lifecycle_snapshot()["unexpected"] == {}
    assert context.close_calls == 1
    manager.launcher.cleanup.assert_called_once_with(
        reason="playwright_context_exit"
    )


@pytest.mark.asyncio
async def test_manager_rejects_second_launch_attempt(monkeypatch) -> None:
    manager = CDPBrowserManager()
    context = FakeContext()
    monkeypatch.setattr("tools.cdp_browser.config.CDP_CONNECT_EXISTING", False)
    manager._get_browser_path = AsyncMock(return_value="/fake/chrome")
    manager.launcher.find_available_port = MagicMock(return_value=9444)
    manager._launch_browser = AsyncMock()
    manager._register_cleanup_handlers = MagicMock()
    manager._connect_via_cdp = AsyncMock()
    manager._create_browser_context = AsyncMock(return_value=context)

    assert await manager.launch_and_connect(playwright=MagicMock()) is context
    with pytest.raises(
        RuntimeError,
        match=r"^cdp_browser_manager_already_started$",
    ):
        await manager.launch_and_connect(playwright=MagicMock())

    manager._launch_browser.assert_awaited_once_with("/fake/chrome", False)
    manager._create_browser_context.assert_awaited_once()


def _crawler_with_healthy_cdp(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[XiaoHongShuCrawler, CDPBrowserManager, FakeProcess, list[float]]:
    page = SimpleNamespace(
        is_closed=lambda: False,
        url="https://www.xiaohongshu.com/search_result",
    )
    process = FakeProcess(pid=1414)
    manager, _browser, context = _connected_manager(process=process)
    context.pages.append(page)

    crawler = XiaoHongShuCrawler()
    crawler.context_page = page
    crawler.browser_context = context
    crawler.cdp_manager = manager
    crawler._record_network_recovery_event = AsyncMock()
    clock = {"now": 20.0}
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock["now"]

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    crawler._popup_monotonic = monotonic
    crawler._popup_sleep = sleep
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_WAIT_SECONDS", "10")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MIN_SECONDS", "2")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MAX_SECONDS", "4")
    return crawler, manager, process, sleeps


@pytest.mark.asyncio
async def test_public_network_outage_keeps_healthy_browser_without_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, manager, process, sleeps = _crawler_with_healthy_cdp(monkeypatch)
    request = httpx.Request("GET", "https://edith.xiaohongshu.com/api/search")
    operation = AsyncMock(
        side_effect=[httpx.ReadTimeout("offline", request=request), "restored"]
    )

    result = await crawler._run_with_network_recovery(
        operation,
        stage="search:frontier:page=3",
    )

    assert result == "restored"
    assert operation.await_count == 2
    assert sleeps == [2.0]
    assert manager.launcher.browser_process is process
    assert manager.last_cleanup_result is None
    assert manager.lifecycle_snapshot()["unexpected"] == {}
    assert manager.browser_context.new_page.await_count == 0


@pytest.mark.asyncio
async def test_process_exit_during_network_wait_is_terminal_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, manager, process, sleeps = _crawler_with_healthy_cdp(monkeypatch)
    request = httpx.Request("GET", "https://edith.xiaohongshu.com/api/search")
    operation = AsyncMock(
        side_effect=httpx.ReadTimeout("offline", request=request)
    )
    original_sleep = crawler._popup_sleep

    async def exit_during_sleep(seconds: float) -> None:
        await original_sleep(seconds)
        process.returncode = 23

    crawler._popup_sleep = exit_during_sleep

    with pytest.raises(
        CDPBrowserLifecycleError,
        match=r"xhs_browser_process_exited:stage=search:frontier:page=3:pid=1414:returncode=23",
    ):
        await crawler._run_with_network_recovery(
            operation,
            stage="search:frontier:page=3",
        )

    assert operation.await_count == 1
    assert sleeps == [2.0]
    assert manager.last_cleanup_result is None
    assert manager.launcher.browser_process is process
    assert manager.browser_context.new_page.await_count == 0
