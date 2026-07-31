---
name: swing-daily
description: >
  Run the end-to-end daily swing-trading analysis loop. Executes `copilot-daily`
  for the deterministic pipeline, fans out news/filings/screening interpretation
  to parallel expert subagents, reconciles their findings, decides a per-symbol
  proceed/skip verdict, writes analysis_result.json, and re-renders the final
  report via `copilot-ingest-analysis`. Use PROACTIVELY when: 日次分析、
  デイリー実行、今日の分析、銘柄分析、定性分析、レポート生成、swing daily,
  daily run, run the pipeline.
---

# 日次分析ワークフロー（統括）

機械処理は Python、定性判断はこのスキル、最終判断は人間。
決定論的な出力（スコア・ランキング・リスク制約）は**絶対に書き換えない**。

作業前に必ず読む:

- [references/analysis-conventions.md](references/analysis-conventions.md) — AC1〜AC15 の共通規約（CON-03・provenance・叙述）
- [references/output-schema.md](references/output-schema.md) — 入出力 JSON スキーマと `analysis_work/` 断片の形式
- `src/swing_copilot/analysis/schemas.py` — **スキーマの最終正本**。JSON を組み立てる前に必ず読む

## Inputs

`analysis_input.json` があるディレクトリを `<WORKDIR>` と呼ぶ（当日のレポート出力先）。

- `<WORKDIR>/analysis_input.json` — 必須。`copilot-daily` が生成。読み取り専用
- `<WORKDIR>/analysis_work/news-<SYMBOL>.json` — 任意。専門家の出力断片。Step 3 でマージ
- `<WORKDIR>/analysis_work/filings-<SYMBOL>.json` — 任意。同上
- `<WORKDIR>/analysis_work/screening-<SYMBOL>.json` — 任意。同上
- `<WORKDIR>/analysis_result.json` — 任意。存在すれば再入とみなす（Step 0）
- `references/analysis-conventions.md`, `references/output-schema.md`,
  `src/swing_copilot/analysis/schemas.py`

## Outputs

- `<WORKDIR>/analysis_result.json` — このスキルが直接書く唯一の成果物
- 当日の Markdown レポート — `copilot-ingest-analysis` 経由で再描画される（間接出力）
- `<WORKDIR>/analysis_work/**` — 専門家サブエージェントが書く。統括は読み取りと
  古い断片の削除のみ行い、内容を書き換えない

## Step 0: 既存アーティファクト確認（Phase 0・冪等な再入）

`<WORKDIR>` が既に分かっている場合（同一セッションでの再実行、ユーザーがパスを
指定した場合）はここで確認する。分からない場合は **Step 1 の直後に同じ確認**を行う。

1. `<WORKDIR>/analysis_result.json` が存在する場合:
   - ユーザーが明示的に再実行・やり直しを求めていなければ **上書きしない**。
     既存の verdict を要約して報告し、再実行するか確認する
   - 再実行を求められている場合のみ、以降のステップで上書きしてよい
2. `<WORKDIR>/analysis_work/` が存在する場合、各断片の `run_id`、`as_of`、`input_digest` を読む:
   - 3値すべてが `analysis_input.json` と**一致**し、JSON として妥当で、
     ペイロードキーがある → その銘柄 × 専門家は **再分析せず流用**する
   - いずれかが**不一致**（別run・前日以前の残骸）→ そのファイルを削除して再分析対象にする
   - JSON として壊れている、ペイロードキーが無い、`symbol` がファイル名と
     食い違う → 削除して再分析対象にする
3. `analysis_input.json` の `candidates[].symbol` に無い銘柄の断片は削除する。
4. 流用した断片は Step 3 でそのままマージ対象に含める（要約は断片内の
   `ac_check` と本文から統括が読み取る）。ただし `facts[].evidence_quote` が
   欠落している、または引用元本文と一致しない断片は ingest の provenance 検査で
   その銘柄ごと fail-closed になる。3 値が一致していても `evidence_quote` を
   欠いた古い断片（本契約変更前に生成されたもの等）は流用せず再分析させる。

## Step 1: パイプライン実行

```bash
uv run copilot-daily <ユーザー指定の引数>
```

引数（対象日、dry-run/live 等）はユーザーの指示に従う。指定が無ければ引数なしで実行。

ターミナル出力から **`analysis_input.json` の絶対パス**を拾う。

- 候補ゼロ、または `analysis_input.json` がエクスポートされなかった場合は、
  そこで終了し「本日は分析対象なし」とパイプラインの要約を報告する。Step 2 以降に進まない。

