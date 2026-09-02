#!/usr/bin/env bash
set -euo pipefail

REPO="${WECHAT_CLAW_REPO:-mctang24/wechat-claw}"
REF="${WECHAT_CLAW_REF:-main}"
BASE_DIR="${WECHAT_CLAW_HOME:-$HOME/.local/share/wechat-claw}"
APP_DIR="$BASE_DIR/app"
VENV_DIR="$BASE_DIR/venv"
DATA_DIR="$BASE_DIR/data"
LOG_DIR="$BASE_DIR/logs"
BIN_DIR="${WECHAT_CLAW_BIN_DIR:-$HOME/.local/bin}"
WRAPPER_PATH="$BIN_DIR/codex"
WRAPPER_BACKUP="$BIN_DIR/codex.wechat-claw-backup"
REAL_CODEX_FILE="$BASE_DIR/real-codex-path"
ZSHRC="${WECHAT_CLAW_ZSHRC:-$HOME/.zshrc}"
PATH_BEGIN="# >>> wechat-claw >>>"
PATH_END="# <<< wechat-claw <<<"
PYTHON="${WECHAT_CLAW_PYTHON:-python3}"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/wechat-claw-install.XXXXXX")"
COMMITTED=0
MUTATED=0
HAD_APP=0
HAD_VENV=0
HAD_WRAPPER=0
HAD_WRAPPER_BACKUP=0
HAD_ZSHRC=0
HAD_REAL_CODEX_FILE=0
STARTED_DAEMON_PID=""
STOPPED_DAEMON=0

cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [[ -n "$STARTED_DAEMON_PID" ]] && kill -0 "$STARTED_DAEMON_PID" 2>/dev/null; then
    kill -TERM "$STARTED_DAEMON_PID" 2>/dev/null || true
    wait "$STARTED_DAEMON_PID" 2>/dev/null || true
  fi
  if [[ "$status" -ne 0 && "$COMMITTED" -eq 0 && "$MUTATED" -eq 1 ]]; then
    rm -rf "$APP_DIR" "$VENV_DIR"
    [[ "$HAD_APP" -eq 1 ]] && mv "$TMP_DIR/old-app" "$APP_DIR"
    [[ "$HAD_VENV" -eq 1 ]] && mv "$TMP_DIR/old-venv" "$VENV_DIR"
    if [[ "$HAD_WRAPPER" -eq 1 ]]; then
      cp "$TMP_DIR/old-wrapper" "$WRAPPER_PATH"
    else
      rm -f "$WRAPPER_PATH"
    fi
    [[ "$HAD_WRAPPER_BACKUP" -eq 0 ]] && rm -f "$WRAPPER_BACKUP"
    if [[ "$HAD_ZSHRC" -eq 1 ]]; then
      cp "$TMP_DIR/old-zshrc" "$ZSHRC"
    else
      rm -f "$ZSHRC"
    fi
    if [[ "$HAD_REAL_CODEX_FILE" -eq 1 ]]; then
      cp "$TMP_DIR/old-real-codex-path" "$REAL_CODEX_FILE"
    else
      rm -f "$REAL_CODEX_FILE"
    fi
    if [[ "$STOPPED_DAEMON" -eq 1 && -x "$VENV_DIR/bin/wechat-claw" ]]; then
      restored_codex="$(cat "$REAL_CODEX_FILE" 2>/dev/null || true)"
      WECHAT_CLAW_PROJECT_ROOT="$APP_DIR" \
      WECHAT_CLAW_DATA_DIR="$DATA_DIR" \
      WECHAT_CLAW_LOG_DIR="$LOG_DIR" \
      WECHAT_CLAW_REAL_CODEX="$restored_codex" \
        nohup "$VENV_DIR/bin/wechat-claw" daemon >> "$LOG_DIR/wechat-claw.log" 2>&1 &
    fi
  fi
  rm -rf "$TMP_DIR"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

fail() {
  printf 'wechat-claw install: %s\n' "$1" >&2
  exit 1
}

valid_path_block() {
  awk -v begin="$PATH_BEGIN" -v end="$PATH_END" '
    $0 == begin { if (open) invalid = 1; open = 1; count++; next }
    $0 == end { if (!open) invalid = 1; open = 0; next }
    END { exit invalid || open || count > 1 }
  ' "$1"
}

