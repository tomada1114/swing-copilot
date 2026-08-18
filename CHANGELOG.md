# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `copilot-backtest --db` が指す DuckDB の隣に `bars/` が無いとき、取引ゼロの
  レポートを書いて `exit 0` していた問題を修正した（Issue #217）。`--db` は
  価格 Parquet の根を `<db>/../bars` に暗黙で決めるが、その存在は誰も検証して
  おらず、`bars/` を並置し忘れたコピー DB を指すと全銘柄が「データ不足」で
  落ちて**正常終了・数秒・体裁の整ったレポート**という3点が揃い、操作ミスだと
  気づけなかった（#200 / PR #215 の A/B 実走で実際に踏んだ）。解決した `bars/`
  がディレクトリとして存在しない場合は、`Database` を開くよりも前・レポートを
  書くよりも前に、解決したパスと期待するレイアウトを添えて終了コード 1 で
  落ちる。「数銘柄だけバーが無い」（新規上場など）は従来どおり警告のみの
  fail-soft のままで、潰したのは両者が区別できないことだけである
- `<prior_verdicts>` の当否ラベルが 1 run 古かった問題を修正した
  （Issue #209）。`verdict_outcomes` へ書く唯一の経路である `retro_evaluate`
  ステップが、その表を読むステップ 6 の**後**に走っていたため、D 日に満期を
  迎えた当否がスキルへ届くのは D+1 の run だった——エントリは出るのに
  `HIT`/`MISS_*` 欄だけが空、という状態が毎日発生していた。`retro_collect`
  → `retro_evaluate` → `6_analysis_export` の順へ並べ替え、エクスポートの
  時間予算判定は両ステップの開始**前**に一度だけ確定させる（#207 と同じ
  先例。前段の帳簿作業が長引いたことが、スキルへの唯一の受け渡し口を
  スキップする理由になってはならない）。満期判定は従来どおり注入された
  `run_date` 基準で、壁時計には寄せない。`track_update` はエクスポートが
  その出力を読まないため後段に残る
- `--limit` を渡さない**本番経路**でだけ、保有銘柄が価格取得の対象集合へ
  合流していなかった問題を修正した（Issue #212）。`_select_symbols()` の
  `limit is None` 分岐だけが `held_symbols` を union しておらず、しかも
  この戻り値は日次経路で価格を取得する唯一の入口である。結果として
  S&P 500 スナップショットから外れた保有銘柄はその日の bar を 1 本も
  取得されず、トレーリングストップ・max-hold の手仕舞い判定とレポートの
  ポジション文脈が古い価格の上で走っていた——指数からの除外直後こそ
  手仕舞い判定を最も必要とする局面である。`--limit` 経路は元から union
  していたので、これに揃えた。追加されるのは**取得対象集合**だけで、
  `_run_step_screening()` は従来どおり `ScreeningInput.universe` を
  `deps.universe` との積集合に絞るため、スナップショット外の保有銘柄が
  新規エントリー候補として再浮上することはない。戻り値は両分岐とも
  辞書順に揃えた（ユニバースは元々 `ORDER BY symbol` で読むため本番の
  並びは実質不変）
- `analysis_input.json` の `<prior_verdicts>` が直近 2 営業日分を含んで
  いなかった問題を修正した（Issue #207）。`verdicts` 表へ書く唯一の経路で
  ある `retro_collect` ステップが、その表を読むステップ 6 の**後**に走って
  いたため、D 日のエクスポートに載る過去 verdict は D-2 日までだった。
  同一銘柄が数日おきに再候補化するスイングでは、直近 2 営業日こそ最も
  参照したい区間であり、そこだけが黙って空白になっていた。
  `retro_collect` をステップ 6 の直前へ移し、エクスポートの時間予算判定は
  収集の開始**前**に確定させる（エクスポートはスキルへの唯一の受け渡し口
  なので、後段の帳簿作業がそのスキップ理由になってはならない）。
  `as_of < run_date` の厳密不等号と同日 run の採用規則（`_adopted_runs`）は
  従来どおり

### Performance

- `copilot-retro collect` と日次の `retro_collect` ステップが、毎回すべての
  過去 run ディレクトリを再パース・再書き込みしていた問題を解消した
  （Issue #209）。ディレクトリの列挙は従来どおり全件（日付窓で切ると古い
  訂正を永久に拾えなくなるため）だが、`analysis_input.json` と
  `analysis_result.json` の内容ハッシュが前回取り込み時の値と一致する run
  は、パースも DELETE-then-INSERT も行わない。digest は新表
  `verdict_collections` へ行と同一トランザクションで書く。mtime やサイズ
  ではなく**内容**を根拠にするので、サイズが同じで更新時刻を復元した訂正で
  あっても必ず取り込み直される。`retro_evaluate` も日次実行時だけは記録済み
  スライスを飛ばす（verdict が訂正された場合と bar 欠損で欠けた銘柄がある
  場合は再分類される。株価訂正による再分類は従来どおり手動の
  `copilot-retro evaluate` / `prepare` が担う）。あわせて `collect` の出力に
  「解析 / 無変更」の run 数が加わった

### Added

