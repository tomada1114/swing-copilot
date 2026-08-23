# Backtest: 初期逆指値アンカー統一（Issue #341）移行前後の比較

`trade_plan.entry_limit_atr_multiple`（`k`）0.0/0.5/1.0/1.5/2.0 の5点で、
移行前（HEAD 9a57c10）と移行後（ブランチ 0cae7b0）を同一候補ストリームで比較した。

## Reproduction metadata

- Before commit（移行前）: `9a57c10deeb44ada4f79dbafc76e750a2e3a771a`
- After commit（移行後）: `0cae7b0d1516d202c23b4aa6488a178021001718`
  （ブランチ `fix/341-entry-stop-anchor`）
- Command（`k`ごとに`--settings`だけを差し替えて実行、両commitで共通）:
  ```bash
  uv run copilot-backtest --strategy default --start 2026-01-01 --end 2026-08-21 \
    --limit 30 --db data/copilot.duckdb \
    --settings <settings-k{k}.yaml> \
    --candidate-cache <issue-341-stream.parquet> \
    --output <output.md>
  ```
  `<settings-k{k}.yaml>`は`config/settings.yaml`の`trade_plan.entry_limit_atr_multiple`
  だけを`k`へ差し替えたコピー。`--candidate-cache`は`backtest/candidate_stream.py`の
  `compute_cache_key`が`settings.trade_plan`をキーから意図的に除外しているため、
  5つの`k`にも両commitにも同一の候補ストリームが再利用され、スクリーニングは
  実質1回で済んでいる。
- Settings SHA-256（`trade_plan.entry_limit_atr_multiple`以外は`config/settings.yaml`と同一）:

  | k | settings SHA-256 |
  |---|---|
  | 0.0 | `c9931f69c409539e75eef5c632ab21928e7f6184cde0b1fae799a9b0c4822d08` |
  | 0.5 | `017328ea8b3916e056cd577c43af942f3468de68685b42c923637b4188eb7c06` |
  | 1.0 | `f17e94806df08406b3ee00a1ed0ffb3b749cb5ff66fb7d80bffbd2cacb02614f` |
  | 1.5 | `9088edc862e1f8b78ed912833c5b2df763ce39bbc1d7763d628f73d417da5ac8` |
  | 2.0 | `2bbb05b0a7b9a3a204ae61892fcb86f269196eb59c583ff1dada7bab111f5f3e` |

- Strategy definitions: `config/strategies.yaml` SHA-256
  `c87ec7bba63d02c880db31db43438f09eda4c3bc4f9dade9e1479171959f2d48`
- Input snapshot: object-storage generation `5`（`just data-pull`で取得、push はしていない）。
  `data/bars/year=2025/data.parquet` SHA-256
  `529cb03b7e0b82a05f89319f1de7f2a7d147f0f8716e6318ccc83a1af6d0c15a`、
  `data/bars/year=2026/data.parquet` SHA-256
  `a6f393b0559a08c52bd2a456037bc2c866d1f73eff9d4341484f63c709b2c9ac`
  （いずれもgeneration 5のリモートと一致、変更なし）。
  `data/copilot.duckdb` は測定完了後のローカル状態で SHA-256
  `b76c961956b56ed63b4a1fe71879c751a401521da27aa67d73335739737834cb`。
  **この値はリモートのgeneration 5と一致しない** — `copilot-backtest`が
  `Database(args.db)`を読み書きモードで開き`init_schema()`を呼ぶため
  （`backtest/cli.py`）、既に初期化済みのファイルに対しても書き込みロックを
  取得してバイト列を変える。行の内容は変わっていない（`data-status`は
  `変更=1`のみで欠落・追加なし）が、これは今回の変更が原因ではなく既存の
  挙動である。フォローアップとして`copilot-backtest`への読み取り専用モード
  追加を別途起票する。
- Simulation contract: initial cash `$100,000`; `trade_plan.exit_atr_multiple=2.5`,
  `exit_atr_period=14`, `max_hold_days=25`; `backtest.entry="next_open"`
  （既定のまま）; `backtest.sim_trade_risk_pct=0.01`, `sim_position_cap_pct=0.10`,
  `max_concurrent_positions=10`; commission `0.001`, slippage `0.001`,
  benchmark `SPY`
- ユニバース: 30/503 銘柄の決定論的サンプル（`default`戦略、2026-01-01..2026-08-21、
  `--limit 30`）。両commit・全`k`で同一の候補ストリームを再利用しているため
  ユニバース構成は完全に一致する。

## 結果

