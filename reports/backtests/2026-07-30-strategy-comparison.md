# 戦略・設定バリアント比較バックテスト（2020-01-02 .. 2026-07-30）

低ボラバイアス是正フェーズで追加した 2 つのスイッチ
（`band_atr_multiple` / ランキング成分 `atr_pct`）を、既定値を変えずに
バックテストで比較した結果である。**採用（既定値の差し替え）は行っていない。**
この文書は判断材料であり、判断そのものではない。

## 0. 実行にあたって行ったエンジン最適化

当初、フル期間（1,640 営業日 x 507 銘柄）のバックテストは 1 run が 40 分を超えても
完走せず、比較そのものが実行不能だった。design.md D10 が「1 run が 45 分を超える
場合のみ」認めている最適化（銘柄別事前グルーピング・日付 index 化）を適用し、
**最適化前後で結果が完全一致することを実データで証明したうえで**採用した。

| 項目 | 値 |
|---|---|
| 検証コマンド | `--strategy default --start 2025-09-01 --end 2026-07-30 --limit 60`（同一 DB） |
| 最適化前 | 5:56.94 |
| 最適化後 | 1:05.81（**5.4 倍**） |
| 結果の一致 | レポート出力を `diff` して**差分なし**（trade_count 45 / final_equity $98,254.16） |

内訳は 3 点。(1) `symbol_bars` がフレーム全体へ銘柄・日ごとにブールマスクを 2 回
掛けていたのを、1 回だけ銘柄別にグルーピングして `searchsorted` で切り出す形にした。
(2) `ScreeningPipeline.run` が棄却理由の分類（レポート用で候補選定には影響しない）を
計算して捨てており、これがバックテスト全体の約半分を占めていた。(3) index を
`bars.attrs` に置くと pandas が `__finalize__` で attrs を deepcopy するため、
派生フレームごとにグルーピング全体が複製されてしまう。id ベースのキャッシュに変更した。

look-ahead が入らないことは `TestNoLookAheadFromUnslicedBars` が機械的に固定する
（フレームを丸ごと渡した場合と `as_of` で事前スライスした場合で候補が一致すること）。

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
| trade_count | 993 | 1013 | 1017 | 1068 | 1 |
| expectancy_per_trade | $12.65 | $35.37 | $34.84 | $67.70 | $836.08 |
| win_rate | 39.38% | 40.38% | 40.31% | 42.70% | 100.00% |
| profit_factor | 1.082 | 1.203 | 1.190 | 1.339 | N/A |
| avg_r_multiple | 0.033 | 0.060 | 0.062 | 0.115 | 2.400 |
| sharpe | 0.242 | 0.497 | 0.470 | 0.721 | 0.285 |
| max_drawdown_pct | 21.08% | 21.82% | 21.90% | 20.16% | 0.58% |
| final_equity | $112,562.96 | $135,834.77 | $135,430.26 | $172,300.94 | $100,836.08 |
| benchmark_final_equity | $250,155.32 | $250,155.32 | $250,155.32 | $250,155.32 | $250,155.32 |

### Exit 内訳

| 指標 | R1 | R2 | R3 | R4 | R5 |
|---|---:|---:|---:|---:|---:|
| stop | 707 | 719 | 724 | 780 | 1 |
| max_hold | 276 | 285 | 284 | 279 | 0 |
| end_of_backtest | 10 | 9 | 9 | 9 | 0 |
| max_hold binding rate | 27.79% | 28.13% | 27.93% | 26.12% | 0.00% |
| holding days (median) | 13.0 | 15.0 | 15.0 | 14.0 | 18.0 |
| holding days (p25 / p75) | 7.0 / 24.0 | 7.0 / 24.0 | 7.0 / 24.0 | 7.0 / 24.0 | 18.0 / 18.0 |

## 2. 感応度グリッド G1（R2 の設定、拡張済みレンジ）

