# API Reference

::: swing_copilot

## リサーチ読み取りAPI（`swing_copilot.research`）

蓄積された判断履歴（verdict・当否・スコア内訳・追跡台帳・レジーム・落選理由）を
pandas DataFrame として読み出す読み取り専用モジュール。各関数はクエリごとに
read-only の DuckDB 接続を開いて即閉じるため、運用データを書き換える経路を持たず、
接続保持による日次実行のロック奪取も起きない。使い方・データ辞書・安全上の規約は
[リサーチガイド](09_research_guide.md)を正とする。

```python
from swing_copilot import research

research.scorecard()            # verdict × 当否 × スコア内訳 × レジーム × 追跡 × セクター
research.candidates()           # 候補とスコア内訳（JSON展開済みの型付き列）
research.tracked_positions()    # 追跡台帳 + recommendation
research.screening_rejections() # 落選理由
research.bars(["AAPL"])        # Parquet 直読の日足（DBファイルに触れない）
research.query("SELECT ...")   # 任意の read-only SQL
research.ensure_views(path)     # ビュー未作成の古い DB を修復
```

結合済みビュー（`v_verdict_scorecard` / `v_candidates` / `v_tracked_positions` /
`v_symbol_sector_asof`）は `storage/schema.py` が定義し、`StateStore.init_schema()`
（毎日次実行）が `CREATE OR REPLACE` で自己移行する。セクターの as-of 解決
（`snapshot_date <= run_date` の inclusive 境界）は `v_symbol_sector_asof` に
一元化されており、分析側で universe_membership を自前 JOIN してはならない。

::: swing_copilot.research.frames

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

## 落選記録アーティファクト（`rejections.json`）

日次runは`reports/<run_date>/<run_id>/rejections.json`（schema `rejections-v1`、
`report/rejections.py`）を必ず書き出す。Markdownの「落選サマリ」が
`reason_code`別の件数しか出さないのに対し、こちらは銘柄単位の明細を残す。

| キー | 内容 |
|---|---|
| `rejections` | 落選銘柄の`symbol`・`stage`・`reason_code`・`detail`（`symbol`昇順） |
| `truncated_by_candidate_limit` | 全FilterとSignalを通過しながら`candidate_limit`で順位落ちした銘柄の`symbol`・切り捨て前の通し`rank`・`score`・スコア内訳・`execution_state`・`execution_distance`（`rank`昇順） |

`truncated_by_candidate_limit`はDuckDBの`screening_rejections`には入らない。
落選理由コードは閉じたenumで、順位落ちは「落選」ではなく設定上の上限だからで
ある。したがってこのファイルが、上限のすぐ外にいた銘柄を確認できる唯一の
run成果物になる。`candidate_limit`は`config/strategies.yaml`で戦略ごとに定める。

書き出しはステップ8がMarkdownアーカイブのあとに行い、失敗しても
`RunStatus.DEGRADED`（終了コード0）に留まる。定性分析の3ファイルとは異なり
digestで束縛せず、読み戻す経路も持たない診断用の成果物である。

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
先頭スライスへ縮退する。8-Kは主文書に加えてExhibit 99系（プレスリリース本文、
合計500,000字の安全弁まで）を同じ本文へ連結して取り込むため、`coverage`の`original_chars`は
主文書とExhibitを合わせた文字数を指す（Issue #128）。各開示の`coverage`には
元/出力文字数、選択方式、章の`full`/`partial`/`missing`が入り、
過去の`analysis-input-v2`はP8アーカイブ読み込みだけ
後方互換で受理する。
`original_chars`/`exported_chars`/`is_truncated`が語るのはexport段の欠落だけで、
Exhibitが取得段の上限（1開示500,000字の安全弁／最大3件）で失われた分は含まれない。これは
`coverage.exhibit_truncated`が本文中のマーカー
（文字数上限は`[... exhibit truncated ...]`、件数上限は
`[... exhibit omitted: per-filing exhibit count cap ...]`）の有無として
別に申告する（Issue #157、#163）。どちらの上限かは本文のマーカーで区別でき、
フラグ自体は両者を1つのbooleanでまとめる。`false`は「マーカーが無い」であって
「欠落が無い」ではない。
候補ごとの`news_supply`は、載せたニュースのうち`headline`／`summary`にティッカーが
現れる件数を数えて`sufficient`／`sparse`／`none`を申告する（Issue #130）。同業の決算記事や
セクター横断記事で枠が埋まった入力を、下流が「悪材料が無い」と読み違えないための
コード所有の観測値であり、記事の除外にも並び順にも使わない。
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

