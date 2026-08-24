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
research.truncated_candidates()   # candidate_limit で順位落ちした near-miss
research.universe_forward_returns()  # 候補 ∪ 順位落ち ∪ 落選の forward return
research.signal_hits()          # run_id キーのシグナル発火
research.verdict_reasons()      # verdict の理由 1 件 1 行（basis / source_id_count）
research.bars(["AAPL"])        # Parquet 直読の日足（DBファイルに触れない）
research.query("SELECT ...")   # 任意の read-only SQL
research.ensure_views(path)     # ビュー未作成の古い DB を修復
```

結合済みビュー（`v_verdict_scorecard` / `v_candidates` / `v_truncated_candidates` /
`v_universe_forward_returns` / `v_tracked_positions` / `v_symbol_sector_asof` /
`v_retro_narrations` / `v_run_configs`）は `storage/schema.py` が定義し、`StateStore.init_schema()`
（毎日次実行）が `CREATE OR REPLACE` で自己移行する。セクターの as-of 解決
（`snapshot_date <= run_date` の inclusive 境界）は `v_symbol_sector_asof` に
一元化されており、分析側で universe_membership を自前 JOIN してはならない。

`v_tracked_positions`は台帳行に加えて、各`(run_id, symbol)`の最新マークから
`last_mark_date`・`last_close`・`unrealized_return_pct`を返し、建玉時に保存した
`max_hold_days`も公開表示へ渡す。最新値が無い行は未記録のまま残るため、読み手はゼロと解釈しない。

::: swing_copilot.research.frames

## `copilot-dashboard` と閲覧用ダッシュボード

`dashboard/`（`copilot-dashboard`）は、同じ蓄積データをブラウザで俯瞰する
読み取り専用ビューアである。4画面（run概観・銘柄詳細・推移・公開追跡）とrun切替だけを持ち、
書き込みルートを一切持たない。画面構成・欠損値の表示規約・起動方法は
[05. CLI・Markdown・ダッシュボード出力設計](05_ui_design.md)の10節を正とする。

```bash
uv run copilot-dashboard                                   # 127.0.0.1:8787
uv run copilot-dashboard --db data/copilot.duckdb --port 9000
uv run copilot-dashboard --tracking-retention-days 5
```

```python
from pathlib import Path

from swing_copilot.dashboard import create_app

app = create_app(db_path=Path("data/copilot.duckdb"), reports_root=Path("reports"))
```

`create_app()` はDBとreportsディレクトリを注入するアプリケーションファクトリで、
テストは実データに触れずに全ルートを検証できる。DuckDBへは `swing_copilot.research`
経由でのみアクセスし（クエリごとに開いて閉じる）、`ensure_views()` はこのプロセスから
呼ばない——読み書き接続を開くため、`just data-pull` / `data-push` のファイルロックを
奪いうる。
ビュー不在は `ResearchError` としてエラーページに変換し、別シェルで一度
`ensure_views()` を実行するよう案内する。

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

## 銘柄レベルの売買計画とリスク

`risk/checks.py::RiskChecker`は読者の口座や保有を受け取らない。候補ごとにrun日終値、
計画指値、終値アンカーの逆指値、ATR14、1R
（`(limit_price - stop_price) / limit_price`）、status、blocking reasons、warningsを返す。
価格/ATR欠損や無効なストップ幅は`not_calculable`、広いストップは`WIDE_STOP`警告になる。

決算近接ガードは`EarningsCalendarClient` Protocolから取得した次回予定日を使い、
2営業日以内を`EARNINGS_PROXIMITY_BLOCK`、5営業日以内を
`EARNINGS_PROXIMITY_WARN`とする。予定不明は`EARNINGS_DATE_UNKNOWN`を明示し、
Finnhubキー未設定時はガード全体を`NO_EARNINGS_DATA`として無効化する。

バックテストの名目資金・サイジングは`settings.backtest.sim_trade_risk_pct`・
`sim_position_cap_pct`・`max_concurrent_positions`で決まるシミュレーション専用値であり、
本番の助言値ではない。市場状態、決算、`not_calculable`だけがバックテストの
エントリー境界に残り、ポートフォリオ熱量・セクター・相関・サーキットブレーカーは
その境界では判定しない。

## 定性分析の境界（`analysis/`、FR-08・CON-03）

定性分析はこのプロセスの中では行わない。`swing_copilot.analysis`は、日次バッチと
GitHub Actions からのみ起動する Claude Code スキル（`.claude/skills/swing-daily`系）
の間の**ファイルを介した境界**であり、モデルAPIを一切呼ばない。

`analysis/export.py`は`copilot-daily`のステップ6で、候補ごとの決定論的文脈
（`analysis/context.py`が整形したP1-01スコア内訳・P1-03リスク制約・市場レジーム・
過去verdict）と、ステップ5で収集済みの未信頼テキストを
`reports/<run_date>/<run_id>/analysis_input.json`（schema `analysis-input-v3`）へまとめ、宛先と同じディレクトリの
一時ファイル＋`os.replace()`で原子的に書き出す。ニュースは
`settings.analysis.max_news_items_per_symbol`件・各`max_news_chars_per_item`文字、
開示は1件`max_filing_chars`文字、1銘柄合計`max_filing_chars_per_symbol`文字までとする（後者は`max_filing_chars`以上、かつ`max_filings_per_symbol × min(max_filing_chars, MIN_FILING_CHARS)`以上でなければ`Settings`の読み込み時に拒否される。Issue #268）。
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
コード所有の観測値であり、記事の除外にも並び順にも使わない。`sufficient`の下限は
`settings.analysis.sufficient_news_mention_items`（既定5、要検証）であり、
初出の較正値がそのまま定数として固まらないようconfig化してある（Issue #191）。
`analysis/validate.py`は`SymbolOutcome.news_supply`として`candidate.news_supply`を
検証結果に関わらずそのまま運び、`report/daily_brief.py`の
`BriefAnalysis.news_supply`（`BriefNewsSupply`）を経て`report/markdown_report.py`が
候補セクションに`- News supply: ...`として描画する（Issue #281）。AC14と
`analyze-news/SKILL.md`は`news`が空なら`news_summary: null`のままなので、
「`level`がnone/sparseで`collected_items`が非0（本文には言及が薄いニュースが
存在した＝抑制）」と「`collected_items`が0（そもそも収集していない）」の違いは
`news_summary`側には現れない。この行はその2つを`collected_items`の値だけで
文言レベルから分けて示す、決定論コード側の記述である。
候補ごとの`prior_verdicts`は、同一銘柄・戦略に対する過去のverdictとその後の当否
（`HIT`／`MISS_*`と`forward_return_pct`）を対にした不活性ブロックで、
「同じ種類の根拠で繰り返し外していないか」をスキル自身が見られるようにする
（Issue #191）。過去runの`source_id`は持ち帰らない。`score_breakdown`の末尾には加重前の生値
（`close`／`rsi14`／`sma50`／`sma200`／`avg_volume`と導出値`atr14_pct`）が
「参考情報」として付き、正規化で潰れた大きさを分析側が読めるようにする。
これらはいずれもコードの計算結果であり、分析側が再計算・上書きできない。
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
`analysis/snapshot.py`で保存した`report_context.json`（schema `report-context-v4`、
表示非依存の`DailyBrief`のスナップショット）を読み直し、候補ごとの定性欄と
run単位の`no_trade`/`no_trade_reason`だけを差し替えて同じMarkdownを再生成する。
スコア・売買計画・実行状態・落選・レジームは無変更で持ち越す。

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
`input_digest` / `ac_check` ＋ペイロードキーちょうど1つ。開示断片のみ
`filing_body_digests`も必須）でparseし、
`SymbolAnalysis`へ持ち上げてから`analysis/validate.py`の
`verify_symbol_analysis()`——`copilot-ingest-analysis`が銘柄ごとに呼ぶのと
**同一の関数**——へ渡す。結果側は`load_analysis_result()`・
`validate_artifact_identity()`・`validate_analysis()`をそのまま呼ぶ。
自前のgrepで代用すると、`evidence.py`のNFKC正規化・記号統一・空白畳み込みと
`safety.py`の正規化を再現できず、ingestで落ちるものを合格と報告してしまう。

結果側のdry-runで唯一省くのは`report_context.json`との照合である。あれは同じrunの
`copilot-daily`がコード側で書くファイルで、スキルが取り違えうるのはresult側だから
であり、identityの照合自体は`validate_artifact_identity()`をそのまま通している。

**断片を流用してよいかの判定も、このコマンドが答える**（Issue #261）。鍵は断片の
種類で違う。`news-<SYMBOL>.json`と`screening-<SYMBOL>.json`は従来どおり
`run_id` / `as_of` / `input_digest`の3値一致——当日のニュースと当日の決定論的スコアを
読むので真に`as_of`依存であり、日跨ぎでは流用できない。`filings-<SYMBOL>.json`だけは
`filing_body_digests`（`source_id` → 開示本文のSHA-256。`schemas.filing_body_digest()`
が算出し、`copilot-export-slices`がスライスへ載せ、開示担当が断片へ逐語コピーする）が
その日の入力がexportする開示本文と**完全一致**することが鍵で、`run_id`が変わっても
流用できる。開示の読みは開示本文の関数であり、連続2営業日で共通5銘柄のaccessionが
14/14一致した実測がこの緩和の動機である。

流用してもprovenanceは緩まない。流用した断片もその日の`analysis_input.json`に対して
provenance・`evidence_quote`の逐語一致・CON-03を改めて通り、本文が変わった開示の
古い読みはdigest判定をすり抜けても引用が現在の本文に無いためFAILする（fail-closed）。

## `copilot-export-slices`と入力スライスの決定論生成

`analysis/slices.py`と`analysis/slice_cli.py`（`copilot-export-slices`）は、
`analysis_input.json`から**専門家×銘柄**の入力スライスを切り出す。統括スキルが
1.4MBの入力を読みながら21件を手で切っていた工程（2026-08-13の実走で5.2分）を
決定論的なコードへ置き換えたものである（Issue #260）。

```bash
copilot-export-slices <WORKDIR>/analysis_input.json \
  --out-dir <REPO_ROOT>/.swing-daily-scratch/slices
