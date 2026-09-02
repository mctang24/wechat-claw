"""Single-process runtime for the Codex and WeChat bridge."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable

from .app_server_process import AppServerProcess
from .bridge import CodexBridge
from .codex_protocol import CodexAppServerClient
from .lease_server import LeaseServer
from .paths import (
    APP_SERVER_ENDPOINT,
    LEASE_SOCKET_PATH,
    REAL_CODEX_BINARY,
    WECHAT_BINDING_PATH,
    WECHAT_CREDENTIALS_PATH,
    WECHAT_QR_PAGE_PATH,
)
from .wechat_adapter import BotProtocol, WeChatAdapter, build_bot
from .wechat_binding import BindingStore


class DaemonError(RuntimeError):
    """Raised when a required daemon event loop stops unexpectedly."""


class WeChatClawDaemon:
    def __init__(
        self,
        *,
        app_server: AppServerProcess | None = None,
        client: CodexAppServerClient | None = None,
        lease_server: LeaseServer | None = None,
        bot: BotProtocol | None = None,
    ) -> None:
        self.app_server = app_server or AppServerProcess(
            APP_SERVER_ENDPOINT,
            codex_binary=REAL_CODEX_BINARY,
        )
        self.client = client or CodexAppServerClient(APP_SERVER_ENDPOINT)
        self.lease_server = lease_server or LeaseServer(
            LEASE_SOCKET_PATH,
            APP_SERVER_ENDPOINT,
        )
        self.bot = bot or build_bot(WECHAT_CREDENTIALS_PATH, WECHAT_QR_PAGE_PATH)
        self.bridge: CodexBridge | None = None

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        bridge_holder: dict[str, CodexBridge] = {}

        async def handle_text(text: str) -> str | None:
            return await bridge_holder["bridge"].handle_text(text)

        adapter = WeChatAdapter(
            self.bot,
            BindingStore(WECHAT_BINDING_PATH),
            handle_text,
        )

        await self.app_server.start()
        try:
            await self.client.connect()
            try:
                await self.lease_server.start()
                try:
                    bridge = CodexBridge(
                        self.client,
                        self.lease_server,
                        adapter.send_to_bound,
                    )
                    self.bridge = bridge
                    bridge_holder["bridge"] = bridge
                    await self._run_loops(adapter, bridge, stop_event)
                finally:
                    _shutdown_log("bridge")
                    if self.bridge is not None:
                        await self.bridge.close()
                    self.bridge = None
                    _shutdown_log("lease server")
                    await self.lease_server.close()
            finally:
                _shutdown_log("app-server client")
                await self.client.close()
        finally:
            _shutdown_log("app-server process")
            await self.app_server.stop()
            _shutdown_log("complete")

    async def _run_loops(
        self,
        adapter: WeChatAdapter,
        bridge: CodexBridge,
        stop_event: asyncio.Event,
    ) -> None:
        tasks = {
            asyncio.create_task(adapter.run(), name="wechat-claw-wechat"),
            asyncio.create_task(
                self._notification_loop(bridge),
                name="wechat-claw-notifications",
            ),
            asyncio.create_task(
                self._server_request_loop(bridge),
                name="wechat-claw-approvals",
            ),
            asyncio.create_task(
                self._lease_loop(bridge),
                name="wechat-claw-leases",
            ),
            asyncio.create_task(
                self._maintenance_loop(bridge),
                name="wechat-claw-maintenance",
            ),
        }
        stop_task = asyncio.create_task(stop_event.wait(), name="wechat-claw-stop")
        try:
            done, _ = await asyncio.wait(
                (*tasks, stop_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done:
                return
            completed = next(task for task in done if task in tasks)
            exception = completed.exception()
            if exception is not None:
                raise exception
            raise DaemonError(f"核心任务意外退出：{completed.get_name()}")
        finally:
            _shutdown_log("background loops")
            _stop_bot(self.bot)
            stop_task.cancel()
            for task in tasks:
                task.cancel()
            done, pending = await asyncio.wait(
                (stop_task, *tasks),
                timeout=3.0,
            )
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            for task in pending:
                task.cancel()
            _shutdown_log("background loops complete")

    async def _notification_loop(self, bridge: CodexBridge) -> None:
        while True:
            await bridge.handle_notification(await self.client.next_notification())

    async def _server_request_loop(self, bridge: CodexBridge) -> None:
        while True:
            await bridge.handle_server_request(await self.client.next_server_request())

    async def _lease_loop(self, bridge: CodexBridge) -> None:
        while True:
            await bridge.handle_lease_event(await self.lease_server.next_event())

    async def _maintenance_loop(self, bridge: CodexBridge) -> None:
        while True:
            await asyncio.sleep(1)
            await bridge.expire_approvals()


async def run_daemon() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await WeChatClawDaemon().run(stop_event)
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)


def _stop_bot(bot: BotProtocol) -> None:
    stop: Callable[[], object] | None = getattr(bot, "stop", None)
    if stop is not None:
        stop()


def _shutdown_log(stage: str) -> None:
    print(f"[wechat-claw] shutdown: {stage}", flush=True)