全ラン `trade_count=25`（`k`・commitによらず一定）。

| k | HEAD final_equity | ブランチ final_equity | HEAD avg_r_multiple | ブランチ avg_r_multiple | HEAD avg_invested_pct | ブランチ avg_invested_pct |
|---|---:|---:|---:|---:|---:|---:|
| 0.0 | $99,857.04 | $99,779.16 | -0.046 | -0.030 | 19.49% | 19.61% |
| 0.5 | $100,172.83 | $99,996.34 | -0.031 | -0.020 | 19.47% | 19.07% |
| 1.0 | $99,977.87 | $99,678.68 | -0.041 | -0.027 | 19.48% | 18.42% |
| 1.5 | $99,857.04 | $99,705.24 | -0.046 | -0.030 | 19.49% | 17.54% |
| 2.0 | $99,857.04 | $99,717.16 | -0.046 | -0.030 | 19.49% | 16.58% |

## 読み

1. **HEADでは`k=1.5`・`k=2.0`が`k=0.0`とバイト単位で同一の結果になる。**
   `_entry_execution_price`は`evaluate_entry_fill`で「始値 ≤ 指値なら
   `open × (1 + slip)`で約定」という規則を使うが、この30銘柄サンプルでは
   指値`close + k × ATR14`が十分広いとすべての候補で`open ≤ limit`が成立し、
   `next_open`互換アーム（無条件`open × (1 + slip)`）と実質同じ約定価格になる。
   移行前は`stop_price`も約定価格からの一定オフセット
   (`exit_atr_multiple × ATR14`)だったため、この場合`k`を上げても損益は
   `k=0.0`から一切動かなかった——**`k`の選択を測っているつもりで、実は何も
   測れていなかった**ことが実測で確認できる。
2. **ブランチでは`avg_invested_pct`が`k`の増加とともに単調に縮む
   （19.61% → 16.58%）。** これが今回の修正の狙いどおりの効果である。
   終値アンカーでは1株あたりリスクが`(exit_atr_multiple + k) × ATR14`へ
   正しく拡大するため、`sim_trade_risk_pct`一定のもとで株数（＝投資比率）が
   `k`に応じて縮む。移行前はこの効果が消えていた。
3. **`avg_r_multiple`はブランチの方が全`k`で改善している**
   （例: `k=0.0`で`-0.046` → `-0.030`）。これは損益の絶対額ではなく
   「1Rに対してどれだけ動いたか」の指標で、逆指値（分母のR）が終値基準の
   より正しい値に変わったことで、同じ値幅の損益がより小さいR-multipleとして
   計上されるようになったため。損益の実額（final_equity）は`k>0`のほとんどの
   セルでわずかに悪化しているが、これはサイジングが縮んだ結果の一部が
   実現益を取り損ねているためであり、「正しさ」と「儲かるかどうか」は
   別軸であることに注意。
4. **`k=0`（既定値）はHEAD/ブランチ間で`stop_price`の値自体は変わらない**
   （生産・台帳と同様、終値アンカーは元々`stop = close - 2.5×ATR14`）。
   `final_equity`の差（$99,857.04 → $99,779.16）は、`limit_price`基準の
   サイジングにより`k=0`でも`limit_price == close`となるため理論上は同一の
   はずだが、実測ではわずかに動いている——`_commit_entry`の
   `calc_position_size`呼び出しが従来`entry_price`（約定価格＝
   `open×(1+slip)`）を受け取っていたのに対し、修正後は`limit_price`
   （＝シグナル日終値、スリッページなし）を渡すため、`k=0`でも
   サイジング基準がわずかに変わる（スリッページ分だけ`limit_price`の方が
   `entry_price`よりわずかに小さい）。これは意図した仕様変更（受け入れ条件2
   「株数は`limit_price`基準」）どおりの副作用である。
5. **`TestGapStop::test_gap_below_initial_stop_on_fill_day_settles_with_both_side_costs`
   が検証する「約定日にギャップダウンして即stop決済される」シナリオは、
   この30銘柄サンプルでは発生していない**（HEAD/ブランチとも exit breakdown
   の内訳・件数は`k`ごとに完全一致——`stop`/`max_hold`/`end_of_backtest`の
   構成が同じ）。この不変条件はユニットテストでのみ確認されている。

## 適用したdocsの差し替え

`docs/04_detailed_design.md`と`docs/reference.md`の
`<!-- ISSUE-341-MEASUREMENT-PLACEHOLDER -->`を、上記の要約と本ファイルへの
参照に差し替えた。
