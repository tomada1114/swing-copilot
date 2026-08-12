# 不変条件テスト台帳

この台帳は、要件定義書の FR/NFR/CON と回帰テストの対応を一か所に
固定する。各行の「代表的な反例」は、そのテストが防ぐ入力または状態の
最小例である。実装上テストに落とせない運用・組織上の条件は、理由と手動
確認方法を明記する。テストの名称・配置を変える場合は、この台帳も同じ変更
で更新する。

## 要件トレーサビリティ

| ID | 不変条件 | 代表的な反例 | 検証 |
| --- | --- | --- | --- |
| FR-01 | `as_of` より未来のユニバースを選ばない | 7/20 時点で 7/22 のスナップショットを採用する | `tests/test_universe.py::TestResolveDailyUniverse::test_historical_run_selects_latest_snapshot_not_after_as_of` |
| FR-02 | 価格自然キーの補正は既存値を置換する | 同一銘柄・日付の訂正終値が無視される | `tests/storage/test_market_store.py::TestWriteAndReadBars::test_write_bars_correction_replaces_same_natural_key` |
| FR-03 | 提出日時が `as_of` より後の財務値を使わない | 将来提出の 10-Q がスクリーニングに混入する | `tests/storage/test_market_store.py::TestUpsertFundamentals::test_read_fundamentals_excludes_filings_after_as_of` |
| FR-04 | 全フィルタを通過した銘柄だけを残す | 流動性不足銘柄が候補に残る | `tests/screening/test_pipeline.py::TestAndSemantics::test_symbol_must_pass_all_filters_and_all_signals` |
| FR-05 | シグナル合致を一候補に集約し、順位を決定的にする | 同一銘柄が重複候補になる | `tests/screening/test_pipeline.py::TestCandidateAggregationAndRanking::test_multiple_signal_hits_aggregate_into_one_candidate` |
| FR-06 | 相関は取引日で整合し、重複日は data-quality 扱いにする | 行位置で結合して高相関と誤判定する | `tests/risk/test_checks.py::TestCorrelationWarnings::test_duplicate_dates_produce_data_quality_warning_without_correlation` |
| FR-07 | 一時的な Finnhub 障害は全試行でレート制限を守って再試行する | 429 後の再試行が待機を飛ばす | `tests/text/test_news_finnhub.py::TestRetries::test_retries_rate_limited_request_and_throttles_every_attempt` |
| FR-08 | スキル結果の run identity 不一致はレポートを書き換えない | 別 run の `analysis_result.json` を取り込む | `tests/test_e2e_smoke.py::TestFiveSymbolEndToEnd::test_mismatched_skill_result_preserves_the_daily_report` |
| FR-09 | 日次 run はローカル Markdown と 8 つの可視 step を残す | 通知成功後にレポートが作られない | `tests/test_e2e_smoke.py::TestFiveSymbolEndToEnd::test_all_eight_steps_complete_and_produce_a_markdown_brief` |
| FR-10 | 売買コストをエントリー・決済の両方に適用する | 決済時のスリッページを損益から漏らす | `tests/backtest/test_engine.py::TestBenchmarkAndReproducibility::test_final_equity_includes_exit_slippage_and_commission` |
| FR-11 | 同一判断の再記録は補正更新になり重複しない | 同じ run/symbol/strategy の判断が二重保存される | `tests/paper/test_journal.py::TestRecordDecisionIdempotency::test_recording_same_natural_key_twice_updates_not_duplicates` |
| FR-12 | required step の失敗は後続 step を実行せず failed で終える | 価格取得失敗後も分析入力を出力する | `tests/pipeline/test_daily_core.py::TestFatalStepFailure::test_price_fetch_failure_marks_run_failed_and_stops` |
| NFR-01 | Python プロセスはモデル API を呼ばない | API キーや従量課金クライアントを production dependency に追加する | 手動確認: `pyproject.toml` と依存グラフにモデル SDK がなく、分析は Claude Code スキル境界だけで行うことをレビューする。 |
| NFR-02 | 日次境界は小さく、構成・実行・step 実装を分離する | CLI が step 実装を直接抱え、テストで fake を差し込めない | `tests/test_quality_contracts.py::test_daily_entrypoint_remains_a_compatible_facade_over_split_boundaries` |
| NFR-03 | 時間予算超過時もローカル出力を残して縮退終了する | text/export/notify を延々実行し、結果を残さない | `tests/pipeline/test_daily_core.py::TestTimeoutBudget::test_pre_step_breach_skips_network_steps_but_the_run_still_completes` |
| NFR-04 | 外部テキスト障害は screening 結果を消さず degraded にする | ニュース取得例外で run 全体を failed にする | `tests/pipeline/test_failsoft.py::TestTextCollectionFailureDegrades::test_text_failure_degrades_but_still_completes_the_run` |
| NFR-05 | run は再構成可能な metadata を保存する | strategy または universe identity が監査できない | `tests/pipeline/test_daily_core.py::TestRunFingerprintAndMetadata::test_run_persists_reconstructable_metadata` |
| NFR-06 | 秘密値をログへ露出しない | HTTP 例外の URL に含まれる API key を stderr へ出す | `tests/pipeline/test_cli.py::TestConfigureLoggingRedactsSecrets::test_redacts_secret_from_message_and_traceback` |
| NFR-07 | strategy building block は登録・組合せ可能である | 未登録 filter/signal が暗黙に実行される | `tests/screening/test_pipeline.py::test_registry_contains_default_strategy_building_blocks` |
| NFR-08 | line+branch coverage の除外は main/抽象 body だけにする | 到達可能分岐へ `no cover` を付ける | `tests/test_quality_contracts.py::test_no_cover_pragmas_are_limited_to_main_and_abstract_protocol_bodies` |
| CON-01 | 発注 API を扱わず、人間だけが発注する | broker client または注文送信コードを追加する | 手動確認: `src/` と依存関係をレビューし、broker/order 実装が存在しないことを確認する。 |
| CON-02 | yfinance は試作データ provider に限定する | 本番 tier が yfinance 固定になる | 手動確認: production provider の採用判断は運用 ADR で行い、`YFinanceProvider` は `DataTier.PROTOTYPE` の既定値だけであることをレビューする。 |
| CON-03 | 表示文の断定的売買指示は ingest で fail-closed にする | 全角英字の「ＢＵＹ」を verdict 理由に入れる | `tests/analysis/test_safety.py::TestImperativeLanguage::test_normalized_commands_and_obligations_are_rejected` |
| CON-04 | ペーパートレード実績なしに実資金へ進む判定を自動化しない | バックテスト成績だけで注文可能状態にする | 手動確認: 本アプリに実資金の状態遷移・注文機能がなく、人間の運用ゲートとして扱うことをレビューする。 |

