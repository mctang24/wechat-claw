from __future__ import annotations

import unittest

from wechat_claw.lease_server import ActiveLease, LeaseRegistration, TmuxLocation
from wechat_claw.session_registry import SessionRegistry, SessionStatus


def lease(lease_id: int, thread_id: str, cwd: str) -> ActiveLease:
    return ActiveLease(
        lease_id=lease_id,
        thread_id=thread_id,
        registration=LeaseRegistration(
            cwd=cwd,
            tmux=TmuxLocation("session", "window", f"%{lease_id}"),
        ),
    )


class SessionRegistryTest(unittest.TestCase):
    def test_same_thread_stays_active_until_last_lease_closes(self) -> None:
        registry = SessionRegistry()
        first = lease(1, "thread-shared", "/tmp/project-a")
        second = ActiveLease(
            lease_id=2,
            thread_id="thread-shared",
            registration=LeaseRegistration(
                cwd="/tmp/project-a",
                tmux=TmuxLocation("second", "editor", "%2"),
            ),
        )

        first_session = registry.add_lease(first)
        second_session = registry.add_lease(second)
        registry.set_focus(first_session.number)

        self.assertEqual(first_session.number, second_session.number)
        self.assertEqual(len(registry.list()), 1)
        self.assertIsNone(registry.remove_lease(first.lease_id))
        remaining = registry.get(first_session.number)
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining.lease_id, second.lease_id)
        self.assertEqual(remaining.tmux.session, "second")
        self.assertEqual(registry.focus.number, first_session.number)

        removed = registry.remove_lease(second.lease_id)
        self.assertEqual(removed.thread_id, "thread-shared")
        self.assertEqual(registry.list(), ())
        self.assertIsNone(registry.focus)

    def test_numbers_stay_stable_while_another_session_is_active(self) -> None:
        registry = SessionRegistry()
        first = registry.add_lease(lease(10, "thread-1", "/tmp/project-a"))
        second = registry.add_lease(lease(11, "thread-2", "/tmp/project-b"))

        self.assertEqual((first.number, second.number), (1, 2))
        self.assertEqual(registry.add_lease(lease(10, "thread-1", "/tmp/project-a")), first)
        self.assertEqual(registry.remove_lease(10), first)
        third = registry.add_lease(lease(12, "thread-3", "/tmp/project-c"))
        self.assertEqual(third.number, 3)
        self.assertIsNone(registry.get(1))
        self.assertEqual(registry.get(2), second)

    def test_numbering_restarts_after_all_sessions_exit(self) -> None:
        registry = SessionRegistry()
        first = registry.add_lease(lease(10, "thread-1", "/tmp/project-a"))

        self.assertEqual(registry.remove_lease(first.lease_id), first)
        fresh = registry.add_lease(lease(11, "thread-2", "/tmp/project-b"))

        self.assertEqual(fresh.number, 1)

    def test_exit_invalidates_focus(self) -> None:
        registry = SessionRegistry()
        session = registry.add_lease(lease(10, "thread-1", "/tmp/project"))
        registry.set_focus(session.number)
        self.assertEqual(registry.focus, session)

        registry.remove_lease(10)

        self.assertIsNone(registry.focus)

    def test_status_message_and_ordered_listing(self) -> None:
        registry = SessionRegistry()
        registry.add_lease(lease(10, "thread-1", "/tmp/Alpha"))
        registry.add_lease(lease(11, "thread-2", "/tmp/Beta"))
        updated = registry.update_status("thread-1", SessionStatus.RUNNING)
        registry.record_message("thread-1", "原文 Needle")

        self.assertEqual(updated.status, SessionStatus.RUNNING)
        self.assertEqual(
            [item.thread_id for item in registry.list()],
            ["thread-1", "thread-2"],
        )
        self.assertEqual(registry.get_by_thread("thread-1").latest_message, "原文 Needle")

    def test_clear_resets_runtime_state_and_numbering(self) -> None:
        registry = SessionRegistry()
        registry.add_lease(lease(10, "thread-1", "/tmp/project"))
        registry.set_focus(1)

        registry.clear()
        fresh = registry.add_lease(lease(11, "thread-2", "/tmp/project"))

        self.assertEqual(fresh.number, 1)
        self.assertIsNone(registry.focus)


if __name__ == "__main__":
    unittest.main()