copilot-export-slices <WORKDIR> \
  --out-dir <REPO_ROOT>/.swing-daily-scratch/slices  # ディレクトリでもよい
```

- 出力は`slice-<kind>-<SYMBOL>.json`（`<kind>`は`news` / `filings` / `screening`）。
  `analysis_work/<kind>-<SYMBOL>.json`の断片と名前の形が似るため`slice-`を付ける——
  両者はスキーマが違い、マージされるのは断片だけである
- グルーピングは`swing-daily`スキル Step 2 の担当割り当てをそのまま写す:
  `news`は`news`が非空の銘柄、`filings`は`filings`が非空の銘柄、`screening`は
  全銘柄。run単位のcontextブロックはscreeningスライスにだけ入る（それを読むと
  規約に書かれているのは`interpret-screening`だけである）
- `news`が空でも`news_supply`を持つ銘柄にnewsスライスを出さないのは、
  `analyze-news`とAC14が「`news`が空なら`news_summary: null`を書く」ことを
  求めているためである。エージェントを立てても null が返るだけなので、この判定
  （`_has_work()`、Issue #260の状態のまま）は変えていない。供給量の申告
  （Issue #130）をレポートへ届かせるのは決定論コード側の役目で、
  `analysis/validate.py`・`report/daily_brief.py`・`report/markdown_report.py`が
  担う（前段の`news_supply`の項を参照、Issue #281）
- `--out-dir`は**必須**である。CI 専用スキルは checkout 直下の
  `.swing-daily-scratch/`配下へ書く。既定値を入力の隣に置くと、スライスが
  runディレクトリに書かれうるため、
  呼び出し側に必ず宣言させる。さらに値そのものを検査し、**runディレクトリと同一・
  その配下・その上位**、および`analysis_input.json`を既に持つディレクトリを拒否する。
  必須にするだけでは「入力として渡したのと同じパスを`--out-dir`にも渡す」誤りを
  防げず、`reports/<date>/<run-id>/`へ落ちた`slice-*.json`はrunごとに溜まるためである。
  `.swing-daily-scratch/`は`.gitignore`対象で、job終了時にGitHub-hosted runnerが
  checkout全体を破棄する。スキルは途中で`rm`を実行しない
- 標準出力は「絶対パス / kind / 銘柄 / `source_chars`」のタブ区切り＋総数行。
  `source_chars`はそのスライスが載せている本文の文字数で、統括はこれを
  1エージェントあたりの文字数上限（開示は240,000文字）に突き合わせる
- 終了コードは`0`（生成成功）/`1`（入力が読めない・スキーマ違反・`--out-dir`が
  拒否形・書き込み失敗）
- スライス群は**1つの論理的な書き込み**として書く（`io_atomic.write_json_batch_atomically()`）。
  全件をまず宛先ディレクトリ内の一時ファイルへ書き、そのあとで`os.replace`する。
  容量不足や書き込み不可で途中失敗した場合、宛先は1つも変更されず一時ファイルも
  残らない——「コマンドは失敗したのに7件だけ残っている」状態を作らないためである。
  統括は非ゼロ終了を「何も生成されなかった」と読み、CI の `<REPO_ROOT>/.swing-daily-scratch/`
  は job 終了時に runner が破棄するため、ワークフローは途中で掃除しない

**決定論性が本コマンドの要件**である（同一入力→バイト同一出力。Issue #261 が
本文ハッシュでの流用判定の前提にする。この性質はプロセスを跨いで成り立つ必要が
あるため、回帰テストは同一プロセス内の2回ではなく、`PYTHONHASHSEED`を変えた
2つの別インタプリタでエントリポイントを実行して出力バイトを比較する）。そのために、値は`analysis_input.json`の
**JSONそのもの**から逐語コピーする——parse済みモデルを再シリアライズすると日時表記や
キー順が書き換わり、provenance検査が突き合わせる文字列と一致しなくなる。
トップレベルのキー順は`run_id` / `as_of` / `input_digest` / `kind` / `context` /
`candidate`に固定し、入れ子は元文書の順序をそのまま保つ。filingsスライスだけは
最後に`filing_body_digests`が続く——スライス内で唯一の計算値で、そのスライスが
載せている`filings[].text`のSHA-256である（Issue #261。開示担当はこれを断片へ
逐語コピーし、翌営業日の流用可否がこの値で決まる）。書き出しは上記の
`io_atomic.write_json_batch_atomically()`で、直列化の形は単発の
`write_json_atomically()`と同一（UTF-8・`indent=2`・`sort_keys=False`・
末尾改行1個・LF・同一ディレクトリの一時ファイル＋`os.replace`）であり、生成時刻・
パス・ホストなど実行環境依存の値をペイロードへ入れない。

スライスは書かれる前に`InputSlice`（`extra="forbid"`）で検証される。`kind`ごとに
`candidate`が持てるキーの集合をスキーマ側で固定してあるので、担当外のフィールド
——他の専門家の長文テキストやrun単位のcontext——が紛れ込んだスライスは、
サブエージェントへ渡る前に落ちる。

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
copilot-retro ingest reports/retro/2027-03-11        # 回答を検証し記録＋narrationを蓄積
```

