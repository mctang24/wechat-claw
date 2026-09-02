#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="${WECHAT_CLAW_HOME:-$HOME/.local/share/wechat-claw}"
APP_DIR="$BASE_DIR/app"
VENV_DIR="$BASE_DIR/venv"
DATA_DIR="$BASE_DIR/data"
LOG_DIR="$BASE_DIR/logs"
BIN_DIR="${WECHAT_CLAW_BIN_DIR:-$HOME/.local/bin}"
WRAPPER_PATH="$BIN_DIR/codex"
WRAPPER_BACKUP="$BIN_DIR/codex.wechat-claw-backup"
ZSHRC="${WECHAT_CLAW_ZSHRC:-$HOME/.zshrc}"
PATH_BEGIN="# >>> wechat-claw >>>"
PATH_END="# <<< wechat-claw <<<"

if [[ -f "$ZSHRC" ]]; then
  awk -v begin="$PATH_BEGIN" -v end="$PATH_END" '
    $0 == begin { if (open) invalid = 1; open = 1; count++; next }
    $0 == end { if (!open) invalid = 1; open = 0; next }
    END { exit invalid || open || count > 1 }
  ' "$ZSHRC" || {
    printf 'wechat-claw uninstall: invalid markers in %s\n' "$ZSHRC" >&2
    exit 1
  }
fi

if [[ -S "$DATA_DIR/wechat-claw.sock" ]] && command -v lsof >/dev/null 2>&1; then
  daemon_pids=()
  while IFS= read -r daemon_pid; do
    [[ -n "$daemon_pid" ]] && daemon_pids+=("$daemon_pid")
  done < <(lsof -t "$DATA_DIR/wechat-claw.sock" 2>/dev/null || true)
  if [[ "${#daemon_pids[@]}" -gt 0 ]]; then
    kill -TERM "${daemon_pids[@]}"
    for _ in 1 2 3 4 5; do
      alive=0
      for daemon_pid in "${daemon_pids[@]}"; do
        kill -0 "$daemon_pid" 2>/dev/null && alive=1
      done
      [[ "$alive" -eq 0 ]] && break
      sleep 1
    done
    [[ "$alive" -eq 0 ]] || {
      printf 'wechat-claw uninstall: running daemon did not stop\n' >&2
      exit 1
    }
  fi
fi

if [[ -e "$WRAPPER_PATH" ]] \
  && grep -q '^# wechat-claw managed wrapper$' "$WRAPPER_PATH"; then
  if [[ -e "$WRAPPER_BACKUP" ]]; then
    mv "$WRAPPER_BACKUP" "$WRAPPER_PATH"
  else
    rm -f "$WRAPPER_PATH"
  fi
fi

if [[ -f "$ZSHRC" ]] && grep -qF "$PATH_BEGIN" "$ZSHRC"; then
  temporary="$(mktemp "${TMPDIR:-/tmp}/wechat-claw-zshrc.XXXXXX")"
  awk -v begin="$PATH_BEGIN" -v end="$PATH_END" '
    $0 == begin { skip = 1; next }
    $0 == end { skip = 0; next }
    !skip { print }
  ' "$ZSHRC" > "$temporary"
  cat "$temporary" > "$ZSHRC"
  rm -f "$temporary"
fi

rm -rf "$APP_DIR" "$VENV_DIR"
rm -f "$BASE_DIR/real-codex-path"

printf 'wechat-claw uninstalled. Preserved data: %s and %s\n' "$DATA_DIR" "$LOG_DIR"
