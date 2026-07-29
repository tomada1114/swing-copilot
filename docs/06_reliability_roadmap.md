# 06. 信頼性改修ロードマップ（Reliability Renovation Roadmap）

- ステータス: 計画確定（2026-07-22）
- 由来: `claude-trading-skills` リポジトリ（約70スキル）の Dynamic Workflow 調査
  （11エージェント・約119万トークン）と swing-copilot 現状監査の統合結果
- 位置づけ: 本書はこの改修一式の**計画の正本**である。実装時に確定した仕様は
  Issue 単位で `docs/03_basic_design.md` / `docs/04_detailed_design.md` /
  `docs/reference.md` へ反映し、本書は履歴として残す（AGENTS.md の正本優先則に従う）。

> **現況注記（P7 スキル移行後）**: P2-12 / P3-15 / P6-26 / P6-27 は完了記録として
> そのまま残すが、これらが対象としていた Anthropic API 直呼びの `llm/` パッケージ、
> `llm_calls` テーブル、月次予算ゲート、応答キャッシュと near-stale 警告は、
> P7（§5 末尾）で機構ごと削除されている。設計の現況は
> `docs/03_basic_design.md` 6.2 節と `docs/04_detailed_design.md` 3.15〜3.17 節を
> 正とすること。以降の本文で「LLM」と書かれている箇所は、現行では
> 「Claude Code スキルによる定性分析」と読み替える。

## 1. 背景と診断

swing-copilot はエンジニアリング基盤（as-of 規律・provenance・fail-soft・原子的書き込み）
は堅牢だが、投資ドメインの中身は初級戦略1本にとどまり、判断根拠の可視性に構造的欠陥がある。

主要な診断結果（ファイル根拠付き）:

| # | 問題 | 根拠 |
|---|------|------|
| D1 | シグナルは「SMA50>200 + RSI<45」の実質1種。`SignalHit.strength` は常に 1.0 固定で未使用 | `screening/technical_signals.py` |
| D2 | ランキングは `(rsi14 asc, avg_volume desc, symbol)` の固定ソートでスコア内訳が存在しない | `screening/pipeline.py:115` |
| D3 | SPY/QQQ/^VIX/^TNX は表示専用。市場環境がスクリーニング・リスク判断に一切反映されない | `pipeline/daily.py:778-782` |
| D4 | スクリーニング落選銘柄は無記録（リスク段階の却下のみ reasons が残る非対称） | `storage/audit_records.py` |
| D5 | バックテストに Sharpe/最大DD/勝率/PF がなく、実行 CLI も未登録 | `backtest/engine.py:64-73`, `pyproject.toml` |
| D6 | `PaperJournal.summarize_performance()` が実装済みなのに未接続。判断履歴の読み出し CLI がない | `paper/journal.py`, `paper/cli.py` |
| D7 | 口座レベルのリスク制御（総リスク量・実現損失上限・決算近接）が皆無 | `risk/checks.py` |

HTML レポートは commit a30f670 で削除済み。terminal/markdown/discord が `DailyBrief`
単一モデルを共有する現行構成を維持し、**HTML は再導入しない**（決定記録）。
Markdown が `reports/` に原子的に残るため「実行結果の永続化」は満たされている。

## 2. 改修原則

1. **信頼性最優先**: 新しいシグナルより先に「見える化」と「検証装置」を作る。
   検証できない知見は導入しない。
2. **仕組みは採用、閾値は要検証**: 参照元スキル集の「ゲート構造・状態機械・スコア内訳・
   fail-closed 設計」はそのまま採用する。一方、具体的な閾値・重みは
   **すべて設定値（config）として実装**し、既定値には出典と検証状態を注記する。
   `(要検証)` の付く値を検証なしでハードコードしてはならない。
3. **既存不変条件の堅持**: as-of 境界（inclusive）、Clock 注入、オフラインテスト
   （socket guard）、1論理書き込み=1トランザクション、原子的置換、CON-03、
   provenance（source_ids）は全 Issue に適用される。
4. **判断はコード、叙述はスキル分析**: ゲート・スコア・却下判定は決定論的コードのみが
   行う。定性分析は根拠の説明・定性文脈の付加に限定し、コードの定量判定を上書きしない。
   定性と定量が矛盾する場合は保守側を採択し両論併記する。
   （P7 移行前は「叙述は LLM」と表記していた。担い手が Anthropic API から
   Claude Code スキルへ変わっただけで、原則そのものは不変である。）
5. **落選にも根拠**: 候補に「なぜ載ったか」だけでなく「なぜ載らなかったか」を
   理由コード付きで永続化する。

## 3. フェーズ構成

既存ラベル `phase-1`（守りと判断根拠の土台）/ `phase-2`（フィードバックループとLLM強化）を
再利用し、`phase-3`〜`phase-5` を追加する。フェーズ = GitHub マイルストーン。

| フェーズ | 内容 | 狙い |
|---|---|---|
| P1 判断根拠の可視化と数値堅牢性 | スコア内訳、落選記録、制約明示、履歴CLI、実績集計接続 | ユーザーの痛点に直結。以降の全機能の「根拠が見える」土台 |
| P2 検証ループとLLM強化 | バックテスト指標/CLI、悲観モード、感応度グリッド、ポストモーテム、LLM文脈強化 | 「知見を数値で裏取りする装置」。P5 の前提 |
| P3 市場レジームゲート | レジームスコア、Distribution Day、Exposure Ceiling、FTD | 最大のドメインギャップ（D3）の解消 |
| P4 口座レベルリスク規律 | ポートフォリオヒート、決算近接、サーキットブレーカー、MAE/MFE | 候補単体→口座全体への守りの拡張 |
| P5 シグナル拡充（検証済み導入） | Minervini、決算後スコア、実行状態分類、VCP | P2 の装置で裏取りしながら追加 |
| P6 実運用ギャップ修正 | 実 API 動作確認で発見したリグレッション・境界欠落・会計/表示バグの修正 | P1〜P5 を「テストが通る」から「毎日回る」へ |
| P7 定性分析のスキル移行 | LLM API 統合の全廃、`analysis/` 境界の新設、Claude Code スキルによる定性分析と `copilot-ingest-analysis` での検証 | 叙述の担い手を API 課金・予算ゲート・キャッシュなしで持てるようにする |
| P8 振り返り→改善提案 | verdict 当否検証（`verdict_outcomes`）、統合振り返り CLI `copilot-retro` とスキル `swing-retro`、提案台帳と適用 PR | 定性レイヤを含む分析ロジック全体を、蓄積した証拠に基づき継続改善する（軽微は即時適用 + PR、中規模以上は設計承認後に適用 + PR） |

実施順序は P1 → P2 → P3 → P4 → P5 を基本とするが、フェーズ内は並列可能、
P3/P4 は P2 完了を待たず着手可能（P5 のみ P2 完了が前提）。

## 4. 全 Issue 共通完了条件（共通 DoD）

各 Issue はこのブロックを受け入れ条件に含める:

- [ ] オフラインテストのみで再現するテストを追加（socket guard 維持、外部ポートはフェイク注入）
- [ ] as-of 境界テスト: カットオフ直前・ちょうど・直後の3点を検証（該当する場合）
- [ ] `just verify` がグリーン（lint / test カバレッジ95%以上 / docs-check / smoke）
- [ ] 変更が公開 API・データ形状に触れる場合、`docs/03_basic_design.md` /
      `docs/04_detailed_design.md` / `docs/reference.md` の該当箇所を同一論理コミットで更新
- [ ] 下記「動作確認」手順を実行し、コマンドと観測結果を PR に記録
      （動作確認は原則 `--dry-run` で行い、live DB を汚さない）
- [ ] 新しい設定値には既定値・単位・出典（本書のセクション番号）をコメントで記載

## 5. Issue 仕様シード

以下の各項が GitHub Issue 1件に対応する。ID は `P<フェーズ>-<連番>`。
Issue 化の際は planning-tickets テンプレートに従い EARS 形式に展開する。

---

### P1-01 【基盤】screening - シグナルメトリクス伝搬とスコア内訳付き複合ランキング

- **目的**: D1/D2 の解消。ランキングを「なぜこの順位か」を説明できる複合スコアにする。
- **スコープ**: `screening/base.py`, `screening/technical_signals.py`,
  `screening/pipeline.py`, `config.py`, `config/strategies.yaml`,
  `report/daily_brief.py`, `report/terminal_report.py`, `report/markdown_report.py`,
  `storage/audit_records.py`