`collect`は`reports/<date>/<run_id>/analysis_result.json`を走査し、run単位の
完全置換で`verdicts`/`verdict_sources`へ取り込む。`strategy_key`と
`source_type`はコードが所有する`analysis_input.json`から解決し、スキルの
申告値を採用しない。文書欠損・解析不能のrunと入力側に存在しない`source_id`は
noteを残してスキップする（fail-soft）。走査0件は正常終了である。ディレクトリの
列挙は毎回全件だが、前回の取り込み時と2文書のハッシュが一致するrunは再パース
も再書き込みもしない（Issue #209）。出力は「走査 / 解析 / 無変更 / 取り込み」の
run数を並べて表示する。どちらかの文書を書き換えれば——サイズと更新時刻が同じ
でも——ハッシュが変わるので、訂正は必ず取り込み直される。

`evaluate`はrun_dateから5/20営業日先の**満期営業日**を求め、`満期日 <= as_of`の
ものだけを分類して`verdict_outcomes`へ`(run_id, horizon_days)`単位の完全置換で
保存する。`verdict_outcomes.as_of`には観測日ではなく満期日を記録するため、
いつ実行しても同じ行が得られる（`signal_outcomes.as_of`が観測日なのとは
意図的に異なる）。分類は非対称で、`proceed`は「重大な逆行がなかった」という
片側の主張のためNEUTRALを持たず、`skip`は下落を的中・上昇を機会損失として
扱う。閾値は`settings.postmortem`の既存値を流用し、新しい閾値体系を作らない。
このコマンドは窓内の満期スライスを**全件**再分類するので、株価が訂正された
場合の分類の更新もここで起きる（日次ステップ側は未記録のスライスだけを評価
する。Issue #209）。`--db`の兄弟`bars/`がディレクトリごと無い場合、`evaluate`
と`export`は評価を始める前にエラーで落ちる（Issue #221）。バー0件から
forward returnを計算しても1件も満期にならず、「評価0 slice」が正常終了として
返ってしまうためである。

`export`は満期日が`[as_of - lookback_window_days, as_of]`に入る当否行を集約し、
`reports/retro/<as_of>/retro_input.json`をstrictスキーマ`retro-input-v1`で
原子的に書き出す。含まれるのはseparation（proceed群−skip群の平均リターン）・
proceed重大外し率（候補全体ベースライン併記、ウォッチ水準0.15超または
ベースライン超でフラグ）・skip的中率（ベースライン比）・ソース貢献表・根拠タイプ貢献表
（`verdict.reasons[].basis`の閉集合別のverdict件数とHIT比率。タグの無い理由は`untagged`として計上し、タグ付与率そのものを可視化する。Issue #191）・news_supply水準×verdictの
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
status=proposedで追記する。さらに検証を通った narration を`--db`
（既定`data/copilot.duckdb`）の`retro_sessions` / `retro_narrations`へ、
当該`retro_as_of`ごと1トランザクションで置換書き込みする（Issue #189）。
それまで`failure_class`はgitignore対象の`reports/retro/`にしか残らず、
設計§8.1のL2定性ゲート（同一分類が直近3回で累計5件）を数える材料が
どこにも無かった。`run_id`/`symbol`はスキルの回答ではなくエクスポート済み
dossierから解決する。次回以降の`export`はここから`failure_class_history`
（直近3回のクロス集計と、決定論コードが判定した`meets_l2_gate`）を作るので、
ingestを飛ばすとその材料が失われる。

`export`にはあわせて`aggregates_by_config`（`runs.config_hash`別のseparation
内訳）が加わる。設定値そのものは`copilot-daily`が`config_versions`台帳へ
upsertしており（`config_hash`が指す8セクションと、その`snapshot_hash`）、
`first_seen_run_date`は`least()`で前方向にしか動かない。台帳導入前のrunは
`snapshot_hash`/`sections_json`がNULL＝未記録である。

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

`tracking/cli.py`（`copilot-track`）は、verdictの出た銘柄を「そのrunの
終値で**無条件に**仮想的に買った」とみなして日次追跡する台帳のCLIである。この台帳が
測るのは**判断の当否**であり、実際に買えたか・約定したか・いくら儲かったかという
執行実績ではない（決定 #327）。手仕舞いルールは`backtest/exits.py`の純関数（ATR
トレーリングストップ + 最大保有日数）を**バックテストと共有**しており、台帳が示す
「いくらになったら手仕舞いか」はシミュレータの挙動とずれない。ネットワークには
接続せず、設定・コード・決定論的なスクリーニング／サイジング値を書き換える経路も
持たない。

```bash
copilot-track update --as-of 2027-03-21          # 建玉と日次前進
copilot-track list                               # 公開対象のproceed（建玉中＋直近手仕舞い）
copilot-track list --status open                 # proceedの建玉中だけ
copilot-track list --status all --recommendation all  # retentionを無視した全台帳
copilot-track list --recommendation all          # skip のシャドウ建玉も含める
copilot-track show --symbol AAPL                 # verdict理由・日次マーク
copilot-track stats                              # 勝率・PF・期待値をverdict区分別に
copilot-track stats --recommendation skip        # 1区分だけ
```

