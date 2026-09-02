"""Unix connection leases for active Codex TUI processes."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class LeaseProtocolError(RuntimeError):
    """Raised when a wrapper sends an invalid lease registration."""


@dataclass(frozen=True, slots=True)
class TmuxLocation:
    session: str | None
    window: str | None
    pane: str | None


@dataclass(frozen=True, slots=True)
class LeaseRegistration:
    cwd: str
    tmux: TmuxLocation


@dataclass(frozen=True, slots=True)
class ActiveLease:
    lease_id: int
    thread_id: str
    registration: LeaseRegistration


@dataclass(frozen=True, slots=True)
class LeaseBound:
    lease: ActiveLease


@dataclass(frozen=True, slots=True)
class LeaseClosed:
    lease: ActiveLease


LeaseEvent = LeaseBound | LeaseClosed


@dataclass(slots=True)
class _PendingLease:
    lease_id: int
    registration: LeaseRegistration
    bound_thread: asyncio.Future[str]


class LeaseServer:
    def __init__(
        self,
        socket_path: Path,
        remote_endpoint: str,
        *,
        registration_timeout: float = 5.0,
        binding_timeout: float = 15.0,
        close_timeout: float = 1.0,
    ) -> None:
        self.socket_path = socket_path
        self.remote_endpoint = remote_endpoint
        self.registration_timeout = registration_timeout
        self.binding_timeout = binding_timeout
        self.close_timeout = close_timeout
        self._server: asyncio.Server | None = None
        self._start_lock = asyncio.Lock()
        self._pending: _PendingLease | None = None
        self._next_lease_id = 1
        self._active: dict[int, ActiveLease] = {}
        self._events: asyncio.Queue[LeaseEvent] = asyncio.Queue()
        self._writers: set[asyncio.StreamWriter] = set()
        self._client_tasks: set[asyncio.Task[None]] = set()

    @property
    def active_leases(self) -> tuple[ActiveLease, ...]:
        return tuple(self._active.values())

    async def start(self) -> None:
        if self._server is not None:
            raise LeaseProtocolError("lease server is already running")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            mode = self.socket_path.stat().st_mode
            if not stat.S_ISSOCK(mode):
                raise LeaseProtocolError(
                    f"lease path exists and is not a socket: {self.socket_path}"
                )
            try:
                reader, writer = await asyncio.open_unix_connection(self.socket_path)
            except (ConnectionRefusedError, FileNotFoundError):
                self.socket_path.unlink()
            except OSError as exc:
                raise LeaseProtocolError(
                    f"cannot verify existing lease socket: {self.socket_path}"
                ) from exc
            else:
                del reader
                writer.close()
                await writer.wait_closed()
                raise LeaseProtocolError("wechat-claw daemon is already running")
        self._server = await asyncio.start_unix_server(
            self._accept_client,
            path=self.socket_path,
        )
        self.socket_path.chmod(0o600)

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            waiter = asyncio.create_task(server.wait_closed())
            done, pending = await asyncio.wait(
                (waiter,),
                timeout=self.close_timeout,
            )
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            for task in pending:
                task.cancel()

        if self.socket_path.exists() and stat.S_ISSOCK(self.socket_path.stat().st_mode):
            self.socket_path.unlink()

        for writer in tuple(self._writers):
            writer.close()
        for task in tuple(self._client_tasks):
            if not task.done():
                task.cancel()
        if self._client_tasks:
            done, pending = await asyncio.wait(
                self._client_tasks,
                timeout=self.close_timeout,
            )
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            for writer in tuple(self._writers):
                transport = getattr(writer, "transport", None)
                if transport is not None:
                    transport.abort()
            for task in pending:
                task.cancel()
        self._writers.clear()
        self._client_tasks.clear()
        self._pending = None
        self._active.clear()

    async def __aenter__(self) -> LeaseServer:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def bind_thread(self, thread: dict[str, Any]) -> bool:
        pending = self._pending
        if pending is None or pending.bound_thread.done():
            return False
        if thread.get("ephemeral") is not False or thread.get("threadSource") != "user":
            return False
        thread_id = thread.get("id")
        cwd = thread.get("cwd")
        if not isinstance(thread_id, str) or not thread_id:
            return False
        if not isinstance(cwd, str) or _normalize_cwd(cwd) != pending.registration.cwd:
            return False
        pending.bound_thread.set_result(thread_id)
        return True

    async def next_event(self, *, timeout: float | None = None) -> LeaseEvent:
        try:
            if timeout is None:
                return await self._events.get()
            return await asyncio.wait_for(self._events.get(), timeout)
        except TimeoutError as exc:
            raise LeaseProtocolError("lease event wait timed out") from exc

    def _accept_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.create_task(
            self._handle_client(reader, writer),
            name="wechat-claw-lease-client",
        )
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._writers.add(writer)
        active: ActiveLease | None = None
        try:
            registration = await self._read_registration(reader)
            async with self._start_lock:
                pending = _PendingLease(
                    lease_id=self._next_lease_id,
                    registration=registration,
                    bound_thread=asyncio.get_running_loop().create_future(),
                )
                self._next_lease_id += 1
                self._pending = pending
                await _write_message(
                    writer,
                    {"type": "launch", "remote": self.remote_endpoint},
                )
                try:
                    thread_id = await asyncio.wait_for(
                        asyncio.shield(pending.bound_thread),
                        self.binding_timeout,
                    )
                except TimeoutError:
                    await _write_message(
                        writer,
                        {"type": "error", "code": "binding_timeout"},
                    )
                    return
                finally:
                    if self._pending is pending:
                        self._pending = None

                active = ActiveLease(
                    lease_id=pending.lease_id,
                    thread_id=thread_id,
                    registration=registration,
                )
                self._active[active.lease_id] = active
                await _write_message(
                    writer,
                    {"type": "bound", "threadId": active.thread_id},
                )
                self._events.put_nowait(LeaseBound(active))

            await reader.read()
        except (LeaseProtocolError, ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            if active is not None and self._active.pop(active.lease_id, None) is not None:
                self._events.put_nowait(LeaseClosed(active))
            self._writers.discard(writer)
            writer.close()
            await _wait_for_writer_closed(writer)

    async def _read_registration(
        self,
        reader: asyncio.StreamReader,
    ) -> LeaseRegistration:
        try:
            line = await asyncio.wait_for(reader.readline(), self.registration_timeout)
        except TimeoutError as exc:
            raise LeaseProtocolError("lease registration timed out") from exc
        if not line or len(line) > 65_536:
            raise LeaseProtocolError("invalid lease registration length")
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LeaseProtocolError("lease registration is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("type") != "register":
            raise LeaseProtocolError("lease registration type must be register")
        cwd = payload.get("cwd")
        tmux = payload.get("tmux", {})
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            raise LeaseProtocolError("lease cwd must be an absolute path")
        if not isinstance(tmux, dict):
            raise LeaseProtocolError("lease tmux field must be an object")
        return LeaseRegistration(
            cwd=_normalize_cwd(cwd),
            tmux=TmuxLocation(
                session=_optional_string(tmux.get("session")),
                window=_optional_string(tmux.get("window")),
                pane=_optional_string(tmux.get("pane")),
            ),
        )


async def _write_message(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
    await writer.drain()


async def _wait_for_writer_closed(
    writer: asyncio.StreamWriter,
    *,
    timeout: float = 1.0,
) -> None:
    waiter = asyncio.create_task(writer.wait_closed())
    done, pending = await asyncio.wait((waiter,), timeout=timeout)
    if done:
        await asyncio.gather(*done, return_exceptions=True)
        return
    transport = getattr(writer, "transport", None)
    if transport is not None:
        transport.abort()
    for task in pending:
        task.cancel()


def _normalize_cwd(value: str) -> str:
    return os.path.realpath(value)


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
