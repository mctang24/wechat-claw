"""Command-line entry point for WeChat Claw."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wechat-claw",
        description="Bridge active Codex CLI sessions to a bound WeChat account.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("daemon", help="Run the local WeChat and Codex bridge.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    namespace = build_parser().parse_args(argv)
    if namespace.command == "daemon":
        from .daemon import run_daemon

        try:
            asyncio.run(run_daemon())
        except KeyboardInterrupt:
            return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
