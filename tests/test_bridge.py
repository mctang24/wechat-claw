from __future__ import annotations

import asyncio
import gc
import unittest

from wechat_claw.approval_registry import COMMAND_APPROVAL
from wechat_claw.bridge import CodexBridge
from wechat_claw.codex_protocol import (
    CodexConnectionClosed,
    CodexNotification,
    CodexRequestTimeout,
    CodexServerRequest,
    CodexTurn,
    CodexTurnTimeout,
)
from wechat_claw.lease_server import (
    ActiveLease,
    LeaseBound,
    LeaseClosed,
    LeaseRegistration,
    TmuxLocation,
)
from wechat_claw.session_registry import SessionStatus


class FakeClient:
    def __init__(self) -> None:
        self.next_turn = CodexTurn("thread-1", "turn-1")
        self.started: list[tuple[str, str]] = []
        self.responded: list[tuple[int | str, object]] = []
        self.interrupted: list[CodexTurn] = []
        self.completed = asyncio.Event()
        self.final = "最终回复原文"
        self.start_error: Exception | None = None
        self.response_error: Exception | None = None
        self.wait_error: Exception | None = None
        self.block_response = False

    async def start_turn(self, thread_id: str, text: str) -> CodexTurn:
        if self.start_error:
            raise self.start_error
        self.started.append((thread_id, text))
        return self.next_turn

    async def wait_for_final_response(self, turn: CodexTurn, **kwargs) -> str:
        if self.wait_error is not None:
            raise self.wait_error
        try:
            await asyncio.wait_for(self.completed.wait(), kwargs["timeout"])
        except TimeoutError as exc:
            raise CodexTurnTimeout("fake turn timed out") from exc
        return self.final

    async def respond(self, request_id, *, result=None, error=None) -> None:
        if self.block_response:
            await asyncio.Event().wait()
        if self.response_error:
            raise self.response_error
        self.responded.append((request_id, result))

    async def interrupt_turn(self, turn: CodexTurn) -> None:
        self.interrupted.append(turn)


class FakeLeaseServer:
    def __init__(self) -> None:
        self.bound: list[dict] = []

    def bind_thread(self, thread: dict) -> bool:
        self.bound.append(thread)
        return True


