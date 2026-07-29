# 事前確定判断（Pre-answered Decisions）

振り返り→改善提案機構（P8）の設計にあたり確定した判断。D1〜D4・D10 は
ユーザー指示で確定（D3 は D10 により同日改訂）、D5〜D9 は設計者判断
（根拠付き）。実装セッションは
これらを再審議しない。変更が必要になったら停止してユーザーに確認する。

## D1. 評価方式 = 二層方式（ユーザー確定）

Python が forward return ベースで決定論的に当否分類し DuckDB に保存
（正本）。振り返りスキルはその集計の上で敗因の定性再読と提案生成のみを
担う。「判断はコード、叙述はスキル分析」（roadmap 改修原則 4）の適用。

## D2. 永続化 = 新テーブル + 遅延取り込み（ユーザー確定）

`copilot-retro collect` が `reports/<date>/<run_id>/analysis_result.json` を
走査して `verdicts` / `verdict_sources` へ冪等に取り込む。
`copilot-ingest-analysis` は DB に触れないという既存不変条件を維持する。
過去 run のバックフィルも同一経路。

## D3. 提案管理 = リポジトリ内 markdown 台帳（ユーザー確定、同日改訂）

`docs/retro/proposals.md`（台帳）+ `docs/retro/proposals/RP-NNN-<slug>.md`
（全文）。GitHub Issues は使わない（振り返り実行時のネットワーク/認証
依存と証拠の分散を避ける）。
**改訂（2026-07-28 ユーザー指示）**: 台帳は承認の場ではなく履歴・監査・
重複抑止の装置とし、status（proposed / applied / rejected / deferred /
verification_failed）は機械管理。承認モデルは D10 を正とする。

## D4. スコープ = 統合俯瞰 + 人間判断 + バックテスト接続（ユーザー確定）

- シグナル成績（`signal_outcomes`）とソース貢献も証拠に含め、指標・
  ニュース源・設計の提案まで射程に入れる。
- `trades_journal` の人間判断と verdict のクロス集計を観測に含める。
- 指標・閾値系の提案には `copilot-backtest` による検証手順
  （verification_plan）を必須添付する。

## D5. postmortem とは並置、プリミティブのみ共有（設計者判断）

`signal_outcomes` は按分セマンティクスと CHECK 制約を持ち意味論が別物
なので相乗りしない。取引カレンダー / forward return 純関数を
`pipeline/forward_returns.py` へ抽出して両者で共有し、閾値は
`settings.postmortem` を参照（新閾値セットを作らない）。統合は
retro_input.json のレベルで行う。

## D6. ホライズン 5/20 営業日・閾値 0.5%/2.0% を流用（設計者判断）

新しい評価パラメータ体系を発明しない。既存 postmortem と同一の窓・
閾値なら、シグナル成績と verdict 成績が同じ物差しで比較でき、
カレンダー実装も共有できる。閾値は既に `(要検証)` config であり、
本機構自身のレビュー対象（`threshold_artifact` 敗因分類）に入る。

## D7. `verdict_outcomes.as_of` は満期営業日（設計者判断）

`signal_outcomes.as_of`（観測日）とは意図的に異なる。retro は数日おきの
バッチ実行なので、観測日を記録すると実行タイミングで行の内容が変わる。
満期日確定にすることで、いつ実行しても同一結果（決定論・冪等）になり
評価漏れ・二重評価が構造的に起きない。相違は 04 昇格時に明記する。

## D8. 新パッケージ `retro/`（設計者判断）

`analysis/` は「ネットワークも DB も触らない」憲章を持つため、DB 読み書き
と鮮度データ取得を行う retro は同居できない。`analysis/` の設計と同型の
export / ingest / schemas 構成を持つ独立パッケージとし、CLI は
`copilot-retro` 1 本（prepare / collect / evaluate / export / ingest）。

## D9. 提案レベルの証拠ゲートは「床」であり「上限」ではない（設計者判断）

L1: n≥20 + 方向一致、L2: n≥40 または敗因分類の反復（3 回の振り返りで
累計 5 件）、L3: separation ≤ 0 の持続または systemic 欠陥 + 代替案比較。
根拠: 二項 95% CI 半幅が n=20 で約 ±22pt、n=40 で約 ±15pt。
計測を可能にする構造変更（confidence フィールド等）は初回から定性根拠で
提案可とし、運用初期（小サンプル期）でも機構が価値を出せるようにする。
スキルは毎回「L2/L3 相当の構造的観察はないか」を自問し、なければ
「再点検の上でなし」と明記する（細かい調整への偏り防止）。

## D10. 承認と適用 = L1 即時適用 + PR、L2/L3 は AskUserQuestion 承認後に適用 + PR（ユーザー確定、2026-07-28）

ユーザーは投資の素人前提のため、個別数値の事前承認は求めない。

- **L1（軽微な調整）**: 事前承認なし。スキルが提案ごとのブランチで
  即時適用し、verification_plan と `just verify` の合格を確認して
  PR を作成する（`smart-commit` / `create-pr` スキルの慣習に従う）。
  不合格は適用取り消し + `verification_failed` 記録。人間の
  チェックポイントは PR レビュー・マージに集約される。
- **L2/L3（中規模以上）**: スキルが設計（変更内容・影響範囲・検証計画・
  代替案）をまとめ、`AskUserQuestion` で設計の方向性の承認を得てから
  適用し PR を作成する。1 セッションに収まらない規模は承認後に
  roadmap P-ID / goal-prompt 化して別セッションへ引き継ぐ。
- 1 提案 = 1 ブランチ = 1 PR（原子性と revert 容易性のため）。
- 将来の細粒度介入への切替余地として `settings.retro.approval_mode:
  auto | manual` の config 名を予約する（初期実装は `auto` 固定）。
