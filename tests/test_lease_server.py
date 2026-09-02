from __future__ import annotations

import asyncio
import json
import stat
import tempfile
import unittest
from pathlib import Path

from wechat_claw.lease_server import LeaseBound, LeaseClosed, LeaseServer


async def register(path: Path, cwd: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(
        json.dumps(
            {
                "type": "register",
                "cwd": cwd,
                "tmux": {"session": "s", "window": "w", "pane": "p"},
            }
        ).encode()
        + b"\n"
    )
    await writer.drain()
    return reader, writer


async def read_message(reader: asyncio.StreamReader) -> dict[str, object]:
    return json.loads(await asyncio.wait_for(reader.readline(), 1))


class LeaseServerTest(unittest.IsolatedAsyncioTestCase):
    async def test_close_does_not_wait_forever_for_server_wait_closed(self) -> None:
        class HangingServer:
            closed = False

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            server = LeaseServer(
                Path(directory) / "lease.sock",
                "ws://127.0.0.1:48731",
                close_timeout=0.01,
            )
            hanging = HangingServer()
            server._server = hanging

            await asyncio.wait_for(server.close(), 0.2)

            self.assertTrue(hanging.closed)
            self.assertIsNone(server._server)

    async def test_second_server_cannot_replace_live_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lease.sock"
            first = LeaseServer(path, "ws://127.0.0.1:48731")
            second = LeaseServer(path, "ws://127.0.0.1:48731")
            await first.start()
            try:
                with self.assertRaisesRegex(Exception, "already running"):
                    await second.start()
                reader, writer = await asyncio.open_unix_connection(path)
                writer.close()
                await writer.wait_closed()
                del reader
            finally:
                await first.close()

    async def test_bind_and_close_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lease.sock"
            async with LeaseServer(path, "ws://127.0.0.1:48731") as server:
                self.assertTrue(stat.S_ISSOCK(path.stat().st_mode))
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                reader, writer = await register(path, directory)
                self.assertEqual(
                    await read_message(reader),
                    {"type": "launch", "remote": "ws://127.0.0.1:48731"},
                )
                self.assertFalse(
                    server.bind_thread(
                        {
                            "id": "wrong-source",
                            "cwd": directory,
                            "ephemeral": True,
                            "threadSource": "system",
                        }
                    )
                )
                self.assertFalse(
                    server.bind_thread(
                        {
                            "id": "wrong-cwd",
                            "cwd": "/tmp/other",
                            "ephemeral": False,
                            "threadSource": "user",
                        }
                    )
                )
                self.assertTrue(
                    server.bind_thread(
                        {
                            "id": "thread-1",
                            "cwd": directory,
                            "ephemeral": False,
                            "threadSource": "user",
                        }
                    )
                )
                self.assertEqual(
                    await read_message(reader),
                    {"type": "bound", "threadId": "thread-1"},
                )
                bound = await server.next_event(timeout=1)
                self.assertIsInstance(bound, LeaseBound)
                self.assertEqual(bound.lease.thread_id, "thread-1")
                self.assertEqual(bound.lease.registration.tmux.pane, "p")
                self.assertEqual(len(server.active_leases), 1)

                writer.close()
                await writer.wait_closed()
                closed = await server.next_event(timeout=1)
                self.assertIsInstance(closed, LeaseClosed)
                self.assertEqual(closed.lease.thread_id, "thread-1")
                self.assertEqual(server.active_leases, ())

            self.assertFalse(path.exists())

    async def test_same_cwd_registrations_are_paired_serially(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lease.sock"
            async with LeaseServer(path, "ws://127.0.0.1:48731") as server:
                first_reader, first_writer = await register(path, directory)
                second_reader, second_writer = await register(path, directory)

                self.assertEqual((await read_message(first_reader))["type"], "launch")
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(second_reader.readline(), 0.05)

                self.assertTrue(
                    server.bind_thread(
                        {
                            "id": "thread-1",
                            "cwd": directory,
                            "ephemeral": False,
                            "threadSource": "user",
                        }
                    )
                )
                self.assertEqual((await read_message(first_reader))["threadId"], "thread-1")
                await server.next_event(timeout=1)

                self.assertEqual((await read_message(second_reader))["type"], "launch")
                self.assertTrue(
                    server.bind_thread(
                        {
                            "id": "thread-2",
                            "cwd": directory,
                            "ephemeral": False,
                            "threadSource": "user",
                        }
                    )
                )
                self.assertEqual((await read_message(second_reader))["threadId"], "thread-2")
                await server.next_event(timeout=1)

                first_writer.close()
                second_writer.close()
                await asyncio.gather(
                    first_writer.wait_closed(),
                    second_writer.wait_closed(),
                )

    async def test_binding_timeout_does_not_create_active_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lease.sock"
            async with LeaseServer(
                path,
                "ws://127.0.0.1:48731",
                binding_timeout=0.02,
            ) as server:
                reader, writer = await register(path, directory)
                self.assertEqual((await read_message(reader))["type"], "launch")
                self.assertEqual(
                    await read_message(reader),
                    {"type": "error", "code": "binding_timeout"},
                )
                self.assertEqual(server.active_leases, ())
                writer.close()
                await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