`analysis_input.json` から、本文を除いたメタデータ投影だけを読み、`run_id`、`as_of`、
`strategy_key`、`input_digest`、`candidates[].symbol`、各sourceの文字数と
`filings[].coverage`を控える。親セッションのツール出力へ`news[].summary`や
`filings[].text`を表示してはならない。4値は後段で result に逐語転記する
（不一致は hard fail）。

`<WORKDIR>` が Step 0 の時点で不明だった場合は、ここで **Step 0 の確認を実施**する。

## Step 2: 専門家サブエージェントの並列起動（1 段目）

Step 0 で流用が決まった組を除いた、残りの「銘柄 × 専門家」の組を洗い出す。

| 専門家 | 参照スキル | 担当 | 対象銘柄 |
|---|---|---|---|
| ニュース分析 | `.claude/skills/analyze-news/SKILL.md` | `candidates[].news` | `news` が非空の銘柄 |
| 開示分析 | `.claude/skills/analyze-filings/SKILL.md` | `candidates[].filings` | `filings` が非空の銘柄 |
| スクリーニング定性評価 | `.claude/skills/interpret-screening/SKILL.md` | `score_breakdown` / `risk_constraints` / `context` | **全銘柄** |

### 実行手段のトリアージ

残った組の数を N とする。

- **N > 9**: **Workflow ツール（Dynamic Workflow）での fan-out を第一候補**にする。
  決定論的な分岐・レジューム・進捗可視化・トークン予算連動が効くため、
  組の数が多いときは Agent ツールの手動列挙より確実。**ただし、Workflow の利用が
  明示的に許可されている場合に限る**。許可が無い、または利用できない場合は、
  Agent ツールで下記と同じ指示を渡し、1 エージェントに同一専門家の複数銘柄を
  割り当てて並列起動する
- **N <= 9**: Agent ツール（`model: sonnet`）を **同一メッセージ内で並列に**起動する。
  1 エージェント = 1 専門家 × 数銘柄。同じ専門家を銘柄分割して複数並列起動してよい
- どちらの手段でも **各エージェントへの指示内容は同一**（下記）。手段の違いが
  分析内容の違いになってはならない

### サブエージェントへ渡す入力範囲

`analysis_input.json` の全件をサブエージェントのメッセージに貼り付けない。親は、
担当する専門家と銘柄に必要な入力だけを含む**読み取り専用の入力スライス**を作成し、
その絶対パスを渡す。スライスの形式・不変条件は
[references/output-schema.md](references/output-schema.md) の「サブエージェント入力スライス」
に従う。

- ニュース／開示専門家には、担当銘柄の該当テキストと必要なメタデータだけを渡す。
  スクリーニング専門家には、担当銘柄の決定論的入力と必要な run-wide context だけを渡す
- 切り出した文字列と `source_id` は元の入力から逐語コピーする。要約・ID の再採番・
  他銘柄のテキスト混入をしない
- 元の `analysis_input.json` の絶対パスも併記するが、サブエージェントは metadata の
  照合以外で全件を読み込まない。これにより、長大な開示本文を担当外銘柄ごとに
  重複してコンテキストへ載せない
- 開示担当は1エージェントあたりの担当銘柄について、`filings[].text`の合計を
  **240,000文字以下**にする。240,000文字に近い銘柄は単独担当とし、複数銘柄を
  詰め込まない。並列枠を超える場合は波に分ける。1銘柄を複数エージェントへ分割して
  マージしない
- この240,000文字はモデルのコンテキスト上限ではなく、安全余白を残す運用上限である。
  スキル規約・決定論的文脈・出力・追加レビューの余白を確保し、モデルの公称上限まで
  本文で埋めない

### 各エージェントへの指示に必ず含めるもの

1. `.claude/skills/<name>/SKILL.md` を読み、それに従うこと
2. `analysis_input.json` と入力スライスの**絶対パス**、担当する銘柄シンボルの列挙。
   スライスを分析に使い、元入力の `run_id` / `as_of` / `input_digest` と一致することを
   確認すること
3. 出力は `<WORKDIR>/analysis_work/<kind>-<SYMBOL>.json` への**ファイル書き出し**
   （`<kind>` は `news` / `filings` / `screening`、1 銘柄 1 ファイル）
4. 親に返すのは **銘柄ごと 1〜2 行の要約 + 特記事項 + AC 自己点検結果**だけ。
   JSON 全文・生の入力テキストをメッセージに載せないこと

