# 実装用リサーチノート（検証済みコードアンカー）

P8-30〜P8-33 の各ゴールセッション向け。行番号は 2026-07-29 時点の main。
ずれていても関数名・パターンは有効。ここに書いた事実がリポジトリ実体と
食い違う場合はリポジトリを正とし、乖離を最終報告に記録すること。

## R0. 環境・ベースラインの検証済み事実

- main の CI（CI / Docs 両ワークフロー）は 2026-07-29 時点で green。
  最新状態は `gh run list --branch main --limit 5` で再取得すること。
- `gh auth status` は tomada1114 アカウントで認証済み（repo/workflow scope）。
  origin = `https://github.com/tomada1114/swing-copilot.git`。
- run 出力ディレクトリの実装: `src/swing_copilot/pipeline/daily.py:897`

  ```python
  def _run_output_dir(deps: DailyDependencies, run_date: date, run_id: UUID) -> Path:
      return Path(deps.output_dir) / run_date.isoformat() / str(run_id)
  ```

  `analysis_input.json` はここへ書かれる（daily.py:954、
  `analysis/export.py:142` `write_analysis_input`）。design §2 の
  `reports/<date>/<run_id>/analysis_result.json` という前提はこのコードと整合。
- **重要**: 現在のローカル `reports/` に `analysis_result.json` は 1 件も
  実在しない（`find reports -iname "analysis_result*"` が空）。既存の
  `reports/2027-03-01/<uuid>.md` 等は per-run Markdown アーカイブのみ。
  → collect は「走査 0 件」を正常系として扱い、テストは tmp ディレクトリに
  フィクスチャを作って検証する。

## R1. forward return / カレンダー（P8-30）

- `src/swing_copilot/pipeline/postmortem.py:171` `_find_target_trading_day`
  — `as_of` から horizon 営業日**前**の逆算専用。カレンダーは専用モジュール
  ではなく benchmark シンボルの bar 日付の distinct 集合から導出:

  ```python
  bars = market_store.read_bars([benchmark_symbol], start, as_of, as_of)
  trading_days: list[date] = sorted(bars["date"].unique().tolist())
  ```

  窓幅は `(horizon_days + _CALENDAR_WINDOW_PADDING_DAYS) * _CALENDAR_WINDOW_MULTIPLIER`
  （postmortem.py:193-195）。`backtest/runner.py` の `_trading_days()` と
  同鏡像である旨 docstring に記載（:178）。
- `src/swing_copilot/pipeline/postmortem.py:206` `_compute_forward_return`

  ```python
  def _compute_forward_return(
      market_store: MarketStore, symbol: str, run_date: date, as_of: date
  ) -> float | None:
      ...
      return (as_of_close - run_close) / run_close * 100
  ```

- `compute_signal_performance` は postmortem.py:106（P8-31 が出力を同梱する）。

## R2. ストレージのパターン（P8-30）

- DDL 定義: `src/swing_copilot/storage/schema.py:23` `INIT_SCHEMA_STATEMENTS`
  （リスト）+ 追加マイグレーション用 `ALTER_SCHEMA_STATEMENTS`。適用は
  `StateStore.init_schema`（`storage/state_store.py:79`、両リストを順に実行）。
- DDL 記法の手本（schema.py:159 `trades_journal`）:

  ```sql
  CREATE TABLE IF NOT EXISTS trades_journal (
      journal_id UUID PRIMARY KEY,
      run_id UUID NOT NULL,
      ...
      decision VARCHAR NOT NULL CHECK(decision IN ('followed','ignored','modified')),
      UNIQUE (run_id, symbol, strategy_key)
  )
  ```

- 完全置換 + 単一トランザクションの手本:
  `src/swing_copilot/storage/audit_records.py:237` `replace_signal_outcomes`
  — `BEGIN TRANSACTION` → `DELETE FROM ... WHERE run_id=? AND horizon_days=?`
  → 行 INSERT ループ → 例外時 `ROLLBACK` else `COMMIT`。
  `StateStore.replace_signal_outcomes`（state_store.py:642）は薄い委譲。
- 失敗注入テストの手本:
  - `tests/storage/test_state_store.py:762`
    `test_rolls_back_entirely_when_a_later_hit_has_a_non_finite_metric`
    — 2 行目で raise → 再接続して `SELECT count(*)` が 0 を検証。
  - `tests/storage/test_state_store.py:828` `_FlakyRejectionConnection`
    — 実接続をラップし N 回目の INSERT で RuntimeError を注入。

## R3. analysis/ 境界のパターン（P8-31 / P8-32）

- パッケージ構成: `__init__.py, cli.py, context.py, export.py, safety.py,
  schemas.py, snapshot.py, validate.py`。
