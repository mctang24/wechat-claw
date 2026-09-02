"""Async client for the Codex app-server JSON RPC protocol."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed as WebSocketConnectionClosed


JsonObject = dict[str, Any]
RequestId = int | str


class CodexProtocolError(RuntimeError):
    """Base error for invalid or unavailable app-server protocol behavior."""


class CodexConnectionClosed(CodexProtocolError):
    """Raised when the app-server connection closes."""


class CodexRequestTimeout(CodexProtocolError):
    """Raised when an app-server request doesn't finish before its deadline."""


class CodexTurnTimeout(CodexRequestTimeout):
    """Raised when a complete turn wait reaches its overall deadline."""


class CodexResponseError(CodexProtocolError):
    """Error response returned by app-server."""

    def __init__(self, code: int | None, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass(frozen=True, slots=True)
class CodexNotification:
    method: str
    params: JsonObject


@dataclass(frozen=True, slots=True)
class CodexServerRequest:
    id: RequestId
    method: str
    params: JsonObject


@dataclass(frozen=True, slots=True)
class CodexTurn:
    thread_id: str
    turn_id: str


QueueItem = CodexNotification | CodexServerRequest | BaseException


class CodexAppServerClient:
    """One initialized WebSocket client connection to Codex app-server."""

    def __init__(
        self,
        uri: str,
        *,
        client_name: str = "wechat-claw",
        client_version: str = "0.1.0",
        request_timeout: float = 15.0,
        open_timeout: float = 10.0,
        close_timeout: float = 3.0,
    ) -> None:
        self.uri = uri
        self.client_name = client_name
        self.client_version = client_version
        self.request_timeout = request_timeout
        self.open_timeout = open_timeout
        self.close_timeout = close_timeout
        self.initialize_result: JsonObject | None = None

        self._websocket: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._next_request_id = 1
        self._pending: dict[RequestId, asyncio.Future[Any]] = {}
        self._notifications: asyncio.Queue[QueueItem] = asyncio.Queue()
        self._server_requests: asyncio.Queue[QueueItem] = asyncio.Queue()
        self._terminal_error: BaseException | None = None

    @property
    def connected(self) -> bool:
        return self._websocket is not None and self._terminal_error is None

    async def connect(self) -> JsonObject:
        if self._websocket is not None:
            raise CodexProtocolError("app-server client is already connected")

        try:
            self._websocket = await connect(
                self.uri,
                open_timeout=self.open_timeout,
                close_timeout=self.close_timeout,
                proxy=None,
            )
            self._reader_task = asyncio.create_task(
                self._read_loop(),
                name="wechat-claw-codex-reader",
            )
            result = await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": self.client_name,
                        "version": self.client_version,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            if not isinstance(result, dict):
                raise CodexProtocolError("initialize response must be an object")
            self.initialize_result = result
            await self.notify("initialized")
            return result
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        websocket = self._websocket
        reader_task = self._reader_task
        self._websocket = None
        self._reader_task = None

        if websocket is not None:
            close_task = asyncio.create_task(websocket.close())
            done, pending = await asyncio.wait(
                (close_task,),
                timeout=self.close_timeout,
            )
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            if pending:
                transport = getattr(websocket, "transport", None)
                if transport is not None:
                    transport.abort()
                for task in pending:
                    task.cancel()
        if reader_task is not None and reader_task is not asyncio.current_task():
            if not reader_task.done():
                reader_task.cancel()
            done, pending = await asyncio.wait((reader_task,), timeout=1.0)
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            for task in pending:
                task.cancel()

        self._terminate(CodexConnectionClosed("app-server client closed"))

    async def __aenter__(self) -> CodexAppServerClient:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        websocket = self._require_connection()
        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        try:
            await self._send(
                websocket,
                {"id": request_id, "method": method, "params": dict(params or {})},
            )
            deadline = self.request_timeout if timeout is None else timeout
            return await asyncio.wait_for(asyncio.shield(future), deadline)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            future.cancel()
            raise CodexRequestTimeout(
                f"app-server request timed out: {method}"
            ) from exc
        except BaseException:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise

    async def notify(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        websocket = self._require_connection()
        payload: JsonObject = {"method": method}
        if params is not None:
            payload["params"] = dict(params)
        await self._send(websocket, payload)

    async def respond(
        self,
        request_id: RequestId,
        *,
        result: Any = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        if error is not None and result is not None:
            raise ValueError("result and error cannot both be set")
        websocket = self._require_connection()
        payload: JsonObject = {"id": request_id}
        if error is None:
            payload["result"] = result
        else:
            payload["error"] = dict(error)
        await self._send(websocket, payload)

    async def next_notification(
        self,
        *,
        timeout: float | None = None,
    ) -> CodexNotification:
        item = await self._next_queue_item(self._notifications, timeout)
        if not isinstance(item, CodexNotification):
            raise CodexProtocolError("invalid notification queue item")
        return item

    async def next_server_request(
        self,
        *,
        timeout: float | None = None,
    ) -> CodexServerRequest:
        item = await self._next_queue_item(self._server_requests, timeout)
        if not isinstance(item, CodexServerRequest):
            raise CodexProtocolError("invalid server request queue item")
        return item

    async def start_turn(self, thread_id: str, text: str) -> CodexTurn:
        if not thread_id:
            raise ValueError("thread_id is required")
        if not text:
            raise ValueError("turn text is required")
        result = await self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
            },
        )
        turn = _required_object(result, "turn/start result").get("turn")
        turn_id = _required_object(turn, "turn/start turn").get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexProtocolError("turn/start response is missing turn.id")
        return CodexTurn(thread_id=thread_id, turn_id=turn_id)

    async def interrupt_turn(self, turn: CodexTurn) -> None:
        await self.request(
            "turn/interrupt",
            {"threadId": turn.thread_id, "turnId": turn.turn_id},
        )

    async def wait_for_final_response(
        self,
        turn: CodexTurn,
        *,
        timeout: float,
        poll_interval: float = 0.5,
    ) -> str:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CodexTurnTimeout(
                    f"turn final response timed out: {turn.turn_id}"
                )
            try:
                final = await self.read_final_response(
                    turn,
                    timeout=min(self.request_timeout, remaining),
                )
            except CodexRequestTimeout:
                final = None
            except CodexResponseError as exc:
                if str(exc) != "thread/items/list is not supported yet":
                    raise
                final = None
            if final is not None:
                return final
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CodexTurnTimeout(
                    f"turn final response timed out: {turn.turn_id}"
                )
            await asyncio.sleep(min(poll_interval, remaining))

    async def list_turn_items(
        self,
        turn: CodexTurn,
        *,
        timeout: float | None = None,
    ) -> JsonObject:
        result = await self.request(
            "thread/items/list",
            {
                "threadId": turn.thread_id,
                "turnId": turn.turn_id,
                "sortDirection": "asc",
            },
            timeout=timeout,
        )
        return _required_object(result, "thread/items/list result")

    async def read_final_response(
        self,
        turn: CodexTurn,
        *,
        timeout: float | None = None,
    ) -> str | None:
        result = await self.list_turn_items(turn, timeout=timeout)
        return extract_final_response(result, turn.turn_id)

    def _require_connection(self) -> ClientConnection:
        if self._terminal_error is not None:
            raise self._terminal_error
        if self._websocket is None:
            raise CodexConnectionClosed("app-server client is not connected")
        return self._websocket

    async def _send(self, websocket: ClientConnection, payload: JsonObject) -> None:
        async with self._send_lock:
            try:
                await websocket.send(json.dumps(payload, ensure_ascii=False))
            except WebSocketConnectionClosed as exc:
                error = CodexConnectionClosed("app-server connection closed")
                self._terminate(error)
                raise error from exc

    async def _read_loop(self) -> None:
        assert self._websocket is not None
        try:
            async for raw_message in self._websocket:
                self._dispatch(raw_message)
        except asyncio.CancelledError:
            raise
        except WebSocketConnectionClosed as exc:
            self._terminate(CodexConnectionClosed(str(exc)))
        except BaseException as exc:
            if isinstance(exc, CodexProtocolError):
                self._terminate(exc)
            else:
                self._terminate(CodexProtocolError(str(exc)))
        else:
            self._terminate(CodexConnectionClosed("app-server connection closed"))

    def _dispatch(self, raw_message: str | bytes) -> None:
        try:
            message = json.loads(raw_message)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CodexProtocolError("app-server sent invalid JSON") from exc
        if not isinstance(message, dict):
            raise CodexProtocolError("app-server message must be an object")

        method = message.get("method")
        request_id = message.get("id")
        if isinstance(method, str):
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise CodexProtocolError("app-server params must be an object")
            if request_id is None:
                self._notifications.put_nowait(CodexNotification(method, params))
            elif isinstance(request_id, (int, str)):
                self._server_requests.put_nowait(
                    CodexServerRequest(request_id, method, params)
                )
            else:
                raise CodexProtocolError("app-server request id has invalid type")
            return

        if isinstance(request_id, (int, str)):
            future = self._pending.pop(request_id, None)
            if future is None or future.done():
                return
            error = message.get("error")
            if error is not None:
                if not isinstance(error, dict):
                    future.set_exception(
                        CodexProtocolError("app-server error must be an object")
                    )
                    return
                future.set_exception(
                    CodexResponseError(
                        error.get("code") if isinstance(error.get("code"), int) else None,
                        str(error.get("message", "app-server request failed")),
                        error.get("data"),
                    )
                )
            else:
                future.set_result(message.get("result"))
            return

        raise CodexProtocolError("unrecognized app-server message")

    def _terminate(self, error: BaseException) -> None:
        if self._terminal_error is not None:
            return
        self._terminal_error = error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        self._notifications.put_nowait(error)
        self._server_requests.put_nowait(error)

    @staticmethod
    async def _next_queue_item(
        queue: asyncio.Queue[QueueItem],
        timeout: float | None,
    ) -> QueueItem:
        try:
            if timeout is None:
                item = await queue.get()
            else:
                item = await asyncio.wait_for(queue.get(), timeout)
        except TimeoutError as exc:
            raise CodexRequestTimeout("app-server event wait timed out") from exc
        if isinstance(item, BaseException):
            raise item
        return item


def extract_final_response(result: Mapping[str, Any], turn_id: str) -> str | None:
    """Return the final assistant text for one exact turn, if present."""

    data = result.get("data")
    if not isinstance(data, list):
        raise CodexProtocolError("thread/items/list response is missing data")

    final_text: str | None = None
    for entry in data:
        if not isinstance(entry, dict) or entry.get("turnId") != turn_id:
            continue
        item = entry.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "agentMessage" or item.get("phase") != "final_answer":
            continue
        text = item.get("text")
        if isinstance(text, str):
            final_text = text
    return final_text


def _required_object(value: Any, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise CodexProtocolError(f"{label} must be an object")
    return value