- verdict 追跡台帳が `skip` も**同一の出口ルール**でシャドウ追跡するように
  なった（Issue #190、2026-08 アーキテクチャレビューの R7）。「verdict
  レイヤに価値があるか」は本質的に「proceed だけ買った場合 vs screening
  通過を全部買った場合」の差であり、片側しか追跡していない台帳では
  その反実仮想が作れなかった。`verdict_positions.recommendation`
  （nullable、NULL = `proceed`）が区分を持ち、両群が同じトレーリング
  ストップと最大保有日数で運ばれる。サンプル母数が採用少数派から候補
  全体へ広がる。`list` / `show` の既定表示は `proceed` のみ
  （`--recommendation` で明示的に開く）なので、日常操作の見え方は
  変わらない
- `copilot-track stats`: 勝率・プロフィットファクタ・期待値・平均 R 倍数・
  保有日数中央値・手仕舞い理由内訳を `proceed` / `skip` / `all` で層別して
  出す。損益はすべて % 単位（シャドウ建玉に株数の決定は存在しないため、
  各建玉を $100 notional へ正規化して測る）
- `retro_input.json` に `aggregates.tracked_performance` を追加
  （`metric:tracked_performance:{proceed,skip,all}`）。追跡台帳の実現損益が
  振り返りの証拠に入るようになった（従来 `retro/export.py` は台帳を一切
  参照していなかった）
- separation のペアード版と超過リターン版
  （`metric:separation_paired:*` / `metric:separation_paired_excess:*`）。
  従来の窓全体プール平均差は地合いと交絡しうるため、run 日ごとに
  proceed−skip を取ってから日次差を平均する版を併記する。片群しか無い日は
  除外し、除外日数を `excluded_day_count` に出す。超過版のために
  `verdict_outcomes.benchmark_return_pct`（nullable、backfill しない）を
  追加した
- 集約指標に散らばりの指標を追加した。`MetricEntry` に `stderr` /
  `ci_low` / `ci_high`、`RateMetricEntry` に Wilson スコア区間の
  `ci_low` / `ci_high`（いずれも両側 95%）。重み合成のヘッドラインには
  区間を出さない——5 日と 20 日は同じ run を測り直した非独立な 2 窓であり、
  そこから作った区間は実際より狭くなる。これに合わせて `swing-retro` の
  L1 証拠ゲートを「n≥20 かつ CI が 0 を跨がない」へ強化した

### Changed

- スクリーニングが読むローリング指標（SMA・Wilder RSI・Wilder ATR・出来高
  トレーリング平均）を、銘柄ごとに**全履歴で 1 回だけ**計算して日付で引く
  ようにした（Issue #214）。従来はバックテストの日次ループが銘柄ごとに
  全履歴の系列を組み直し、最後の 1 点だけ取って捨てていたため、計算量が
  `O(days × symbols × history_len)` になり、候補ストリーム生成が 1 変種の
  実行時間の 93%（2020-01-02〜2026-07-30・S&P 500 で 52 分／全 55.9 分）を
  占めていた。`screening/indicators.py` に `SymbolWindow` /
  `symbol_window()` を追加し、`ranking_metrics`・`trend_sma`・`pullback_rsi`・
  `volume_min`・`minervini_stage2` の SMA をこの経路へ移した。合成
  ユニバース（300 銘柄 × 2050 本 × 250 日）で 105 秒 → 2.5 秒。指標は
  すべて因果的なので `as_of` までの前置きはビット単位で一致し、
  バックテストの equity/trade は 1 セントも動かない（純粋な高速化）
- 勝率・プロフィットファクタ・期待値・R 倍数・保有日数・手仕舞い理由内訳の
  定義を `backtest/metrics.py` に一本化した（Issue #190）。同モジュールは
  `engine.Trade` ではなく `ClosedTrade` Protocol を受け取るようになり、
  バックテスト・紙トレ台帳（`paper/journal.py`）・verdict 追跡台帳の 3 者が
  同じ関数を通る。`PaperJournal._win_rate` などの private な二重実装は削除
  （`PerformanceSummary` の外形は不変）
- `copilot-daily` の「保有銘柄」（開示・ニュース収集の優先対象、設計 3.14）が
  読む仮想建玉を `proceed` に限定した。`skip` のシャドウ建玉には notional にも
  何も保有されておらず、含めると保有優先のテキスト予算が定性レイヤの落とした
  銘柄すべてへ向いてしまう
- `proceed` から `skip` へ訂正された verdict の追跡ポジションを**削除しなく
  なった**。両側を同じ出口ルールで追跡している以上リプレイは依然正しく、
  削除すると訂正のたびに skip 側の標本が痩せる。`recommendation` 列だけを
  verdict 側へ追随させ、その旨を note に出す。verdict 行そのものが消えた
  場合（銘柄が分析対象から外れた場合）は従来どおり削除する

- バックテストが本番と同じ系を測るようになった（Issue #184、2026-08
  アーキテクチャレビューの P1）。本番は候補と建玉の間にレジーム
  （`CASH_PRIORITY` / `REDUCE_ONLY`）・portfolio heat・決算・サーキット
  ブレーカー・セクター上限の 6 ゲートを置くが、シミュレータはどれも通して
  おらず、`reduce_only_risk_multiplier` / `max_portfolio_heat_pct` /
  `earnings_block_business_days` / `circuit_*` は定義上バックテストの数字を
  1 つも動かせなかった。新規 `backtest/policy.py` が唯一のポート
  `EntryPolicy` を定義し、本番の `RiskChecker` を**包んで**注入する
  （エンジン側にゲートを再実装しない）。ゲートの入力は必ずシグナル日の
  バーだけで評価する（約定は翌営業日寄付のため、当日バーでの判定は
  look-ahead になる）
