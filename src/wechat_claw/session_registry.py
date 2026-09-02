"""In-memory active Codex session registry."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from .lease_server import ActiveLease, TmuxLocation


class SessionStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"


@dataclass(frozen=True, slots=True)
class ActiveSession:
    number: int
    lease_id: int
    thread_id: str
    cwd: str
    tmux: TmuxLocation
    status: SessionStatus
    last_activity: datetime
    latest_message: str | None = None

    @property
    def project_name(self) -> str:
        return Path(self.cwd).name or self.cwd


class SessionRegistry:
    def __init__(self) -> None:
        self._next_number = 1
        self._by_number: dict[int, ActiveSession] = {}
        self._number_by_lease: dict[int, int] = {}
        self._number_by_thread: dict[str, int] = {}
        self._lease_by_id: dict[int, ActiveLease] = {}
        self._focus_number: int | None = None

    @property
    def focus(self) -> ActiveSession | None:
        if self._focus_number is None:
            return None
        return self._by_number.get(self._focus_number)

    def add_lease(self, lease: ActiveLease) -> ActiveSession:
        existing_number = self._number_by_lease.get(lease.lease_id)
        if existing_number is not None:
            return self._by_number[existing_number]
        thread_number = self._number_by_thread.get(lease.thread_id)
        if thread_number is not None:
            self._number_by_lease[lease.lease_id] = thread_number
            self._lease_by_id[lease.lease_id] = lease
            return self._by_number[thread_number]
        session = ActiveSession(
            number=self._next_number,
            lease_id=lease.lease_id,
            thread_id=lease.thread_id,
            cwd=lease.registration.cwd,
            tmux=lease.registration.tmux,
            status=SessionStatus.IDLE,
            last_activity=_now(),
        )
        self._next_number += 1
        self._by_number[session.number] = session
        self._number_by_lease[session.lease_id] = session.number
        self._lease_by_id[session.lease_id] = lease
        self._number_by_thread[session.thread_id] = session.number
        return session

    def remove_lease(self, lease_id: int) -> ActiveSession | None:
        number = self._number_by_lease.pop(lease_id, None)
        if number is None:
            return None
        self._lease_by_id.pop(lease_id, None)
        remaining_lease_ids = tuple(
            active_lease_id
            for active_lease_id, active_number in self._number_by_lease.items()
            if active_number == number
        )
        if remaining_lease_ids:
            replacement_lease = self._lease_by_id[min(remaining_lease_ids)]
            session = self._by_number[number]
            self._by_number[number] = replace(
                session,
                lease_id=replacement_lease.lease_id,
                cwd=replacement_lease.registration.cwd,
                tmux=replacement_lease.registration.tmux,
                last_activity=_now(),
            )
            return None
        session = self._by_number.pop(number)
        self._number_by_thread.pop(session.thread_id, None)
        if self._focus_number == number:
            self._focus_number = None
        if not self._by_number:
            self._next_number = 1
        return session

    def get(self, number: int) -> ActiveSession | None:
        return self._by_number.get(number)

    def get_by_thread(self, thread_id: str) -> ActiveSession | None:
        number = self._number_by_thread.get(thread_id)
        return self._by_number.get(number) if number is not None else None

    def list(self, keyword: str | None = None) -> tuple[ActiveSession, ...]:
        sessions = tuple(self._by_number[number] for number in sorted(self._by_number))
        if not keyword:
            return sessions
        needle = keyword.casefold()
        return tuple(
            session
            for session in sessions
            if needle
            in " ".join(
                value
                for value in (
                    session.project_name,
                    session.cwd,
                    session.tmux.session,
                    session.tmux.window,
                    session.tmux.pane,
                    session.latest_message,
                )
                if value
            ).casefold()
        )

    def set_focus(self, number: int) -> ActiveSession | None:
        session = self._by_number.get(number)
        if session is not None:
            self._focus_number = number
        return session

    def clear_focus(self) -> ActiveSession | None:
        previous = self.focus
        self._focus_number = None
        return previous

    def update_status(
        self,
        thread_id: str,
        status: SessionStatus,
        *,
        at: datetime | None = None,
    ) -> ActiveSession | None:
        return self._replace_thread(
            thread_id,
            status=status,
            last_activity=at or _now(),
        )

    def record_message(
        self,
        thread_id: str,
        text: str,
        *,
        at: datetime | None = None,
    ) -> ActiveSession | None:
        return self._replace_thread(
            thread_id,
            latest_message=text,
            last_activity=at or _now(),
        )

    def clear(self) -> None:
        self._by_number.clear()
        self._number_by_lease.clear()
        self._number_by_thread.clear()
        self._lease_by_id.clear()
        self._focus_number = None
        self._next_number = 1

    def _replace_thread(self, thread_id: str, **changes: object) -> ActiveSession | None:
        session = self.get_by_thread(thread_id)
        if session is None:
            return None
        updated = replace(session, **changes)
        self._by_number[updated.number] = updated
        return updated


def _now() -> datetime:
    return datetime.now(timezone.utc)