`list`の既定表示は公開用の`proceed`ボードで、`tracking.published_retention_business_days`
(既定5営業日)の範囲にある手仕舞い済みだけを残す。`--status all`はこの保持期間を
適用しない従来の台帳表示で、`--recommendation all`を併用すると`skip`のシャドウ建玉も含められる。
公開表示の残り日数と`update`の手仕舞いリプレイは、どちらも建玉時に保存した
`max_hold_days`を使うため、後から設定を変更しても既存ポジションの期限表示と手仕舞い日は
変わらない。設定変更は変更後に新しく建玉されるポジションから適用される。なお、既存行の
マイグレーションで補完された`25`は、建玉当時の実際の設定値であるとは限らない。

`update`は台帳の唯一の書き込みで、run日の基準終値へ無条件に建玉し、
`backtest/exits.py`の手仕舞いルールをそのままリプレイした結果しか書かない
——この台帳が測るのは判断の当否であり、執行実績ではない（決定 #327）。
このCLIがかつて受け付けていた人間の判断メモ（`note`）と手動クローズ（`close`）は、
実売買記録機能一式の撤去（2026-08）に伴い削除された——台帳を機械的なものに保ち、
そのまま公開できる記録にするためである。撤去前に記録された`exit_reason='manual'`の
行は移行データとしてそのまま表示され続ける。

`update`は`verdicts`の`proceed`と`skip`のうち未追跡のものを建玉し、
保有中を`--as-of`まで1取引日ずつ前進させる。`no_trade`（そのrun全体が当日
エントリー非推奨だった判断）は**除外しない**——実運用ではレジームが
`CASH_PRIORITY`のrunで全verdictが`no_trade=true`になることがあり、除外すると
台帳が空になって定性判断の質を測る材料が集まらないため、`verdicts.no_trade`を
そのまま`verdict_positions.no_trade`へ引き継いで建玉する。エントリー価格は
`risk_assessments.entry_price`（= run日終値）を使い、**約定ゲートは意図的に置かない**
——`risk_assessments.limit_price`（計画指値）は参照せず、指値に刺さったかどうかに
関わらず基準終値で無条件に建玉する（決定 #327）。台帳が測るのは判断の当否であって
執行実績ではないため、計画指値とは別の基準値を使う。初期stopは`stop_price`で、
いずれもNULLなら保存済みバーの終値・`entry − exit_atr_multiple × ATR(exit_atr_period)`で
代替する（ATR期間はバックテストと同じ`settings.trade_plan.exit_atr_period`）。
バックテストも初期逆指値の終値アンカーは本番・台帳と統一されている。ただしバックテスト
の指値約定ゲート（#326、`entry_limit_atr_multiple`）は「`k`をいくつにするか」という
別の問いに答えるものであり、`k > 0`では約定価格そのものが台帳の無条件な終値エントリー
と異なるため、バックテストの数値と本台帳の数値は直接比較できない。
どちらも解決できない銘柄は建玉せず理由をnoteに出し、次回`update`で再試行する
（fail-soft）。保存済みバーが1本も無いポジション（上場廃止・ユニバース離脱など）は
前進も手仕舞い判定もできないため、毎回のupdateでその旨をnoteに出し続ける——
手動クローズが撤去された現在、対応するverdictが再取り込みで消えない限り
台帳から出る経路が無いことを利用者に知らせるためである。
日付引数（`update`の`--as-of`）を省略したときだけ、
CLI境界で`SystemClock().today()`を使う。

「バーが1本も無い」がfail-softなのは**銘柄単位**の話であり、`--db`の兄弟
`bars/`が**ディレクトリごと無い**場合は台帳に触れる前にエラーで落ちる
（Issue #221）。DuckDBファイルだけをコピーして`bars/`を並置し忘れると、
価格が1本も読めないまま「新規0件／更新0件／手仕舞い0件」を正常終了として
返してしまうためである。

`update`は建玉の前に、**verdict行を失った建玉を削除する**。`copilot-ingest-analysis`の
再取り込みはrunのverdictを丸ごと置き換えるため（`replace_run_verdicts`）、
分析対象から外れた銘柄の仮想建玉が孤児として残り、取り消された判断の
損益を出し続けてしまう。台帳は`verdicts`の派生状態なので、対応するverdictが
消えたポジションはマークごと1トランザクションで削除し、削除した銘柄を
noteに出す。`proceed`↔`skip`の訂正は孤児ではない——両側を同じ出口ルールで追跡して
いるのでリプレイは依然正しく、`recommendation`列だけをverdict側へ追随させて
その旨をnoteに出す。

### skip のシャドウ追跡と `stats`

`skip`のシャドウ建玉（Issue #190）は、**proceedと完全に同じ出口ルール**で運ぶ。
「verdictレイヤに価値があるか」は突き詰めれば「proceedだけ買った場合 vs
screening通過を全部買った場合」の差であり、片側しか追跡していない台帳では
その反実仮想が作れないためである。副産物としてサンプル母数が採用少数派から
候補全体へ広がる。

`skip`群は計測用の母集団であって提案された建玉ではないので、`list`と`show`の
既定は`--recommendation proceed`のままで、日常の朝の確認の見え方は変わらない。
`--recommendation skip` / `all`で明示的に開く。

`stats`は勝率・プロフィットファクタ・期待値・平均R倍数・保有日数中央値・
手仕舞い理由内訳を`proceed` / `skip` / `all`の3層で出す（`--recommendation`で
1層に絞れる）。勝率などのレート定義は`backtest/metrics.py`の共通関数を通るので、
バックテスト・紙トレ台帳・追跡台帳の3者で「勝ち」の意味が一致する。損益は
すべて**%単位**である——シャドウ建玉には株数が決まっていないため、各建玉を
$100 notionalへ正規化して測る（$400の株1株と$20の株1株が同列に並ぶのを防ぐ）。
R倍数はエントリー日のマークに残る**当時の**stopから計算する（ポジション行の
`stop_price`はトレーリングで切り上がっているため、それを使うとRを過大評価する）。

`list`は`区分`列でverdictの側を、`⚠`列で`no_trade`を示し、フラグが立つ行は`no_trade`と表示する
（立たない行は空欄）。`show`はさらに一文で「銘柄単体は`proceed`だが、run全体は
当日エントリー非推奨だった（実際に提案された買いとは区別して読む）」と明示する。
いずれも判定の質を測る材料として台帳に残す一方、実際に提案された買いではない
ことを一目で区別できるようにするためである。

日次前進はバックテストと同じ順序を守る: その日の手仕舞い判定は**前日までの**stopで
行い、生き残った日の終値で初めてstopをラチェット更新する（翌日から有効）。
ギャップダウンは寄り付き価格、日中安値タッチはstop価格で手仕舞いを確定し、同日に
stopと最大保有日数の両方が成立したときは常にstopが優先される。`last_marked_date`が
再開位置なので、同じ`--as-of`での再実行は何も変えない。

