# 戦略・設定バリアント比較バックテスト（2020-01-02 .. 2026-07-30）

低ボラバイアス是正フェーズで追加した 2 つのスイッチ
（`band_atr_multiple` / ランキング成分 `atr_pct`）を、既定値を変えずに
バックテストで比較した結果である。**採用（既定値の差し替え）は行っていない。**
この文書は判断材料であり、判断そのものではない。

## 0. この比較が完了しなかった理由（最重要）

**R1〜R5 および G1 はいずれも完走していない。** 数値欄の「未実行」は推定値を
伏せているのではなく、文字通り実行結果が存在しないという意味である
（推定・補完は行わない）。

実測した事実:

| 観測 | 値 |
|---|---|
| R1（default, 2020-01-02..2026-07-30）単独実行 | 40 分超で未完了 |
| R1〜R5 を専用 DB で 5 並列（8 コア機、各 ~95% CPU） | 40 分超でいずれも未完了 |
| 3 ヶ月窓（62 営業日）の較正 run | 10 分超で未完了 |

原因は `ScreeningPipeline` の計算量である。`symbol_bars()` は銘柄・日ごとに
バーフレーム全体へブールマスクを掛け直すため、1,640 営業日 × 507 銘柄 ×
94 万行で概算 8×10¹¹ 行比較になる。さらに `_ranking_metrics` が毎日・毎銘柄で
RSI/ATR/SMA を全履歴から再計算する。これは
`docs/goal-prompts/.../research.md` §4 が事前に「長期化で顕在化する性能ハザード」
として挙げていたもので、実際にこのフェーズの律速になった。

感応度グリッド G1 は 1 run の **25 倍**のコストであり、上記の単価では
実行可能な見込みが立たない。

### 必要な対処（次フェーズ）

バーを銘柄別に事前グルーピング（`dict[str, DataFrame]`、日付 index 化）して
`symbol_bars()` をスライスに置き換えれば、支配的コストは消える見込みである。
ただしこれはスクリーニングの共有コードであり、日次経路にも影響する。
**最適化前後で同一入力の `BacktestResult` が完全一致することをテストで固定してから**
でなければ入れられない（結果が変わる最適化は禁止）。その証明を伴う改修は
本 PR のスコープに収まらないため、意図的に着手していない。

この制約により、`band_atr_multiple` と `atr_pct` の**採用可否を裏付ける実測は
まだ存在しない**。両スイッチを既定無効のまま出しているのは、この空白を
埋めないまま既定を差し替えないためである。

## 実行条件

| 項目 | 値 |
|---|---|
| 期間 | 2020-01-02 .. 2026-07-30（1,640 営業日相当） |
| ユニバース | `config/universe_snapshot.csv`（503 銘柄）＋ SPY/QQQ/^VIX/^TNX |
| バー | 2019-01-02 .. 2026-07-30、507 銘柄 940,226 行（`copilot-backfill bars`） |
| ファンダ | 2019-01-01 .. 2026-07-29、498 銘柄 14,676 件（`copilot-backfill fundamentals`） |
| 初期資金 | $100,000 |

## 1. 主要指標比較（R1〜R5）

| 指標 | R1<br>default / 現行設定（ベースライン） | R2<br>default / band_atr_multiple: 2.0 | R3<br>default / R2 + atr_pct 重み 0.2 | R4<br>minervini_stage2 / 現行設定 | R5<br>vcp_breakout / 現行設定 |
|---|---:|---:|---:|---:|---:|
| trade_count | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |
| expectancy_per_trade | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |
| win_rate | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |
| profit_factor | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |
| avg_r_multiple | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |
| sharpe | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |
| max_drawdown_pct | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |
| final_equity | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |
| benchmark_final_equity | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |

### Exit 内訳

| 指標 | R1 | R2 | R3 | R4 | R5 |
|---|---:|---:|---:|---:|---:|
| stop | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |
| max_hold | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |
| end_of_backtest | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |
| max_hold binding rate | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |
| holding days (median) | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |
| holding days (p25 / p75) | 未実行 | 未実行 | 未実行 | 未実行 | 未実行 |

## 2. 感応度グリッド G1（R2 の設定、拡張済みレンジ）

**未実行**。1 run が 40 分超で完走しない以上、その 25 倍を要する 25 セルのグリッドは実行できない（第 0 節）。judge_grid の判定も存在しない。

## 3. 規則ベースの推奨

- **R2 採用可否: 判定不能** — R1 または R2 が未実行。

- **max_hold_days**: binding rate は R1 未実行 / R2 未実行。判定不能（binding rate を取得できていない）。

- **R3〜R5** は参考情報として事実のみを記載する。採用判断は人間が行う。

## 4. 付録: 各バリアントの設定差分

バリアント YAML はリポジトリにコミットしていない。以下の差分を
`config/settings.yaml` / `config/strategies.yaml` のコピーに適用すれば再現できる。

```diff
# R2 / R3 / G1 が使う settings.yaml の差分
  technical_signals:
    pullback:
-     band_atr_multiple: null
+     band_atr_multiple: 2.0
```

```diff
# R3 が使う strategies.yaml の差分（default 戦略の ranking）
  strategies:
    default:
      ranking:
        score_weights:
-         rsi_pullback: 0.5
+         rsi_pullback: 0.3
          trend_quality: 0.3
          liquidity: 0.2
+         atr_pct: 0.2
```

実行コマンド:

```bash
copilot-backtest --strategy default --start 2020-01-02 --end 2026-07-30
copilot-backtest --strategy default --start 2020-01-02 --end 2026-07-30 \
  --settings <R2 settings>
copilot-backtest --strategy default --start 2020-01-02 --end 2026-07-30 \
  --settings <R2 settings> --strategies <R3 strategies>
copilot-backtest --strategy minervini_stage2 --start 2020-01-02 --end 2026-07-30
copilot-backtest --strategy vcp_breakout --start 2020-01-02 --end 2026-07-30
copilot-backtest grid --strategy default --start 2020-01-02 --end 2026-07-30 \
  --settings <R2 settings>
```

## 5. 生存者バイアス注記

> This backtest applies one S&P 500 constituent snapshot to the entire
> period. It does not reconstruct day-by-day index membership; when
> historical membership is unavailable, the current universe is used.
> Removed or delisted symbols may be absent, overstating historical
> performance (survivorship bias).

上記は `backtest/engine.py::SURVIVORSHIP_BIAS_NOTE` の全文である。
現行ユニバースを全期間へ適用しているため、2020 年時点で S&P 500 に
含まれていなかった銘柄（後年採用組）が候補に混ざり、逆に期間中に
除外・上場廃止された銘柄は存在しない。この比較は**バリアント間の相対**を
読むためのもので、絶対的な期待収益の推定には使えない。
