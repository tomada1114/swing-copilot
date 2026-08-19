# 09. リサーチガイド（蓄積データの Python 分析）

`swing_copilot.research` は、DuckDB に蓄積された判断履歴を pandas DataFrame と
して読み出す**読み取り専用**の入口である。ノートブックやアドホックスクリプトから
「verdict の当否 × スコア内訳 × レジーム」のような横断分析を 1 行で書けるように
し、かつ運用中のデータベースを壊す経路を構造的に持たない。

Claude Code からは `swing-research` スキル（「蓄積データを分析して」「勝率を見て」
等で発動）が本ガイドの規約に従って読み取り専用の集計を行う。

## クイックスタート

```bash
uv run python  # または jupyter 等（リポジトリルートで実行）
```

```python
from swing_copilot import research

# verdict を軸にした結合済みスコアカード（下記データ辞書参照）
df = research.scorecard()

# 例1: レジーム別 × 判断別の平均 forward return
df.groupby(["gate_verdict", "recommendation"])["forward_return_pct"].agg(["mean", "count"])

# 例2: スコア上位/下位で 20 日リターンに差はあるか
h20 = df[df["horizon_days"] == 20]
h20.groupby(h20["score"] > h20["score"].median())["forward_return_pct"].mean()

# 例3: 落選理由コード別の件数推移
research.screening_rejections().groupby(["as_of", "reason_code"]).size()

# 例4: 任意の read-only SQL（書き込み文は失敗する）
research.query("SELECT count(*) FROM verdicts WHERE recommendation = 'proceed'")

# 価格バーは Parquet から直接（DB ファイルに一切触れない）
bars = research.bars(["AAPL", "MSFT"])
```

`db_path` を省略すると既定の `data/copilot.duckdb` を読む。別ファイルを見るときは
`research.scorecard(db_path=...)`。

## API 一覧

| 関数 | 返すもの | 元 |
|---|---|---|
| `scorecard()` | verdict × 当否 × スコア内訳 × リスク制約 × レジーム × 追跡 × セクター | `v_verdict_scorecard` |
| `candidates()` | 候補とスコア内訳（JSON から型付き列へ展開済み） | `v_candidates` |
| `tracked_positions()` | verdict 追跡台帳の仮想ポジション + recommendation（Issue #190 以降は `skip` のシャドウ建玉も含む） | `v_tracked_positions` |
| `truncated_candidates()` | `candidate_limit` で順位落ちした near-miss とスコア内訳（列は `candidates()` と揃えてある） | `v_truncated_candidates` |
| `universe_forward_returns()` | 候補 ∪ 順位落ち ∪ 落選の forward return（`outcome_class` / `reason_code` / `execution_state` 付き） | `v_universe_forward_returns` |
| `signal_hits()` | その run のシグナル発火（候補にならなかった銘柄の分も含む） | `v_signal_hits` |
| `verdict_reasons()` | verdict の理由 1 件 1 行（`basis` / `source_id_count` 付き） | `v_verdict_reasons` |
| `runs()` / `verdicts()` / `verdict_outcomes()` / `screening_rejections()` / `regime_snapshots()` | 各テーブルそのまま | 実テーブル |
| `bars(symbols)` | 日足 OHLCV（in-memory DuckDB で Parquet を直読） | `data/parquet/` |
| `query(sql, params)` | 任意 SQL の結果 | — |
| `ensure_views(db_path)` | ビュー未作成の古い DB を修復（唯一 read-write を短時間開く） | — |

## データ辞書: `v_verdict_scorecard`

粒度は **(verdict, 成熟したホライズン)**。未成熟の verdict も horizon 列が NULL の
1 行として必ず現れる（「今日何を判断したか」用途を兼ねる）。