`update`は`copilot-daily`のfail-softステップ`track_update`としても
`retro_evaluate`の直後に毎日走る。したがって手動実行は取りこぼしの補完と
即時反映のためのものになる。

台帳のオープン建玉（`status='open'`かつ`proceed`）は、`copilot-daily`の
「保有銘柄」（ニュース・開示の収集と分析を優先的に対象へ含める held-first の
供給源、`docs/04_detailed_design.md` 3.14）の唯一の供給源である。実売買記録機能
一式の撤去（2026-08）により実オープンポジション（`positions`）は存在しなくなった
ため、日次runは台帳だけを読んで保有銘柄を決める。台帳を読まなければ保有銘柄の
ニュース収集が一度も発火せず、遡及取得できない`company-news`が恒久的に欠ける。
ただしこれは収集・分析の対象集合にだけ効き、口座や保有を受け取らない銘柄単位の
リスク判定へ仮想建玉は渡さない。`skip`のシャドウ
建玉は保有銘柄に**含めない**——そこにはnotionalにも何も保有されておらず、含めると
開示・ニュースの「保有優先」予算が定性レイヤが落とした銘柄すべてへ向いてしまう。
`--as-of`指定の再現runは台帳を読まない（現在状態であり時点再現性が無いため）。
台帳の読み取り失敗はfail-softで、警告を出して仮想側を空としてrunを続行する。

retroの`verdict_outcomes`（5/20営業日の2点分類）とは別レイヤである。
棲み分けの理由は`docs/04_detailed_design.md` 3.24.1にある。

> **P7（スキル移行）で廃止**: Anthropic API直呼びのLLM統合（`llm/`パッケージ、
> `llm_calls`テーブル、月次予算ゲート・実行単位の呼び出し上限、応答キャッシュと
> near-stale警告）はすべて削除した。表示専用だった`catalyst_quality`と
> `guidance_direction`も新しい分析契約には含まれない。

## `copilot-backfill`と履歴バックフィル

`pipeline/backfill.py`（`copilot-backfill`）は、バックテストに必要な過去データを
一度だけまとめて取り込むツールである。日次runの価格ステップは戦略のシグナルが
宣言する`required_bars`から導いたローリング窓（`default`戦略なら400暦日、
`vcp_breakout`なら770暦日）しか取らないため、これを走らせるまでローカルには
複数レジームをまたぐ検証に足る履歴が存在しない。

```bash
copilot-backfill bars --start 2019-01-01                   # ユニバース全銘柄の日足
copilot-backfill bars --start 2019-01-01 --symbols SPY,QQQ # 個別指定
copilot-backfill fundamentals --start 2019-01-01           # 10-K/10-Qの過去分
```

`--end`を省略した場合だけCLI境界で`SystemClock().today()`を使う。ドメイン関数へは
常に明示的な日付を渡す。

`--symbols`を省略したときの対象はユニバース全銘柄で、`--limit N`を付けるとその
**決定論的なNサンプル**になる（辞書順の先頭N件ではない。後述の
「`--limit`の銘柄サンプリング」節を参照。`copilot-backtest`／`copilot-daily`と
同じサンプラ・同じsaltなので、同じ`N`なら3つのCLIが同じ銘柄集合を扱う）。
`--limit`は1以上の整数のみで、0以下はfail-fastで拒否する。

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

!!! note "全件破棄と回復手順（Issue #295 の判断）"
    `write_bars`は非有限値（NaN／±inf）を1セルでも検出すると、そのバッチ全体を
    書き込まずに拒否する。これは意図した挙動で（Issue #227）、チャンク単位や銘柄単位の
    自動スキップ・除外リストは提供しない。例外メッセージが違反した`(symbol, date)`を
    最大5件まで名指しするので、回復手順はその銘柄を除いた集合を`--symbols`に渡して
    再実行すること、これが唯一の想定手順である。`YFinanceProvider`は非有限値・重複バーを
    銘柄単位で先に失敗させる（Issue #249／#301）ため、現在この拒否に到達する既知の経路は
    なく、到達するのは事実上別のプロバイダ実装を追加した場合に限られる。

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
| `--db` | `data/copilot.duckdb` | Parquetバーの根（`<db>/../bars`）も一緒に決まる。根がディレクトリとして存在しなければ、DuckDBを開く前にエラーで落ちる（Issue #221） |
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
実行せず、`--db`が存在しなければ作らずにエラーにする。`--db`の兄弟`bars/`が
無い場合も同様にエラーで、こちらはDuckDBを開く前に落ちる——DuckDBファイル
だけをコピーして`bars/`を並置し忘れると、バー系チェックが全銘柄データ不足に
なり、設定した閾値ではなく手元の欠測を測った表が出てしまうからである
（Issue #221。`copilot-track` / `copilot-retro` / `copilot-dd-forward` /
`copilot-backtest`も同じ検証を共有する）。ただし`MarketStore`は
共有DuckDBファイルを読み書きモードで開いて自分の`fundamentals`テーブルと
`bars`ビューを用意するため、DuckDBの単一ライターロックはかかる。
`copilot-daily`の実行中には走らせないこと。

## `copilot-dd-forward`とDistribution Day水準の予測力

`regime.dd_*`は roadmap §5 P3-13 で**要検証**のまま本番に入っていた。しかも
当初は検証する手段が無かった。`backtest/`は`regime.exposure`も
`regime.distribution`もimportしておらず、`dd_*`をどう動かしても
`copilot-backtest`の数字は1つも動かなかった。現在は`SEVERE`だけが
`REDUCE_ONLY`の警戒ラベルに影響し、HIGH/CAUTIONは表示専用である。
`CASH_PRIORITY`はSMA200を3%超下回る非FTD状態、またはVIX>30に限定する。

Issue #184の`copilot-backtest --policy none|regime|regime+earnings`は、この閉路を
戦略まるごとの水準で開いた（`backtest/policy.py`が本番の`RiskChecker`を包んで
注入する）。以下の`copilot-dd-forward`はそれと補完関係にあり、戦略の勝ち負けに
混ぜず**水準そのもの**の予測力だけを分離して見る道具である。

`copilot-dd-forward`（`regime/dd_forward_cli.py`）は保存済み履歴を1日ずつ
`as_of`として再生し、`pipeline/daily.py::_calculate_regime_snapshot`と同一の
分類（SPYとQQQそれぞれの水準の`max`）を出したうえで、その後に実際に起きた
リターンとドローダウンを水準別に集計する。