`evidence_quote`が本文に実在することと、それを言い換えた`facts[].text`の数値が
正しいことは別である（Issue #131）。`analysis/numeric_consistency.py`は、`text`と
`evidence_quote`の**両方に単位・通貨の付いた数値がある**factに限り、text側の数値が
quote側の数値から10のべき乗（千/百万/billion/million/億/万）で到達できるかを、
有効数字が粗い側の桁数で照合する。ただし**両側とも桁を明示している**場合
（quote側の`$119.8B`・`billion`とtext側の`億`）はべき乗も一致を要求する
（Issue #158。`$119.8 billion`は「1,198億ドル」と一致し、「119.8億ドル」とは
一致しない）。片側にしか桁の明示がないfactは従来どおり仮数だけを比較する。
到達できない数値はログへ**警告**として出るが、
当該銘柄は縮退しない——単位系は入力に明示されておらず、誤検知の代償を分析の消失に
できないためである。年号・四半期・比率のような単位の付かない数値は対象外で、
検算責任はスキル側の規約（AC16）にある。

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

## `copilot-verify-analysis`とスキルの事前検査

`analysis/verify_cli.py`（`copilot-verify-analysis`）は、スキルが書いた文書を
**レポートを書かずに**契約と突き合わせる読み取り専用のコマンドである。
`analysis_work/`の断片1件と、マージ後の`analysis_result.json`の両方を受け取る。

```bash
copilot-verify-analysis <WORKDIR>/analysis_work/news-AAPL.json  # 断片1件
copilot-verify-analysis <WORKDIR>/analysis_work                 # 全断片
copilot-verify-analysis <WORKDIR>/analysis_result.json          # ingestのdry-run
```

- 対象が断片か結果かは`schema_version`で判別する。結果スキーマはこの項を必須と
  し、断片スキーマは`extra="forbid"`で禁じるため、どちらか一方としてしか解釈
  されえない。判別後はそれぞれ自分のstrictスキーマで検証される
- ディレクトリを渡すと直下の`*.json`を検査し、コード所有の
  `analysis_input.json` / `report_context.json` / `rejections.json`は除外する。
  よって`<WORKDIR>/analysis_work`は全断片、`<WORKDIR>`はマージ後の結果を意味する
- `analysis_input.json`は対象の隣か1つ上の階層から解決する（`--input`で明示可）
- 終了コードは`0`（全件合格）/`1`（契約違反あり）/`2`（パスや入力の解決失敗）
- `copilot-ingest-analysis`と同じくネットワークにもDBにも触れず、
  スクリーニングを再実行せず、レポートも`latest.md`も書き換えない

**検査水準がingestと同一であること**が本コマンドの要件である（Issue #132）。
断片は`analysis/fragment.py`の`AnalysisFragment`（`run_id` / `as_of` /
`input_digest` / `ac_check` ＋ペイロードキーちょうど1つ）でparseし、
`SymbolAnalysis`へ持ち上げてから`analysis/validate.py`の
`verify_symbol_analysis()`——`copilot-ingest-analysis`が銘柄ごとに呼ぶのと
**同一の関数**——へ渡す。結果側は`load_analysis_result()`・
`validate_artifact_identity()`・`validate_analysis()`をそのまま呼ぶ。
自前のgrepで代用すると、`evidence.py`のNFKC正規化・記号統一・空白畳み込みと
`safety.py`の正規化を再現できず、ingestで落ちるものを合格と報告してしまう。

結果側のdry-runで唯一省くのは`report_context.json`との照合である。あれは同じrunの
`copilot-daily`がコード側で書くファイルで、スキルが取り違えうるのはresult側だから
であり、identityの照合自体は`validate_artifact_identity()`をそのまま通している。

## `copilot-retro`とverdictの当否評価

`retro/cli.py`（`copilot-retro`）は振り返り機構のCLIで、`collect`/`evaluate`/
`export`、その3つを順に走らせるumbrella `prepare`、そしてスキルの回答を検証する
`ingest`の5つを持つ。このうちオフラインで冪等な`collect`と`evaluate`は
`copilot-daily`のfail-softステップ（`retro_collect`／`retro_evaluate`）としても
毎日走るので、手動実行は取りこぼしの補完と即時反映のためのものになる。外部APIを
叩く`export`以降は従来どおり数日おきに手動で回す。

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
（`trades_journal`×verdict×当否）・ソース貢献表・news_supply水準×verdictの
クロス集計（自社材料の供給量しきい値を実績で検証するための観測）・既存`signal_outcomes`の
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

