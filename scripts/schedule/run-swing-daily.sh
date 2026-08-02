#!/bin/bash
# 平日15時ごろに launchd から起動され、/swing-daily を headless 実行するラッパー。
# launchd の PATH は最小構成のため、ここで claude / uv の解決を行う。
set -euo pipefail

REPO_DIR="/Users/masuyama/ghq/github.com/tomada1114/swing-copilot"
LOG_DIR="$HOME/Library/Logs/swing-copilot"
LOG_FILE="$LOG_DIR/swing-daily-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"
if [[ -z "$CLAUDE_BIN" ]]; then
  echo "ERROR: claude CLI が見つかりません (PATH=$PATH)" >&2
  exit 1
fi
if ! command -v uv >/dev/null; then
  echo "ERROR: uv が見つかりません (PATH=$PATH)" >&2
  exit 1
fi

cd "$REPO_DIR"

echo "=== swing-daily headless run: $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
# 無人実行のため permission プロンプトで停止できず bypass する。
# .claude/hooks/guard.py (PreToolUse) は permission モードに関係なく動作し、
# uv.lock / .env* / secrets への書き込みや force-push を引き続き遮断する。
rc=0
"$CLAUDE_BIN" -p "/swing-daily" --dangerously-skip-permissions || rc=$?
echo "=== finished: $(date '+%Y-%m-%d %H:%M:%S %Z') (exit=$rc) ==="
exit "$rc"