## Step 3: 断片のマージ

`<WORKDIR>/analysis_work/` を列挙し、同一の`run_id`・`as_of`・`input_digest`を持つ断片をすべて読む
（Step 0 で流用した断片を含む）。

- 期待する組がすべて揃っているか確認する。欠けている組があれば、その専門家を
  再起動する（メッセージで結果を送り直させない。必ずファイルに書かせる）
- 各断片から**ペイロードキーだけ**を取り出し、銘柄ごとに `news_summary` /
  `filing_analyses` / `screening_assessment` を組み立てる
- `as_of` / `ac_check` は作業用メタデータなので**捨てる**（strict 検証で hard fail する）。
  一方 `facts[].evidence_quote` は作業用メタデータではなく契約フィールドなので、
  断片の値をそのまま逐語で運ぶ（落とすと ingest でその銘柄が fail-closed になる）
- 断片の本文は書き換えない。問題があれば Step 3.5 で再分析を依頼する
- news が空の銘柄は `news_summary: null`、filings が空の銘柄は `filing_analyses: []`。
  `screening_assessment` は全銘柄必須

## Step 3.5: 統合レビュー（2〜3 段目、セッション本体で実施）

マージした内容を突き合わせ、以下を点検する。括弧内は
[references/analysis-conventions.md](references/analysis-conventions.md) の AC 番号。

- **矛盾**: ニュースの示唆と開示の示唆が食い違っていないか
- **見落とし**: スクリーニングの強みが、ニュース／開示の懸念で打ち消されていないか
- **provenance 破れ**: `facts[].source_ids` が非空か（AC6・AC10）、入力の該当銘柄に
  実在する ID の部分集合か（AC6）、逐語コピーか（AC7）、複数ソースを取りこぼして
  いないか（AC9）、`facts[].evidence_quote` がその `source_ids` の本文（ニュースは
  見出し＋要約、開示は入力の `text`、カレンダーイベントはタイトル＋要約）からの
  逐語引用になっているか（AC6）。この一致は `analysis/validate.py` が正規化した
  うえで機械的に照合するため、統括のレビューは「別銘柄の本文からの取り違えが
  無いか」の見立てであり、最終判定は ingest 側が行う
- **入力外情報**: 入力に無い企業情報・株価・決算数値が混ざっていないか（AC8）、
  決定論的スコアを書き換えていないか（AC1・AC2）
- **CON-03**: 断定的売買指示（AC3）・命令形（AC4）・根拠なき心理診断（AC5）
- **叙述規約**: facts に評価語や推論が混ざっていないか（AC11）、hedge があるか（AC12）、
  非網羅である旨が書かれているか（AC13）、空入力を無理に埋めていないか（AC14）
- 各専門家が返した `ac_check` に懸念 AC 番号が挙がっていれば、その項目を優先的に見る

深掘りが要る点があれば、該当専門家に**追加分析を依頼する**（2 段目、必要なら 3 段目）。
追加分析には `model: opus` を指定してよい。追加依頼では「違反または矛盾している AC 番号」と
「入力のどの source_id を見直すか」を具体的に書き、**同じ断片ファイルを上書きさせる**。

統合レビュー自体はセッション本体で行う（サブエージェントに丸投げしない）。
文言を自分で書き換えて検査を通そうとしない（AC15）。

## Step 4: verdict 決定

銘柄ごとに `proceed` / `skip` を決める。

- `skip`（見送り推奨）にする典型: 開示に新規の重大リスク記載がある、ニュースが
  スクリーニングの前提を崩している、専門家間で示唆が矛盾し解消できない、
  イベント（決算・訴訟・規制）が直近に控える
- `proceed`: 定性情報の範囲で追加の懸念が見当たらない

判断規範:

- スクリーニングの決定論的結果は**改変しない**。verdict は定性情報による「推奨」であり、
  スコアや順位を否定するものではない
- `reasons[].text` には根拠を書き、ニュース／開示由来なら該当 `source_ids` を必ず引用。
  スコア等の決定論的入力のみに基づく理由は `source_ids: []` でよい
- `context.calendar_events`（マクロ／経済カレンダーイベント）も verdict 理由の根拠に
  できる。引用する場合は該当イベントの `source_id` を `reasons[].source_ids` に含める
  （run単位の文脈のため、全銘柄が共通して引用可）
