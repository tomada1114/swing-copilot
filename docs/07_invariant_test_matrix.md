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
| FR-06 | 公開リスク判定は銘柄単位の指値・逆指値・ATR14・1Rだけを計算し、読者の口座や保有を参照しない | 口座評価額から株数を推測して分析入力へ出す | `tests/risk/test_checks.py::TestTradePlan::test_approved_plan_exposes_close_limit_stop_atr_and_one_r`、`tests/analysis/test_context.py::TestRiskConstraints::test_it_renders_the_symbol_level_trade_plan` |
| FR-07 | 一時的な Finnhub 障害は全試行でレート制限を守って再試行する | 429 後の再試行が待機を飛ばす | `tests/text/test_news_finnhub.py::TestRetries::test_retries_rate_limited_request_and_throttles_every_attempt` |
| FR-08 | スキル結果の run identity 不一致はレポートを書き換えない | 別 run の `analysis_result.json` を取り込む | `tests/test_e2e_smoke.py::TestFiveSymbolEndToEnd::test_mismatched_skill_result_preserves_the_daily_report` |
| FR-09 | 日次 run はローカル Markdown と 8 つの可視 step を残す | 通知成功後にレポートが作られない | `tests/test_e2e_smoke.py::TestFiveSymbolEndToEnd::test_all_eight_steps_complete_and_produce_a_markdown_brief` |
| FR-10 | 売買コストをエントリー・決済の両方に適用する | 決済時のスリッページを損益から漏らす | `tests/backtest/test_engine.py::TestBenchmarkAndReproducibility::test_final_equity_includes_exit_slippage_and_commission` |
| FR-11 | **廃止(2026-08-19)**: 公開トラックレコード化に伴い実売買記録機能を撤去した。不変条件も対応テストも無い | — | — |
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
| CON-04 | **廃止(2026-08-19)**: ペーパートレード記録機能ごと撤去したため、このゲート自体が存在しない | — | 手動確認: 本アプリに実資金の状態遷移・注文機能が無いことは変わらない。 |

## レビュー修正の回帰対応