- **主要仕様**:
  - 各シグナルの個別メトリクス（rsi14, sma50, sma200, atr14, avg_volume20 等）を
    `Candidate.metrics` に保持し、レポートまで到達させる。
  - 複合スコア `score = Σ(weight_i × component_i)`、component は [0,1] に正規化:
    - `rsi_pullback = clamp((rsi_threshold − rsi14) / rsi_threshold, 0, 1)`（重み既定 0.5）
    - `trend_quality = clamp((sma50/sma200 − 1) / 0.10, 0, 1)`（重み既定 0.3）
    - `liquidity` = 候補集合内の avg_volume20 パーセンタイル（重み既定 0.2）
  - 重みは strategies.yaml の ranking 設定で構成可能。合計 1.0・各重み ≥0 を strict 検証、
    未知キーは拒否。同点時の決定的 tiebreak は symbol 昇順（決定的順序の既存不変条件）。
  - スコアと内訳（component ごとの値×重み）を DuckDB に永続化し、
    terminal / markdown にスコア内訳列を表示する。
  - 既定重みは出典なしの初期値であり (要検証)。P2-10 感応度グリッドの検証対象。
- **動作確認**: `uv run copilot-daily --as-of <直近営業日> --dry-run --skip-text --limit 20`
  （`--skip-llm` は P7 で廃止）
  → terminal 出力に `score` 列とスコア内訳、markdown に内訳テーブルが出ること。
  重み合計 ≠1.0 の strategies.yaml で起動し fail-fast のエラーメッセージを確認。
- **Not in scope**: 新シグナル追加（P5）、レジーム項のスコア組み込み（P3-14）
- **依存**: なし（フェーズ基盤）

### P1-02 【並列可/worktree:p1-screening】screening/storage - 落選銘柄の理由コード台帳

- **目的**: D4 の解消。「なぜ候補に挙がらなかったか」を事後検証可能にする。
- **スコープ**: `screening/pipeline.py`, `storage/schema.py`, `storage/audit_records.py`,
  `report/daily_brief.py`, `report/markdown_report.py`
- **主要仕様**:
  - 新テーブル `screening_rejections(run_id, symbol, stage, reason_code, detail, as_of)`。
    stage ∈ {data_quality, fundamental_filter, technical_signal}。
  - reason_code は列挙型（例: `FILTER_NEGATIVE_NET_INCOME`, `FILTER_NEGATIVE_FCF`,
    `FILTER_LOW_EQUITY_RATIO`, `SIGNAL_TREND_NOT_MET`, `SIGNAL_RSI_NOT_MET`,
    `DATA_INSUFFICIENT_HISTORY`）。detail に観測値と閾値を JSON で記録
    （例: `{"equity_ratio": 0.24, "threshold": 0.30}`）。
  - 候補書き込みと同一トランザクションで記録（原子性不変条件）。
  - markdown / terminal に落選サマリ（reason_code 別件数）を表示。
- **動作確認**: dry-run 実行後、DuckDB に落選行が入ること
  （`uv run python -c "..."` で件数と1行サンプルを表示）、markdown に
  「落選サマリ」節が出ること。全銘柄合格のケースで 0 件でも節が壊れないこと。
- **Not in scope**: 落選履歴の閲覧 CLI（P1-05）、ユニバース選定段階の記録
- **依存**: なし

### P1-03 【並列可/worktree:p1-risk】risk - binding constraint 明示とサイジング内訳

- **目的**: 「なぜこの株数か」を即答可能にする。
- **スコープ**: `risk/position_sizing.py`, `risk/checks.py`, `models.py`,
  `storage/audit_records.py`, `report/*`
- **主要仕様**:
  - サイジング結果に中間値を保持: `shares_by_risk`（リスク%基準）、
    `shares_by_position_cap`（ポジション%基準）、最終 `shares`、
    `binding_constraint` ∈ {trade_risk, position_cap, sector, correlation, not_calculable}。
  - 損切り幅がエントリー価格の 10%（config、既定 10.0 (要検証)）を超える場合
    `WIDE_STOP` warning を追加。
  - 1株未満に切り捨てられた場合・計算リスク額が極小の場合は
    `SMALL_ACCOUNT_FRICTION` warning。
  - レポート表示例: `128株（制約: リスク1.0%）`。DuckDB へ内訳を永続化。
- **動作確認**: dry-run 実行で候補ごとに制約名が表示されること。
  設定の `max_position_pct` を極端に絞って再実行し、binding constraint が
  `position_cap` に切り替わることを確認。
- **Not in scope**: ポートフォリオヒート（P4-17）、レジーム連動の上限（P3-14）
- **依存**: なし

### P1-04 【並列可/worktree:p1-risk】risk/storage - 数値堅牢性（Fraction 床計算・NaN/Inf 書き込みガード）

- **目的**: 丸め誤差・非数の混入という既知バグクラスを構造的に排除する。
- **スコープ**: `risk/position_sizing.py`, `storage/` 配下の JSON 直列化境界
- **主要仕様**:
  - 株数の床計算を float 除算から `fractions.Fraction` による厳密床計算へ置換。
    `shares × risk_per_share ≤ risk_budget` が構成的に成立することをプロパティ的
    テスト（極端な口座額・微小リスク%を含む）で保証。
  - `storage/` に共通ガード（反復スタック方式で dict/list を走査し inf/-inf/nan を検出、
    `json.dumps(allow_nan=False)` を第二防御）を追加し、JSON を書くすべての箇所に適用。
  - NaN/Inf 検出時は書き込み前に明示的例外（どのキーかを含む）。再帰は使わない
    （深いネストで RecursionError になるため）。
- **動作確認**: `uv run pytest tests/risk tests/storage -q` グリーン。
  NaN を含むレコードを書こうとするテストでキー名入りの例外が出ること。
- **Not in scope**: Decimal 化などの全面的な数値型変更
- **依存**: なし

### P1-05 【依存あり/worktree:p1-cli】cli/report - 判断履歴の読み出し（copilot-history）

- **目的**: D6 の解消（読み出し側）。書き込み専用 CLI に「見る」手段を追加する。
- **スコープ**: 新 `[project.scripts]` エントリ `copilot-history`（新モジュール、
  例: `paper/history_cli.py` または `report/history_cli.py`）、
  `storage/state_store.py`, `report/markdown_report.py`
- **主要仕様**:
  - サブコマンド: `runs`（直近 N 件の run 一覧: as_of・候補数・落選数・判断数）、
    `run --run-id <id>`（候補+リスク+判断の詳細）、`symbol <SYM>`（銘柄横断の
    候補化・判断・結果の時系列）、`rejections --run-id <id>`（P1-02 の台帳照会）、
    `performance`（P1-06 の集計表示）。すべて読み出し専用・Rich テーブル出力。
  - markdown レポートに「過去判断」節を追加: 各候補銘柄について直近3件の判断
    （decision, reason, 記録日、クローズ済みなら損益）を LLM を経由せず
    DuckDB から直接表示する。
- **動作確認**: `uv run copilot-history runs`、`uv run copilot-history symbol AAPL` 等を
  実行し、既存 DB の内容が表形式で出ること。DB が空でも例外にならず
  「記録なし」表示になること。
- **Not in scope**: 判断の書き込み・編集（既存 copilot-decision のまま）
- **依存**: P1-02（rejections 照会）、P1-06（performance 表示）

### P1-06 【並列可/worktree:p1-cli】paper - パフォーマンス集計の接続と拡張

- **目的**: D6 の解消（集計側）。「過去の判断は報われたか」を数値で出す。
- **スコープ**: `paper/journal.py`, `paper/cli.py`, `models.py`
- **主要仕様**:
  - `close` 操作に `exit_reason` を必須化: {stop_loss, target, time_stop, manual, other}。
    既存レコードは `unknown` として移行。
  - `summarize_performance()` を拡張: 勝率、期待値（平均損益）、
    profit_factor（総益/総損の絶対値、損失ゼロ時は None で 0 除算回避）、
    R-multiple（`pnl / ((entry − stop) × shares)`、stop 未記録なら省略し件数を警告）、
    exit_reason 別・戦略別の内訳、既存の SPY 対比。
  - 集計対象はクローズ済みポジションのみ。部分決済は本 Issue の対象外。
- **動作確認**: テストフィクスチャで手計算値（勝率・PF・R-multiple）と一致すること。
  `uv run copilot-history performance`（P1-05 完了後）または一時スクリプトで
  集計が表示されること。
- **Not in scope**: 部分決済（trim）対応、MAE/MFE（P4-20）
- **依存**: なし（P1-05 が本 Issue の表示を利用）

---

### P2-07 【基盤】backtest - リスク調整後指標の追加

