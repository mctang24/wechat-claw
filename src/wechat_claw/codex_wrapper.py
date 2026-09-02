"""Transparent launcher used by the user's existing codex shell wrapper."""

from __future__ import annotations

import argparse
import json
import os
import queue
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .paths import DAEMON_LOG_PATH, PROJECT_ROOT


NON_INTERACTIVE_COMMANDS = {
    "agents",
    "exec",
    "e",
    "review",
    "login",
    "logout",
    "mcp",
    "plugin",
    "mcp-server",
    "app-server",
    "remote-control",
    "app",
    "completion",
    "update",
    "doctor",
    "sandbox",
    "debug",
    "apply",
    "a",
    "queue",
    "archive",
    "delete",
    "migrate-rollouts",
    "unarchive",
    "cloud",
    "exec-server",
    "features",
    "help",
}
INTERACTIVE_COMMANDS = {"resume", "fork"}
VALUE_OPTIONS = {
    "-c",
    "--config",
    "--enable",
    "--disable",
    "--remote",
    "--remote-auth-token-env",
    "-i",
    "--image",
    "-m",
    "--model",
    "--local-provider",
    "-p",
    "--profile",
    "-s",
    "--sandbox",
    "-C",
    "--cd",
    "--add-dir",
    "-a",
    "--ask-for-approval",
}
MAX_CONSECUTIVE_RECONNECTS = 3
STABLE_CHILD_SECONDS = 30.0


class WrapperError(RuntimeError):
    """Raised for invalid local wrapper coordination."""


def is_interactive_invocation(arguments: Sequence[str]) -> bool:
    if any(argument in {"-h", "--help", "-V", "--version"} for argument in arguments):
        return False
    if _has_option(arguments, "--remote"):
        return False
    positional = _first_positional(arguments)
    if positional is None:
        return True
    if positional in INTERACTIVE_COMMANDS:
        return True
    if positional in NON_INTERACTIVE_COMMANDS:
        return False
    return True


def effective_cwd(arguments: Sequence[str], current_directory: str) -> str:
    value: str | None = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-C", "--cd"}:
            if index + 1 >= len(arguments):
                break
            value = arguments[index + 1]
            index += 2
            continue
        if argument.startswith("--cd="):
            value = argument.split("=", 1)[1]
        elif argument.startswith("-C") and argument != "-C":
            value = argument[2:]
        index += 1
    base = Path(current_directory)
    selected = Path(value).expanduser() if value else base
    if not selected.is_absolute():
        selected = base / selected
    return os.path.realpath(selected)


def build_managed_arguments(
    arguments: Sequence[str],
    *,
    remote_endpoint: str,
    cwd: str,
) -> list[str]:
    managed = ["--remote", remote_endpoint]
    if not _has_cd_option(arguments):
        managed.extend(["-C", cwd])
    managed.extend(arguments)
    return managed


def tmux_location(environment: Mapping[str, str]) -> dict[str, str | None]:
    pane = environment.get("TMUX_PANE") or None
    if pane is None:
        return {"session": None, "window": None, "pane": None}
    return {
        "session": _tmux_format(pane, "#{session_name}"),
        "window": _tmux_format(pane, "#{window_name}"),
        "pane": pane,
    }