## `copilot-track`とverdict追跡台帳

`tracking/cli.py`（`copilot-track`）は、`proceed`と判定された銘柄を「そのrunの
終値で仮想的に買った」とみなして日次追跡する台帳のCLIである。手仕舞いルールは
`backtest/exits.py`の純関数（ATRトレーリングストップ + 最大保有日数）を
**バックテストと共有**しており、台帳が示す「いくらになったら手仕舞いか」は
シミュレータの挙動とずれない。ネットワークには接続せず、設定・コード・
決定論的なスクリーニング／サイジング値を書き換える経路も持たない。

```bash
copilot-track update --as-of 2027-03-21          # 建玉と日次前進
copilot-track list --status open                 # 含み損益・stop・残営業日
copilot-track show --symbol AAPL                 # verdict理由・日次マーク・ノート
copilot-track close --run-id <UUID> --symbol AAPL --note "決算をまたがない"
copilot-track note --run-id <UUID> --symbol AAPL --text "想定内の推移"
```

`update`は`verdicts`の`recommendation='proceed'`のうち未追跡のものを建玉し、
保有中を`--as-of`まで1取引日ずつ前進させる。`no_trade`（そのrun全体が当日
エントリー非推奨だった判断）は**除外しない**——実運用ではレジームが
`CASH_PRIORITY`のrunで全verdictが`no_trade=true`になることがあり、除外すると
台帳が空になって定性判断の質を測る材料が集まらないため、`verdicts.no_trade`を
そのまま`verdict_positions.no_trade`へ引き継いで建玉する。エントリー価格は
`risk_assessments.entry_price`（= run日終値）、初期stopは同`stop_price`で、いずれも
NULLなら保存済みバーの終値・`entry − exit_atr_multiple × ATR14`で代替する。
どちらも解決できない銘柄は建玉せず理由をnoteに出し、次回`update`で再試行する
（fail-soft）。保存済みバーが1本も無いポジション（上場廃止・ユニバース離脱など）は
前進も手仕舞い判定もできないため、毎回のupdateでその旨をnoteに出し続ける——
手動`close`以外に台帳から消える経路が無いことを利用者に知らせるためである。
日付引数（`update`/`close`の`--as-of`、`note`の`--date`）を
省略したときだけ、CLI境界で`SystemClock().today()`を使う。

`update`は建玉の前に、**verdictを失った建玉を削除する**。`copilot-ingest-analysis`の
再取り込みはrunのverdictを丸ごと置き換えるため（`replace_run_verdicts`）、
`proceed`から`skip`へ訂正された銘柄の仮想建玉が孤児として残り、取り消された判断の
損益を出し続けてしまう。台帳は`verdicts`の派生状態なので、対応する`proceed`が
消えたポジションはマーク・ノートごと1トランザクションで削除し、削除した銘柄を
noteに出す。

`list`は`⚠`列で`no_trade`を示し、フラグが立つ行は`no_trade`と表示する
（立たない行は空欄）。`show`はさらに一文で「銘柄単体は`proceed`だが、run全体は
当日エントリー非推奨だった（実際に提案された買いとは区別して読む）」と明示する。
いずれも判定の質を測る材料として台帳に残す一方、実際に提案された買いではない
ことを一目で区別できるようにするためである。

日次前進はバックテストと同じ順序を守る: その日の手仕舞い判定は**前日までの**stopで
行い、生き残った日の終値で初めてstopをラチェット更新する（翌日から有効）。
ギャップダウンは寄り付き約定、日中安値タッチはstop価格約定、同日にstopと
最大保有日数の両方が成立したときは常にstopが優先される。`last_marked_date`が
再開位置なので、同じ`--as-of`での再実行は何も変えない。

`update`は`copilot-daily`のfail-softステップ`track_update`としても
`retro_evaluate`の直後に毎日走る。したがって手動実行は取りこぼしの補完と
即時反映のためのものになる。

