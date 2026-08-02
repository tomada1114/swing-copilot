# 定時実行 (launchd)

平日 15:05 (JST/ローカル時刻) に Claude Code の `/swing-daily` スキルを
headless で自動実行するための launchd 設定。

米国市場の EOD データは JST 朝 5〜6 時ごろに確定するため、15 時実行は
データ鮮度の面で十分に安全なタイミング。米国休場日はパイプラインが
fail-soft に流れる(新しいバーが無いだけで壊れない)ため、祝日除外は
していない。

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
