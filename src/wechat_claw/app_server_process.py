"""Lifecycle management for the local Codex app-server process."""

from __future__ import annotations

import asyncio
import os
import signal
from collections import deque
from collections.abc import Sequence
from urllib.parse import urlsplit


SLOW_CONNECTION_DIAGNOSTIC = (
    "disconnecting slow connection after outbound queue filled"
)


class AppServerProcessError(RuntimeError):
    """Raised when the managed Codex app-server cannot become ready."""


class AppServerProcess:
    def __init__(
        self,
        endpoint: str,
        *,
        codex_binary: str = "/opt/homebrew/bin/codex",
        command: Sequence[str] | None = None,
        startup_timeout: float = 10.0,
        stop_timeout: float = 5.0,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.port is None:
            raise ValueError("endpoint must be ws://127.0.0.1:<port>")
        self.endpoint = endpoint
        self.host = parsed.hostname
        self.port = parsed.port
        self.codex_binary = codex_binary
        self.command = tuple(command) if command is not None else None
        self.startup_timeout = startup_timeout
        self.stop_timeout = stop_timeout
        self.process: asyncio.subprocess.Process | None = None
        self._drain_tasks: list[asyncio.Task[None]] = []
        self._diagnostics: deque[str] = deque(maxlen=20)

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def diagnostics(self) -> tuple[str, ...]:
        return tuple(self._diagnostics)

    async def start(self) -> None:
        if self.running:
            raise AppServerProcessError("Codex app-server is already running")
        argv = self.command or (
            self.codex_binary,
            "app-server",
            "--listen",
            self.endpoint,
        )
        self._diagnostics.clear()
        self.process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self._drain_tasks = [
            asyncio.create_task(self._drain(self.process.stdout)),
            asyncio.create_task(self._drain(self.process.stderr)),
        ]

        try:
            await asyncio.wait_for(self._wait_until_ready(), self.startup_timeout)
        except BaseException as exc:
            await self.stop()
            details = "; ".join(self._diagnostics)
            suffix = f": {details}" if details else ""
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AppServerProcessError(
                f"Codex app-server failed to become ready{suffix}"
            ) from exc

    async def stop(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), self.stop_timeout)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        elif process is not None:
            await process.wait()

        for task in self._drain_tasks:
            if not task.done():
                task.cancel()
        if self._drain_tasks:
            await asyncio.gather(*self._drain_tasks, return_exceptions=True)
        self._drain_tasks.clear()

    async def __aenter__(self) -> AppServerProcess:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def _wait_until_ready(self) -> None:
        assert self.process is not None
        while True:
            if self.process.returncode is not None:
                raise AppServerProcessError(
                    f"Codex app-server exited with code {self.process.returncode}"
                )
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
            except OSError:
                await asyncio.sleep(0.05)
                continue
            writer.close()
            await writer.wait_closed()
            del reader
            return

    async def _drain(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._record_diagnostic(text)

    def _record_diagnostic(self, text: str) -> None:
        if SLOW_CONNECTION_DIAGNOSTIC in text:
            redacted = (
                "app-server transport: slow remote TUI disconnected"
            )
            self._diagnostics.append(redacted)
            print(
                f"[wechat-claw] {redacted}",
                flush=True,
            )
            return
        self._diagnostics.append("app-server stderr event")
