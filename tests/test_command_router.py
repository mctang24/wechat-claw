from __future__ import annotations

import unittest

from wechat_claw.command_router import ApproveRequest, CommandRouter, SendMessage
from wechat_claw.lease_server import ActiveLease, LeaseRegistration, TmuxLocation
from wechat_claw.session_registry import SessionRegistry, SessionStatus


def populated_registry() -> SessionRegistry:
    registry = SessionRegistry()
    registry.add_lease(
        ActiveLease(
            lease_id=10,
            thread_id="thread-1",
            registration=LeaseRegistration(
                cwd="/tmp/project-a",
                tmux=TmuxLocation("main", "editor", "%1"),
            ),
        )
    )
    return registry


class CommandRouterTest(unittest.TestCase):
    def test_sessions_shows_original_message_and_truncation(self) -> None:
        registry = populated_registry()
        registry.update_status("thread-1", SessionStatus.RUNNING)
        registry.record_message("thread-1", "第一行\n第二行很长")
        router = CommandRouter(registry, preview_limit=8)

        result = router.route("/sessions")

        self.assertIn("1. project-a", result.reply)
        self.assertIn("编号用于 /focus 和 /send", result.reply)
        self.assertIn("tmux：main/editor/%1", result.reply)
        self.assertIn("状态：处理中", result.reply)
        self.assertIn("第一行\n第二", result.reply)
        self.assertIn("内容已截断", result.reply)

    def test_sessions_omits_tmux_line_outside_tmux(self) -> None:
        registry = SessionRegistry()
        registry.add_lease(
            ActiveLease(
                lease_id=11,
                thread_id="thread-plain-terminal",
                registration=LeaseRegistration(
                    cwd="/tmp/plain-terminal",
                    tmux=TmuxLocation(None, None, None),
                ),
            )
        )

        result = CommandRouter(registry).route("/sessions")

        self.assertIn("1. plain-terminal", result.reply)
        self.assertNotIn("tmux：", result.reply)

    def test_focus_plain_text_send_and_unfocus(self) -> None:
        registry = populated_registry()
        router = CommandRouter(registry)

        self.assertIn("已 focus", router.route("/focus 1").reply)
        self.assertEqual(
            router.route("原文\n第二行").send,
            SendMessage(1, "thread-1", "原文\n第二行"),
        )
        self.assertIn("已解除", router.route("/unfocus").reply)
        self.assertIn("没有 focus", router.route("普通消息").reply)

    def test_invalid_focus_does_not_replace_existing_focus(self) -> None:
        registry = populated_registry()
        router = CommandRouter(registry)
        router.route("/focus 1")

        self.assertIn("不存在", router.route("/focus 99").reply)
        self.assertEqual(registry.focus.number, 1)

    def test_send_preserves_body_and_does_not_change_focus(self) -> None:
        registry = populated_registry()
        router = CommandRouter(registry)

        result = router.route("/send 1 第一行\n第二行")

        self.assertEqual(result.send, SendMessage(1, "thread-1", "第一行\n第二行"))
        self.assertIsNone(registry.focus)

    def test_approve_is_local_action(self) -> None:
        router = CommandRouter(populated_registry())

        self.assertEqual(router.route("/approve").approve, ApproveRequest(None))
        self.assertEqual(router.route("/approve A1B2").approve, ApproveRequest("A1B2"))

    def test_unknown_command_and_empty_input_never_send(self) -> None:
        router = CommandRouter(populated_registry())

        for text in ("/unknown value", "", "   "):
            with self.subTest(text=text):
                result = router.route(text)
                self.assertIsNone(result.send)
                self.assertIsNotNone(result.reply)

    def test_malformed_known_commands_return_usage(self) -> None:
        router = CommandRouter(populated_registry())
        cases = {
            "/sessions keyword": "/sessions",
            "/focus abc": "/focus <编号>",
            "/unfocus extra": "/unfocus",
            "/send 1": "/send <编号> <消息>",
            "/approve A B": "/approve [审批码]",
        }

        for text, usage in cases.items():
            with self.subTest(text=text):
                result = router.route(text)
                self.assertIn(usage, result.reply)
                self.assertIsNone(result.send)
                self.assertIsNone(result.approve)

    def test_help_contains_all_v1_commands_and_focus(self) -> None:
        registry = populated_registry()
        registry.set_focus(1)
        result = CommandRouter(registry).route("/help")

        for command in ("/sessions", "/focus", "/unfocus", "/send", "/approve", "/help"):
            self.assertIn(command, result.reply)
        self.assertIn("当前 focus：1（project-a）", result.reply)

    def test_command_boundaries_are_table_driven(self) -> None:
        registry = populated_registry()
        router = CommandRouter(registry)
        long_text = "长" * 5000
        cases = (
            (" /sessions project-a ", "reply", "用法：/sessions"),
            ("/SESSIONS", "reply", "未知命令"),
            ("/focus", "reply", "/focus <编号>"),
            ("/focus 0", "reply", "不存在"),
            ("/unfocus ", "reply", "当前未选择"),
            ("/send 0 text", "reply", "不存在"),
            ("/send 1   ", "reply", "/send <编号> <消息>"),
            ("/approve ", "approve", None),
            ("/help extra", "reply", "未知命令"),
            (f"/send 1 {long_text}", "send", long_text),
        )

        for text, kind, expected in cases:
            with self.subTest(text=text[:40], kind=kind):
                result = router.route(text)
                if kind == "reply":
                    self.assertIsNotNone(result.reply)
                    self.assertIn(expected, result.reply)
                    self.assertIsNone(result.send)
                    self.assertIsNone(result.approve)
                elif kind == "approve":
                    self.assertEqual(result.approve, ApproveRequest(None))
                else:
                    self.assertIsNotNone(result.send)
                    self.assertEqual(result.send.text, expected)

    def test_shell_syntax_is_only_message_text(self) -> None:
        router = CommandRouter(populated_registry())
        payload = "$(touch /tmp/must-not-run); `uname`; $HOME"

        result = router.route(f"/send 1 {payload}")

        self.assertEqual(result.send, SendMessage(1, "thread-1", payload))


if __name__ == "__main__":
    unittest.main()