この検証の結果、`dd_severe_d25`/`dd_severe_d15`は2026-08-07にIssue #111で
`7`/`6`（従来`6`/`4`）で**採用済み**（根拠:
`reports/regime/2026-08-06-dd-threshold-review.md` §10）。`dd_high_*`（5/3/2）と
`dd_caution_d25`は据え置きで、Exposureには影響しない表示用である。

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
| `--start` | 履歴の先頭 | 最初の観測日。手前は助走（DD窓とSMA200シード）として読む |
| `--horizons` | `5,10,25` | 先行きリターンの保有営業日数。25は`trade_plan.max_hold_days` |
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
`CASH_PRIORITY`軸（旧来のDD単独モデルで`severe_*`だけが決める）と
`REDUCE_ONLY`軸（`high_*`だけが決める）に分けて出す。これは保存済みの閾値レビューと
比較可能にする探索用の写像であり、本番のIssue #252の6分岐（SMA200/VIX/FTDを含む）を
再現するものではない。2つは独立なので、片方の差で5次元を並べると
もう片方の同じ挙動の変種で埋まるためである。候補は
`config.RegimeConfig._validate_dd_level_order`と同じ順序制約を通したものだけで、
そのまま`settings.yaml`に書けば読める。

順位は同じ履歴の in-sample スコアである。候補の絞り込みには使えるが、それ
自体は out-of-sample の検証ではない。

`copilot-filter-matrix`と同じくオフラインかつ読み取り専用で、スキーマ
マイグレーションを実行せず、`--db`が無ければ作らずにエラーにする。`--db`の
兄弟`bars/`が無い場合も同じくエラーである（Issue #221。放置すると全銘柄が
`NO_DATA`の、本物の結果と同じ体裁の診断が出てしまう）。
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

`--db`を渡すと**価格バーの置き場所も一緒に決まる**。根は常に`<db>/../bars`で、
`data/copilot.duckdb` + `data/bars`という既定の対応規約をそのまま`--db`に適用
したものである。バリアントごとにDuckDBをコピーして`--db`で指す運用では、
**`bars/`を並置し忘れやすい**。

!!! warning "`bars/`が無い`--db`は実行前にエラーで止まる（Issue #217）"

    解決した`<db>/../bars`がディレクトリとして存在しなければ、
    `copilot-backtest`はレポートを書く前に終了コード1で落ちる。以前は全銘柄が
    「データ不足」となり、**取引ゼロのレポートを数秒で書いて正常終了**して
    いた——40〜56分かかるはずの処理が3秒で終わり、体裁の整ったレポートだけが
    残るため、操作ミスだと気づけなかった。`bars/`をコピー先へ並置するか、
    `--db`を元の場所へ向けること。「数銘柄だけバーが無い」は従来どおり警告
    のみで完走する（fail-soft）。

!!! note "バックテストはDuckDBを読み取り専用で開く（Issue #358）"

    `copilot-backtest`は既存のDuckDBスキーマを検証するだけで、実行中に
    `init_schema()`や永続ビュー作成を行わない。`--db`が未作成または未初期化なら
    終了コード1で止まるため、先に`data-pull`またはデータ収集を実行すること。

`run`のレポート（`--pessimistic`の通常vs悲観比較を含む）には`Exit breakdown`
セクションが出る。決済理由の内訳（`stop` /
`max_hold` / `end_of_backtest`、発火0件の理由も0として必ず表示）、
`max_hold binding rate`（全決済に占める`max_hold`の割合）、実保有日数の
中央値と四分位である。感応度グリッドのMaxHold列が全て同値だったとき、
「そのパラメータが効かない」のか「一度も発火していない」のかを区別するために
ある。binding rateが0%に近ければ、`max_hold_days`をどう振っても結果は動かない。

### 指値約定ゲート（Issue #326）

`trade_plan.entry_limit_atr_multiple` が`0.0`のときは既存互換の翌営業日寄付モデルを
使う。正の`k`では、シグナル日の終値とATR14から共有純関数
`backtest/entries.py::entry_limit_price(close, atr14, k)`で
`limit = close + k × ATR14`を作る。翌日のOHLCが始値`<= limit`なら始値に通常の
entry slippageを適用し、始値が上でも安値`<= limit`なら指値ちょうどで約定する。
安値も指値を上回る日はDay注文の窓内に刺さらなかったとして約定せず、レポートの
`Entry blocks`に`limit_not_reached`を1候補日として計上する。未約定注文は翌日へ
持ち越さない。日中足がないため、安値に触れたことを約定とみなす日足近似であり、
実板での約定保証ではない。

`risk/checks.py`も同じ価格関数を使い、計画指値と逆指値から銘柄単位の1Rを計算する。
感応度を測る場合は
`backtest.sensitivity.entry_limit_grid_values()`の絶対ATR倍率
`0.0/0.5/1.0/1.5/2.0`を`BacktestCostOverrides(entry_limit_atr_multiple=...)`へ
順に渡す。`copilot-backtest entry-grid`はこの5点を同じ候補ストリームへ順に渡す
CLIであり、既定出力は`reports/backtests/<end>-<strategy>-entry-grid.md`である。
既存の`grid`と同様に`--policy`へ非デフォルトのゲートを指定するとfail-fastする。
`--candidate-cache`は`settings.trade_plan`をキャッシュキーから除外しているため、
この運用でもスクリーニングは1回で済む。バックテストの初期逆指値は本番・台帳と
同じくシグナル日終値をアンカーとし、株数サイジングは`limit_price`を基準にする。

```bash
copilot-backtest entry-grid --strategy default --start 2020-01-02 --end 2026-07-30 \
  --candidate-cache /tmp/candidates-default-2026-07-30.parquet
```

約定日に寄付が逆指値を下回る場合も、その日の出口評価で寄付価格のstop決済
（`days_held=0`）になる。**移行前後の実測**
（`reports/backtests/2026-08-23-issue-341-entry-stop-anchor.md`）: 移行前は
約定価格アンカーのため`k=1.5/2.0`が`k=0.0`とバイト単位で同一の結果に潰れる
（`open ≤ limit`が常に成立し`next_open`互換アームと同じ約定価格になるため）
回帰があった。移行後は`k`の増加に応じて`avg_invested_pct`が単調に縮小し
（19.61%→16.58%）、`k`の選択が実際に指標へ反映されるようになった。

## 決算ゲートの決算日はどこから来るか（`--policy regime+earnings`）

`--policy regime+earnings`のアームだけが決算ブロック（`risk.earnings_block_business_days`）
を適用する。バックテストには過去の決算カレンダーが無いので、決算日は
**収集済みの提出履歴（`fundamentals`テーブルの`10-K`/`10-Q`）から推定**する。
外部の決算カレンダーAPIは呼ばない。実行時に

```text
決算ゲート: 提出履歴（10-K/10-Q）から132/500 銘柄の決算日を推定します（…）
```

