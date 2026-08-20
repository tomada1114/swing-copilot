---
name: swing-deepdive
description: >
  Produce an on-demand deep-dive memo for one or more specific equity symbols
  from an existing analysis_input.json — multi-quarter filing shifts,
  risk-factor language, news chronology, an explicit bull case / bear
  case, and a watch-trigger list — written to a standalone Markdown file that
  never touches analysis_result.json or the daily report. Use PROACTIVELY when:
  深掘り、この銘柄を詳しく、個別銘柄の掘り下げ、単独で深く分析、
  deep dive, deepdive, dig into this symbol, take a closer look at.
---

# 銘柄深掘り分析（オンデマンド）

指定された銘柄について、日次の `swing-daily` より**深く長く**読む。
日次が「全候補を同じ深さで捌く」のに対し、こちらは「気になる 1〜数銘柄を掘る」。

出力は**独立した Markdown メモだけ**。日次パイプラインの成果物
（`analysis_result.json` / `analysis_work/**` / `reports/latest.md`）には**一切触れない**。

作業前に必ず読む:

- [../swing-daily/references/analysis-conventions.md](../swing-daily/references/analysis-conventions.md)
  — AC1〜AC16 の共通規約（**必読**）。このスキルの出力は ingest の機械検査を通らないため、
  規約はスキル指示として**自力で**守る必要がある

## Inputs

- **対象銘柄**（1〜複数）— 必須。指示に銘柄が無ければ、勝手に選ばず
  `AskUserQuestion` 等でユーザーに確認する
- **`analysis_input.json` の絶対パス** — 任意。読み取り専用。省略時は下記の手順で
  最新 run のものを探す
- 参考: `<WORKDIR>/analysis_result.json`（既にあれば当日の verdict を**読むだけ**）

### 最新 run の特定（パス省略時）

```bash
ls -1t reports/20*/*/analysis_input.json | head -5
```

- `reports/<run_date>/<run_id>/analysis_input.json` が正しい形。`reports/dry_run/**`
  と `reports/retro/**` は対象外（上のグロブで除外される）
- **日付ディレクトリが最新のもの**を優先し、同一日付に複数 run があればタイムスタンプが
  最新のものを選ぶ。選んだパスと `as_of` をユーザーに明示してから進む
- 候補が 1 件も無ければ、分析せず「入力が無いため `swing-daily` の実行が先」と報告する

### 対象銘柄が入力に含まれない場合

`candidates[].symbol` に対象が無ければ、**その銘柄は分析しない**。事前知識で埋めない（AC8）。
「この run の候補は A/B/C であり、指定の X は含まれないため深掘りできない」と、
選んだ run のパス・`as_of`・候補一覧を添えて報告する。複数銘柄の指定なら、
含まれるものだけを分析し、含まれないものを個別に理由付きで報告する。

## Outputs

- `<WORKDIR>/deepdive-<SYMBOL>.md` — 銘柄ごとに 1 ファイル
  （`<WORKDIR>` は `analysis_input.json` があるディレクトリ、`<SYMBOL>` は入力の
  `symbol` をそのまま大文字で）。既存ファイルは上書きしてよい。
  `swing-daily` の `headless_note.md` と同様、**契約されたアーティファクトではなく**
  ingest は読まない
- ユーザーへ返すのは**要約（結論 / 強気の要点 / 弱気の要点 / 監視トリガー）+ メモの絶対パス**。
  メモ全文・開示原文・ニュース本文はメッセージに載せない

## 触れてはならないもの【最重要】

以下は**読むのは可、書くのは絶対不可**。

- `<WORKDIR>/analysis_result.json`
- `<WORKDIR>/analysis_work/**`
- `reports/latest.md` および当日の `reports/<run_date>/<run_id>.md`

理由: これらは `copilot-ingest-analysis` の strict schema 検証・provenance 検証・
CON-03 fail-closed 判定、および `swing-daily` Step 0 の再入判定の管理下にある。
deepdive は**そのゲートの外**で動くため、ここに書き込むと検証を受けていないテキストが
レポートへ流れ込み、`analysis_work/` の断片流用判定も壊れる。
深掘りの結果を日次レポートに反映したい場合は、メモを根拠として
`swing-daily` を再実行する（このスキルが直接書き換えることはしない）。

## Step 1: スライスの切り出し（セッション本体）

セッション本体が `analysis_input.json` を読み、対象銘柄ごとに必要な範囲だけを取り出す。