| Issue | 不変条件 | 代表的な反例 | 検証 |
| --- | --- | --- | --- |
| #54 | analysis artifact の run identity を厳密に照合する | 同日別 run の result が既存レポートを上書きする | `tests/test_e2e_smoke.py::TestFiveSymbolEndToEnd::test_mismatched_skill_result_preserves_the_daily_report` |
| #55 | ユニバース更新失敗時は保存済みスナップショットを明示警告付きで再利用する | 取得障害で空ユニバースを永続化する | `tests/test_universe.py::TestResolveDailyUniverse::test_refresh_failure_reuses_persisted_snapshot_with_warning` |
| #56 | Parquet 置換は失敗しても旧ファイルを残し一時ファイルを掃除する | 置換失敗で価格パーティションを失う | `tests/storage/test_market_store.py::TestWriteAndReadBars::test_replace_failure_preserves_partition_and_cleans_unique_temp` |
| #57 | テキスト収集失敗は status/exit/report の fail-soft 契約を保つ | export 前に text 例外で process が終了する | `tests/pipeline/test_failsoft.py::TestTextCollectionFailureDegrades::test_text_failure_degrades_but_still_completes_the_run` |
| #58 | 旧相関チェックを本番経路から撤去し、公開リスク判定を銘柄単位に限定する | 重複日付の系列結合や相関制約が公開経路へ戻る | `tests/risk/test_checks.py::TestTradePlan::test_public_plan_has_no_account_or_correlation_constraints` |
| #348 | 本番経路は口座依存ルールを持たず、旧監査列は新規行でNULLにする | 旧履歴を読めなくする、または新規行へ株数を残す | `tests/storage/test_state_store.py::TestRecordRiskAssessments::test_new_rows_leave_legacy_sizing_columns_null`、`tests/report/test_terminal_markdown_report.py::test_terminal_and_markdown_show_one_r_without_account_sections` |
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
| ランキング | 戦略別成分は既定 0.0 で、出荷中3戦略のスコアを1ビットも動かさない | 機構追加が運用中のランキングを黙って書き換える | `tests/screening/test_pipeline.py::TestStrategySpecificScoreComponentsAreOffByDefault::test_a_shipped_strategy_scores_exactly_as_it_did_before_the_components` |
| ランキング | 成分内訳のキーは `ScoreWeights` のフィールドと完全一致する | 成分を足しても実列・レポートに現れず、score と内訳の合計がずれる | `tests/screening/test_pipeline.py::TestStrategySpecificScoreComponentsAreOffByDefault::test_the_breakdown_keys_are_exactly_the_score_weights_fields` |
| ランキング | メトリクスを生む signal を持たない戦略で戦略別成分に重みを付けると外部I/O前に落ちる | 全候補が同じ 0.0 を得て他成分の実効重みだけが薄まる | `tests/test_config.py::TestLoadStrategies::test_a_weighted_component_without_its_signal_is_rejected` |
| ランキング | ピボットが欠損・非正でもその銘柄だけ 0.0 になり候補からは落ちない | 除算で run 全体が落ちる／成分が事実上のフィルタになる | `tests/screening/test_pipeline.py::TestPivotProximityComponent::test_an_absent_or_nonpositive_pivot_scores_zero_and_keeps_the_candidate` |
| ランキング | 戦略別成分を重み付けても同点は symbol 昇順で決まる | 決定的順序が壊れる（REQ-010） | `tests/screening/test_pipeline.py::TestPivotProximityComponent::test_a_tie_still_breaks_on_symbol_ascending` |
| ストレージ | 戦略別成分の実列は既存DBへ追加されるが backfill しない | 記録が無い行に 0.0 が入り「計測された寄与ゼロ」と読めてしまう | `tests/storage/test_schema_migration.py::TestPromotedColumnBackfill::test_strategy_specific_score_columns_are_added_but_not_backfilled` |
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
| `data/edgar.py` | Exhibit 合計は 1 開示 500,000 字の安全弁で打ち切り、切り詰めを本文に明示する | 安全弁超過が黙って落ち、読み手が連結を連続本文と誤認する | `tests/data/test_edgar.py::TestEightKExhibitBudget::test_exhibit_longer_than_the_budget_is_cut_with_an_inline_marker` |
| `data/edgar.py` | 予算を使い切った後続 Exhibit はダウンロードもしない | 捨てる本文のために SEC へリクエストを投げる | `tests/data/test_edgar.py::TestEightKExhibitBudget::test_exhausted_budget_skips_the_next_exhibit_without_downloading_it` |
| `data/edgar.py` | Exhibit 取得の失敗は fail-soft で、主文書と取得済み Exhibit を保持する | 添付 1 件の 404 で開示そのものが入力から消える | `tests/data/test_edgar.py::TestEightKExhibitFailSoft::test_failing_exhibit_keeps_the_exhibits_already_retrieved` |
| `data/edgar.py` | 添付ダウンロードにも 10 リクエスト/秒の throttle を適用する | Exhibit 取得だけがレート制限を迂回する | `tests/data/test_edgar.py::TestEightKExhibitRateLimiting::test_throttles_the_attachment_index_and_every_exhibit_download` |
| `analysis/filing_selection.py` | 予算逼迫時は主文書と EX-99.1 を supplement より優先して配分する | 数倍大きい supplemental package が予算を食い、プレスリリースが削られる | `tests/analysis/test_filing_selection.py::TestEightKExhibitSelection::test_budget_pressure_serves_the_press_release_before_a_supplement` |
| `analysis/filing_selection.py` | 割当超過の Exhibit は末尾切りではなく、定型文から落として markdown テーブルを最後まで残す | 末尾に置かれる財務諸表・非 GAAP 調整表が真っ先に落ちる（Issue #157 の GOOG 申告） | `tests/analysis/test_filing_selection.py::TestEightKExhibitSelection::test_a_far_over_allocation_keeps_the_tables_after_everything_else` |
| `analysis/filing_selection.py` | 各開示は最低保証字数を確保してから、余りを優先順に配る（優先順位は読める量を決め、読めるかどうかは決めない） | per-filing 上限に達する開示 2 件で 1 銘柄予算が尽き、3 件目が 10 字（HST）や 0 字（UDR）のまま「分析済み」になる | `tests/analysis/test_filing_selection.py::TestPerSymbolMinimumGuarantee::test_a_small_third_filing_survives_two_ceiling_filling_filings` |
| `analysis/filing_selection.py` | 保証すら全件に配れないときは割り当て順に保証を配り、尽きたら 0 にする | 窮迫時の配分が実装詳細で変わり、同じ入力から同じ出力が出ない | `tests/analysis/test_filing_selection.py::TestPerSymbolMinimumGuarantee::test_a_ceiling_too_small_for_every_guarantee_serves_them_in_priority_order` |
| `analysis/filing_selection.py` | Exhibit 選別は `selection_mode` / `sections_json` へ Exhibit 語彙で記録され、P8 から読める | 「開示が切れた」までしか分からず、どの Exhibit が削られたか追跡できない | `tests/analysis/test_filing_selection.py::TestEightKExhibitSelection::test_exhibit_coverage_survives_the_analysis_source_coverage_round_trip` |

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

