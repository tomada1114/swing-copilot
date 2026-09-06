"""Tests for `backtest/render.py`'s pure rendering.

Moved out of `tests/backtest/test_cli.py` (Issue #399) alongside the
`backtest/cli.py` -> `backtest/render.py` split: every class/helper here
calls a `render_*` function directly with an in-memory `BacktestResult`/
`ReportMeta`, with no argparse, DuckDB, or filesystem I/O. Test content is
unchanged from its previous home in test_cli.py.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import TYPE_CHECKING

from swing_copilot.backtest.engine import BacktestResult, Trade
from swing_copilot.backtest.metrics import (
    ENTRY_BLOCK_REGIME,
    entry_block_breakdown,
    exit_reason_breakdown,
    holding_days_stats,
    max_hold_binding_rate,
)
from swing_copilot.backtest.render import (
    ReportMeta,
    render_entry_grid_markdown,
    render_entry_grid_terminal,
    render_grid_markdown,
    render_grid_terminal,
    render_markdown,
    render_markdown_comparison,
    render_policy_comparison_markdown,
    render_policy_comparison_terminal,
    render_terminal,
    render_terminal_comparison,
)
from swing_copilot.backtest.sensitivity import (
    ATR_MULTIPLIER_PCT_GRID,
    ENTRY_LIMIT_ATR_MULTIPLE_GRID,
    MAX_HOLD_PCT_GRID,
    PLATEAU,
    SPIKE,
    GridCell,
    SensitivityGridResult,
)
from swing_copilot.universe_sampling import UniverseSample

if TYPE_CHECKING:
    from collections.abc import Sequence

_D0 = date(2027, 1, 1)
_D1 = date(2027, 1, 2)


def _result(
    *,
    trades: tuple[Trade, ...] = (),
    warnings: tuple[str, ...] = (),
    blocked: int = 4,
) -> BacktestResult:
    equity_curve = ((_D0, 100_000.0), (_D1, 101_000.0))
    return BacktestResult(
        trades=trades,
        equity_curve=equity_curve,
        benchmark_curve=equity_curve,
        final_equity=101_000.0,
        benchmark_final_equity=100_500.0,
        trade_count=len(trades),
        sharpe=1.234,
        max_drawdown_pct=0.05,
        win_rate=0.6 if trades else None,
        profit_factor=1.8 if trades else None,
        expectancy_per_trade=80.0 if trades else None,
        avg_r_multiple=0.5 if trades else None,
        warnings=warnings,
        exit_reason_counts=tuple(exit_reason_breakdown(trades).items()),
        max_hold_binding_rate=max_hold_binding_rate(trades),
        holding_days=holding_days_stats(trades),
        entry_block_counts=tuple(
            entry_block_breakdown({ENTRY_BLOCK_REGIME: blocked}).items()
        ),
        entry_block_days=tuple(
            entry_block_breakdown({ENTRY_BLOCK_REGIME: min(blocked, 1)}).items()
        ),
        avg_invested_pct=0.42,
        max_concurrent_reached=3,
    )


class TestRenderTerminal:
    def test_includes_metrics_warnings_and_missing_symbols(self):
        trade = Trade(
            symbol="AAA",
            entry_date=_D0,
            entry_price=100.0,
            exit_date=_D1,
            exit_price=110.0,
            shares=10,
            exit_reason="stop",
            initial_stop_price=90.0,
        )
        result = _result(trades=(trade,), warnings=("予備的（trade_count=5）",))
        meta = _meta(missing_data_symbols=["ZZZ"])

        text = render_terminal(result, meta)

        assert "trade_count" in text
        assert "1" in text
        assert "AAA" in text
        assert "予備的" in text
        assert "データ不足のためスキップ: ZZZ" in text
        assert "survivorship" in text.lower()

    def test_no_trades_shows_placeholder(self):
        result = _result()
        meta = _meta()

        text = render_terminal(result, meta)

        assert "Trades: (none)" in text

    def test_empty_equity_curve_shows_no_trading_days(self):
        result = dataclasses.replace(_result(), equity_curve=())
        meta = _meta()

        text = render_terminal(result, meta)

        assert "Equity curve: (no trading days)" in text


class TestRenderMarkdown:
    def test_includes_all_sections(self):
        trade = Trade(
            symbol="AAA",
            entry_date=_D0,
            entry_price=100.0,
            exit_date=_D1,
            exit_price=110.0,
            shares=10,
            exit_reason="stop",
            initial_stop_price=90.0,
        )
        result = _result(trades=(trade,), warnings=("統計的に不十分",))
        meta = _meta(missing_data_symbols=["ZZZ"])

        text = render_markdown(result, meta)

        assert "# Backtest: default" in text
        assert "## Metrics" in text
        assert "## Warnings" in text
        assert "統計的に不十分" in text
        assert "## Data quality" in text
        assert "データ不足のためスキップ: ZZZ" in text
        assert "## Trades" in text
        assert "| AAA |" in text
        assert "## Survivorship bias" in text

    def test_no_trades_shows_placeholder(self):
        result = _result()
        meta = _meta()

        text = render_markdown(result, meta)

        assert "(no trades)" in text
        assert "## Warnings" not in text
        assert "## Data quality" not in text


class TestRenderComparison:
    def test_terminal_comparison_shows_both_scenarios_and_labeled_warnings(self):
        normal = _result(warnings=("normal warning",))
        pessimistic = dataclasses.replace(
            _result(warnings=("pessimistic warning",)), final_equity=90_000.0
        )
        meta = _meta(missing_data_symbols=["ZZZ"])

        text = render_terminal_comparison(normal, pessimistic, meta)

        assert "normal vs pessimistic" in text
        assert "normal: normal warning" in text
        assert "pessimistic: pessimistic warning" in text
        assert "データ不足のためスキップ: ZZZ" in text
        assert "101,000.00" in text
        assert "90,000.00" in text

    def test_terminal_comparison_with_no_missing_data_omits_the_skip_line(self):
        normal = _result()
        pessimistic = _result()
        meta = _meta()

        text = render_terminal_comparison(normal, pessimistic, meta)

        assert "データ不足のためスキップ" not in text

    def test_markdown_comparison_shows_both_scenarios_as_a_diff_table(self):
        normal = _result(warnings=("normal warning",))
        pessimistic = dataclasses.replace(
            _result(warnings=("pessimistic warning",)), final_equity=90_000.0
        )
        meta = _meta()

        text = render_markdown_comparison(normal, pessimistic, meta)

        assert "normal vs pessimistic" in text
        assert "| Metric | Normal (x1.0) | Pessimistic |" in text
        assert "- normal: normal warning" in text
        assert "- pessimistic: pessimistic warning" in text
        assert "101,000.00" in text
        assert "90,000.00" in text

    def test_no_warnings_omits_warnings_section(self):
        normal = _result()
        pessimistic = _result()
        meta = _meta()

        text = render_markdown_comparison(normal, pessimistic, meta)

        assert "## Warnings" not in text


def _grid_result(
    *,
    verdict: str = SPIKE,
    cell_overrides: dict[tuple[int, int], GridCell] | None = None,
) -> SensitivityGridResult:
    cell_overrides = cell_overrides or {}
    cells = tuple(
        cell_overrides.get(
            (atr_pct, max_hold_pct),
            GridCell(
                atr_multiplier_pct=atr_pct,
                max_hold_pct=max_hold_pct,
                expectancy_per_trade=50.0,
                trade_count=50,
            ),
        )
        for atr_pct in ATR_MULTIPLIER_PCT_GRID
        for max_hold_pct in MAX_HOLD_PCT_GRID
    )
    label = "スパイク（過学習疑い）" if verdict == SPIKE else "プラトー（頑健）"
    return SensitivityGridResult(cells=cells, verdict=verdict, verdict_label=label)


class TestRenderGrid:
    def test_terminal_shows_verdict_matrix_and_gray_marker(self):
        gray_cell = GridCell(
            atr_multiplier_pct=50,
            max_hold_pct=40,
            expectancy_per_trade=1.0,
            trade_count=5,
        )
        grid = _grid_result(cell_overrides={(50, 40): gray_cell})
        meta = _meta()

        text = render_grid_terminal(grid, meta, gray_threshold=30)

        assert "スパイク（過学習疑い）" in text
        assert "$50.00" in text
        assert "*" in text
        assert "灰色扱い" in text

    def test_markdown_shows_matrix_as_a_table_with_verdict(self):
        grid = _grid_result(verdict=PLATEAU)
        meta = _meta(missing_data_symbols=["ZZZ"])

        text = render_grid_markdown(grid, meta, gray_threshold=30)

        assert "Verdict: プラトー（頑健）" in text
        assert "| ATR% \\ MaxHold% |" in text
        assert "データ不足のためスキップ: ZZZ" in text

    def test_markdown_with_no_missing_data_omits_data_quality_section(self):
        grid = _grid_result(verdict=PLATEAU)
        meta = _meta()

        text = render_grid_markdown(grid, meta, gray_threshold=30)

        assert "## Data quality" not in text


def _entry_grid_results(
    *, warnings: tuple[str, ...] = ()
) -> list[tuple[float, BacktestResult]]:
    return [
        (k_value, _result(warnings=warnings))
        for k_value in ENTRY_LIMIT_ATR_MULTIPLE_GRID
    ]


class TestRenderEntryGrid:
    def test_terminal_shows_k_values_and_data_quality_warning(self):
        text = render_entry_grid_terminal(
            _entry_grid_results(warnings=("entry warning",)),
            _meta(missing_data_symbols=["ZZZ"]),
        )

        assert "copilot-backtest entry-grid" in text
        assert "entry_limit_atr_multiple" in text
        assert "0.0" in text
        assert "2.0" in text
        assert "entry warning" in text
        assert "データ不足のためスキップ: ZZZ" in text

    def test_markdown_shows_all_k_values_and_warnings(self):
        text = render_entry_grid_markdown(
            _entry_grid_results(warnings=("entry warning",)),
            _meta(missing_data_symbols=["ZZZ"]),
        )

        assert "# Backtest entry-limit sensitivity grid: default" in text
        assert "| k (ATR multiple) |" in text
        assert "| 0.0 |" in text
        assert "| 2.0 |" in text
        assert "## Warnings" in text
        assert "entry warning" in text
        assert "データ不足のためスキップ: ZZZ" in text

    def test_markdown_without_warnings_or_missing_data_omits_optional_sections(self):
        text = render_entry_grid_markdown(_entry_grid_results(), _meta())

        assert "## Warnings" not in text
        assert "## Data quality" not in text


class TestUniverseSamplingIsRendered:
    """Issue #194: no report may present a `--limit` sample as a full run."""

    def _sampled_meta(self) -> ReportMeta:
        return _meta(
            universe_sample=_sample(
                ("AAA", "BBB"), universe_size=10, is_stratified_sample=True
            )
        )

    def _rendered(self) -> list[str]:
        meta = self._sampled_meta()
        return [
            render_terminal(_result(), meta),
            render_markdown(_result(), meta),
            render_terminal_comparison(_result(), _result(), meta),
            render_markdown_comparison(_result(), _result(), meta),
            render_policy_comparison_terminal([("none", _result())], meta),
            render_policy_comparison_markdown([("none", _result())], meta),
            render_grid_terminal(_grid_result(), meta, gray_threshold=30),
            render_grid_markdown(_grid_result(), meta, gray_threshold=30),
            render_entry_grid_terminal(_entry_grid_results(), meta),
            render_entry_grid_markdown(_entry_grid_results(), meta),
        ]

    def test_every_report_states_the_method_and_the_composition(self):
        for text in self._rendered():
            assert "2/10 銘柄の決定論的サンプル" in text
            assert "セクター構成: Information Technology 2" in text

    def test_a_full_universe_run_says_so_instead(self):
        text = render_markdown(_result(), _meta())

        assert "全 1 銘柄（--limit 指定なし）" in text
        assert "決定論的サンプル" not in text


