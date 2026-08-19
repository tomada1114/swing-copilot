#!/usr/bin/env bash
# 締切ウォッチャ: 指定秒数だけ待って `TIMEBOX_REACHED` を 1 行出力する。
#
# `.claude/skills/swing-daily/SKILL.md` の「親（統括セッション）の待ち方」から
# `run_in_background` で起動され、完了通知がその波の締切とみなされる。
# 待つ以外のことは何もしない（ファイル・ネットワーク・git のいずれにも触れない）。
#
# 1 本のスクリプトに切り出してあるのは、無人実行中にパーミッション承認待ちで
# 止まらないためである。複合ワンライナーのままでは `.claude/settings.json` の
# allowlist に前方一致させられず、打ち切り機構そのものが居残りの原因になる。
#
# usage: scripts/timebox.sh <seconds>
#   seconds: 1 以上の整数。SKILL.md は 900（1 波 15 分）と 2700（全体 45 分）を使う。
#
# exit status:
#   0  待ち切って `TIMEBOX_REACHED` を出力した
#   2  引数が不正（個数違い、非整数、0 以下）

set -euo pipefail

# 残り時間がこれより長い間は、この間隔で刻んで待つ。
readonly POLL_SECONDS=30

usage() {
    echo "usage: ${0##*/} <seconds>" >&2
    echo "  seconds: positive integer (e.g. 900 for a 15-minute timebox)" >&2
}

if [ "$#" -ne 1 ]; then
    echo "${0##*/}: expected exactly 1 argument, got $#" >&2
    usage
    exit 2
fi

case "$1" in
    '' | *[!0-9]*)
        echo "${0##*/}: seconds must be a positive integer, got '$1'" >&2
        usage
        exit 2
        ;;
esac

seconds=$1

if [ "$seconds" -le 0 ]; then
    echo "${0##*/}: seconds must be greater than 0, got '$seconds'" >&2
    usage
    exit 2
fi

end=$(($(date +%s) + seconds))

while :; do
    remaining=$((end - $(date +%s)))
    if [ "$remaining" -le 0 ]; then
        break
    fi
    if [ "$remaining" -gt "$POLL_SECONDS" ]; then
        sleep "$POLL_SECONDS"
    else
        sleep "$remaining"
    fi
done

echo TIMEBOX_REACHED