def lease(lease_id: int = 1, thread_id: str = "thread-1") -> ActiveLease:
    return ActiveLease(
        lease_id,
        thread_id,
        LeaseRegistration(
            "/tmp/project-one",
            TmuxLocation("work", "editor", f"%{lease_id}"),
        ),
    )


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = FakeClient()
        self.lease_server = FakeLeaseServer()
        self.pushed: list[str] = []

        async def push(text: str) -> bool:
            self.pushed.append(text)
            return True

        self.bridge = CodexBridge(self.client, self.lease_server, push)
        await self.bridge.handle_lease_event(LeaseBound(lease()))

    async def asyncTearDown(self) -> None:
        await self.bridge.close()

    async def test_wechat_turn_is_acknowledged_and_final_is_pushed_once(self) -> None:
        reply = await self.bridge.handle_text("/send 1 微信原文")
        self.assertIn("已发送", reply)
        self.assertEqual(self.client.started, [("thread-1", "微信原文")])
        self.assertEqual(
            self.bridge.sessions.get(1).latest_message,
            "微信原文",
        )
        self.client.completed.set()
        await asyncio.sleep(0.01)
        self.assertEqual(self.pushed, ["最终回复原文"])

        await self.bridge.handle_notification(
            CodexNotification(
                "turn/completed",
                {"threadId": "thread-1", "turn": {"id": "turn-1"}},
            )
        )
        self.assertEqual(self.pushed, ["最终回复原文"])

    async def test_slow_wechat_turn_reports_progress_then_interrupts_exactly_once(self) -> None:
        await self.bridge.close()

        async def push(text: str) -> bool:
            self.pushed.append(text)
            return True

        self.bridge = CodexBridge(
            self.client,
            self.lease_server,
            push,
            turn_progress_after=0.01,
            turn_timeout=0.03,
        )
        await self.bridge.handle_lease_event(LeaseBound(lease()))
        loop_errors: list[dict] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        try:
            await self.bridge.handle_text("/send 1 长任务")
            await asyncio.sleep(0.06)
            gc.collect()
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)

        turn = CodexTurn("thread-1", "turn-1")
        self.assertEqual(self.client.interrupted, [turn])
        self.assertEqual(len(self.pushed), 2)
        self.assertIn("仍在处理中", self.pushed[0])
        self.assertIn("等待超过 10 分钟", self.pushed[1])
        self.assertEqual(self.bridge._wechat_turn_tasks, {})
        self.assertEqual(loop_errors, [])

        await self.bridge.handle_notification(
            CodexNotification(
                "turn/completed",
                {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "interrupted"},
                },
            )
        )
        self.assertEqual(len(self.pushed), 2)

    async def test_request_timeout_never_claims_turn_reached_ten_minutes(self) -> None:
        self.client.wait_error = CodexRequestTimeout(
            "app-server request timed out: thread/items/list"
        )

        await self.bridge.handle_text("/send 1 查询暂时超时")
        await asyncio.sleep(0.01)

        self.assertEqual(self.client.interrupted, [])
        self.assertEqual(len(self.pushed), 1)
        self.assertIn("回复获取失败", self.pushed[0])
        self.assertNotIn("10 分钟", self.pushed[0])

    async def test_unsupported_user_input_request_is_not_reported_as_approval(self) -> None:
        await self.bridge.handle_server_request(
            CodexServerRequest(
                "input-1",
                "item/tool/requestUserInput",
                {"threadId": "thread-1", "turnId": "turn-local"},
            )
        )

        self.assertEqual(len(self.pushed), 1)
        self.assertIn("交互选择", self.pushed[0])
        self.assertNotIn("审批请求", self.pushed[0])

    async def test_local_turn_updates_state_but_never_pushes_reply(self) -> None:
        await self.bridge.handle_notification(
            CodexNotification(
                "turn/started",
                {"threadId": "thread-1", "turn": {"id": "local-turn"}},
            )
        )
        await self.bridge.handle_notification(
            CodexNotification(
                "item/started",
                {
                    "threadId": "thread-1",
                    "turnId": "local-turn",
                    "item": {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "本地原文"}],
                    },
                },
            )
        )
        await self.bridge.handle_notification(
            CodexNotification(
                "turn/completed",
                {"threadId": "thread-1", "turn": {"id": "local-turn"}},
            )
        )
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.bridge.sessions.get(1).latest_message, "本地原文")
        self.assertEqual(self.bridge.sessions.get(1).status, SessionStatus.IDLE)

    async def test_control_commands_never_start_turn(self) -> None:
        sessions = await self.bridge.handle_text("/sessions")
        focus = await self.bridge.handle_text("/focus 1")
        unfocus = await self.bridge.handle_text("/unfocus")
        help_text = await self.bridge.handle_text("/help")
        self.assertIn("project-one", sessions)
        self.assertIn("focus", focus)
        self.assertIn("解除", unfocus)
        self.assertIn("/approve", help_text)
        self.assertEqual(self.client.started, [])

    async def test_genuine_approval_is_pushed_and_approved_exactly_once(self) -> None:
        await self.bridge.handle_server_request(
            CodexServerRequest(
                "request-1",
                COMMAND_APPROVAL,
                {
                    "threadId": "thread-1",
                    "turnId": "turn-local",
                    "itemId": "item-1",
                    "startedAtMs": 1,
                    "command": "/usr/bin/true",
                    "availableDecisions": ["accept", "decline"],
                },
            )
        )
        code = self.pushed[0].splitlines()[0].rsplit(" ", 1)[1]
        result = await self.bridge.handle_text(f"/approve {code}")
        duplicate = await self.bridge.handle_text(f"/approve {code}")

        self.assertIn("已批准", result)
        self.assertIn("不存在或已失效", duplicate)
        self.assertEqual(self.client.responded, [("request-1", {"decision": "accept"})])

    async def test_locally_resolved_approval_is_invalidated_immediately(self) -> None:
        await self.bridge.handle_server_request(
            CodexServerRequest(
                21,
                COMMAND_APPROVAL,
                {
                    "threadId": "thread-1",
                    "turnId": "turn-local",
                    "itemId": "item-21",
                    "startedAtMs": 1,
                    "command": "/usr/bin/true",
                },
            )
        )
        code = self.pushed[-1].splitlines()[0].rsplit(" ", 1)[1]

        await self.bridge.handle_notification(
            CodexNotification(
                "serverRequest/resolved",
                {"threadId": "thread-1", "requestId": 21},
            )
        )

        self.assertEqual(self.bridge.approvals.pending_for_thread("thread-1"), ())
        self.assertEqual(self.bridge.sessions.get(1).status, SessionStatus.RUNNING)
        result = await self.bridge.handle_text(f"/approve {code}")
        self.assertIn("不存在或已失效", result)
        self.assertEqual(self.client.responded, [])

    async def test_resolved_notification_cannot_turn_sent_approval_into_false_failure(self) -> None:
        await self.bridge.handle_server_request(
            CodexServerRequest(
                23,
                COMMAND_APPROVAL,
                {
                    "threadId": "thread-1",
                    "turnId": "turn-local",
                    "itemId": "item-23",
                    "startedAtMs": 1,
                    "command": "/usr/bin/true",
                },
            )
        )
        code = self.pushed[-1].splitlines()[0].rsplit(" ", 1)[1]
        resolved_tasks: list[asyncio.Task[None]] = []

        async def respond_then_resolve(request_id, *, result=None, error=None) -> None:
            self.client.responded.append((request_id, result))
            resolved_tasks.append(
                asyncio.create_task(
                    self.bridge.handle_notification(
                        CodexNotification(
                            "serverRequest/resolved",
                            {"threadId": "thread-1", "requestId": request_id},
                        )
                    )
                )
            )
            await asyncio.sleep(0)

        self.client.respond = respond_then_resolve
        result = await self.bridge.handle_text(f"/approve {code}")
        await asyncio.gather(*resolved_tasks)

        self.assertIn("已批准", result)
        self.assertEqual(self.client.responded, [(23, {"decision": "accept"})])
        self.assertEqual(self.bridge.approvals.pending_for_thread("thread-1"), ())

    async def test_failed_wechat_turn_stops_wait_and_pushes_once(self) -> None:
        await self.bridge.handle_text("/send 1 会失败的消息")

        await self.bridge.handle_notification(
            CodexNotification(
                "turn/completed",
                {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "failed"},
                },
            )
        )
        await self.bridge.handle_notification(
            CodexNotification(
                "turn/completed",
                {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "failed"},
                },
            )
        )

        self.assertEqual(self.bridge._wechat_turn_tasks, {})
        self.assertEqual(len(self.pushed), 1)
        self.assertIn("任务失败", self.pushed[0])
        self.assertEqual(self.bridge.sessions.get(1).status, SessionStatus.IDLE)

    async def test_failed_local_turn_never_pushes_wechat(self) -> None:
        await self.bridge.handle_notification(
            CodexNotification(
                "turn/completed",
                {
                    "threadId": "thread-1",
                    "turn": {"id": "local-turn", "status": "interrupted"},
                },
            )
        )

        self.assertEqual(self.pushed, [])

    async def test_proactive_push_failures_do_not_escape_bridge(self) -> None:
        await self.bridge.close()

        async def failing_push(_text: str) -> bool:
            raise RuntimeError("PRIVATE_SEND_FAILURE")

        self.bridge = CodexBridge(
            self.client,
            self.lease_server,
            failing_push,
            approval_ttl=0.01,
        )
        await self.bridge.handle_lease_event(LeaseBound(lease()))
        await self.bridge.handle_server_request(
            CodexServerRequest(
                22,
                COMMAND_APPROVAL,
                {
                    "threadId": "thread-1",
                    "turnId": "turn-local",
                    "itemId": "item-22",
                    "startedAtMs": 1,
                    "command": "/usr/bin/true",
                },
            )
        )
        await asyncio.sleep(0.02)
        await self.bridge.expire_approvals()
        await self.bridge.handle_lease_event(LeaseClosed(lease()))

        self.assertIsNone(self.bridge.sessions.get(1))

    async def test_final_reply_push_failure_is_contained(self) -> None:
        await self.bridge.close()

        async def failing_push(_text: str) -> bool:
            raise RuntimeError("PRIVATE_SEND_FAILURE")

        self.bridge = CodexBridge(
            self.client,
            self.lease_server,
            failing_push,
        )
        await self.bridge.handle_lease_event(LeaseBound(lease()))
        await self.bridge.handle_text("/send 1 微信消息")
        self.client.completed.set()
        await asyncio.sleep(0.01)

        self.assertEqual(self.bridge._wechat_turn_tasks, {})

    async def test_failed_approval_response_stays_pending(self) -> None:
        await self.bridge.handle_text("/focus 1")
        await self.bridge.handle_server_request(
            CodexServerRequest(
                2,
                COMMAND_APPROVAL,
                {
                    "threadId": "thread-1",
                    "turnId": "turn-local",
                    "itemId": "item-2",
                    "startedAtMs": 1,
                    "command": "/usr/bin/true",
                },
            )
        )
        self.client.response_error = CodexConnectionClosed("closed")
        result = await self.bridge.handle_text("/approve")
        self.assertIn("仍保持未批准", result)
        self.assertEqual(len(self.bridge.approvals.pending_for_thread("thread-1")), 1)

    async def test_approval_response_timeout_and_expiry_never_approve(self) -> None:
        await self.bridge.close()

        async def push(text: str) -> bool:
            self.pushed.append(text)
            return True

        self.bridge = CodexBridge(
            self.client,
            self.lease_server,
            push,
            approval_ttl=0.1,
            approval_response_timeout=0.01,
        )
        await self.bridge.handle_lease_event(LeaseBound(lease()))
        await self.bridge.handle_server_request(
            CodexServerRequest(
                20,
                COMMAND_APPROVAL,
                {
                    "threadId": "thread-1",
                    "turnId": "turn-local",
                    "itemId": "item-20",
                    "startedAtMs": 1,
                    "command": "/usr/bin/true",
                },
            )
        )
        code = self.pushed[-1].splitlines()[0].rsplit(" ", 1)[1]
        self.client.block_response = True
        result = await self.bridge.handle_text(f"/approve {code}")
        self.assertIn("仍保持未批准", result)
        self.assertEqual(len(self.bridge.approvals.pending_for_thread("thread-1")), 1)

        await asyncio.sleep(0.11)
        await self.bridge.expire_approvals()
        self.assertIn("已超时失效", self.pushed[-1])
        duplicate = await self.bridge.handle_text(f"/approve {code}")
        self.assertIn("不存在或已失效", duplicate)
        self.assertEqual(self.client.responded, [])

    async def test_lease_exit_invalidates_focus_approvals_and_turn_task(self) -> None:
        await self.bridge.handle_text("/focus 1")
        await self.bridge.handle_text("微信消息")
        await self.bridge.handle_server_request(
            CodexServerRequest(
                3,
                COMMAND_APPROVAL,
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "item-3",
                    "startedAtMs": 1,
                    "command": "/usr/bin/true",
                },
            )
        )
        await self.bridge.handle_lease_event(LeaseClosed(lease()))
        await asyncio.sleep(0)
        self.assertIsNone(self.bridge.sessions.focus)
        self.assertEqual(self.bridge.approvals.pending_for_thread("thread-1"), ())
        self.assertEqual(self.bridge._wechat_turn_tasks, {})
        self.assertIn("已退出", self.pushed[-1])

    async def test_duplicate_thread_lease_close_keeps_session_and_work(self) -> None:
        second_lease = ActiveLease(
            2,
            "thread-1",
            LeaseRegistration(
                "/tmp/project-one",
                TmuxLocation("work", "second", "%2"),
            ),
        )
        await self.bridge.handle_lease_event(LeaseBound(second_lease))
        await self.bridge.handle_text("/send 1 微信消息")
        await self.bridge.handle_server_request(
            CodexServerRequest(
                30,
                COMMAND_APPROVAL,
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "item-30",
                    "startedAtMs": 1,
                    "command": "/usr/bin/true",
                },
            )
        )

        await self.bridge.handle_lease_event(LeaseClosed(lease()))

        self.assertIsNotNone(self.bridge.sessions.get_by_thread("thread-1"))
        self.assertEqual(len(self.bridge.approvals.pending_for_thread("thread-1")), 1)
        self.assertIn(("thread-1", "turn-1"), self.bridge._wechat_turn_tasks)
        self.assertNotIn("已退出", "\n".join(self.pushed))

    async def test_thread_started_is_forwarded_only_to_pairing_server(self) -> None:
        thread = {"id": "thread-new", "cwd": "/tmp/new"}
        await self.bridge.handle_notification(
            CodexNotification("thread/started", {"thread": thread})
        )
        self.assertEqual(self.lease_server.bound, [thread])


if __name__ == "__main__":
    unittest.main()
