---
name: swing-research
description: >
  Answer ad-hoc questions about the accumulated decision history (verdict
  hit rates, score breakdown vs forward returns, regime-conditional
  performance, rejection reasons, tracking-ledger stats) by writing read-only
  Python against `swing_copilot.research` DataFrames. Strictly read-only:
  never mutates the DuckDB file, config, or reports, and routes any
  improvement idea to an issue or swing-retro instead of applying it.
  Use PROACTIVELY when: 蓄積データ、当たってる？、勝率を見て、スコアと成績、
  レジーム別、落選理由の集計、データ分析、ノートブック、集計して、分布を見て、
  hit rate, win rate by, scorecard, analyze the data, query the database,
  how are proceeds doing, rejection stats.
---

# 蓄積データのアドホック分析（読み取り専用）

DuckDB に溜まった判断履歴（verdict・当否・スコア内訳・追跡台帳・レジーム・
落選理由）から、**利益に直結する問い**に答える。売買の指示・推奨はしない。
設定変更もしない。**読むだけ**。

canonical な使い方・データ辞書・NULL の意味論は
[docs/09_research_guide.md](../../../docs/09_research_guide.md) を正とする。
発見を改善へつなぐ判断規律は `docs/08_architecture_review_2026-08.md` §5 に従う。

## 安全規約（違反したら即中断してやり直す）

1. **必ず `swing_copilot.research` 経由で読む。** 生の `duckdb.connect()` を
   `data/copilot.duckdb` に向けて開かない。research の各関数はクエリ毎に
   read-only 接続を即開閉するので、これだけでロック規律が守られる
2. **接続・REPL を掴んだまま放置しない。** この作業コピーは 18:30 の無人日次
   実行の実行環境でもある。長い対話セッション中も、クエリはスクリプト単位で
   `uv run python` を使い捨てにするのが安全
3. **何も書かない。** DB・`config/**`・`analysis_result.json`・`reports/latest.md`
   に触れない。分析メモを残すのはユーザーが求めたときだけで、置き場所は
   `reports/research/<as_of>-<slug>.md`（新規ファイルのみ、上書き不可）
4. スキーマに無い列・テーブルを推測で参照しない。迷ったら
   `research.query("DESCRIBE v_verdict_scorecard")` で確かめる

## 手順

1. **問いを1文に固定する**（例:「score 上位群は下位群より 20 日リターンが
   良いか」）。曖昧な依頼はまず問いへ言い換えてユーザーに見せる
2. `uv run python` のワンショットスクリプトで `research.scorecard()` 等を読む。
   定番の入口:

   ```python
   from swing_copilot import research

   df = research.scorecard()          # verdict × 当否 × スコア × レジーム × 追跡 × セクター
   c  = research.candidates()         # スコア内訳（型付き列）
   t  = research.tracked_positions()  # 2.5×ATR/25セッションの仮想成績
   r  = research.screening_rejections()
   ```

3. **集計には必ず件数を併記する**。`mean` 単独禁止 — `.agg(["mean", "median", "count"])`
4. 結果をユーザーの問いに対する答えとして日本語で要約する（下記の解釈規律つき）

## 解釈の規律（数字を語るときの必須事項）

- **n を必ず添える**。n < 20 は「予備的」と明記する（retro の
  `preliminary_sample_threshold` と同じ慣例）
- **重複窓の従属性**: 同一銘柄・近接日の forward return は独立ではない。
  run 数・銘柄数ベースの実効サンプル感を併記する
- **地合いとの交絡**: proceed 群と skip 群の素リターン差は市場全体の方向を
  含む（`docs/08` R7 の既知の弱点）。断定せず「地合い未調整」と注記する
- **多重比較**: 切り口を何通りも試して出た差を「発見」と呼ばない。試した
  切り口の数を正直に書く
- **結論の上限**: このスキルの出力は仮説であって検証結果ではない。
  「〜の傾向がある（n=xx、未検証）」より強い言い方をしない

## 発見の出口（ここから先はこのスキルの仕事ではない）

- 設定値・閾値を変えたくなったら: **変えない**。`docs/08` 原則 3
  （点推定だけで設定を変えない）に従い、GitHub Issue を起票するか
  `swing-retro` の提案ルートへ回す。Issue には現状の課題（数字と根拠）と
  完了条件を明記する
- 特定銘柄を深掘りしたくなったら: `swing-deepdive` へ
- 台帳へ判断メモを残したくなったら: `swing-track` へ

## 定番レシピ

```python
import pandas as pd

from swing_copilot import research

df = research.scorecard()
t = research.tracked_positions()
r = research.screening_rejections()

# レジーム別 × 判断別の成績（地合い未調整の注記を忘れない）
df.groupby(["gate_verdict", "recommendation"])["forward_return_pct"].agg(["mean", "median", "count"])

# スコア分位 × 20日リターン（score-lift の手動版）
h20 = df[df["horizon_days"] == 20].copy()
h20["score_q"] = pd.qcut(h20["score"], 3, labels=["low", "mid", "high"], duplicates="drop")
h20.groupby("score_q", observed=True)["forward_return_pct"].agg(["mean", "count"])

# 追跡台帳の手仕舞い理由別の実現リターン
t[t["status"] == "closed"].groupby("exit_reason")["realized_return_pct"].agg(["mean", "count"])

# 落選理由の件数推移（フィルタがユニバースをどれだけ削っているか）
r.groupby(["as_of", "reason_code"]).size().unstack(fill_value=0).tail(10)

# config 変更前後の比較（config_hash で分割）
df.groupby(["config_hash", "recommendation"])["forward_return_pct"].agg(["mean", "count"])
```

古い DB で `ResearchError`（ビュー未作成）が出たら `research.ensure_views()` を
一度だけ呼ぶ（唯一 read-write を短時間開く操作。日次実行中は避ける）。
