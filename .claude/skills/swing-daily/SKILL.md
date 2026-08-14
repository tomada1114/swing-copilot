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

- [references/analysis-conventions.md](references/analysis-conventions.md) — AC1〜AC16 の共通規約（CON-03・provenance・叙述・数値整合）
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
- `<WORKDIR>/analysis_work/**` — 専門家サブエージェントが書く。統括は読み取りのみ行い、
  内容を書き換えない。陳腐化した断片は削除せず、上書き・無視で処理する（Step 0）
- `<WORKDIR>/headless_note.md` — 無人実行時のみの人間向けメモ（「無人実行（headless）
  時の方針」を参照）。契約されたアーティファクトではなく、ingest は読まない

## 一時ファイルと後始末（統括・全サブエージェント共通）

このワークフローは平日定時に**無人起動**される。`rm` は許可リストに無いため、
1 回でも実行しようとすると承認待ちで実行全体が停止する（実測で 44 分停止した例がある）。
したがって次を全員が守る。

- 作業用の一時ファイル（入力スライス、抽出テキスト等）は
  **セッションの scratchpad ディレクトリ配下にだけ**作る。`<WORKDIR>` 直下や
  リポジトリ配下に作らない（`analysis_work/` に置かないのは従来どおり）
- **契約検証のスクリプトは統括もサブエージェントも書かない。** 断片と
  `analysis_result.json` の検証は `uv run copilot-verify-analysis` が担う
  （Step 3 / Step 5）。同じ検査を各自が実装し直すと、実装のばらつきがそのまま
  検査水準のばらつきになる（Issue #132）
- **一時ファイルを削除しない。`rm` を実行しない。** scratchpad はセッション終了時に
  破棄されるため掃除は不要で、掃除の実行コストの方が高い
- 掃除が必要になるのは、リポジトリ配下に一時ファイルを作ってしまった場合だけである。
  そもそも作らないことで、この分岐自体を無くす

## Step 0: 既存アーティファクト確認（Phase 0・冪等な再入）

`<WORKDIR>` が既に分かっている場合（同一セッションでの再実行、ユーザーがパスを
指定した場合）はここで確認する。分からない場合は **Step 1 の直後に同じ確認**を行う。

1. 次のいずれかが存在する場合、既存アーティファクトとみなす:
   - `<WORKDIR>/analysis_result.json`
   - `reports/<as_of>/*/analysis_result.json`（glob。`<WORKDIR>` が未確定でも
     対象日 `as_of` さえ分かれば確認できる。#118 の同日重複起動ガードが
     `run_id` 発行前に判定する`run_date`単位の重複と同じ粒度で見るための追加確認）
   - ユーザーが明示的に再実行・やり直しを求めていなければ **上書きしない**。
     既存の verdict を要約して報告し、再実行するか確認する
   - 再実行を求められている場合のみ、以降のステップで上書きしてよい（Step 1 の
     `uv run copilot-daily` に `--allow-same-day-rerun` を付けて実行する）
2. `<WORKDIR>/analysis_work/` が存在する場合、各断片の `run_id`、`as_of`、`input_digest` を読む:
   - 3値すべてが `analysis_input.json` と**一致**し、JSON として妥当で、
     ペイロードキーがある → その銘柄 × 専門家は **再分析せず流用**する
   - いずれかが**不一致**（別run・前日以前の残骸）→ 流用せず再分析対象にし、
     担当専門家に**同じパスを上書き**させる
   - JSON として壊れている、ペイロードキーが無い、`symbol` がファイル名と
     食い違う → 同じく再分析対象にし、同じパスを上書きさせる
   - **陳腐化した断片を `rm` で消さない。** 上書きで同じ結果になり、削除は
     「一時ファイルと後始末」の禁止事項に当たる
   - この 3 値の照合と JSON・ペイロードキーの妥当性は
     `uv run copilot-verify-analysis <WORKDIR>/analysis_work` が機械的に行う。
     **自前の照合スクリプトを書かない**（Step 3 の「断片の機械検証」参照）
3. `analysis_input.json` の `candidates[].symbol` に無い銘柄の断片は、削除せず
   **無視する**（Step 3 のマージ対象から外す）。
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

**終了コード 2（preflight abort）を再入シグナルとして扱う（#118）。** これは
同一 `run_date` に対して成功済みの run が既にあることを意味する（同日重複起動
ガード。`run_date` は最新 bar 由来でプリフェッチ後にしか確定しないため、Step 0
の事前確認をすり抜けることがある）。この場合:

1. stderr のメッセージから既存の `run_id` とレポートパスを読み取る
2. 既存レポート（`reports/<as_of>/<run_id>.md`）または
   `uv run copilot-history run --run-id <run_id>` を読み、既存 verdict を要約する
3. 「本日は既に分析済み」として上記要約とともに正常終了する。
   `analysis_result.json` は書かない。Step 2 以降に進まない
4. ユーザーが明示的に再実行を求めている場合のみ、`--allow-same-day-rerun` を
   付けて Step 1 を再実行し、通常どおり続行する

終了コード 0/1 は従来どおり続行する。

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

### 実行手段: Agent ツール並列が標準

**標準経路は Agent ツール（`model: sonnet`）の並列起動である。** 平日定時の無人実行
（`CLAUDE.md` の "Scheduled Daily Run"）を含む headless 実行では Workflow ツールの
利用が明示的に許可されないため、組の数にかかわらず常に Agent 経路を使う。
**実行のたびに手段を選び直さない。** 候補数は `candidate_limit` に律速されて日々
似た規模になるため、毎回トリアージし直しても結論は変わらず、判断と説明のコストだけが
積み上がる。

残った組の数を N とし、次の方針で N 組をエージェントへ割り当てる。

- **1 エージェント = 1 専門家 × 数銘柄。** 同じ専門家を銘柄で分割して複数エージェントに
  割り当ててよい。逆に、1 銘柄 × 1 専門家という 1 組を複数エージェントへ分割して
  結果をマージすることはしない
- **開示分析は既定で 1 銘柄 1 エージェント。** `filings[].text` は長く、1 エージェント
  あたりの合計文字数上限（下記「サブエージェントへ渡す入力範囲」）にすぐ届くため。
  まとめてよいのは、合計がその上限に十分収まる短い銘柄同士に限る
- **ニュース分析・スクリーニング定性評価は 1 エージェントあたり 3 銘柄程度**を目安に
  まとめる。担当テキストが短ければ増やしてよい
- 起動は **同一メッセージ内で並列に**行う。並列枠に収まらない場合は波に分け、
  波をまたいでも各エージェントへの指示内容は変えない

**Workflow ツールは対話セッション限定の任意手段。**
Workflow ツール（Dynamic Workflow）での fan-out は、決定論的な分岐・レジューム・
進捗可視化・トークン予算連動が効くため、組の数が多いときは上位互換になりうる。
ただしこれは標準経路ではなく、次の**両方**を満たすときにだけ選べる任意の代替手段である。

1. 対話セッションであり、かつ Workflow の利用が**明示的に許可**されている
2. N > 9（これ以下の規模なら Agent 並列で十分で、切り替える利点が無い）

どちらか一方でも欠けるなら、可否を検討せずそのまま Agent 並列で進める。Workflow を
使う場合も **各エージェントへ渡す指示内容と銘柄割り当て方針は Agent 経路と同一**
（上記および下記）。手段の違いが分析内容の違いになってはならない。

### サブエージェントへ渡す入力範囲

`analysis_input.json` の全件をサブエージェントのメッセージに貼り付けない。親は、
担当する専門家と銘柄に必要な入力だけを含む**読み取り専用の入力スライス**を作成し、
その絶対パスを渡す。スライスは**セッションの scratchpad ディレクトリ配下**に置き、
`<WORKDIR>` やリポジトリ配下には置かない（「一時ファイルと後始末」参照）。
スライスの形式・不変条件は
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
5. 作業用の一時ファイルは scratchpad ディレクトリ配下にだけ作り、**削除しないこと
   （`rm` を実行しないこと）**。無人実行では `rm` の承認待ちでワークフロー全体が
   停止する（「一時ファイルと後始末」参照）
6. 書き出した断片を `uv run copilot-verify-analysis <断片の絶対パス>` で検証し、
   **契約検証のスクリプトを自作しないこと**。合否と FAIL 理由を親への要約に含めること

## Step 3: 断片のマージ

`<WORKDIR>/analysis_work/` を列挙し、同一の`run_id`・`as_of`・`input_digest`を持つ断片をすべて読む
（Step 0 で流用した断片を含む）。

- 期待する組がすべて揃っているか確認する。欠けている組があれば、その専門家を
  再起動する（メッセージで結果を送り直させない。必ずファイルに書かせる）
- 3値が不一致の断片、および `candidates[].symbol` に無い銘柄の断片は**読み飛ばす**
  （Step 0 で削除していないため、ディレクトリには残っている前提で扱う）