## 断片の契約検証の共通化

要件 ID を持たない、Issue #132 で追加した経路の不変条件。

| 対象 | 不変条件 | 代表的な反例 | 検証 |
| --- | --- | --- | --- |
| `analysis/fragment.py` | 断片の事前検査は ingest と同じ関数で同じ理由を返す | grep ベースの自己検査が、NFKC 正規化後に初めて見える違反を「合格」と報告する | `tests/analysis/test_fragment.py::TestSharedCheckMatchesIngest::test_a_violating_payload_fails_identically_in_both_paths` |
| `analysis/fragment.py` | 正規化で吸収されるだけの表記差は落とさない | 全角・NBSP・大小の違いだけで正しい引用が withhold される | `tests/analysis/test_fragment.py::TestSharedCheckMatchesIngest::test_a_conforming_payload_passes_identically_in_both_paths` |
| `analysis/fragment.py` | 断片はペイロードキーをちょうど 1 つだけ持つ | 1 ファイルに 2 専門家分が混ざり、マージが取りこぼす | `tests/analysis/test_fragment.py::TestFragmentEnvelope::test_a_fragment_carrying_two_experts_answers_is_rejected` |
| `analysis/fragment.py` | 別 run の断片は内容検査より先に identity 違反として報告する | 前日の残骸が provenance 違反として報告され、原因を取り違える | `tests/analysis/test_fragment.py::TestFragmentIdentity::test_identity_is_reported_before_the_content_checks` |
| `analysis/fragment.py` | ファイル名の `<kind>-<SYMBOL>` とペイロードの不一致を検出する | 別銘柄の断片が正しい名前で置かれ、マージが取り違える | `tests/analysis/test_fragment.py::TestFragmentFilename::test_a_filename_naming_another_symbol_is_reported` |
| `analysis/verify_cli.py` | result の dry-run は ingest が縮退させる銘柄と理由を一致させる | 事前検査を通ったのに ingest で縮退する | `tests/analysis/test_verify_cli.py::TestVerificationStrengthMatchesIngest::test_the_dry_run_reports_exactly_what_ingest_would_withhold` |
| `analysis/verify_cli.py` | 検査はレポートも run ディレクトリも書き換えない | 事前検査が当日の成果物を書き換える | `tests/analysis/test_verify_cli.py::TestResultDryRun::test_a_valid_result_passes_without_writing_anything` |
| `analysis/verify_cli.py` | 壊れた 1 件が同じディレクトリの他の断片の判定を隠さない | 1 ファイルの JSON 破損で全断片の合否が分からなくなる | `tests/analysis/test_verify_cli.py::TestDirectoryExpansion::test_one_unreadable_entry_does_not_hide_its_siblings` |
| 専門家スキル | 共有コマンドを使う指示が指示文に残っている | コマンドはあるのに誰も呼ばず、各自が自前検査へ戻る | `tests/analysis/test_skill_contract.py::test_every_fragment_author_is_pointed_at_the_shared_checker` |
| `output-schema.md` | 「ingest と同一の関数」の主張が関数名で束縛されている | リネームで主張だけが残り、実体との対応が切れる | `tests/analysis/test_skill_contract.py::test_the_schema_reference_binds_the_checker_to_the_ingest_function` |

