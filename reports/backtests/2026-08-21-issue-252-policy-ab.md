# Issue #252 新ゲートの Policy A/B 証跡

これは受入れゲートではなく、Issue #252 の変更後に既存の Policy A/B を
再実行しようとした記録である。比較の基準は
[`2026-08-17-policy-ab-equity-basis.md`](2026-08-17-policy-ab-equity-basis.md) の
実行条件（2020-01-02〜2026-07-30、default、`none,regime,regime+risk`）とした。

## 実行記録

現行の候補キャッシュは、バー・ファンダメンタル入力の訂正後データと
`cache_key` が一致せず、候補を古いキャッシュから読み出すことができなかった。
そのため次のフル条件を一時DB上で開始した。

```bash
copilot-backtest --strategy default --start 2020-01-02 --end 2026-07-30 \
  --policy none,regime,regime+risk \
  --candidate-cache reports/backtests/2026-08-17-policy-ab-raw/candidate-cache-default.parquet \
  --db <temporary-copy>/copilot.duckdb \
  --output <temporary-output>/A-policy-ab.md
```

キャッシュ不一致後の候補再生成は、503銘柄の全期間スクリーニングへ進み、
限定30銘柄でも同じ候補生成工程が長時間化したため、共有 `data/` を変更しない
一時実行を中断した。中断したプロセスの途中値は保存・解釈していない。

したがって、このファイルには現行コードのフル期間 A/B 数値を捏造して載せない。
既存レポートの数値は #252 前の EMA50 / 旧Exposure仕様の歴史的比較であり、
現行仕様の成績証拠として再利用しない。現行配線の振る舞いは次で固定している。

- `tests/backtest/test_policy.py::TestRegimeGate::test_reduce_only_keeps_the_configured_trade_risk_budget`
- `tests/risk/test_checks.py::TestCheckSizing::test_reduce_only_is_a_label_and_preserves_trade_risk`
- `tests/regime/test_exposure.py` の6分岐・FTD・UNKNOWNケース

状態分布の非ゲート証跡は
[`2026-08-21-issue-252-state-shares.md`](../regime/2026-08-21-issue-252-state-shares.md)
に保存した。フルA/Bの再実行は、現行データに対する候補キャッシュを生成してから
別の計測作業として行う。