の1行が出る（分子は提出が2件以上ある＝周期を測れる銘柄数で、期間中どこかで
推定できる上限である）ので、`Entry blocks`の`earnings`件数は必ずこの被覆率と
合わせて読むこと。
推定できない銘柄は「不明」として警告のみに落ち、**ブロックされない**——0件は
「決算が近い候補が無かった」ではなく「推定できなかった」かもしれない。

推定の中身は「`as_of`時点で見えている提出日の連続差の中央値を、最新の提出日へ
加える」である。`filed_at <= as_of`しか見ないので、当時知り得なかった提出を
先取りすることはない。射影日が`risk.earnings_lookahead_days`より先なら「窓内に
無し」、逆に`as_of`が射影日を追い越していたら（＝予測した時期に提出が来なかった）
「不明」へ落とす。

!!! warning "前提と限界"

    - **提出日は発表日より遅い**。決算発表（8-K Item 2.02）は10-Q受理の数日前が
      通例なので、この推定に基づくブロック窓は真の決算日より**後ろにずれる**。
      周期も射影も提出日で測るため内部整合はしているが、実際の決算発表を
      避けた効果をそのまま測っているわけではない。
    - **被覆率は収集履歴に等しい**。`copilot-daily`が触れた銘柄・期間しか
      `fundamentals`に入っていないため、バックテスト期間の前半ほど推定できない
      銘柄が増える。
    - **Q4の決算は観測できない**。年次報告（10-K）の提出は発表の数週間後であり、
      Q4に対応する10-Qも存在しないため、Q4は射影でしか覆われない。
    - 訂正再提出（`10-Q/A`）は決算イベントに数えない。同一四半期の再提出は
      最初の提出日1件へ畳む。

## `--limit` の銘柄サンプリング

`copilot-backtest --limit N`・`copilot-daily --limit N`・`copilot-backfill --limit N`は
**ユニバースのサンプル**を対象にする。以前の`symbols[:limit]`は`ORDER BY symbol`の先頭N件、つまり
「Aで始まるN銘柄」を返していた。セクター構成がS&P500と別物になるうえ、
Minerviniの RSパーセンタイル（条件7）のように*渡された集合内の相対順位*で
決まるチェックは条件の意味自体が変わってしまう。

現在は`swing_copilot.universe_sampling.select_universe_sample()`が
`gics_sector`ごとに比例配分（最大剰余法、端数は剰余の大きい順・同率は
セクター名順）し、各セクター内はsalt付きblake2bのハッシュ順で選ぶ。
アルファベット順とは無関係で、同じユニバースと同じ`N`なら実行環境や実行日を
問わず必ず同じ銘柄集合になる（saltは固定。変えると過去レポートとの比較可能性が
失われる）。`N`がユニバース規模以上なら全銘柄と同義である。

**3つのCLIは同じサンプラと同じsaltを共有する**（Issue #205、#206）。同じユニバースと
同じ`N`なら`copilot-daily --dry-run --limit 20`のスモーク実行と
`copilot-backtest --limit 20`は同じ銘柄集合を見るので、両者の結果を突き合わせ
られる。`copilot-backfill bars --limit 20`が暖機するのも同じ20銘柄なので、
「Aで始まる銘柄だけキャッシュが温まっている」状態にはならない。
差分は`--limit`の下限だけである。`copilot-backtest`と`copilot-backfill`は
0以下をfail-fastで拒否し（`copilot-backfill`のメッセージは
「`--limit`は1以上の整数で指定してください。」）、`copilot-daily`は`0`を
「ユニバース由来の新規候補を選ばず、開いている保有銘柄だけを残す」意味の
有効値として受け付ける（負数はいずれも拒否）。
`copilot-daily`は`--limit`の値に関わらず保有銘柄を常に対象集合へ足す。

採用した方式・実銘柄数・セクター構成は、`copilot-backtest`のterminal出力と
markdownレポートの冒頭に必ず出る（`run`・`--pessimistic`比較・`--policy`比較・
`grid`の全レポート共通）。`copilot-daily`は日次レポートの体裁を変えず、同じ
2行をINFOログに出す（`--limit`は検証・スモーク用フラグで、本番の定時実行は
`--limit`を付けないため）。

```text
ユニバース: 60/503 銘柄の決定論的サンプル（gics_sector 比例配分 + blake2b ハッシュ順、シード固定・再現可能）
セクター構成: Communication Services 3, Consumer Discretionary 6, ...
```

生存者バイアス注記と同じ扱いで、指標だけを切り出して読まれないようにするための
但し書きである。

## 本番ゲートのA/B（`--policy`）

`--policy`は「候補→建玉」の間に市場状態と決算ブロックをどこまで通すかを選ぶ。
カンマ区切りで複数指定すると、
**同一の候補ストリーム**に対してアームごとにエンジンだけを走らせ、指標と
ゲート発動回数を列比較する。

```bash
copilot-backtest --strategy default --start 2020-01-02 --end 2026-07-30 \
  --policy none,regime,regime+earnings
```

- `none`: ゲート無し（従来の挙動）
- `regime`: レジームのExposure Ceilingのみ
- `regime+earnings`: レジーム＋決算ブロック

レポートの`Entry blocks`は「入らなかった理由」を*候補件数（発動セッション数）*
の形で出す。`regime`が`120 (37d)`なら、37営業日でレジームが閉じ、その日の候補
延べ120件が入らなかった、という読み方になる。

ゲートの入力は必ず**シグナル日**（＝候補生成日）のバーだけで評価する。約定は
翌営業日寄付なので、約定日当日のバーでレジームを判定すればlook-aheadになる。
なお`SPY`/`QQQ`/`^VIX`のバーは`--policy`の指定有無にかかわらず常に読み込む
（アームごとにキャッシュキーが変わると、A/Bが1本のストリームを共有できなく
なるため）。これらの価格履歴が無い状態で`--policy`を指定すると、レジームが
UNKNOWNのまま全期間を塞ぐ結果を黙って出す代わりに、実行前にエラーで止まる。

`--policy`とは独立して、1建玉のサイズは
残現金ではなく`equity = cash + 建玉時価`から決まる。旧来の現金基準は保有が
増えるたびにサイズを`0.9^n`で縮め、10銘柄満玉でも投下資本が約65%にしかならず、
本番/公開分析とは独立した名目資金系である。なお、ここでの金額と比率は
バックテストのシミュレーション値であり、投資助言や本番のポジションサイズではない。
`run`の指標に
出る`avg_invested_pct`（各日の建玉時価/equityの平均）と
`max_concurrent_reached`が、この投下度合いをそのまま数字にする。

## 候補ストリームキャッシュ（`--candidate-cache`）

候補生成（スクリーニング）は`copilot-backtest`の中でもっとも重い工程であり、
同時に、グリッド・シナリオ・CLI実行をまたいで使い回せる唯一の中間生成物でも
ある。そこで候補生成をエンジン走行から切り離し、`--candidate-cache PATH`で
Parquetへ永続化できるようにした。

