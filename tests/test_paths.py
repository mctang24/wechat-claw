from __future__ import annotations

import importlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_claw import paths


class PathsTest(unittest.TestCase):
    def test_install_environment_overrides_runtime_paths(self) -> None:
        overrides = {
            "WECHAT_CLAW_PROJECT_ROOT": "/tmp/wechat-claw-app",
            "WECHAT_CLAW_DATA_DIR": "/tmp/wechat-claw-data",
            "WECHAT_CLAW_LOG_DIR": "/tmp/wechat-claw-logs",
            "WECHAT_CLAW_REAL_CODEX": "/tmp/bin/codex-real",
        }
        try:
            with patch.dict(os.environ, overrides):
                reloaded = importlib.reload(paths)
                self.assertEqual(
                    reloaded.PROJECT_ROOT,
                    Path(overrides["WECHAT_CLAW_PROJECT_ROOT"]).resolve(),
                )
                self.assertEqual(reloaded.DATA_DIR, Path(overrides["WECHAT_CLAW_DATA_DIR"]))
                self.assertEqual(reloaded.LOG_DIR, Path(overrides["WECHAT_CLAW_LOG_DIR"]))
                self.assertEqual(reloaded.REAL_CODEX_BINARY, overrides["WECHAT_CLAW_REAL_CODEX"])
        finally:
            for name in overrides:
                os.environ.pop(name, None)
            importlib.reload(paths)


if __name__ == "__main__":
    unittest.main()