- `symbol` / `score_breakdown` / `risk_constraints`
- `news[]`（`source_id` / `published_at` / `headline` / `summary` / `provider`）
- `filings[]`（`source_id` / `form_type` / `filed_at` / `text` / `coverage`）
- run 単位の `context.market_regime` / `context.calendar_events`
- `run_id` / `as_of` / `input_digest`（メモのヘッダに逐語転記する）

文字列と `source_id` は**逐語コピー**する。要約・ID の再採番・他銘柄のテキスト混入をしない。
担当外銘柄の長文本文を親コンテキストへ載せない。切り出しは読み取り専用の作業ファイルへ書き、
その絶対パスをサブエージェントへ渡す（置き場所は `analysis_work/` 以外にする）。

## Step 2: 銘柄ごとにサブエージェントへ委譲

Agent ツール（**`model: opus`**）を、銘柄分**同一メッセージ内で並列**に起動する。
1 エージェント = 1 銘柄。指示には必ず次を含める。

1. `.claude/skills/swing-deepdive/SKILL.md` と
   `.claude/skills/swing-daily/references/analysis-conventions.md` を読み、それに従うこと
2. 入力スライスと元 `analysis_input.json` の**絶対パス**、担当銘柄シンボル
3. 出力は `<WORKDIR>/deepdive-<SYMBOL>.md` への**ファイル書き出し**（下記テンプレート）
4. **供給されたデータのみを使うこと**（Web 検索・ネットワーク取得の禁止。例外は Step 3）
5. 親に返すのは要約 + AC 自己点検結果だけ。メモ全文・入力本文を返さないこと

### 深掘りの観点（日次より深く読む部分）

- **開示の時系列変化**: 同一銘柄に複数四半期の `filings` があれば、四半期をまたいだ
  文言・数値の変化を追う。ただし**リスク要因（Item 1A）の新規性は論点として立てない**
  （Issue #127）。10-Q の Item 1A は前回提出から重要な変更が無ければ 10-K への参照援用だけで
  済ませてよく、比較対象となる 10-K 本文は `analysis_input.json` に含まれない。deepdive は
  日次より深く読むが**入力の範囲は日次と同一**であり、四半期を並べても新規性の判定材料は
  増えない。よって新規性は**現在の入力範囲では判定不能として扱い、判定を試みない**。
  「新規追加／削除／表現の強まり」を特定しようとせず、「入力からは判定不能」
  「比較対象が無い」といった文言も本文・「入力の限界」節のいずれにも書かない
  （毎回同じ判定不能を書いても情報価値がなく、他の論点を薄めるだけである）。
  Item 1A に実質本文（リスクの記述本文、あるいはリスク見出し／キャプションの列挙）があれば、
  新規性ではなく**そこに書かれているリスクの内容そのもの**を読む。リスク要因以外の
  文言・数値（MD&A、ガイダンス、法的手続など）の四半期比較は従来どおり行ってよい。
  いずれの場合も「新規リスクなし」「リスクは変化していない」とは書かない（AC8）
- **ニュースの時系列文脈**: `published_at` 順に並べ、単発の見出しではなく**流れ**として読む。
  古い項目は古い旨を明記する
- **bull case / bear case の両論併記**: どちらか一方に寄せず、両方を同じ密度で書く。
  各ケースについて「この読みが崩れる条件」を添える
- **監視トリガー**: 「今後どの観測値が出たら読みが変わるか」を条件式として列挙する。
  行動の指示ではなく**観測条件**として書く
  （NG:「割れたら手仕舞ってください」／ OK:「x を下回った場合、bear case の前提が
  補強されると読める」）
- 決定論的な数値（スコア、サイジング、ランキング、リスク制約）は**解釈のみ**。
  再計算・上書き・訂正をしない（AC1・AC2）

## Step 3: Web 調査（既定は禁止）

**既定では供給データのみ**を使う。新規のネットワーク取得・Web 検索は行わない。

ユーザーが明示的に「Web も調べて」と依頼した場合に限り許可し、次を守る。

- Web 由来の内容は本文に混ぜず、**「## 付録: Web 調査（point-in-time 保証の外）」**
  という独立セクションにまとめる
- 各記述に**出典 URL と取得日**を付ける
- そのセクションの冒頭に「本節は `analysis_input.json` の外部にあり、`as_of` 時点の
  point-in-time 可視性が保証されない。日次パイプラインの判断材料と同列に扱えない」旨を書く
