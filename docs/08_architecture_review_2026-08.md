# 08. アーキテクチャレビューと利益直結改修計画（2026-08）

- ステータス: 調査完了・改修着手（2026-08-17）
- 由来: マルチエージェント調査（6観点並列、約75万トークン、全発見に file:line 根拠付き）
- 位置づけ: 本書はこのレビュー一式の**計画の正本**である。実装時に確定した仕様は
  Issue 単位で `docs/03_basic_design.md` / `docs/04_detailed_design.md` /
  `docs/reference.md` へ反映し、本書は履歴として残す（`docs/06_reliability_roadmap.md`
  と同じ運用）。

## 1. 目的と優先基準

P1〜P8（06 ロードマップ）で工学基盤（as-of 規律・provenance・fail-soft・原子的
書き込み・スキル境界・振り返りループ）は完成した。本レビューの優先基準はただ一つ、
**利益を出しやすくすること**である。具体的には:

1. 分析精度を高め、確実性を上げる
2. 分析結果（verdict・追跡・振り返り）を後のロジック改善に活用しやすくする
3. 蓄積データを扱いやすくし、Python レベル（ノートブック等）で分析できるようにする
4. 拡張・メンテ・ロジック変更を行いやすくする

制約は不変: 米国株特化、売買の最終判断・発注は人間、定性分析は Claude Code
スキル（このプロセスはモデル API を呼ばない）、既存の全不変条件。

## 2. 中心診断: 「測る装置」と「測られる本番」が別の系になっている

個々の検証部品（dd_forward の前向き計測、filter_matrix の独立通過率、sensitivity の
spike/plateau 判定、retro の separation）は丁寧に作られているが、**本番で実際に
効いているレイヤを測れる構成になっていない**。これが利益改善を律速している。

| # | 診断 | 根拠 | 対応 |
|---|------|------|------|
| R1 | バックテストは regime ゲート・portfolio heat・決算ブロック・サーキットブレーカーを一切通さず、建玉サイズも残現金基準（本番は口座資産基準）。要検証の防御閾値は定義上バックテストの数字を動かせない | `backtest/engine.py:27-29,300-307`、`risk/checks.py:433-476` | #184 |
| R2 | 感応度グリッドは 25 セル × 54 分 ≒ 22 時間で未実行のまま。だが候補列は 25 セルで同一であり、分離すれば約 1 時間になる | `reports/backtests/2026-07-30-strategy-comparison.md` §2 | #185 |
| R3 | vcp_breakout は履歴全域を 1 パターンと扱う設計欠陥で 6.5 年 1 トレード。しかも本番(400日)と検証(730日)で供給履歴長が違い挙動が一致しない | `screening/vcp.py:114-169`、`screening/pipeline.py:63` | #186 |
| R4 | score_weights（要検証）を検証する経路が無い。一方で score 内訳と forward return は毎日蓄積されており、結合すればバックテスト無しで毎日答えが更新される | `strategies.yaml:11-16`、`retro/aggregate.py:210-259` | #187 |
| R5 | 測れているのは偽陽性率だけ。candidate_limit で切られた near-miss と落選銘柄の forward return が DB に無く、偽陰性率・選抜効果・candidate_limit 自体を検証できない | `report/rejections.py:19-22`、`pipeline/postmortem.py:215-243` | #188 |
| R6 | 振り返りループの後半（提案→検証→適用→効果測定）が閉じていない: failure_class は gitignore 対象にしか残らず、config 変更の効果を測る装置が無く、提案台帳は 0 行 | `retro/ingest.py:104`、`docs/retro/proposals.md` | #189 |
| R7 | verdict レイヤの寄与を測る反実仮想が無い（skip は追跡されない）。separation は地合いと交絡し、散らばり指標なしの n≥20 だけで config 変更ゲートを通す | `tracking_records.py:158-198`、`retro/aggregate.py:87-113,241-259` | #190 |
| R8 | スキル入力の情報密度不足: 過去 verdict の自己参照経路が無い、根拠タイプのタグが無い、生テクニカル値が渡らない | `paper_records.py:155-198`、`analysis/schemas.py:470-487` | #191 |
| R9 | 蓄積 23 テーブルを Python で横断分析する入口が無く、read_only 接続も無かった | `storage/database.py`（改修前）、`__init__.py:21-29` | **本ブランチで実装済み**（§4） |

このほか、アーキテクチャ面では「リスクチェック追加の拡張コストがシグナル追加と
非対称（registry が無い）」「pipeline/daily.py 1490 行に 8 ステップ実装が同居」
（#193）、統計面では「dd スイープが 5 万候補の in-sample 順位付けのみ」「成功例が
構造化されず学習が片肺」（#195）を確認した。

### 調査済み・対応不要と判断した点

- regime ゲート・決算近接ブロック・portfolio heat は**本番経路には実装済み**
  （`risk/checks.py`）。06 ロードマップ D3/D7 の記述は現状より古い。ギャップは
  「本番に無い」ことではなく「バックテストで測れない」こと（R1）。
