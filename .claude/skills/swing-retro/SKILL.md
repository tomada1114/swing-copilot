---
name: swing-retro
description: >
  Run the retrospective loop that measures whether the qualitative verdict layer
  earns its place, and turn the evidence into improvement proposals. Executes
  `copilot-retro prepare` for the deterministic dossier, fans out surprise
  post-mortems / signal-vs-verdict reconciliation / source-contribution review to
  parallel subagents, writes retro_result.json, validates it via
  `copilot-retro ingest`, then applies L1 proposals immediately and L2/L3
  proposals only after design approval — one proposal, one branch, one PR.
  Use PROACTIVELY when: 振り返り、振り返り実行、改善提案、verdict の当否、
  当否評価、提案台帳、retrospective, retro run, propose improvements.
---

# 振り返り→改善提案ワークフロー（統括）

当否分類・集約・閾値判定・検証はすべて Python の決定論コードが行う。
このスキルが担うのは「なぜ外したか」の定性再読と改善提案の叙述、そして
承認された変更の適用だけ。**決定論的な数値（集約指標・分類・スコア）は
絶対に書き換えない。**

Python 側（`copilot-retro`）には config / コードを書き換える経路がない。
変更を行うのはこのスキルの適用段階だけで、必ず提案ごとのブランチ +
検証合格 + PR を経由する。`main` へ直接コミットしない。

`retro_input.json` の定型集約に無い切り口を確かめたくなったら（提案の裏取り、
「この差はレジームで説明できないか」等）、`swing-research` の読み取り専用
DataFrame（`swing_copilot.research`）で検証できる。ただし提案の証拠として
使うのは `retro_input.json` の metric_id 付き数値だけであり、research の
アドホック集計は補助的な確認に留める。

作業前に必ず読む:

- `.claude/skills/swing-daily/references/analysis-conventions.md` — AC1〜AC16 の
  共通叙述規約（CON-03・provenance・hedge）。振り返りの叙述にもそのまま適用する。
  **このスキルへコピーせず、上記パスを直接読むこと**
- [references/proposal-rules.md](references/proposal-rules.md) — 敗因分類・提案レベルと
  証拠ゲート・台帳 status ライフサイクル・再提案ガード
- [references/result-schema.md](references/result-schema.md) — `retro_result.json` の
  組み立て方と evidence_refs の空間
- `src/swing_copilot/retro/schemas.py` — **スキーマの最終正本**。JSON を組み立てる前に必ず読む

## Inputs

`reports/retro/<as_of>/` を `<RETRODIR>` と呼ぶ。

- `<RETRODIR>/retro_input.json` — 必須。Step 1 の `prepare` が生成。読み取り専用
- `docs/retro/proposals.md` — 提案台帳（履歴・監査・重複抑止）。パスは
  `retro_input.json` の `proposals_ledger.path` が正
- `docs/retro/proposals/RP-NNN-<slug>.md` — 過去提案の全文
- `<RETRODIR>/retro_result.json` — 任意。存在すれば再入とみなす（Step 2）
- スクリーニングパラメーター起因の仮説（「そもそも候補に上がらなかった」系）を
  検証する証拠源 — 任意、必要なときだけ読む:
  - 各 run の `reports/<run_date>/<run_id>/rejections.json` — 落選銘柄の明細と
    candidate_limit 切り捨て（score 内訳付き）
  - `uv run copilot-history rejections --run-id <uuid>` — DuckDB の落選台帳
    （銘柄ごとに最初に失敗した 1 条件のみ記録される点に注意）
  - `uv run copilot-filter-matrix --as-of <YYYY-MM-DD>` — 各フィルタ/シグナルを
    単独適用した独立通過率と重複行列（真のボトルネック特定用）
  - `uv run copilot-dd-forward --as-of <YYYY-MM-DD>` — Distribution Day 水準別の
    先行きリターン/ドローダウン。「候補は出たが Exposure Ceiling で全部
    shares=0 になった」系の仮説を検証する（`--sweep` / `--grid` で閾値感度）

## Outputs

- `<RETRODIR>/retro_result.json` — このスキルが直接書く唯一の JSON 成果物
- `<RETRODIR>/retro_report.md` — `copilot-retro ingest` が描画（間接出力）
- `docs/retro/proposals.md` / `docs/retro/proposals/` — ingest が status=proposed で
  追記し、以降の status 遷移をこのスキルが記録する（D10）