## 開示断片の日跨ぎ流用（本文ハッシュ）

要件 ID を持たない、Issue #261 で断片契約を緩めた経路の不変条件。緩めたのは
**開示断片だけ**であり、この表はその境界と、緩和を安全にしている fail-closed の網を
束縛する。

| 対象 | 不変条件 | 代表的な反例 | 検証 |
| --- | --- | --- | --- |
| `analysis/fragment.py` | 開示本文が同じなら別 run の開示断片を流用できる | 本文が 1 文字も変わらないのに毎日 10-Q を読み直す | `tests/analysis/test_fragment.py::TestFilingFragmentReuse::test_an_unchanged_filing_body_makes_yesterdays_reading_reusable` |
| `analysis/fragment.py` | 本文が変わった開示は再分析になる | 切り詰め量が変わった開示に、古い読みがそのまま載る | `tests/analysis/test_fragment.py::TestFilingFragmentReuse::test_a_changed_filing_body_forces_a_re_analysis` |
| `analysis/fragment.py` | 読んでいない開示を含む銘柄は流用できない | 夜間に出た 8-K が「分析済み」として素通りする | `tests/analysis/test_fragment.py::TestFilingFragmentReuse::test_a_filing_the_reading_never_saw_forces_a_re_analysis` |
| `analysis/fragment.py` | `news_summary` / `screening_assessment` は本文ハッシュに関わらず日跨ぎ流用できない | 前日のニュース解釈・前日のスコア評価が当日のレポートに載る | `tests/analysis/test_fragment.py::TestFilingFragmentReuse::test_the_as_of_dependent_readings_are_never_reusable_across_runs` |
| `analysis/validate.py` | 誤って流用された古い読みは `evidence_quote` の逐語検証で FAIL する | 流用の鍵が壊れたとき、provenance を担保するものが何も残らない | `tests/analysis/test_fragment.py::TestFilingFragmentReuse::test_a_wrongly_reused_reading_still_fails_the_verbatim_quote_check` |
| `analysis/slices.py` | 本文ハッシュは filings スライスだけが載せ、その本文の digest と一致する | 専門家が自分でハッシュを計算し、実装差が流用判定の差になる | `tests/analysis/test_slices.py::test_only_the_filings_slice_carries_the_reuse_digests` |
| `analysis/schemas.py` | ハッシュの入力は export 後の本文である | 収集段階の原文で採ると、切り詰め方が変わっても流用されてしまう | `tests/analysis/test_slices.py::test_the_reuse_digest_follows_the_exported_body_not_the_original` |
| `swing-daily` / `analyze-filings` スキル | 緩和が開示に限られる旨と、ハッシュを自分で計算しない旨が指示文に残っている | 指示の書き換えでニュース断片まで日跨ぎ流用され、機械検査では見えない | `tests/analysis/test_skill_contract.py::test_only_the_filing_reading_is_reusable_across_trading_days` |

## 成果物読み取りの失敗型

要件 ID を持たない、Issue #153 と Issue #164 で追加した経路の不変条件。
ファイルをディスクから読む経路は `documents.py` の
`read_text_document()`（テキスト）と、それを土台にした `read_json_document()`
（JSON）に集約されており、境界ごとに変わるのは例外型とメッセージ接頭辞だけである。
JSON 成果物だけでなく、YAML 設定と Markdown 台帳も同じ入口を通る——
`UnicodeDecodeError` は `OSError` ではなく `ValueError` なので、
呼び出し側ごとに `except OSError` を書き足すやり方では必ずどこかで穴が開く。
`config.py` が呼び出し元に含まれるため、この関数は `analysis/` ではなく
パッケージ直下に置く（設定ローダーが分析境界を import しないため）。