!!! note "「大半はスクリーニング」という目安の履歴"

    この節はかつて「実行時間の大半はスクリーニングであり、エンジン走行では
    ない」と断定していたが、その比率は2度動いている。#214/#242でATRの
    O(n²)再平滑化を1パス読みへ移した結果、支配項はエンジン走行——`_bar`/
    `_latest_bar`によるフルフレームmasking——へ移った。#244でこの2つを
    銘柄別索引の参照へ移し、支配項は再び候補生成側へ戻っている。実測
    （合成データ・オフライン・同一マシン）は次のとおり:

    | 計測対象 | 変更前 | 変更後 |
    |---|---|---|
    | `_bar` 1呼び出し（508銘柄×1,652セッション＝839,216行） | 32.81ms | 0.0153ms |
    | `_latest_bar` 1呼び出し（同上） | 29.56ms | 0.0120ms |
    | エンジン走行のみ（101銘柄×500セッション＝50,500行） | 37.72秒 | 0.23〜0.25秒 |
    | エンジン走行のみ（509銘柄×1,652セッション＝840,868行） | 約34分（外挿） | 1.05〜1.11秒 |

    キャッシュを置く理由は「常にスクリーニングが大半だから」ではなく、
    **再利用できるのが候補ストリームだけだから**である。どちらが支配的かは
    銘柄数・期間・マシンで動くので、必要なら都度計測する。

```bash
copilot-backtest grid --strategy default --start 2020-01-02 --end 2026-07-30 \
  --candidate-cache /tmp/candidates-default-2026-07-30.parquet
```

キャッシュキーは**スクリーニングが読む入力だけ**で構成する: 戦略キーと
そのspec（`candidate_limit`・`score_weights`等）、`technical_signals`、
`fundamental_filters`、ユニバース、対象銘柄、`--start`/`--end`、ベンチマーク
（取引日カレンダーの源泉）、そして価格・ファンダの内容ダイジェストである。

`settings.trade_plan`（`entry_limit_atr_multiple`・`exit_atr_multiple`・
`exit_atr_period`・`max_hold_days`）と、`settings.backtest`のコスト・名目資金設定、
`settings.risk`、初期資金は
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
| `technical_signals.pullback.band_atr_multiple` | `settings.yaml` | `2.0` | `\|close − SMA50\| / ATR14 ≤ 倍率`で帯を判定する |
| `ranking.score_weights.atr_pct` | `strategies.yaml` | `0.0`（無効） | ATR%が高いほど高得点の成分を合成スコアへ加える |

`band_atr_multiple`が無ければ互換モードとして帯は
`|close − SMA50| / SMA50 ≤ 0.03`という固定3%で判定される。これは低ボラ銘柄を
高ボラ銘柄の約4.5倍通過させる事実上のローボラフィルタとして働く。ATR単位で
測れば、パイプラインが既に執行距離に使っている`execution.fair_max_d`（SMA50から
ATR 2.0個分）と同じ尺度になる。
ATRがNaNまたは0のときは距離が定義できないため帯を閉じる（安全側）。

`atr_pct`成分は候補集合内のパーセンタイルではなく、ATR% 6%を満点とする
**絶対正規化**である。候補が5件程度しかない集合でパーセンタイルを取ると、
`liquidity`成分が既に抱えている小標本ノイズを再生産するためである。
`score_weights`の合計1.0検証にも加算されるので、`atr_pct`を入れるときは
他の重みを必ず下げることになる。

## 戦略別ランキング成分（issue #251）

出荷中の3戦略は`score_weights`が同一で、押し目買い前提の`rsi_pullback`が
最大の重みを持つ。そのため**ブレイクアウト戦略`vcp_breakout`は「押し目の
深さ」で順位付けされる**（戦略の意図と逆）。是正のための成分を3つ追加した。
いずれも**既定0.0**で、出荷中のどの戦略のランキングも動かさない。

| 重み | 由来メトリクス | 必要なsignal | 正規化 |
| --- | --- | --- | --- |
| `ranking.score_weights.pivot_proximity` | `vcp_pivot`と`close` | `vcp_breakout` | ピボット丁度で1.0、上下どちらへ`chase_pivot_pct`離れると0.0 |
| `ranking.score_weights.rs_percentile` | `minervini_rs_percentile` | `minervini_stage2` | 0–100を100で割る |
| `ranking.score_weights.criteria_met` | `minervini_criteria_met` | `minervini_stage2` | 0–7を7で割る |

3成分とも特定のsignalしか書き込まないメトリクスを読むため、**そのsignalを
`signals_all`に持たない戦略で重みを0より大きくすると、外部I/Oの前に
`ConfigError`で落ちる**。重みを付けたのに全候補で同じ0.0が入り、実質的に
他成分の重みだけが薄まる、という無言の劣化を防ぐためである。

signalは走っているがメトリクスが無い候補（例: 252日履歴が足りずRSが
計算できないまま6/7条件でヒットしたMinervini銘柄）は、その成分だけ0.0と
して扱う。条件7が満たされなかったのと同じ「最も弱い読み」であり、候補を
落としはしない。

`pivot_proximity`がピボットの上下で対称なのは、ピボット直下で収縮している
セットアップと抜けた直後の銘柄がどちらも「ピボット付近」であり、すでに
`chase_pivot_pct`上へ伸びた銘柄こそVCPが避けたい追いかけ買いだからである。
正規化幅は`vcp.chase_pivot_pct`（ピボットからどこまで上の候補を通すかの
上限）そのものから導出する（Issue #297）。かつては独立の定数だったが、
フィルタが通す帯域とスコアが飽和する帯域は一致しているべきで、独立させると
`chase_pivot_pct`を動かした瞬間に帯域の一部が無言で同点化する結合になる。
`chase_pivot_pct`は幅としての意味を失う`0.0`を`config.py`が`gt=0.0`で
弾くため、`pivot_proximity`が幅ゼロ除算を起こすことはない（既定値`0.05`は
変更なし）。

ただし一致するのは**上側だけ**である。`is_chasing_pivot`が縛るのは
`close > pivot * (1 + chase_pivot_pct)`、つまりピボットより上への伸びだけで、
下側に対応するフィルタは無い。下側で同じ幅を使うのは「ピボット直下の収縮も
同じ尺度で測る」という設計上の選択であって、フィルタ帯域との一致ではない。
`chase_pivot_pct`を動かすと、上側は「通す帯域」と「スコアが飽和する幅」が
揃ったまま動き、下側は評価尺度だけが動く。

**既定値の変更（段階2）は未了である。** 機構は入ったが、`vcp_breakout`の
順位付けは依然として押し目の深さを向いている。既定値を動かすには
`docs/08_architecture_review_2026-08.md`の原則どおりバックテストの裏取りが要る。

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
