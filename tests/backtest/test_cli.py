"""Tests for `copilot-backtest`'s CLI parsing, rendering, and composition (P2-08)."""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import pytest
import yaml

from swing_copilot.backtest import cli as cli_module
from swing_copilot.backtest.candidate_stream import generate_candidate_stream
from swing_copilot.backtest.cli import (
    DEFAULT_SETTINGS_PATH,
    DEFAULT_STRATEGIES_PATH,
    BacktestCliError,
    ReportMeta,
    UniverseSample,
    _atomic_write,
    _compose_dependencies,
    _grid_output_path,
    _missing_data_symbols,
    _output_path,
    _parse_args,
    _select_symbols,
    _validate_args,
    main,
    render_grid_markdown,
    render_grid_terminal,
    render_markdown,
    render_markdown_comparison,
    render_policy_comparison_markdown,
    render_policy_comparison_terminal,
    render_terminal,
    render_terminal_comparison,
)
from swing_copilot.backtest.engine import BacktestResult, Trade
from swing_copilot.backtest.metrics import (
    ENTRY_BLOCK_REGIME,
    entry_block_breakdown,
    exit_reason_breakdown,
    holding_days_stats,
    max_hold_binding_rate,
)
from swing_copilot.backtest.policy import EntryPolicyArm
from swing_copilot.backtest.runner import run_backtest
from swing_copilot.backtest.sensitivity import (
    ATR_MULTIPLIER_PCT_GRID,
    MAX_HOLD_PCT_GRID,
    PLATEAU,
    SPIKE,
    GridCell,
    SensitivityGridResult,
)
from swing_copilot.config import StrategiesConfig, load_settings, load_strategies
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.universe import UniverseMember
from tests.backtest.conftest import bars_frame, flat_bars

if TYPE_CHECKING:
    from collections.abc import Sequence

_D0 = date(2027, 1, 1)
_D1 = date(2027, 1, 2)


def _with_provider_columns(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {**row, "provider": "test", "fetched_at": pd.Timestamp("2027-01-20", tz="UTC")}
        for row in rows
    ]


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


class TestParseArgs:
    def test_required_args_parse(self):
        args = _parse_args(
            ["--strategy", "default", "--start", "2025-01-01", "--end", "2026-06-30"]
        )
        assert args.strategy == "default"
        assert args.start == date(2025, 1, 1)
        assert args.end == date(2026, 6, 30)
        assert args.limit is None
        assert args.output is None
        assert args.pessimistic is False

    def test_optional_flags(self):
        args = _parse_args(
            [
                "--strategy",
                "default",
                "--start",
                "2025-01-01",
                "--end",
                "2026-06-30",
                "--limit",
                "30",
                "--output",
                "out.md",
                "--pessimistic",
            ]
        )
        assert args.limit == 30
        assert args.output == Path("out.md")
        assert args.pessimistic is True

    def test_missing_required_arg_parses_as_none(self):
        # Not enforced by argparse `required=True` (conflicts with
        # subparsers) -- `_validate_args` checks presence instead.
        args = _parse_args(["--start", "2025-01-01", "--end", "2026-06-30"])
        assert args.strategy is None

    def test_grid_subcommand_sets_command(self):
        args = _parse_args(
            [
                "grid",
                "--strategy",
                "default",
                "--start",
                "2025-01-01",
                "--end",
                "2026-06-30",
            ]
        )
        assert args.command == "grid"
        assert args.strategy == "default"

    def test_no_subcommand_defaults_to_run(self):
        args = _parse_args(
            ["--strategy", "default", "--start", "2025-01-01", "--end", "2026-06-30"]
        )
        assert args.command == "run"


class TestValidateArgs:
    def _strategies(self) -> StrategiesConfig:
        return StrategiesConfig.model_validate(
            {
                "strategies": {
                    "default": {
                        "filters_all": [],
                        "signals_all": [],
                        "candidate_limit": 10,
                    }
                }
            }
        )

    def test_missing_strategy_raises(self):
        args = _parse_args(["--start", "2025-01-01", "--end", "2026-06-30"])
        with pytest.raises(BacktestCliError, match="--strategy"):
            _validate_args(args, self._strategies())

    def test_start_after_end_raises(self):
        args = _parse_args(
            ["--strategy", "default", "--start", "2026-06-30", "--end", "2025-01-01"]
        )
        with pytest.raises(BacktestCliError, match="--start"):
            _validate_args(args, self._strategies())

    def test_unknown_strategy_lists_available(self):
        args = _parse_args(
            [
                "--strategy",
                "nonexistent",
                "--start",
                "2025-01-01",
                "--end",
                "2026-06-30",
            ]
        )
        with pytest.raises(BacktestCliError, match="default"):
            _validate_args(args, self._strategies())

    def test_valid_args_do_not_raise(self):
        args = _parse_args(
            ["--strategy", "default", "--start", "2025-01-01", "--end", "2026-06-30"]
        )
        _validate_args(args, self._strategies())

    @pytest.mark.parametrize("limit", ["0", "-1"])
    def test_non_positive_limit_raises(self, limit):
        args = _parse_args(
            [
                "--strategy",
                "default",
                "--start",
                "2025-01-01",
                "--end",
                "2026-06-30",
                "--limit",
                limit,
            ]
        )

        with pytest.raises(BacktestCliError, match="1以上"):
            _validate_args(args, self._strategies())


