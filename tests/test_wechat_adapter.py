from __future__ import annotations

import json
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from wechat_claw.wechat_adapter import (
    LOW_LATENCY_POLL_SECONDS,
    LowLatencyILinkApi,
    RedactedWeChatBot,
    WeChatAdapter,
    format_wechat_error,
    split_text,
)
from wechat_claw.wechat_binding import BindingError, BindingStore


@dataclass
class Message:
    user_id: str
    text: str
    _context_token: str


class FakeBot:
    def __init__(self) -> None:
        self._context_tokens: dict[str, str] = {}
        self.handler = None
        self.replies: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.login_calls = 0
        self.start_calls = 0
        self.send_error: Exception | None = None

    def on_message(self, handler):
        self.handler = handler
        return handler

    async def login(self) -> None:
        self.login_calls += 1

    async def start(self) -> None:
        self.start_calls += 1

    async def reply(self, message: Message, text: str) -> None:
        self.replies.append((message.user_id, text))

    async def send(self, user_id: str, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((user_id, text))


class BindingStoreTest(unittest.TestCase):
    def test_save_load_refresh_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "binding.json"
            store = BindingStore(path)
            saved = store.save("user-1", "ctx-1")
            refreshed = store.refresh("user-1", "ctx-2")

            self.assertEqual(store.load(), refreshed)
            self.assertEqual(refreshed.user_id, saved.user_id)
            self.assertEqual(refreshed.bound_at, saved.bound_at)
            self.assertEqual(refreshed.context_token, "ctx-2")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                set(json.loads(path.read_text())),
                {"user_id", "context_token", "bound_at", "context_updated_at"},
            )

    def test_invalid_binding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "binding.json"
            path.write_text('{"context_token":"secret"}')
            with self.assertRaises(BindingError):
                BindingStore(path).load()


