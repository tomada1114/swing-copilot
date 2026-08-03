# 定時実行 (launchd)

平日 15:05 (マシンのローカル時刻) に Claude Code の `/swing-daily` スキルを
headless で自動実行するための launchd 設定。

## 発火時刻とタイムゾーン

`StartCalendarInterval` にタイムゾーン指定は無く、**マシンのローカル
タイムゾーン**で解釈される。同じ plist でもマシンを移動すれば米国市場に対する
相対位置が変わるため、時刻の妥当性は移動のたびに再確認が要る。

満たすべき条件は「15:05 が米国のザラ場中でないこと」。
`src/swing_copilot/data/yfinance_provider.py` の `get_latest_bars` は日付範囲で
しか絞り込まず、**未確定バーを弾くガードが無い**ので、ザラ場中に走ると途中経過の
Close がその日の日足として取り込まれる。

| ローカル TZ | 15:05 の米国東部時刻 (夏時間基準) | 対象バー | 可否 |
|---|---|---|---|
| JST (UTC+9) | 02:05 ET (場外) | 前営業日 | OK |
| MDT (UTC-6) | 17:05 ET (引けの約 1 時間後) | 当日 | OK |
| BST (UTC+1) | 10:05 ET (**ザラ場中**) | 当日の未確定バー | NG |

欧州圏など上表で NG になる TZ へ移動する場合は、発火時刻の見直しが必要。

JST と MDT はどちらも安全だが、対象バーが 1 営業日ずれる点は追跡台帳に効く。
MDT では `as_of` とバー日付が一致するため `entry_date` が `entry_price` の
由来日と揃う。JST では `as_of` が前営業日のバーを指すため、この 2 つは
1 セッションずれる。

米国休場日はパイプラインが fail-soft に流れる(新しいバーが無いだけで壊れない)
ため、祝日除外はしていない。

## 構成

| ファイル | 役割 |
|---|---|
| `run-swing-daily.sh` | ラッパースクリプト。PATH 解決、ログ出力、`claude -p "/swing-daily"` の起動 |
| `com.tomada.swing-copilot.daily.plist` | launchd 定義。月〜金 15:05 に上記スクリプトを起動 |

ログは `~/Library/Logs/swing-copilot/` に実行ごとのファイルとして残る。

## インストール

```bash
cp scripts/schedule/com.tomada.swing-copilot.daily.plist ~/Library/LaunchAgents/
plutil -lint ~/Library/LaunchAgents/com.tomada.swing-copilot.daily.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tomada.swing-copilot.daily.plist
launchctl print gui/$(id -u)/com.tomada.swing-copilot.daily | head -20  # ロード確認
```

## アンインストール

```bash
launchctl bootout gui/$(id -u)/com.tomada.swing-copilot.daily
rm ~/Library/LaunchAgents/com.tomada.swing-copilot.daily.plist
```

## 手動テスト

```bash
bash scripts/schedule/run-swing-daily.sh
```

注意: 手動テストでも本物の日次分析が丸ごと走る(パイプライン実行と
サブエージェント fan-out によるトークン消費が発生する)。スケジュール
設定の検証だけなら `plutil -lint` と `launchctl print` で十分。

## `--dangerously-skip-permissions` について

無人実行では permission プロンプトに答えられないため、ラッパーは
`--dangerously-skip-permissions` を付けて起動する。トレードオフ:

- 緩和要因: `.claude/hooks/guard.py` (PreToolUse) は permission モードに
  かかわらず動作し、`uv.lock` / `.env*` / `secrets/**` への書き込み、
  `--no-verify` コミット、force-push を遮断する。
- 残るリスク: フックの守備範囲外の操作は無確認で実行される。headless 時の
  行動方針(保守側に倒す・fail-closed 厳守)は swing-daily スキル側に
  明記されている。

このリスク許容が変わった場合は、`.claude/settings.local.json` の
allowlist を拡充して `--permission-mode acceptEdits` へ切り替える選択肢も
ある(ただし allowlist 外の操作で headless 実行が停止しうる)。
