#!/bin/bash
# 平日15時ごろに launchd から起動され、/swing-daily を headless 実行するラッパー。
# launchd の PATH は最小構成のため、ここで claude / uv の解決を行う。
set -euo pipefail

REPO_DIR="/Users/masuyama/ghq/github.com/tomada1114/swing-copilot"
LOG_DIR="$HOME/Library/Logs/swing-copilot"
LOG_FILE="$LOG_DIR/swing-daily-$(date +%Y%m%d-%H%M%S).log"
LOCK_DIR="$LOG_DIR/.run.lock"

# 実行結果に影響するパスだけを対象にする。docs/ や tests/ の編集中は許容する。
GUARDED_PATHS=(src config pyproject.toml uv.lock)
# 手動テスト時に短縮できるよう環境変数で上書き可能にしてある。
RETRY_LIMIT="${SWING_DAILY_RETRY_LIMIT:-4}"
RETRY_INTERVAL="${SWING_DAILY_RETRY_INTERVAL:-1200}" # 20分

mkdir -p "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

notify() {
  # launchd の GUI セッションから通知を出す。失敗しても実行は続ける。
  osascript -e "display notification \"$1\" with title \"swing-copilot\"" || true
}

dirty_paths() {
  git -C "$REPO_DIR" status --porcelain -- "${GUARDED_PATHS[@]}"
}

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

# --- ガード1: 二重起動の防止 -------------------------------------------------
# macOS に flock が無いため mkdir のアトミック性を使う。異常終了で取り残された
# ロックは、記録した PID が生きていない場合にのみ奪う。
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  stale_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ -n "$stale_pid" ]] && kill -0 "$stale_pid" 2>/dev/null; then
    echo "SKIP: 別の swing-daily が実行中 (pid=$stale_pid)。本日の起動を見送る。"
    notify "swing-daily をスキップしました（別プロセスが実行中）"
    exit 0
  fi
  echo "WARN: 残留ロックを検出 (pid=${stale_pid:-unknown} は不在)。奪って続行する。"
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
fi
echo $$ >"$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

# --- ガード2: 作業ツリーがダーティなら時間を置いてリトライ -------------------
# ブランチ名では判定しない。コミット済みならフィーチャーブランチでも実行する。
# 未コミット差分がある場合のみ、編集が落ち着くのを待つ。
for ((attempt = 1; attempt <= RETRY_LIMIT; attempt++)); do
  dirty="$(dirty_paths)"
  if [[ -z "$dirty" ]]; then
    break
  fi
  echo "作業ツリーがダーティ (attempt $attempt/$RETRY_LIMIT):"
  echo "$dirty"
  if ((attempt == RETRY_LIMIT)); then
    echo "ERROR: ${RETRY_LIMIT}回リトライしても ${GUARDED_PATHS[*]} に未コミット差分あり。本日の実行を見送る。"
    notify "swing-daily を見送りました（作業ツリーがダーティ）"
    exit 1
  fi
  echo "${RETRY_INTERVAL}秒後に再確認する。"
  sleep "$RETRY_INTERVAL"
done

# --- 実行 -------------------------------------------------------------------
HEAD_BEFORE="$(git -C "$REPO_DIR" rev-parse HEAD)"
BRANCH="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)"
echo "=== swing-daily headless run: $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
echo "commit: $HEAD_BEFORE / branch: $BRANCH"
# 無人実行のため permission プロンプトで停止できず bypass する。
# .claude/hooks/guard.py (PreToolUse) は permission モードに関係なく動作し、
# uv.lock / .env* / secrets への書き込みや force-push を引き続き遮断する。
# モデルは明示する。CLI のデフォルトに委ねると、対話セッション側の設定変更で
# 無人実行の品質が黙って変わる。
rc=0
"$CLAUDE_BIN" -p "/swing-daily" --model opus --dangerously-skip-permissions || rc=$?

# --- 実行後の検証 -----------------------------------------------------------
# 実行中に別セッションが編集を始めていた場合、前半と後半で別のコードが走って
# いる可能性がある。レポートは残すが、再現性が担保できない旨をログに刻む。
HEAD_AFTER="$(git -C "$REPO_DIR" rev-parse HEAD)"
if [[ "$HEAD_BEFORE" != "$HEAD_AFTER" ]]; then
  echo "WARN: 実行中に HEAD が変化した ($HEAD_BEFORE -> $HEAD_AFTER)。この run の再現性は担保されない。"
fi
dirty_after="$(dirty_paths)"
if [[ -n "$dirty_after" ]]; then
  echo "WARN: 実行中に ${GUARDED_PATHS[*]} が編集された。この run の再現性は担保されない:"
  echo "$dirty_after"
fi

echo "=== finished: $(date '+%Y-%m-%d %H:%M:%S %Z') (exit=$rc) ==="
exit "$rc"