## レビュー修正の回帰対応

| Issue | 不変条件 | 代表的な反例 | 検証 |
| --- | --- | --- | --- |
| #54 | analysis artifact の run identity を厳密に照合する | 同日別 run の result が既存レポートを上書きする | `tests/test_e2e_smoke.py::TestFiveSymbolEndToEnd::test_mismatched_skill_result_preserves_the_daily_report` |
| #55 | ユニバース更新失敗時は保存済みスナップショットを明示警告付きで再利用する | 取得障害で空ユニバースを永続化する | `tests/test_universe.py::TestResolveDailyUniverse::test_refresh_failure_reuses_persisted_snapshot_with_warning` |
| #56 | Parquet 置換は失敗しても旧ファイルを残し一時ファイルを掃除する | 置換失敗で価格パーティションを失う | `tests/storage/test_market_store.py::TestWriteAndReadBars::test_replace_failure_preserves_partition_and_cleans_unique_temp` |
| #57 | テキスト収集失敗は status/exit/report の fail-soft 契約を保つ | export 前に text 例外で process が終了する | `tests/pipeline/test_failsoft.py::TestTextCollectionFailureDegrades::test_text_failure_degrades_but_still_completes_the_run` |
| #58 | 重複取引日は相関係数を算出せず data-quality 警告にする | duplicate date を行番号で結合する | `tests/risk/test_checks.py::TestCorrelationWarnings::test_duplicate_dates_produce_data_quality_warning_without_correlation` |
| #59 | result symbol/source ID/no-trade 契約を strict に検証する | 同一 source ID または空白 no-trade 理由を受理する | `tests/analysis/test_schemas.py::TestUniqueAnalysisEntities::test_duplicate_source_ids_within_a_candidate_are_rejected` |

## 履歴バックフィルと低ボラバイアス是正

要件 ID を持たない、このフェーズで追加した経路の不変条件。