- `copilot-backtest --policy none|regime|regime+risk`（カンマ区切りで複数
  指定可）。複数アームは同一の候補ストリーム・同一の `MarketFrame` で走り、
  指標とゲート発動回数を列比較したレポートを 1 コマンドで出す。`grid` は
  `--policy` 非対応で、既定以外を渡すと fail-fast する
- `BacktestResult` にエントリー計器を追加した。`entry_block_counts` /
  `entry_block_days`（「入らなかった理由」の候補件数と発動セッション数、
  発火 0 件の理由も 0 として必ず報告）、`avg_invested_pct`（各日の建玉時価 /
  equity の平均）、`max_concurrent_reached`
- `copilot-backtest` の候補生成をエンジン走行から分離した
  （`backtest/candidate_stream.py`、Issue #185）。`grid` の 25 セルと
  `--pessimistic` の 2 シナリオは 1 本の候補ストリームを共有し、支配的
  コストだったスクリーニングを 1 回しか払わない（フル期間で従来
  25 セル ≒ 22 時間 → 1 スクリーニング + 25 エンジン走行）。新フラグ
  `--candidate-cache PATH` で候補ストリームを Parquet に永続化し、CLI
  実行をまたいで再利用できる。キャッシュキーはスクリーニングが読む入力
  （戦略 spec・`technical_signals`・`fundamental_filters`・ユニバース・
  銘柄・期間・ベンチマーク・価格/ファンダの内容ダイジェスト）のみで構成し、
  `settings.backtest`・`settings.risk`・初期資金では無効化されない。
  注入ストリームのキーは実行毎に再検証され、不一致は fail-fast。
  キャッシュ有無で `BacktestResult` は bit-exact に一致する
- 読み取り専用のリサーチ API `swing_copilot.research` を追加した。蓄積された
  判断履歴（verdict・当否・スコア内訳・追跡台帳・レジーム・落選理由）を
  pandas DataFrame として読み出す。`research.scorecard()` は verdict × 当否 ×
  スコア内訳 × リスク制約 × レジーム × 追跡結果 × セクターを 1 行で返す。
  `Database` に `read_only=True` を追加し、各アクセサはクエリ毎に read-only
  接続を即開閉する（誤書き込み・DDL の構造的排除と、ロック保持の最小化）。
  結合済み分析ビュー `v_verdict_scorecard` / `v_candidates` /
  `v_tracked_positions` / `v_symbol_sector_asof`（as-of inclusive なセクター
  解決の唯一の実装）を `storage/schema.py` に定義し、`init_schema()` が
  `CREATE OR REPLACE` で自己移行する。使い方とデータ辞書は
  `docs/09_research_guide.md`、アドホック分析の入口は `swing-research`
  スキル。2026-08 アーキテクチャレビュー（`docs/08_architecture_review_2026-08.md`、
  改修計画は Issue #184〜#195）の実装第一弾
- **同一銘柄が再候補になったとき、過去の verdict とその後の当否が
  `analysis_input.json` に載るようになった**（Issue #191、2026-08 アーキテクチャ
  レビューの P1）。従来スキルは「前回自分がどの根拠で proceed/skip し、その後
  どうなったか」を見る経路が構造的に無く、繰り返し外している根拠パターンに
  自ら気づけなかった。`candidates[].prior_verdicts` に、同一銘柄・戦略の過去
  verdict と成熟済みの `HIT`/`MISS_*`・`forward_return_pct` を対で載せる。
  人間の記帳である `decision_history` とは別読み（`trades_journal` は人間が
  記録したときしか行を持たないため）。時点整合性は `as_of < run_date` の厳密
  不等号で、過去 run の `source_id` は持ち帰らない
- **`verdict.reasons[].basis`（根拠タイプの閉集合タグ）を追加した**（Issue
  #191）。`technical_score` / `news_catalyst` / `filing_fundamental` /
  `risk_sizing` / `market_regime` / `peer_relative` の 6 値。retro に
  `basis_contribution`（根拠タイプ別の verdict 件数と HIT 比率）が加わり、
  「決算根拠の proceed」と「テクニカルのみ根拠の proceed」のヒット率を初めて
  比較できる。従来 `source_contribution` は provider 単位までしか切れなかった。
  タグの無い理由は `untagged` として計上し、タグ付与率そのものを可視化する。
  ingest はこのタグの正しさを検証できない（照合先が入力に無い）ため、スキル
  文書側で「自信が無ければ省略する」を規約化した
- **`score_breakdown` に加重前の生値が並ぶようになった**（Issue #191）。
  `close` / `rsi14` / `sma50` / `sma200` / `avg_volume` と導出 `atr14_pct` を
  「参考情報（コード計算・上書き不可）」として同じブロック内へ追記する。
  正規化済みの加重値だけでは RSI14 が 28 なのか 44 なのかを区別できず、
  押し目の深さという定性的読みがまさにその情報に依存していた
- スキーマとスキル文書のドリフト検出テスト（Issue #191）。`AnalysisResult` の
  pydantic `model_fields` を再帰的に走査し、全フィールド名と全 `basis` 値が
  `references/output-schema.md` に現れることを検証する

### Changed

- `analysis.sufficient_news_mention_items`（既定 5）を新設し、`news_supply` の
  `sufficient` しきい値を設定化した（Issue #191）。従来は
  `news_supply.py` の定数で、1 run の較正値がそのまま固定されていた