- Web 由来の内容を `source_id` 付きの fact として書かない（`source_id` は入力にしか存在しない）

## Step 4: 検収と報告

セッション本体がメモを読み、次を点検してからユーザーへ報告する。

- 触れてはならないファイルに書き込んでいないか（`git status` で確認できる）
- 事実に `source_id` が付いているか、その ID が**その銘柄の入力に実在**するか（AC6・AC7）
- 入力に無い情報が混ざっていないか（AC8）、決定論的数値を書き換えていないか（AC1）
- CON-03 相当（AC3・AC4・AC5）、hedge（AC12）、非網羅の明記（AC13）
- bull / bear の両方が書かれているか

報告は**結論 / 強気の要点 / 弱気の要点 / 監視トリガー / メモの絶対パス**。
最終判断は人間である旨を添える。

## 言語規律（コード側のゲートの外にある）

このメモは `copilot-ingest-analysis` を通らない。CON-03 の機械検査も provenance 検査も
かからないため、**スキル指示としてより厳格に**課す。

- **断定的売買指示を書かない（AC3）**、**読者への命令形を使わない（AC4）**。
  「買うべき」「売るべき」「〜してください」「you should buy」等
- **根拠なき心理・行動診断を書かない（AC5）。** 迷ったら心理に言及せず、観測事実で書く
- **事実と推論を分離する（AC11）。** 事実には `(source_id: news-...)` の形で出典を明記し、
  評価語（「好調」「悪化」）は推論側に置く。ID は入力からの逐語コピーとし、複数ソースに
  支えられる事実は該当 ID をすべて挙げる（AC7・AC9）
- **推論には hedge を付ける（AC12）。**「〜の可能性がある」「〜と読める」「入力の範囲では〜」
- **非網羅である旨を明記する（AC13）。** `filings[].coverage` の `is_truncated` や章の
  `partial` / `missing` を無視せず、未分析範囲を「章名 + 欠落量 + 欠落位置」で書く。
  `exhibit_truncated: true` は取得段で 8-K の Exhibit が切られたことを指し、
  `is_truncated: false` と同時に立ちうる（欠落の範囲・位置は入力からは特定できない）。
  `false` は「マーカーが無い」であって「欠落が無い」ではない
- **入力が空・極小なら無理に埋めない（AC14）。**「該当テキストなし」と書く
- **検査を通すための言い換えをしない（AC15）。** 検査が無いからこそ実質で守る

## 出力ファイル

`<WORKDIR>/deepdive-<SYMBOL>.md` を次の構成で書く。

```markdown
# 深掘りメモ: <SYMBOL>

- as_of: <入力の as_of を逐語コピー>
- run_id: <逐語コピー>
- input_digest: <逐語コピー>
- 入力: <analysis_input.json の絶対パス>
- 注記: 本メモは copilot-ingest-analysis の検証を通らない独立文書であり、
  analysis_result.json および日次レポートには反映されない。最終判断は人間が行う。

## 結論（1〜3 行）
## スクリーニングの読み直し   <- 決定論的数値は引用のみ
## 開示の時系列変化           <- 四半期比較（リスク要因の新規性は扱わない）、未分析範囲の明記
## ニュースの時系列文脈
## bull case                  <- 各項に根拠 (source_id: ...) と「崩れる条件」
## bear case                  <- 同上
## 監視トリガー               <- 観測条件の列挙。行動指示にしない
## 入力の限界                 <- 非網羅・欠落章・古い出典・判定不能だった論点
## AC 自己点検                <- 「AC1-AC16 違反なし」または懸念の AC 番号と一言
```

## 禁止事項

- `analysis_result.json` / `analysis_work/**` / `reports/latest.md` /
  `reports/<run_date>/<run_id>.md` への書き込み
- 決定論的なスコア・ランキング・サイジング・リスク制約の再計算や書き換え（AC1）
- 入力に無い情報の補完（事前知識による企業情報・株価・決算数値の追加）（AC8）
- 明示依頼の無い Web 検索・ネットワーク取得、および Web 由来の内容を本文へ混入させること
- 断定的売買指示、命令形、根拠なき心理・行動診断（AC3〜AC5）
- `copilot-daily` / `copilot-ingest-analysis` の実行（このスキルは読むだけ。
  再実行が要るなら `swing-daily` に任せる）
- `src/`, `tests/`, `docs/` の編集（このスキルは分析であり実装変更ではない）