- **目的**: D5 の解消（指標側）。エッジの有無を標準指標で判定可能にする。
- **スコープ**: `backtest/engine.py`, `backtest/runner.py`, `models.py`
- **主要仕様**:
  - `BacktestResult` に追加: `sharpe`（日次リターンから年率化、rf=0、√252）、
    `max_drawdown_pct`、`win_rate`、`profit_factor`、`expectancy_per_trade`、
    `avg_r_multiple`、`trade_count`。
  - トレード数警告: <30 は「統計的に不十分（最低30、推奨100+）」、<100 は「予備的」を
    結果オブジェクトの warnings に含める（出典: backtest-expert）。
  - 勝率90%超や最大DDが極小の場合「ルックアヘッド疑い」警告を付す。
  - 小さな合成価格系列による手計算フィクスチャで全指標を検証
    （既存の「手計算 cash/equity」テスト方針を踏襲）。
- **動作確認**: `uv run pytest tests/backtest -q` グリーン。
  手計算フィクスチャの期待値がコメントで検算可能なこと。
- **Not in scope**: CLI（P2-08）、グリッド実行（P2-10）
- **依存**: なし（フェーズ基盤）

### P2-08 【依存あり/worktree:p2-backtest】backtest - CLI エントリポイント copilot-backtest

- **目的**: D5 の解消（実行側)。バックテストを tests 専用から日常道具に昇格させる。
- **スコープ**: 新 `[project.scripts]` エントリ `copilot-backtest`、`backtest/cli.py`（新規）
- **主要仕様**:
  - 引数: `--strategy <name>`（strategies.yaml のキー）、`--start YYYY-MM-DD`、
    `--end YYYY-MM-DD`、`--limit N`（ユニバース制限）、`--output <path.md>`（省略時は
    `reports/backtests/<end>-<strategy>.md`）、`--pessimistic`（P2-09）。
  - 出力: P2-07 の全指標 + 取引一覧 + equity curve サマリを terminal（Rich）と
    markdown（一時ファイル + `os.replace` の既存原子的置換パターン）で出力。
  - 生存者バイアス注記を出力に含める（既存方針の踏襲）。
- **動作確認**: `uv run copilot-backtest --strategy default --start 2025-01-01 --end 2026-06-30 --limit 30`
  が完走し、terminal に指標テーブル、`reports/backtests/` に markdown が生成されること。
  データ不足銘柄が混在しても完走すること（fail-soft）。
- **Not in scope**: ウォークフォワード自動分割（将来）、パラメータ探索（P2-10）
- **依存**: P2-07

### P2-09 【依存あり/worktree:p2-backtest】backtest - 悲観シナリオモード

- **目的**: 「摩擦を増やしても壊れないか」を常時確認できるようにする。
- **スコープ**: `backtest/engine.py`, `backtest/cli.py`, `config.py`
- **主要仕様**:
  - config に `slippage_multiplier`（既定 1.0）を追加。悲観プリセットは 1.75
    （出典: backtest-expert の 1.5〜2.0 帯の中央値、(要検証)）。
  - `--pessimistic` 指定時は通常(×1.0)と悲観(×1.75)を両方実行し、指標の差分表を併記。
  - 両側（entry/exit、強制清算含む）に乗数が効くことをテストで確認。
- **動作確認**: `uv run copilot-backtest --strategy default --start 2025-01-01 --end 2026-06-30 --limit 30 --pessimistic`
  で2列比較表が出ること。悲観側の final_equity が通常側以下であること。
- **Not in scope**: 約定モデルの高度化（板・出来高制約）
- **依存**: P2-07, P2-08

### P2-10 【依存あり/worktree:p2-backtest】backtest - パラメータ感応度グリッドと過学習警告

- **目的**: 「プラトーを探し、ピークを避ける」を機械化し、閾値の妥当性検証装置を作る。
- **スコープ**: `backtest/sensitivity.py`（新規）、`backtest/cli.py`
- **主要仕様**:
  - 対象パラメータ: ATR ストップ倍率 {50, 75, 100, 125, 150}% ×
    最大保有日数 {80, 90, 100, 110, 120}%（基準値比）。将来 P1-01 のランキング重みも対象。
  - 各セルで expectancy_per_trade と trade_count を算出したマトリクスを markdown 出力。
  - 過学習警告: 最良セルの成績が隣接セル中央値の 1.5 倍超なら
    「スパイク（過学習疑い）」、全セル±20%以内なら「プラトー（頑健）」と判定 (要検証)。
  - trade_count < 30 のセルは灰色扱い（結論に使わない）。
  - サブコマンド例: `copilot-backtest grid --strategy default --start ... --end ...`。
- **動作確認**: grid 実行でマトリクス markdown が生成され、判定ラベルが出ること。
- **Not in scope**: ベイズ最適化等の探索高度化（意図的に総当たりのみ）
- **依存**: P2-07, P2-08

### P2-11 【並列可/worktree:p2-feedback】feedback - シグナル・ポストモーテム（フォワードリターン検証）

- **目的**: 過去の候補が実際どうなったかを自動追跡し、シグナルの実力を測る。
- **スコープ**: `pipeline/daily.py`（新ステップ）、`storage/schema.py`（新テーブル
  `signal_outcomes`）、`report/markdown_report.py`
- **主要仕様**:
  - 日次パイプラインで、5営業日前・20営業日前の run の候補について as-of 時点までの
    リターンを計算し分類: |リターン| < 0.5% は NEUTRAL（ノイズ除外）、
    +0.5% 超は TRUE_POSITIVE、−0.5% 未満は FALSE_POSITIVE
    （−0.5〜−2% MILD、−2% 超 SEVERE）。5日は重み60%・20日は重み40%として
    シグナル別の的中集計を保持（出典: signal-postmortem、閾値は (要検証) config 化）。
  - すべて `date <= as_of` の価格のみ使用（look-ahead 禁止）。
  - markdown に「シグナル成績（直近90日）」節: シグナル別 TP/FP/NEUTRAL 件数と的中率。
    サンプル 20 件未満は「暫定」表示。
- **動作確認**: フィクスチャ DB で dry-run し、`signal_outcomes` 相当の行が生成され
  markdown に節が出ること。5/20営業日前に run が無い日はスキップされ壊れないこと。
- **Not in scope**: 的中率に基づく重みの自動調整（人間判断を挟む）
- **依存**: なし（P1-01 のスコア記録があると内訳が豊かになるが必須ではない）

### P2-12 【並列可/worktree:p2-llm】llm - 分析コンテキストと出力スキーマの判断根拠強化

> **現況（P7）**: 完了済み。`llm/decision_context.py` の整形純関数は
> `analysis/context.py` へ移設され現役だが、`catalyst_quality` と
> キャッシュ near-stale 警告は P7 で廃止された。

- **目的**: LLM 出力の曖昧語を減らし、根拠の構造化と定量/定性の役割分担を明確化する。
- **スコープ**: `llm/decision_context.py`, `llm/schemas.py`, `llm/summarize.py`,
  `llm/filings_analysis.py`
- **主要仕様**:
  - decision_context にコード側の定量ブロックを注入: スコア内訳（P1-01）、
    リスク制約（P1-03）、直近実現損益サマリ（P1-06）。
  - 保守的不一致ルールをシステム指示に明記: LLM の定性判断がコードの定量シグナルと
    矛盾する場合、保守側を採択し矛盾自体を両論併記する（定量判定の上書き禁止）。
  - `NewsSummary` に `catalyst_quality` ∈ {high, medium, low, none} を追加。
    判定基準をプロンプトに明記: high = ガイダンス上方修正/beat-and-raise/FDA承認/
    初回の決算加速/大型契約、medium = M&A/製品ローンチ/提携/ショートスクイーズ、
    low = アナリスト格上げのみ/テーマのみ。dilution / secondary offering /
    investigation / lawsuit / resignation / downgrade の検出時は `risk_flags` に必須反映。
    catalyst_quality の根拠も source_ids 必須（既存 provenance 規約）。
  - キャッシュ near-stale 警告: キャッシュ済み分析が TTL まで残り 2 日以内なら
    レポートに警告表示（as_of 基準で計算、壁時計は使わない）。
  - 行動パターン言及規則: 実績値と計画値の差分という具体的根拠がある場合のみ
    「〜の可能性(possible pattern)」表現で言及し、断定的な心理診断を禁止
    （CON-03 検査対象に含める）。
- **動作確認**: フェイク LLM クライアントでスキーマ検証テストが通ること。
  `--skip-llm` なしの dry-run（API キーがある場合のみ）で catalyst_quality が
  出力され、CON-03 検査を通過すること。
- **Not in scope**: 新しい LLM プロバイダ、シナリオ確率分析（swing-copilot には未実装）
- **依存**: P1-01, P1-03, P1-06（注入データ。先行未完なら該当ブロックを段階的に追加）

---

### P3-13 【基盤】regime - レジームモジュール新設（市場ゲートスコア + Distribution Day）

- **目的**: D3 の解消。表示専用だった市場データを判断ロジックに接続する第一歩。
- **スコープ**: 新パッケージ `src/swing_copilot/regime/`（`gate.py`, `distribution.py`）、
  `screening/indicators.py`（EMA 追加）、`storage/schema.py`（regime スナップショット）、
  `config/settings.yaml`