- 開示の per-symbol 予算配分順を「決算関連 8-K > 10-Q > その他」へ再設計した
  （Issue #191）。従来は 10-Q 固定優先で、予算枯渇時に決算プレスリリース
  （`EX-99*` を伴う 8-K）だけが `omitted_symbol_budget` になりえた。
  変わるのは配分順だけで、返却順は従来どおり新しい順

### Fixed

- `copilot-daily --limit N` が `universe[:limit]`、つまり「A で始まる N 銘柄」
  を選んでいた（Issue #205）。#194（PR #202）で `copilot-backtest` 側だけを
  直したため取り残されていた片側である。`--limit` は検証・スモーク用フラグで
  本番の定時実行は付けないので本番レポートは歪んでいなかったが、Minervini の
  RS パーセンタイル（条件7）は*渡された集合内の相対順位*なので、スモーク実行は
  本番と別の条件を検証していた。fundamentals の NULL 率のようなカバレッジ
  スポットチェックも常に同じアルファベット先頭群を測っていた。サンプラを
  `swing_copilot/universe_sampling.py`（`select_universe_sample`）へ抽出し、
  両 CLI が同じ関数・同じ salt を共有する。同じユニバースと同じ `N` なら
  スモーク実行とバックテストが同じ銘柄集合を見る。`--limit 0`（保有銘柄のみ）・
  負数の usage error・保有銘柄の常時 union という既存の不変条件は変わらない
- `vcp_breakout` が VCP を「履歴全域の一本のパターン」と定義しており、
  `pattern_days` がほぼ履歴全長になって 6.5 年で 1 トレードしか生成しない
  構造欠陥を修正した（Issue #186）。`extract_pattern` は直近
  `max_contractions` 個（`technical_signals.vcp.max_contractions`、既定 4）
  の収縮だけを 1 パターンとして採用し、シグナルは評価前に入力を自身の
  `required_bars` へ切り詰めるため、判定は呼び出し側の履歴供給長に依存
  しない。各 Signal は必要バー数を `required_bars` として宣言し、日次
  パイプラインとバックテストは `ScreeningPipeline.required_bars` から
  導いた同一のルックバック（`default` 400 暦日 / `vcp_breakout` 770 暦日）
  を読むため、本番 400 日 / バックテスト 730 日のハードコード乖離も解消
  した。同一条件の再バックテストは 70 トレード
  （`reports/backtests/2026-08-17-vcp-redefinition.md`）
- `record_risk_assessments` が銘柄ごとに独立コミットしており、途中失敗で
  1 run 分のリスク評価が部分的に残り得た（「1 論理書き込み = 1 トランザク
  ション」不変条件の違反）。明示トランザクションで包み、失敗時は全行
  ロールバックするようにした
- `copilot-daily` の exit code 2（preflight abort）が「同日再実行」と
  「`risk.account_equity_usd` 未設定」を区別できず、無人実行の swing-daily
  スキルが設定不備を「本日は分析済み」と誤要約してサイレントに no-op し
  続けるリスクがあった。`PreflightAbort` に閉集合の `reason` を持たせ、
  stderr 先頭へ機械可読な `PREFLIGHT_ABORT[<reason>]:` プレフィックスを
  書く契約へ変更し、スキル側の分岐を更新した
- `backtest.exit_atr_period` が死んだ設定キーだった（`settings.yaml` にあり
  strict スキーマも受け付けるのに、`backtest/exits.py` の `ATR_PERIOD = 14`
  固定でどこからも読まれず、「設定した = 検証した」の誤認を招いていた）。
  `atr14_as_of` / `atr14_by_date` を `atr_as_of` / `atr_by_date`
  （`period` 引数は既定値なし）へ一般化し、`BacktestEngine` のトレーリング
  ストップと `tracking/update.py` の台帳リプレイが
  `settings.backtest.exit_atr_period` を渡すようにした（Issue #194）。
  これで #185 の感応度グリッドの 1 次元スイープ対象にできる。エントリー側の
  ストップ距離はランキング指標 `atr14`（`screening/pipeline.py` の
  `_ATR_WINDOW`、本番 `risk/checks.py` と同じ値）のままで、意図的に統合
  していない
- `copilot-backtest --limit N` が `ORDER BY symbol` の先頭 N 件、つまり
  「A で始まる N 銘柄」を返していた（Issue #194）。セクター構成が S&P 500 と
  別物になるうえ、Minervini の RS パーセンタイル（条件 7）のように*渡された
  集合内の相対順位*で決まるチェックは条件の意味自体が変わる。`gics_sector`
  で比例配分（最大剰余法）したうえで各セクター内を salt 付き blake2b の
  ハッシュ順に選ぶ決定論的サンプリングへ変更し、採用方式・実銘柄数・
  セクター構成を terminal と markdown 双方のレポート冒頭に必ず出すように
  した（生存者バイアス注記と同じ扱い）

### Changed

- **バックテストのサイジング基底を `cash` から `equity`（現金＋建玉時価）へ
  変更した**（Issue #184、既存の数値は新基準で読み直しが必要）。旧来の現金
  基準は保有が増えるたびにサイズを `0.9^n` で縮め、10 銘柄満玉でも投下資本が
  約 65% にしかならず、固定 `account_equity_usd` 基準で建てる本番とは別の系を
  測っていた。時価評価はシグナル日の終値までで行う。`copilot-backtest` は
  `--policy` の指定有無にかかわらず SPY / QQQ / ^VIX のバーを常に読み込む
  （アームごとに候補ストリームのキャッシュキーが変わると A/B が成立しない
  ため）。この変更で既存の `--candidate-cache` は一度だけ再生成される
