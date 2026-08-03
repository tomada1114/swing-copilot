# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `copilot-backfill`（`pipeline/backfill.py`）。バックテストに必要な過去の
  バー／ファンダメンタルズを一度だけ取り込む一回限りのCLI。日次runは400暦日の
  ローリング窓しか取らないため、複数レジームをまたぐ検証に足る履歴が
  ローカルに存在しなかった。50銘柄チャンク＋チャンク間2秒スリープで取得し、
  `write_bars`は年パーティション全書き直しのコストを避けるため最後に1回だけ
  呼ぶ。既存バーが`--start`以前まで届いている銘柄はネットワークを叩かずに
  スキップし、銘柄単位の失敗はfail-softで集約報告する。
  **ベンチマーク／レジーム系（`SPY`・`QQQ`・`^VIX`・`^TNX`）はS&P 500
  ユニバースに含まれないため`--symbols`で別途取得が必要**——特に`SPY`は
  バックテストの取引日カレンダーそのものである
- 低ボラバイアス是正の2つのスイッチ（いずれも**既定では無効**で、既定挙動は
  不変）。`technical_signals.pullback.band_atr_multiple`（既定`null`）は
  SMA50からの距離をATR14単位で測るモードで、絶対3%帯が低ボラ銘柄を高ボラ銘柄の
  約4.5倍通過させていた事実上のローボラフィルタを解消する。ATRがNaNまたは0の
  ときは距離が定義できないため帯を閉じる。`ranking.score_weights.atr_pct`
  （既定`0.0`）はATR%が高いほど高得点の成分で、ATR% 6%満点の絶対正規化
  （候補n≈5のパーセンタイルは小標本ノイズを再生産するため採らない）
- バックテストレポートに`Exit breakdown`セクション。決済理由の内訳
  （発火0件の理由も0として必ず表示）、`max_hold`バインド率、実保有日数の
  中央値と四分位。感応度グリッドのMaxHold列が全て同値だったとき「効かない」のか
  「一度も発火していない」のかを区別するための計器。`Trade.days_held`を追加
- `copilot-backtest`の`--settings` / `--strategies`。リポジトリの設定を
  書き換えずに設定バリアントを比較するための入り口

### Changed

- 感応度グリッドの`MAX_HOLD_PCT_GRID`を基準値比`(80,90,100,110,120)%`から
  `(40,70,100,140,200)%`へ拡張。ATR軸が±50%を探索するのに時間軸だけ±20%という
  非対称では、そのパラメータが効かないのか振り足りないのかを区別できない

- factに逐語引用`evidence_quote`を必須化し、ingestが引用元本文との一致を機械
  検証する。`source_ids`は「そのIDが当該銘柄に供給されている」ことしか証明せず、
  別銘柄の本文を読みながら自分の正しい`source_id`を申告したfactは検証を通過して
  いた。`evidence_quote`が、そのfactが引用する`source_ids`のいずれかの本文
  （ニュースは見出し＋要約、開示は入力の`text`、カレンダーイベントはタイトル＋
  要約）に存在しない銘柄はfail-closedで縮退し、リトライしない。照合はNFKC・
  引用符/ダッシュ畳み込み・空白圧縮・大小無視のうえで行うため表記ゆれは通るが、
  言い換えは通らない。`analysis_result.json`のスキーマは`analysis-result-v3`
  （旧`analysis-result-v2`はP8アーカイブ読み込みのみ後方互換）
- 開示の章coverageに欠落量と欠落位置を追加。`FilingSectionCoverage`が
  `original_chars` / `exported_chars` / `omission_shape`（`head_only` /
  `head_and_tail`）を任意フィールドとして持ち、`partial`の章が「どれだけ・
  どこが落ちたか」を伝える。先頭＋末尾を残す切り詰めでは未分析なのは中間で
  あり、分析スキルはそれを具体的に明示する。スキーマは`analysis-input-v3`
  据え置き（追加のみ・後方互換）
- `analysis-input-v3`に開示coverageを追加し、10-Q/10-Q-Aを財務諸表・MD&A・
  リスク要因・法的手続の章優先で120,000字へ構成。銘柄合計240,000字の
  コンテキスト予算と、P8で重大外しとの併存を切り分けるcoverage集計も追加