- **主要仕様**:
  - 市場ゲートスコア（決定論・純関数）: 入力 = SPY 終値 vs EMA50、^VIX 終値。
    BULL = SPY > EMA50 かつ VIX < 20、BEAR = SPY < EMA50×0.97 または VIX > 30、
    それ以外 NEUTRAL。閾値はすべて config（出典: canslim-screener の M ゲート、(要検証)）。
  - IBD 式 Distribution Day カウンター（SPY・QQQ 各指数）:
    DD = 前日比 −0.2% 以下かつ出来高が前日超。停滞日（出来高増・値動き +0.1% 未満）は
    0.5 日換算。25 営業日で失効、または DD 当日終値から +5% 上昇で無効化。
    水準: d25 ≤ 2 → NORMAL、d25 ≥ 3 → CAUTION、d25 ≥ 5 または d15 ≥ 3 または
    d5 ≥ 2 → HIGH、d25 ≥ 6 または d15 ≥ 4 → SEVERE（出典: ibd-distribution-day-monitor）。
  - すべて既存 OHLCV ストアから `date <= as_of` で計算。市場指数データが不足する場合は
    `data_quality = INSUFFICIENT` を返し、判定を UNKNOWN とする（欠損時は安全側）。
  - run ごとに RegimeSnapshot を DuckDB へ永続化。
- **動作確認**: dry-run で terminal/markdown にレジーム節（ゲート判定・DD カウント・
  水準）が表示されること。指数データを意図的に欠損させたフィクスチャで UNKNOWN と
  なり例外にならないこと。
- **Not in scope**: エクスポージャー judgement への接続（P3-14）、FTD（P3-16）、
  ブレッド指標（ユニバース全体の MA 上比率等は将来検討）
- **依存**: なし（フェーズ基盤）

### P3-14 【依存あり/worktree:p3-regime】regime/risk/report - Exposure Ceiling 統合

- **目的**: 「今どれだけ張ってよいか」を個別銘柄より先に提示し、サイジングに強制反映する。
- **スコープ**: `regime/exposure.py`（新規）、`risk/checks.py`, `risk/position_sizing.py`,
  `report/daily_brief.py`, `report/*`, `pipeline/daily.py`
- **主要仕様**:
  - 判定: ゲートスコアと DD 水準から
    `NEW_ENTRY_ALLOWED`（BULL かつ NORMAL/CAUTION）、
    `REDUCE_ONLY`（NEUTRAL または HIGH）、
    `CASH_PRIORITY`（BEAR または SEVERE）を決定（マッピングは config、(要検証)）。
  - 欠損時保守則: 入力のいずれかが UNKNOWN なら判定を 1 段階厳しい側へ倒す。
    緩める方向への自動変更は行わない（出典: exposure-coach の安全第一原則）。
  - リスク統合: CASH_PRIORITY では新規候補のサイジングを 0 株 +
    理由 `REGIME_CASH_PRIORITY` で返す。REDUCE_ONLY では max_trade_risk_pct を
    半減し warning を付す。
  - レポート: terminal / markdown / discord の**先頭**（候補一覧より前）に
    Exposure Ceiling ブロック（判定・根拠・データ品質）を表示。
- **動作確認**: フィクスチャで3判定それぞれを再現する dry-run を行い、
  レポート先頭に判定が出ること、CASH_PRIORITY で候補の株数が 0 になり理由が
  表示されることを確認。
- **Not in scope**: 加重平均型の複合エクスポージャー式（複数指標が揃う将来に拡張）
- **依存**: P3-13、P1-03（理由コード表示の枠組み）

### P3-15 【依存あり/worktree:p3-regime】llm - レジームコンテキスト注入と整合性自己点検

> **現況（P7）**: 完了済み。`format_market_regime()` は `analysis/context.py` に
> 残り、注入先が「API の system フィールド」から
> 「`analysis_input.json` の run 単位 `context` フィールド」へ変わった。
> 未信頼テキストと分離するという不変条件は維持されている。

- **目的**: 個別銘柄分析が地合いと矛盾したまま出力されるのを防ぐ。
- **スコープ**: `llm/decision_context.py`, `llm/summarize.py`
- **主要仕様**:
  - `<market_regime>` ブロック（ゲート判定・DD 水準・Exposure Ceiling・データ品質）を
    プロンプトへ決定論的に注入（信頼できるコード側データとしてシステム側フィールドに置く）。
  - 自己点検指示: 各銘柄の interpretation にレジームとの整合性を1文で言及させる。
    レジームと矛盾する強気/弱気の結論には根拠の明示を要求し、
    保守的不一致ルール（P2-12）を適用。
- **動作確認**: プロンプトハッシュのテスト更新。フェイククライアントで
  レジームブロックが user/system の適切なフィールドに分離されていることを検証。
- **Not in scope**: LLM によるレジーム判定そのもの（判定はコードのみ）
- **依存**: P3-13, P3-14, P2-12

### P3-16 【依存あり/worktree:p3-regime】regime - フォロースルーデー（FTD）状態機械

- **目的**: 底打ち後の「再エントリー許可」を日数・出来高・終値ルールで根拠づける。
- **スコープ**: `regime/ftd.py`（新規）、`storage/schema.py`、`report/*`
- **主要仕様**（出典: ftd-detector。優先度は P3 内で最後）:
  - 調整確定: 直近高値から −3% 以上かつ下落日 3 日以上。
  - Day1 = 前日終値超え（または当日レンジ上位 50% で引け）。Day2-3 は Day1 安値
    割れでリセット。FTD = Day4-10 に +1.25% 以上かつ前日超え出来高。
  - 品質スコア: Day4-7 基礎 60 点、Day8-10 基礎 50 点、上昇率 1.25/1.5/2.0% で段階加点、
    SPY・QQQ 両指数同時確認で +15 点。
  - 「FTD 成功率 25%」等の成功率言説は採用しない（一次資料未確認）。
    表示は状態と品質スコアのみ。
  - 状態機械は明示的な状態遷移（純関数 + 状態列挙）で実装し、全遷移をテストする。
- **動作確認**: 2020年3月・2022年等の実データ相当フィクスチャで既知の FTD 相当日を
  検出すること。リセット条件のテスト。dry-run のレジーム節に FTD 状態が出ること。
- **Not in scope**: FTD を自動でエクスポージャー判定に組み込む（表示のみ。接続は
  実績評価後に判断）
- **依存**: P3-13

---

### P4-17 【並列可/worktree:p4-risk】risk - ポートフォリオヒート上限

- **目的**: 候補単体ではなく口座全体の総リスク量を制御する。
- **スコープ**: `risk/checks.py`, `config.py`, `paper/journal.py`（保有ポジション参照）,
  `report/*`
- **主要仕様**:
  - `portfolio_heat = Σ((entry − stop) × shares) / account_equity`
    を保有中ポジション + 承認候補について算出。
  - config `max_portfolio_heat_pct` 既定 6.0%（出典: breakout-trade-planner /
    Minervini の 6-8% 帯の保守側、(要検証)）。超過する候補は
    `PORTFOLIO_HEAT_EXCEEDED` で reject し、現在ヒート値をレポートのリスク節に常時表示。
  - stop 未記録の保有ポジションがある場合はヒート計算不能として
    `not_calculable` + 警告（沈黙のゼロ扱いをしない）。
- **動作確認**: フィクスチャ（保有2銘柄 + 候補3銘柄）で手計算のヒート値と一致し、
  上限超過候補が理由付き reject になること。dry-run のリスク節にヒート%が出ること。
- **Not in scope**: 相関を加味したリスク合算（既存相関チェックと将来統合）
- **依存**: P1-03（内訳表示の枠組み）

### P4-18 【並列可/worktree:p4-data】data/risk - 決算近接ガード（決算カレンダーアダプタ）

- **目的**: 決算跨ぎの二値的・非対称リスクを機械的に警告・回避する。
- **スコープ**: `data/`（新アダプタ、Finnhub earnings calendar 想定）、
  `storage/schema.py`, `risk/checks.py`, `config.py`
- **主要仕様**:
  - アダプタは既存外部境界規約に完全準拠: 明示タイムアウト、有界リトライ、
    全試行へのレート制限、決定論的バックオフのテスト、オフラインテストはフェイク注入。
  - 取得した決算予定日はスナップショットとして保存（取得日時を記録。
    予定日は変更されうるため、rerun は補正を取り込む upsert）。
  - リスクチェック: 次回決算まで 2 営業日以内 → blocking reject
    `EARNINGS_PROXIMITY_BLOCK`、5 営業日以内 → warning `EARNINGS_PROXIMITY_WARN`
    （N は config、既定 2/5、出典: parabolic-short-trade-planner の原則、(要検証)）。
  - 決算日が取得できない銘柄は warning `EARNINGS_DATE_UNKNOWN`（沈黙させない）。