- 8-K の export 選別を先頭スライスから**価値ベースの Exhibit 選別**へ変更した
  （Issue #181）。`analysis/filing_selection.py` が `content_text` を
  `[EXHIBIT ...]` ヘッダで主文書と各 Exhibit へ分割し、主文書＋プレスリリース
  （`EX-99` / `EX-99.1` / `EX-99.01`、無ければ先頭 Exhibit）と supplemental
  package へ 4:1 で配分したうえで、余りを不足率順ではなく優先層→文書順で配る。
  割当に収まらない Exhibit は末尾切りではなく、空行で分けたブロックを
  markdown テーブル → 通常本文 → 定型文（forward-looking statements 免責、
  About、IR/メディア連絡先、conference call / webcast 案内）の順に採用し、
  落ちた箇所へ `[... omitted lower-value exhibit passage ...]` を挿入する。
  末尾に置かれる財務諸表・非 GAAP 調整表が真っ先に落ちていた問題
  （Issue #157 の GOOG 申告）への対処。選別結果は `selection_mode`
  （`section_priority_partial`、値は増やしていない）と `sections_json`
  （`exhibit_primary` / `exhibit_ex_99_1` …）へ記録され、P8 から読める。
  取得段・export 予算値（120,000 / 240,000）・10-Q の章選別・CON-03 検査と
  provenance 検証の経路は変更していない
- `FilingSectionOmissionShape` に `value_selected` を追加した（Issue #181）。
  Exhibit の欠落位置が「章の中間」（`head_and_tail`）でも「先頭以降」
  （`head_only`）でもなく、本文中のマーカーの位置であることを表す。追加のみで
  既存アーカイブは読めるため、スキーマは `analysis-input-v3` に据え置き

- 8-K Exhibit の取得段上限（`data/edgar.py` の `_MAX_EXHIBIT_CHARS_PER_FILING`）を
  60,000 字から 500,000 字へ引き上げ、その意味を「export 予算からの逆算値」から
  「病的な文書に対する安全弁」へ変えた（Issue #180）。取得段で切ると
  `text_items.content_text` に切れた状態が保存されて不可逆になるため、切り詰めの
  一次責務は export 段（`analysis/filing_selection.py`）へ一元化する。Issue #165 の
  リプレイ実測では対象 5 件すべてが旧上限を超えていた（最大 375,403 字）。
  `analysis_input.json` の上限は据え置き（1 開示 120,000 字、1 銘柄 240,000 字）で、
  8-K の実効サイズが 1 開示あたり最大 120,000 字へ漸近する。上限の消費方式・
  `[... exhibit truncated ...]` marker・`coverage.exhibit_truncated` の読み戻しは
  変更していない
- `technical_signals.pullback.band_atr_multiple` を `null` から `2.0` へ変更し、
  プルバック帯の判定を絶対%（`sma_band_pct`）から ATR14 単位へ切り替えた。
  `reports/backtests/2026-07-30-strategy-comparison.md` の R2（期待値・PF・
  Sharpe 改善、DD 同等）に基づく採用判断。`score_weights.atr_pct` は R3 で
  上積みが観測されなかったため `0.0` のまま見送り
- `regime/distribution.py` の `calculate_distribution_days` を、1日ごとの
  `DataFrame.iloc` 行アクセスと末尾スライスから、close/volume を一度リスト化
  したうえで「その日より後の最高値」を逆順に積み上げた配列と比較する走査へ
  変更した。判定結果は変えていない（160本の履歴を全`as_of`・NaN あり・
  短い履歴で照合済み）。`scan_forward` が観測日ごとに呼ぶため、
  `copilot-dd-forward` と日次パイプラインの regime 判定が速くなる。
  計測は 0.83s → 0.06s（14.7x）、`pytest tests/regime` は 107s → 5.0s。
  NaN の close は明示的に読み飛ばす。置き換え前の `NaN >= threshold` が
  常に偽だったのと同じ扱いで、欠損を回復と誤認させないため
- `just smoke` のビルドを `uv build --wheel` に変更し、`scripts/smoke_test.py`
  の `uv pip install` を `--no-cache` から `--refresh-package <project>` へ
  変更した。検証対象の wheel は毎回ディスクから読み直すが、pandas/pyarrow/
  duckdb などの依存は共有 uv キャッシュから引く。sdist を含む完全なビルドは
  `just build` と CI / release ワークフローが引き続き行う。ローカルの
  `just verify` で smoke が 16s → 4.1s、smoke 用ビルドが 21s（cold）→ 1.0s

### Removed

- `retro.approval_mode`（`auto` | `manual`、既定 `auto`）を削除した
  （Issue #178）。将来の細粒度介入への切替余地として名前だけ予約されていた
  設定だが、この値を読んで挙動を変えるコードは存在せず、`manual` と書いても
  L1 提案は従来どおり即時適用されて PR になっていた。「書けるが効かない設定」
  を受理し続けるのは AGENTS.md の fail-fast validation と衝突するため、
  予約を取り下げた。`settings.yaml` に `approval_mode` が残っていると
  `extra="forbid"` により起動時に失敗するので、キーごと削除すること。
  `retro_input.json` の `config_snapshot.sections.retro` は `max_surprises`
  のみになり、**その結果 `config_hash` の値が変わる**（過去の dossier /
  提案台帳との hash 連続性は切れるが、再生成・backfill は行わない）。
  予約の経緯は `docs/goal-prompts/swing-copilot-retrospective/` に履歴として
  残る。per-proposal の人手承認が必要になった時点で、改めて設計に載せてから
  追加する