def run_managed(
    real_codex: str,
    socket_path: Path,
    arguments: Sequence[str],
) -> int:
    cwd = effective_cwd(arguments, os.getcwd())
    try:
        lease_socket = _connect_or_start_daemon(socket_path)
        _send_registration(lease_socket, cwd, tmux_location(os.environ))
        launch = _read_message(lease_socket)
        if launch.get("type") != "launch" or not isinstance(launch.get("remote"), str):
            raise WrapperError("lease server did not return a launch endpoint")
    except (OSError, TimeoutError, WrapperError, ValueError) as exc:
        print(
            f"codex wrapper: WeChat bridge unavailable ({exc}); starting unmanaged Codex",
            file=sys.stderr,
        )
        return _run_unmanaged(real_codex, arguments)

    child = subprocess.Popen(
        [
            real_codex,
            *build_managed_arguments(
                arguments,
                remote_endpoint=launch["remote"],
                cwd=cwd,
            ),
        ]
    )
    binding_results: queue.Queue[str | None] = queue.Queue(maxsize=1)
    binding_thread = threading.Thread(
        target=_report_binding_result,
        args=(lease_socket, binding_results),
        daemon=True,
        name="wechat-claw-binding-result",
    )
    binding_thread.start()
    previous_handlers = _ignore_terminal_signals()
    try:
        return _wait_for_child_with_reconnect(
            child,
            real_codex=real_codex,
            remote_endpoint=launch["remote"],
            cwd=cwd,
            lease_socket=lease_socket,
            binding_results=binding_results,
            previous_handlers=previous_handlers,
        )
    finally:
        lease_socket.close()
        _restore_terminal_signals(previous_handlers)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--real-codex", required=True)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    namespace = parser.parse_args(argv)
    arguments = namespace.arguments
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if not is_interactive_invocation(arguments):
        return _run_unmanaged(namespace.real_codex, arguments)
    return run_managed(namespace.real_codex, namespace.socket, arguments)


def _run_unmanaged(real_codex: str, arguments: Sequence[str]) -> int:
    try:
        os.execvpe(real_codex, [real_codex, *arguments], os.environ)
    except OSError as exc:
        print(f"codex wrapper: failed to start real Codex: {exc}", file=sys.stderr)
        return 127
    raise AssertionError("os.execvpe returned unexpectedly")


