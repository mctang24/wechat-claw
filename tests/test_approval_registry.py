from __future__ import annotations

import unittest

from wechat_claw.approval_registry import (
    ApprovalError,
    ApprovalRegistry,
    COMMAND_APPROVAL,
    FILE_APPROVAL,
    PERMISSIONS_APPROVAL,
)
from wechat_claw.codex_protocol import CodexServerRequest
from wechat_claw.lease_server import ActiveLease, LeaseRegistration, TmuxLocation
from wechat_claw.session_registry import SessionRegistry


def make_sessions() -> SessionRegistry:
    sessions = SessionRegistry()
    sessions.add_lease(
        ActiveLease(
            lease_id=1,
            thread_id="thread-1",
            registration=LeaseRegistration(
                cwd="/tmp/project-one",
                tmux=TmuxLocation("work", "editor", "%1"),
            ),
        )
    )
    sessions.add_lease(
        ActiveLease(
            lease_id=2,
            thread_id="thread-2",
            registration=LeaseRegistration(
                cwd="/tmp/project-two",
                tmux=TmuxLocation("work", "editor", "%2"),
            ),
        )
    )
    return sessions


def request(
    request_id: int | str,
    method: str = COMMAND_APPROVAL,
    *,
    thread_id: str = "thread-1",
    turn_id: str = "turn-1",
    item_id: str = "item-1",
    **params: object,
) -> CodexServerRequest:
    return CodexServerRequest(
        request_id,
        method,
        {
            "threadId": thread_id,
            "turnId": turn_id,
            "itemId": item_id,
            "startedAtMs": 1,
            **params,
        },
    )


class ApprovalRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100.0
        codes = iter(("ABC123", "DEF456", "GHI789"))
        self.sessions = make_sessions()
        self.registry = ApprovalRegistry(
            self.sessions,
            ttl=300,
            clock=lambda: self.now,
            code_factory=lambda: next(codes),
        )

    def test_registers_command_once_and_uses_schema_accept(self) -> None:
        incoming = request(
            7,
            command="curl https://example.com?token=private",
            reason="password=hunter2",
            availableDecisions=["accept", "decline"],
        )
        first = self.registry.register(incoming)
        second = self.registry.register(incoming)

        self.assertIsNotNone(first)
        assert first is not None and second is not None
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.approval, second.approval)
        self.assertEqual(first.approval.response, {"decision": "accept"})
        self.assertIn("/approve ABC123", first.approval.notification)
        self.assertNotIn("private", first.approval.notification)
        self.assertNotIn("hunter2", first.approval.notification)

    def test_file_and_permissions_responses_match_schema(self) -> None:
        file_result = self.registry.register(request(8, FILE_APPROVAL, grantRoot="/tmp"))
        permission_result = self.registry.register(
            request(
                9,
                PERMISSIONS_APPROVAL,
                thread_id="thread-2",
                turn_id="turn-2",
                item_id="item-2",
                cwd="/tmp/project-two",
                permissions={"network": {"enabled": True}},
            )
        )

        assert file_result is not None and permission_result is not None
        self.assertEqual(file_result.approval.response, {"decision": "accept"})
        self.assertEqual(
            permission_result.approval.response,
            {"permissions": {"network": {"enabled": True}}, "scope": "turn"},
        )

    def test_requires_accept_when_server_limits_decisions(self) -> None:
        with self.assertRaisesRegex(ApprovalError, "不允许单次批准"):
            self.registry.register(
                request(10, availableDecisions=["decline", "cancel"])
            )

    def test_ignores_unknown_or_inactive_approval(self) -> None:
        self.assertIsNone(self.registry.register(request(11, "unknown/request")))
        self.assertIsNone(
            self.registry.register(request(12, thread_id="inactive-thread"))
        )

    def test_resolve_by_code_or_unique_focused_request(self) -> None:
        first = self.registry.register(request(13))
        assert first is not None
        self.assertEqual(
            self.registry.resolve("abc123", focus_thread_id=None),
            first.approval,
        )
        self.assertEqual(
            self.registry.resolve(None, focus_thread_id="thread-1"),
            first.approval,
        )

        self.registry.register(request(14, turn_id="turn-2", item_id="item-2"))
        with self.assertRaisesRegex(ApprovalError, "多个待审批"):
            self.registry.resolve(None, focus_thread_id="thread-1")

    def test_complete_duplicate_expiry_and_session_exit_are_safe(self) -> None:
        first = self.registry.register(request(15))
        assert first is not None
        self.assertTrue(self.registry.complete(first.approval))
        self.assertFalse(self.registry.complete(first.approval))
        with self.assertRaisesRegex(ApprovalError, "不存在或已失效"):
            self.registry.resolve(first.approval.code, focus_thread_id=None)

        second = self.registry.register(request(16))
        assert second is not None
        self.now = 401.0
        with self.assertRaisesRegex(ApprovalError, "不存在或已失效"):
            self.registry.resolve(second.approval.code, focus_thread_id=None)

        third = self.registry.register(request(17))
        assert third is not None
        self.sessions.remove_lease(1)
        with self.assertRaisesRegex(ApprovalError, "所属会话已退出"):
            self.registry.resolve(third.approval.code, focus_thread_id=None)

    def test_request_id_type_is_part_of_identity(self) -> None:
        numeric = self.registry.register(request(18))
        textual = self.registry.register(request("18", turn_id="turn-2", item_id="item-2"))
        assert numeric is not None and textual is not None
        self.assertNotEqual(numeric.approval.code, textual.approval.code)

        self.assertEqual(self.registry.invalidate_request(18), numeric.approval)
        self.assertIsNone(self.registry.invalidate_request(18))
        self.assertEqual(
            self.registry.resolve("DEF456", focus_thread_id=None),
            textual.approval,
        )

    def test_notification_redacts_common_secret_forms(self) -> None:
        incoming = request(
            19,
            command=(
                "curl -H 'Authorization: Bearer bearer-private' "
                "-H 'Authorization: Basic basic-private' "
                "-H 'Cookie: sid=cookie-private' "
                "--token cli-private --api-key=key-private "
                "https://example.test?access_token=query-private"
            ),
            reason="credential sk-proj-privatevalue123456",
        )

        registered = self.registry.register(incoming)

        assert registered is not None
        notification = registered.approval.notification
        for secret in (
            "bearer-private",
            "basic-private",
            "cookie-private",
            "cli-private",
            "key-private",
            "query-private",
            "sk-proj-privatevalue123456",
        ):
            self.assertNotIn(secret, notification)
        self.assertIn("[REDACTED]", notification)


if __name__ == "__main__":
    unittest.main()
