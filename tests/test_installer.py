from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = PROJECT_ROOT / "install.sh"
UNINSTALLER = PROJECT_ROOT / "uninstall.sh"


class InstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.bin_dir = self.home / ".local" / "bin"
        self.base_dir = self.home / ".local" / "share" / "wechat-claw"
        self.source = self.root / "source"
        self.tools = self.root / "tools"
        self.home.mkdir()
        self.bin_dir.mkdir(parents=True)
        self.source.mkdir()
        self.tools.mkdir()
        (self.source / "pyproject.toml").write_text(
            "[build-system]\nrequires=[]\nbuild-backend='stub'\n",
            encoding="utf-8",
        )
        self.fake_python = self.tools / "python3"
        self.fake_codex = self.tools / "codex-real"
        self.launcher_log = self.root / "launcher.log"
        self._write_executable(
            self.fake_codex,
            "#!/bin/sh\nexit 0\n",
        )
        daemon_source = f"""#!{sys.executable}
import os
import signal
import socket
import time
from pathlib import Path

data_dir = Path(os.environ["WECHAT_CLAW_DATA_DIR"])
socket_path = data_dir / "wechat-claw.sock"
qr_path = data_dir / "wechat_login_qr.html"
server = socket.socket(socket.AF_UNIX)
server.bind(str(socket_path))
qr_path.write_text("qr")
stopped = False
def stop(*_):
    global stopped
    stopped = True
signal.signal(signal.SIGTERM, stop)
while not stopped:
    time.sleep(0.05)
server.close()
socket_path.unlink(missing_ok=True)
"""
        daemon_literal = repr(daemon_source).replace("\\", "\\\\")
        self._write_executable(
            self.fake_python,
            f"""#!{sys.executable}
import os
import stat
import sys
from pathlib import Path

if sys.argv[1:2] == ["-c"]:
    raise SystemExit(0)
if sys.argv[1:3] != ["-m", "venv"]:
    raise SystemExit(2)
venv = Path(sys.argv[3])
bin_dir = venv / "bin"
bin_dir.mkdir(parents=True)
python = bin_dir / "python"
python.write_text('''#!{sys.executable}
import os
import stat
import sys
from pathlib import Path

if os.environ.get("FAIL_FAKE_PIP") == "1":
    raise SystemExit(9)
bin_dir = Path(__file__).parent
launcher = bin_dir / "wechat-claw-codex"
launcher.write_text("#!/bin/sh" + chr(10) + "printf '%s ' $@ > $FAKE_LAUNCHER_LOG" + chr(10))
launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
daemon = bin_dir / "wechat-claw"
daemon.write_text({daemon_literal})
daemon.chmod(daemon.stat().st_mode | stat.S_IXUSR)
''')
python.chmod(python.stat().st_mode | stat.S_IXUSR)
""",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_install_repeat_wrapper_forwarding_and_uninstall(self) -> None:
        original = self.bin_dir / "codex"
        self._write_executable(original, "#!/bin/sh\necho original\n")
        environment = self._environment()
        environment.pop("WECHAT_CLAW_REAL_CODEX")
        environment["PATH"] = f"{self.bin_dir}:{self.tools}:{environment['PATH']}"

        self._run(INSTALLER, environment)
        self._run(INSTALLER, environment)

        wrapper = self.bin_dir / "codex"
        backup = self.bin_dir / "codex.wechat-claw-backup"
        zshrc = self.home / ".zshrc"
        self.assertIn("wechat-claw managed wrapper", wrapper.read_text())
        self.assertEqual(backup.read_text(), "#!/bin/sh\necho original\n")
        self.assertEqual(zshrc.read_text().count("# >>> wechat-claw >>>"), 1)
        self.assertTrue((self.base_dir / "data").is_dir())
        self.assertTrue((self.base_dir / "logs").is_dir())

        subprocess.run(
            [wrapper, "--version"],
            check=True,
            env=environment,
            capture_output=True,
            text=True,
        )
        forwarded = self.launcher_log.read_text()
        self.assertIn(str(backup), forwarded)
        self.assertIn("--version", forwarded)

        self._run(UNINSTALLER, environment)
        self.assertEqual(wrapper.read_text(), "#!/bin/sh\necho original\n")
        self.assertFalse((self.base_dir / "app").exists())
        self.assertFalse((self.base_dir / "venv").exists())
        self.assertTrue((self.base_dir / "data").is_dir())
        self.assertTrue((self.base_dir / "logs").is_dir())
        self.assertNotIn("wechat-claw", zshrc.read_text())

    def test_failed_install_restores_existing_files(self) -> None:
        app = self.base_dir / "app"
        venv = self.base_dir / "venv"
        app.mkdir(parents=True)
        venv.mkdir()
        (app / "old.txt").write_text("old app", encoding="utf-8")
        (venv / "old.txt").write_text("old venv", encoding="utf-8")
        wrapper = self.bin_dir / "codex"
        self._write_executable(wrapper, "#!/bin/sh\necho original\n")
        zshrc = self.home / ".zshrc"
        zshrc.write_text("export ORIGINAL=1\n", encoding="utf-8")
        real_codex_file = self.base_dir / "real-codex-path"
        real_codex_file.write_text("/old/codex\n", encoding="utf-8")
        environment = self._environment()
        environment["FAIL_FAKE_PIP"] = "1"

        result = self._run(INSTALLER, environment, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((app / "old.txt").read_text(), "old app")
        self.assertEqual((venv / "old.txt").read_text(), "old venv")
        self.assertEqual(wrapper.read_text(), "#!/bin/sh\necho original\n")
        self.assertEqual(zshrc.read_text(), "export ORIGINAL=1\n")
        self.assertEqual(real_codex_file.read_text(), "/old/codex\n")
        self.assertFalse((self.bin_dir / "codex.wechat-claw-backup").exists())

    def test_failed_first_install_does_not_leave_zshrc_or_codex_path(self) -> None:
        environment = self._environment()
        environment["FAIL_FAKE_PIP"] = "1"

        result = self._run(INSTALLER, environment, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".zshrc").exists())
        self.assertFalse((self.base_dir / "real-codex-path").exists())

    def test_default_install_starts_daemon_and_uninstall_stops_it(self) -> None:
        environment = self._environment()
        environment.pop("WECHAT_CLAW_SKIP_DAEMON")

        self._run(INSTALLER, environment)

        self.assertTrue((self.base_dir / "data" / "wechat-claw.sock").is_socket())
        self.assertTrue((self.base_dir / "data" / "wechat_login_qr.html").is_file())

        self._run(UNINSTALLER, environment)
        self.assertFalse((self.base_dir / "data" / "wechat-claw.sock").exists())

    def test_uninstall_rejects_malformed_zshrc_without_changes(self) -> None:
        environment = self._environment()
        self._run(INSTALLER, environment)
        wrapper = self.bin_dir / "codex"
        zshrc = self.home / ".zshrc"
        malformed = "export KEEP=1\n# >>> wechat-claw >>>\nexport KEEP_TOO=1\n"
        zshrc.write_text(malformed, encoding="utf-8")

        result = self._run(UNINSTALLER, environment, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(zshrc.read_text(), malformed)
        self.assertTrue(wrapper.exists())

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "WECHAT_CLAW_HOME": str(self.base_dir),
                "WECHAT_CLAW_BIN_DIR": str(self.bin_dir),
                "WECHAT_CLAW_ZSHRC": str(self.home / ".zshrc"),
                "WECHAT_CLAW_PYTHON": str(self.fake_python),
                "WECHAT_CLAW_REAL_CODEX": str(self.fake_codex),
                "WECHAT_CLAW_SOURCE_DIR": str(self.source),
                "WECHAT_CLAW_SKIP_DAEMON": "1",
                "FAKE_LAUNCHER_LOG": str(self.launcher_log),
            }
        )
        for name in (
            "ALL_PROXY",
            "all_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "HTTP_PROXY",
            "http_proxy",
        ):
            environment.pop(name, None)
        return environment

    def _run(
        self,
        script: Path,
        environment: dict[str, str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [script],
            check=False,
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            log_path = self.base_dir / "logs" / "wechat-claw.log"
            log = log_path.read_text() if log_path.exists() else ""
            self.fail(
                f"{script.name} failed with {result.returncode}: "
                f"{result.stdout}{result.stderr}{log}"
            )
        return result

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