def _sample(
    symbols: tuple[str, ...] = ("AAA",),
    *,
    universe_size: int | None = None,
    is_stratified_sample: bool = False,
) -> UniverseSample:
    return UniverseSample(
        symbols=symbols,
        universe_size=universe_size if universe_size is not None else len(symbols),
        is_stratified_sample=is_stratified_sample,
        sector_counts=(("Information Technology", len(symbols)),),
    )


def _meta(
    *,
    missing_data_symbols: Sequence[str] = (),
    universe_sample: UniverseSample | None = None,
) -> ReportMeta:
    return ReportMeta(
        strategy="default",
        start=_D0,
        end=_D1,
        missing_data_symbols=list(missing_data_symbols),
        universe_sample=universe_sample or _sample(),
    )


def _exit_trade(reason: str, days_held: int) -> Trade:
    return Trade(
        symbol="AAA",
        entry_date=_D0,
        entry_price=100.0,
        exit_date=_D1,
        exit_price=104.0,
        shares=10,
        exit_reason=reason,
        days_held=days_held,
    )


class TestExitBreakdownRendering:
    _TRADES = (
        _exit_trade("stop", 3),
        _exit_trade("stop", 5),
        _exit_trade("max_hold", 25),
        _exit_trade("end_of_backtest", 7),
    )

    def test_markdown_has_an_exit_breakdown_section_with_every_reason(self):
        markdown = render_markdown(_result(trades=self._TRADES), _meta())

        assert "## Exit breakdown" in markdown
        assert "| stop | 2 |" in markdown
        assert "| max_hold | 1 |" in markdown
        assert "| end_of_backtest | 1 |" in markdown

    def test_markdown_reports_the_max_hold_binding_rate(self):
        markdown = render_markdown(_result(trades=self._TRADES), _meta())

        assert "| max_hold binding rate | 25.00% |" in markdown

    def test_markdown_reports_holding_day_quartiles(self):
        markdown = render_markdown(_result(trades=self._TRADES), _meta())

        # Sorted holding days 3, 5, 7, 25 -> p25 4.5, median 6.0, p75 11.5
        assert "| holding days (median) | 6.0 |" in markdown
        assert "| holding days (p25 / p75) | 4.5 / 11.5 |" in markdown

    def test_markdown_marks_every_exit_statistic_unavailable_without_trades(self):
        markdown = render_markdown(_result(), _meta())

        assert "## Exit breakdown" in markdown
        assert "| max_hold binding rate | N/A |" in markdown
        assert "| holding days (median) | N/A |" in markdown

    def test_terminal_renders_the_exit_breakdown_table(self):
        text = render_terminal(_result(trades=self._TRADES), _meta())

        assert "Exit breakdown" in text
        assert "max_hold binding rate" in text

    def test_pessimistic_comparison_renders_the_exit_breakdown_for_both(self):
        # A higher slippage assumption is exactly where the stop-vs-max_hold
        # split matters, so the comparison report must not drop it.
        normal = _result(trades=self._TRADES)
        pessimistic = _result(trades=(_exit_trade("stop", 3),))

        text = render_terminal_comparison(normal, pessimistic, _meta())
        markdown = render_markdown_comparison(normal, pessimistic, _meta())

        assert "Exit breakdown: normal vs pessimistic" in text
        assert "## Exit breakdown" in markdown
        assert "| Exit | Normal (x1.0) | Pessimistic |" in markdown
        assert "| stop | 2 | 1 |" in markdown

    def test_a_reason_only_one_scenario_produced_renders_as_zero(self):
        # `max_hold` never fires in the pessimistic run here. It must still
        # occupy a row with an explicit 0, so the reader can tell "the higher
        # slippage stopped everything out first" from "this scenario's report
        # simply omits the reason".
        normal = _result(trades=self._TRADES)
        pessimistic = _result(trades=(_exit_trade("stop", 3),))

        markdown = render_markdown_comparison(normal, pessimistic, _meta())

        assert "| max_hold | 1 | 0 |" in markdown