- **動作確認**: フェイクアダプタで 1/2/3/5/6 営業日前の境界テスト。dry-run で
  該当候補に決算警告が表示されること。API キー未設定時は機能全体が
  「無効(理由表示)」として fail-soft すること。
- **Not in scope**: 決算後スコアリング（P5-22）、経済指標カレンダー
- **依存**: なし
- **備考**: Finnhub 決算カレンダー API の無料枠での利用可否を実装前に確認し、
  不可なら yfinance の決算日で代替（精度低下を README に注記）。

### P4-19 【依存あり/worktree:p4-risk】risk - サーキットブレーカーと連敗クールダウン

- **目的**: 「今日は新規建玉してよい日か」という口座レベルの規律ゲートを追加する。
- **スコープ**: `risk/circuit_breaker.py`（新規）、`paper/journal.py`,
  `pipeline/daily.py`, `report/*`, `config.py`
- **主要仕様**（出典: drawdown-circuit-breaker）:
  - 集計対象は**確定済み実現損益のみ**（含み損益は使わない）。
  - 閾値（config、既定は出典値 (要検証)）: 日次実現損失 2.0% → HALTED（翌 ET 営業日まで）、
    週次 5.0% → HALTED（翌月曜 ET まで）、月次 8.0% → HALTED（翌月初 ET まで）、
    直近 2 件連続負け → COOLDOWN（直近負け決済から 24 時間）。
    損益ゼロは「勝ち」扱いで連敗リセット（非対称定義）。
  - 期間境界は America/New_York 基準で as_of から導出（`zoneinfo`、Clock 注入規約に従う）。
  - 複数発火時は HALTED > COOLDOWN > TRADING_ALLOWED の厳格側。発火ルールは全件記録。
  - ジャーナルが空/未整備なら TRADING_ALLOWED + `data_quality=EMPTY_STATE`
    （新規ユーザーをブロックしない）。
  - パイプラインでは HALTED/COOLDOWN でも情報収集とレポートは実行し、
    新規候補に `CIRCUIT_BREAKER_<state>` の reject/warning を付す。レポート先頭の
    Exposure Ceiling ブロック（P3-14）と並べて表示。
- **動作確認**: フィクスチャジャーナル（日次 −2.1% / 連敗 2 件 / 境界ちょうど）で
  3 状態を再現し、dry-run のレポートにバナーが出ること。ET 境界（日付のみの
  タイムスタンプ含む）の境界テスト。
- **Not in scope**: 実弾口座残高との照合（paper journal の口座額を使用）
- **依存**: P1-06（exit_reason・実現損益の整備）

### P4-20 【依存あり/worktree:p4-risk】paper/pipeline - MAE/MFE 日次トラッキング

- **目的**: 「ストップが緩い/利確が早い」を数値で言えるようにする。
- **スコープ**: `paper/journal.py`, `pipeline/daily.py`, `storage/paper_records.py`
- **主要仕様**:
  - 日次パイプラインでオープンポジションの当日高値・安値から
    MAE（最大逆行、≤0 に clamp）/ MFE（最大順行、≥0 に clamp）を更新・永続化。
  - `date <= as_of` の価格のみ使用。データ欠損日はスキップし品質フラグを残す。
  - performance 集計（P1-06）に平均 MAE/MFE を追加し、ヒューリスティック注記:
    平均 MFE が実現損益に対して大きい → 「利確が早すぎる可能性」、
    平均 MAE が大きい → 「ストップが緩い/エントリーが早い可能性」
    （断定せず可能性表現、出典: weekly-performance-digest）。
- **動作確認**: 既知の価格系列フィクスチャで手計算の MAE/MFE と一致。
  dry-run 後に `copilot-history performance` で MAE/MFE が表示されること。
- **Not in scope**: intraday 粒度（日足の高安のみ）
- **依存**: P1-06

---

### P5-21 【並列可/worktree:p5-signals】screening - Minervini トレンドテンプレートシグナル

- **目的**: Stage2 判定を 7 条件に精密化し、条件ごとの充足状況を根拠として出す。
- **スコープ**: `screening/technical_signals.py`, `screening/indicators.py`,
  `config/strategies.yaml`, `data/`（52週高安値・RS 計算に必要な範囲）、
  `pipeline/daily.py`（--strategy 引数追加）
- **主要仕様**（出典: vcp-screener の Minervini Trend Template）:
  - 7 条件: (1) close > SMA150 かつ > SMA200、(2) SMA150 > SMA200、
    (3) SMA200 が 22 営業日以上上昇継続、(4) close > SMA50、
    (5) close ≥ 52週安値 × 1.25、(6) close ≥ 52週高値 × 0.75、
    (7) RS パーセンタイル ≥ 70（ユニバース内、63/126/189/252 日リターンの
    加重 40/20/20/20% で算出 (要検証)）。
  - 合格ライン: 7 条件中 6 以上（config `min_criteria` 既定 6）。
  - `SignalHit.strength = 充足条件数 / 7`、metrics に条件ごとの bool と実値を保持
    → P1-01 経由でレポート到達。
  - `copilot-daily` に `--strategy <key>` 引数を追加する（既定 "default"。
    strategies.yaml に存在しないキーは起動時に fail-fast）。P5 の全戦略の
    動作確認はこの引数を使う。
  - **導入前検証を DoD に含める**: `copilot-backtest` で本シグナル追加戦略 vs default を
    同一期間（最低3年）で比較し、結果の指標表を PR に添付する。結果が悪くても
    導入自体は可（strategies.yaml で無効化可能なため）だが、事実を記録する。
- **動作確認**: --strategy minervini_stage2 を指定して dry-run し、
  候補の根拠列に「6/7 条件」等が表示されること。境界テスト（ちょうど 22 日、
  ちょうど 25% 上、RS=70）。
- **Not in scope**: VCP 検出(P5-24)、RS のセクター内比較
- **依存**: P1-01、P2-08（検証用）

### P5-22 【依存あり/worktree:p5-signals】screening - 決算後 5 因子スコアシグナル

- **目的**: 決算というイベントを「避ける」(P4-18) だけでなく「使う」選択肢を追加する。
- **スコープ**: `screening/technical_signals.py`, `config/strategies.yaml`
- **主要仕様**（出典: earnings-trade-analyzer、重み・閾値は config、(要検証)）:
  - ギャップ算出: BMO 決算 = 当日 open / 前日 close − 1、AMC = 翌日 open / 当日 close − 1。
  - 5 因子加重: Gap 幅 25%、決算前 20 日リターン 30%、出来高比（20d/60d 平均）20%、
    MA200 乖離 15%、MA50 乖離 10%。因子ごとの閾値表でスコア化し、
    A(85+)/B(70-84)/C(55-69)/D(<55) グレード。
  - グレードと因子内訳を metrics に保持しレポート表示。D グレードはシグナル不成立。
  - 導入前検証を DoD に含める（P5-21 と同じ方式）。
- **動作確認**: 決算日フィクスチャ + 価格フィクスチャでグレード境界（84.9/85.0 等）の
  テスト。dry-run で決算後銘柄にグレードが表示されること。
- **Not in scope**: PEAD 週足パターン（将来）、Episodic Pivot
- **依存**: P4-18（決算日データ）、P1-01、P2-08（検証用）

### P5-23 【並列可/worktree:p5-signals】screening - エグゼキューション状態分類（品質と買い時の2軸化）

- **目的**: 「パターンが良い」と「今買ってよい」を分離し、候補を3バケットで提示する。
- **スコープ**: `screening/pipeline.py`, `report/daily_brief.py`, `report/*`
- **主要仕様**（出典: vcp-screener の 2 軸 + state cap 設計。閾値は (要検証) config）:
  - 状態判定（ATR 正規化距離 `d = (close − SMA50) / ATR14` を使用）:
    DAMAGED（d < −3）、PULLBACK_ZONE（−3 ≤ d < 0）、FAIR（0 ≤ d < 2）、
    EXTENDED（2 ≤ d < 4）、OVEREXTENDED（d ≥ 4）。
  - バケット提示: 即検討可（PULLBACK_ZONE/FAIR）、様子見（EXTENDED）、
    見送り（OVEREXTENDED/DAMAGED、スコアに関わらず候補リスト末尾へ降格 = state cap）。
  - レポートは3バケット見出しで候補を分けて表示。状態と d 値を根拠列に出す。
- **動作確認**: d 値境界（−3, 0, 2, 4 ちょうど）のテスト。dry-run で候補が
  バケット別に表示されること。
- **Not in scope**: ピボット価格ベースの状態判定（P5-24 の VCP 導入後に高度化）
- **依存**: P1-01

### P5-24 【依存あり/worktree:p5-signals】screening - VCP 収縮パターン検出（オプション・最後）

