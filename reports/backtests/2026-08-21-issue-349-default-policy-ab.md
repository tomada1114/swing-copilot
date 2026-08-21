# Backtest: default (2026-01-01 .. 2026-08-19) -- policy A/B

同一候補ストリームに対して none, regime+earnings を比較した。

## Reproduction metadata

- Source commit: `da1bf29a2ca0c3649320838c1391e4cc0bd4af7e`（測定時点。後続の文書修正は `5126661cb74d725f68020fe344144f7d54a5dbad`）
- Command: `uv run copilot-backtest --strategy default --start 2026-01-01 --end 2026-08-19 --limit 30 --policy none,regime+earnings --db data/copilot.duckdb --output reports/backtests/2026-08-21-issue-349-default-policy-ab.md`
- Settings: `config/settings.yaml` SHA-256 `b4e450de01c03803b334e56cd999d2cb56fd788ea7ddb997f9753a7492d2ec49`
- Strategy definitions: `config/strategies.yaml` SHA-256 `c87ec7bba63d02c880db31db43438f09eda4c3bc4f9dade9e1479171959f2d48`
- Input snapshot: object-storage generation `3`; `data/copilot.duckdb` SHA-256 `8f3cf1448e22d036a7ea76235d01cd01581a2c638da0a470072595fef24b9798`; `data/bars/` file-manifest SHA-256 `189edbe63aba869ec0f77999794379526b64756dc3eb82fec33f4ab2f0b1eef9`
- Simulation contract: initial cash `$100,000`; `trade_plan.entry_limit_atr_multiple=0.0`, `exit_atr_multiple=2.5`, `exit_atr_period=14`, `max_hold_days=25`; `backtest.sim_trade_risk_pct=0.01`, `sim_position_cap_pct=0.10`, `max_concurrent_positions=10`; commission `0.001`, slippage `0.001`, benchmark `SPY`
- The report was measured on the 30-symbol deterministic sample shown below; both policy arms reused one candidate stream.

ユニバース: 30/503 銘柄の決定論的サンプル（gics_sector 比例配分 + blake2b ハッシュ順、シード固定・再現可能）
セクター構成: Communication Services 1, Consumer Discretionary 3, Consumer Staples 2, Energy 1, Financials 5, Health Care 4, Industrials 5, Information Technology 4, Materials 1, Real Estate 2, Utilities 2

## Metrics

| Metric | none | regime+earnings |
|---|---:|---:|
| trade_count | 23 | 23 |
| sharpe | -0.030 | 0.083 |
| max_drawdown_pct | 2.47% | 2.47% |
| win_rate | 39.13% | 43.48% |
| profit_factor | 0.973 | 1.033 |
| expectancy_per_trade | $-6.03 | $7.04 |
| avg_r_multiple | -0.053 | -0.038 |
| avg_invested_pct | 19.17% | 19.10% |
| max_concurrent_reached | 4 | 4 |
| final_equity | $99,861.28 | $100,161.97 |
| benchmark_final_equity | $113,156.71 | $113,156.71 |

## Delta summary (regime+earnings minus none)

| Metric | Delta |
|---|---:|
| trade_count | 0 |
| win_rate | +4.35 pp |
| expectancy_per_trade | +$13.07 |
| sharpe | +0.113 |
| final_equity | +$300.69 |
| earnings blocks | +1 candidate / +1 session |

この期間は23トレードで、最低30件の統計閾値に達しない。したがって差分は
設定変更や投資判断の根拠ではなく、実測したシミュレーションの記録である。

## Exit breakdown

| Exit | none | regime+earnings |
|---|---:|---:|
| stop | 16 | 16 |
| max_hold | 4 | 4 |
| end_of_backtest | 3 | 3 |
| max_hold binding rate | 17.39% | 17.39% |
| holding days (median) | 13.0 | 13.0 |
| holding days (p25 / p75) | 6.0 / 18.0 | 6.0 / 18.0 |

## Entry blocks

候補件数（発動セッション数）

| Reason | none | regime+earnings |
|---|---:|---:|
| regime | 0 (0d) | 0 (0d) |
| earnings | 0 (0d) | 1 (1d) |
| not_calculable | 0 (0d) | 0 (0d) |
| max_concurrent | 0 (0d) | 0 (0d) |
| already_held | 72 (54d) | 71 (53d) |
| missing_data | 0 (0d) | 0 (0d) |
| limit_not_reached | 0 (0d) | 0 (0d) |
| invalid_stop | 0 (0d) | 0 (0d) |
| zero_shares | 0 (0d) | 0 (0d) |
| insufficient_cash | 0 (0d) | 0 (0d) |

## Equity curve summary

| Point | none | regime+earnings |
|---|---:|---:|
| first | 2026-01-02=100,000.00 | 2026-01-02=100,000.00 |
| peak | 2026-08-14=100,584.51 | 2026-08-14=100,885.20 |
| trough | 2026-06-01=97,532.13 | 2026-06-01=97,532.13 |

## Warnings

- none: 統計的に不十分（trade_count=23、最低30件、推奨100件以上）
- regime+earnings: 統計的に不十分（trade_count=23、最低30件、推奨100件以上）

## Survivorship bias

This backtest applies one S&P 500 constituent snapshot to the entire period. It does not reconstruct day-by-day index membership; when historical membership is unavailable, the current universe is used. Removed or delisted symbols may be absent, overstating historical performance (survivorship bias).