| 列 | 意味 | NULL の意味 |
|---|---|---|
| `run_date` / `mode` / `config_hash` | run の属性。config_hash で設定変更前後を分割できる | — |
| `recommendation` / `no_trade` | スキル verdict（proceed/skip） | — |
| `news_supply_level` | verdict 時のニュース供給量（sufficient/sparse/none） | 計測導入(#130)前の行。none ではない |
| `horizon_days` / `forward_return_pct` / `classification` | 5/20 営業日の当否（HIT/MISS_*） | まだ成熟していない |
| `rank` / `score` / `score_*` / `rsi14` / `atr14` / `close` / `avg_volume` | スクリーニングのランキング内訳と生値 | 該当 run の candidates に無い、または成分導入前の行 |
| `execution_state` / `execution_distance` | ランキング時の実行状態と SMA50 からの ATR 距離（Issue #192） | 未記録（列の導入前の行）。`UNKNOWN` とは別 |
| `dd15_spy` / `dd5_spy` / `vix_close` | 短い窓の distribution 件数と VIX（Issue #192） | レジーム snapshot が無い run、またはゲート評価不能 |
| `risk_status` / `binding_constraint` | リスク評価と、株数を決めた制約 | リスク評価が無い |
| `gate_verdict` / `dd_level` / `dd_count_*` | 市場レジームゲートの状態 | レジーム snapshot が無い run |
| `position_status` / `exit_reason` / `realized_return_pct` / `days_held` | 追跡台帳（2.5×ATR トレーリング / 25 セッション）の結果 | まだ追跡が始まっていない（Issue #190 以降、`skip` も同じルールで追跡されるので「skip だから NULL」ではなくなった） |
| `gics_sector` | run 日時点で有効なユニバース snapshot のセクター（as-of inclusive） | snapshot が run 日以前に無い |

セクターの as-of 解決（`snapshot_date <= run_date` の最新）は
`v_symbol_sector_asof` に一元化してある。**ノートブック側で universe_membership を
自前 JOIN しないこと**（look-ahead 混入の典型源）。

## Issue #190 で増えた列

| 列 | 意味 | NULL の意味 |
|---|---|---|
| `verdict_positions.recommendation` | そのシャドウ建玉がどちら側の verdict を追っているか（`proceed` / `skip`） | 列の導入前に書かれた行。`proceed` と読む（`v_tracked_positions` は `COALESCE` 済み） |
| `verdict_outcomes.benchmark_return_pct` | 同一区間のベンチマーク（既定 SPY）リターン。超過リターンで separation を見るための材料 | **未計測**。0 ではない。列の導入前に分類された行か、ベンチマークのバーが揃わなかった行 |

`skip` 群は**同一の出口ルールで仮想追跡した反実仮想**であって、実際に提案された建玉では
ない。`tracked_positions()` を集計するときは `recommendation` で層別するか、
`proceed` へ絞ること。層別せずに平均を取ると「proceed だけ買った場合」でも
「候補を全部買った場合」でもない、解釈できない数字になる。

## Issue #188 で増えた対照群

候補になった銘柄にしか forward return が付かない間、測れているのは**偽陽性率だけ**
だった。`universe_forward_returns()` はその run が下した screening 判断すべてに
同じ forward return を付ける。

| 列 | 意味 | NULL の意味 |
|---|---|---|
| `outcome_class` | その日その銘柄がどちら側だったか（`candidate` / `truncated` / `rejected`） | — |
| `reason_code` | 落選理由（`screening_rejections` と同じ閉じた enum） | 候補・順位落ちは「何にも落とされていない」ので理由が無い |
| `rank` / `score` | 候補または順位落ちとしてのランキング位置 | 落選銘柄はそもそもランク付けされていない |
| `gics_sector` | as-of 解決済みセクター（`v_symbol_sector_asof`） | snapshot が run 日以前に無い |

```python
# フィルタは利益に貢献しているか（落選銘柄のその後）
df = research.universe_forward_returns()
df[df.outcome_class == "rejected"].groupby("reason_code")["forward_return_pct"].mean()

# candidate_limit を広げたら成績は上がるか（順位帯別）
df[df.outcome_class.isin(["candidate", "truncated"])].groupby("rank")[
    "forward_return_pct"
].agg(["mean", "count"])
```

注意点が2つある。`truncated` 側は**切り口のすぐ下だけ**（`candidate_limit * 3` 件）
しか保存していないので、順位帯の平均は保存範囲までしか語れない。もう一つ、
これらは `signal_outcomes` / `verdict_outcomes` のような当否分類を持たない**生の
リターン**であり、ここから「勝率」を作るときは分類の閾値を自分で決めることになる。

## Issue #192 で実列になった値

JSON の中にしか無かった値が実列になった。`json_extract` を書く必要はもう無い。

| 列 | 意味 | NULL の意味 |
|---|---|---|
| `candidates.score` / `score_rsi_pullback` / `score_trend_quality` / `score_liquidity` / `score_atr_pct` | 複合スコアと当初の 4 成分 | 既存行は `metrics_json` からバックフィル済み。それでも NULL なら成分導入前の run |
| `candidates.score_pivot_proximity` / `score_rs_percentile` / `score_criteria_met` | 戦略別ランキング成分の加重後の値（Issue #251、既定重み 0.0） | **未記録**。成分が存在しなかった run は `metrics_json` にも無いのでバックフィルしていない。0.0（計測された寄与ゼロ）と読み替えないこと |
| `candidates.execution_state` / `execution_distance` | ランキングの実行状態（`READY` / `EXTENDED` など）と SMA50 からの ATR 距離 | **未記録**。どこにも永続化されていなかったので過去行は復元不能。`UNKNOWN`（距離が計算不能という測定結果）と読み替えないこと |
| `regime_snapshots.dd15_*` / `dd5_*` / `spy_close` / `spy_ema` / `vix_close` | 短い窓の distribution 件数とゲート入力（`dd_count_*` は従来どおり 25 セッション） | ゲート入力はバー欠損で評価不能だった run。バックフィル済みなので「列が無かった」ではない |
| `exposure_decisions.gate_verdict` / `dd_level` / `is_conservatively_downgraded` / `reduce_only_risk_multiplier` | 露出上限の判断根拠 | バックフィル済み |
| `verdict_reasons` / `verdict_reason_sources` | `verdicts.reasons_json` の正規化投影（`reasons_json` は引き続き記録の正） | バックフィル済み。`basis` の NULL は Issue #191 のタグが付く前に書かれた理由 |

```python
# 実行状態別に、その後のリターンはどう違ったか（DoD の 1 行集計）
research.universe_forward_returns().groupby("execution_state")[
    "forward_return_pct"
].mean()

# ソースを一つも引かなかった理由だけで proceed した銘柄
reasons = research.verdict_reasons()
uncited = reasons.groupby(["run_id", "symbol"])["source_id_count"].max() == 0

# 候補にはならなかったが、そのシグナルには当たっていた銘柄
research.signal_hits().groupby("signal_name")["symbol"].nunique()
```

`signal_hits()` は `run_id` キーの `signal_hits` テーブルを読む。旧 `signals`
テーブル（`run_date` キー）は同日の `dry_run` と `live` が衝突するため読まない。
2026-08 以前の run には `signal_hits` の行が無い（一方向の切断であり、
`signals` に残っている行も run に紐付けられない）。

## Issue #189 で増えた 2 つの台帳

「記録しなければ後から復元できない」値の蓄積。専用アクセサは置いていないので
`research.query()` から下の 2 ビュー（または実テーブル）を読む。

| ビュー / テーブル | 何が入るか | NULL の意味 |
|---|---|---|
| `v_retro_narrations` | 振り返り 1 回 × サプライズ 1 銘柄の敗因分類と叙述。当時の run・verdict を結合済み | `run_date` / `recommendation` が NULL＝その run の verdict 行が再 `collect` で消えた |
| `retro_sessions` | 取り込んだ振り返りそのもの（`window_start` / `input_digest` / `outcome_count` / `proposal_count`） | — |
| `v_run_configs` | run と、その run が実行された設定値（提案対象の 8 セクション） | `snapshot_hash` / `sections_json` が NULL＝**未記録**。台帳導入（2026-08）前の run であって「設定が空」ではない |

```python
# 同じ敗因分類が何回繰り返しているか（L2 定性ゲートの素材）
research.query(
    "SELECT failure_class, count(*) AS n, count(DISTINCT retro_as_of) AS sessions "
    "FROM v_retro_narrations GROUP BY 1 ORDER BY n DESC"
)

# 設定変更の前後で candidate の成績は動いたか
research.query(
    "SELECT c.config_hash, c.first_seen_run_date, avg(s.forward_return_pct) "
    "FROM v_verdict_scorecard s JOIN v_run_configs c USING (run_id) "
    "WHERE s.horizon_days = 20 GROUP BY 1, 2 ORDER BY 2"
)
```

`config_versions` の主キーは `runs.config_hash`（設定全体＋戦略の完全指紋）で、
`snapshot_hash` は提案対象になりうる 8 セクションだけのダイジェスト。通知や
スケジュールしか違わない 2 設定は `config_hash` が割れても `snapshot_hash` は
一致するので、比較窓を割るかどうかの判断には `snapshot_hash` を使うこと。
なお設定で層別すると必ずサンプルが小さくなる。差が出ても点推定であって、
`docs/08_architecture_review_2026-08.md` のとおり設定変更の根拠にはならない。

## 安全上の規約

- **接続は関数呼び出しの内側だけ**: 各関数はクエリごとに read-only 接続を開いて
  即閉じる。DuckDB のファイルロックは read-write プロセスと排他なので、生の
  `duckdb.connect()` を開いたまま保持するノートブックは（read-only でも）
  18:30 の無人日次実行をブロックする。必ずこのモジュール経由で読むこと。
- **書けない**: read-only 接続なので INSERT/UPDATE/DDL は失敗する。これは仕様。
  修正・取り込みは従来どおり `storage` リポジトリと各 CLI の責務。
- **古い DB**: ビュー導入前のファイルを読むと `ResearchError` がヒント付きで出る。
  `research.ensure_views()` を一度呼べば直る（次の日次実行でも自動作成される）。
- NULL の意味論はテーブルごとに異なる（上表）。特に `news_supply_*` の NULL を
  0/none と混同しないこと。スキーマの正本は `storage/schema.py`。