| 対象 | 不変条件 | 代表的な反例 | 検証 |
| --- | --- | --- | --- |
| `analysis/validate.py` | UTF-8 として読めない成果物も `AnalysisIngestError` として届く | 文字化けした `analysis_result.json` が生の `UnicodeDecodeError` になり、「壊れた成果物」ではなく想定外の異常として無人実行が落ちる | `tests/analysis/test_validate.py::TestHardFailures::test_a_wrongly_encoded_document_is_a_hard_failure` |
| `analysis/verify_cli.py` | 事前検査と ingest が同じ読み取り関数で同じ失敗型を返す | 新 CLI だけが不正エンコーディングを FAIL 行に落とし、本番 ingest では例外型が揃わない | `tests/analysis/test_verify_cli.py::TestDirectoryExpansion::test_a_wrongly_encoded_fragment_is_reported_rather_than_raised` |
| `analysis/snapshot.py` | UTF-8 として読めない `report_context.json` も `AnalysisIngestError` として届く | レポート再描画のコンテキストだけが `except OSError` に留まり、無人実行が生んだ文字化けが想定外の異常として漏れる | `tests/analysis/test_snapshot.py::TestReadFailures::test_a_wrongly_encoded_context_is_a_hard_failure` |
| `retro/validate.py` | UTF-8 として読めない振り返り成果物も `RetroIngestError` として届く | `retro_input.json` / `retro_result.json` の文字化けが生の `UnicodeDecodeError` になり、`copilot-retro ingest` の呼び出し側が壊れた成果物と区別できない | `tests/retro/test_validate.py::TestLoading::test_rejects_a_wrongly_encoded_document` |
| `config.py` | UTF-8 として読めない `settings.yaml` / `strategies.yaml` も `ConfigError` として届く | 別エンコーディングで保存された設定が生の `UnicodeDecodeError` になり、`ConfigError` を fatal として扱う CLI 境界を素通りする | `tests/test_config.py::TestLoadSettings::test_non_utf8_file_raises_config_error`、`::TestLoadStrategies::test_non_utf8_file_raises_config_error` |
| `retro/ledger.py` | 存在するのに読めない提案台帳は `RetroIngestError` として届き、「空の台帳」に化けない | 台帳が読めないまま `closed_proposal_keys()` が空になり、却下済みの提案が新しい RP-ID で再び通る | `tests/retro/test_ledger.py::TestReadLedger::test_an_unreadable_ledger_fails_instead_of_reading_as_empty`、`tests/retro/test_cli.py::TestExportCommand::test_exits_when_the_proposal_ledger_cannot_be_read` |

## バックテストへの市場状態ゲート注入（Issue #184）

本番RiskCheckerは市場状態・決算と銘柄単位の価格計画だけを扱う。バックテストは
Issue #184でこれを注入し、名目資金の株数計算はバックテスト専用の設定で行う。
ポートフォリオ熱量・セクター・相関・サーキットブレーカーはこの境界で判定しない。
ここで固定するのはゲート入力のas-of規律である。

