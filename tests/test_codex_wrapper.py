from __future__ import annotations

import os
import queue
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from wechat_claw.codex_wrapper import (
    build_managed_arguments,
    effective_cwd,
    is_interactive_invocation,
    tmux_location,
    _connect_or_start_daemon,
    _wait_for_child_with_reconnect,
)


class CodexWrapperTest(unittest.TestCase):
    def test_normal_exit_never_reconnects(self) -> None:
        child = MagicMock()
        child.wait.return_value = 0
        binding_results: queue.Queue[str | None] = queue.Queue()
        binding_results.put("thread-1")

        with patch(
            "wechat_claw.codex_wrapper._spawn_reconnected_child"
        ) as reconnect:
            result = _wait_for_child_with_reconnect(
                child,
                real_codex="/opt/homebrew/bin/codex",
                remote_endpoint="ws://127.0.0.1:48731",
                cwd="/tmp/project",
                lease_socket=MagicMock(spec=socket.socket),
                binding_results=binding_results,
                previous_handlers={},
            )

        self.assertEqual(result, 0)
        reconnect.assert_not_called()

    def test_bound_nonzero_exit_resumes_exact_thread(self) -> None:
        failed = MagicMock()
        failed.wait.return_value = 1
        recovered = MagicMock()
        recovered.wait.return_value = 0
        binding_results: queue.Queue[str | None] = queue.Queue()
        binding_results.put("thread-1")
        lease_socket = MagicMock(spec=socket.socket)

        with patch(
            "wechat_claw.codex_wrapper._lease_is_open",
            return_value=True,
        ), patch(
            "wechat_claw.codex_wrapper._spawn_reconnected_child",
            return_value=recovered,
        ) as reconnect, patch("wechat_claw.codex_wrapper.time.sleep"):
            result = _wait_for_child_with_reconnect(
                failed,
                real_codex="/opt/homebrew/bin/codex",
                remote_endpoint="ws://127.0.0.1:48731",
                cwd="/tmp/project",
                lease_socket=lease_socket,
                binding_results=binding_results,
                previous_handlers={},
            )

        self.assertEqual(result, 0)
        reconnect.assert_called_once_with(
            "/opt/homebrew/bin/codex",
            "ws://127.0.0.1:48731",
            "/tmp/project",
            "thread-1",
            {},
        )

    def test_consecutive_fast_reconnect_failures_stop(self) -> None:
        children = []
        for _ in range(4):
            child = MagicMock()
            child.wait.return_value = 1
            children.append(child)
        binding_results: queue.Queue[str | None] = queue.Queue()
        binding_results.put("thread-1")

        with patch(
            "wechat_claw.codex_wrapper._lease_is_open",
            return_value=True,
        ), patch(
            "wechat_claw.codex_wrapper._spawn_reconnected_child",
            side_effect=children[1:],
        ) as reconnect, patch(
            "wechat_claw.codex_wrapper.time.monotonic",
            side_effect=range(8),
        ), patch("wechat_claw.codex_wrapper.time.sleep"):
            result = _wait_for_child_with_reconnect(
                children[0],
                real_codex="/opt/homebrew/bin/codex",
                remote_endpoint="ws://127.0.0.1:48731",
                cwd="/tmp/project",
                lease_socket=MagicMock(spec=socket.socket),
                binding_results=binding_results,
                previous_handlers={},
            )

        self.assertEqual(result, 1)
        self.assertEqual(reconnect.call_count, 3)

    def test_closed_lease_prevents_reconnect(self) -> None:
        child = MagicMock()
        child.wait.return_value = 1
        binding_results: queue.Queue[str | None] = queue.Queue()
        binding_results.put("thread-1")

        with patch(
            "wechat_claw.codex_wrapper._lease_is_open",
            return_value=False,
        ), patch(
            "wechat_claw.codex_wrapper._spawn_reconnected_child"
        ) as reconnect:
            result = _wait_for_child_with_reconnect(
                child,
                real_codex="/opt/homebrew/bin/codex",
                remote_endpoint="ws://127.0.0.1:48731",
                cwd="/tmp/project",
                lease_socket=MagicMock(spec=socket.socket),
                binding_results=binding_results,
                previous_handlers={},
            )

        self.assertEqual(result, 1)
        reconnect.assert_not_called()

    def test_interactive_invocations(self) -> None:
        cases = [
            [],
            ["写一个测试"],
            ["-m", "gpt-5.6-sol", "写一个测试"],
            ["resume", "thread-id"],
            ["-C", "/tmp/project", "fork", "--last"],
        ]

        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertTrue(is_interactive_invocation(arguments))

    def test_non_interactive_invocations(self) -> None:
        cases = [
            ["--help"],
            ["--version"],
            ["exec", "echo", "ok"],
            ["review"],
            ["app-server", "--help"],
            ["delete", "thread-id"],
            ["--remote", "ws://127.0.0.1:1"],
        ]

        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertFalse(is_interactive_invocation(arguments))

    def test_effective_cwd_uses_last_explicit_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child = os.path.join(directory, "child")
            os.mkdir(child)
            self.assertEqual(effective_cwd([], directory), os.path.realpath(directory))
            self.assertEqual(
                effective_cwd(["-C", "/tmp", "--cd", "child"], directory),
                os.path.realpath(child),
            )

    def test_managed_arguments_preserve_original_values(self) -> None:
        original = ["resume", "thread-id", "--no-alt-screen"]
        self.assertEqual(
            build_managed_arguments(
                original,
                remote_endpoint="ws://127.0.0.1:48731",
                cwd="/tmp/project",
            ),
            [
                "--remote",
                "ws://127.0.0.1:48731",
                "-C",
                "/tmp/project",
                *original,
            ],
        )
        with_cwd = ["-C", "/tmp/custom", "resume", "thread-id"]
        self.assertEqual(
            build_managed_arguments(
                with_cwd,
                remote_endpoint="ws://127.0.0.1:48731",
                cwd="/tmp/custom",
            ),
            ["--remote", "ws://127.0.0.1:48731", *with_cwd],
        )

    def test_tmux_location_is_empty_outside_tmux(self) -> None:
        self.assertEqual(
            tmux_location({}),
            {"session": None, "window": None, "pane": None},
        )

    def test_existing_daemon_is_used_without_starting_another(self) -> None:
        connected = MagicMock(spec=socket.socket)
        with patch(
            "wechat_claw.codex_wrapper._connect_socket",
            return_value=connected,
        ), patch("wechat_claw.codex_wrapper.subprocess.Popen") as popen:
            result = _connect_or_start_daemon(Path("/tmp/existing.sock"))
        self.assertIs(result, connected)
        popen.assert_not_called()

    def test_missing_daemon_is_started_then_connected(self) -> None:
        connected = MagicMock(spec=socket.socket)
        process = MagicMock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory, patch(
            "wechat_claw.codex_wrapper.DAEMON_LOG_PATH",
            Path(directory) / "daemon.log",
        ), patch(
            "wechat_claw.codex_wrapper._connect_socket",
            side_effect=[FileNotFoundError(), connected],
        ), patch(
            "wechat_claw.codex_wrapper.subprocess.Popen",
            return_value=process,
        ) as popen:
            result = _connect_or_start_daemon(
                Path(directory) / "lease.sock",
                startup_timeout=0.1,
            )
        self.assertIs(result, connected)
        popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