class WeChatAdapterTest(unittest.IsolatedAsyncioTestCase):
    def test_sdk_internal_log_never_outputs_identity_or_raw_error(self) -> None:
        bot = object.__new__(RedactedWeChatBot)
        output = StringIO()

        with redirect_stderr(output):
            bot._log("Logged in as PRIVATE_ACCOUNT_ID")
            bot._log("token=PRIVATE_RAW_ERROR")

        logged = output.getvalue()
        self.assertIn("Logged in", logged)
        self.assertIn("SDK error", logged)
        self.assertNotIn("PRIVATE_ACCOUNT_ID", logged)
        self.assertNotIn("PRIVATE_RAW_ERROR", logged)

    async def test_low_latency_poll_passes_through_success_and_other_errors(self) -> None:
        response = {"ret": 0, "msgs": [{"seq": 1}], "get_updates_buf": "next"}

        class StubApi(LowLatencyILinkApi):
            error: Exception | None = None

            async def _post(
                self,
                _base_url: str,
                _endpoint: str,
                _token: str,
                _body: dict[str, object],
                timeout_secs: int = 15,
            ) -> dict[str, object]:
                self.asserted_timeout = timeout_secs
                if self.error is not None:
                    raise self.error
                return response

        api = StubApi()
        self.assertIs(
            await api.get_updates("https://example.test", "token", "cursor"),
            response,
        )
        self.assertEqual(api.asserted_timeout, LOW_LATENCY_POLL_SECONDS)

        api.error = RuntimeError("protocol failure")
        with self.assertRaisesRegex(RuntimeError, "protocol failure"):
            await api.get_updates("https://example.test", "token", "cursor")

    async def test_low_latency_poll_timeout_returns_empty_cursor_result(self) -> None:
        class TimeoutApi(LowLatencyILinkApi):
            timeout_seconds: int | None = None

            async def _post(
                self,
                _base_url: str,
                _endpoint: str,
                _token: str,
                _body: dict[str, object],
                timeout_secs: int = 15,
            ) -> dict[str, object]:
                self.timeout_seconds = timeout_secs
                raise TimeoutError

        api = TimeoutApi()
        result = await api.get_updates("https://example.test", "token", "cursor")

        self.assertEqual(api.timeout_seconds, LOW_LATENCY_POLL_SECONDS)
        self.assertEqual(
            result,
            {"ret": 0, "msgs": [], "get_updates_buf": "cursor"},
        )

    async def test_timing_log_does_not_include_message_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()

            async def handler(_text: str) -> str:
                return "完成"

            adapter = WeChatAdapter(
                bot,
                BindingStore(Path(directory) / "binding.json"),
                handler,
            )
            output = StringIO()
            with redirect_stdout(output):
                await adapter.handle_message(
                    Message("user-1", "PRIVATE_TIMING_TEXT", "ctx-1")
                )

            self.assertIn("kind=text", output.getvalue())
            self.assertIn("queue_ms=unknown", output.getvalue())
            self.assertNotIn("PRIVATE_TIMING_TEXT", output.getvalue())

    async def test_first_sender_binds_and_message_is_processed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()
            received: list[str] = []

            async def handler(text: str) -> str:
                received.append(text)
                return "处理完成"

            store = BindingStore(Path(directory) / "binding.json")
            adapter = WeChatAdapter(bot, store, handler)
            await adapter.handle_message(Message("user-1", "/sessions", "ctx-1"))

            self.assertEqual(store.load().user_id, "user-1")
            self.assertEqual(received, ["/sessions"])
            self.assertEqual(bot.replies, [])
            self.assertEqual(bot.sent, [("user-1", "处理完成")])
            self.assertEqual(bot._context_tokens, {"user-1": "ctx-1"})

    async def test_other_sender_cannot_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()
            called = False

            async def handler(_text: str) -> None:
                nonlocal called
                called = True

            store = BindingStore(Path(directory) / "binding.json")
            store.save("user-1", "ctx-1")
            adapter = WeChatAdapter(bot, store, handler)
            await adapter.handle_message(Message("user-2", "/sessions", "ctx-2"))

            self.assertFalse(called)
            self.assertIn("未绑定", bot.replies[0][1])
            self.assertEqual(store.load().context_token, "ctx-1")

    async def test_bound_sender_refreshes_context_and_can_receive_push(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()

            async def handler(_text: str) -> None:
                return None

            store = BindingStore(Path(directory) / "binding.json")
            store.save("user-1", "old")
            adapter = WeChatAdapter(bot, store, handler, message_limit=20)
            await adapter.handle_message(Message("user-1", "hello", "new"))
            sent = await adapter.send_to_bound("123456789012345678901")

            self.assertTrue(sent)
            self.assertEqual(store.load().context_token, "new")
            self.assertEqual(bot._context_tokens, {"user-1": "new"})
            self.assertEqual(
                bot.sent,
                [
                    ("user-1", "[1/2]\n12345678901234"),
                    ("user-1", "[2/2]\n5678901"),
                ],
            )

    async def test_proactive_push_never_overwrites_rotated_sdk_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()

            async def handler(_text: str) -> None:
                return None

            store = BindingStore(Path(directory) / "binding.json")
            store.save("user-1", "persisted-old")
            bot._context_tokens["user-1"] = "sdk-rotated"
            adapter = WeChatAdapter(bot, store, handler)

            sent = await adapter.send_to_bound("主动消息")

            self.assertTrue(sent)
            self.assertEqual(bot._context_tokens["user-1"], "sdk-rotated")

    async def test_empty_context_never_overwrites_valid_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()

            async def handler(_text: str) -> str:
                return "处理完成"

            store = BindingStore(Path(directory) / "binding.json")
            store.save("user-1", "valid-context")
            adapter = WeChatAdapter(bot, store, handler)
            adapter.hydrate_context()

            await adapter.handle_message(Message("user-1", "/sessions", ""))

            self.assertEqual(store.load().context_token, "valid-context")
            self.assertEqual(bot._context_tokens, {"user-1": "valid-context"})
            self.assertEqual(bot.sent, [("user-1", "处理完成")])

    async def test_missing_initial_context_does_not_bind_or_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()
            called = False

            async def handler(_text: str) -> None:
                nonlocal called
                called = True

            store = BindingStore(Path(directory) / "binding.json")
            adapter = WeChatAdapter(bot, store, handler)
            output = StringIO()

            with redirect_stdout(output):
                await adapter.handle_message(Message("user-1", "/sessions", ""))

            self.assertIsNone(store.load())
            self.assertFalse(called)
            self.assertIn("missing_initial_context", output.getvalue())

    async def test_all_outbound_text_is_redacted_before_splitting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()

            async def handler(_text: str) -> str:
                return "结果 token=inbound-private"

            store = BindingStore(Path(directory) / "binding.json")
            store.save("user-1", "context")
            adapter = WeChatAdapter(bot, store, handler)

            await adapter.handle_message(Message("user-1", "/sessions", "context"))
            await adapter.send_to_bound(
                "Authorization: Bearer proactive-private"
            )

            rendered = "\n".join(text for _, text in bot.sent)
            self.assertNotIn("inbound-private", rendered)
            self.assertNotIn("proactive-private", rendered)
            self.assertGreaterEqual(rendered.count("[REDACTED]"), 2)

    async def test_run_logs_in_hydrates_and_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()
            store = BindingStore(Path(directory) / "binding.json")
            store.save("user-1", "ctx")

            async def handler(_text: str) -> None:
                return None

            adapter = WeChatAdapter(bot, store, handler)
            await adapter.run()

            self.assertEqual((bot.login_calls, bot.start_calls), (1, 1))
            self.assertEqual(bot._context_tokens, {"user-1": "ctx"})

    async def test_missing_binding_and_send_failure_never_report_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()

            async def handler(_text: str) -> None:
                return None

            store = BindingStore(Path(directory) / "binding.json")
            adapter = WeChatAdapter(bot, store, handler)
            self.assertFalse(await adapter.send_to_bound("message"))

            store.save("user-1", "ctx")
            bot.send_error = RuntimeError("send failed")
            with self.assertRaises(RuntimeError):
                await adapter.send_to_bound("message")
            self.assertEqual(bot.sent, [])

    async def test_inbound_response_send_failure_is_contained_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()
            bot.send_error = RuntimeError("PRIVATE_SEND_FAILURE")

            async def handler(_text: str) -> str:
                return "处理完成"

            adapter = WeChatAdapter(
                bot,
                BindingStore(Path(directory) / "binding.json"),
                handler,
            )
            output = StringIO()
            with redirect_stdout(output):
                await adapter.handle_message(
                    Message("PRIVATE_USER_ID", "/sessions", "PRIVATE_CONTEXT")
                )

            logged = output.getvalue()
            self.assertIn("wechat send failed: inbound_response", logged)
            self.assertNotIn("PRIVATE_SEND_FAILURE", logged)
            self.assertNotIn("PRIVATE_USER_ID", logged)
            self.assertNotIn("PRIVATE_CONTEXT", logged)

    def test_split_text_and_error_redaction(self) -> None:
        self.assertEqual(split_text("abc", 8), ("abc",))
        self.assertEqual(
            split_text("123456789012345678901", 20),
            ("[1/2]\n12345678901234", "[2/2]\n5678901"),
        )

        class ApiError(Exception):
            http_status = 200
            errcode = -14
            is_session_expired = True
            payload = {"token": "secret"}

        formatted = format_wechat_error(ApiError("token=secret"))
        self.assertIn("http_status=200", formatted)
        self.assertNotIn("secret", formatted)


if __name__ == "__main__":
    unittest.main()