| 対象 | 不変条件 | 代表的な反例 | 検証 |
| --- | --- | --- | --- |
| `backtest/engine.py` | サイジングは equity 基準で、清算後の最終 equity が手計算値と厳密に一致する | 2件目を残現金基準で建て、保有が増えるほどサイズが `0.9^n` に縮む | `tests/backtest/test_engine.py::TestEquityBasedSizing::test_second_entry_sizes_from_equity_not_remaining_cash` |
| `backtest/engine.py` | サイジングの時価評価はシグナル日の終値までで、約定日当日の終値を見ない | 約定日に急騰した保有銘柄の時価で当日の新規建玉を大きくする | `tests/backtest/test_engine.py::TestEquityBasedSizing::test_equity_basis_uses_the_signal_days_close_not_the_fill_days` |
| `backtest/policy.py` | レジーム判定はシグナル日のバーだけを見る（直前・同日・直後の3点） | 翌日の VIX 急騰が前日の判断を後ろ向きに書き換える | `tests/backtest/test_policy.py::TestAsOfDiscipline::test_bar_immediately_before_the_cutoff_leaves_entries_allowed`、`tests/backtest/test_policy.py::TestAsOfDiscipline::test_bar_exactly_at_the_cutoff_is_included_and_blocks`、`tests/backtest/test_policy.py::TestAsOfDiscipline::test_bar_after_the_cutoff_cannot_reach_back_and_block_an_earlier_day` |
| `backtest/policy.py` | `CASH_PRIORITY` は全候補を `regime` 理由でブロックする | レジームが閉じた日にバックテストだけが建玉を作る | `tests/backtest/test_policy.py::TestRegimeGate::test_cash_priority_blocks_every_candidate_with_the_regime_reason` |
| `backtest/policy.py` | `REDUCE_ONLY` は警戒ラベルだけで、シミュレータの名目サイジングを上書きしない | レジームの警戒表示が口座固有のリスク半減や候補削減へ変換される | `tests/backtest/test_policy.py::TestRegimeGate::test_reduce_only_is_a_label_and_does_not_override_simulator_sizing`、`tests/risk/test_checks.py::TestMarketState::test_reduce_only_is_a_label_without_filtering_or_risk_warning` |
| `backtest/policy.py` | 価格計画を算出不能な候補は fail-closed で建てない | `close`/`atr14` を欠く候補を既定値で建てる | `tests/backtest/test_policy.py::TestRegimeGate::test_a_candidate_the_checker_cannot_size_is_withheld_fail_closed` |
| `backtest/policy.py` | SPY/QQQ/^VIX のバーが無い状態の `--policy` は実行前に落ちる | レジーム UNKNOWN の fail-closed で全期間ゼロ取引のレポートを黙って出す | `tests/backtest/test_policy.py::TestArmSelection::test_missing_regime_bars_fail_fast_instead_of_blocking_silently` |
| `backtest/earnings_history.py` | 決算日の推定は `filed_at <= as_of` の提出だけを見る（直前・同日・直後の3点） | 未提出の決算を先取りして、当時知り得なかった日付でエントリーを止める | `tests/backtest/test_earnings_history.py::TestVisibilityCutoff::test_filing_from_the_day_after_as_of_is_not_visible`、`tests/backtest/test_earnings_history.py::TestVisibilityCutoff::test_filing_dated_exactly_as_of_is_visible`、`tests/backtest/test_earnings_history.py::TestVisibilityCutoff::test_filing_from_the_day_before_as_of_is_visible`、`tests/backtest/test_earnings_history.py::TestVisibilityCutoff::test_the_projection_itself_never_reads_past_the_cutoff` |
| `backtest/earnings_history.py` | 推定できない銘柄は「不明」と報告し、日付を捏造しない | 提出履歴が1件しか無い銘柄に既定の四半期日程を当てはめる | `tests/backtest/test_earnings_history.py::TestProjection::test_a_single_visible_filing_cannot_establish_a_cadence`、`tests/backtest/test_earnings_history.py::TestProjection::test_symbol_with_no_collected_filings_reports_unknown`、`tests/backtest/test_earnings_history.py::TestProjection::test_projection_the_calendar_has_outrun_is_reported_as_unknown` |
| `storage/market_store.py` | `read_filing_dates` は `filed_at <= as_of` を自身のクエリで切る | 呼び出し側の切り忘れで未来の提出が決算推定に混入する | `tests/storage/test_market_store.py::TestReadFilingDates::test_filing_accepted_exactly_on_the_cutoff_is_visible`、`tests/storage/test_market_store.py::TestReadFilingDates::test_filing_accepted_the_day_after_the_cutoff_is_invisible` |
| `backtest/cli.py` | `--policy` の A/B は 1 本の候補ストリームを共有する | アームごとにスクリーニングし直し、差分がゲート以外にも由来する | `tests/backtest/test_cli.py::TestPolicyEndToEnd::test_ab_run_compares_arms_over_one_candidate_stream` |
| `backtest/cli.py` | `--policy regime+earnings` にだけ決算カレンダーを配線する | 決算ゲートを適用しないアームが提出履歴を読み、被覆率だけを表示する | `tests/backtest/test_cli.py::TestEarningsGuardWiring::test_regime_risk_arm_receives_the_filing_derived_calendar`、`tests/backtest/test_cli.py::TestEarningsGuardWiring::test_arms_that_cannot_use_the_gate_never_read_the_filing_history` |
| `backtest/metrics.py` | 発火 0 件のエントリー阻止理由も 0 として必ず報告する | 一度も効かなかったゲートがレポートから消え、「効いた」と読めてしまう | `tests/backtest/test_metrics.py::TestEntryBlockBreakdown::test_every_known_reason_is_reported_even_at_zero` |