- **目的**: 収縮比・出来高 dry-up という検証可能な数値基準でセットアップ品質を測る。
- **スコープ**: `screening/vcp.py`（新規）、`screening/technical_signals.py`
- **主要仕様**（出典: vcp-screener、全閾値 config、(要検証)）:
  - スイング高安値検出は ATR 倍数ジグザグ。収縮列 T1, T2, ... について
    T1 深さ 8-35%（小型株 50% まで許容）、T_{i+1} ≤ 0.75 × T_i、最低 2 回収縮、
    パターン全長 15-325 営業日。
  - dry-up 比 = ピボット手前 10 本平均出来高 / 50 日平均。< 0.30 理想、> 0.70 弱い。
  - ピボット = 最終収縮の高値。ピボット超え +5% 超は追いかけ禁止（候補から降格）。
  - metrics: 収縮回数・各収縮深さ・dry-up 比・ピボット価格。
  - **導入判定を DoD に含める**: P2-11 のポストモーテム様式で過去 2 年の検出時点
    のフォワード成績を集計し、結果を PR に記録した上で既定 off の戦略として追加。
- **動作確認**: 教科書的な VCP を含む合成価格フィクスチャで検出。収縮比 0.76 で
  不成立になる境界テスト。dry-run で VCP 戦略有効時に metrics が表示されること。
- **Not in scope**: 週足での検証、チャート画像出力
- **依存**: P1-01, P2-08, P2-11

---

### P6: 実運用ギャップ修正（2026-07-25 実 API 動作確認より）

P1〜P5 完了後、実 API（yfinance / SEC EDGAR / Finnhub / Anthropic）を使った
動作確認を2セッション実施した。ローカルメモは gitignore 対象のため、
判断に必要な結論は本節に記録する。

**総合診断**: ガードレール機構（fail-soft、キャッシュ再利用と再検証、
provenance 検証、予算ゲート機構そのもの、補正 upsert、原子的置換、
バックテスト一式、P3/P4 のゲート群）はすべて設計どおり動作した。
設計の見直しは不要。修正対象は次の3クラスに限られる:

1. **リグレッション**: fundamentals 抽出がコミット `29ffa2c`（bulk facts API 化）で
   壊れ、net_income 等の 76% が NULL 化 → スクリーニングが候補0件（P6-25）
2. **開示経路だけの境界欠落**: ニュース側には存在する lookback・件数上限・
   ソート・プロンプトへの source_id 明記が、開示側だけ欠けている非対称
   （P6-26, P6-27）。1回の実行で LLM 呼び出し 3,000 回超・provenance 検証
   実効成功率 0% の複合要因
3. **会計・表示の局所バグ**: 応答受信後の検証失敗が cost_usd=0 で記録され
   月次予算上限が素通し（上限 $0.30 に対し実消費 約$1.5 を観測）、
   markdown テーブル分断、株数0の理由誤表示（P6-26, P6-28）

### P6-25 【最優先/worktree:p6-screening】data/screening/storage - fundamentals 抽出リグレッション修正とスクリーニング機能回復

- **目的**: 実データで候補0件になる主因（fundamentals の 76% NULL）を解消する。
- **スコープ**: `data/edgar.py`, `screening/rejection_classifier.py`,
  `storage/market_store.py`, `pipeline/daily.py`
- **主要仕様**:
  - 根本原因: `_group_facts_by_filing` の `fiscal_period_end = max(period_ends)` が
    `dei` タクソノミの表紙 fact（`EntityCommonStockSharesOutstanding` 等、
    期末より 30〜45 日後の日付を持つ）に乗っ取られ、
    `_pick_concept_value` の期末日厳密一致が全 concept で失敗する
    （net_income だけでなく revenue/equity/assets/fcf も同時に NULL 化）。
    期末日算出を `us-gaap` タクソノミの fact のみに限定して修正する。
    `filed_at`（可視性境界）には触れないため as-of 規律への影響はない。
  - 落選 detail の根拠修正: `_classify_fundamentals` の net_income 判定は
    「直近 N 四半期のいずれかが不合格」で落選させた後、最新四半期の値を
    detail に書いている。実際に条件を満たさなかった四半期の
    `fiscal_period_end` と値を記録する。「古い四半期が原因で最新は陽性」の
    回帰テストを追加する（現状このシナリオのテストが存在しない）。
  - 理由コードの分離: net_income が NULL（データ欠損）の場合は
    `FILTER_NEGATIVE_NET_INCOME` ではなくデータ不足系の理由コードに分離する
    （「純損失で落ちた」と「データが無くて判定不能」は別の事実）。
  - fundamentals キャッシュ判定の修正: `has_fundamentals_fetched_on` の呼び出し側が
    `as_of` を渡すため、過去日 `--as-of` では `CAST(fetched_at AS DATE) = day` が
    永久に不成立で毎回フル取得になる。判定を注入 Clock の wall clock 基準に
    改める（as_of は可視性境界、wall time はキャッシュ鮮度、という役割分離。
    AGENTS.md「wall time は metadata」の裏返しの誤用を正す）。
- **動作確認**: dry-run `--limit 150` で fundamentals の NULL 率が激減し
  （post-2011 の非訂正申告で 99% 超が値を持つ見込み）、落選理由が実データに
  基づくこと。同一 `--as-of` での再実行で EDGAR 取得がスキップされること。
- **Not in scope**: Q4 離散値の導出（FY − ΣQ1..Q3）。10-K 行は通期値のまま扱う
- **依存**: なし

### P6-26 【最優先/worktree:p6-llm】llm/storage/data - LLM 予算会計の実支出化と開示取得の境界設定

> **現況（P7）**: 完了済みだが、予算会計・月次上限ゲート・`max_llm_calls_per_run`
> は LLM API 呼び出しの廃止に伴い削除された。生き残ったのは開示取得の境界設定
> （`filing_lookback_days` / `max_filings_per_symbol`）で、`settings.llm.*` から
> `settings.analysis.*` へ移設されている。

- **目的**: NFR-01 の月次予算上限を「実支出に対する保証」にする。
  1回の実行で予算を溶かす構造を止める。
- **スコープ**: `llm/client.py`, `storage/llm_records.py`, `config.py`,
  `config/settings.yaml`, `data/edgar.py`, `text/edgar_filings.py`,
  `pipeline/daily.py`
- **主要仕様**:
  - 会計の実支出化: 応答受信後の検証失敗（SchemaValidationError /
    ForbiddenLanguageError）と refusal の監査記録に `response.usage` 由来の
    実トークン数・実コストを載せる（status は failed のまま）。
    API 例外分岐は response 不在のため現状どおり 0 で正しい。
  - 月次集計の意味論変更: `get_monthly_cost()` の `WHERE status = 'success'` を
    廃し、実際に課金が発生した全行（cost_usd）の合算にする。
    これを直さない限り client 側の記録修正はゲートに反映されない。
  - トークン見積もり係数: `_CHARS_PER_TOKEN_ESTIMATE = 4` は日本語主体
    プロンプトの実測（約 2.0）に対し2倍過小見積もりで、「保守的」の意図と
    逆方向。2.0 に変更しコメントを実態に合わせる。
  - 開示取得の境界: `filing_lookback_days`（既定 90）と
    `max_filings_per_symbol`（既定 3）を config 化し、取得〜LLM 分析の経路に
    通す（ニュース側 `max_news_items_per_symbol` と対称）。
    `fetch_filing_texts` の結果を `filed_at` 降順で決定的にソートする
    （現状は外部ライブラリの返却順に依存し、fundamentals 側の明示ソートと
    非対称）。
  - 実行単位の呼び出し上限: `max_llm_calls_per_run` を config 化し、
    パイプラインの LLM ステップで超過分をスキップ・監査記録する
    （予算ゲートの第二防御。無音の暴走を防ぐ）。
- **動作確認**: オフラインテストで (a) 検証失敗・refusal 時に cost_usd > 0 が
  記録され月次集計とゲートに反映される (b) 開示が 90日・3件・filed_at 降順に
  絞られる (c) 呼び出し上限超過でスキップと監査記録が残ること。
- **Not in scope**: 開示分析のプロンプト修正・レポート反映（P6-27）
- **依存**: なし

### P6-27 【依存あり/worktree:p6-llm】llm/report - 開示分析の provenance 修正とレポート反映・P2-12 残件

> **現況（P7）**: 完了済み。「本文に source_id を明記する」原則と
> 「同一銘柄の全開示を個別に描画する」修正は新しいスキル契約へ引き継がれた
> （`analysis/schemas.py::FilingAnalysis`、`BriefFilingAnalysis`）。
> `catalyst_quality` の表示接続と near-stale 警告は廃止された。

