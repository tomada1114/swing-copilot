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

## 定性分析の境界（`analysis/`、FR-08・CON-03）

定性分析はこのプロセスの中では行わない。`swing_copilot.analysis`は、日次バッチと
Claude Codeスキル（`.claude/skills/swing-daily`系）の間の**ファイルを介した境界**
であり、モデルAPIを一切呼ばない。

`analysis/export.py`は`copilot-daily`のステップ6で、候補ごとの決定論的文脈
（`analysis/context.py`が整形したP1-01スコア内訳・P1-03リスク制約・P1-06実現損益
サマリ・市場レジーム・過去判断）と、ステップ5で収集済みの未信頼テキストを
`reports/<run_date>/<run_id>/analysis_input.json`（schema `analysis-input-v3`）へまとめ、宛先と同じディレクトリの
一時ファイル＋`os.replace()`で原子的に書き出す。ニュースは
`settings.analysis.max_news_items_per_symbol`件・各`max_news_chars_per_item`文字、
開示は1件`max_filing_chars`文字、1銘柄合計`max_filing_chars_per_symbol`文字までとする。
10-Q/10-Q-Aは財務諸表、MD&A、リスク要因、法的手続を章優先で構成し、抽出不能時のみ
先頭スライスへ縮退する。各開示の`coverage`には元/出力文字数、選択方式、章の
`full`/`partial`/`missing`が入り、過去の`analysis-input-v2`はP8アーカイブ読み込みだけ
後方互換で受理する。
ニュースも開示も無い候補を除外しない——`screening_assessment`と`verdict`は
どの候補にも等しく必要だからである。symbolを持たないマクロ／経済カレンダーの
`TextItem`（`source_type="calendar"`）はどの候補にも属さないため、run単位の
`context.calendar_events`として`max_calendar_events`件・各
`max_calendar_chars_per_item`文字までに切り詰めて別出しする。

`analysis/validate.py`はスキルが書いた`analysis_result.json`
（schema `analysis-result-v3`。新規runはこのバージョン以外をhard failさせ、
`analysis-result-v2`はP8アーカイブ読み込みだけ後方互換で受理する）を検証する。
ingestはまず3文書の`run_id`、`as_of`、
`strategy_key`、完全なinput digestを照合し、不一致なら既存reportと`latest.md`を変更せずhard failする。銘柄ごとに、(1) strictスキーマ
（`extra="forbid"`）で解析できること、(2) 引用された`source_id`がすべて当該銘柄に
ついて実際に供給したもの、または`context.calendar_events`のID（run単位でどの銘柄
からも引用可）であり、各`SourcedFact`が1件以上引用していること、(3) 各`SourcedFact`の
`evidence_quote`（12〜300字）が、その`source_ids`のいずれかの本文（ニュースは
見出し＋要約、開示は入力に渡された`text`、カレンダーイベントはタイトル＋要約）に
Unicode NFKC正規化・記号統一・空白畳み込み・大小無視のうえで実在すること、
(4) 表示テキストが`analysis/safety.py`のCON-03検査を通ること、を確かめる。
(2)(3)(4)の違反は**銘柄単位のfail-closed**で、当該銘柄の定性セクションを保留
（`SymbolOutcome.error`を設定し他フィールドを空に）してログへ残すだけで、
リトライしない。(3)は、別銘柄の本文を読みながら自分の正しい`source_id`だけを
申告するような取り違えを機械的に検出するための検査である。文書が読めない・JSONでない・`as_of`が入力と食い違う場合だけは
run全体のhard failとする——別の取引日を記述しているかもしれないファイルに
「安全な部分読み込み」は存在しないためである。

書類種別・提出日・ソースURLはコードが所有するメタデータとして
`analysis_input.json`から解決し、スキルの申告値を採用しない。

## `copilot-ingest-analysis`とレポート再描画

`analysis/cli.py`（`copilot-ingest-analysis`）はネットワークにも接続せず、
スクリーニング・リスク・ランキングを再計算しない。日次runが
`analysis/snapshot.py`で保存した`report_context.json`（schema `report-context-v2`、
表示非依存の`DailyBrief`のスナップショット）を読み直し、候補ごとの定性欄と
run単位の`no_trade`/`no_trade_reason`だけを差し替えて同じMarkdownを再生成する。
スコア・サイジング・実行状態・落選・レジームは無変更で持ち越す。

`report/daily_brief.py::build_analysis_brief()`は不合格経路をすべて
`degraded=True`＋説明文へ畳む: 分析未実施は「分析待ち」、当該銘柄が分析対象外なら
「定性分析なし」、検証で保留された銘柄は「検証不合格のため非表示」。
`format_verdict()`は`degraded`または`verdict`が`None`のとき`None`を返して
**何も描画しない**——沈黙が「懸念なし」と読まれてはならないためである。
verdictがあるときだけ`⚠ 定性: 見送り推奨（要約）`または`✓ 定性: 懸念なし`を出す。

`reports/<run_date>/<run_id>/`に残る`analysis_input.json`・`analysis_result.json`・
`report_context.json`の3ファイルが、そのままNFR-05の監査証跡になる。

## `copilot-retro`とverdictの当否評価

