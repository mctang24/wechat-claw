"""Coordinate active leases, WeChat commands, Codex turns, and approvals."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .approval_registry import ApprovalError, ApprovalRegistry
from .codex_protocol import (
    CodexAppServerClient,
    CodexNotification,
    CodexProtocolError,
    CodexServerRequest,
    CodexTurn,
    CodexTurnTimeout,
)
from .command_router import CommandRouter
from .lease_server import LeaseBound, LeaseClosed, LeaseEvent, LeaseServer
from .session_registry import SessionRegistry, SessionStatus


PushText = Callable[[str], Awaitable[bool]]
USER_INPUT_REQUEST = "item/tool/requestUserInput"


class CodexBridge:
    """The in-memory authority for all first-version routing decisions."""

    def __init__(
        self,
        client: CodexAppServerClient,
        lease_server: LeaseServer,
        push_text: PushText,
        *,
        turn_progress_after: float = 60.0,
        turn_timeout: float = 600.0,
        poll_interval: float = 0.5,
        approval_ttl: float = 300.0,
        approval_response_timeout: float = 10.0,
    ) -> None:
        if turn_progress_after <= 0:
            raise ValueError("turn progress deadline must be positive")
        if turn_timeout <= turn_progress_after:
            raise ValueError("turn timeout must exceed progress deadline")
        self.client = client
        self.lease_server = lease_server
        self.push_text = push_text
        self.turn_progress_after = turn_progress_after
        self.turn_timeout = turn_timeout
        self.poll_interval = poll_interval
        self.approval_response_timeout = approval_response_timeout
        self.sessions = SessionRegistry()
        self.router = CommandRouter(self.sessions)
        self.approvals = ApprovalRegistry(self.sessions, ttl=approval_ttl)
        self._approval_lock = asyncio.Lock()
        self._wechat_turn_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._delivered_turns: set[tuple[str, str]] = set()
        self._closed = False

    async def handle_text(self, text: str) -> str | None:
        result = self.router.route(text)
        if result.reply is not None:
            return result.reply
        if result.send is not None:
            return await self._send_turn(
                result.send.session_number,
                result.send.thread_id,
                result.send.text,
            )
        if result.approve is not None:
            return await self._approve(result.approve.approval_code)
        return "消息未被处理。"

    async def handle_notification(self, notification: CodexNotification) -> None:
        params = notification.params
        if notification.method == "thread/started":
            thread = params.get("thread")
            if isinstance(thread, dict):
                self.lease_server.bind_thread(thread)
            return

        if notification.method == "serverRequest/resolved":
            request_id = params.get("requestId")
            if isinstance(request_id, (int, str)) and not isinstance(request_id, bool):
                async with self._approval_lock:
                    approval = self.approvals.invalidate_request(request_id)
                    pending = (
                        self.approvals.pending_for_thread(approval.thread_id)
                        if approval is not None
                        else ()
                    )
                if approval is not None:
                    status = (
                        SessionStatus.WAITING_APPROVAL
                        if pending
                        else SessionStatus.RUNNING
                    )
                    self.sessions.update_status(approval.thread_id, status)
            return

        thread_id = params.get("threadId")
        if not isinstance(thread_id, str) or not thread_id:
            return
        if notification.method == "turn/started":
            self.sessions.update_status(thread_id, SessionStatus.RUNNING)
            return
        if notification.method == "turn/completed":
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            turn_status = turn.get("status") if isinstance(turn, dict) else None
            if isinstance(turn_id, str):
                async with self._approval_lock:
                    self.approvals.invalidate_turn(thread_id, turn_id)
                if turn_status in ("failed", "interrupted"):
                    key = (thread_id, turn_id)
                    task = (
                        None
                        if key in self._delivered_turns
                        else self._wechat_turn_tasks.pop(key, None)
                    )
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        self._delivered_turns.add(key)
                        session = self.sessions.get_by_thread(thread_id)
                        if session is not None:
                            outcome = "失败" if turn_status == "failed" else "已中断"
                            await self._safe_push(
                                f"会话 {session.number} 的任务{outcome}，请在本地终端查看。",
                                event="turn_terminal_notice",
                            )
            async with self._approval_lock:
                pending = self.approvals.pending_for_thread(thread_id)
            if pending:
                self.sessions.update_status(thread_id, SessionStatus.WAITING_APPROVAL)
            else:
                self.sessions.update_status(thread_id, SessionStatus.IDLE)
            return
        if notification.method in ("item/started", "item/completed"):
            original = _extract_user_text(params.get("item"))
            if original is not None:
                self.sessions.record_message(thread_id, original)

    async def handle_server_request(self, request: CodexServerRequest) -> None:
        if request.method == USER_INPUT_REQUEST:
            thread_id = request.params.get("threadId")
            session = (
                self.sessions.get_by_thread(thread_id)
                if isinstance(thread_id, str)
                else None
            )
            if session is not None:
                await self._safe_push(
                    f"会话 {session.number} 正在等待 Codex 交互选择，"
                    "第一版无法通过微信回答，请回到本地终端处理。",
                    event="user_input_notice",
                )
            return
        try:
            async with self._approval_lock:
                registration = self.approvals.register(request)
        except ApprovalError:
            thread_id = request.params.get("threadId")
            if isinstance(thread_id, str) and self.sessions.get_by_thread(thread_id):
                await self._safe_push(
                    "Codex 发出了当前版本无法安全代理的审批请求，请回到本地终端处理。",
                    event="unsupported_approval_notice",
                )
            return
        if registration is None:
            return
        approval = registration.approval
        self.sessions.update_status(approval.thread_id, SessionStatus.WAITING_APPROVAL)
        if registration.created:
            await self._safe_push(
                approval.notification,
                event="approval_notice",
            )

    async def handle_lease_event(self, event: LeaseEvent) -> None:
        if isinstance(event, LeaseBound):
            self.sessions.add_lease(event.lease)
            return
        if not isinstance(event, LeaseClosed):
            return
        removed = self.sessions.remove_lease(event.lease.lease_id)
        if removed is None:
            return
        async with self._approval_lock:
            self.approvals.invalidate_thread(event.lease.thread_id)
        cancelled: list[asyncio.Task[None]] = []
        for key, task in tuple(self._wechat_turn_tasks.items()):
            if key[0] == event.lease.thread_id:
                self._wechat_turn_tasks.pop(key, None)
                task.cancel()
                cancelled.append(task)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        await self._safe_push(
            f"Codex 会话 {removed.number}（{removed.project_name}）已退出。",
            event="session_exit_notice",
        )

    async def expire_approvals(self) -> None:
        async with self._approval_lock:
            expired = self.approvals.prune()
        for approval in expired:
            await self._safe_push(
                f"审批 {approval.code} 已超时失效，请回到本地终端处理。",
                event="approval_expiry_notice",
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._wechat_turn_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._wechat_turn_tasks.clear()
        self._delivered_turns.clear()
        async with self._approval_lock:
            self.approvals.clear()
        self.sessions.clear()

    async def _send_turn(
        self,
        session_number: int,
        thread_id: str,
        text: str,
    ) -> str:
        session = self.sessions.get(session_number)
        if session is None or session.thread_id != thread_id:
            return "会话编号已失效，请重新发送 /sessions。"
        try:
            turn = await self.client.start_turn(thread_id, text)
        except CodexProtocolError:
            return "消息发送失败：Codex 连接不可用或拒绝了该消息。"
        except Exception:
            return "消息发送失败：Codex 返回了未预期错误。"

        key = (turn.thread_id, turn.turn_id)
        if key in self._wechat_turn_tasks or key in self._delivered_turns:
            return "消息发送失败：Codex 返回了重复的 turn ID。"
        self.sessions.record_message(thread_id, text)
        self.sessions.update_status(thread_id, SessionStatus.RUNNING)
        task = asyncio.create_task(
            self._wait_and_push(turn, session_number),
            name=f"wechat-claw-turn-{turn.turn_id}",
        )
        self._wechat_turn_tasks[key] = task
        return f"已发送到会话 {session_number}（{session.project_name}）。"

    async def _wait_and_push(self, turn: CodexTurn, session_number: int) -> None:
        key = (turn.thread_id, turn.turn_id)
        wait_task: asyncio.Task[str] | None = None
        try:
            wait_task = asyncio.create_task(
                self.client.wait_for_final_response(
                    turn,
                    timeout=self.turn_timeout,
                    poll_interval=self.poll_interval,
                )
            )
            done, _ = await asyncio.wait(
                (wait_task,),
                timeout=self.turn_progress_after,
            )
            if not done:
                await self._safe_push(
                    f"会话 {session_number} 仍在处理中，完成后会继续推送结果。",
                    event="turn_progress_notice",
                )
            final = await wait_task
            if key in self._delivered_turns:
                return
            sent = await self._safe_push(final, event="turn_final_reply")
            if sent:
                self._delivered_turns.add(key)
        except asyncio.CancelledError:
            raise
        except CodexTurnTimeout:
            self._delivered_turns.add(key)
            interrupted = False
            try:
                await self.client.interrupt_turn(turn)
                interrupted = True
            except Exception:
                pass
            detail = (
                "已中断该任务。"
                if interrupted
                else "已停止等待，但未能确认中断，请在本地终端查看。"
            )
            await self._safe_push(
                f"会话 {session_number} 等待超过 10 分钟，{detail}",
                event="turn_timeout_notice",
            )
        except Exception:
            await self._safe_push(
                f"会话 {session_number} 的回复获取失败，请在本地终端查看。",
                event="turn_failure_notice",
            )
        finally:
            if wait_task is not None and not wait_task.done():
                wait_task.cancel()
                await asyncio.gather(wait_task, return_exceptions=True)
            self._wechat_turn_tasks.pop(key, None)
            if self.sessions.get_by_thread(turn.thread_id) is not None:
                async with self._approval_lock:
                    pending = self.approvals.pending_for_thread(turn.thread_id)
                status = (
                    SessionStatus.WAITING_APPROVAL if pending else SessionStatus.IDLE
                )
                self.sessions.update_status(turn.thread_id, status)

    async def _safe_push(self, text: str, *, event: str) -> bool:
        try:
            sent = await self.push_text(text)
        except Exception:
            print(
                f"[wechat-claw] wechat push failed: event={event}",
                flush=True,
            )
            return False
        if not sent:
            print(
                f"[wechat-claw] wechat push unavailable: event={event}",
                flush=True,
            )
            return False
        return True

    async def _approve(self, code: str | None) -> str:
        async with self._approval_lock:
            focus = self.sessions.focus
            try:
                approval = self.approvals.resolve(
                    code,
                    focus_thread_id=focus.thread_id if focus is not None else None,
                )
            except ApprovalError as exc:
                return str(exc)

            try:
                await asyncio.wait_for(
                    self.client.respond(approval.request_id, result=approval.response),
                    timeout=self.approval_response_timeout,
                )
            except Exception:
                return "审批响应发送失败，请求仍保持未批准。"
            if not self.approvals.complete(approval):
                return "审批请求已失效，未重复批准。"
            pending = self.approvals.pending_for_thread(approval.thread_id)
        status = (
            SessionStatus.WAITING_APPROVAL if pending else SessionStatus.RUNNING
        )
        self.sessions.update_status(approval.thread_id, status)
        return f"已批准审批 {approval.code}，仅对本次请求生效。"


def _extract_user_text(item: Any) -> str | None:
    if not isinstance(item, dict) or item.get("type") != "userMessage":
        return None
    content = item.get("content")
    if not isinstance(content, list):
        return None
    text_parts = [
        entry.get("text")
        for entry in content
        if isinstance(entry, dict)
        and entry.get("type") == "text"
        and isinstance(entry.get("text"), str)
    ]
    return "\n".join(text_parts) if text_parts else None