台帳のオープン建玉は、`copilot-daily`の「保有銘柄」のもう一方の供給源でもある。
日次runは実オープンポジション（`positions`）と台帳の`status='open'`の**和集合**を
保有銘柄として扱い、ニュース・開示の収集と分析の対象に優先的に含める
（`docs/04_detailed_design.md` 3.14）。実売買を始める前は`positions`が空なので、
台帳を読まなければ保有銘柄のニュース収集が一度も発火せず、遡及取得できない
`company-news`が恒久的に欠ける。ただしこれは収集・分析の対象集合にだけ効き、
リスク計算（サイジング・集中度・相関）へ渡すポートフォリオは実ポジションのみで、
仮想建玉は混ざらない。`--as-of`指定の再現runは台帳を読まない（現在状態であり
時点再現性が無いため）。台帳の読み取り失敗はfail-softで、警告を出して
仮想側を空としてrunを続行する。

スキルからの書き込みは`close`（`exit_reason='manual'`で確定）と`note`
（日付キーのcorrection upsert）の2つだけである。存在しない／既にクローズ済みの
ポジション、エントリー日より前のクローズ、**最終マーク日より前のクローズ**
（前進済みの日次マーク・`days_held`・再開位置と矛盾するため）、空メモは
いずれも非0終了で拒否する。

メモ本文（`note --text`と`close --note`）は`copilot-track show`がそのまま表示する
スキル生成テキストなので、他のスキル出力と同じく`analysis/safety.py`の中央
CON-03ガードを通す。売買を命じる表現を含むメモは保存されず非0終了になり、
`close --note`ではポジションを閉じる前に検査するため、拒否されたメモが
「理由の無いクローズ」を残すこともない。スキルへの指示だけでは不十分、という
本プロジェクトの原則をこの経路でも守るためである。

retroの`verdict_outcomes`（5/20営業日の2点分類）とは別レイヤであり、
paperの`positions`（人間が実際に持つと決めたFR-11の検証ゲート）とも混ぜない。
棲み分けの理由は`docs/04_detailed_design.md` 3.24.1にある。

> **P7（スキル移行）で廃止**: Anthropic API直呼びのLLM統合（`llm/`パッケージ、
> `llm_calls`テーブル、月次予算ゲート・実行単位の呼び出し上限、応答キャッシュと
> near-stale警告）はすべて削除した。表示専用だった`catalyst_quality`と
> `guidance_direction`も新しい分析契約には含まれない。

## `copilot-backfill`と履歴バックフィル

`pipeline/backfill.py`（`copilot-backfill`）は、バックテストに必要な過去データを
一度だけまとめて取り込むツールである。日次runの価格ステップは400暦日の
ローリング窓しか取らないため、これを走らせるまでローカルには複数レジームを
またぐ検証に足る履歴が存在しない。

```bash
copilot-backfill bars --start 2019-01-01                   # ユニバース全銘柄の日足
copilot-backfill bars --start 2019-01-01 --symbols SPY,QQQ # 個別指定
copilot-backfill fundamentals --start 2019-01-01           # 10-K/10-Qの過去分
```

`--end`を省略した場合だけCLI境界で`SystemClock().today()`を使う。ドメイン関数へは
常に明示的な日付を渡す。

`bars`は既存の`YFinanceProvider`を**50銘柄チャンク**で呼び、チャンク間に2秒の
スリープを挟む。yfinance側にレート制限の実装が無いための配慮である。取得した
バーはメモリに蓄積し、最後に`MarketStore.write_bars`を**1回だけ**呼ぶ——
`write_bars`は年パーティションを丸ごと書き直すので、チャンクごとに呼ぶと
書き直し回数が銘柄数に比例して増えるためである。

再実行は安全かつ安価で、既存バーが`--start`まで届いている銘柄は
ネットワークを叩かずにスキップする（`MarketStore.earliest_bar_dates`）。
「届いている」の判定には`COVERAGE_TOLERANCE_DAYS`（7暦日）の猶予がある——
`--start`は人間が選ぶ暦日で、上の例の`2019-01-01`は市場休日なので、
存在しうる最古のバーは`--start`以後の最初の**取引日**である。厳密に
`--start`以前を要求すると、この猶予なしではどの銘柄も条件を満たせず、
再実行のたびにユニバース全体を取り直すことになる。逆に、その時期にまだ
上場していなかった銘柄はこの条件を満たしようがないため毎回再取得される。
銘柄単位の「取得済みだが空」状態を持たないという設計上の割り切りで、
一回限りのツールにその台帳を持たせるほどの価値が無いと判断した。
銘柄単位の失敗はfail-softで、失敗した銘柄名を集約して最後に報告し、
他の銘柄の取得は続行する。ただし1銘柄も取得できず書き込みが0行だった場合は
終了コード1で落ちる——`copilot-backfill ... && copilot-backtest ...`が
空のDBに対して走るのを防ぐためである。

