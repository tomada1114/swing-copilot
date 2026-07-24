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
    _atomic_write,
    _missing_data_symbols,
    _output_path,
    _parse_args,
    _select_symbols,
    _validate_args,
    main,
    render_markdown,
    render_terminal,
)
from swing_copilot.backtest.engine import BacktestResult, Trade
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

    def test_missing_required_arg_exits(self):
        with pytest.raises(SystemExit):
            _parse_args(["--start", "2025-01-01", "--end", "2026-06-30"])


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

    def test_limit_zero_returns_empty(self):
        assert _select_symbols(self._universe(), 0) == []


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
        assert not path.with_name(".report.md.tmp").exists()


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

        text = render_terminal(
            result,
            strategy="default",
            start=_D0,
            end=_D1,
            missing_data_symbols=["ZZZ"],
        )

        assert "trade_count" in text
        assert "1" in text
        assert "AAA" in text
        assert "予備的" in text
        assert "データ不足のためスキップ: ZZZ" in text
        assert "survivorship" in text.lower()

    def test_no_trades_shows_placeholder(self):
        result = _result()

        text = render_terminal(
            result, strategy="default", start=_D0, end=_D1, missing_data_symbols=[]
        )

        assert "Trades: (none)" in text

    def test_empty_equity_curve_shows_no_trading_days(self):
        result = dataclasses.replace(_result(), equity_curve=())

        text = render_terminal(
            result, strategy="default", start=_D0, end=_D1, missing_data_symbols=[]
        )

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

        text = render_markdown(
            result,
            strategy="default",
            start=_D0,
            end=_D1,
            missing_data_symbols=["ZZZ"],
        )

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

        text = render_markdown(
            result, strategy="default", start=_D0, end=_D1, missing_data_symbols=[]
        )

        assert "(no trades)" in text
        assert "## Warnings" not in text
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


def test_real_settings_and_strategies_load():
    # Sanity check that main()'s default (no override) config loading targets
    # exist and parse -- exercised indirectly by TestMainEndToEnd already.
    settings = load_settings()
    strategies = load_strategies()
    assert "default" in strategies.strategies
    assert settings.backtest.benchmark == "SPY"