- 提案ごとの PR — L1 は即時、L2/L3 は設計承認後

## Step 1: Preflight（`prepare` の実行）

まず `just data-pull` でローカルの `data/` を最新にする。正本は R2 にあり、
日次実行は GitHub Actions が書いている。このスキルは DuckDB に**書く**
（`collect` の verdict 取り込み、`ingest` の retro 結果）ので、取得 → 作業 →
`just data-push`（Step 8）を 1 セットで行い、pull したまま放置しない。

```bash
uv run copilot-retro prepare --as-of <YYYY-MM-DD>
```

`collect`（reports/ 走査 → verdict を DuckDB へ）→ `evaluate`（満期を迎えた
forward return の分類）→ `export`（証拠一式を `retro_input.json` へ）を順に実行する
umbrella コマンド。`--as-of` は必須。日付の指定がなければユーザーに確認する。

- ターミナル出力から `retro_input.json` の**絶対パス**（= `<RETRODIR>`）を拾う
- 黄色の note（bar 欠損・鮮度データ取得失敗など fail-soft の記録）は捨てずに控える。
  欠損のある窓で計算された指標は、その欠損を知った上で読む必要がある
- 評価対象 0 件（`outcome 0 行`）の場合はそこで終了し、「評価可能な満期がまだない」と
  報告する。Step 2 以降に進まない

## Step 2: dossier と台帳の読み込み

1. `<RETRODIR>/retro_result.json` が既に存在する場合、ユーザーが明示的に再実行を
   求めていなければ**上書きしない**。既存の提案を要約して報告し、再実行するか確認する
2. `retro_input.json` を読み、`as_of` / `input_digest` / `window_start` を控える
   （2 値は後段で result に逐語転記する。不一致は run ごと hard fail）
3. 読む順序: `aggregates`（verdict_mix → separation 3 版 → tracked_performance →
   proceed 重大外し率 → skip 的中率 → news_supply）→
   `signal_performance` → `source_contribution` →
   `basis_contribution` → `failure_class_history` → `aggregates_by_config` →
   `surprises` → `config_snapshot` → `notes`。`verdict_mix` は他の指標より先に読む——
   proceed が出ていない窓では `separation` / 重大外し率が `value: null` で沈黙するが、
   `verdict_mix` は成熟を待たず窓内の verdicts から算出されるため沈黙しない
   （`verdict_count >= 20` かつ `proceed_count == 0` で `is_flagged: true`）
4. `proposals_ledger.path` の台帳と、`rejected_proposal_ids` が指す提案全文を読む。
   過去に却下・検証不合格になった提案は Step 4 の突合対象になる
5. `is_preliminary: true` の指標は「暫定」。`value: null` は「この窓では測れない」で
   あって「ゼロ」ではない。両者を混同した提案は書かない
5b. separation は 3 版ある（`metric:separation:*` = 窓全体プール平均差、
   `metric:separation_paired:*` = run 日ごとの差の平均、
   `metric:separation_paired_excess:*` = 同じペアリングをベンチマーク超過リターンで）。
   **プール版だけを根拠にしない**——地合いと交絡しうる。3 版が一致すれば効果はベータ
   由来でないことの傍証、食い違えばその食い違い自体が所見である。一致するまで版を
   選び直すのは AC15 の「検査を通すための書き換え」に当たる
5c. `stderr` / `ci_low` / `ci_high`（両側 95%）を必ず見る。**区間が 0 を跨ぐ点推定を
   根拠に config を動かさない**。重み合成のヘッドラインに区間が無いのは意図であり
   （5 日と 20 日は同じ run を測り直した非独立な 2 窓）、そこを「精度が高い」と
   読んではならない。詳細は `references/proposal-rules.md`
5d. `tracked_performance` は追跡台帳の判断当否の集計結果を proceed / skip / all で層別したもの。
   skip 群は**同一の出口ルールで仮想追跡した反実仮想**であって実際に提案された建玉では
   ない。proceed と skip の差が verdict レイヤの寄与そのものだが、両群の
   `closed_count` を必ず併記して読むこと