**ベンチマーク・レジーム系のシンボル（`SPY`・`QQQ`・`^VIX`・`^TNX`）は
S&P 500ユニバースに含まれないので、`--symbols`で別途バックフィルする必要が
ある。**特に`SPY`はバックテストの取引日カレンダーそのもの
（`backtest/runner.py::_trading_days`）なので、これを取り込まないと
バックテスト期間はSPYのバーがある範囲まで黙って縮む。

`fundamentals`は`EdgarClient.fetch_fundamentals`の`lookback_days`を`--start`まで
広げて呼ぶ。EDGARのbulk company-factsは常に全履歴を返すので、追加のアダプタ
改修なしに過去四半期を`filed_at`付きで取り込める。`EDGAR_IDENTITY`が未設定なら
何もせず非0終了する。

## `copilot-filter-matrix`とフィルタ独立通過率

落選台帳（`screening_rejections`、PK `(run_id, symbol)`）は**銘柄ごとに最初に
失敗した1条件だけ**を記録する。優先順位は`strategies.yaml`の
`filters_all` → `signals_all`の記載順なので、「各条件が単独でどれだけ落として
いるか」も「複数条件に同時に引っかかる銘柄がどれだけいるか」も台帳からは
分からない。パラメーターチューニングはまずそこを知りたい。

`copilot-filter-matrix`（`screening/filter_matrix_cli.py`）は、設定済みの
フィルタとシグナルを**1つずつ独立に全ユニバースへ適用**して、その3つを出す。

```bash
copilot-filter-matrix --as-of 2026-07-29                        # 既定戦略
copilot-filter-matrix --as-of 2026-07-29 --strategy vcp_breakout
copilot-filter-matrix --as-of 2026-07-29 --json reports/filter_matrix.json
```

| オプション | 既定 | 意味 |
| --- | --- | --- |
| `--as-of` | 必須 | 可視性の基準日。バー・ファンダ・スナップショットの全てに効く |
| `--strategy` | `default` | `strategies.yaml`のキー |
| `--db` | `data/copilot.duckdb` | Parquetバーの根（`<db>/../bars`）も一緒に決まる |
| `--settings` / `--strategies` | `config/*.yaml` | 設定を書き換えずに閾値バリアントを試すため |
| `--json` | なし | 機械可読な集計の書き出し先（同ディレクトリの一時ファイル＋`os.replace`） |

出力は3つの表と、その下の2行のサマリである。

1. **チェック別 独立通過率**——各チェック単独の通過／落選／データ不足と、
   「そのチェックだけで落ちている」銘柄数（単独ボトルネック）。
   単独ボトルネックは「その条件を外せば他の条件では落ちなくなる銘柄数」で
   あって、そのまま候補が増える数ではない（後述のランキング指標の壁がある）
2. **落選チェック数の分布**——0個（＝全チェック通過）・1個・2個以上。
   1個に寄っていれば条件は互いに独立に効いており、2個以上に寄っていれば
   緩和は1条件では効かない
3. **同時落選マトリクス**——チェック対ごとの同時落選数。対角は各チェックの
   落選合計
4. **全チェック通過**——0個バケツの銘柄そのもの
5. **候補相当（`candidate_limit`適用前）**——そのうち`ScreeningPipeline`が
   実際に候補として出すもの

**0個バケツ＝候補ではない。** `ScreeningPipeline`はチェックの後にもう2つの
ゲートを持つ。ランキング指標（`screening.pipeline.ranking_metrics`：SMA200を
含むので約200本の履歴が要る）が取れない銘柄は落とされ、`signals_all`が空の
戦略は候補を1つも出さない。診断はこの2つを`candidate_equivalent_symbols`に
反映するので、**同じ銘柄が0個バケツと落選台帳の両方に現れることはない**。

**データ不足は落選と別カテゴリで数える。** 履歴不足やファンダ欠損は「閾値が
厳しすぎるか」を何も語らないためで、`FAILED`／`NO_DATA`の判定には
`rejection_classifier`の`data_quality`ステージをそのまま使う。ミラーである
`rejection_classifier`がFilter本体と食い違う場合（例：出来高が全欠損で平均が
NaNになり、Filterは落とすがミラーは通す）も、閾値については何も言えないので
データ不足に数える。

