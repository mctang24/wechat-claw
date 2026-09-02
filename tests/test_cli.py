from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import unittest

from wechat_claw import __version__
from wechat_claw.cli import main


class CliTest(unittest.TestCase):
    def test_python_module_entry_runs_cli(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "wechat_claw.cli", "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("Bridge active Codex CLI sessions", result.stdout)

    def test_help_exits_successfully(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("Bridge active Codex CLI sessions", output.getvalue())

    def test_version_exits_successfully(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"wechat-claw {__version__}")


if __name__ == "__main__":
    unittest.main()