class TestSelectSymbols:
    """Issue #194: `--limit` samples the universe instead of truncating it."""

    def _members(self, symbols: Sequence[str], sector: str) -> list[UniverseMember]:
        return [
            UniverseMember(
                symbol=symbol,
                company_name=symbol,
                gics_sector=sector,
                source_symbol=symbol,
            )
            for symbol in symbols
        ]

    def _universe(self) -> tuple[UniverseMember, ...]:
        return tuple(self._members(("AAA", "BBB", "CCC"), "Information Technology"))

    def _sectored_universe(self, sizes: dict[str, int]) -> tuple[UniverseMember, ...]:
        """One member per slot, named so alphabetical order is fully predictable."""
        members: list[UniverseMember] = []
        for sector, size in sorted(sizes.items()):
            offset = len(members)
            members += self._members(
                [f"S{offset + index:03d}" for index in range(size)], sector
            )
        return tuple(members)

    def test_no_limit_returns_the_whole_universe(self):
        sample = _select_symbols(self._universe(), None)

        assert sample.symbols == ("AAA", "BBB", "CCC")
        assert sample.is_stratified_sample is False
        assert sample.universe_size == 3
        assert sample.sector_counts == (("Information Technology", 3),)

    def test_limit_at_or_above_the_universe_size_returns_the_whole_universe(self):
        assert _select_symbols(self._universe(), 3).symbols == ("AAA", "BBB", "CCC")
        assert _select_symbols(self._universe(), 99).symbols == ("AAA", "BBB", "CCC")

    def test_limit_is_not_the_alphabetically_first_n_symbols(self):
        # The regression: `symbols[:limit]` returned exactly S000..S019.
        universe = self._sectored_universe({"Energy": 100, "Utilities": 100})

        sample = _select_symbols(universe, 20)

        alphabetical_head = tuple(f"S{index:03d}" for index in range(20))
        assert len(sample.symbols) == 20
        assert sample.symbols != alphabetical_head
        # Spread across the whole alphabet, not clustered at its start.
        assert max(sample.symbols) > "S150"

    def test_same_universe_and_limit_always_select_the_same_symbols(self):
        universe = self._sectored_universe({"Energy": 40, "Utilities": 60})
        shuffled = tuple(reversed(universe))

        first = _select_symbols(universe, 25)
        again = _select_symbols(universe, 25)
        from_shuffled_input = _select_symbols(shuffled, 25)

        assert first.symbols == again.symbols == from_shuffled_input.symbols

    def test_sector_shares_are_proportional_to_the_universe(self):
        universe = self._sectored_universe(
            {"Energy": 100, "Financials": 60, "Health Care": 30, "Utilities": 10}
        )

        sample = _select_symbols(universe, 20)

        assert sample.is_stratified_sample is True
        assert sample.universe_size == 200
        assert sample.sector_counts == (
            ("Energy", 10),
            ("Financials", 6),
            ("Health Care", 3),
            ("Utilities", 1),
        )

    def test_leftover_seats_go_to_the_largest_remainder_ties_broken_by_name(self):
        # 4 seats over three equal sectors: every sector floors to 1 and the
        # single leftover goes to the alphabetically first of the tied three.
        universe = self._sectored_universe({"Aaa": 3, "Bbb": 3, "Ccc": 3})

        sample = _select_symbols(universe, 4)

        assert sample.sector_counts == (("Aaa", 2), ("Bbb", 1), ("Ccc", 1))

    def test_a_limit_smaller_than_the_sector_count_still_fills_every_seat(self):
        universe = self._sectored_universe({"Energy": 100, "Utilities": 100})

        sample = _select_symbols(universe, 1)

        assert len(sample.symbols) == 1

    def test_summary_lines_state_the_method_and_the_sector_composition(self):
        universe = self._sectored_universe({"Energy": 100, "Utilities": 100})

        method, composition = _select_symbols(universe, 20).summary_lines()

        assert "20/200" in method
        assert "blake2b" in method
        assert composition == "セクター構成: Energy 10, Utilities 10"

    def test_full_universe_summary_says_so(self):
        method, _composition = _select_symbols(self._universe(), None).summary_lines()

        assert "全 3 銘柄" in method