シグナルは、パイプラインが渡すフィルタ通過後の部分集合ではなく**全ユニバース**
に対して評価する。独立通過率は全チェックが同じ母集団で測られて初めて比較
できるためである。したがってここでのシグナル通過数は、日次runの候補数とは
一致しない。とくに`minervini_stage2`は**母集団依存**で、条件7の相対強度
パーセンタイルを「渡された銘柄集合の中での順位」として計算する。全ユニバース
で測ったここでの順位はフィルタ通過後の順位より緩くも厳しくもなりうるので、
この診断はそうしたチェックを検出して表の下に注意行を出す
（JSONでは`population_dependent_checks`）。

チェックの実体は`ScreeningPipeline`と同じ`build_strategy_components`で
組み立てるので、この診断は日次runが実際に使うのと同一のFilter/Signal
インスタンスを測る（ロジックのミラーは増やしていない）。同じキーが
`filters_all`／`signals_all`に重複して書かれていても、各チェックは1回だけ
測る（2回測ると分布・同時落選・単独ボトルネックが壊れる）。

**測る母集団はスナップショットそのものではない。** ローカルにバーもファンダも
1行も無い銘柄は除外し、除外件数を出力する。`copilot-daily --limit N`や途中で
失敗した取得の後には未取得銘柄がスナップショットに大量に残り、それらを数える
と「バー系チェックが9割落としている」ように見えてしまうためである
（`pipeline/daily.py`が`ScreeningInput.universe`をそのrunの取得銘柄に絞って
いるのと同じ理由）。

完全にオフラインである。ユニバースは`--as-of`時点で可視な永続スナップショット
（`snapshot_date <= as_of`）だけを使い、無ければWikipediaを取りに行かずに
エラーで落ちる。`--as-of`当時のmembershipでないものを測っても意味がない
からである。スクリーニング結果の行は1行も書かず、スキーママイグレーションも
実行せず、`--db`が存在しなければ作らずにエラーにする。ただし`MarketStore`は
共有DuckDBファイルを読み書きモードで開いて自分の`fundamentals`テーブルと
`bars`ビューを用意するため、DuckDBの単一ライターロックはかかる。
`copilot-daily`の実行中には走らせないこと。

## `copilot-dd-forward`とDistribution Day水準の予測力

`regime.dd_*`は roadmap §5 P3-13 で**要検証**のまま本番に入っていた。しかも
検証する手段が無かった。`backtest/`は`regime.exposure`も`regime.distribution`も
importしておらず（`backtest/engine.py`のサイジングは`risk/position_sizing.py`を
直接呼ぶ）、`dd_*`をどう動かしても`copilot-backtest`の数字は1つも動かない。
一方で`SEVERE`は`_base_exposure`で単独に`CASH_PRIORITY`まで落とし、その日の
候補は全銘柄`shares=0`になる。**効果を測れないまま、最も強い制約を課している
パラメーターだった。**

`copilot-dd-forward`（`regime/dd_forward_cli.py`）は保存済み履歴を1日ずつ
`as_of`として再生し、`pipeline/daily.py::_calculate_regime_snapshot`と同一の
分類（SPYとQQQそれぞれの水準の`max`）を出したうえで、その後に実際に起きた
リターンとドローダウンを水準別に集計する。

この検証の結果、`dd_severe_d25`/`dd_severe_d15`は2026-08-07にIssue #111で
`7`/`6`（従来`6`/`4`）で**採用済み**（根拠:
`reports/regime/2026-08-06-dd-threshold-review.md` §10）。`dd_high_*`（5/3/2）と
`dd_caution_d25`は据え置きで、引き続き**要検証**。

```bash
copilot-dd-forward --as-of 2026-08-06
copilot-dd-forward --as-of 2026-08-06 --sweep --score-target UNIVERSE_EW
copilot-dd-forward --as-of 2026-08-06 --grid --json reports/dd_forward.json
# settings.yaml を書き換えずに閾値バリアントを測る
copilot-dd-forward --as-of 2026-08-06 --settings /tmp/variant/settings.yaml
```