- Reliability phase 1 (judgment-basis visibility and numeric robustness):
  composite screening scores with a per-candidate breakdown
  (`rsi_pullback`/`trend_quality`/`liquidity`, configurable via
  `strategies.yaml`'s `ranking.score_weights`); a `screening_rejections`
  ledger recording why each non-candidate symbol was screened out, with a
  reason-code summary in terminal/markdown reports; explicit position-sizing
  binding constraints (`shares_by_risk`/`shares_by_position_cap`/
  `binding_constraint`) plus `WIDE_STOP`/`SMALL_ACCOUNT_FRICTION` warnings;
  exact `fractions.Fraction`-based share-count floor arithmetic and a
  common NaN/Inf write guard (`storage/json_guard.py`) for every JSON
  column under `storage/`; `PaperJournal.summarize_performance()` extended
  with win rate, expectancy, profit factor, average R-multiple, and
  exit-reason/strategy breakdowns (`Position.exit_reason` is now required
  on close); and a new read-only `copilot-history` CLI (`runs`, `run
  --run-id`, `symbol`, `rejections --run-id`, `performance`) plus a "過去判断"
  (past decisions) section in the daily Markdown report.
- `copilot-daily` CLI (`uv run copilot-daily [--as-of] [--dry-run]
  [--skip-text] [--skip-llm] [--limit N] [--no-open]`), wiring all nine
  daily-batch steps: price/fundamentals/screening/risk (fatal on
  failure), text collection and LLM analysis (fail-soft, degrade the
  run without aborting it), report generation, Discord notification,
  and local browser auto-open
- `report/html_report.py` + `templates/report.html.j2` +
  `reports/assets/style.css`: the daily Morning Briefing report —
  market strip, risk warnings, ranked candidate table, and per-symbol
  detail cards (TradingView Lightweight Charts v5, fundamentals, risk
  sizing, a fail-soft LLM summary block), written atomically to
  `reports/{run_date}.html` and `reports/latest.html`
- `report/discord_notify.py`: optional Discord webhook notification
  (`notification.enabled`), never raising — a failed send degrades the
  run instead of stopping it
- `paper/journal.py`: paper-trading decision log (idempotent
  `record_decision`), position lifecycle (`close_position`, rejecting a
  missing/already-closed position instead of a silent no-op), and
  `summarize_performance()` (closed-trade P&L/win-rate vs. a SPY
  buy-and-hold benchmark over the same span)
- `MarketStore.get_latest_fundamentals()`, `StateStore.get_position()`/
  `get_closed_positions()`/`record_trade_decision()`/
  `record_text_items()`/`get_source_urls()`: report- and paper-trading-
  oriented storage queries
- Report risk block now renders 想定リスク（対資金） and 1銘柄上限比
  (both degrade to N/A without a configured account equity)
- `PaperJournal.record_decision()` accepts an optional `position_id`, so a
  recorded decision can be linked to the paper position it resulted in
  (completing FR-11's signal-to-decision-to-fill-to-P&L traceability via
  a correction re-record on the same natural key)
- Initial project structure
- `scripts/bootstrap.py` deterministic template initializer: renames the
  package and replaces every placeholder (`swing-copilot`, `swing_copilot`,
  `tomada1114`, `tomada`, `tmasuyama1114@gmail.com`) across tracked files
- Python 3.14 support in the CI test matrix and trove classifiers
- `zizmor` security lint for GitHub Actions workflows, wired into both CI
  and pre-commit
- `actions/dependency-review-action` on pull requests
- Weekly `pip-audit` dependency vulnerability scan
- Weekly OpenSSF Scorecard analysis
- PR auto-labeling by Conventional Commit type, so the release changelog
  categories actually populate
- `.devcontainer/devcontainer.json` for a ready-to-use dev environment
- `.github/ISSUE_TEMPLATE/config.yml` disabling blank issues and linking
  security reports to GitHub Security Advisories
- Dependabot cooldown and `tool.uv.exclude-newer` supply-chain cutoff,
  documented in `.claude/rules/pyproject.md`
- `AGENTS.md` as the canonical, tool-agnostic agent guide (previously a
  symlink to `CLAUDE.md`, which breaks on Windows checkouts)
- `.claude/hooks/guard.py` PreToolUse guard blocking writes to
  `uv.lock`/`.env*`/`secrets/**` (via Edit/Write or shell commands),
  `git commit --no-verify`, and plain force-pushes
- `.claude/hooks/stop_check.py` Stop-hook gate running ruff (lint + format
  check) and mypy before an agent turn ends when Python files changed
- Committed Claude Code permission allowlist covering local build, lint,
  and test commands only — commit/push/PR creation stay behind approval

### Changed

- Daily-analysis skills now require reusable expert fragments to retain the
  source input identity and use per-symbol, read-only input slices for
  large qualitative-analysis inputs.
- Moved coverage enforcement (`--cov-fail-under=80`) out of pytest
  `addopts` and into `just test` / CI, so a single test can be run in
  isolation without failing the coverage gate
- Restructured the release pipeline: a dedicated `build` job now builds
  and attests provenance once; `publish` and the GitHub Release both
  consume that artifact instead of rebuilding
- Scoped all workflow permissions to job level, added `timeout-minutes`
  to every job, added `--locked` to every `uv sync` in CI, and disabled
  checkout credential persistence outside the docs deploy job
- Simplified `src/swing_copilot/__init__.py`'s version resolution to the
  standard `importlib.metadata.version()` pattern, dropping the ~50-line
  local-pyproject-walking fallback chain
- Replaced the bespoke `no-commit-to-main` pre-commit hook with the
  pre-commit-hooks builtin `no-commit-to-branch`
- Unified mypy targets (`src scripts tests`) across justfile, CI,
  release, and pre-commit
- Expanded ruff rule set (`D`, `PT`, `N`, `TRY`, `EM`, `DTZ`, `RSE`,
  `PGH`) to match `.claude/rules/python.md`; renamed `TCH` -> `TC`
- The post-edit format hook now formats only the edited Python file and
  surfaces failures to the agent, replacing the repo-wide ruff run that
  suppressed all errors
- `CLAUDE.md` is now a thin `@AGENTS.md` import plus Claude Code
  specifics; `.claude/rules/python.md` no longer restates rules ruff
  already enforces mechanically
- `just fmt` now runs `ruff check --fix` before `ruff format` (ruff's
  recommended order, matching the post-edit hook), so lint autofixes can
  no longer leave formatting drift behind

### Fixed

- 日次runの「保有銘柄」が実オープンポジション（`positions`）だけを見ており、
  実売買前で常に0行のため保有銘柄のニュース・開示収集が一度も発火していなかった
  問題を解消。`pipeline/daily_runner.py::_held_symbols()`が実オープンポジションと
  verdict追跡台帳`verdict_positions`の`status='open'`な仮想建玉の**和集合**を
  保有集合とする。Finnhubの`company-news`は遡及取得できないため、この欠落は
  その都度恒久的なデータ損失になっていた。影響するのは収集・分析の対象集合だけで、
  リスクチェック（サイジング・集中度・相関）へ渡すポートフォリオは従来どおり
  実ポジションのみ。`--as-of`の再現runは台帳を読まず（現在状態であり時点再現性が
  無いため）、台帳の読み取り失敗は警告のみのfail-soft
- ニュース枠に関連度の選別が無く、対象銘柄への言及が名目的なだけの記事
  （セクター横断記事・他社記事・定型マーケットサマリ）が判断価値のある記事を
  `max_news_items_per_symbol`の枠から押し出していた問題を解消。
  `text/news_finnhub.py`がFinnhub応答の`related`/`category`を
  `TextItem.related_symbols`/`TextItem.category`へ正規化して保持し、
  `analysis/export.py`が関連ティッカーに対象銘柄を含まない記事を**降格**する
  （除外ではないため、関連記事が少ない銘柄でも枠が空かない）。関連ティッカーの
  宣言が無い記事は降格しない。`analysis_input.json`のスキーマは変更せず、
  関連度は`news[]`の順序としてのみ伝わる
- `analysis_input.json`の`context.calendar_events[]`で`title`と`summary`が
  リリース名の重複になっていた問題を解消。`text/calendar_fred.py`が
  `fred/release/series` → `fred/series/observations`のAPI連鎖で代表系列の
  直近実績値・前回値・差分を要約に載せる。観測値は`as_of`以前に限り
  （境界は含む）、全リクエストを120 req/minでスロットルし、値取得は
  リリース単位のメモ化と上限20件で有界化。取得失敗はイベントを落とさず
  欠落理由を明示した要約へ縮退する。市場予想はFREDに存在しないため
  発明せず不在を明示する
- `copilot-daily --as-of` no longer leaks current open positions or
  currently known Finnhub earnings dates into historical replays; both
  unavailable point-in-time sources now fail soft with explicit notices,
  while normal runs keep using current state
- Screening rejection detail no longer reports a raw NaN `net_income` (a
  real EDGAR data gap) as a non-finite JSON value — it now reports `null`,
  matching the existing `fcf`/`equity_ratio` convention; the raw NaN
  previously reached the new NaN/Inf write guard and made
  `copilot-daily --dry-run` fail at the screening step for affected symbols
- `Database.connect()` now forces the DuckDB session `TimeZone` to UTC —
  `TIMESTAMPTZ -> DATE` `as_of` boundary casts previously used the host
  machine's local timezone, which could include or exclude a filing
  near a UTC-midnight boundary depending on where the batch ran
- The price step now also fetches the market strip's fixed index
  symbols (SPY/QQQ/^VIX/^TNX), which are never S&P 500 constituents and
  so previously never got bars written for the report's market strip
- Steps 5/6 (text collection, LLM analysis) no longer discard every
  already-collected symbol's result when one symbol/candidate fails —
  each is isolated per-symbol, degrading the run instead of losing the
  successful ones
- Text/LLM target symbols now include held positions, not only today's
  screening candidates, capped at 30 per NFR-03
- `StateStore.get_closed_positions()` takes an `as_of` cutoff, and
  `summarize_performance()` passes it, so a position closed after the
  summary's `as_of` no longer leaks into the performance summary
- `record_trade_decision()`'s correction upsert no longer overwrites the
  original `created_at` audit timestamp
- `_atomic_write()` (HTML report writer) now removes its temp file on a
  write failure instead of leaving it behind
- Fundamentals block's EPS no longer depends on a valid close price
  (only PER does); a missing close previously hid a computable EPS
- LLM summary block no longer renders a single-item interpretation's
  sentence twice (once as the conclusion, once as its own reason)
- Discord webhook notification now retries transport errors and HTTP
  429/5xx up to 3 total attempts with deterministic backoff, instead of
  giving up after one attempt; a non-retryable 4xx still fails fast
- Switched to PEP 639 license metadata (`license-files`, dropped the
  redundant OSI trove classifier)
- `CONTRIBUTING.md`'s manual mypy command now includes `tests`, matching
  justfile/CI/pre-commit
- The `create-pr` skill re-checks the working tree after `just check` so
  formatting changes cannot be left uncommitted behind a green checklist
- EDGAR fundamentals extraction no longer lets a filing's `dei` cover-page
  fact (e.g. `EntityCommonStockSharesOutstanding`, dated weeks after the
  actual fiscal period) hijack the derived `fiscal_period_end`, which
  previously made every `us-gaap` financial concept's exact-match lookup
  fail and every metric (`net_income`, `revenue`, `fcf`, `equity`, `assets`)
  come back `null` for many real filings, starving screening of candidates
- Screening rejection detail for a `net_income` filter failure now reports
  the actual quarter that failed the `> 0` check (with its `fiscal_period_end`)
  instead of always the latest quarter, which could misreport a healthy
  latest quarter's value when an older quarter was the real offender; a
  `NaN` (missing) `net_income` is now classified as the new
  `DATA_MISSING_NET_INCOME` reason code (`data_quality` stage) instead of
  being reported as a business rejection under `FILTER_NEGATIVE_NET_INCOME`
- The daily fundamentals step's same-day-rerun skip now compares against
  the injected `Clock`'s wall-clock date instead of `--as-of`; a past
  `--as-of` previously never matched `fetched_at` and forced every rerun to
  refetch every symbol's fundamentals over the network
- Filing analysis prompts now state each chunk's `source_id` in the user
  prompt body (matching news summarization's existing convention); the
  model previously had to guess which ID to cite in `facts[].source_ids`
  and almost always fabricated one, failing provenance validation for
  262 of 263 real filing analyses in production
- The daily report now shows every filing analysis for a candidate,
  individually labeled by filing type and filed date — previously only
  the first filing analysis per symbol reached the report, silently
  dropping any second or later filing (e.g. an 8-K following a 10-Q)
- `catalyst_quality`/`catalyst_quality_source_ids` (added for provenance
  validation only) now render in the terminal/Markdown report as
  display-only information; they still never feed screening/risk/ranking
  logic
- The cache "near-stale" warning mechanism is now wired into the daily
  report: a filing/news analysis served from a cache entry within
  `llm.cache_ttl_days` (new setting) of `near_stale_threshold_days`
  remaining now surfaces a re-run warning instead of being silently reused

[Unreleased]: https://github.com/tomada1114/swing-copilot/commits/main