6. `input_coverage` と各サプライズの `input_filing_coverage` を確認する。
   `severe_miss_symbol_count_with_gap` は情報不足との併存数であり因果を証明しない。
   `without_gap` / `unknown` と比較し、個別dossierの章状態を読んだうえでのみ
   `information_present_missed` と `information_absent` を分類する。
   gap は export 段（`truncated_filing_count`）だけでなく取得段
   （`exhibit_truncated_filing_count`＝8-K Exhibit が取得段の文字数安全弁で切られた件数、
   Issue #157）も含む。`unknown` は「そのrunの入力に取得段の欠落があったか記録が無い」
   であって「欠落が無い」ではないので、`without_gap` と同じ扱いにしない。
   `starved_filing_count`（Issue #267。旧 dossier では未計測の 0）は「分析済みと
   呼べる量が渡っていなかった開示」の件数で、**縮退を読むならこれを見る**。
   `fallback_filing_count` / `omitted_filing_count` は切られ方の内訳にすぎず、
   Issue #255 以降は飢餓した開示も `omitted_symbol_budget` にならない。この件数が
   立っている run の verdict は「材料が無かった」のではなく「材料を渡していなかった」
   側なので、`information_absent` と断じる前に当該 dossier の
   `input_filing_coverage` の字数を必ず読む
7. `aggregates.news_supply`（Issue #154。旧 dossier では `null`）を確認する。
   `sufficient_threshold`（自社材料の件数しきい値）に対し、`sparse` / `none` 判定の
   銘柄でどれだけ `proceed` が出たか、各セルの `symbol_mention_items` の min/max/mean
   が境界の内外どちらへ寄っているかを読む。`level: "unrecorded"` は Issue #130 以前の
   アーカイブで**未計測**であり、計測された `none` と同じ扱いにしない。
   `symbol_mention_items` はティッカー出現数なので実際の自社材料数の**下限値**であり、
   `sufficient` でも材料が薄い場合はありうる。この偽陰性はサプライズ dossier の
   `news_supply` と当時の reasons を読んで初めて言えることで、集計だけでは言えない

## Step 3: 並列深掘り（サブエージェント fan-out）

3 つの観点を**同一メッセージ内で並列に**起動する（`model: sonnet`）。

**標準経路は Agent ツールの並列起動である。** 振り返りも定時実行（`CLAUDE.md` の
"Scheduled Daily Run"）から呼ばれうるが、headless 実行では Workflow ツールの利用が
明示的に許可されないため、組の数にかかわらず常に Agent 経路を使う。
**実行のたびに手段を選び直さない。** 毎回トリアージし直しても結論は変わらず、
判断と説明のコストだけが積み上がる。

**Workflow ツールは対話セッション限定の任意手段。** 決定論的な分岐・レジューム・
進捗可視化が効くため、組の数が多いときは上位互換になりうる。ただしこれは標準経路では
なく、次の**両方**を満たすときにだけ選べる任意の代替手段である。

1. 対話セッションであり、かつ Workflow の利用が**明示的に許可**されている
2. 組の数が 9 を超える（サプライズ銘柄が多い回。これ以下の規模なら Agent 並列で
   十分で、切り替える利点が無い）

どちらか一方でも欠けるなら、可否を検討せずそのまま Agent 並列で進める。Workflow を
使う場合も **各エージェントへ渡す指示内容は Agent 経路と同一**（下記）。
手段の違いが分析内容の違いになってはならない。

| 観点 | 担当 | 出力 |
|---|---|---|
| サプライズ敗因分析 | `surprises.items[]` の 1 銘柄ずつ。当時の verdict・reasons・`input_filing_coverage`・`news_supply`・引用 facts と、`freshness`（run 以降に公開されたニュース・開示）を突き合わせる | 銘柄ごとの `failure_class` 1 値 + 叙述 + `evidence_refs` |
| シグナル×verdict 突合 | `signal_performance` と `aggregates` を並べ、「シグナルが外した」のか「読みが外した」のかを切り分ける | 指標の取捨選択観点の観察 |
| ソース貢献レビュー | `source_contribution` の引用回数と HIT/MISS 引用比率、`information_absent` の反復 | ニュース源の増減観点の観察 |
| 根拠タイプ貢献レビュー | `basis_contribution` の根拠タイプ別 verdict 件数と `hit_citation_ratio`。「決算根拠の proceed」と「テクニカルのみ根拠の proceed」のどちらが当たっているかを比較する。`untagged` の割合が高い窓では、他の行の比較を根拠に使わず「タグ付与率が低く比較不能」と書く | 根拠タイプの偏り観点の観察 |

各エージェントへの指示に必ず含めるもの:

1. `.claude/skills/swing-daily/references/analysis-conventions.md` を読み、AC1〜AC16 に
   従うこと（特に AC3〜AC5 の CON-03、AC6 の provenance、AC12 の hedge）
