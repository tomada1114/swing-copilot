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
