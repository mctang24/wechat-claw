from __future__ import annotations

import asyncio
import unittest

from wechat_claw.codex_protocol import CodexConnectionClosed
from wechat_claw.daemon import WeChatClawDaemon


class FakeAppServer:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


class FakeClient:
    def __init__(self) -> None:
        self.connected = 0
        self.closed = 0
        self.release = asyncio.Event()

    async def connect(self) -> None:
        self.connected += 1

    async def close(self) -> None:
        self.closed += 1
        self.release.set()

    async def next_notification(self):
        await self.release.wait()
        raise CodexConnectionClosed("closed")

    async def next_server_request(self):
        await self.release.wait()
        raise CodexConnectionClosed("closed")


class FakeLeaseServer:
    def __init__(self) -> None:
        self.started = 0
        self.closed = 0
        self.release = asyncio.Event()

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        self.closed += 1
        self.release.set()

    async def next_event(self):
        await self.release.wait()
        raise RuntimeError("closed")

    def bind_thread(self, _thread: dict) -> bool:
        return False


class FakeBot:
    def __init__(self) -> None:
        self._context_tokens = {}
        self.handler = None
        self.release = asyncio.Event()
        self.stopped = 0

    def on_message(self, handler):
        self.handler = handler

    async def login(self) -> None:
        return None

    async def start(self) -> None:
        await self.release.wait()

    async def reply(self, _message, _text: str) -> None:
        return None

    async def send(self, _user_id: str, _text: str) -> None:
        return None

    def stop(self) -> None:
        self.stopped += 1
        self.release.set()


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_event_closes_every_component_and_clears_memory(self) -> None:
        app_server = FakeAppServer()
        client = FakeClient()
        lease_server = FakeLeaseServer()
        bot = FakeBot()
        daemon = WeChatClawDaemon(
            app_server=app_server,
            client=client,
            lease_server=lease_server,
            bot=bot,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(daemon.run(stop))
        while daemon.bridge is None:
            await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, 1)

        self.assertEqual((app_server.started, app_server.stopped), (1, 1))
        self.assertEqual((client.connected, client.closed), (1, 1))
        self.assertEqual((lease_server.started, lease_server.closed), (1, 1))
        self.assertEqual(bot.stopped, 1)
        self.assertIsNone(daemon.bridge)

    async def test_core_loop_failure_propagates_and_still_cleans_up(self) -> None:
        app_server = FakeAppServer()
        client = FakeClient()
        lease_server = FakeLeaseServer()
        bot = FakeBot()
        daemon = WeChatClawDaemon(
            app_server=app_server,
            client=client,
            lease_server=lease_server,
            bot=bot,
        )
        task = asyncio.create_task(daemon.run(asyncio.Event()))
        while daemon.bridge is None:
            await asyncio.sleep(0)
        client.release.set()
        with self.assertRaises(CodexConnectionClosed):
            await asyncio.wait_for(task, 1)

        self.assertEqual(app_server.stopped, 1)
        self.assertEqual(client.closed, 1)
        self.assertEqual(lease_server.closed, 1)
        self.assertEqual(bot.stopped, 1)


if __name__ == "__main__":
    unittest.main()
