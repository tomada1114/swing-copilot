"""Tests for `copilot-backtest`'s CLI parsing, rendering, and composition (P2-08)."""

from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from swing_copilot.backtest import cli as cli_module
from swing_copilot.backtest.cli import (
    BacktestCliError,
    ReportMeta,
    _atomic_write,
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
    render_terminal,
    render_terminal_comparison,
)
from swing_copilot.backtest.engine import BacktestResult, Trade
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
from swing_copilot.universe import UniverseMember
from tests.backtest.conftest import bars_frame, flat_bars

_D0 = date(2027, 1, 1)
_D1 = date(2027, 1, 2)


def _with_provider_columns(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {**row, "provider": "test", "fetched_at": pd.Timestamp("2027-01-20", tz="UTC")}
        for row in rows
    ]


def _result(
    *, trades: tuple[Trade, ...] = (), warnings: tuple[str, ...] = ()
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
    def _universe(self) -> tuple[UniverseMember, ...]:
        return tuple(
            UniverseMember(
                symbol=symbol,
                company_name=symbol,
                gics_sector="Information Technology",
                source_symbol=symbol,
            )
            for symbol in ("AAA", "BBB", "CCC")
        )

    def test_no_limit_returns_all(self):
        assert _select_symbols(self._universe(), None) == ["AAA", "BBB", "CCC"]

    def test_limit_truncates(self):
        assert _select_symbols(self._universe(), 2) == ["AAA", "BBB"]


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
        meta = ReportMeta(
            strategy="default", start=_D0, end=_D1, missing_data_symbols=["ZZZ"]
        )

        text = render_terminal(result, meta)

        assert "trade_count" in text
        assert "1" in text
        assert "AAA" in text
        assert "予備的" in text
        assert "データ不足のためスキップ: ZZZ" in text
        assert "survivorship" in text.lower()

    def test_no_trades_shows_placeholder(self):
        result = _result()
        meta = ReportMeta(
            strategy="default", start=_D0, end=_D1, missing_data_symbols=[]
        )

        text = render_terminal(result, meta)

        assert "Trades: (none)" in text

    def test_empty_equity_curve_shows_no_trading_days(self):
        result = dataclasses.replace(_result(), equity_curve=())
        meta = ReportMeta(
            strategy="default", start=_D0, end=_D1, missing_data_symbols=[]
        )

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
        meta = ReportMeta(
            strategy="default", start=_D0, end=_D1, missing_data_symbols=["ZZZ"]
        )

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
        meta = ReportMeta(
            strategy="default", start=_D0, end=_D1, missing_data_symbols=[]
        )

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
        meta = ReportMeta(
            strategy="default", start=_D0, end=_D1, missing_data_symbols=["ZZZ"]
        )

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
        meta = ReportMeta(
            strategy="default", start=_D0, end=_D1, missing_data_symbols=[]
        )

        text = render_terminal_comparison(normal, pessimistic, meta)

        assert "データ不足のためスキップ" not in text

    def test_markdown_comparison_shows_both_scenarios_as_a_diff_table(self):
        normal = _result(warnings=("normal warning",))
        pessimistic = dataclasses.replace(
            _result(warnings=("pessimistic warning",)), final_equity=90_000.0
        )
        meta = ReportMeta(
            strategy="default", start=_D0, end=_D1, missing_data_symbols=[]
        )

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
        meta = ReportMeta(
            strategy="default", start=_D0, end=_D1, missing_data_symbols=[]
        )

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
            max_hold_pct=80,
            expectancy_per_trade=1.0,
            trade_count=5,
        )
        grid = _grid_result(cell_overrides={(50, 80): gray_cell})
        meta = ReportMeta(
            strategy="default", start=_D0, end=_D1, missing_data_symbols=[]
        )

        text = render_grid_terminal(grid, meta, gray_threshold=30)

        assert "スパイク（過学習疑い）" in text
        assert "$50.00" in text
        assert "*" in text
        assert "灰色扱い" in text

    def test_markdown_shows_matrix_as_a_table_with_verdict(self):
        grid = _grid_result(verdict=PLATEAU)
        meta = ReportMeta(
            strategy="default", start=_D0, end=_D1, missing_data_symbols=["ZZZ"]
        )

        text = render_grid_markdown(grid, meta, gray_threshold=30)

        assert "Verdict: プラトー（頑健）" in text
        assert "| ATR% \\ MaxHold% |" in text
        assert "データ不足のためスキップ: ZZZ" in text

    def test_markdown_with_no_missing_data_omits_data_quality_section(self):
        grid = _grid_result(verdict=PLATEAU)
        meta = ReportMeta(
            strategy="default", start=_D0, end=_D1, missing_data_symbols=[]
        )

        text = render_grid_markdown(grid, meta, gray_threshold=30)

        assert "## Data quality" not in text


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
        *flat_bars("AAA", days, 100.0),
        # BBB intentionally has no bars -- exercises the missing-data warning.
    ]
    store.write_bars(bars_frame(_with_provider_columns(rows)))
    return db_path, days


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