| オプション | 既定 | 意味 |
| --- | --- | --- |
| `--as-of` | 必須 | 可視性の基準日。これ以降のバーはどの用途でも読まない |
| `--start` | 履歴の先頭 | 最初の観測日。手前は助走（窓とEMAシード）として読む |
| `--horizons` | `5,10,25` | 先行きリターンの保有営業日数。25は`backtest.max_hold_days` |
| `--sweep` | off | 閾値を1つずつ動かした感度表 |
| `--grid` | off | 順序制約を満たすグリッドの全走査（既定レンジで約1分） |
| `--score-target` / `--score-horizon` | `SPY` / `10` | `--sweep`・`--grid`・ゲート表の採点軸。測定していない対象・保有日数を指定するとエラーで落ちる（空欄の表を出さないため）。`--horizons`を絞ったら合わせて指定する |
| `--json` | なし | 日次の観測列を含む機械可読な書き出し（一時ファイル＋`os.replace`） |

**先行きの窓は評価専用の意図的な先読みである。** 各日の*分類*は`date <= as_of`の
包含境界を厳密に守り、先読みするのは「その日に付ける成績」だけで、全体は
外側の`--as-of`で閉じている。先行きの値が水準に戻ることは無い。

測る対象は SPY・QQQ と、`--as-of`時点の永続スナップショットの銘柄による
**等加重バスケット**（`UNIVERSE_EW`）の3つ。露出上限がゲートしているのは
指数ではなく個別株なので、バスケットの方が「その日を`CASH_PRIORITY`にした
コスト」に近い代理になる。ただしメンバーは現在のスナップショットなので
**生存バイアスがある**。水準間の比較にだけ使い、水準の絶対値の根拠にはしない。

**N(日)ではなくN(エピソード)を見ること。** 日次観測の先行き窓は重なっており、
日数はサンプルサイズを大きく見せる。表は連続した同一水準のランを
エピソードとして併記する。

掃引するのは6つのうち5つで、`dd_caution_d25`は含めない。`_base_exposure`は
`CAUTION`と`NORMAL`を同じ分岐に落とし、`DistributionLevel.CAUTION`は
パッケージ内に他の消費者を持たない——つまり**`dd_caution_d25`は露出上限を
1日も動かせない表示専用のラベルである**。グリッドはさらに
`CASH_PRIORITY`軸（`severe_*`だけが決める）と`REDUCE_ONLY`軸（`high_*`だけが
決める）に分けて出す。2つは独立なので、片方の差で5次元を並べると
もう片方の同じ挙動の変種で埋まるためである。候補は
`config.RegimeConfig._validate_dd_level_order`と同じ順序制約を通したものだけで、
そのまま`settings.yaml`に書けば読める。

順位は同じ履歴の in-sample スコアである。候補の絞り込みには使えるが、それ
自体は out-of-sample の検証ではない。

`copilot-filter-matrix`と同じくオフラインかつ読み取り専用で、スキーマ
マイグレーションを実行せず、`--db`が無ければ作らずにエラーにする。
`MarketStore`が共有DuckDBを読み書きで開く点も同じなので、`copilot-daily`の
実行中には走らせないこと。

## バックテストの設定バリアント比較

`copilot-backtest`は`--settings`と`--strategies`でそれぞれ`config/settings.yaml`と
`config/strategies.yaml`を差し替えられる。リポジトリの設定を書き換えずに
A/B比較を回すための入り口である。ランキング重み（`score_weights`）は
`strategies.yaml`側にあるため、重みバリアントの比較には`--strategies`が要る。

```bash
copilot-backtest --strategy default --start 2020-01-02 --end 2026-07-30 \
  --settings /tmp/variant/settings.yaml --strategies /tmp/variant/strategies.yaml
```

`run`のレポート（`--pessimistic`の通常vs悲観比較を含む）には`Exit breakdown`
セクションが出る。決済理由の内訳（`stop` /
`max_hold` / `end_of_backtest`、発火0件の理由も0として必ず表示）、
`max_hold binding rate`（全決済に占める`max_hold`の割合）、実保有日数の
中央値と四分位である。感応度グリッドのMaxHold列が全て同値だったとき、
「そのパラメータが効かない」のか「一度も発火していない」のかを区別するために
ある。binding rateが0%に近ければ、`max_hold_days`をどう振っても結果は動かない。

## 候補ストリームキャッシュ（`--candidate-cache`）

`copilot-backtest`の実行時間の大半はスクリーニングであり、エンジン走行では
ない。そこで候補生成をエンジン走行から切り離し、`--candidate-cache PATH`で
Parquetへ永続化できるようにした。