- 各断片から**ペイロードキーだけ**を取り出し、銘柄ごとに `news_summary` /
  `filing_analyses` / `screening_assessment` を組み立てる
- `as_of` / `ac_check` は作業用メタデータなので**捨てる**（strict 検証で hard fail する）。
  一方 `facts[].evidence_quote` は作業用メタデータではなく契約フィールドなので、
  断片の値をそのまま逐語で運ぶ（落とすと ingest でその銘柄が fail-closed になる）
- 断片の本文は書き換えない。問題があれば Step 3.5 で再分析を依頼する
- news が空の銘柄は `news_summary: null`、filings が空の銘柄は `filing_analyses: []`。
  `screening_assessment` は全銘柄必須

### 断片の機械検証（自前実装しない）

断片を読んだら、マージ内容を確定する前に、まとめて機械検証する。

```bash
uv run copilot-verify-analysis <WORKDIR>/analysis_work
```

- ingest（`copilot-ingest-analysis`）と**同一の関数**で、断片の strict schema、
  `run_id` / `as_of` / `input_digest` の一致、ファイル名とペイロードの一致、
  provenance、`evidence_quote` の逐語一致、CON-03 を検査する。契約と終了コードは
  [references/output-schema.md](references/output-schema.md) の「断片の契約検証」参照
- **同じ検査を自前のスクリプトで書き直さない。** 逐語一致と CON-03 は Unicode NFKC
  正規化を経て判定されるため、grep ベースの自己検査は ingest より弱くなる（Issue #132）
- FAIL した断片は、統括が本文を書き換えるのではなく Step 3.5 の手順で該当専門家に
  再分析を依頼して**同じパスを上書き**させる（AC15）
- 3 値が不一致の断片（別 run・前日以前の残骸）と、入力に無い銘柄の断片も FAIL として
  出るが、扱いは Step 0 のとおりに分かれる。前者は流用せず**再分析対象**にし、
  後者はマージ対象から外して**無視する**。どちらも `rm` で消さない

## Step 3.5: 統合レビュー（2〜3 段目、セッション本体で実施）

マージした内容を突き合わせ、以下を点検する。括弧内は
[references/analysis-conventions.md](references/analysis-conventions.md) の AC 番号。

- **矛盾**: ニュースの示唆と開示の示唆が食い違っていないか
- **見落とし**: スクリーニングの強みが、ニュース／開示の懸念で打ち消されていないか
- **provenance 破れ**: `facts[].source_ids` が非空か（AC6・AC10）、入力の該当銘柄に
  実在する ID の部分集合か（AC6）、逐語コピーか（AC7）、複数ソースを取りこぼして
  いないか（AC9）、`facts[].evidence_quote` がその `source_ids` の本文（ニュースは
  見出し＋要約、開示は入力の `text`、カレンダーイベントはタイトル＋要約）からの
  逐語引用になっているか（AC6）。ID の部分集合性と引用の逐語一致は Step 3 の
  `copilot-verify-analysis` が `analysis/validate.py` の関数で照合済みなので、
  **統括はこの照合を書き直さない**。ここで見るのは機械が見られない部分——
  複数ソースの取りこぼし（AC9）と、正しい ID を申告しつつ内容が別銘柄の
  取り違えになっていないかの見立て——に絞る
- **数値の桁**: `facts[].text` の数値が `evidence_quote` の数値と桁まで整合しているか
  （AC16）。開示の表は多くが `(in thousands)` で、`3,495,296` は 34億9,530万ドルである。
  引用が正しくても変換だけを誤った fact は逐語一致の検査を素通りするため、**単位変換を
  跨ぐ数値は 1 件ずつ引用と突き合わせる**。前年同期・前四半期を並べた fact は各行を
  個別に確認する（1 行だけ誤る事故が実際に起きている）。ingest 側の機械検査は単位・
  通貨の付いた数値だけを対象にした**警告**であり、fail-closed ではない。警告が出て
  いないことは正しさの証明にならない
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

## Step 3.6: 反証パス（adversarial review）

Step 3.5 を終えた時点で **暫定的に `proceed` に傾いている銘柄だけ**を対象に、
反証（bear case）を立てさせる。

- 暫定 `proceed` 候補がゼロなら、このパスは**丸ごとスキップ**する（コストガード）。
  スキップした旨は Step 7 で報告する
- 対象銘柄ごとに Agent ツール（`model: opus`）で 1 体を起動する。
  **同一メッセージ内で並列起動**してよい