### Added

- 自社材料の供給量（`news_supply`、Issue #130）を P8 の振り返りへ取り込み、
  しきい値 `SUFFICIENT_SYMBOL_MENTION_ITEMS = 5` の妥当性を実績で検証できる
  ようにした（Issue #154）。`retro/collect.py` が `analysis_input.json` から
  `verdicts` の `news_supply_*` 4 列（nullable、`ADD COLUMN IF NOT EXISTS`、
  backfill なし＝`NULL` は「未計測」であって `none` ではない）へ取り込み、
  `retro_input.json` の `aggregates.news_supply` に level × recommendation の
  クロス集計（セルごとの件数と `symbol_mention_items` の min/max/mean、全体の
  `sufficient_threshold` と未計測件数）を出す。`verdict_mix` と同じく窓内
  `verdicts` を直接読むため成熟を待たずに算出できる。level だけでなく 3 つの
  件数も持つのは、後から別のしきい値で再採点するときに `reports/` の再走査を
  要らなくするため。旧アーカイブ由来の未計測行は `none` へ畳まず
  `unrecorded` という第 4 の level として数える。各サプライズ dossier にも
  当時の `news_supply` を付ける——コードが数えられるのは「`sparse`/`none` で
  どれだけ `proceed` が出たか」までで、`sufficient` なのに材料が薄かった
  偽陰性はスキルの再読でしか言えないため（`symbol_mention_items` は
  ティッカー出現数であり自社材料数の下限値）。`aggregates.news_supply` と
  サプライズの `news_supply` は既定 `null` の optional で、`input_digest` は
  この既定を落として計算するため、過去の `retro-input-v1` dossier も検証を
  通り続ける。しきい値 5 自体は変更していない
- `copilot-dd-forward`（`regime/dd_forward_cli.py`、コアは`regime/dd_forward.py`
  と`regime/dd_forward_sweep.py`）。保存済み履歴を1日ずつ`as_of`として再生し、
  `_calculate_regime_snapshot`と同一の分類を出したうえで、その後に実際に起きた
  リターンとドローダウンを Distribution Day 水準別に集計する読み取り専用の
  診断CLI。`regime.dd_*`は roadmap §5 P3-13 で要検証のまま本番に入っていたが、
  `backtest/`は`regime.exposure`も`regime.distribution`もimportしていないため
  `copilot-backtest`では効果を1つも測れなかった。対象はSPY・QQQと`--as-of`時点
  スナップショットの等加重バスケット、既定の保有期間は5/10/25営業日
  （25は`backtest.max_hold_days`）。`--sweep`は閾値の一変数感度、`--grid`は
  順序制約を満たすグリッドの全走査を`CASH_PRIORITY`軸と`REDUCE_ONLY`軸に
  分けて出す。`dd_caution_d25`は`_base_exposure`が`CAUTION`と`NORMAL`を同じ
  分岐に落とすため掃引対象から外している（露出上限を1日も動かせない）。
  `config/settings.yaml`は変更していない
- run成果物`reports/<run_date>/<run_id>/rejections.json`（schema
  `rejections-v1`、`report/rejections.py`）。落選明細はDuckDBの
  `screening_rejections`にしかなく、run成果物にはreason_code別の**件数**しか
  残っていなかったため、「どの銘柄がなぜ落ちたか」を見るにはDBを引く必要が
  あった。あわせて`truncated_by_candidate_limit`節を持ち、全Filter・全Signalを
  通過しながら`candidate_limit`で順位落ちした銘柄（`symbol`・切り捨て前の通し
  `rank`・`score`・スコア内訳・実行状態）を残す。**これらは従来どこにも
  記録されていなかった**——順位落ちは落選ではないため落選台帳に載らず、上限外
  なので候補にも載らない。DuckDBスキーマは変更していない（`reason_code`は
  CHECK制約で守られた閉集合であり、順位落ちに充てられる値が存在しない）。
  書き出しは一時ファイル＋`os.replace`で原子的に行い、失敗しても
  `RunStatus.DEGRADED`（終了コード0）に留まる
- `copilot-filter-matrix`（`screening/filter_matrix_cli.py`）。設定済みの
  フィルタとシグナルを1つずつ独立に全ユニバースへ適用し、チェック別の
  単独通過率・落選チェック数の分布・同時落選マトリクス・「そのチェックだけで
  落ちている」銘柄数を出す読み取り専用の診断CLI。落選台帳は銘柄ごとに最初の
  失敗1件しか持たないため、各条件が単独でどれだけ落としているかも、重複構造も
  既存データからは分からなかった。データ不足（履歴不足・ファンダ欠損）は
  `rejection_classifier`の`data_quality`ステージをそのまま使って落選とは
  別カテゴリで数える。シグナルはフィルタ通過後ではなく全ユニバースに対して
  評価するので、通過数は日次runの候補数とは一致しない（母集団依存の
  `minervini_stage2`は表の下に注意行を出す）。「全チェック通過」と
  「候補相当」は別に出す——`ScreeningPipeline`はチェックの後にランキング指標
  （SMA200）の有無と`signals_all`空の判定でさらに絞るため、0個バケツを
  そのまま候補数として読むと落選台帳と食い違う。チェックの実体は
  `ScreeningPipeline`と共有する`build_strategy_components`で組み立てるため、
  スクリーニングロジックのミラーは増えていない（同じキーの重複記載は1回だけ
  測る）。完全にオフラインで、ユニバースは`--as-of`時点で可視な永続
  スナップショットだけを使い（無ければ再取得せずエラー）、ローカルにバーも
  ファンダも無い未取得銘柄は母集団から除外して件数を報告する。
  スクリーニング結果の行は1行も書かず、スキーママイグレーションも実行せず、
  `--db`が無ければ作らずにエラーにする（ただし`MarketStore`は共有DuckDBを
  読み書きで開くので`copilot-daily`と同時には走らせない）。`--json`の
  書き出しは同ディレクトリの一時ファイル＋`os.replace`