def _connect_or_start_daemon(
    socket_path: Path,
    *,
    startup_timeout: float = 8.0,
) -> socket.socket:
    try:
        return _connect_socket(socket_path)
    except OSError:
        pass

    DAEMON_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DAEMON_LOG_PATH.open("a", encoding="utf-8") as log:
        DAEMON_LOG_PATH.chmod(0o600)
        process = subprocess.Popen(
            [sys.executable, "-m", "wechat_claw.cli", "daemon"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    deadline = time.monotonic() + startup_timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            return _connect_socket(socket_path)
        except OSError as exc:
            last_error = exc
        if process.poll() is not None:
            break
        time.sleep(0.05)
    raise WrapperError(
        "wechat-claw daemon did not become ready; "
        f"see {DAEMON_LOG_PATH}"
    ) from last_error


def _connect_socket(socket_path: Path) -> socket.socket:
    lease_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    lease_socket.settimeout(2)
    try:
        lease_socket.connect(str(socket_path))
    except BaseException:
        lease_socket.close()
        raise
    return lease_socket


def _send_registration(
    lease_socket: socket.socket,
    cwd: str,
    tmux: Mapping[str, str | None],
) -> None:
    payload = {"type": "register", "cwd": cwd, "tmux": dict(tmux)}
    lease_socket.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")


def _read_message(lease_socket: socket.socket) -> dict[str, Any]:
    buffer = bytearray()
    while len(buffer) <= 65_536:
        chunk = lease_socket.recv(4096)
        if not chunk:
            raise WrapperError("lease server closed the connection")
        buffer.extend(chunk)
        newline = buffer.find(b"\n")
        if newline >= 0:
            try:
                payload = json.loads(buffer[:newline])
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise WrapperError("lease server returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise WrapperError("lease server response must be an object")
            return payload
    raise WrapperError("lease server response is too large")


def _report_binding_result(
    lease_socket: socket.socket,
    binding_results: queue.Queue[str | None] | None = None,
) -> None:
    thread_id: str | None = None
    try:
        lease_socket.settimeout(20)
        result = _read_message(lease_socket)
        if result.get("type") == "bound":
            candidate = result.get("threadId")
            if not isinstance(candidate, str) or not candidate:
                raise WrapperError("lease server returned an invalid thread id")
            thread_id = candidate
            lease_socket.settimeout(None)
            return
        code = result.get("code", "unknown")
        print(
            f"codex wrapper: WeChat bridge did not register this session ({code})",
            file=sys.stderr,
        )
    except (OSError, TimeoutError, WrapperError, ValueError) as exc:
        print(
            f"codex wrapper: WeChat bridge registration failed ({exc})",
            file=sys.stderr,
        )
    finally:
        if binding_results is not None:
            try:
                binding_results.put_nowait(thread_id)
            except queue.Full:
                pass


def _wait_for_child_with_reconnect(
    child: subprocess.Popen[Any],
    *,
    real_codex: str,
    remote_endpoint: str,
    cwd: str,
    lease_socket: socket.socket,
    binding_results: queue.Queue[str | None],
    previous_handlers: Mapping[int, Any],
) -> int:
    thread_id: str | None = None
    consecutive_failures = 0

    while True:
        started_at = time.monotonic()
        return_code = child.wait()
        runtime = time.monotonic() - started_at
        if return_code == 0:
            return 0

        if thread_id is None:
            try:
                thread_id = binding_results.get(timeout=1)
            except queue.Empty:
                return return_code
        if thread_id is None or not _lease_is_open(lease_socket):
            return return_code

        if runtime >= STABLE_CHILD_SECONDS:
            consecutive_failures = 0
        if consecutive_failures >= MAX_CONSECUTIVE_RECONNECTS:
            return return_code
        consecutive_failures += 1
        print(
            "codex wrapper: remote TUI disconnected; reconnecting session "
            f"(attempt {consecutive_failures}/{MAX_CONSECUTIVE_RECONNECTS})",
            file=sys.stderr,
        )
        time.sleep(0.5)
        try:
            child = _spawn_reconnected_child(
                real_codex,
                remote_endpoint,
                cwd,
                thread_id,
                previous_handlers,
            )
        except OSError as exc:
            print(
                f"codex wrapper: failed to reconnect remote TUI ({exc})",
                file=sys.stderr,
            )
            return return_code


def _spawn_reconnected_child(
    real_codex: str,
    remote_endpoint: str,
    cwd: str,
    thread_id: str,
    previous_handlers: Mapping[int, Any],
) -> subprocess.Popen[Any]:
    _restore_terminal_signals(previous_handlers)
    try:
        return subprocess.Popen(
            [
                real_codex,
                "--remote",
                remote_endpoint,
                "-C",
                cwd,
                "resume",
                thread_id,
            ]
        )
    finally:
        _ignore_terminal_signals()


def _lease_is_open(lease_socket: socket.socket) -> bool:
    try:
        readable, _, _ = select.select([lease_socket], [], [], 0)
        if not readable:
            return True
        return bool(lease_socket.recv(1, socket.MSG_PEEK))
    except (OSError, ValueError):
        return False


def _first_positional(arguments: Sequence[str]) -> str | None:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return arguments[index + 1] if index + 1 < len(arguments) else None
        if argument in VALUE_OPTIONS:
            index += 2
            continue
        if argument.startswith("--") and "=" in argument:
            index += 1
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return argument
    return None


def _has_option(arguments: Sequence[str], name: str) -> bool:
    return any(argument == name or argument.startswith(f"{name}=") for argument in arguments)


def _has_cd_option(arguments: Sequence[str]) -> bool:
    return any(
        argument in {"-C", "--cd"}
        or argument.startswith("--cd=")
        or (argument.startswith("-C") and argument != "-C")
        for argument in arguments
    )


def _tmux_format(pane: str, template: str) -> str | None:
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, template],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _ignore_terminal_signals() -> dict[int, Any]:
    previous: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGQUIT):
        previous[signum] = signal.signal(signum, signal.SIG_IGN)
    return previous


def _restore_terminal_signals(previous: Mapping[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)
