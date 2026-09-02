from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

from wechat_claw.codex_protocol import (
    CodexAppServerClient,
    CodexConnectionClosed,
    CodexRequestTimeout,
    CodexResponseError,
    CodexTurn,
    CodexTurnTimeout,
    extract_final_response,
)


class CodexProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_close_cancels_reader_when_websocket_close_hangs(self) -> None:
        class HangingWebSocket:
            transport = None

            async def close(self) -> None:
                await asyncio.Event().wait()

        client = CodexAppServerClient("ws://127.0.0.1:1", close_timeout=0.01)
        client._websocket = HangingWebSocket()
        client._reader_task = asyncio.create_task(asyncio.Event().wait())

        await asyncio.wait_for(client.close(), 0.2)

        self.assertIsNone(client._websocket)
        self.assertTrue(client._terminal_error)

    async def test_handshake_request_notification_and_server_request(self) -> None:
        received: list[dict[str, Any]] = []
        server_done = asyncio.Event()

        async def handler(websocket: ServerConnection) -> None:
            initialize = json.loads(await websocket.recv())
            received.append(initialize)
            await websocket.send(json.dumps({"id": initialize["id"], "result": {}}))
            received.append(json.loads(await websocket.recv()))

            echo = json.loads(await websocket.recv())
            received.append(echo)
            await websocket.send(
                json.dumps({"method": "thread/started", "params": {"thread": {"id": "t1"}}})
            )
            await websocket.send(
                json.dumps({"id": "approval-1", "method": "item/commandExecution/requestApproval", "params": {"threadId": "t1"}})
            )
            await websocket.send(json.dumps({"id": echo["id"], "result": {"value": "ok"}}))
            received.append(json.loads(await websocket.recv()))
            server_done.set()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with CodexAppServerClient(f"ws://127.0.0.1:{port}") as client:
                result = await client.request("test/echo", {"value": "ok"})
                notification = await client.next_notification(timeout=1)
                approval = await client.next_server_request(timeout=1)
                await client.respond(approval.id, result={"decision": "accept"})
                await asyncio.wait_for(server_done.wait(), 1)

        self.assertEqual(result, {"value": "ok"})
        self.assertEqual(notification.method, "thread/started")
        self.assertEqual(approval.id, "approval-1")
        self.assertEqual(received[0]["method"], "initialize")
        self.assertEqual(
            received[0]["params"]["capabilities"],
            {"experimentalApi": True},
        )
        self.assertEqual(received[1], {"method": "initialized"})
        self.assertEqual(received[2]["method"], "test/echo")
        self.assertEqual(
            received[3],
            {"id": "approval-1", "result": {"decision": "accept"}},
        )

    async def test_response_error_preserves_code_and_data(self) -> None:
        async def handler(websocket: ServerConnection) -> None:
            initialize = json.loads(await websocket.recv())
            await websocket.send(json.dumps({"id": initialize["id"], "result": {}}))
            await websocket.recv()
            request = json.loads(await websocket.recv())
            await websocket.send(
                json.dumps(
                    {
                        "id": request["id"],
                        "error": {"code": -32600, "message": "invalid", "data": {"field": "x"}},
                    }
                )
            )

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with CodexAppServerClient(f"ws://127.0.0.1:{port}") as client:
                with self.assertRaises(CodexResponseError) as raised:
                    await client.request("test/error")

        self.assertEqual(raised.exception.code, -32600)
        self.assertEqual(raised.exception.data, {"field": "x"})

    async def test_concurrent_responses_are_matched_when_out_of_order(self) -> None:
        async def handler(websocket: ServerConnection) -> None:
            initialize = json.loads(await websocket.recv())
            await websocket.send(json.dumps({"id": initialize["id"], "result": {}}))
            await websocket.recv()
            first = json.loads(await websocket.recv())
            second = json.loads(await websocket.recv())
            await websocket.send(
                json.dumps({"id": second["id"], "result": second["params"]["value"]})
            )
            await websocket.send(
                json.dumps({"id": first["id"], "result": first["params"]["value"]})
            )

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with CodexAppServerClient(f"ws://127.0.0.1:{port}") as client:
                first, second = await asyncio.gather(
                    client.request("test/value", {"value": "first"}),
                    client.request("test/value", {"value": "second"}),
                )

        self.assertEqual(first, "first")
        self.assertEqual(second, "second")

    async def test_request_timeout(self) -> None:
        release = asyncio.Event()

        async def handler(websocket: ServerConnection) -> None:
            initialize = json.loads(await websocket.recv())
            await websocket.send(json.dumps({"id": initialize["id"], "result": {}}))
            await websocket.recv()
            await websocket.recv()
            await release.wait()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with CodexAppServerClient(
                f"ws://127.0.0.1:{port}", request_timeout=0.05
            ) as client:
                with self.assertRaises(CodexRequestTimeout):
                    await client.request("test/slow")
            release.set()

    async def test_transient_item_list_timeout_does_not_end_turn_wait(self) -> None:
        class TransientTimeoutClient(CodexAppServerClient):
            def __init__(self) -> None:
                super().__init__("ws://127.0.0.1:1", request_timeout=0.01)
                self.read_calls = 0

            async def read_final_response(
                self,
                turn: CodexTurn,
                *,
                timeout: float | None = None,
            ) -> str | None:
                self.read_calls += 1
                if self.read_calls == 1:
                    raise CodexRequestTimeout(
                        "app-server request timed out: thread/items/list"
                    )
                return "REMOTE_OK"

        client = TransientTimeoutClient()
        final = await client.wait_for_final_response(
            CodexTurn("thread-1", "turn-1"),
            timeout=0.1,
            poll_interval=0.001,
        )

        self.assertEqual(final, "REMOTE_OK")
        self.assertEqual(client.read_calls, 2)

    async def test_only_overall_deadline_raises_turn_timeout(self) -> None:
        class AlwaysTimingOutClient(CodexAppServerClient):
            async def read_final_response(
                self,
                turn: CodexTurn,
                *,
                timeout: float | None = None,
            ) -> str | None:
                raise CodexRequestTimeout(
                    "app-server request timed out: thread/items/list"
                )

        client = AlwaysTimingOutClient(
            "ws://127.0.0.1:1",
            request_timeout=0.001,
        )
        with self.assertRaises(CodexTurnTimeout):
            await client.wait_for_final_response(
                CodexTurn("thread-1", "turn-1"),
                timeout=0.01,
                poll_interval=0.001,
            )

    async def test_disconnect_fails_pending_request(self) -> None:
        async def handler(websocket: ServerConnection) -> None:
            initialize = json.loads(await websocket.recv())
            await websocket.send(json.dumps({"id": initialize["id"], "result": {}}))
            await websocket.recv()
            await websocket.recv()
            await websocket.close()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with CodexAppServerClient(f"ws://127.0.0.1:{port}") as client:
                with self.assertRaises(CodexConnectionClosed):
                    await client.request("test/disconnect")

    async def test_turn_helpers_use_exact_thread_and_turn(self) -> None:
        methods: list[dict[str, Any]] = []
        item_list_count = 0

        async def handler(websocket: ServerConnection) -> None:
            nonlocal item_list_count
            initialize = json.loads(await websocket.recv())
            await websocket.send(json.dumps({"id": initialize["id"], "result": {}}))
            await websocket.recv()
            async for raw in websocket:
                request = json.loads(raw)
                methods.append(request)
                if request["method"] == "turn/start":
                    result = {"turn": {"id": "turn-1", "status": "inProgress"}}
                elif request["method"] == "turn/interrupt":
                    result = {}
                elif request["method"] == "thread/items/list":
                    item_list_count += 1
                    if item_list_count == 1:
                        await websocket.send(
                            json.dumps(
                                {
                                    "id": request["id"],
                                    "error": {
                                        "code": -32601,
                                        "message": "thread/items/list is not supported yet",
                                    },
                                }
                            )
                        )
                        continue
                    result = {
                        "data": [
                            {
                                "turnId": "turn-other",
                                "item": {"type": "agentMessage", "phase": "final_answer", "text": "wrong"},
                            },
                            {
                                "turnId": "turn-1",
                                "item": {"type": "agentMessage", "phase": "commentary", "text": "working"},
                            },
                            *(
                                [
                                    {
                                        "turnId": "turn-1",
                                        "item": {
                                            "type": "agentMessage",
                                            "phase": "final_answer",
                                            "text": "REMOTE_OK",
                                        },
                                    }
                                ]
                                if item_list_count > 2
                                else []
                            ),
                        ]
                    }
                else:
                    raise AssertionError(request["method"])
                await websocket.send(json.dumps({"id": request["id"], "result": result}))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with CodexAppServerClient(f"ws://127.0.0.1:{port}") as client:
                turn = await client.start_turn("thread-1", "微信原文")
                await client.interrupt_turn(turn)
                final = await client.wait_for_final_response(
                    turn,
                    timeout=1,
                    poll_interval=0.001,
                )

        self.assertEqual(turn, CodexTurn("thread-1", "turn-1"))
        self.assertEqual(final, "REMOTE_OK")
        self.assertEqual(
            methods[0]["params"],
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "微信原文"}],
            },
        )
        self.assertEqual(
            methods[1],
            {
                "id": methods[1]["id"],
                "method": "turn/interrupt",
                "params": {"threadId": "thread-1", "turnId": "turn-1"},
            },
        )
        self.assertEqual(
            methods[-1]["params"],
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "sortDirection": "asc",
            },
        )

    def test_extract_final_response_requires_exact_turn(self) -> None:
        result = {
            "data": [
                {
                    "turnId": "turn-2",
                    "item": {"type": "agentMessage", "phase": "final_answer", "text": "other"},
                }
            ]
        }

        self.assertIsNone(extract_final_response(result, "turn-1"))


if __name__ == "__main__":
    unittest.main()
