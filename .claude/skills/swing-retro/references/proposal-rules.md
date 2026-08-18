# 敗因分類・証拠ゲート・台帳ライフサイクル

`SKILL.md` の Step 4 / Step 6 / Step 7 が参照する判定規則。設計正本は
`docs/04_detailed_design.md` 3.23 節、スキーマ正本は
`src/swing_copilot/retro/schemas.py`。

## 1. 敗因分類（`failure_class`、閉じた 5 値）

サプライズ銘柄ごとに**必ず 1 値**を選ぶ。複数にまたがってヘッジすると数えられなくなり、
「同じ原因が繰り返している」という証拠が作れなくなる。

| failure_class | 意味 | つながる提案の典型 |
|---|---|---|
| `information_absent` | 判断材料が当時の入力に存在しなかった（`freshness` の後追い情報には兆候がある） | ニュース源・データ源の追加（L2） |
| `information_present_missed` | 入力にあったが分析が見落とした | スキル手順・fan-out 構成の改善（L2） |
| `interpretation_error` | 情報は捉えたが読み違えた | 叙述規約・スキーマ語彙の改善（L2/L3） |
| `exogenous` | 当時のいかなる入力からも予見不能な外生イベント | 提案なし（ノイズとして記録） |
| `threshold_artifact` | 判断は妥当だが当否分類の閾値・ホライズンが不適切に「外れ」を作った | 評価フレームワーク自体の調整（L1/L3） |

`freshness.fetch_failed: true` の銘柄で「後から取得した情報にも兆候がない」ことを
`exogenous` の根拠にしない。取得が失敗しただけで、沈黙の証拠にはならない。

## 2. 提案レベルと証拠ゲート

ゲートは**床であって上限ではない**。満たしても書く義務はなく、満たさない提案は書けない。

| レベル | 射程 | 最低証拠 |
|---|---|---|
| **L1** パラメータ調整 | 既存 config 値の変更（閾値・重み・予算・ウォッチ水準） | 該当集約 n≥20 **かつ 95% 信頼区間が 0 を跨がない**（`ci_low`/`ci_high` が同符号）。区間が出ない指標なら、2 回以上の振り返りで同方向の再現 |
| **L2** 構成変更 | 指標/シグナル/フィルタの追加・削除、ニュース源の増減、analysis スキーマやスキル手順の変更 | 定量: n≥40。または定性: 同一 `failure_class` が直近 3 回の振り返りで累計 5 件以上（`failure_class_history.counts[].meets_l2_gate`。自分で数えない） |
| **L3** 設計見直し | アーキテクチャ、verdict 語彙、評価フレームワーク自体、パイプライン構成の大幅変更 | separation ≤ 0 が n≥40 で持続、L1/L2 を経ても改善しない systemic 欠陥、または構造的欠陥の発見。診断メモと代替案比較（最低 2 案）を必須添付 |

例外: **計測を可能にするための構造変更**（例: `analysis_result` への confidence
フィールド追加）は、初回から定性根拠のみで L2 提案してよい。運用初期の小サンプル期に
機構が何も出せなくなるのを避けるための意図的な抜け道であり、「計測できるようにする」
以外の変更には使わない。

n の読み方: `MetricEntry.sample_size` が n。`is_preliminary: true`（既定 20 件未満）は
「暫定」であり、L1 の床（n≥20）を満たさない。`value: null` は「この窓では測れない」で
あって「ゼロ」ではない。

散らばりの読み方（Issue #190）:

- `MetricEntry` の `stderr` / `ci_low` / `ci_high`、`RateMetricEntry` の
  `ci_low` / `ci_high`（Wilson 区間）は、いずれも両側 95%。**区間が 0 を跨ぐ点推定を
  根拠に config を動かさない**——これは「効果の符号すら窓から言えない」という状態である
- **5 日と 20 日の方向一致は独立した 2 証拠ではない**。同じ run・同じ銘柄を 2 つの窓で
  測り直したものなので、相関した 1 証拠として読む。「両ホライズンで方向一致」だけを
  根拠に L1 を通さない（旧ゲートの文言はこの点で誤解を招いていた）