## レート制限スロットルの記録時点（Issue #253）

4つの外部クライアントの `_throttle` は、待機してから実際にリクエストを
発行する。記録すべきは「発行時点」であって「スロットル判定を始めた時点」では
ない。後者を記録すると sleep した分が次回の間隔計算から落ち、発行間隔が
`_MIN_REQUEST_INTERVAL_SECONDS` と待機なしの間隔を交互に取って実効レートが
公称上限を上回る。ここで固定するのは sleep の呼び出し回数や引数ではなく、
「実際にリクエストが出た時刻の間隔」の下限である。`retry_external_call` は
試行ごとに `before_attempt` を呼ぶため、失敗した試行も1リクエストとして
同じ不変条件に服する。

| 対象 | 不変条件 | 代表的な反例 | 検証 |
| --- | --- | --- | --- |
| `data/edgar.py` | 連続する発行時刻の間隔が 10 リクエスト/秒の下限を下回らない | 待機前の時刻を記録し、1回おきに 0.1 秒未満で SEC へ投げる | `tests/data/test_edgar.py::TestRateLimiting::test_successive_requests_are_issued_at_least_one_interval_apart` |
| `data/edgar.py` | リトライを挟んでも発行間隔の下限が保たれる | 失敗試行を発行として数えず、後続の待機を過小評価する | `tests/data/test_edgar.py::TestRateLimiting::test_retried_attempts_keep_the_minimum_issue_interval` |
| `text/news_finnhub.py` | 連続する発行時刻の間隔が 60 calls/分の下限を下回らない | 待機前の時刻を記録し、実効レートが 60/分を超える | `tests/text/test_news_finnhub.py::TestRateLimiting::test_successive_requests_are_issued_at_least_one_interval_apart` |
| `text/news_finnhub.py` | リトライを挟んでも発行間隔の下限が保たれる | 429 の試行を発行として数えず、後続の待機を過小評価する | `tests/text/test_news_finnhub.py::TestRetries::test_retried_attempts_keep_the_minimum_issue_interval` |
| `data/earnings_finnhub.py` | 連続する発行時刻の間隔が 60 calls/分の下限を下回らない | 待機前の時刻を記録し、実効レートが 60/分を超える | `tests/data/test_earnings_finnhub.py::test_successive_requests_are_issued_at_least_one_interval_apart` |
| `data/earnings_finnhub.py` | リトライを挟んでも発行間隔の下限が保たれる | 失敗試行を発行として数えず、後続の待機を過小評価する | `tests/data/test_earnings_finnhub.py::test_retried_attempts_keep_the_minimum_issue_interval` |
| `text/calendar_fred.py` | 連続する発行時刻の間隔が 120 リクエスト/分の下限を下回らない | 待機前の時刻を記録し、enrichment 2件が 0.5 秒未満で続く | `tests/text/test_calendar_fred.py::TestRateLimiting::test_successive_requests_are_issued_at_least_one_interval_apart` |
| `text/calendar_fred.py` | リトライを挟んでも発行間隔の下限が保たれる | 失敗試行を発行として数えず、後続の待機を過小評価する | `tests/text/test_calendar_fred.py::TestRateLimiting::test_retried_attempts_keep_the_minimum_issue_interval` |