- strict スキーマ基底（`analysis/schemas.py:76`）:

  ```python
  class _StrictModel(BaseModel):
      model_config = ConfigDict(extra="forbid")
  ```

- 原子的書き込み: `analysis/export.py:163` `write_json_atomically`、
  ファイル名定数 `ANALYSIS_INPUT_FILENAME` / `ANALYSIS_RESULT_FILENAME`
  （export.py:48-49）。
- 検証側: `analysis/validate.py:116` `load_analysis_input` /
  :132 `load_analysis_result` / :148 `validate_analysis` /
  :190 `validate_artifact_identity`（as_of・digest 同一性検証の手本）。
- CON-03 中央検査: `analysis/safety.py:183`

  ```python
  def check_display_texts(texts: Iterable[str]) -> None:
      materialized = list(texts)
      check_no_imperative_language(materialized)
      check_no_unevidenced_behavioral_claims(materialized)
  ```

- CLI 構造の手本（`analysis/cli.py`）: `_parse_args`(:51) →
  `_resolve_paths`(:87) → `ingest`(:98) → `main`(:162)。
- テスト構成の手本: `tests/analysis/` =
  `conftest.py, test_cli.py, test_context.py, test_export.py, test_safety.py,
  test_schemas.py, test_skill_contract.py, test_snapshot.py, test_validate.py`。

## R4. config のパターン（P8-31）

- `src/swing_copilot/config.py:338-363` `PostmortemConfig(_StrictModel)`:

  ```python
  horizon_5d_weight: float = Field(default=0.6, ge=0.0)
  horizon_20d_weight: float = Field(default=0.4, ge=0.0)
  neutral_threshold_pct: float = Field(default=0.5, ge=0.0)
  severe_threshold_pct: float = Field(default=2.0, ge=0.0)
  preliminary_sample_threshold: int = Field(default=20, ge=1)
  lookback_window_days: int = Field(default=90, ge=1)
  ```

  末尾に `@model_validator(mode="after")` のクロスフィールド検証あり。
- `Settings`（config.py:392-404）へのぶら下げ:
  `postmortem: PostmortemConfig = PostmortemConfig()` の並び。
  `RetroConfig` は `RegimeConfig` の後・`Settings` の前に定義し、
  `Settings` には `regime:` の次の行に `retro: RetroConfig = RetroConfig()`。

## R5. エントリポイント（P8-30）

`pyproject.toml:38-43` の現状:

```toml
[project.scripts]
copilot-daily = "swing_copilot.pipeline.daily:main"
copilot-decision = "swing_copilot.paper.cli:main"
copilot-history = "swing_copilot.report.history_cli:main"
copilot-backtest = "swing_copilot.backtest.cli:main"
copilot-ingest-analysis = "swing_copilot.analysis.cli:main"
```

ここへ `copilot-retro = "swing_copilot.retro.cli:main"` を 1 行追加する。
依存追加はどのフェーズにもないため `uv.lock` は変化しないはず。

## R6. テスト全般

- `tests/conftest.py:19-27` autouse ソケットガード:

  ```python
  @pytest.fixture(autouse=True)
  def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
      def blocked_connect(*_args: object, **_kwargs: object) -> NoReturn:
          msg = "Real network access is forbidden in the test suite"
          raise AssertionError(msg)
      monkeypatch.setattr(socket.socket, "connect", blocked_connect)
  ```

- postmortem 回帰: `tests/pipeline/test_postmortem.py`（P8-30 の抽出後も
  全 green であること）。

## R7. text アダプタ入口（P8-31 鮮度データ）

- ニュース: `src/swing_copilot/text/news_finnhub.py:75`

  ```python
  def fetch_company_news(self, symbol: str, since: date, *, as_of: date) -> list[TextItem]:
  ```

- `text_items` テーブルは `storage/schema.py:173` に実在
  （ソース貢献表の join 先。design §5.3 項目 4）。
- 開示: `src/swing_copilot/text/edgar_filings.py:49`

  ```python
  def fetch_recent_filings_text(
      edgar_client: _EdgarClientLike, symbol: str,
      form_types: list[str], as_of: date, bounds: FilingLookbackBounds,
  ) -> list[TextItem]:
  ```

## R8. スキル・ドキュメント（P8-33）

- `.claude/skills/swing-daily/` = `SKILL.md` +
  `references/output-schema.md` + `references/analysis-conventions.md`。
  swing-retro はこの構成を手本にする。
- `docs/04_detailed_design.md` の 3.x 節は現在 3.22
  （`report/history_cli.py`、line 1252）が最後 → retro 昇格節は **3.23**。
- `trades_journal` 列（schema.py:159-170）: journal_id / run_id / symbol /
  strategy_key / position_id / decision(followed|ignored|modified) /
  reason_memo / virtual_fill_price / created_at、
  UNIQUE (run_id, symbol, strategy_key)。
