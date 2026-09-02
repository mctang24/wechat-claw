"""Bound-user text adapter for the Python WeChat iLink SDK."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from html import escape
from pathlib import Path
import sys
from time import monotonic
from typing import Any, Protocol

from wechatbot import WeChatBot
from wechatbot.protocol import ILinkApi

from .redaction import redact_sensitive_text
from .wechat_binding import BindingStore, WeChatBinding


TextHandler = Callable[[str], Awaitable[str | Sequence[str] | None]]
LOW_LATENCY_POLL_SECONDS = 4


class LowLatencyILinkApi(ILinkApi):
    """Use sequential short polls to bound stale long-poll connections."""

    async def get_updates(
        self,
        base_url: str,
        token: str,
        cursor: str,
    ) -> dict[str, Any]:
        body = {"get_updates_buf": cursor, "base_info": self._base_info()}
        try:
            return await self._post(
                base_url,
                "/ilink/bot/getupdates",
                token,
                body,
                LOW_LATENCY_POLL_SECONDS,
            )
        except TimeoutError:
            return {"ret": 0, "msgs": [], "get_updates_buf": cursor}


class RedactedWeChatBot(WeChatBot):
    """Restrict SDK-owned logs to fixed events without user-controlled text."""

    def _log(self, msg: str) -> None:
        if msg.startswith("Logged in as "):
            safe_message = "Logged in"
        elif msg in {
            "Long-poll started",
            "Long-poll stopped",
            "Session expired — re-login",
        }:
            safe_message = msg
        elif msg.startswith("notify_start failed"):
            safe_message = "notify_start failed"
        elif msg.startswith("notify_stop failed"):
            safe_message = "notify_stop failed"
        else:
            safe_message = "SDK error"
        print(f"[wechatbot] {safe_message}", file=sys.stderr)


class IncomingMessage(Protocol):
    user_id: str
    text: str
    _context_token: str


class BotProtocol(Protocol):
    _context_tokens: dict[str, str]

    def on_message(self, handler: Callable[[IncomingMessage], Awaitable[None]]) -> Any: ...

    async def login(self) -> Any: ...

    async def start(self) -> Any: ...

    async def reply(self, message: IncomingMessage, text: str) -> Any: ...

    async def send(self, user_id: str, text: str) -> Any: ...

    def stop(self) -> Any: ...


class WeChatAdapter:
    def __init__(
        self,
        bot: BotProtocol,
        binding_store: BindingStore,
        text_handler: TextHandler,
        *,
        message_limit: int = 1800,
    ) -> None:
        if message_limit < 8:
            raise ValueError("message_limit must be at least 8")
        self.bot = bot
        self.binding_store = binding_store
        self.text_handler = text_handler
        self.message_limit = message_limit
        self.bot.on_message(self.handle_message)

    async def run(self) -> None:
        await self.bot.login()
        self.hydrate_context()
        await self.bot.start()

    async def handle_message(self, message: IncomingMessage) -> None:
        received_at = datetime.now(timezone.utc)
        started = monotonic()
        user_id = str(message.user_id or "")
        context_token = str(getattr(message, "_context_token", "") or "")
        if not user_id or self._is_bot_self(user_id):
            return
        self._remember_context(user_id, context_token)

        binding = self.binding_store.load()
        if binding is None:
            if not context_token:
                _log_send_failure("missing_initial_context")
                return
            binding = self.binding_store.save(user_id, context_token)
        elif binding.user_id != user_id:
            try:
                await self.bot.reply(message, "当前微信帐号未绑定，无法执行操作。")
            except Exception:
                _log_send_failure("unbound_reply")
            return
        else:
            if context_token:
                refreshed = self.binding_store.refresh(user_id, context_token)
                if refreshed is not None:
                    binding = refreshed

        text = message.text or ""
        response = await self.text_handler(text)
        routed = monotonic()
        for part in self._response_parts(response):
            try:
                await self.bot.send(user_id, part)
            except Exception:
                _log_send_failure("inbound_response")
                break
        _log_message_timing(
            message,
            text,
            received_at,
            started,
            routed,
            monotonic(),
        )

    async def send_to_bound(self, text: str) -> bool:
        binding = self.binding_store.load()
        if binding is None or self._is_bot_self(binding.user_id):
            return False
        self._restore_context(binding.user_id, binding.context_token)
        safe_text = redact_sensitive_text(text)
        for part in split_text(safe_text, self.message_limit):
            await self.bot.send(binding.user_id, part)
        return True

    def hydrate_context(self) -> bool:
        binding = self.binding_store.load()
        if binding is None or not binding.context_token or self._is_bot_self(binding.user_id):
            return False
        return self._restore_context(binding.user_id, binding.context_token)

    def _response_parts(self, response: str | Sequence[str] | None) -> tuple[str, ...]:
        if response is None:
            return ()
        values = (response,) if isinstance(response, str) else tuple(response)
        return tuple(
            part
            for value in values
            if value
            for part in split_text(
                redact_sensitive_text(value),
                self.message_limit,
            )
        )

    def _remember_context(self, user_id: str, context_token: str) -> bool:
        if not user_id or not context_token:
            return False
        tokens = getattr(self.bot, "_context_tokens", None)
        if not isinstance(tokens, dict):
            return False
        tokens[user_id] = context_token
        return True

    def _restore_context(self, user_id: str, context_token: str) -> bool:
        if not user_id or not context_token:
            return False
        tokens = getattr(self.bot, "_context_tokens", None)
        if not isinstance(tokens, dict):
            return False
        if tokens.get(user_id):
            return True
        tokens[user_id] = context_token
        return True

    def _is_bot_self(self, user_id: str) -> bool:
        ids: set[str] = set()
        credentials = getattr(self.bot, "_credentials", None)
        account_id = str(getattr(credentials, "account_id", "") or "")
        if account_id:
            ids.add(account_id)
        direct_account_id = str(getattr(self.bot, "account_id", "") or "")
        if direct_account_id:
            ids.add(direct_account_id)
        return user_id in ids


def build_bot(credentials_path: Path, qr_page_path: Path) -> WeChatBot:
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    bot = RedactedWeChatBot(
        cred_path=str(credentials_path),
        on_qr_url=lambda url: _write_qr_page(qr_page_path, url),
        on_scanned=lambda: print("微信已扫码，等待确认登录。"),
        on_expired=lambda: print("微信登录二维码已过期，等待刷新。"),
        on_error=lambda error: print(
            f"微信连接事件：{format_wechat_error(error)}"
        ),
    )
    bot._api = LowLatencyILinkApi()
    return bot


def split_text(text: str, limit: int) -> tuple[str, ...]:
    if limit < 8:
        raise ValueError("limit must be at least 8")
    if len(text) <= limit:
        return (text,) if text else ()
    total = 2
    while True:
        prefix_length = len(f"[{total}/{total}]\n")
        content_limit = limit - prefix_length
        calculated = (len(text) + content_limit - 1) // content_limit
        if calculated == total:
            break
        total = calculated
    chunks = tuple(
        text[index : index + content_limit]
        for index in range(0, len(text), content_limit)
    )
    return tuple(f"[{index}/{total}]\n{chunk}" for index, chunk in enumerate(chunks, 1))


def format_wechat_error(error: Exception) -> str:
    fields = [type(error).__name__]
    for attribute in ("http_status", "errcode"):
        value = getattr(error, attribute, None)
        if isinstance(value, int):
            fields.append(f"{attribute}={value}")
    expired = getattr(error, "is_session_expired", None)
    if isinstance(expired, bool):
        fields.append(f"session_expired={str(expired).lower()}")
    return " ".join(fields)


def _log_send_failure(event: str) -> None:
    print(f"[wechat-claw] wechat send failed: {event}", flush=True)


def _log_message_timing(
    message: IncomingMessage,
    text: str,
    received_at: datetime,
    started: float,
    routed: float,
    finished: float,
) -> None:
    created_at = getattr(message, "timestamp", None)
    queue_ms: int | None = None
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        queue_ms = max(
            0,
            round(
                (received_at - created_at.astimezone(timezone.utc))
                .total_seconds()
                * 1000
            ),
        )
    kind = "command" if text.lstrip().startswith("/") else "text"
    queue_value = str(queue_ms) if queue_ms is not None else "unknown"
    print(
        "[wechat-claw] wechat timing: "
        f"kind={kind} queue_ms={queue_value} "
        f"route_ms={round((routed - started) * 1000)} "
        f"send_ms={round((finished - routed) * 1000)}",
        flush=True,
    )


def _write_qr_page(path: Path, url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_url = escape(url, quote=True)
    path.write_text(
        "".join(
            (
                '<!doctype html><meta charset="utf-8">',
                '<meta name="viewport" content="width=device-width,initial-scale=1">',
                "<title>WeChat Claw Login</title>",
                '<body style="font-family:sans-serif;text-align:center;padding:24px">',
                "<h1>WeChat Claw Login</h1>",
                f'<p><a href="{safe_url}" target="_blank" rel="noreferrer">打开登录页面</a></p>',
                f'<iframe src="{safe_url}" title="WeChat login" style="width:420px;height:520px;max-width:96vw"></iframe>',
                "<p>请使用微信扫描页面中的二维码。</p></body>",
            )
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    print(f"微信登录页面：{path}")