### 反証エージェントへ渡す入力

- 当該銘柄の `<WORKDIR>/analysis_work/news-<SYMBOL>.json` /
  `filings-<SYMBOL>.json` / `screening-<SYMBOL>.json` の**絶対パス**（存在するものだけ）
- `analysis_input.json` の当該銘柄スライス（`score_breakdown` / `risk_constraints` /
  `news` / `filings` と run-wide `context`）の**絶対パス**。スライスの作り方と
  不変条件は Step 2 と同じ（逐語コピー・他銘柄の混入禁止）
- 「**供給されたデータのみ**から結論を出すこと。新規のデータ取得・Web 検索・
  事前知識による補完をしないこと」（AC8）
- 「作業用の一時ファイルは scratchpad ディレクトリ配下にだけ作り、**削除しないこと
  （`rm` を実行しないこと）**」（「一時ファイルと後始末」参照）

### 反証エージェントのタスク

供給データの範囲で**最も強い bear case** を組み立て、次の 3 点を返す。

1. `proceed` を支えるポジティブ材料のうち、**出典が弱いもの・単一ソースにしか
   支えられていないもの**の指摘（該当する `source_id` を明示する）
2. **見落とされているリスク**の指摘（断片間の矛盾、開示本文に記載されたリスク、
   直近に控えるイベント、リスク制約との緊張など）
3. 「**この反証を覆すには何が必要か**」の明示（入力のどこを見直せば、
   何が確認できれば反証が弱まるのか）

**リスク要因（Item 1A）の新規性は論点として立てない**（Issue #127）。10-Q の Item 1A は
前回提出から重要な変更が無ければ 10-K への参照援用だけで済ませてよく、比較対象となる
10-K 本文は入力に含まれない。よって新規性は現在の入力範囲では判定不能として扱い、
反証側でも判定しない。「新規記載があるか」「文言が強まったか」を検査項目にせず、
「判定不能」「比較対象が無い」といった文言も反証ポイントとして書かない。開示に実質本文が
あれば、新規性ではなく**そこに書かれているリスクの内容そのもの**を bear case の材料にする。
いずれの場合も「新規リスクなし」「リスクは変化していない」とは書かない（AC8）。

- 返答は**要約のみ**。ファイルを書かせない。反証パスの出力は `analysis_work/` の
  断片契約に含めず、`analysis_work/` にも `analysis_result.json` にも置かない
- 反証側も CON-03 の言語規律に従う（断定的売買指示・命令形を使わない。AC3〜AC5）

### セッション本体の応答義務

返ってきた反証ポイントに、**1 件ずつ明示的に応答**してから Step 4 の verdict を確定する。

- 各ポイントについて「棄却（その理由）」か「受容（verdict へどう反映したか）」の
  どちらかを決める。黙って無視しない
- 実質的な矛盾が解消できない場合は**保守側（`skip`）に倒す**
- 反証を受けて断片そのものに修正が要ると判明した場合は、統括が本文を書き換えず、
  Step 3.5 の手順で該当専門家に再分析を依頼して断片を上書きさせる

## Step 3.7: 対称レビュー（材料不在型 skip の検査）

Step 3.6 の反証パスは**暫定的に `proceed` に傾いている銘柄だけ**を追加検査する。
`skip` はこの検査を一度も受けず、「実質的な矛盾が解消できない場合は保守側（`skip`）に
倒す」規範（Step 3.6・Step 4）と重なって、`proceed` だけが追加の1段をくぐらなければ
生き残れない非対称なラチェット構造になる。このステップは**保守側に倒す基本方針は
変えず**、`skip` の理由が「材料があること」ではなく「材料が見当たらないこと」で
構成されている銘柄に限り、対称の検査を当てる。

### 材料不在型 skip の判定規則

暫定 `verdict.reasons` のうち、次の (a) または (b) に該当する reason が**過半（半数超）**を
占める銘柄が対象になる。ちょうど半数（例: 4件中2件）は過半ではないため対象外。
reasonsが1件のみでそれが(a)か(b)なら、1/1は過半として対象になる。

- (a) **当該事実を確認するための本文・章・フォームが入力に存在しないことを述べる**
  reason: 章が入力に無い、coverage が partial で中間が未分析、当該フォーム自体が
  入力に含まれない、固有材料が限定的で確認できない、判定不能。
  **含めない**もの: 入力に存在する事実に対する解釈上の注意喚起（例: 「+75.6%は
  買収の連結効果を含む可能性があり区別して読む必要がある」は、買収完了日が入力から
  読み取れるため (a) ではない）