## レート制限の単位はアカウント（Issue #263）

Finnhub の 60 calls/分は API キーの**アカウント**に対する上限であり、
クライアントオブジェクト単位ではない。`FinnhubNewsClient` と
`FinnhubEarningsClient` は同一キーを使うため、合成ルート
（`pipeline/daily_composition.py::_finnhub_clients`）が 1 個の
`ratelimit.MinIntervalThrottle` を両者へ注入し、**2クライアント合計**の発行
間隔を上限以下に保つ。注入しない既定は従来どおりインスタンス固有であり、
既存の呼び出し元の挙動は変わらない（EDGAR / FRED は別アカウント・別上限
なので共有しない）。固定するのは Issue #253 と同じく「実際にリクエストが出た
時刻の間隔」であって、どちらのクライアントが sleep したかではない。

共有予算は 1 つの時計の上でしか測れないので、スロットル注入と**クライアント
自身のレート時計の注入**（news の `rate_clock`、earnings の
`EarningsTiming.rate_clock` / `sleep_fn`）は排他であり、両方渡すと
`ValueError` で落とす。黙って無視すると、呼び出し元は動いていない
タイムラインを信じたままになる。リトライバックオフ（news の `sleep_fn`、
earnings の `EarningsTiming.backoff_fn`）は共有スロットル下でも生きているので
注入可能なまま残す。

| 対象 | 不変条件 | 代表的な反例 | 検証 |
| --- | --- | --- | --- |
| `ratelimit.py` | 共有スロットルを注入した2クライアントを交互に呼んでも発行間隔が 60 calls/分の下限を下回らない | クライアントごとに `_last_request_at` を持ち、合計レートが上限の約2倍になる | `tests/test_ratelimit.py::TestSharedFinnhubThrottle::test_shared_budget_keeps_alternating_clients_one_interval_apart` |
| `ratelimit.py` | 片方のクライアントのリトライ試行も共有予算を消費する | 失敗試行をアカウントのリクエストとして数えず、もう一方を早く通す | `tests/test_ratelimit.py::TestSharedFinnhubThrottle::test_shared_budget_survives_a_retry_inside_one_client` |
| `ratelimit.py` | 未注入時はインスタンス固有のままで、既存呼び出し元の挙動が変わらない | 既定を暗黙のグローバル共有にし、単体利用のクライアントに無関係な待機を課す | `tests/test_ratelimit.py::TestSharedFinnhubThrottle::test_without_a_shared_budget_each_client_still_throttles_only_itself` |
| `pipeline/daily_composition.py` | 日次実行の2つの Finnhub クライアントは同一スロットルを共有する | 生成箇所で別々に `MinIntervalThrottle` を作り、共有が静かに失われる | `tests/pipeline/test_cli.py::TestFinnhubClients::test_both_clients_share_one_account_wide_throttle` |
| `text/news_finnhub.py`・`data/earnings_finnhub.py` | スロットルとクライアント自身のレート時計の同時注入は `ValueError` で拒否する | 後から渡した方を黙って無視し、呼び出し元が動かない時計を信じる | `tests/test_ratelimit.py::TestThrottleAndRateClockAreMutuallyExclusive::test_news_client_rejects_a_rate_clock_beside_a_throttle`、`tests/test_ratelimit.py::TestThrottleAndRateClockAreMutuallyExclusive::test_earnings_client_rejects_throttle_owned_timing_fields` |
| `text/news_finnhub.py`・`data/earnings_finnhub.py` | バックオフ関数の注入はスロットル注入下でも受け付ける | 排他の範囲を広げすぎ、リトライを fake sleep で試験できなくする | `tests/test_ratelimit.py::TestThrottleAndRateClockAreMutuallyExclusive::test_news_client_keeps_sleep_fn_injectable_beside_a_throttle`、`tests/test_ratelimit.py::TestThrottleAndRateClockAreMutuallyExclusive::test_earnings_client_keeps_backoff_fn_injectable_beside_a_throttle` |