class TestSingleArmMarkdownIsPinned:
    """Issue #216 extended the multi-arm renderer only.

    `reports/backtests/*.md` are tracked records read long after the run --
    `2026-08-17-policy-ab-equity-basis.md` is Issue #200's canonical one -- so
    the single-arm report is pinned character-for-character rather than by
    section name: a reordered section or a re-worded label would silently make
    the archive inconsistent with what the tool now emits.
    """

    _EXPECTED = """\
# Backtest: default (2027-01-01 .. 2027-01-02)

ユニバース: 全 1 銘柄（--limit 指定なし）
セクター構成: Information Technology 1

## Metrics

| Metric | Value |
|---|---:|
| trade_count | 4 |
| sharpe | 1.234 |
| max_drawdown_pct | 5.00% |
| win_rate | 60.00% |
| profit_factor | 1.800 |
| expectancy_per_trade | $80.00 |
| avg_r_multiple | 0.500 |
| avg_invested_pct | 42.00% |
| max_concurrent_reached | 3 |
| final_equity | $101,000.00 |
| benchmark_final_equity | $100,500.00 |

## Exit breakdown

| Exit | Value |
|---|---:|
| stop | 2 |
| max_hold | 1 |
| end_of_backtest | 1 |
| max_hold binding rate | 25.00% |
| holding days (median) | 6.0 |
| holding days (p25 / p75) | 4.5 / 11.5 |

## Entry blocks

候補件数（発動セッション数）

| Reason | Value |
|---|---:|
| regime | 4 (1d) |
| earnings | 0 (0d) |
| not_calculable | 0 (0d) |
| max_concurrent | 0 (0d) |
| already_held | 0 (0d) |
| missing_data | 0 (0d) |
| limit_not_reached | 0 (0d) |
| invalid_stop | 0 (0d) |
| zero_shares | 0 (0d) |
| insufficient_cash | 0 (0d) |

## Warnings

- 低サンプル

## Data quality

データ不足のためスキップ: BBB

## Equity curve summary

Equity curve: 2027-01-01=100,000.00 -> 2027-01-02=101,000.00
  Peak: 2027-01-02=101,000.00
  Trough: 2027-01-01=100,000.00

## Trades

| Symbol | Entry date | Entry | Exit date | Exit | Shares | PnL | Reason |
|---|---|---:|---|---:|---:|---:|---|
| AAA | 2027-01-01 | 100.00 | 2027-01-02 | 104.00 | 10 | 40.00 | stop |
| AAA | 2027-01-01 | 100.00 | 2027-01-02 | 104.00 | 10 | 40.00 | stop |
| AAA | 2027-01-01 | 100.00 | 2027-01-02 | 104.00 | 10 | 40.00 | max_hold |
| AAA | 2027-01-01 | 100.00 | 2027-01-02 | 104.00 | 10 | 40.00 | end_of_backtest |

## Survivorship bias

This backtest applies one S&P 500 constituent snapshot to the entire period. It does not reconstruct day-by-day index membership; when historical membership is unavailable, the current universe is used. Removed or delisted symbols may be absent, overstating historical performance (survivorship bias).
"""

    def test_markdown_is_unchanged_character_for_character(self):
        result = _result(
            trades=(
                _exit_trade("stop", 3),
                _exit_trade("stop", 5),
                _exit_trade("max_hold", 25),
                _exit_trade("end_of_backtest", 7),
            ),
            warnings=("低サンプル",),
        )

        markdown = render_markdown(result, _meta(missing_data_symbols=["BBB"]))

        assert markdown == self._EXPECTED