- `copilot-backfill`（`pipeline/backfill.py`）。バックテストに必要な過去の
  バー／ファンダメンタルズを一度だけ取り込む一回限りのCLI。日次runは400暦日の
  ローリング窓しか取らないため、複数レジームをまたぐ検証に足る履歴が
  ローカルに存在しなかった。50銘柄チャンク＋チャンク間2秒スリープで取得し、
  `write_bars`は年パーティション全書き直しのコストを避けるため最後に1回だけ
  呼ぶ。既存バーが`--start`まで届いている銘柄はネットワークを叩かずに
  スキップする（`--start`は暦日で市場休日を指しうるため、判定には
  `COVERAGE_TOLERANCE_DAYS`=7暦日の猶予がある）。銘柄単位の失敗は
  fail-softで集約報告し、`logging.exception`でトレースを残す。
  1銘柄も取得できず書き込みが0行だった場合のみ終了コード1で落ちる。
  **ベンチマーク／レジーム系（`SPY`・`QQQ`・`^VIX`・`^TNX`）はS&P 500
  ユニバースに含まれないため`--symbols`で別途取得が必要**——特に`SPY`は
  バックテストの取引日カレンダーそのものである
- 低ボラバイアス是正の2つのスイッチ（いずれも**既定では無効**で、既定挙動は
  不変）。`technical_signals.pullback.band_atr_multiple`（既定`null`）は
  SMA50からの距離をATR14単位で測るモードで、絶対3%帯が低ボラ銘柄を高ボラ銘柄の
  約4.5倍通過させていた事実上のローボラフィルタを解消する。ATRがNaNまたは0の
  ときは距離が定義できないため帯を閉じる。帯で落ちた銘柄は却下台帳でも
  帯が理由だと分かる（RSIが閾値を通っているのに`SIGNAL_RSI_NOT_MET`と
  記録されない）。`ranking.score_weights.atr_pct`
  （既定`0.0`）はATR%が高いほど高得点の成分で、ATR% 6%満点の絶対正規化
  （候補n≈5のパーセンタイルは小標本ノイズを再生産するため採らない）。
  加重後の値は`score_atr_pct`としてスコア内訳（レポート・`analysis_input.json`
  の`<score_breakdown>`）にも出る——合計と内訳行が食い違わないようにするため
- バックテスト`run`レポート（`--pessimistic`の通常vs悲観比較を含む）に
  `Exit breakdown`セクション。決済理由の内訳
  （発火0件の理由も0として必ず表示）、`max_hold`バインド率、実保有日数の
  中央値と四分位。感応度グリッドのMaxHold列が全て同値だったとき「効かない」のか
  「一度も発火していない」のかを区別するための計器。`Trade.days_held`を追加
- `copilot-backtest`の`--settings` / `--strategies`。リポジトリの設定を
  書き換えずに設定バリアントを比較するための入り口。`grid`サブコマンドの
  前後どちらに置いても効く（サブパーサ側の既定値でリポジトリ設定に
  巻き戻らない）

### Changed

- サポートするPythonを3.14単一に引き上げ（`requires-python = ">=3.14"`）。
  ローカル（`.python-version`）・lint・build・docs・pip-audit・releaseは元々
  すべて3.12固定で、テストマトリクスだけが3.12/3.13/3.14に広がっており、
  「実際に開発・リリースしている版」と「テストしている版の集合」が一致して
  いなかった。ruffの`target-version`とmypyの`python_version`も3.14へ揃える。
  PEP 758により`except`の括弧が不要になったため、`ruff format`が3箇所を
  `except A, B:`へ整形する（Python 2の構文に見えるが3.14の正式な文法）。
  CIの`test`ジョブはマトリクスを廃して単一ジョブになり、あわせて3.12だけに
  付いていたcodecovアップロードの条件分岐（他バージョンより所要時間が
  長くなる原因だった）も不要になった。将来的な複数バージョン検証は
  `matrix.python-version`を戻すだけで再開できる
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

- 8-K の `EX-99*` 添付が取得段の**件数上限**（1 開示 3 件）で落とされていても
  `analysis_input.json` の `coverage` が `exhibit_truncated: false` を主張して
  いた問題を解消（Issue #163）。Issue #157 が塞いだのは文字数上限だけで、
  4 本目以降の添付は文字数予算の経路へ入る前に落ちるためマーカーも警告も
  残らなかった。`data/edgar.py` は件数上限で取得しなかった添付を
  `text/base.py::EXHIBIT_OMISSION_MARKER`
  （`\n[... exhibit omitted: per-filing exhibit count cap ...]`）として本文へ
  申告し、`analysis/filing_selection.py` は `EXHIBIT_LOSS_MARKERS` の
  いずれかで `exhibit_truncated` を立てる。「末尾が切れた Exhibit」と
  「1 文字も取得していない Exhibit」は本文中の別リテラルで区別でき、
  `coverage` 側は既存の 1 つの boolean にまとめる（新フィールドも DB 列も
  増やしていないので、過去のアーカイブと `analysis_source_coverage` の
  スキーマは変わらない）。件数上限のマーカーは fail-soft の外で付けるため、
  後続のダウンロード失敗があっても「そもそも取得機会が無かった」事実は残る。
  `_MAX_EXHIBITS_PER_FILING = 3` という値自体は変更していない