- 06 の D1（strength 固定 1.0）/ D2（固定ソートでスコア内訳なし）も解消済み:
  strength は可変計算され（`technical_signals.py:96,130`）、ランキングは
  P1-01 の score_breakdown 4 成分になっている。残る課題は重みの検証（R4）と
  戦略別成分（#187）。06 §1 の表は 2026-07-22 時点の履歴として読むこと。
- 出口ルール（`backtest/exits.py`）を tracking が import 共有している設計は正しく、
  重複していない。重複しているのは成績集計（勝率・PF）の側（#190）。

## 3. 改修ロードマップ（Issue 一覧）

フェーズは依存関係順。**最優先は「検証装置と本番の一致」（V1）**で、以降の全ての
閾値検証・改善提案の土台になる。

| フェーズ | Issue | 内容 | 優先度 / 規模 |
|---|---|---|---|
| V1 検証装置 | [#184](https://github.com/tomada1114/swing-copilot/issues/184) | 本番リスクゲートのバックテスト注入 + サイジング基準の口座資産化 | P1 / L |
| V1 | [#185](https://github.com/tomada1114/swing-copilot/issues/185) | 候補ストリームキャッシュで感応度グリッド実行可能化 | P1 / M |
| V1 | [#186](https://github.com/tomada1114/swing-copilot/issues/186) | VCP の直近K収縮化 + シグナル必要バー数の宣言制 | P1 / M |
| V2 計測 | [#187](https://github.com/tomada1114/swing-copilot/issues/187) | score-lift（スコア内訳 × forward return）+ 戦略別ランキング成分 | P1 / M |
| V2 | [#188](https://github.com/tomada1114/swing-copilot/issues/188) | 対照群の永続化（truncated / 落選の forward return、落選 detail 充実） | P1 / L |
| V3 ループ | [#189](https://github.com/tomada1114/swing-copilot/issues/189) | 振り返り後半を閉じる（narration 永続化・config 台帳・効果測定・実験定義） | P1 / L |
| V3 | [#190](https://github.com/tomada1114/swing-copilot/issues/190) | skip シャドウ追跡 + 成績集計統一 + separation 是正 + 信頼区間 | P1 / L |
| V4 入力品質 | [#191](https://github.com/tomada1114/swing-copilot/issues/191) | 分析入力の情報密度（過去 verdict 還元・basis タグ・生値） | P1 / M |
| V5 基盤 | [#192](https://github.com/tomada1114/swing-copilot/issues/192) | スコア内訳・レジーム詳細の実列昇格、signals の run_id キー化 | P2 / M |
| V5 | [#193](https://github.com/tomada1114/swing-copilot/issues/193) | リスクチェック registry 化 + pipeline/steps 分割 | P2 / L |
| V5 | [#194](https://github.com/tomada1114/swing-copilot/issues/194) | exit_atr_period 配線 + --limit 無偏サンプリング | P2 / S |
| V5 | [#195](https://github.com/tomada1114/swing-copilot/issues/195) | 成功例 dossier + dd スイープのホールドアウト検証 | P2 / M |

並列性: V1 内は #185 → #184 の順が効率的だが独立着手可。V2 は V1 と独立。
V3 の #190-1〜2（メトリクス統一）は他と独立。V5 は挙動不変系で随時。

## 4. 本ブランチで実装済みのもの

1. **`swing_copilot.research` — 読み取り専用リサーチ API**（優先基準 3 の解消)
   - `Database(read_only=True)` 対応と、分析用ビュー
     `v_verdict_scorecard` / `v_candidates` / `v_tracked_positions` /
     `v_symbol_sector_asof`（`storage/schema.py`、CREATE OR REPLACE で自己移行）
   - `research.scorecard()` 1 行で「verdict × 当否 × スコア内訳 × レジーム ×
     追跡結果 × セクター」の DataFrame が返る。使い方は
     `docs/09_research_guide.md` を参照
2. **`record_risk_assessments` のトランザクション化** — 「1 論理書き込み = 1
   トランザクション」不変条件違反の修正（銘柄ごとの独立コミットだった）
3. **`PreflightAbort` の理由分離** — exit code 2 が「同日再実行」と「口座残高
   未設定」を区別できず、無人日次実行が設定不備を「本日は分析済み」と誤要約して
   サイレント no-op し続けるリスクがあった。stderr に機械可読プレフィックス
   `PREFLIGHT_ABORT[<reason>]:` を導入し、swing-daily スキルの分岐を更新

## 5. 改修原則（06 ロードマップの原則を継承・追加）

1. **検証できない知見は導入しない**（06 原則 1 の堅持）。V1 が最優先なのはこのため
2. **二重実装の禁止**: ゲート・出口ルール・成績集計は 1 実装を注入・共有する。
   バックテスト側に本番ロジックを書き写してはならない
3. **点推定だけで設定を変えない**: 散らばり（標準誤差・信頼区間・実効サンプル数）
   の無い指標を config 変更の根拠にしない（#190）
4. **対照群なき評価をしない**: 候補だけを見て当否を論じない。truncated・落選・
   skip の forward return を常に併置する（#188、#190）
5. **リサーチは read-only**: 分析・ノートブックは `swing_copilot.research` 経由。
   接続の長期保持や read-write 接続での探索は日次実行のロックを奪うため禁止