class TestRenderPolicyComparison:
    _META = _meta(missing_data_symbols=["BBB"])

    @staticmethod
    def _arms() -> list[tuple[str, BacktestResult]]:
        return [
            ("none", _result(blocked=0)),
            ("regime", _result(warnings=("低サンプル",), blocked=7)),
        ]

    def test_terminal_shows_one_column_per_arm(self):
        text = render_policy_comparison_terminal(self._arms(), self._META)

        assert "none" in text
        assert "regime" in text
        assert "avg_invested_pct" in text
        assert "Entry blocks by policy" in text

    def test_markdown_reports_each_arms_block_counts_and_warnings(self):
        text = render_policy_comparison_markdown(self._arms(), self._META)

        assert "-- policy A/B" in text
        assert "| Metric | none | regime |" in text
        assert "| regime | 0 (0d) | 7 (1d) |" in text
        assert "- regime: 低サンプル" in text
        assert "データ不足のためスキップ: BBB" in text

    def test_terminal_omits_the_data_quality_note_when_nothing_was_skipped(self):
        text = render_policy_comparison_terminal([("none", _result())], _meta())

        assert "データ不足のためスキップ" not in text

    def test_markdown_omits_the_warning_section_when_no_arm_warns(self):
        text = render_policy_comparison_markdown([("none", _result())], _meta())

        assert "## Warnings" not in text
        assert "## Data quality" not in text