2. `retro_input.json` の**絶対パス**と、担当する `surprise_id` / 指標 ID の列挙
3. `evidence_refs` に書けるのは `retro_input.json` が供給した ID だけであること
   （[references/result-schema.md](references/result-schema.md) の「証拠 ID の空間」）。
   捏造・整形・推測は当該項目の非表示を招く
4. 親に返すのは**観点ごと数行の要約 + 特記事項**だけ。JSON 全文や生のニュース本文を
   メッセージに載せないこと

## Step 4: 統合と自己 QA（セッション本体で実施）

サブエージェントに丸投げしない。

1. **敗因分類の集計**: 今回の `failure_class` の分布を出し、`retro_input.json` の
   `failure_class_history` を**読む**（Issue #189 以降、過去分は数えない）。そこには
   直近 3 回の取り込み済み振り返りの件数・出現セッション数と、決定論コードが計算した
   `meets_l2_gate` が入っている。件数は**下限**である——今回のあなたの読みはまだ
   ingest されていないので含まれない。`failure_class_history` が `null` なら、
   取り込み済みの振り返りがまだ 1 回も無いということ（L2 定性ゲートは未成立）
2. **証拠ゲート判定**: 提案案ごとに
   [references/proposal-rules.md](references/proposal-rules.md) の床を満たすか判定する。
   ゲートは**上限ではなく床**であり、満たしても書く義務はなく、満たさない提案は書けない
3. **毎回の自問（必須）**: 「L2/L3 相当の構造的観察はないか」を明示的に自問する。
   細かいパラメータ調整（L1）ばかりを挙げていないかを疑う。**ゼロ件で終わったなら、
   それは十分に探していない**。探した上で無ければ、`structural_review_note` に
   **「再点検の上でなし」**と、何を見て無いと判断したかを明記する。この自問の結果は
   スキーマ上の必須フィールドなので、省略すると ingest が hard fail する
4. **再提案の突合**: 台帳で `rejected` / `verification_failed` の提案と同一
   `proposal_key` を出そうとしていないか確認する。出すなら当該 RP-ID への言及と
   新規証拠の説明を `reopen_justification` に書く。無ければ ingest が差し戻す
5. **数値の非改変**: 集約指標・分類・シグナル成績を再計算・書き換えしない。
   叙述で「この指標はこう読める」と書くのは可、値を書き換えるのは不可

## Step 5: `retro_result.json` の書き出しと `ingest`

`src/swing_copilot/retro/schemas.py` を読んで最新のフィールド名を確認したうえで、
[references/result-schema.md](references/result-schema.md) の形で組み立てる。

```bash
uv run python -c "import json,sys;json.load(open(sys.argv[1]))" <RETRODIR>/retro_result.json
uv run copilot-retro ingest <RETRODIR>
```

`ingest` は strict スキーマ検証・`as_of`/`input_digest` 同一性・evidence 参照検証・
CON-03 機械検査・再提案ガードを行い、通過した提案だけを台帳へ status=proposed で
追記し `retro_report.md` を描画する。あわせて検証済み narration を
`data/copilot.duckdb`（`--db` で変更可）の `retro_narrations` へ蓄積する
（Issue #189）。次回以降の `retro_input.json` の `failure_class_history` は
ここから作られるので、**ingest を飛ばすと L2 定性ゲートの材料が失われる**。

**非表示（withheld）が出た場合:**

- **リトライで検証を通そうとしない。** fail-closed が仕様であり、文言を書き換えて
  再投入するのは規約違反にあたる（AC15）
- 非表示になった項目と理由（evidence 参照違反 / CON-03 違反 / 再提案ガード）を
  そのまま報告する
- スキーマ不一致による hard fail のみ、`schemas.py` を読み直して
  **フィールド名の誤りを修正**して再実行してよい（内容の書き換えではないため）

ユーザーへ提示する内容: 成績サマリ（verdict_mix・separation・重大外し率・skip 的中率と
暫定表示）、敗因分類の分布、記録された提案一覧（RP-ID / level / タイトル）、
非表示になった項目、`structural_review_note`、`retro_report.md` のパス。

ingest が台帳へ追記した status=proposed の行と提案全文は、この振り返り回の記録として
1 本のブランチ（例 `docs/retro-<as_of>`）にコミットし PR を作る。`reports/retro/**` は
gitignore 対象なのでコミットしない。

