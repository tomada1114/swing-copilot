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
| `run-swing-daily.sh` | ラッパースクリプト。PATH 解決、実行前ガード、ログ出力、`claude -p "/swing-daily"` の起動 |
| `com.tomada.swing-copilot.daily.plist` | launchd 定義。月〜金 15:05 に上記スクリプトを起動 |

ログは `~/Library/Logs/swing-copilot/` に実行ごとのファイルとして残る。

## モデル

ラッパーは `--model opus` を明示して起動する。CLI のデフォルトに委ねると、
対話セッション側でモデル設定を変えたときに無人実行の品質が黙って変わるため、
定時実行のモデルはスクリプトに固定する。変更するときはこの 1 箇所を直す。

## 実行前ガード

無人実行は、リポジトリがどういう状態かを選べない。2026-08-03 の実行では
(1) 別スケジュールが同じ run ディレクトリに同時に書き込み、(2) 並行する開発
セッションが `screening/` を編集している最中にパイプラインが走った。前者は
成果物の取り違えを、後者は「同一 run の前半と後半で別のコードが動く」状態を
生む。ラッパーは実行前に次の 2 つを確認する。

### 1. 二重起動の防止

`~/Library/Logs/swing-copilot/.run.lock` をロックとして使う。macOS には
`flock` が無いため `mkdir` のアトミック性で代用する。ロックに記録された PID が
生存していればスキップして終了コード 0（分析は現に走っているので異常ではない）。
PID が不在なら異常終了の残留とみなし、ロックを奪って続行する。

限界: このロックが守れるのは**このラッパー経由の起動だけ**である。2026-08-03 の
競合相手（Claude Desktop のスケジュール）は `claude -p "/swing-daily"` を直接
起動していたため、同じ事象が再発してもロックでは止まらない。ラッパーを通さない
経路まで塞ぐには `/swing-daily` スキル側にロックを持たせる必要がある。

### 2. 作業ツリーのダーティ判定とリトライ

**ブランチ名では判定しない。** コミット済みであれば、マージ待ちのフィーチャー
ブランチ上でもそのまま実行する。判定するのは `src` `config` `pyproject.toml`
`uv.lock` に未コミット差分があるかどうかだけで、`docs/` や `tests/` の編集中は
実行結果に影響しないため許容する。

差分がある場合は 20 分間隔で最大 4 回まで再確認する（15:05 発火なら最悪 16:05
開始）。MDT では米国引けが 14:00、JST では前営業日バーが対象なので、この程度の
遅延で取り込む日足は変わらない。4 回とも解消しなければ本日の実行を見送り、
終了コード 1 と macOS 通知を出す。無言でスキップはしない。

`SWING_DAILY_RETRY_LIMIT` と `SWING_DAILY_RETRY_INTERVAL` で上書きできる
（手動テスト用。本番は既定値のまま）。

### 実行後の検証

実行前がクリーンでも、実行中に別セッションが編集を始めることは防げない。
そのため実行後に HEAD と作業ツリーを再取得し、変化していれば
「この run の再現性は担保されない」旨をログに刻む。レポート自体は破棄しない
（判断材料として残し、人間が読めるようにする）。実行時のコミットハッシュと
ブランチ名も毎回ログの冒頭に記録する。

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

ガードの挙動だけを確かめたい場合は、`CLAUDE_BIN` にダミーを渡すと分析を
起動せずに前後処理だけを実行できる。

```bash
# クリーン時: 実行に進む
CLAUDE_BIN=/bin/echo bash scripts/schedule/run-swing-daily.sh

# ダーティ時: リトライして見送る(間隔を縮めて確認)
touch config/.dirty-probe
SWING_DAILY_RETRY_INTERVAL=1 SWING_DAILY_RETRY_LIMIT=3 \
  CLAUDE_BIN=/bin/echo bash scripts/schedule/run-swing-daily.sh
rm -f config/.dirty-probe
```

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