- (b) `source_ids` が空で、決定論的制約だけを述べる定型 reason: `not_calculable` /
  `shares` 不明 / `Exposure Ceiling: REDUCE_ONLY`

対象は該当銘柄のうち**`score_breakdown` の総合スコア上位3銘柄まで**。0件ならこの
パスは**丸ごとスキップ**する（Step 3.6 と同じコストガード）。スキップした旨は
Step 7 で報告する。4件以上該当する場合は上位3銘柄だけを対象にし、見送った銘柄と
その件数を Step 7 で報告する。

### 対称エージェントへ渡す入力

Step 3.6 の反証エージェントと**同一の入力契約**を使う: 当該銘柄の
`<WORKDIR>/analysis_work/news-<SYMBOL>.json` / `filings-<SYMBOL>.json` /
`screening-<SYMBOL>.json` の絶対パス（存在するものだけ）、`analysis_input.json` の
当該銘柄スライスの絶対パス（逐語コピー・他銘柄の混入禁止）、「供給されたデータのみ
から結論を出すこと。新規のデータ取得・Web検索・事前知識による補完をしないこと」
（AC8）、一時ファイルは scratchpad 配下にのみ作り `rm` しないこと。

- 対象銘柄ごとに Agent ツール（`model: opus`）で1体を起動する。暫定 `proceed` 候補が
  同時に存在し Step 3.6 も実施する場合、**同一メッセージ内で両パスを並列起動**して
  よい（Boundary: 両方ある場合、既存の各パスの上限枠内で同時に走らせる）

### 対称エージェントのタスク

供給データの範囲で**最も強い bull case** を組み立て、次の3点を返す。

1. その skip 理由のうち「**材料の不在**」に依存している箇所の特定
2. 供給データ内に**存在するが暫定判断に反映されていない肯定材料**の指摘（該当する
   `source_id` を明示する）
3. 「**この skip を維持するには何が確認できれば足りるか**」の明示

- 返答は**要約のみ**。ファイルを書かせない。対称パスの出力は Step 3.6 の反証パスと
  同じく `analysis_work/` の断片契約に含めず、`analysis_work/` にも
  `analysis_result.json` にも置かない
- 対称エージェントも CON-03 の言語規律に従う（断定的売買指示・命令形を使わない。AC3〜AC5）

### セッション本体の応答義務

返ってきた3点に、**1件ずつ明示的に応答**してから Step 4 の verdict を確定する
（Step 3.6 と同じ形式）。

- 各点について「棄却（その理由）」か「受容（verdict へどう反映したか）」のどちらかを
  決める。黙って無視しない
- **実質的な矛盾が解消できない場合は保守側（`skip`）に倒す**（Step 3.6・Step 4・
  無人実行時の方針と同じ規範。判断が割れても `skip` を維持する）
- 応答の結果 `proceed` が妥当と判断された場合は、確定前に Step 3.6 の反証パスを
  通す（対称パスを通過しただけで反証を免除しない）
- `skip` を維持する場合、`reasons[].text` の少なくとも1件に、この skip が
  「確認できる材料が入力に無いことに基づく見送りであり、特定されたリスクに基づく
  ものではない」旨を明記する

## Step 4: verdict 決定

銘柄ごとに `proceed` / `skip` を決める。Step 3.6 / Step 3.7 を実施した銘柄は、
それぞれの検査ポイントへの応答を済ませてから確定する。

- `skip`（見送り推奨）にする典型: 開示本文に重大なリスク記載がある（記載の**新規性**は
  Step 3.6 と同じく論点にしない。Issue #127）、ニュースが
  スクリーニングの前提を崩している、専門家間で示唆が矛盾し解消できない、
  イベント（決算・訴訟・規制）が直近に控える
- `proceed`: 定性情報の範囲で追加の懸念が見当たらない

判断規範:

- スクリーニングの決定論的結果は**改変しない**。verdict は定性情報による「推奨」であり、
  スコアや順位を否定するものではない
- `reasons[].text` には根拠を書き、ニュース／開示由来なら該当 `source_ids` を必ず引用。
  スコア等の決定論的入力のみに基づく理由は `source_ids: []` でよい
- Step 3.6 / Step 3.7 で検討した内容を `reasons[].text` に反映してよい。ただし根拠が
  定性テキスト由来なら該当 `source_ids` を必ず付け（AC6・AC7・AC10）、入力に無い情報を
  足さない（AC8）。CON-03 の言語規律（断定的売買指示・命令形の禁止、hedge 表現）は
  従来どおり厳守する（AC3〜AC5・AC12）
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