stop_daemon() {
  [[ -S "$DATA_DIR/wechat-claw.sock" ]] || return 0
  daemon_pids=()
  while IFS= read -r daemon_pid; do
    [[ -n "$daemon_pid" ]] && daemon_pids+=("$daemon_pid")
  done < <(lsof -t "$DATA_DIR/wechat-claw.sock" 2>/dev/null || true)
  [[ "${#daemon_pids[@]}" -gt 0 ]] || return 0
  kill -TERM "${daemon_pids[@]}"
  for _ in 1 2 3 4 5; do
    alive=0
    for daemon_pid in "${daemon_pids[@]}"; do
      kill -0 "$daemon_pid" 2>/dev/null && alive=1
    done
    if [[ "$alive" -eq 0 ]]; then
      STOPPED_DAEMON=1
      return
    fi
    sleep 1
  done
  fail "running daemon did not stop"
}

[[ "$(uname -s)" == "Darwin" ]] || fail "only macOS is supported"
command -v "$PYTHON" >/dev/null 2>&1 || fail "Python 3.11+ is required"
"$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' \
  || fail "Python 3.11+ is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v lsof >/dev/null 2>&1 || fail "lsof is required"

mkdir -p "$BASE_DIR" "$BIN_DIR" "$DATA_DIR" "$LOG_DIR"

if [[ -n "${WECHAT_CLAW_REAL_CODEX:-}" ]]; then
  real_codex="$WECHAT_CLAW_REAL_CODEX"
elif [[ -f "$REAL_CODEX_FILE" && -x "$WRAPPER_PATH" ]] \
  && grep -q '^# wechat-claw managed wrapper$' "$WRAPPER_PATH"; then
  real_codex="$(cat "$REAL_CODEX_FILE")"
else
  real_codex="$(command -v codex || true)"
fi
[[ -n "$real_codex" && -x "$real_codex" ]] || fail "Codex CLI was not found in PATH"

if [[ -e "$WRAPPER_PATH" ]]; then
  HAD_WRAPPER=1
  cp "$WRAPPER_PATH" "$TMP_DIR/old-wrapper"
fi
[[ -e "$WRAPPER_BACKUP" ]] && HAD_WRAPPER_BACKUP=1
if [[ -e "$ZSHRC" ]]; then
  valid_path_block "$ZSHRC" || fail "invalid wechat-claw markers in $ZSHRC"
  HAD_ZSHRC=1
  cp "$ZSHRC" "$TMP_DIR/old-zshrc"
fi
if [[ -e "$REAL_CODEX_FILE" ]]; then
  HAD_REAL_CODEX_FILE=1
  cp "$REAL_CODEX_FILE" "$TMP_DIR/old-real-codex-path"
fi

if [[ -e "$WRAPPER_PATH" ]] \
  && ! grep -q '^# wechat-claw managed wrapper$' "$WRAPPER_PATH"; then
  [[ ! -e "$WRAPPER_BACKUP" ]] || fail "wrapper backup already exists: $WRAPPER_BACKUP"
  if [[ "$real_codex" == "$WRAPPER_PATH" ]]; then
    real_codex="$WRAPPER_BACKUP"
  fi
fi

stage_app="$TMP_DIR/app"
mkdir -p "$stage_app"
if [[ -n "${WECHAT_CLAW_SOURCE_DIR:-}" ]]; then
  cp -R "$WECHAT_CLAW_SOURCE_DIR/." "$stage_app"
else
  archive="$TMP_DIR/source.tar.gz"
  extracted="$TMP_DIR/source"
  mkdir -p "$extracted"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" -o "$archive"
  tar -xzf "$archive" -C "$extracted"
  source_root="$(find "$extracted" -mindepth 1 -maxdepth 1 -type d -print -quit)"
  [[ -n "$source_root" ]] || fail "downloaded archive is empty"
  cp -R "$source_root/." "$stage_app"
fi
[[ -f "$stage_app/pyproject.toml" ]] || fail "downloaded source is missing pyproject.toml"

stop_daemon
MUTATED=1
if [[ -d "$APP_DIR" ]]; then
  mv "$APP_DIR" "$TMP_DIR/old-app"
  HAD_APP=1