- **目的**: 実効成功率 0% の開示分析を機能させ、生成した分析を捨てない。
- **スコープ**: `llm/filings_analysis.py`, `report/daily_brief.py`,
  `report/markdown_report.py`, `report/terminal_report.py`, `config/settings.yaml`
- **主要仕様**:
  - provenance 修正: `_analyze_chunk` のユーザープロンプトに
    `[source_id: {chunk_source_id}]` を明記する（ニュース側
    `_format_news_item` と同形）。モデルは引用すべき ID を知らされないまま
    「source_ids 必須」と指示され、263 件中 262 件が検証失敗していた。
    回帰テストは「プロンプト本文に source_ids の各要素が含まれること」を
    assert する。プロンプト変更により prompt_hash が変わり既存キャッシュは
    自然に無効化される（想定内）。
  - レポート反映の拡張: 銘柄あたり最初の 1 件のみだった開示分析を、
    `max_filings_per_symbol` 件まで表示する（terminal / markdown 両方）。
  - `catalyst_quality` の表示接続: `BriefLlm` に追加しレポートに表示する
    （表示のみ。ランキングへの定量統合は P6-29 で別途判断）。
  - near-stale 警告: `is_cache_near_stale()` を本番経路に接続し、TTL 残り
    2 日以内のキャッシュ済み分析に警告を表示する（P2-12 未完項目の解消）。
- **動作確認**: オフラインテストに加え、絞った予算上限での実 API 再検証で
  開示分析が provenance 検証を通過し facts が非空であること
  （実 API 実行は事前にユーザー確認を取る）。
- **Not in scope**: catalyst_quality のランキング統合（P6-29）
- **依存**: P6-26（境界設定と会計修正が先）

### P6-28 【並列可/worktree:p6-report】report - 表示の正確性修正

- **目的**: レポートが事実と異なる・壊れた表示をしない。
- **スコープ**: `report/markdown_report.py`, `report/daily_brief.py`
- **主要仕様**:
  - markdown の Candidates テーブル: ヘッダ行・区切り行の直後にバケット見出し
    （### 即検討可 等）が挿入され、データ行が見出しの後ろに孤立して
    テーブルとして描画されない。バケットごとに完結したテーブルを出力する形に
    修正する（P5-23 のバケット節導入時のリグレッション）。
  - 株数 0 の理由表示: `max_shares == 0` で固定文言「資金規模過小」を返す
    単純化をやめ、`binding_constraint` 由来の文言にする（レジーム起因なら
    その旨を表示。DB・LLM プロンプトへは正しい値が渡っており表示層のみの問題）。
- **動作確認**: バケットに候補が分散するフィクスチャで markdown が有効な
  テーブルとしてレンダリングされること。`binding_constraint = regime` の
  候補で表示文言が一致すること。
- **Not in scope**: レポートのレイアウト変更・新節の追加
- **依存**: なし

### P6-29 【obsolete】screening/llm - catalyst_quality のランキング統合検討

> **obsolete（P7）**: `catalyst_quality` フィールド自体が P7 のスキル移行で
> 廃止されたため、本 Issue は検討対象を失った。改修原則 4（判断はコード、
> 叙述はスキル分析）は維持されており、定性シグナルを定量ランキングへ
> 組み込まないという結論も変わらない。将来 `analysis/schemas.py` に
> 同等の任意フィールドを復活させる場合は、新しい Issue を起こす。

- **目的（当時）**: 表示接続（P6-27）の先にある「定性シグナルを定量ランキングに
  組み込むか」の設計判断を行う。
- **実装しない**。改修原則 4 との整合が論点であり、検討結果（採否と理由）を
  本 Issue に記録して閉じる。
- **依存**: P6-27

### P7: 定性分析の Claude Code スキル移行（2026-07-28）

- **目的**: Anthropic API 直呼びの LLM 統合を全廃し、定性分析を Claude Code
  スキルへ移す。判断はコード・叙述はスキル分析という原則を、API 課金・
  予算ゲート・キャッシュという運用負債なしで成立させる。
- **スコープ**: `analysis/`（新設）, `pipeline/daily.py`, `report/daily_brief.py`,
  `report/terminal_report.py`, `report/markdown_report.py`, `config.py`,
  `storage/`, `.claude/skills/swing-daily`（`analyze-news` / `analyze-filings` /
  `interpret-screening`）
- **実施内容**:
  - `llm/` パッケージ・`storage/llm_records.py`・`llm_calls` テーブル・
    `anthropic` 依存・`LLMConfig`/`BudgetConfig`/`ANTHROPIC_API_KEY`・
    月次予算・実行単位の呼び出し上限・応答キャッシュ / near-stale 機構を削除。
  - 日次バッチのステップ 6 を `6_llm` から `6_analysis_export` へ。候補と
    テキストがあれば `analysis_input.json`（strict スキーマ）を日付付き
    レポートディレクトリへ原子的に書き出す。あわせて `report_context.json`
    （`DailyBrief` のスナップショット、schema `report-context-v2`）を保存。
  - 新 CLI `copilot-ingest-analysis` が strict スキーマ検証・provenance 検証
    （`source_ids` ⊆ 入力、facts 非空）・CON-03 機械検査（旧 `llm/safety.py` の
    純関数を `analysis/safety.py` へ移設）を行い、違反銘柄は fail-closed で
    縮退表示（リトライなし）。レポートを再描画する。
  - 銘柄ごとの verdict（`proceed` / `skip`）と run 単位の `no_trade` 表示を追加。
    スクリーニングの決定論的結果は不変。
  - 旧 `llm.*` セクションのテキスト境界設定を `analysis.*` へ移設。
- **意図的な仕様変更**: `catalyst_quality`（REQ-006/007 の触媒の質表示）と
  `guidance_direction` は新契約に含めず廃止した。いずれも表示専用で
  ランキング・リスク判定に接続されておらず、必要になれば
  `analysis/schemas.py` の任意フィールドとして復活できる。
- **監査証跡**: `llm_calls` に代わり、レポートディレクトリの
  `analysis_input.json` / `analysis_result.json` / `report_context.json` が
  そのまま監査アーティファクトになる（NFR-05）。
- **動作確認**: `uv run copilot-daily --dry-run --limit 20` で
  `analysis_input.json` が出力されること。`swing-daily` スキルを実行し
  `uv run copilot-ingest-analysis <dir>` がレポートを再描画すること。
  provenance 違反・CON-03 違反を含む `analysis_result.json` で、当該銘柄だけが
  「検証不合格のため非表示」になり他の銘柄が巻き込まれないこと。
- **依存**: P6-26 / P6-27（同じ境界を扱うため、両者の完了後に実施）

### P8: 振り返り→改善提案機構（2026-07-28）

過去の LLM 定性 verdict の当否を定量計測し、シグナル成績・ソース貢献・
人間判断との突き合わせを統合俯瞰した上で、パラメータ調整（L1）から
構成変更（L2）・設計見直し（L3）までの改善提案を生成・適用する仕組み。
適用は L1 が即時（検証合格 + PR 作成まで）、L2/L3 が設計の
AskUserQuestion 承認後（適用 + PR 作成まで）。いずれも 1 提案 1 PR で
main 直接コミットはせず、「人間判断を挟む」原則は PR マージ（および
L2/L3 の設計承認）に集約する。Python 側には config / コードを書き換える
経路を持たせない。詳細設計と事前確定判断は
`docs/goal-prompts/swing-copilot-retrospective/`（design.md / decisions.md）を
正とする。P7 完了が前提。

### P8-30 【基盤/worktree:p8-retro】storage/retro - verdict 永続化と当否評価

> **現況（P8）**: 完了済み（#71）。`verdicts` / `verdict_sources` /
> `verdict_outcomes` の 3 テーブル、`copilot-retro collect` / `evaluate`、
> `pipeline/forward_returns.py`（逆算 `find_target_trading_day` / 順算
> `find_maturity_trading_day`）の抽出が入った。設計の現況は
> `docs/04_detailed_design.md` 3.23 節を正とする。

- **目的**: LLM verdict を DuckDB に正本化し、forward return で当否を
  決定論的に分類する（P2-11 の verdict 版・観測専用）。
- **スコープ**: `storage/schema.py`（新テーブル `verdicts` /
  `verdict_sources` / `verdict_outcomes`）、`retro/`（新設: `cli.py` /
  `collect.py` / `evaluate.py`）、`pipeline/forward_returns.py`（postmortem
  から純関数抽出）、`pyproject.toml`（`copilot-retro`）
- **主要仕様**:
  - `copilot-retro collect`: `reports/<date>/<run_id>/analysis_result.json` を
    走査し run 単位の完全置換で取り込み。source_type は
    `analysis_input.json`（コード所有側）から解決。
  - `copilot-retro evaluate --as-of`: 満期営業日（run_date + 5/20 営業日、
    `満期日 <= as_of`）の終値で forward return を確定し、verdict 非対称
    分類（proceed: HIT/MISS_MILD/MISS_SEVERE、skip: HIT/NEUTRAL/
    MISS_MILD/MISS_SEVERE）。閾値は `settings.postmortem` を流用。
    `(run_id, horizon_days)` 単位の完全置換で冪等。
  - `signal_outcomes` と postmortem の既存挙動は不変（回帰テストで担保）。