書き出したら、ingest を走らせる前に dry-run で検証する（**自前の検証スクリプトを
書かない**）。

```bash
uv run copilot-verify-analysis <analysis_result.json の絶対パス>
```

- JSON の妥当性、strict schema、input との identity 一致（`run_id` / `as_of` /
  `strategy_key` / `input_digest`）、symbol 集合の完全一致、provenance、
  `evidence_quote` の逐語一致、CON-03 を、**ingest と同一の関数**で検査する。
  レポートは書かない
- 終了コード `0` なら Step 6 へ進む。`1` の場合、FAIL 行が縮退または hard fail の
  理由を示す。**文言を書き換えて検査を通そうとしない（AC15）。** 断片に修正が要るなら
  Step 3.5 の手順で該当専門家に再分析させ、Step 3 からやり直す
- マージ時のフィールド名の取り違え（`ac_check` の混入、`evidence_quote` の落とし）は
  ここで hard fail として出る。これは内容の書き換えではないので直して再実行してよい

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
- Step 3.6 の反証パスを実施した銘柄と、反証ポイントを棄却／受容した結論。
  暫定 `proceed` がゼロでパスをスキップした場合はその旨
- Step 3.7 の対称パスの対象数と、そのうち何件の verdict が変わったか。材料不在型
  skip が0件でパスをスキップした場合はその旨。該当が3件を超えて上限により
  見送った銘柄がある場合はその件数

最終判断は人間である旨を添える。

## 無人実行（headless）時の方針

平日の定時に Claude Desktop の Routines から **無人起動**される運用がある
（ルーティンの構成は `CLAUDE.md` の "Scheduled Daily Run" を参照。スケジュール
自体はこのスキルの外で管理される）。対話セッションでは従来どおり
ユーザーに確認しながら進めてよいが、headless では以下に従う。

- **ユーザーに質問できない前提で動く。** `AskUserQuestion` は使えない。判断が割れる
  分岐（既存 `analysis_result.json` の上書き可否、断片の流用可否、proceed か skip か）は
  **常に保守側**を選ぶ — `skip` / withhold / 中断
- 既存の `analysis_result.json` があり、再実行の明示指示が無い場合は Step 0 のルール
  どおり**上書きしない**。既存 verdict を要約して終了する
- degraded・fail-closed 検疫・断片欠落は、既存ルールどおり**リトライで検証を通そうと
  しない**（AC15）。状況をそのまま最終報告に書いて終了する
- 加えて、可能であれば `<WORKDIR>/headless_note.md` に同じ内容（実行日時、どこまで
  進んだか、縮退・欠落の理由、次に人間が確認すべき点）を残す。これは人間向けのメモで
  あり、`analysis_work/` の断片契約にも `analysis_result.json` にも含めない。
  ユーザーは翌朝 `reports/latest.md` とログ、必要ならこのメモを確認する
- 中断して終了する場合も、Step 7 の報告項目は分かる範囲で必ず出力する

## 禁止事項

- スクリーニングのスコア・ランキング・リスク制約の再計算や書き換え（AC1）
- 入力に無い情報の補完（事前知識による企業情報・株価・決算数値の追加）（AC8）
- 検証を通すための文言の書き換え・再投入（AC15）
- 断定的売買指示、命令形、根拠なき心理・行動診断（AC3〜AC5）
- 専門家が書いた `analysis_work/` 断片の本文を統括が直接書き換えること
  （修正が要るなら該当専門家に再分析を依頼し、断片を上書きさせる）
- 反証パス（Step 3.6）・対称パス（Step 3.7）の出力をファイル化して `analysis_work/` や
  `analysis_result.json` に混ぜること（いずれも verdict の材料であり出力契約ではない）
- ユーザーが再実行を求めていないのに既存の `analysis_result.json` を上書きすること
- 一時ファイルの掃除目的で `rm` を実行すること（統括・サブエージェントとも。
  「一時ファイルと後始末」参照）
- `<WORKDIR>` やリポジトリ配下に作業用の一時ファイル（スライス、抽出テキスト等）を
  作ること
- 断片や `analysis_result.json` の契約検証を自前のスクリプトで実装すること
  （`copilot-verify-analysis` を使う。Issue #132）
- `src/`, `tests/`, `docs/` の編集（このスキルは分析実行であり実装変更ではない）