fi
if [[ -d "$VENV_DIR" ]]; then
  mv "$VENV_DIR" "$TMP_DIR/old-venv"
  HAD_VENV=1
fi
mv "$stage_app" "$APP_DIR"
"$PYTHON" -m venv "$VENV_DIR"
proxy_url="${HTTPS_PROXY:-${https_proxy:-${ALL_PROXY:-${all_proxy:-}}}}"
case "$proxy_url" in
  socks*|SOCKS*)
    socks_metadata="$TMP_DIR/pysocks.json"
    curl -fsSL https://pypi.org/pypi/PySocks/json -o "$socks_metadata"
    socks_url="$("$PYTHON" -c 'import json, sys; data=json.load(open(sys.argv[1])); print(next(item["url"] for item in data["urls"] if item["filename"].endswith("py3-none-any.whl")))' "$socks_metadata")"
    socks_wheel="$TMP_DIR/${socks_url##*/}"
    curl -fsSL "$socks_url" -o "$socks_wheel"
    env -u ALL_PROXY -u all_proxy -u HTTPS_PROXY -u https_proxy \
      -u HTTP_PROXY -u http_proxy \
      "$VENV_DIR/bin/python" -m pip install --no-index "$socks_wheel"
    ;;
esac
"$VENV_DIR/bin/python" -m pip install "$APP_DIR"

if [[ -e "$WRAPPER_PATH" ]] \
  && ! grep -q '^# wechat-claw managed wrapper$' "$WRAPPER_PATH"; then
  cp "$WRAPPER_PATH" "$WRAPPER_BACKUP"
fi
printf '%s\n' "$real_codex" > "$REAL_CODEX_FILE"
chmod 600 "$REAL_CODEX_FILE"

cat > "$TMP_DIR/codex-wrapper" <<EOF
#!/bin/zsh
# wechat-claw managed wrapper
export WECHAT_CLAW_PROJECT_ROOT="$APP_DIR"
export WECHAT_CLAW_DATA_DIR="$DATA_DIR"
export WECHAT_CLAW_LOG_DIR="$LOG_DIR"
export WECHAT_CLAW_REAL_CODEX="$real_codex"
exec "$VENV_DIR/bin/wechat-claw-codex" \\
  --real-codex "$real_codex" \\
  --socket "$DATA_DIR/wechat-claw.sock" \\
  -- "\$@"
EOF
chmod 755 "$TMP_DIR/codex-wrapper"
mv "$TMP_DIR/codex-wrapper" "$WRAPPER_PATH"

if ! grep -qF "$PATH_BEGIN" "$ZSHRC" 2>/dev/null; then
  {
    printf '\n%s\n' "$PATH_BEGIN"
    printf 'export PATH="%s:$PATH"\n' "$BIN_DIR"
    printf '%s\n' "$PATH_END"
  } >> "$ZSHRC"
fi

if [[ "${WECHAT_CLAW_SKIP_DAEMON:-0}" != "1" ]]; then
  export WECHAT_CLAW_PROJECT_ROOT="$APP_DIR"
  export WECHAT_CLAW_DATA_DIR="$DATA_DIR"
  export WECHAT_CLAW_LOG_DIR="$LOG_DIR"
  export WECHAT_CLAW_REAL_CODEX="$real_codex"
  nohup "$VENV_DIR/bin/wechat-claw" daemon >> "$LOG_DIR/wechat-claw.log" 2>&1 &
  STARTED_DAEMON_PID=$!
  daemon_ready=0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$STARTED_DAEMON_PID" 2>/dev/null || fail "daemon failed to start; see $LOG_DIR/wechat-claw.log"
    [[ -S "$DATA_DIR/wechat-claw.sock" ]] && daemon_ready=1
    if [[ -f "$DATA_DIR/wechat_login_qr.html" ]]; then
      open "$DATA_DIR/wechat_login_qr.html" >/dev/null 2>&1 || true
    fi
    [[ "$daemon_ready" -eq 1 ]] && break
    sleep 1
  done
  [[ "$daemon_ready" -eq 1 ]] || fail "daemon did not become ready; see $LOG_DIR/wechat-claw.log"
  STARTED_DAEMON_PID=""
fi

COMMITTED=1

printf '\nwechat-claw installed. Open a new terminal, then run: codex\n'
