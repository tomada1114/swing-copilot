# API Reference

::: swing_copilot

## スクリーニング戦略

日次実行では`--strategy`で`config/strategies.yaml`に定義した戦略を選択できる。
既定値は`default`であり、引数を省略しても従来のシグナル構成は変わらない。

```bash
copilot-daily --strategy default
copilot-daily --strategy minervini_stage2
copilot-daily --strategy vcp_breakout
```

`minervini_stage2`は七つのStage 2条件の充足数、`vcp_breakout`は収縮数・
出来高dry-up・pivot距離を候補レポートへ出力する。候補はエントリーからの
距離に応じて「即検討可」「様子見」「見送り」へ分類される。各しきい値と
strategy定義は`config/settings.yaml`および`config/strategies.yaml`を正とする。

## 口座レベルリスク

`risk/checks.py`の`calculate_portfolio_heat()`は、保有中ポジションと承認候補の
stopリスクを口座資産に対する百分率で計算する。`RiskChecker.check()`は候補を
ランキング順に評価し、`risk.max_portfolio_heat_pct`を厳密に超える候補を
`PORTFOLIO_HEAT_EXCEEDED`で拒否する。stop未記録の保有がある場合は
`not_calculable`となり、0リスクとして承認しない。

決算近接ガードは`EarningsCalendarClient` Protocolから取得した次回予定日を使い、
2営業日以内を`EARNINGS_PROXIMITY_BLOCK`、5営業日以内を
`EARNINGS_PROXIMITY_WARN`とする。予定不明は`EARNINGS_DATE_UNKNOWN`を明示し、
Finnhubキー未設定時はガード全体を`NO_EARNINGS_DATA`として無効化する。

`risk/circuit_breaker.py`はクローズ済みペーパーポジションの実現損益だけを使い、
米東部時間の日次・週次（月曜開始）・月次境界で毎回再計算する。既定では損失率が
2%/5%/8%に達すると`HALTED`、2連敗後は最後の負け決済から24時間
`COOLDOWN`となる。両状態とも候補へ`CIRCUIT_BREAKER_<state>`を付けて拒否するが、
市場データ収集とレポート生成は止めない。空の履歴は
`TRADING_ALLOWED (EMPTY_STATE)`、欠損決済時刻・損益は安全側の
`HALTED (PARTIAL)`となる。

## MAE/MFE

`paper/excursions.py`の`update_position_excursions()`は、オープン中および
当日クローズしたペーパーポジションを対象に、`entry_date <= date <= as_of`の
日足高安だけから1株あたりドル幅を算出する。MAEは`min(0, low-entry)`、
MFEは`max(0, high-entry)`へclampし、`position_id + as_of_date`で
correction-upsertする。当日バー欠損は0扱いせず、過去の極値を保ったまま
`MISSING_BAR`を保存する。

`PaperJournal.summarize_performance()`はクローズ済み取引だけを株数換算し、
`avg_mae_usd`と`avg_mfe_usd`を返す。平均excursionの絶対額が平均実現損益の
絶対額より大きいときだけ、利確時期またはストップ/エントリーに関する
可能性表現の注記を返す。

## LLM予算会計と開示取得の境界（NFR-01、roadmap §5 P6-26）

`llm/client.py`の`LLMClient.analyze()`は、応答受信後の検証失敗
（`SchemaValidationError`/`ForbiddenLanguageError`）や`stop_reason=="refusal"`
でも、Anthropic側が既に課金した`response.usage`由来の実トークン数・実コストを
`llm_calls`へ記録する（`status`は`"failed"`のまま）。SDK呼び出し自体が例外を
送出した場合のみ応答が存在しないため0のまま記録する。
`storage/llm_records.py::get_monthly_cost()`はこの実コストを`status`を問わず
全行合算するため、NFR-01の月次予算上限は「実際に課金された金額」に対する
保証になる（以前は`status='success'`のみ合算しており、失敗呼び出しの実消費が
ゲートから見えなかった）。予算ゲートの事前概算に使う文字/トークン比
（`_CHARS_PER_TOKEN_ESTIMATE`）は日本語主体プロンプトの実測値`2.0`を使う。

開示取得（`data/edgar.py::EdgarClient.fetch_filing_texts()`）は
`settings.llm.filing_lookback_days`（既定90日）・`max_filings_per_symbol`
（既定3件、ニュース側`max_news_items_per_symbol`と対称）で絞り込み、
`filed_at`降順に決定的ソートしてから件数上限を適用する。加えて
`settings.llm.max_llm_calls_per_run`（既定200）が1回の実行内の総LLM呼び出し数を
上限し、超過分は実API呼び出しへ到達させず`"budget_skipped"`として監査記録する
（月次予算ゲートとは独立した第二防御）。

## 開示分析のprovenance修正・レポート反映・near-stale警告（roadmap §5 P2-12/P6-27）

実API検証で、開示分析263件中262件がprovenance検証で失敗していた
（唯一の「成功」もfactsが空）。`llm/filings_analysis.py`のユーザープロンプトが
`source_id`をモデルへ一切明示していなかったため、モデルが引用すべきIDを
知らずに文字列を捏造し、`llm/client.py::_validate_source_ids()`が
`SchemaValidationError`でfail-closedにしていたことが原因だった。ニュース側
`llm/summarize.py::_format_news_item()`と同様、ユーザープロンプト本文へ
`source_id: {chunk_source_id}`を明記するよう修正した。プロンプト変更で
`prompt_hash`が変わるため既存キャッシュ行は自然に無効化される。

`report/daily_brief.py::_llm_brief()`は同一銘柄の最初の開示分析だけを
採用していたため、2件目以降の開示（例: 10-Qに続く8-K）が黙って
レポートから欠落していた。`analyze_filing()`/`summarize_news()`は
`llm/filings_analysis.py::FilingAnalysisResult`/
`llm/summarize.py::NewsSummaryResult`（分析本体 + 提出日等のメタデータ +
`is_near_stale`）を返すようになり、`DailyBriefContext.filing_analyses`は
当該銘柄の全開示分析を保持する。`BriefLlm.filings`（新規）が
`BriefFilingAnalysis`（書類種別・提出日・facts等・`is_near_stale`）の
tupleとして各開示を個別に描画し、terminal/markdownとも「どの開示に基づく
分析か」を識別できる見出しを出す。

`NewsSummary.catalyst_quality`/`catalyst_quality_source_ids`
（roadmap §5 P2-12で追加、これまでprovenance検証にしか使われていなかった）
は`BriefLlm.catalyst_quality`/`catalyst_quality_sources`として
terminal/markdownへ表示専用で接続された。ランキング・判定ロジックには
一切接続しない（改修原則4「判断はコード、叙述はLLM」）。

`llm/decision_context.py::is_cache_near_stale()`（roadmap §5 P2-12で
メカニズムのみ実装、TTL概念が存在せず未配線だった）を本番経路へ配線した。
`settings.llm.cache_ttl_days`（既定30日、要検証）を新設し、
`near_stale_threshold_days`（既定2日）が数える対象とした
（`near_stale_threshold_days <= cache_ttl_days`を`LLMConfig`で検証）。
`LLMClient.get_cached_at()`（新規、`analyze()`のシグネチャ/挙動は不変の
純粋加算メソッド）がキャッシュ済み応答の作成日を返し、
`summarize_news()`/`analyze_filing()`が実行のas_of（`ctx.run_date`、
壁時計不使用）と突き合わせて`is_near_stale`を判定する。TTL残り日数が
`near_stale_threshold_days`以下ならレポートに再実行を促す警告を表示する。