| 対象 | 不変条件 | 代表的な反例 | 検証 |
| --- | --- | --- | --- |
| `copilot-backfill` | 年パーティション全書き直しを避けるため、全チャンクを1回の `write_bars` に集約する | チャンクごとに書き、書き直し回数が銘柄数に比例する | `tests/pipeline/test_backfill.py::TestBackfillBarsChunking::test_writes_every_chunk_in_a_single_write_bars_call` |
| `copilot-backfill` | 既に `--start` 以前まで届いている銘柄はネットワークを叩かない | 再実行でユニバース全銘柄を取り直す | `tests/pipeline/test_backfill.py::TestBackfillBarsResume::test_skips_symbols_already_covered_from_before_start` |
| `copilot-backfill` | `--start` の直後の取引日が最古のバーである銘柄も「届いている」と判定する | `--start` が市場休日だと resume が全銘柄で成立せず毎回全取得になる | `tests/pipeline/test_backfill.py::TestBackfillBarsResume::test_skips_a_symbol_whose_first_bar_is_the_trading_day_after_start` |
| `copilot-backfill` | 銘柄単位の取得失敗は他銘柄の取得を止めない | 1銘柄の失敗でバックフィル全体が中断する | `tests/pipeline/test_backfill.py::TestBackfillBarsFailSoft::test_a_failing_chunk_does_not_stop_later_chunks` |
| `copilot-backfill` | 全銘柄が失敗し 0 行しか書けなかった run は非ゼロ終了する | 後続の `&& copilot-backtest` が空のDBに対して走る | `tests/pipeline/test_backfill.py::TestBackfillCli::test_exits_non_zero_when_every_symbol_failed` |
| ランキング | 終値が 0 の銘柄はその銘柄だけ落ちる | `atr_pct` の除算が run 全体を落とす | `tests/screening/test_pipeline.py::TestCandidateAggregationAndRanking::test_symbol_dropped_when_the_last_close_is_zero` |
| 却下台帳 | RSI が閾値を通った銘柄は `SIGNAL_RSI_NOT_MET` と記録されない | 帯で落ちた銘柄に、通過した RSI 値付きで矛盾した理由が付く | `tests/screening/test_rejection_classifier.py::TestSignalReasons::test_a_passing_rsi_is_never_reported_as_rsi_not_met` |
| `pullback_rsi` | ATR が NaN または 0 のとき ATR 正規化帯は閉じる | 距離を測れない銘柄を帯の内側として通す | `tests/screening/test_technical_signals.py::TestPullbackATRBand::test_a_zero_atr_is_rejected_fail_safe` |
| `pullback_rsi` | `band_atr_multiple` 未設定時の判定は従来どおりである | 追加したモードが既定挙動を書き換える | `tests/screening/test_technical_signals.py::TestPullbackATRBand::test_none_keeps_the_legacy_percentage_band_hit` |
| ランキング | `atr_pct` は既定 0.0 で、出荷中のスコアを変えない | 新成分が既定で合成スコアに混入する | `tests/screening/test_pipeline.py::TestAtrPctScoreComponent::test_default_weight_is_zero_so_existing_scores_are_unchanged` |
| ランキング | `atr_pct` も score_weights 合計 1.0 検証の対象である | 新成分を足しても合計 1.0 とみなされる | `tests/test_config.py::TestLoadStrategies::test_atr_pct_counts_toward_the_sum_to_one_requirement` |
| バックテスト | 発火 0 件の決済理由も 0 として必ず報告する | 一度も出ていない理由がレポートから消える | `tests/backtest/test_metrics.py::TestExitReasonBreakdown::test_counts_every_reason_including_the_absent_ones` |
| 分析数値整合 | 逐語引用と桁が食い違う fact を警告として名指しする | 千ドル単位の 3,495,296 を「35億9,530万ドル」と書いた fact が全検査を素通りする | `tests/analysis/test_validate.py::TestNumericConsistencyWarnings::test_a_misconverted_figure_is_warned_about` |
| 分析数値整合 | 単位変換を跨いだ正しい言い換えは警告しない | 34億9,530万ドルや前年同期 29億2,818万ドルを誤検知する | `tests/analysis/test_numeric_consistency.py::TestTheIssueCase::test_the_corrected_figure_is_not_reported` |
| 分析数値整合 | 数値の警告は銘柄を縮退させない | 誤検知した銘柄の定性セクションが丸ごと消える | `tests/analysis/test_validate.py::TestNumericConsistencyWarnings::test_a_warned_symbol_is_still_rendered` |

## 日次統合 E2E の境界

通常経路は `DailyDependencies` の全外部 port を fake にした offline E2E で、価格・
財務・screening・risk・text・analysis input export・通知・Markdown を順に確認する。
スキルは同じ run の fixture result を書くだけで、Python プロセスはモデル呼出しを
行わない。identity が一致する正経路は
`tests/test_e2e_smoke.py::TestFiveSymbolEndToEnd::test_exported_run_identity_allows_offline_skill_result_ingest`、
不一致時の report 不変性は上表 #54 の node が担保する。