- **verdict を確定する前に provenance を検証する。** 各 `reasons[].source_ids` について、
  空でなければ、該当銘柄の `news` / `filings` の ID または
  `context.calendar_events` の ID に実在する完全一致の部分集合であることを、入力を見て
  1 件ずつ確認する。ID を推測・生成・整形しない（AC6・AC7・AC10）。根拠がテキスト由来なら
  空リストにせず、確認できない ID は書かない
- 全銘柄が `skip`、または市場環境（`context.market_regime`）から当日の新規エントリーを
  推奨しないと判断した場合は `no_trade: true` とし、`no_trade_reason` に理由を書く
- 最終判断は人間。verdict は指示ではなく推奨として書く（命令形・断定的売買指示は禁止）

## Step 5: analysis_result.json の書き出し

`src/swing_copilot/analysis/schemas.py` を読んで最新のフィールド名を確認したうえで、
[references/output-schema.md](references/output-schema.md) の形で JSON を組み立てる。

- `run_id`、`as_of`、`strategy_key`、`input_digest`は input から**逐語コピー**する
- `schema_version` は `analysis-result-v3`
- `screening_assessment` と `verdict` は**全銘柄必須**
- `symbols[].symbol` は重複させず、input の `candidates[].symbol` と**完全一致**させる
- `no_trade: true` なら非空白の `no_trade_reason` を書き、`false` なら必ず `null` にする
- 出力先は `<WORKDIR>/analysis_result.json`（`analysis_input.json` と同じディレクトリ）
- input に無い symbol を追加しない。input にある symbol を落とさない
- `analysis_work/` 由来の`run_id` / `as_of` / `input_digest` / `ac_check`を result へ持ち込まない（未知フィールドは hard fail）
- 書き出し直前に、全 `verdict.reasons[].source_ids` をもう一度走査する。空でない各 ID は、
  その symbol の入力 `news` / `filings` または run-wide `context.calendar_events` に
  実在する完全一致の ID でなければならない。テキスト由来の理由に空リストを使わず、
  決定論的入力だけの理由に限り空リストを許可する

書き出したら JSON として妥当かを確認する。

```bash
uv run python -c "import json,sys;json.load(open(sys.argv[1]))" <analysis_result.json の絶対パス>
```

## Step 6: ingest 実行

```bash
uv run copilot-ingest-analysis <analysis_result.json の絶対パス>
# 例: uv run copilot-ingest-analysis /path/to/<WORKDIR>/analysis_result.json
```

`<result.json|dir>` を第1引数に取り、`--input` / `--context` / `--log-level` を
任意で指定できる（省略時は `analysis_input.json` / `report_context.json` を
result ファイルと同じディレクトリから解決する）。hard fail 時は exit code 1。
正確な引数は `uv run copilot-ingest-analysis --help` で確認する。

**検証で縮退（degraded）や error が出た場合:**

- **リトライで検証を通そうとしない。** fail-closed が仕様であり、
  文言を書き換えて再投入するのは規約違反にあたる（AC15）
- 縮退した銘柄と、ログに出た違反理由（CON-03 違反か provenance 違反か
  スキーマ不一致か）をそのまま報告する
- スキーマ不一致による hard fail のみ、`schemas.py` を読み直して
  **フィールド名の誤りを修正**して再実行してよい（内容の書き換えではないため）

## Step 7: 報告

ユーザーに簡潔に報告する。

- パイプラインの結果（候補数、対象日）
- 銘柄ごとの verdict（proceed / skip と一行理由）
- `no_trade` の有無と理由
- ingest の検証結果（全件通過 / 縮退した銘柄と理由）
- 最終レポートのパス
- Step 0 で既存断片を流用した場合はその旨（流用した銘柄 × 専門家の組）

最終判断は人間である旨を添える。

## 禁止事項

- スクリーニングのスコア・ランキング・リスク制約の再計算や書き換え（AC1）
- 入力に無い情報の補完（事前知識による企業情報・株価・決算数値の追加）（AC8）
- 検証を通すための文言の書き換え・再投入（AC15）
- 断定的売買指示、命令形、根拠なき心理・行動診断（AC3〜AC5）
- 専門家が書いた `analysis_work/` 断片の本文を統括が直接書き換えること
  （修正が要るなら該当専門家に再分析を依頼し、断片を上書きさせる）
- ユーザーが再実行を求めていないのに既存の `analysis_result.json` を上書きすること
- `src/`, `tests/`, `docs/` の編集（このスキルは分析実行であり実装変更ではない）
