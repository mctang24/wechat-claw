"""Local paths for ignored runtime state."""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(
    os.environ.get("WECHAT_CLAW_PROJECT_ROOT", Path(__file__).resolve().parents[2])
).resolve()
DATA_DIR = Path(os.environ.get("WECHAT_CLAW_DATA_DIR", PROJECT_ROOT / "data"))
LOG_DIR = Path(os.environ.get("WECHAT_CLAW_LOG_DIR", PROJECT_ROOT / "logs"))
WECHAT_CREDENTIALS_PATH = DATA_DIR / "wechatbot" / "credentials.json"
WECHAT_BINDING_PATH = DATA_DIR / "wechat_binding.json"
LEASE_SOCKET_PATH = DATA_DIR / "wechat-claw.sock"
WECHAT_QR_PAGE_PATH = DATA_DIR / "wechat_login_qr.html"
DAEMON_LOG_PATH = LOG_DIR / "wechat-claw.log"

APP_SERVER_ENDPOINT = "ws://127.0.0.1:48731"
REAL_CODEX_BINARY = os.environ.get(
    "WECHAT_CLAW_REAL_CODEX",
    "/opt/homebrew/bin/codex",
)