`--limit 0` の候補ゼロ run は analysis export を成功扱いで skip し、短い report を
残す。これは
`tests/pipeline/test_failsoft.py::TestAnalysisExportFailureDegrades::test_no_candidates_skips_the_export_without_degrading`
で固定する。

## 決算 8-K の Exhibit 99.1 取り込み

要件 ID を持たない、Issue #128 で追加した経路の不変条件。

| 対象 | 不変条件 | 代表的な反例 | 検証 |
| --- | --- | --- | --- |
| `data/edgar.py` | 8-K の `EX-99*` 本文を主文書と同じ `content_text` へ連結する | Item 2.02 の告知だけが入力に載り、ガイダンスが存在しない | `tests/data/test_edgar.py::TestEightKExhibits::test_exhibit_text_is_appended_to_the_primary_document_text` |
| `data/edgar.py` | 8-K 以外は添付一覧を取りに行かない | 10-Q ごとに不要な添付リクエストが増える | `tests/data/test_edgar.py::TestEightKExhibits::test_forms_other_than_eight_k_never_request_attachments` |
| `data/edgar.py` | Exhibit 合計は 1 開示 60,000 字で打ち切り、切り詰めを本文に明示する | 予算超過が黙って落ち、読み手が連結を連続本文と誤認する | `tests/data/test_edgar.py::TestEightKExhibitBudget::test_exhibit_longer_than_the_budget_is_cut_with_an_inline_marker` |
| `data/edgar.py` | 予算を使い切った後続 Exhibit はダウンロードもしない | 捨てる本文のために SEC へリクエストを投げる | `tests/data/test_edgar.py::TestEightKExhibitBudget::test_exhausted_budget_skips_the_next_exhibit_without_downloading_it` |
| `data/edgar.py` | Exhibit 取得の失敗は fail-soft で、主文書と取得済み Exhibit を保持する | 添付 1 件の 404 で開示そのものが入力から消える | `tests/data/test_edgar.py::TestEightKExhibitFailSoft::test_failing_exhibit_keeps_the_exhibits_already_retrieved` |
| `data/edgar.py` | 添付ダウンロードにも 10 リクエスト/秒の throttle を適用する | Exhibit 取得だけがレート制限を迂回する | `tests/data/test_edgar.py::TestEightKExhibitRateLimiting::test_throttles_the_attachment_index_and_every_exhibit_download` |

## 自社材料の供給量の申告

要件 ID を持たない、Issue #130 で追加した経路の不変条件。

| 対象 | 不変条件 | 代表的な反例 | 検証 |
| --- | --- | --- | --- |
| `analysis/news_supply.py` | 自社材料ゼロの入力を `level: "none"` として申告する | 20 件供給されているという事実だけが下流に届き、「悪材料なし」と読まれる | `tests/analysis/test_news_supply.py::TestSupplyLevel::test_a_full_feed_that_never_names_the_symbol_reports_none` |
| `analysis/news_supply.py` | しきい値の直下は `sparse`、直上は `sufficient` になる | 境界が片側にずれ、薄い供給が十分と申告される | `tests/analysis/test_news_supply.py::TestSupplyLevel::test_one_below_the_threshold_the_supply_is_sparse` |
| `analysis/export.py` | `related_symbols` が空の記事は選別で降格されないが、自社材料としては数えない | ティッカー未宣言の記事が自社材料に化け、供給量が水増しされる | `tests/analysis/test_export.py::TestBuildAnalysisInput::test_news_without_related_tickers_is_ranked_on_target_but_not_counted` |
| `analysis/schemas.py` | `news_supply` を持たない過去アーカイブが v2/v3 とも読める | フィールド追加で P8 collect が過去 run を読めなくなる | `tests/analysis/test_schemas.py::TestSchemaVersions::test_an_archive_without_news_supply_still_parses` |
| `analysis/schemas.py` | `level: "none"` と件数ゼロが常に一致する | 申告と件数が食い違い、どちらを信じるか読み手が判断できない | `tests/analysis/test_schemas.py::TestNewsSupplyCounts::test_the_none_level_must_mean_exactly_zero_mentions` |
| `.claude/skills/analyze-news/SKILL.md` | 供給不足の申告経路が指示文に残っている | コードは数えているのに誰も読まず、下流に届かない | `tests/analysis/test_skill_contract.py::test_news_skill_must_declare_a_thin_symbol_specific_supply` |
