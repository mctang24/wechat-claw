"""Parse supported WeChat commands without executing external operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .session_registry import ActiveSession, SessionRegistry, SessionStatus


@dataclass(frozen=True, slots=True)
class SendMessage:
    session_number: int
    thread_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ApproveRequest:
    approval_code: str | None


@dataclass(frozen=True, slots=True)
class RouteResult:
    reply: str | None = None
    send: SendMessage | None = None
    approve: ApproveRequest | None = None


_SEND = re.compile(r"^/send\s+(\d+)\s+(.+)$", re.DOTALL)
_FOCUS = re.compile(r"^/focus\s+(\d+)\s*$")
_APPROVE = re.compile(r"^/approve(?:\s+(\S+))?\s*$")
_STATUS_LABELS = {
    SessionStatus.IDLE: "空闲",
    SessionStatus.RUNNING: "处理中",
    SessionStatus.WAITING_APPROVAL: "等待审批",
}


class CommandRouter:
    def __init__(self, sessions: SessionRegistry, *, preview_limit: int = 800) -> None:
        self.sessions = sessions
        self.preview_limit = preview_limit

    def route(self, raw_text: str) -> RouteResult:
        text = raw_text.strip()
        if not text:
            return RouteResult(reply="消息为空，未发送。")
        if text == "/sessions" or text.startswith("/sessions "):
            keyword = text[len("/sessions") :].strip() or None
            return RouteResult(reply=self._format_sessions(keyword))
        if match := _FOCUS.fullmatch(text):
            return RouteResult(reply=self._focus(int(match.group(1))))
        if text == "/unfocus":
            previous = self.sessions.clear_focus()
            if previous is None:
                return RouteResult(reply="当前未选择会话。")
            return RouteResult(reply=f"已解除会话 {previous.number}（{previous.project_name}）的 focus。")
        if match := _SEND.fullmatch(text):
            return self._send(int(match.group(1)), match.group(2))
        if match := _APPROVE.fullmatch(text):
            return RouteResult(approve=ApproveRequest(match.group(1)))
        if text == "/help":
            return RouteResult(reply=self._format_help())
        if text.startswith("/focus"):
            return RouteResult(reply="用法：/focus <编号>")
        if text.startswith("/unfocus"):
            return RouteResult(reply="用法：/unfocus")
        if text.startswith("/send"):
            return RouteResult(reply="用法：/send <编号> <消息>")
        if text.startswith("/approve"):
            return RouteResult(reply="用法：/approve [审批码]")
        if text.startswith("/"):
            return RouteResult(reply="未知命令。请发送 /help 查看支持的命令。")
        focus = self.sessions.focus
        if focus is None:
            return RouteResult(reply="当前没有 focus。请先使用 /sessions、/focus 或 /send。")
        return RouteResult(
            send=SendMessage(focus.number, focus.thread_id, raw_text),
        )

    def _focus(self, number: int) -> str:
        previous = self.sessions.focus
        session = self.sessions.set_focus(number)
        if session is None:
            return "会话编号不存在或已失效，请重新发送 /sessions。"
        if previous is not None and previous.number == session.number:
            return f"当前已 focus 会话 {session.number}（{session.project_name}）。"
        return f"已 focus 会话 {session.number}（{session.project_name}）。"

    def _send(self, number: int, message: str) -> RouteResult:
        session = self.sessions.get(number)
        if session is None:
            return RouteResult(reply="会话编号不存在或已失效，请重新发送 /sessions。")
        return RouteResult(send=SendMessage(number, session.thread_id, message))

    def _format_sessions(self, keyword: str | None) -> str:
        sessions = self.sessions.list(keyword)
        if not sessions:
            return "当前没有匹配的活跃 Codex 会话。" if keyword else "当前没有活跃 Codex 会话。"
        lines = [f"活跃 Codex 会话（{len(sessions)} 个）"]
        for session in sessions:
            lines.extend(self._format_session(session))
        return "\n".join(lines)

    def _format_session(self, session: ActiveSession) -> list[str]:
        tmux = "/".join(
            value or "-"
            for value in (session.tmux.session, session.tmux.window, session.tmux.pane)
        )
        latest = session.latest_message or "（暂无消息）"
        if len(latest) > self.preview_limit:
            latest = latest[: self.preview_limit] + "\n（内容已截断）"
        return [
            "",
            f"{session.number}. {session.project_name}",
            f"目录：{session.cwd}",
            f"tmux：{tmux}",
            f"状态：{_STATUS_LABELS[session.status]}",
            f"最近活动：{_format_time(session.last_activity)}",
            f"最近消息：{latest}",
        ]

    def _format_help(self) -> str:
        focus = self.sessions.focus
        focus_text = (
            f"{focus.number}（{focus.project_name}）" if focus is not None else "未选择"
        )
        return "\n".join(
            (
                "WeChat Claw 命令",
                "/sessions [关键词]：查看或过滤活跃会话",
                "/focus <编号>：持续选择一个会话",
                "/unfocus：解除当前选择",
                "/send <编号> <消息>：单次定向发送，不改变 focus",
                "/approve [审批码]：批准精确匹配的待审批请求",
                "/help：查看本帮助",
                "",
                f"当前 focus：{focus_text}",
                "普通文本只会发送给当前 focus；审批前请核对操作和风险。",
            )
        )


def _format_time(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")