- `stderr: null` / `ci_low: null` は「散らばりが定義できない」（観測 2 件未満、または
  重み合成のヘッドライン）であって「推定が正確」ではない。合成行に区間が無いのは意図
  であり、非独立な 2 ホライズンから作った区間は実際より狭くなるため出していない
- `excluded_day_count`（ペアード指標のみ）は片群しか無くて捨てた run 日数。3/20 日から
  作った差と 20/20 日から作った差は同じ主張ではない

separation の 3 版の読み方:

| metric_id | 意味 |
|---|---|
| `metric:separation:*` | 窓全体のプール平均差。地合いと交絡しうる（従来どおり、参照用） |
| `metric:separation_paired:*` | run 日ごとに proceed−skip を取ってから平均。その日の共通変動が相殺される |
| `metric:separation_paired_excess:*` | 同じペアリングをベンチマーク超過リターンで実施 |

3 版が一致すれば効果はベータ由来ではない。食い違うなら、その食い違い自体が所見であり、
一致するまで版を選び直すのは AC15 の「検査を通すための書き換え」に当たる。

`metric:tracked_performance:{proceed,skip,all}` は追跡台帳の実現成績（勝率・PF・
期待値・平均 R・保有日数・手仕舞い理由内訳）。`skip` 群は同一の出口ルールで仮想追跡した
反実仮想であり、実際に提案された建玉ではない。損益はすべて % 単位。

## 3. 提案の必須フィールド

`Proposal`（`retro/schemas.py`）が strict に要求する。欠けると ingest が hard fail する。

- `proposal_key` — 提案の安定した識別子（例 `config:postmortem.severe_threshold_pct`）。
  再提案ガードがこの**完全一致**で判定するので、言い回しではなく対象を表す文字列にする
- `level` / `target`（config パス・モジュール・領域）/ `title`
- `claim`（主張）/ `expected_effect`（期待効果）
- `evidence_refs`（非空、`retro_input.json` が供給した ID の部分集合）/ `evidence_basis`
  （`quantitative` | `qualitative` | `mixed`）
- `verification_plan` — **L1/L2 は必須**。L3（設計見直し）のみ `null` 可。
  指標・閾値系は `uv run copilot-backtest` による前後比較のコマンドと合否基準を明記する
- `risks`（非空）— 「リスクなし」も主張であり、書かれない主張はレビューできない
- `reopen_justification` — 台帳が閉じた提案を再提案するときのみ

## 4. 台帳の status ライフサイクル（D10）

台帳（`docs/retro/proposals.md`）は**承認の場ではなく、履歴・監査・重複抑止の装置**。

```text
proposed ──┬─→ applied ──┬─→ merged
           │             └─→ reverted
           ├─→ rejected
           ├─→ deferred
           └─→ verification_failed
```

- `proposed` を書くのは `copilot-retro ingest` **だけ**。以降の遷移はこのスキルと人間が
  記録する
- `applied`: 適用して PR を作成した。「PR/決裁メモ」欄に PR 番号を記録する
- `rejected`: `AskUserQuestion` で却下された。理由を提案全文へ追記する
- `deferred`: 保留、または規模超過で goal-prompt へ引き継いだ。引き継ぎ先を記録する
- `verification_failed`: `verification_plan` または `just verify` が不合格で適用を
  取り消した
- `merged` / `reverted`: PR の顛末に追従する

台帳の行は人間が手で直してもよい（git 履歴が監査証跡）。追記は既存行・手書きの列・
表の下の注記を壊さない形で行う。

## 5. 再提案ガード

`rejected` / `verification_failed` の行と**同一 `proposal_key`** の提案は、
`reopen_justification`（当該 RP-ID への言及 + 新規証拠の説明）がなければ ingest が
差し戻す。差し戻された提案は台帳へ記録されない。

同じ提案を通すために `proposal_key` を言い換えて回避することは、ガードの意図に反する
（AC15 の「検査を通すための書き換え」に相当する）。再提案するなら、前回の却下理由に
対して何が新しくなったかを書く。