class TestPolicyComparisonExitAndEquitySections:
    """Issue #216: the A/B must say how each arm exited and when it drew down.

    Without these sections both questions are answerable only by a 40-56 minute
    single-arm rerun of the same configuration, which is exactly what Issue
    #200 / PR #215 had to leave unanswered.
    """

    _TRADES = (
        _exit_trade("stop", 3),
        _exit_trade("stop", 5),
        _exit_trade("max_hold", 25),
        _exit_trade("end_of_backtest", 7),
    )
    _D2 = date(2027, 1, 3)

    @classmethod
    def _arms(cls) -> list[tuple[str, BacktestResult]]:
        # The regime arm gets its own curve: the whole point of the section is
        # that two arms can peak and trough on different dates.
        regime = dataclasses.replace(
            _result(trades=(_exit_trade("stop", 3),)),
            equity_curve=((_D0, 100_000.0), (_D1, 90_000.0), (cls._D2, 105_000.0)),
        )
        return [("none", _result(trades=cls._TRADES)), ("regime", regime)]

    def test_markdown_breaks_the_exits_down_per_arm(self):
        text = render_policy_comparison_markdown(self._arms(), _meta())

        assert "## Exit breakdown" in text
        assert "| Exit | none | regime |" in text
        assert "| stop | 2 | 1 |" in text
        assert "| end_of_backtest | 1 | 0 |" in text
        assert "| max_hold binding rate | 25.00% | 0.00% |" in text

    def test_markdown_keeps_a_reason_only_one_arm_produced_as_an_explicit_zero(self):
        # A gate that never lets a position reach `max_hold` must read as 0,
        # not as a missing row indistinguishable from "not reported".
        text = render_policy_comparison_markdown(self._arms(), _meta())

        assert "| max_hold | 1 | 0 |" in text

    def test_markdown_reports_holding_day_quantiles_per_arm(self):
        text = render_policy_comparison_markdown(self._arms(), _meta())

        assert "| holding days (median) | 6.0 | 3.0 |" in text
        assert "| holding days (p25 / p75) | 4.5 / 11.5 | 3.0 / 3.0 |" in text

    def test_markdown_summarizes_each_arms_equity_curve(self):
        text = render_policy_comparison_markdown(self._arms(), _meta())

        assert "## Equity curve summary" in text
        assert "| Point | none | regime |" in text
        assert "| first | 2027-01-01=100,000.00 | 2027-01-01=100,000.00 |" in text
        assert "| peak | 2027-01-02=101,000.00 | 2027-01-03=105,000.00 |" in text
        assert "| trough | 2027-01-01=100,000.00 | 2027-01-02=90,000.00 |" in text

    def test_markdown_marks_an_arm_without_trading_days_as_unavailable(self):
        arms = [
            ("none", _result()),
            ("regime", dataclasses.replace(_result(), equity_curve=())),
        ]

        text = render_policy_comparison_markdown(arms, _meta())

        assert "| first | 2027-01-01=100,000.00 | N/A |" in text
        assert "| peak | 2027-01-02=101,000.00 | N/A |" in text
        assert "| trough | 2027-01-01=100,000.00 | N/A |" in text

    def test_terminal_renders_both_sections_as_tables(self):
        text = render_policy_comparison_terminal(self._arms(), _meta())

        assert "Exit breakdown by policy" in text
        assert "max_hold binding rate" in text
        assert "Equity curve summary by policy" in text
        assert "trough" in text