```bash
copilot-backtest grid --strategy default --start 2020-01-02 --end 2026-07-30 \
  --candidate-cache /tmp/candidates-default-2026-07-30.parquet
```

キャッシュキーは**スクリーニングが読む入力だけ**で構成する: 戦略キーと
そのspec（`candidate_limit`・`score_weights`等）、`technical_signals`、
`fundamental_filters`、ユニバース、対象銘柄、`--start`/`--end`、ベンチマーク
（取引日カレンダーの源泉）、そして価格・ファンダの内容ダイジェストである。

`settings.backtest`（`exit_atr_multiple`・`max_hold_days`・`commission_pct`・
`slippage_pct`・`slippage_multiplier`）と`settings.risk`、初期資金は
**キーに含めない**。これらはエンジンの入力であってFilter/Signalは一切読まない
ため、手仕舞いパラメータやコストを振ってもキャッシュは無効化されない——
感応度グリッドやコスト比較を同じキャッシュで回せることが、この設計の目的で
ある。逆に、銘柄・期間・戦略・スクリーニング設定・価格データのいずれかが
動けばキーは変わり、キャッシュは自動で再生成・上書きされる。読めない
キャッシュファイルもエラーではなくミス扱いで再生成する。

`--candidate-cache`を付けなくても、**1回のコマンド実行の中では候補生成は
1回だけ**である。`grid`の25セルも`--pessimistic`の通常/悲観2シナリオも、
同一の候補ストリームを共有する（`--candidate-cache`が足すのは、CLI実行を
またいだ再利用だけである）。

## 低ボラバイアス是正の2つのスイッチ

スクリーニング候補が構造的に低ボラ銘柄へ偏る原因は2つあり、それぞれに
スイッチを用意した。採用は比較レポート
（`reports/backtests/2026-07-30-strategy-comparison.md`）を見た人間が判断する。
`band_atr_multiple`は2026-08-04にR2の結果（期待値・PF・Sharpe改善、DD同等）を
根拠に`2.0`で**採用済み**。`atr_pct`はR3でR2比の上積みが観測されなかったため
**見送り**（`0.0`のまま）。

| 設定 | 場所 | 現在値 | 効果 |
| --- | --- | --- | --- |
| `technical_signals.pullback.band_atr_multiple` | `settings.yaml` | `2.0`（キー削除で旧モードに戻る） | `\|close − SMA50\| / ATR14 ≤ 倍率`で帯を判定し、`sma_band_pct`を無視する |
| `ranking.score_weights.atr_pct` | `strategies.yaml` | `0.0`（無効） | ATR%が高いほど高得点の成分を合成スコアへ加える |

`band_atr_multiple`が無ければ帯は`|close − SMA50| / SMA50 ≤ 0.03`という
絶対3%で、これは低ボラ銘柄を高ボラ銘柄の約4.5倍通過させる事実上の
ローボラフィルタとして働く。ATR単位で測れば、パイプラインが既に執行距離に
使っている`execution.fair_max_d`（SMA50からATR 2.0個分）と同じ尺度になる。
ATRがNaNまたは0のときは距離が定義できないため帯を閉じる（安全側）。

`atr_pct`成分は候補集合内のパーセンタイルではなく、ATR% 6%を満点とする
**絶対正規化**である。候補が5件程度しかない集合でパーセンタイルを取ると、
`liquidity`成分が既に抱えている小標本ノイズを再生産するためである。
`score_weights`の合計1.0検証にも加算されるので、`atr_pct`を入れるときは
他の重みを必ず下げることになる。

## 決算日エントリー回避（実装済み）

決算をまたぐエントリーの回避は**すでにrisk層に実装されている**。
`RiskChecker._apply_earnings_guard`が`risk.earnings_block_business_days`（既定2）
以内の決算予定を`binding_constraint="earnings"`でrejectedにし、
`earnings_warn_business_days`（既定5）以内なら警告を出す。予定日は日次runの
riskステップがFinnhubから取得する。

ただし**バックテスト経路にこのガードは無い**。`backtest/engine.py`は
`RiskChecker`を通らず`position_sizing`だけを使い、加えて`earnings_calendar`は
symbol主キーの上書き保存で履歴を持たないため、過去時点で「当時知られていた
予定日」を復元できない。したがって決算ルールの効果はバックテストでは
測れない、というのが現状の制約である。
