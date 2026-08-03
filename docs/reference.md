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

再実行は安全かつ安価で、既存バーが`--start`以前まで届いている銘柄は
ネットワークを叩かずにスキップする（`MarketStore.earliest_bar_dates`）。
逆に、その時期にまだ上場していなかった銘柄はこの条件を満たしようがないため
毎回再取得される。銘柄単位の「取得済みだが空」状態を持たないという設計上の
割り切りで、一回限りのツールにその台帳を持たせるほどの価値が無いと判断した。
銘柄単位の失敗はfail-softで、失敗した銘柄名を集約して最後に報告し、
他の銘柄の取得は続行する。

**ベンチマーク・レジーム系のシンボル（`SPY`・`QQQ`・`^VIX`・`^TNX`）は
S&P 500ユニバースに含まれないので、`--symbols`で別途バックフィルする必要が
ある。**特に`SPY`はバックテストの取引日カレンダーそのもの
（`backtest/runner.py::_trading_days`）なので、これを取り込まないと
バックテスト期間はSPYのバーがある範囲まで黙って縮む。

`fundamentals`は`EdgarClient.fetch_fundamentals`の`lookback_days`を`--start`まで
広げて呼ぶ。EDGARのbulk company-factsは常に全履歴を返すので、追加のアダプタ
改修なしに過去四半期を`filed_at`付きで取り込める。`EDGAR_IDENTITY`が未設定なら
何もせず非0終了する。

## バックテストの設定バリアント比較

`copilot-backtest`は`--settings`と`--strategies`でそれぞれ`config/settings.yaml`と
`config/strategies.yaml`を差し替えられる。リポジトリの設定を書き換えずに
A/B比較を回すための入り口である。ランキング重み（`score_weights`）は
`strategies.yaml`側にあるため、重みバリアントの比較には`--strategies`が要る。

```bash
copilot-backtest --strategy default --start 2020-01-02 --end 2026-07-30 \
  --settings /tmp/variant/settings.yaml --strategies /tmp/variant/strategies.yaml
```

レポートには`Exit breakdown`セクションが出る。決済理由の内訳（`stop` /
`max_hold` / `end_of_backtest`、発火0件の理由も0として必ず表示）、
`max_hold binding rate`（全決済に占める`max_hold`の割合）、実保有日数の
中央値と四分位である。感応度グリッドのMaxHold列が全て同値だったとき、
「そのパラメータが効かない」のか「一度も発火していない」のかを区別するために
ある。binding rateが0%に近ければ、`max_hold_days`をどう振っても結果は動かない。

## 低ボラバイアス是正の2つのスイッチ

スクリーニング候補が構造的に低ボラ銘柄へ偏る原因は2つあり、それぞれに
**既定では無効**なスイッチを用意した。既定値の変更＝採用は、比較レポートを見た
人間が行う。

| 設定 | 場所 | 既定 | 効果 |
| --- | --- | --- | --- |
| `technical_signals.pullback.band_atr_multiple` | `settings.yaml` | `null` | `\|close − SMA50\| / ATR14 ≤ 倍率`で帯を判定し、`sma_band_pct`を無視する |
| `ranking.score_weights.atr_pct` | `strategies.yaml` | `0.0` | ATR%が高いほど高得点の成分を合成スコアへ加える |

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
