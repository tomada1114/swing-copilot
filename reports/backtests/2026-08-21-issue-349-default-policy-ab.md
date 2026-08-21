# Backtest: default (2026-01-01 .. 2026-08-19) -- policy A/B

同一候補ストリームに対して none, regime+earnings を比較した。

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
| circuit_breaker | 0 (0d) | 0 (0d) |
| portfolio_heat | 0 (0d) | 0 (0d) |
| earnings | 0 (0d) | 1 (1d) |
| sector | 0 (0d) | 0 (0d) |
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