## Step 6: L1 提案の即時適用

証拠ゲートを満たす L1（既存 config 値の変更）は事前承認なしで適用する。
人間のチェックポイントは PR レビュー・マージに集約される。**1 提案 = 1 ブランチ = 1 PR**。

提案ごとに、順に:

1. `main` から `feat/rp-NNN-<slug>` を切る
2. 提案の `target`（config パス）だけを編集する。他の提案の変更を混ぜない
3. 提案の `verification_plan` を**そのまま実行**する。指標・閾値系なら
   `uv run copilot-backtest` の前後比較で、提案が書いた合否基準に照らす
4. `just verify` を実行する（lint / test / docs-check / smoke）
5. **合格した場合**: `smart-commit` / `create-pr` スキルの慣習に従ってコミット・PR 作成。
   PR 本文に verification_plan の実行結果を貼る。台帳の当該行を `applied` に更新し、
   「PR/決裁メモ」欄へ PR 番号を記録して同じブランチへ追加コミット・push する
6. **不合格の場合**: 適用を取り消す（ブランチを捨てる）。台帳の当該行を
   `verification_failed` に更新し、不合格の内容を提案全文へ追記する。この記録も
   台帳更新のみの PR として出す。**不合格を通すために基準や検証手順を緩めない**

適用後の `merged` / `reverted` は PR の顛末に追従して記録する。

## Step 7: L2/L3 提案の設計承認と適用

L2（構成変更）・L3（設計見直し）は、適用前に `AskUserQuestion` で**設計の方向性**の
承認を得る。ユーザーは投資の素人前提なので、個別数値の妥当性ではなく方向性を問う。

1. 提案ごとに設計をまとめる: **変更内容 / 影響範囲 / 検証計画 / 代替案**。
   L3 は代替案を最低 2 案添える
2. `AskUserQuestion` で承認を得る。選択肢は「承認して適用」「保留」「却下」を軸に、
   代替案があればそれも選択肢に出す。個別の閾値の数値を選ばせない
3. **承認された場合**: Step 6 と同じ手順（提案ごとのブランチ → 適用 →
   verification_plan → `just verify` → PR）で適用する。台帳を `applied` に更新する
4. **1 セッションに収まらない規模**（大幅なアーキテクチャ変更等）: 承認後に
   roadmap の P-ID を起こし goal-prompt 化して別セッションへ引き継ぐ。台帳は
   `deferred` とし、引き継ぎ先を「PR/決裁メモ」欄に記録する
5. **却下・保留**: その場の回答で確定する。台帳を `rejected` / `deferred` に更新し、
   理由を提案全文へ追記する。`rejected` の記録が次回以降の再提案ガードの入力になる

## Step 8: 書き戻しと報告

報告の前に `just data-push` で DuckDB を R2 へ書き戻す。generation 不一致で
拒否されたら、その間に別の実行（日次実行など）が書いている。**ローカルの変更を
捨てて上書きしない**で、拒否された事実をユーザーに報告して指示を仰ぐ。

- 評価対象の窓（`window_start` 〜 `as_of`）と評価件数
- 成績サマリ（verdict_mix・separation・重大外し率・skip 的中率、暫定表示の有無）
- 敗因分類の分布と、そこから昇格させた構造的観察
- 提案ごとの顛末（RP-ID / level / applied+PR 番号 / rejected / deferred /
  verification_failed）
- 非表示になった項目と理由
- `retro_report.md` と台帳のパス

最終判断は人間である旨を添える（PR のレビュー・マージ）。

## 禁止事項

- 集約指標・当否分類・シグナル成績の再計算や書き換え（AC1）
- `retro_input.json` に無い情報を証拠として書くこと・`evidence_refs` の捏造（AC8）
- 検証を通すための文言の書き換え・再投入（AC15）
- 断定的売買指示、命令形、根拠なき心理・行動診断（AC3〜AC5）
- 証拠ゲートの床を満たさない提案を書くこと、および床を緩めること
- `verification_plan` 不合格のまま適用を残すこと、検証手順や合否基準を緩めること
- `main` への直接コミット、1 PR への複数提案の同梱
- L2/L3 を `AskUserQuestion` の承認なしに適用すること
- `structural_review_note` の省略、および「なし」とだけ書いて根拠を書かないこと
- ユーザーが再実行を求めていないのに既存の `retro_result.json` を上書きすること