- **動作確認**: フィクスチャの reports/ ディレクトリと DB で collect →
  evaluate を dry-run し、3 テーブルに行が生成されること。再実行で行が
  重複しないこと。bar 欠損銘柄がスキップされ壊れないこと。
- **Not in scope**: 集約・レポート（P8-31）、評価コードによる config /
  コードの書き換え（恒久。適用は P8-33 のスキルが PR 経由で行う）
- **依存**: P7

### P8-31 【依存あり/worktree:p8-retro】retro - 集約とretro_input.json エクスポート

> **現況（P8）**: 完了済み（#72）。`retro/aggregate.py` / `surprises.py` /
> `export.py` と strict スキーマ `retro-input-v1`、`RetroConfig`
> （`max_surprises` / 予約済み `approval_mode`）、`prepare` umbrella が入った。
> 設計の現況は `docs/04_detailed_design.md` 3.23.3〜3.23.4 節を正とする。

- **目的**: 振り返りスキルに渡す証拠 dossier を strict スキーマで出力する。
- **スコープ**: `retro/export.py` / `retro/schemas.py`（`retro-input-v1`）、
  `config.py`（`RetroConfig`: `max_surprises=5` (要検証) 等）
- **主要仕様**:
  - 集約: separation（proceed 群−skip 群の平均 forward return、最重要）、
    proceed 重大外し率（ウォッチ 15% (要検証)・候補全体ベースライン併記）、
    skip 的中率（ベースライン比）、`trades_journal` × verdict クロス集計、
    ソース貢献表（`text_items` join）。サンプル 20 件未満は「暫定」。
  - サプライズ選定: MISS_SEVERE 両方向、上限超過は |return| 降順で切り
    件数明示。各銘柄に当時の verdict・reasons・実現パスと、run 以降の
    鮮度データ（既存 text アダプタで取得、`analysis.*` 予算と timeout/
    retry/rate limit を流用）を同梱。
  - `input_digest`（SHA-256）付与、原子的書き込み。
- **動作確認**: フィクスチャ DB から export し、strict スキーマで
  round-trip できること。サプライズ超過時に切り捨て件数が出力に残ること。
- **Not in scope**: 提案の生成・検証（スキル / P8-32）
- **依存**: P8-30

### P8-32 【依存あり/worktree:p8-retro】retro - retro_result 検証・レポート・提案台帳

> **現況（P8）**: 完了済み（#73）。`retro/ingest.py` / `validate.py` /
> `ledger.py` と strict スキーマ `retro-result-v1` が入った。台帳は再提案
> ガードが完全一致で判定できるよう `proposal_key` 列を持ち、この乖離は
> `docs/04_detailed_design.md` 3.23.7 節に記録した。設計の現況は同 3.23.5〜
> 3.23.6 節を正とする。

- **目的**: スキル出力を信用せず検証し、振り返りレポートと提案台帳へ
  fail-closed で反映する。
- **スコープ**: `retro/ingest.py` / `retro/schemas.py`（`retro-result-v1`）、
  `docs/retro/`（台帳 `proposals.md` + `proposals/RP-NNN-<slug>.md`）
- **主要仕様**:
  - strict 検証・`as_of`/`input_digest` 同一性（不一致は run ごと hard fail）。
  - evidence 参照検証: `evidence_refs` ⊆ 供給した集約 ID・サプライズ ID・
    source_id。CON-03 機械検査（`analysis/safety.py` 流用）。違反は
    当該提案/叙述のみ withhold・リトライなし。
  - 提案は `level`（L1/L2/L3）・`evidence_basis`・`verification_plan`
    （L1/L2 必須、指標・閾値系は `copilot-backtest` の前後比較手順）・
    敗因分類（`information_absent` 等 5 値）を必須フィールドに持つ。
  - ingest の台帳操作は status=proposed の追記のみ。以降の遷移
    （applied(PR#) / rejected / deferred / verification_failed）は適用段階の
    スキルが記録する。rejected / verification_failed と同一 `proposal_key` の
    再提案は `reopen_justification` がなければ差し戻す。
- **動作確認**: 違反入りフィクスチャ result で当該提案だけが withhold され
  他が巻き込まれないこと。再 ingest で台帳が重複追記されないこと。
- **Not in scope**: ingest 自身による config / コードの書き換え（恒久。
  適用は P8-33 のスキルが検証・承認を経て PR で行う）
- **依存**: P8-31

### P8-33 【依存あり/worktree:p8-retro】skill/docs - swing-retro スキルと設計正本昇格

> **現況（P8）**: 完了済み。`.claude/skills/swing-retro/`（SKILL.md +
> `references/proposal-rules.md` / `result-schema.md`）、空の提案台帳
> `docs/retro/proposals.md`（ヘッダが `ingest` の生成物と一致することを
> `tests/retro/test_ledger.py::TestCommittedLedger` が固定）、
> `docs/04_detailed_design.md` 3.23 節への昇格が入った。以降、振り返り機構の
> 設計正本は 3.23 節であり、`docs/goal-prompts/swing-copilot-retrospective/`
> は実装前シードと実行履歴として残す。「動作確認」の実データ 1 周は本フェーズの
> スコープ外（E33.3）で、初回の手動運用時に行う。

- **目的**: 人間が数日おきに手動起動する振り返りスキルを整備し、設計を
  正本へ昇格する。
- **スコープ**: `.claude/skills/swing-retro/`、`docs/retro/` 初期化、
  `docs/04_detailed_design.md`（3.x 節新設）、`docs/reference.md`
- **主要仕様**:
  - スキル手順: `copilot-retro prepare` → dossier + 台帳読み込み →
    サプライズ敗因分析 / シグナル×verdict 突合 / ソース貢献の並列深掘り →
    証拠ゲート判定（L1: n≥20 + 方向一致、L2: n≥40 または敗因反復、
    L3: separation ≤ 0 持続または systemic 欠陥 + 代替案 2 案）→
    `retro_result.json` → `copilot-retro ingest` → ユーザーへ提示 →
    適用: L1 は即時（提案ごとのブランチで config 編集 →
    verification_plan + `just verify` 合格 → PR 作成）、L2/L3 は設計を
    AskUserQuestion で承認後に適用 + PR 作成（規模超過は goal-prompt 化）。
    不合格・却下は台帳に記録して終了。
  - 毎回「L2/L3 相当の構造的観察はないか」を自問し、なければ
    「再点検の上でなし」と明記（細かい調整への偏り防止）。
  - 叙述規約は `swing-daily` の analysis-conventions を流用。
- **動作確認**: フィクスチャ dossier でスキルを 1 周し、レポートと
  proposed 提案が生成されること。L1 適用で検証合格時のみ PR 用ブランチと
  台帳の applied 記録が作られ、不合格時は適用が取り消されること。
  rejected 記録後、同一提案が ingest で差し戻されること。
- **Not in scope**: daily レポートへの Verdict 成績節、`analysis_result` への
  confidence フィールド追加（いずれも本機構の初回運用で提案として検討）
- **依存**: P8-30〜P8-32

## 6. 知見の出典と信頼性

- 出典リポジトリ: `/Users/masuyama/Downloads/claude-trading-skills-main`（ローカルコピー）。
  スキル名は各 Issue の「出典」注記を参照。
- 調査で `(要検証)` と付いた値の例: FTD 成功率 25%（伝聞）、Druckenmiller の数値表
  （作者創作の疑い）、FINRA 規制変更日付（一次資料未確認）、ブレッド閾値
  （単一サイトの独自バックテスト由来）。**方針**: 仕組みは採用し、閾値は config 化して
  既定値に (要検証) を明記、P2 の検証装置で自前データによる裏取りを行う。
- 未調査の補足資料（必要になったら参照）: 同リポジトリの `workflows/*.yaml`
  （decision_gate 付きオーケストレーション定義）、`audits/errata/`
  （実運用で起きた誤判定の記録。スクリーニングのテストケース源として有用）。

## 7. 実施体制

- 各フェーズ = GitHub マイルストーン。Issue には `phase-N` / `area:*` ラベルと
  タイトルプレフィックス（【基盤】/【並列可】/【依存あり】、worktree 提案付き）を付す。
- 実装は Claude Code の /goal による無人セッションを想定し、フェーズ単位で
  ゴールプロンプトを用意する（`~/.claude/goal-prompts/`）。
- 完了報告は必ず `just verify` の結果と「動作確認」手順の実行ログを伴うこと。
