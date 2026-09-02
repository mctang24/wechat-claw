from __future__ import annotations

import socket
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO

from wechat_claw.app_server_process import AppServerProcess, AppServerProcessError


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class AppServerProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_slow_connection_diagnostic_is_redacted(self) -> None:
        process = AppServerProcess("ws://127.0.0.1:48731")
        output = StringIO()

        with redirect_stdout(output):
            process._record_diagnostic(
                "disconnecting slow connection after outbound queue filled: "
                "ConnectionId(7) PRIVATE_PAYLOAD"
            )

        self.assertIn("slow remote TUI disconnected", output.getvalue())
        self.assertNotIn("PRIVATE_PAYLOAD", output.getvalue())
        self.assertNotIn("PRIVATE_PAYLOAD", " ".join(process.diagnostics))

    async def test_unknown_stderr_diagnostic_is_never_retained_verbatim(self) -> None:
        process = AppServerProcess("ws://127.0.0.1:48731")

        process._record_diagnostic("token=PRIVATE_APP_SERVER_STDERR")

        rendered = " ".join(process.diagnostics)
        self.assertIn("app-server stderr event", rendered)
        self.assertNotIn("PRIVATE_APP_SERVER_STDERR", rendered)

    async def test_process_becomes_ready_and_stops(self) -> None:
        port = available_port()
        script = (
            "import asyncio\n"
            "async def main():\n"
            f"    server = await asyncio.start_server(lambda r, w: w.close(), '127.0.0.1', {port})\n"
            "    await server.serve_forever()\n"
            "asyncio.run(main())\n"
        )
        process = AppServerProcess(
            f"ws://127.0.0.1:{port}",
            command=(sys.executable, "-c", script),
            startup_timeout=2,
            stop_timeout=2,
        )

        await process.start()
        self.assertTrue(process.running)
        await process.stop()
        self.assertFalse(process.running)

    async def test_early_exit_is_reported(self) -> None:
        port = available_port()
        process = AppServerProcess(
            f"ws://127.0.0.1:{port}",
            command=(sys.executable, "-c", "raise SystemExit(7)"),
            startup_timeout=1,
            stop_timeout=1,
        )

        with self.assertRaises(AppServerProcessError):
            await process.start()
        self.assertFalse(process.running)


if __name__ == "__main__":
    unittest.main()