**未実行。** 最適化後でもフル期間 1 run は 54 分（単独実行）かかり、グリッドは
その 25 セル分を逐次実行するため約 22 時間に相当する。数値を推定で埋めることは
しない（decisions.md F8）。したがって **judge_grid の spike/plateau 判定は存在しない**。

ただし、拡張したレンジ `(40,70,100,140,200)%` を読む価値があることは第 1 節が
示している: `max_hold` バインド率は R1〜R4 のいずれでも **26〜28%** であり、
事前に定めた「5% 未満なら感応なし」を大きく上回る。**`max_hold_days` は結果に
効いている**。旧レンジ（±20%）の MaxHold 列が全て同値だったのは、
`reports/backtests/2026-06-30-default-grid.md` が 14 ヶ月分のデータしか無く
トレードがほぼ発生していなかったためであって、パラメータが効かないからでは
なかった。

次フェーズでグリッドを現実的にする道筋は具体的にある。25 セルは
`exit_atr_multiple` と `max_hold_days` しか変えず、この 2 つは**エンジンの手仕舞い
判定にしか使われない**（screening は `settings.backtest.*` を参照しない）。
つまり 25 セルの候補列は同一であり、支配的コストである screening を 1 回に
まとめれば、グリッドは「1 run + 25 回の安価なエンジン走行」になる。
本 PR では D10 が認めた最適化の範囲を超えるため着手していない。

## 3. 規則ベースの推奨

判定に使う実測値: R1 trade_count=993, R2 trade_count=1013。

- **R2 採用可否: 採用を推奨しない** — trade_count 条件（R2 >= R1 x 1.30 = 1290.9）: 満たさない（実測 1013）。expectancy 条件（R2 の平均トレード収益率 0.8924% >= R1 の 0.5309% - 1SE 0.2941% = 0.2368%）: 満たす。

- **max_hold_days**: binding rate は R1 27.79% / R2 28.13%。binding rate が 5% 以上のセットがあるため、max_hold_days は**結果に効いている**。グリッドの時間軸を読む価値がある。

- **規則と実測のずれについて（事実の記載であり、規則の解釈変更ではない）**:
  上の判定は事前に確定した規則をそのまま適用した結果であり、曲げていない。
  一方で実測は規則が想定していなかった形をしている——R2 は候補数をほとんど
  増やさない（+2.0%）が、expectancy は $12.65 → $35.37（約 2.8 倍）、
  Sharpe は 0.242 → 0.497（約 2.1 倍）、profit factor は 1.082 → 1.203 に改善する。
  事前規則は「ATR 正規化帯は通過銘柄を増やすはずだ」という前提で trade_count に
  +30% の条件を置いていたが、実際に起きたのは**銘柄数の増加ではなく質の入れ替え**
  だった。この前提のずれ自体が判断材料であり、どちらに読むかは人間の裁量に属する。
  規則を後から書き換えて「採用を推奨」に変えることはしない。

- **R3 は R2 とほぼ同一**（expectancy $34.84 vs $35.37、trade_count 1017 vs 1013）。
  atr_pct 重み 0.2 を足しても R2 からの上積みは観測されない。

- **R4（minervini_stage2）は全構成で最良**（expectancy $67.70、Sharpe 0.721、
  final_equity $172,300.94、最大 DD も最小の 20.16%）。ただし本フェーズは
  default 戦略のスイッチ評価が目的であり、戦略の乗り換えは別判断である。

- **R5（vcp_breakout）は 6.5 年で 1 トレードのみ**。統計的な評価は不可能であり、
  勝率 100% や Sharpe 0.285 といった数値に意味は無い。

- **全構成がベンチマーク（SPY 買い持ち $250,155.32）を下回る**。最良の R4 でも
  $172,300.94 であり、この期間・このユニバースにおいてはスイング戦略そのものが
  買い持ちに勝てていない。生存者バイアス（第 5 節）を考えると実際の差はさらに
  不利な側にある。スイッチの採否以前に読むべき事実として記載する。

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