- UTF-8 として解釈できない `report_context.json` / `retro_input.json` /
  `retro_result.json` が、それぞれ `AnalysisIngestError` / `RetroIngestError`
  ではなく生の `UnicodeDecodeError` として伝播していた問題を解消
  （Issue #164）。Issue #153 の修正は `analysis/validate.py` の読み取り経路
  だけを直しており、`analysis/snapshot.py::_read_payload` と
  `retro/validate.py::_load` は `except OSError` のままだった
  （`UnicodeDecodeError` は `ValueError` のサブクラスであって `OSError` では
  ない）。読み取りと JSON パースを `analysis/validate.py::read_json_document()`
  に集約し、例外型とメッセージ接頭辞だけを引数で受けるようにして、
  `read_analysis_document()` を含む 3 経路すべてが同じ実装を通るようにした。
  無人の定時実行が生成した文字化け成果物こそ「壊れた成果物」として
  扱われる必要があり、呼び出し側はそれを例外型だけで判別している
- 8-K Exhibit が取得段の 60,000 字上限で切られていても
  `analysis_input.json` の `coverage` が `is_truncated: false` /
  `selection_mode: "full"` を主張していた問題を解消（Issue #157）。既存の
  文字数 3 点は export 段の欠落しか語れず、取得段で切られたテキストが
  そのまま「原文」として数えられていた。`FilingCoverage` に
  `exhibit_truncated`（既定 `false`）を追加し、`analysis/filing_selection.py`
  が `TextItem.content_text` 中の切り詰めマーカー
  （`text/base.py::EXHIBIT_TRUNCATION_MARKER`、`data/edgar.py` と共有）の
  有無で判定する。マーカーは永続化される本文の中にあるため、DB から
  `TextItem` を読み直す（あるいは再取得する）P8 の `retro/surprises.py` でも
  同じ値になる。`false` は「マーカーが無い」であって「欠落が無い」ではない。
  既定値により過去の `analysis-input-v2`/`v3` アーカイブは引き続き読める
  （`input_digest` 検証は生 JSON を対象にするため影響しない）。
  併せて、予算を使い切って**丸ごと落とす** Exhibit にもマーカーを付けるように
  した（従来はテキストが 1 文字も入らずマーカーの書き先が無いため、上限が
  効いた事実自体が消えていた）
- P8 の振り返りが、取得段で切られた開示を「入力は完全だった」と数えていた問題を
  解消（Issue #157）。`analysis_source_coverage` に `exhibit_truncated BOOLEAN`
  （nullable、backfill なし＝既存行は `NULL`＝未記録）を追加し、
  `retro/collect.py` は pydantic の `model_fields_set` を見てアーカイブが実際に
  記載していた場合だけ保存する。`input_coverage` の
  `severe_miss_symbol_count_with_gap` は両方の段の切り詰めで立ち、
  `without_gap` は全行が「gap 無しかつ記録済み」のときだけ立つ（未記録を含む
  銘柄は `unknown`）。取得段の件数は新設の `exhibit_truncated_filing_count`
  （既定 0＝未集計）で出す
- 8-K Exhibit（EX-99*）由来テキストで、表セル内の単語と数値が途中で切られ
  `…` に置き換わっていた問題を解消（Issue #156）。`data/edgar.py` は
  edgartools の `Attachment.text()` に変換を委ねていたが、これは Exhibit の
  HTML を Rich で固定コンソール幅にレイアウトし、収まらないセルを省略記号で
  打ち切る。実測 run では `…` が 1,708 個、うち 540 個が数値の途中
  （`1,543,…`・`135,8…`）で、単位表記も `(In th… ex… per sh…` となり桁が
  復元できず、AC16（`text` の数値と `evidence_quote` の数値を桁まで一致させる）
  が原理的に守れなかった。新設の `_exhibit_plain_text()` が
  `Attachment.markdown()` と同じ変換（`get_clean_html()` → `to_markdown()`）を
  行う——markdown の表には収めるべき幅が無いので、列数がいくつでもセルが
  切られない。バイナリ Exhibit は拡張子で弾き、ダウンロードもしなくなった。
  10-Q 本文の抽出経路（元から無傷）と Exhibit の 60,000 字上限は変更していない
- UTF-8 として解釈できない `analysis_input.json` / `analysis_result.json` が
  `AnalysisIngestError` ではなく生の `UnicodeDecodeError` として伝播していた
  問題を解消。`UnicodeDecodeError` は `ValueError` のサブクラスであって
  `OSError` ではないため、`analysis/validate.py` の読み取り段の except を
  すり抜けており、`copilot-ingest-analysis` の呼び出し側（`analysis/cli.py`、
  `swing-daily` Step 5）が「壊れた成果物」と「想定外の異常」を例外型で
  区別できなかった。読み取りと JSON パースを `read_analysis_document()` に
  切り出し、`copilot-verify-analysis` の独自捕捉もこれを呼ぶようにして、
  事前検査と本番 ingest の失敗型を揃えた
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