class TestOutputPath:
    def test_explicit_output_is_used_as_is(self):
        args = _parse_args(
            [
                "--strategy",
                "default",
                "--start",
                "2025-01-01",
                "--end",
                "2026-06-30",
                "--output",
                "custom/report.md",
            ]
        )
        assert _output_path(args) == Path("custom/report.md")

    def test_default_output_uses_end_and_strategy(self):
        args = _parse_args(
            ["--strategy", "default", "--start", "2025-01-01", "--end", "2026-06-30"]
        )
        assert _output_path(args) == Path("reports/backtests/2026-06-30-default.md")

    def test_default_grid_output_uses_end_strategy_and_grid_suffix(self):
        args = _parse_args(
            [
                "grid",
                "--strategy",
                "default",
                "--start",
                "2025-01-01",
                "--end",
                "2026-06-30",
            ]
        )
        assert _grid_output_path(args) == Path(
            "reports/backtests/2026-06-30-default-grid.md"
        )


class TestMissingDataSymbols:
    @pytest.fixture
    def market_store(self, tmp_path):
        store = MarketStore(
            Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
        )
        days = [date(2027, 1, 1 + i) for i in range(5)]
        store.write_bars(
            bars_frame(_with_provider_columns(flat_bars("AAA", days, 100.0)))
        )
        return store

    def test_symbol_with_no_bars_is_reported_missing(self, market_store):
        missing = _missing_data_symbols(
            market_store, ["AAA", "ZZZ"], date(2027, 1, 1), date(2027, 1, 5)
        )
        assert missing == ["ZZZ"]

    def test_all_symbols_present_is_empty(self, market_store):
        missing = _missing_data_symbols(
            market_store, ["AAA"], date(2027, 1, 1), date(2027, 1, 5)
        )
        assert missing == []

    def test_empty_symbol_list_is_empty(self, market_store):
        assert (
            _missing_data_symbols(market_store, [], date(2027, 1, 1), date(2027, 1, 5))
            == []
        )


class TestAtomicWrite:
    def test_writes_content(self, tmp_path):
        path = tmp_path / "report.md"
        _atomic_write(path, "hello")
        assert path.read_text(encoding="utf-8") == "hello"

    def test_failure_preserves_previous_destination_and_cleans_up_tmp(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "report.md"
        path.write_text("original", encoding="utf-8")

        def _boom(self, _target):
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(Path, "replace", _boom)

        with pytest.raises(OSError, match="disk full"):
            _atomic_write(path, "new content")

        assert path.read_text(encoding="utf-8") == "original"
        assert list(tmp_path.glob(".report.md.*.tmp")) == []


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
        ]

    def test_every_report_states_the_method_and_the_composition(self):
        for text in self._rendered():
            assert "2/10 銘柄の決定論的サンプル" in text
            assert "セクター構成: Information Technology 2" in text

    def test_a_full_universe_run_says_so_instead(self):
        text = render_markdown(_result(), _meta())

        assert "全 1 銘柄（--limit 指定なし）" in text
        assert "決定論的サンプル" not in text


@pytest.fixture
def two_symbol_universe(monkeypatch):
    members = [
        UniverseMember(
            symbol="AAA",
            company_name="AAA Inc.",
            gics_sector="Information Technology",
            source_symbol="AAA",
        ),
        UniverseMember(
            symbol="BBB",
            company_name="BBB Inc.",
            gics_sector="Information Technology",
            source_symbol="BBB",
        ),
    ]
    monkeypatch.setattr(
        cli_module, "get_sp500_universe", lambda *_args, **_kwargs: members
    )
    return members


@pytest.fixture
def seeded_db(tmp_path):
    db_path = tmp_path / "copilot.duckdb"
    store = MarketStore(Database(db_path), parquet_root=tmp_path / "bars")
    days = [date(2027, 1, 1 + i) for i in range(10)]
    rows = [
        *flat_bars("SPY", days, 400.0),
        # QQQ/^VIX are what `--policy` needs to evaluate the regime at all;
        # `load_market_frame` always loads them, so seeding them here matches
        # what a real database holds.
        *flat_bars("QQQ", days, 350.0),
        *flat_bars("^VIX", days, 15.0),
        *flat_bars("AAA", days, 100.0),
        # BBB intentionally has no bars -- exercises the missing-data warning.
    ]
    store.write_bars(bars_frame(_with_provider_columns(rows)))
    return db_path, days