`retro/cli.py`（`copilot-retro`）は振り返り機構のCLIで、日次フローとは独立に
数日おき手動で回す。`collect`/`evaluate`/`export`、その3つを順に走らせる
umbrella `prepare`、そしてスキルの回答を検証する`ingest`の5つを持つ。

```bash
copilot-retro collect --reports-dir reports          # verdictをDuckDBへ取り込む
copilot-retro evaluate --as-of 2027-03-11            # 満期を迎えた当否を分類する
copilot-retro export --as-of 2027-03-11              # 証拠一式をJSONへ書き出す
copilot-retro prepare --as-of 2027-03-11             # 上記3つをまとめて実行する
copilot-retro ingest reports/retro/2027-03-11        # スキルの回答を検証して記録する
```

`collect`は`reports/<date>/<run_id>/analysis_result.json`を走査し、run単位の
完全置換で`verdicts`/`verdict_sources`へ取り込む。`strategy_key`と
`source_type`はコードが所有する`analysis_input.json`から解決し、スキルの
申告値を採用しない。文書欠損・解析不能のrunと入力側に存在しない`source_id`は
noteを残してスキップする（fail-soft）。走査0件は正常終了である。

`evaluate`はrun_dateから5/20営業日先の**満期営業日**を求め、`満期日 <= as_of`の
ものだけを分類して`verdict_outcomes`へ`(run_id, horizon_days)`単位の完全置換で
保存する。`verdict_outcomes.as_of`には観測日ではなく満期日を記録するため、
いつ実行しても同じ行が得られる（`signal_outcomes.as_of`が観測日なのとは
意図的に異なる）。分類は非対称で、`proceed`は「重大な逆行がなかった」という
片側の主張のためNEUTRALを持たず、`skip`は下落を的中・上昇を機会損失として
扱う。閾値は`settings.postmortem`の既存値を流用し、新しい閾値体系を作らない。

`export`は満期日が`[as_of - lookback_window_days, as_of]`に入る当否行を集約し、
`reports/retro/<as_of>/retro_input.json`をstrictスキーマ`retro-input-v1`で
原子的に書き出す。含まれるのはseparation（proceed群−skip群の平均リターン）・
proceed重大外し率（候補全体ベースライン併記、ウォッチ水準0.15超または
ベースライン超でフラグ）・skip的中率（ベースライン比）・人間整合クロス集計
（`trades_journal`×verdict×当否）・ソース貢献表・既存`signal_outcomes`の
シグナル成績・サプライズ銘柄の証拠一式（当時のverdictとreasons、実現パス、
run以降の鮮度データ）・提案対象になりうる設定のスナップショットと
`config_hash`・提案台帳の参照・`input_digest`。サプライズは
`settings.retro.max_surprises`で打ち切り、切った件数を必ず出力に残す。
鮮度データは既存textアダプタ（timeout/retry/rate limitはそのまま）で取得し、
APIキー未設定や取得失敗は当該欄を空にしてnoteを残す（fail-soft）。

`ingest`は`retro_result.json`（strictスキーマ`retro-result-v1`）を検証し、
`retro_report.md`を同ディレクトリへ原子的に描画したうえで、通過した提案を
提案台帳（既定`docs/retro/proposals.md`、`--ledger`で変更可）へ
status=proposedで追記する。5つのサブコマンドで唯一DBに触れない。

検証は`analysis/`の境界と同型である。`as_of`と`input_digest`の不一致は
retro全体のhard fail（何も書かずに非0終了）。個別の提案・叙述については、
CON-03機械検査（`analysis/safety.py`の`check_display_texts`）→
`evidence_refs`がexportの供給したID空間（集約ID・サプライズID・source_id）の
部分集合であることの検証→再提案ガード、の順に適用し、いずれかに触れた項目
**だけ**をリトライなしでwithholdする。CON-03を先に検査するのは、後続の
withhold理由が識別子を安全に引用できるようにするためで、CON-03で落ちた項目は
識別子も出さない。

台帳操作は追記のみで、`proposed`以降の遷移（applied/rejected/deferred/
verification_failed）は適用段階のスキルと人間が記録する。RP-IDは台帳の既存
最大値と既存全文ファイルの双方から採番し、提案全文は
`docs/retro/proposals/RP-NNN-<slug>.md`に生成する。同一`proposal_key`の再
ingestは既存RP-IDを再利用して行を重複させない。台帳が`rejected`/
`verification_failed`として持つ`proposal_key`の再提案は、
`reopen_justification`が無ければ差し戻す。

`retro/`が`analysis/`と別パッケージなのは、`analysis/`が「ネットワークもDBも
触らない」憲章を持つのに対し、retroはDBを読み書きするためである。
`copilot-ingest-analysis`がDBに触れない不変条件はこれで維持される。

> **P7（スキル移行）で廃止**: Anthropic API直呼びのLLM統合（`llm/`パッケージ、
> `llm_calls`テーブル、月次予算ゲート・実行単位の呼び出し上限、応答キャッシュと
> near-stale警告）はすべて削除した。表示専用だった`catalyst_quality`と
> `guidance_direction`も新しい分析契約には含まれない。
