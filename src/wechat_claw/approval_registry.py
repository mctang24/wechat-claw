"""In-memory routing for genuine Codex app-server approval requests."""

from __future__ import annotations

import json
import re
import secrets
import string
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .codex_protocol import CodexServerRequest, RequestId
from .redaction import redact_sensitive_text
from .session_registry import ActiveSession, SessionRegistry


COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"
PERMISSIONS_APPROVAL = "item/permissions/requestApproval"
SUPPORTED_APPROVALS = frozenset(
    (COMMAND_APPROVAL, FILE_APPROVAL, PERMISSIONS_APPROVAL)
)


class ApprovalError(RuntimeError):
    """Raised when an approval cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class PendingApproval:
    code: str
    request_id: RequestId
    method: str
    thread_id: str
    turn_id: str
    item_id: str
    session_number: int
    response: dict[str, Any]
    expires_at: float
    notification: str


@dataclass(frozen=True, slots=True)
class ApprovalRegistration:
    approval: PendingApproval
    created: bool


class ApprovalRegistry:
    def __init__(
        self,
        sessions: SessionRegistry,
        *,
        ttl: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        code_factory: Callable[[], str] | None = None,
    ) -> None:
        if ttl <= 0:
            raise ValueError("approval ttl must be positive")
        self.sessions = sessions
        self.ttl = ttl
        self.clock = clock
        self.code_factory = code_factory or _random_code
        self._by_code: dict[str, PendingApproval] = {}
        self._code_by_request: dict[tuple[type, RequestId], str] = {}

    def register(self, request: CodexServerRequest) -> ApprovalRegistration | None:
        if request.method not in SUPPORTED_APPROVALS:
            return None
        params = request.params
        thread_id = _required_string(params, "threadId")
        turn_id = _required_string(params, "turnId")
        item_id = _required_string(params, "itemId")
        session = self.sessions.get_by_thread(thread_id)
        if session is None:
            return None
        self.prune()

        request_key = (type(request.id), request.id)
        existing_code = self._code_by_request.get(request_key)
        if existing_code is not None:
            existing = self._by_code.get(existing_code)
            if existing is not None:
                if (
                    existing.method != request.method
                    or existing.thread_id != thread_id
                    or existing.turn_id != turn_id
                    or existing.item_id != item_id
                ):
                    raise ApprovalError("审批 request ID 与既有请求冲突。")
                return ApprovalRegistration(existing, created=False)

        response = _approval_response(request.method, params)
        code = self._new_code()
        approval = PendingApproval(
            code=code,
            request_id=request.id,
            method=request.method,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            session_number=session.number,
            response=response,
            expires_at=self.clock() + self.ttl,
            notification=_format_notification(code, request.method, params, session),
        )
        self._by_code[code] = approval
        self._code_by_request[request_key] = code
        return ApprovalRegistration(approval, created=True)

    def resolve(
        self,
        code: str | None,
        *,
        focus_thread_id: str | None,
    ) -> PendingApproval:
        self.prune()
        if code:
            approval = self._by_code.get(code.upper())
            if approval is None:
                raise ApprovalError("审批码不存在或已失效。")
            if self.sessions.get_by_thread(approval.thread_id) is None:
                self.discard(approval.code)
                raise ApprovalError("审批所属会话已退出。")
            return approval

        if focus_thread_id is None:
            raise ApprovalError("未指定审批码，且当前没有 focus 会话。")
        candidates = tuple(
            approval
            for approval in self._by_code.values()
            if approval.thread_id == focus_thread_id
        )
        if not candidates:
            raise ApprovalError("当前 focus 会话没有待审批请求。")
        if len(candidates) != 1:
            raise ApprovalError("当前 focus 会话有多个待审批请求，请指定审批码。")
        return candidates[0]

    def complete(self, approval: PendingApproval) -> bool:
        current = self._by_code.get(approval.code)
        if current != approval:
            return False
        self.discard(approval.code)
        return True

    def discard(self, code: str) -> PendingApproval | None:
        approval = self._by_code.pop(code.upper(), None)
        if approval is not None:
            self._code_by_request.pop((type(approval.request_id), approval.request_id), None)
        return approval

    def invalidate_thread(self, thread_id: str) -> tuple[PendingApproval, ...]:
        removed = tuple(
            approval
            for approval in self._by_code.values()
            if approval.thread_id == thread_id
        )
        for approval in removed:
            self.discard(approval.code)
        return removed

    def invalidate_request(self, request_id: RequestId) -> PendingApproval | None:
        code = self._code_by_request.get((type(request_id), request_id))
        if code is None:
            return None
        return self.discard(code)

    def invalidate_turn(
        self,
        thread_id: str,
        turn_id: str,
    ) -> tuple[PendingApproval, ...]:
        removed = tuple(
            approval
            for approval in self._by_code.values()
            if approval.thread_id == thread_id and approval.turn_id == turn_id
        )
        for approval in removed:
            self.discard(approval.code)
        return removed

    def pending_for_thread(self, thread_id: str) -> tuple[PendingApproval, ...]:
        self.prune()
        return tuple(
            approval
            for approval in self._by_code.values()
            if approval.thread_id == thread_id
        )

    def clear(self) -> None:
        self._by_code.clear()
        self._code_by_request.clear()

    def prune(self) -> tuple[PendingApproval, ...]:
        now = self.clock()
        expired = tuple(
            approval
            for approval in self._by_code.values()
            if approval.expires_at <= now
        )
        for approval in expired:
            self.discard(approval.code)
        return expired

    def _new_code(self) -> str:
        for _ in range(100):
            code = self.code_factory().strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{4,12}", code):
                raise ApprovalError("审批码生成器返回了无效格式。")
            if code not in self._by_code:
                return code
        raise ApprovalError("无法生成唯一审批码。")


def _approval_response(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    if method in (COMMAND_APPROVAL, FILE_APPROVAL):
        available = params.get("availableDecisions")
        if available is not None:
            if not isinstance(available, list) or "accept" not in available:
                raise ApprovalError("该请求不允许单次批准。")
        return {"decision": "accept"}
    if method == PERMISSIONS_APPROVAL:
        permissions = params.get("permissions")
        if not isinstance(permissions, dict):
            raise ApprovalError("权限审批请求缺少 permissions。")
        return {"permissions": permissions, "scope": "turn"}
    raise ApprovalError("不支持的审批类型。")


def _format_notification(
    code: str,
    method: str,
    params: Mapping[str, Any],
    session: ActiveSession,
) -> str:
    type_label = {
        COMMAND_APPROVAL: "命令执行",
        FILE_APPROVAL: "文件修改",
        PERMISSIONS_APPROVAL: "额外权限",
    }[method]
    lines = [
        f"Codex 等待审批 {code}",
        f"会话：{session.number}（{session.project_name}）",
        f"类型：{type_label}",
    ]
    if method == COMMAND_APPROVAL:
        lines.append(f"命令：{_safe_text(params.get('command'))}")
    elif method == FILE_APPROVAL:
        lines.append(f"写入范围：{_safe_text(params.get('grantRoot') or session.cwd)}")
    else:
        lines.append(
            "权限："
            + _safe_text(json.dumps(params.get("permissions"), ensure_ascii=False))
        )
    reason = params.get("reason")
    if reason:
        lines.append(f"原因：{_safe_text(reason)}")
    lines.extend(("", f"批准：/approve {code}", "审批码 5 分钟后失效。"))
    return "\n".join(lines)


def _safe_text(value: Any, *, limit: int = 600) -> str:
    text = str(value if value is not None else "（未提供）")
    text = "".join(char if char in "\n\t" or ord(char) >= 32 else "�" for char in text)
    text = redact_sensitive_text(text)
    if len(text) > limit:
        return text[:limit] + "…（已截断）"
    return text


def _required_string(params: Mapping[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise ApprovalError(f"审批请求缺少 {name}。")
    return value


def _random_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))