class TestPointInTimeUniverseComposition:
    def test_uses_persisted_snapshot_not_after_backtest_end(
        self,
        seeded_db: tuple[Path, list[date]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path, days = seeded_db
        settings = load_settings("config/settings.yaml")
        strategies = load_strategies("config/strategies.yaml")
        state_store = StateStore(Database(db_path))
        state_store.init_schema()
        state_store.record_universe_membership(
            days[-2],
            [
                UniverseMember(
                    symbol="PIT",
                    company_name="Point in time Corp.",
                    gics_sector="Industrials",
                    source_symbol="PIT",
                )
            ],
        )
        state_store.record_universe_membership(
            days[-1] + timedelta(days=1),
            [
                UniverseMember(
                    symbol="FUTURE",
                    company_name="Future Corp.",
                    gics_sector="Industrials",
                    source_symbol="FUTURE",
                )
            ],
        )
        monkeypatch.setattr(
            cli_module,
            "get_sp500_universe",
            lambda *_args, **_kwargs: pytest.fail(
                "a persisted historical snapshot must avoid current-universe fallback"
            ),
        )

        args = _parse_args(
            [
                "--strategy",
                "default",
                "--start",
                days[0].isoformat(),
                "--end",
                days[-1].isoformat(),
                "--db",
                str(db_path),
            ]
        )
        deps, sample, _missing = _compose_dependencies(args, settings, strategies)

        assert [member.symbol for member in deps.universe] == ["PIT"]
        assert sample.symbols == ("PIT",)


@pytest.mark.usefixtures("two_symbol_universe")
class TestMainEndToEnd:
    def test_happy_path_completes_and_writes_report(self, seeded_db, tmp_path, capsys):
        db_path, days = seeded_db
        output_path = tmp_path / "out" / "report.md"

        main(
            [
                "--strategy",
                "default",
                "--start",
                days[0].isoformat(),
                "--end",
                days[-1].isoformat(),
                "--db",
                str(db_path),
                "--output",
                str(output_path),
            ]
        )

        captured = capsys.readouterr()
        assert "trade_count" in captured.out
        assert "データ不足のためスキップ: BBB" in captured.out
        assert output_path.exists()
        assert "# Backtest: default" in output_path.read_text(encoding="utf-8")

    def test_grid_subcommand_completes_and_writes_matrix_report(
        self, seeded_db, tmp_path, capsys
    ):
        db_path, days = seeded_db
        output_path = tmp_path / "out" / "grid.md"

        main(
            [
                "grid",
                "--strategy",
                "default",
                "--start",
                days[0].isoformat(),
                "--end",
                days[-1].isoformat(),
                "--db",
                str(db_path),
                "--output",
                str(output_path),
            ]
        )

        captured = capsys.readouterr()
        assert "Verdict:" in captured.out
        # A 10-day window is far too short for any real trades: every one of
        # the 25 cells is gray (trade_count < 30) -> inconclusive.
        assert "判定不能（データ不足）" in captured.out
        assert output_path.exists()
        report_text = output_path.read_text(encoding="utf-8")
        assert "# Backtest sensitivity grid: default" in report_text
        assert "| ATR% \\ MaxHold% |" in report_text

    def test_pessimistic_runs_both_scenarios_and_writes_comparison_report(
        self, seeded_db, tmp_path, capsys
    ):
        db_path, days = seeded_db
        output_path = tmp_path / "out" / "report.md"

        main(
            [
                "--strategy",
                "default",
                "--start",
                days[0].isoformat(),
                "--end",
                days[-1].isoformat(),
                "--db",
                str(db_path),
                "--output",
                str(output_path),
                "--pessimistic",
            ]
        )

        captured = capsys.readouterr()
        assert "normal vs pessimistic" in captured.out
        assert "Normal (x1.0)" in captured.out
        assert "Pessimistic" in captured.out
        report_text = output_path.read_text(encoding="utf-8")
        assert "normal vs pessimistic" in report_text
        assert "| Metric | Normal (x1.0) | Pessimistic |" in report_text

    def test_start_after_end_exits_without_running(self, seeded_db, tmp_path):
        db_path, days = seeded_db

        with pytest.raises(SystemExit, match="--start"):
            main(
                [
                    "--strategy",
                    "default",
                    "--start",
                    days[-1].isoformat(),
                    "--end",
                    days[0].isoformat(),
                    "--db",
                    str(db_path),
                    "--output",
                    str(tmp_path / "out.md"),
                ]
            )

        assert not (tmp_path / "out.md").exists()

    def test_unknown_strategy_exits_without_running(self, seeded_db, tmp_path):
        db_path, days = seeded_db

        with pytest.raises(SystemExit, match="default"):
            main(
                [
                    "--strategy",
                    "nonexistent",
                    "--start",
                    days[0].isoformat(),
                    "--end",
                    days[-1].isoformat(),
                    "--db",
                    str(db_path),
                    "--output",
                    str(tmp_path / "out.md"),
                ]
            )

        assert not (tmp_path / "out.md").exists()

    def test_grid_start_after_end_exits_without_running(self, seeded_db, tmp_path):
        db_path, days = seeded_db

        with pytest.raises(SystemExit, match="--start"):
            main(
                [
                    "grid",
                    "--strategy",
                    "default",
                    "--start",
                    days[-1].isoformat(),
                    "--end",
                    days[0].isoformat(),
                    "--db",
                    str(db_path),
                    "--output",
                    str(tmp_path / "grid.md"),
                ]
            )

        assert not (tmp_path / "grid.md").exists()

    def test_grid_unknown_strategy_exits_without_running(self, seeded_db, tmp_path):
        db_path, days = seeded_db

        with pytest.raises(SystemExit, match="default"):
            main(
                [
                    "grid",
                    "--strategy",
                    "nonexistent",
                    "--start",
                    days[0].isoformat(),
                    "--end",
                    days[-1].isoformat(),
                    "--db",
                    str(db_path),
                    "--output",
                    str(tmp_path / "grid.md"),
                ]
            )

        assert not (tmp_path / "grid.md").exists()


def test_real_settings_and_strategies_load():
    # Sanity check that main()'s default (no override) config loading targets
    # exist and parse -- exercised indirectly by TestMainEndToEnd already.
    settings = load_settings()
    strategies = load_strategies()
    assert "default" in strategies.strategies
    assert settings.backtest.benchmark == "SPY"


def _settings_copy(tmp_path: Path, *, initial_cash_usd: int) -> Path:
    """A real settings.yaml with one backtest value replaced, for --settings."""
    raw = yaml.safe_load(Path("config/settings.yaml").read_text(encoding="utf-8"))
    raw["backtest"]["initial_cash_usd"] = initial_cash_usd
    override = tmp_path / "settings-variant.yaml"
    override.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return override


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


@pytest.mark.usefixtures("two_symbol_universe")
class TestSettingsOverride:
    def test_settings_flag_defaults_to_the_repository_settings_path(self):
        args = _parse_args(
            ["--strategy", "default", "--start", "2025-01-01", "--end", "2026-06-30"]
        )

        assert args.settings == DEFAULT_SETTINGS_PATH

    def test_grid_subcommand_accepts_its_own_settings_override(self, tmp_path):
        override = tmp_path / "settings.yaml"

        args = _parse_args(
            [
                "grid",
                "--strategy",
                "default",
                "--start",
                "2025-01-01",
                "--end",
                "2026-06-30",
                "--settings",
                str(override),
            ]
        )

        assert args.settings == str(override)

    def test_settings_given_before_the_grid_subcommand_survive(self, tmp_path):
        # argparse parses a subcommand into a fresh namespace and copies all
        # of it onto the shared one, so a real default on the subparser would
        # silently snap these back to the repository files and the grid would
        # measure the baseline while its report named the variant.
        settings_override = tmp_path / "settings.yaml"
        strategies_override = tmp_path / "strategies.yaml"

        args = _parse_args(
            [
                "--settings",
                str(settings_override),
                "--strategies",
                str(strategies_override),
                "grid",
                "--strategy",
                "default",
                "--start",
                "2025-01-01",
                "--end",
                "2026-06-30",
            ]
        )

        assert args.settings == str(settings_override)
        assert args.strategies == str(strategies_override)
        assert args.command == "grid"
        assert args.strategy == "default"

    def test_overridden_settings_reach_the_backtest_result(
        self, seeded_db, tmp_path, capsys
    ):
        db_path, days = seeded_db
        override = _settings_copy(tmp_path, initial_cash_usd=50_000)
        output_path = tmp_path / "out" / "report.md"

        main(
            [
                "--strategy",
                "default",
                "--start",
                days[0].isoformat(),
                "--end",
                days[-1].isoformat(),
                "--db",
                str(db_path),
                "--output",
                str(output_path),
                "--settings",
                str(override),
            ]
        )

        # No trades fire in this 10-day window, so final equity is exactly the
        # overridden starting cash -- not the repository default of 100,000.
        assert "| final_equity | $50,000.00 |" in output_path.read_text(
            encoding="utf-8"
        )
        assert "$100,000.00" not in capsys.readouterr().out

    def test_a_missing_settings_file_fails_before_any_backtest_runs(
        self, seeded_db, tmp_path
    ):
        db_path, days = seeded_db

        with pytest.raises(SystemExit):
            main(
                [
                    "--strategy",
                    "default",
                    "--start",
                    days[0].isoformat(),
                    "--end",
                    days[-1].isoformat(),
                    "--db",
                    str(db_path),
                    "--output",
                    str(tmp_path / "report.md"),
                    "--settings",
                    str(tmp_path / "nope.yaml"),
                ]
            )

        assert not (tmp_path / "report.md").exists()


@pytest.mark.usefixtures("two_symbol_universe")
class TestStrategiesOverride:
    """`--strategies`: score_weights variants live in strategies.yaml, not settings."""

    def test_strategies_flag_defaults_to_the_repository_strategies_path(self):
        args = _parse_args(
            ["--strategy", "default", "--start", "2025-01-01", "--end", "2026-06-30"]
        )

        assert args.strategies == DEFAULT_STRATEGIES_PATH

    def test_an_overridden_strategy_name_is_accepted(self, seeded_db, tmp_path):
        db_path, days = seeded_db
        override = tmp_path / "strategies-variant.yaml"
        override.write_text(
            "strategies:\n"
            "  volatility_tilt:\n"
            "    filters_all: []\n"
            "    signals_all: [pullback_rsi]\n"
            "    candidate_limit: 5\n"
            "    ranking:\n"
            "      score_weights:\n"
            "        rsi_pullback: 0.3\n"
            "        trend_quality: 0.3\n"
            "        liquidity: 0.2\n"
            "        atr_pct: 0.2\n",
            encoding="utf-8",
        )
        output_path = tmp_path / "out" / "report.md"

        main(
            [
                "--strategy",
                "volatility_tilt",
                "--start",
                days[0].isoformat(),
                "--end",
                days[-1].isoformat(),
                "--db",
                str(db_path),
                "--output",
                str(output_path),
                "--strategies",
                str(override),
            ]
        )

        assert "# Backtest: volatility_tilt" in output_path.read_text(encoding="utf-8")

    def test_a_missing_strategies_file_fails_before_any_backtest_runs(
        self, seeded_db, tmp_path
    ):
        db_path, days = seeded_db

        with pytest.raises(SystemExit):
            main(
                [
                    "--strategy",
                    "default",
                    "--start",
                    days[0].isoformat(),
                    "--end",
                    days[-1].isoformat(),
                    "--db",
                    str(db_path),
                    "--output",
                    str(tmp_path / "report.md"),
                    "--strategies",
                    str(tmp_path / "nope.yaml"),
                ]
            )

        assert not (tmp_path / "report.md").exists()


def _screening_settings_copy(tmp_path: Path, *, min_equity_ratio: float) -> Path:
    """A real settings.yaml with one *screening* value replaced, for --settings.

    Deliberately not a `backtest.*` value: those are engine inputs and must
    leave the candidate cache valid (Issue #185).
    """
    raw = yaml.safe_load(Path("config/settings.yaml").read_text(encoding="utf-8"))
    raw["fundamental_filters"]["min_equity_ratio"] = min_equity_ratio
    override = tmp_path / "settings-screening-variant.yaml"
    override.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return override


@pytest.mark.usefixtures("two_symbol_universe")
class TestCandidateCache:
    """`--candidate-cache`: screening is paid for once, even across processes."""

    def _argv(
        self,
        db_path: Path,
        days: list[date],
        tmp_path: Path,
        cache_path: Path,
        *extra: str,
    ) -> list[str]:
        return [
            "--strategy",
            "default",
            "--start",
            days[0].isoformat(),
            "--end",
            days[-1].isoformat(),
            "--db",
            str(db_path),
            "--output",
            str(tmp_path / "out" / "report.md"),
            "--candidate-cache",
            str(cache_path),
            *extra,
        ]

    def test_candidate_cache_flag_defaults_to_none(self):
        args = _parse_args(
            ["--strategy", "default", "--start", "2025-01-01", "--end", "2026-06-30"]
        )

        assert args.candidate_cache is None

    def test_candidate_cache_given_before_the_grid_subcommand_survives(self, tmp_path):
        args = _parse_args(
            [
                "--candidate-cache",
                str(tmp_path / "cache.parquet"),
                "grid",
                "--strategy",
                "default",
                "--start",
                "2025-01-01",
                "--end",
                "2026-06-30",
            ]
        )

        assert args.candidate_cache == tmp_path / "cache.parquet"

    def test_first_run_writes_the_cache_and_the_second_reuses_it(
        self, seeded_db, tmp_path, capsys, monkeypatch
    ):
        db_path, days = seeded_db
        cache_path = tmp_path / "cache" / "candidates.parquet"

        main(self._argv(db_path, days, tmp_path, cache_path))

        assert cache_path.exists()
        assert "候補ストリームキャッシュを保存" in capsys.readouterr().out

        monkeypatch.setattr(
            cli_module,
            "generate_candidate_stream",
            lambda *_args, **_kwargs: pytest.fail(
                "a matching cache must not trigger a second screening pass"
            ),
        )
        main(self._argv(db_path, days, tmp_path, cache_path))

        assert "候補ストリームキャッシュを再利用" in capsys.readouterr().out

    def test_a_changed_screening_setting_invalidates_and_overwrites_the_cache(
        self, seeded_db, tmp_path, capsys
    ):
        db_path, days = seeded_db
        cache_path = tmp_path / "candidates.parquet"
        main(self._argv(db_path, days, tmp_path, cache_path))
        capsys.readouterr()
        first_bytes = cache_path.read_bytes()
        override = _screening_settings_copy(tmp_path, min_equity_ratio=0.55)

        main(
            self._argv(db_path, days, tmp_path, cache_path, "--settings", str(override))
        )

        captured = capsys.readouterr()
        assert "キーが一致しません" in captured.out
        assert "候補ストリームキャッシュを保存" in captured.out
        assert cache_path.read_bytes() != first_bytes

    def test_an_unreadable_cache_is_regenerated_rather_than_failing(
        self, seeded_db, tmp_path, capsys
    ):
        db_path, days = seeded_db
        cache_path = tmp_path / "candidates.parquet"
        cache_path.write_bytes(b"corrupted")

        main(self._argv(db_path, days, tmp_path, cache_path))

        captured = capsys.readouterr()
        assert "候補ストリームキャッシュを読めませんでした" in captured.out
        assert "候補ストリームキャッシュを保存" in captured.out
        assert cache_path.read_bytes() != b"corrupted"

    def test_grid_screens_once_and_still_runs_every_cell(
        self, seeded_db, tmp_path, monkeypatch
    ):
        db_path, days = seeded_db
        screenings: list[int] = []
        engine_runs: list[int] = []
        original_generate = generate_candidate_stream
        original_run = run_backtest

        def counting_generate(*args, **kwargs):
            screenings.append(1)
            return original_generate(*args, **kwargs)

        def counting_run(*args, **kwargs):
            engine_runs.append(1)
            return original_run(*args, **kwargs)

        monkeypatch.setattr(cli_module, "generate_candidate_stream", counting_generate)
        monkeypatch.setattr(cli_module, "run_backtest", counting_run)

        main(
            [
                "grid",
                "--strategy",
                "default",
                "--start",
                days[0].isoformat(),
                "--end",
                days[-1].isoformat(),
                "--db",
                str(db_path),
                "--output",
                str(tmp_path / "grid.md"),
            ]
        )

        assert len(screenings) == 1
        assert len(engine_runs) == len(ATR_MULTIPLIER_PCT_GRID) * len(MAX_HOLD_PCT_GRID)

    def test_pessimistic_shares_one_screening_pass_across_both_scenarios(
        self, seeded_db, tmp_path, monkeypatch
    ):
        db_path, days = seeded_db
        screenings: list[int] = []
        engine_runs: list[int] = []
        original_generate = generate_candidate_stream
        original_run = run_backtest

        def counting_generate(*args, **kwargs):
            screenings.append(1)
            return original_generate(*args, **kwargs)

        def counting_run(*args, **kwargs):
            engine_runs.append(1)
            return original_run(*args, **kwargs)

        monkeypatch.setattr(cli_module, "generate_candidate_stream", counting_generate)
        monkeypatch.setattr(cli_module, "run_backtest", counting_run)

        main(
            [
                "--strategy",
                "default",
                "--start",
                days[0].isoformat(),
                "--end",
                days[-1].isoformat(),
                "--db",
                str(db_path),
                "--output",
                str(tmp_path / "report.md"),
                "--pessimistic",
            ]
        )

        assert len(screenings) == 1
        assert len(engine_runs) == 2


class TestPolicyArgument:
    def test_default_is_the_ungated_arm(self):
        args = _parse_args(
            ["--strategy", "default", "--start", "2027-01-01", "--end", "2027-01-10"]
        )

        assert args.policy == EntryPolicyArm.NONE.value

    def test_unknown_arm_fails_fast(self):
        strategies = load_strategies(DEFAULT_STRATEGIES_PATH)
        args = _parse_args(
            [
                "--strategy",
                "default",
                "--start",
                "2027-01-01",
                "--end",
                "2027-01-10",
                "--policy",
                "bogus",
            ]
        )

        with pytest.raises(BacktestCliError, match=r"未知の --policy"):
            _validate_args(args, strategies)

    def test_multi_arm_policy_with_pessimistic_is_rejected(self):
        strategies = load_strategies(DEFAULT_STRATEGIES_PATH)
        args = _parse_args(
            [
                "--strategy",
                "default",
                "--start",
                "2027-01-01",
                "--end",
                "2027-01-10",
                "--policy",
                "none,regime",
                "--pessimistic",
            ]
        )

        with pytest.raises(BacktestCliError, match=r"--pessimistic"):
            _validate_args(args, strategies)

    def test_single_arm_policy_with_pessimistic_is_allowed(self):
        strategies = load_strategies(DEFAULT_STRATEGIES_PATH)
        args = _parse_args(
            [
                "--strategy",
                "default",
                "--start",
                "2027-01-01",
                "--end",
                "2027-01-10",
                "--policy",
                "regime",
                "--pessimistic",
            ]
        )

        _validate_args(args, strategies)


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


@pytest.mark.usefixtures("two_symbol_universe")
class TestPolicyEndToEnd:
    def test_ab_run_compares_arms_over_one_candidate_stream(
        self, seeded_db, tmp_path, capsys, monkeypatch
    ):
        db_path, days = seeded_db
        output_path = tmp_path / "policy.md"
        screenings: list[int] = []
        original_generate = generate_candidate_stream

        def counting_generate(*args, **kwargs):
            screenings.append(1)
            return original_generate(*args, **kwargs)

        monkeypatch.setattr(cli_module, "generate_candidate_stream", counting_generate)

        main(
            [
                "--strategy",
                "default",
                "--start",
                days[0].isoformat(),
                "--end",
                days[-1].isoformat(),
                "--db",
                str(db_path),
                "--output",
                str(output_path),
                "--policy",
                "none,regime+risk",
            ]
        )

        captured = capsys.readouterr()
        assert "Backtest metrics by policy" in captured.out
        # One screening pass feeds both arms: the diff is attributable to the
        # gates and nothing else.
        assert len(screenings) == 1
        report_text = output_path.read_text(encoding="utf-8")
        assert "| Metric | none | regime+risk |" in report_text

    def test_missing_regime_bars_abort_the_run_with_a_clear_message(
        self, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "copilot.duckdb"
        store = MarketStore(Database(db_path), parquet_root=tmp_path / "bars")
        days = [date(2027, 1, 1 + i) for i in range(10)]
        store.write_bars(
            bars_frame(
                _with_provider_columns(
                    [*flat_bars("SPY", days, 400.0), *flat_bars("AAA", days, 100.0)]
                )
            )
        )
        monkeypatch.setattr(
            cli_module,
            "get_sp500_universe",
            lambda *_args, **_kwargs: [
                UniverseMember(
                    symbol="AAA",
                    company_name="AAA Inc.",
                    gics_sector="Information Technology",
                    source_symbol="AAA",
                )
            ],
        )

        with pytest.raises(SystemExit, match=r"レジームゲートに必要なバー"):
            main(
                [
                    "--strategy",
                    "default",
                    "--start",
                    days[0].isoformat(),
                    "--end",
                    days[-1].isoformat(),
                    "--db",
                    str(db_path),
                    "--output",
                    str(tmp_path / "out.md"),
                    "--policy",
                    "regime",
                ]
            )

    def test_grid_refuses_a_policy_instead_of_ignoring_it(
        self, seeded_db, tmp_path, monkeypatch
    ):
        db_path, days = seeded_db
        monkeypatch.setattr(
            cli_module,
            "get_sp500_universe",
            lambda *_args, **_kwargs: [
                UniverseMember(
                    symbol="AAA",
                    company_name="AAA Inc.",
                    gics_sector="Information Technology",
                    source_symbol="AAA",
                )
            ],
        )

        with pytest.raises(SystemExit, match=r"grid サブコマンドは --policy"):
            main(
                [
                    "--policy",
                    "regime",
                    "grid",
                    "--strategy",
                    "default",
                    "--start",
                    days[0].isoformat(),
                    "--end",
                    days[-1].isoformat(),
                    "--db",
                    str(db_path),
                    "--output",
                    str(tmp_path / "grid.md"),
                ]
            )
