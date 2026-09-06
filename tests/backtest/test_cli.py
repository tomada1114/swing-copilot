"""Tests for `copilot-backtest`'s CLI parsing, rendering, and composition (P2-08)."""

from __future__ import annotations

import os
from collections.abc import Callable
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
    _compose_dependencies,
    _entry_grid_output_path,
    _grid_output_path,
    _missing_data_symbols,
    _output_path,
    _parse_args,
    _resolve_parquet_root,
    _validate_args,
    main,
)
from swing_copilot.backtest.policy import EntryPolicyArm, build_entry_policy
from swing_copilot.backtest.runner import run_backtest
from swing_copilot.backtest.sensitivity import (
    ATR_MULTIPLIER_PCT_GRID,
    MAX_HOLD_PCT_GRID,
    entry_limit_grid_values,
)
from swing_copilot.config import StrategiesConfig, load_settings, load_strategies
from swing_copilot.io_atomic import write_text_atomically
from swing_copilot.risk.checks import EarningsGuardInput
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import (
    DEFAULT_PARQUET_ROOT,
    FundamentalsRecord,
    MarketStore,
)
from swing_copilot.storage.state_store import StateStore
from swing_copilot.universe import UniverseMember
from tests.backtest.conftest import bars_frame, flat_bars

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The `build_entry_policy(..., earnings_guard_fn=...)` seam's signature.
_EarningsGuardFn = Callable[[date, tuple[str, ...]], EarningsGuardInput]


def _with_provider_columns(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {**row, "provider": "test", "fetched_at": pd.Timestamp("2027-01-20", tz="UTC")}
        for row in rows
    ]


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

    def test_entry_grid_subcommand_sets_command(self):
        args = _parse_args(
            [
                "entry-grid",
                "--strategy",
                "default",
                "--start",
                "2025-01-01",
                "--end",
                "2026-06-30",
            ]
        )
        assert args.command == "entry-grid"
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
                        "signals_all": ["trend_sma"],
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

    def test_default_entry_grid_output_uses_entry_grid_suffix(self):
        args = _parse_args(
            [
                "entry-grid",
                "--strategy",
                "default",
                "--start",
                "2025-01-01",
                "--end",
                "2026-06-30",
            ]
        )
        assert _entry_grid_output_path(args) == Path(
            "reports/backtests/2026-06-30-default-entry-grid.md"
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
    """Issue #394: the CLI writes its reports through the shared writer.

    The atomic-replace contract itself (temp file in the same directory,
    old file preserved and temp cleaned up on failure) is pinned once, at its
    dependency-zero home in `tests/test_io_atomic.py`. This class has to
    prove two more things a name-binding check alone cannot: that the CLI
    has not quietly grown its own copy of that contract, and that its
    report-write path actually calls through the shared writer at runtime --
    `_run_backtest_command` reverting to a bare `output_path.write_text(...)`
    while leaving the now-unused import in place would still satisfy the
    binding check below.
    """

    def test_report_writing_calls_the_shared_atomic_text_writer(self):
        # `cli_module.write_text_atomically` would work at runtime too, but
        # `cli.py` never declares it in `__all__`, so mypy strict's implicit
        # re-export check rejects the static attribute access; `vars()`
        # sidesteps that without weakening what the assertion proves.
        assert vars(cli_module)["write_text_atomically"] is write_text_atomically

    @pytest.mark.usefixtures("two_symbol_universe")
    def test_report_write_goes_through_the_shared_writer_at_runtime(
        self, seeded_db, tmp_path, monkeypatch
    ):
        db_path, days = seeded_db
        output_path = tmp_path / "out" / "report.md"
        calls: list[Path] = []
        real_write_text_atomically = write_text_atomically

        def _spy(destination: Path, content: str) -> None:
            calls.append(destination)
            real_write_text_atomically(destination, content)

        monkeypatch.setattr(cli_module, "write_text_atomically", _spy)

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

        assert calls == [output_path]
        assert "# Backtest: default" in output_path.read_text(encoding="utf-8")

    @pytest.mark.usefixtures("two_symbol_universe")
    def test_a_mid_write_failure_leaves_the_previous_report_and_cleans_up_the_temp_file(
        self, seeded_db, tmp_path, monkeypatch
    ):
        db_path, days = seeded_db
        output_path = tmp_path / "out" / "report.md"
        output_path.parent.mkdir(parents=True)
        output_path.write_text("previous report", encoding="utf-8")

        def _boom(_source: Path, _target: Path) -> None:
            msg = "disk full"
            raise OSError(msg)

        # `write_text_atomically` itself is exercised for real -- only the
        # `os.replace` it depends on is made to fail -- so this proves the
        # CLI's report write inherits `io_atomic`'s actual cleanup-on-failure
        # behavior, not a hand-rolled stand-in for it.
        monkeypatch.setattr(os, "replace", _boom)

        with pytest.raises(OSError, match="disk full"):
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

        assert output_path.read_text(encoding="utf-8") == "previous report"
        assert list(output_path.parent.glob(".report.md.tmp")) == []


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
    database = Database(db_path)
    store = MarketStore(database, parquet_root=tmp_path / "bars")
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
    StateStore(database).init_schema()
    with store.get_connection():
        pass
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


class TestResolveParquetRoot:
    """How `--db` resolves the bars root, and what happens when it is absent (#217)."""

    def test_default_db_path_pairs_with_the_default_parquet_root(self) -> None:
        # `--db` 未指定の既定経路が指す先は、この対応規約そのものである。
        assert DEFAULT_DB_PATH.parent / "bars" == DEFAULT_PARQUET_ROOT

    def test_existing_sibling_directory_is_returned(self, tmp_path: Path) -> None:
        (tmp_path / "bars").mkdir()

        assert _resolve_parquet_root(tmp_path / "copilot.duckdb") == tmp_path / "bars"

    def test_missing_sibling_directory_raises_naming_the_resolved_path(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(
            BacktestCliError, match=r"Parquetディレクトリが見つかりません"
        ):
            _resolve_parquet_root(tmp_path / "copilot.duckdb")

    def test_sibling_bars_file_is_not_accepted_as_a_root(self, tmp_path: Path) -> None:
        (tmp_path / "bars").write_text("not a directory", encoding="utf-8")

        with pytest.raises(
            BacktestCliError, match=r"Parquetディレクトリが見つかりません"
        ):
            _resolve_parquet_root(tmp_path / "copilot.duckdb")


@pytest.mark.usefixtures("two_symbol_universe")
class TestMissingBarsRootFailsFast:
    """A run pointed at a copied DuckDB with no sibling `bars/` (Issue #217).

    以前は全銘柄が「データ不足」で落ち、取引ゼロのレポートを数秒で書いて
    `exit 0` していた——操作ミスが正常終了に見える形の失敗である。
    """

    @staticmethod
    def _argv(db_path: Path, days: list[date], output_path: Path) -> list[str]:
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
            str(output_path),
        ]

    def test_run_exits_nonzero_without_writing_a_report(
        self, seeded_db, tmp_path, capsys
    ):
        _db_path, days = seeded_db
        detached_dir = tmp_path / "copied"
        detached_dir.mkdir()
        detached_db = detached_dir / "copilot.duckdb"
        output_path = tmp_path / "out" / "report.md"

        with pytest.raises(SystemExit) as excinfo:
            main(self._argv(detached_db, days, output_path))

        message = str(excinfo.value)
        assert str(detached_dir / "bars") in message
        assert not output_path.exists()
        # 何も作らずに落ちること: DuckDBファイルを開く前段で止める。
        assert not detached_db.exists()
        assert "データ不足のためスキップ" not in capsys.readouterr().out

    def test_grid_exits_nonzero_without_writing_a_report(self, seeded_db, tmp_path):
        _db_path, days = seeded_db
        detached_dir = tmp_path / "copied"
        detached_dir.mkdir()
        output_path = tmp_path / "out" / "grid.md"

        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "grid",
                    *self._argv(detached_dir / "copilot.duckdb", days, output_path),
                ]
            )

        assert str(detached_dir / "bars") in str(excinfo.value)
        assert not output_path.exists()

    def test_per_symbol_gaps_under_a_present_root_stay_fail_soft(
        self, seeded_db, tmp_path, capsys
    ):
        # 「数銘柄だけバーが無い」（BBBは未シード）は正当なfail-softのまま:
        # 潰すのは「根ごと無い」ケースだけである。
        db_path, days = seeded_db
        output_path = tmp_path / "out" / "report.md"

        main(self._argv(db_path, days, output_path))

        assert "データ不足のためスキップ: BBB" in capsys.readouterr().out
        assert output_path.exists()


@pytest.mark.usefixtures("two_symbol_universe")
class TestReadOnlyDatabase:
    """The backtest must never migrate or rewrite the operator's database."""

    def test_run_preserves_initialized_database_bytes_and_mtime(
        self, seeded_db, tmp_path
    ):
        db_path, days = seeded_db
        output_path = tmp_path / "out" / "report.md"
        before_bytes = db_path.read_bytes()
        before_mtime_ns = db_path.stat().st_mtime_ns

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

        assert db_path.read_bytes() == before_bytes
        assert db_path.stat().st_mtime_ns == before_mtime_ns

    def test_existing_uninitialized_database_fails_explicitly(self, tmp_path):
        db_path = tmp_path / "uninitialized.duckdb"
        bars_root = tmp_path / "bars"
        bars_root.mkdir()
        Database(db_path).connect().close()

        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "--strategy",
                    "default",
                    "--start",
                    "2027-01-01",
                    "--end",
                    "2027-01-10",
                    "--db",
                    str(db_path),
                    "--output",
                    str(tmp_path / "report.md"),
                ]
            )

        assert "未初期化" in str(excinfo.value)
        assert "universe_membership" in str(excinfo.value)

    def test_missing_database_fails_without_creating_it(self, tmp_path):
        db_path = tmp_path / "missing.duckdb"
        (tmp_path / "bars").mkdir()

        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "--strategy",
                    "default",
                    "--start",
                    "2027-01-01",
                    "--end",
                    "2027-01-10",
                    "--db",
                    str(db_path),
                ]
            )

        assert "見つかりません" in str(excinfo.value)
        assert not db_path.exists()


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

    def test_entry_grid_subcommand_completes_and_writes_k_report(
        self, seeded_db, tmp_path, capsys
    ):
        db_path, days = seeded_db
        output_path = tmp_path / "out" / "entry-grid.md"

        main(
            [
                "entry-grid",
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
        assert "copilot-backtest entry-grid" in captured.out
        assert "entry_limit_atr_multiple" in captured.out
        assert output_path.exists()
        report_text = output_path.read_text(encoding="utf-8")
        assert "# Backtest entry-limit sensitivity grid: default" in report_text
        assert "| k (ATR multiple) |" in report_text
        assert "| 2.0 |" in report_text

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

    def test_entry_grid_screens_once_and_runs_every_k_value(
        self, seeded_db, tmp_path, monkeypatch
    ):
        db_path, days = seeded_db
        screenings: list[int] = []
        engine_runs: list[int] = []
        entry_values: list[float | None] = []
        original_generate = generate_candidate_stream
        original_run = run_backtest

        def counting_generate(*args, **kwargs):
            screenings.append(1)
            return original_generate(*args, **kwargs)

        def counting_run(*args, **kwargs):
            engine_runs.append(1)
            entry_values.append(args[2].entry_limit_atr_multiple)
            return original_run(*args, **kwargs)

        monkeypatch.setattr(cli_module, "generate_candidate_stream", counting_generate)
        monkeypatch.setattr(cli_module, "run_backtest", counting_run)

        main(
            [
                "entry-grid",
                "--strategy",
                "default",
                "--start",
                days[0].isoformat(),
                "--end",
                days[-1].isoformat(),
                "--db",
                str(db_path),
                "--output",
                str(tmp_path / "entry-grid.md"),
            ]
        )

        assert len(screenings) == 1
        assert len(engine_runs) == len(entry_limit_grid_values())
        assert entry_values == list(entry_limit_grid_values())

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
                "none,regime+earnings",
            ]
        )

        captured = capsys.readouterr()
        assert "Backtest metrics by policy" in captured.out
        # One screening pass feeds both arms: the diff is attributable to the
        # gates and nothing else.
        assert len(screenings) == 1
        report_text = output_path.read_text(encoding="utf-8")
        assert "| Metric | none | regime+earnings |" in report_text

    def test_missing_regime_bars_abort_the_run_with_a_clear_message(
        self, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "copilot.duckdb"
        database = Database(db_path)
        store = MarketStore(database, parquet_root=tmp_path / "bars")
        days = [date(2027, 1, 1 + i) for i in range(10)]
        store.write_bars(
            bars_frame(
                _with_provider_columns(
                    [*flat_bars("SPY", days, 400.0), *flat_bars("AAA", days, 100.0)]
                )
            )
        )
        StateStore(database).init_schema()
        with store.get_connection():
            pass
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

    def test_entry_grid_refuses_a_policy_instead_of_ignoring_it(
        self, seeded_db, tmp_path
    ):
        db_path, days = seeded_db

        with pytest.raises(SystemExit, match=r"entry-grid サブコマンドは --policy"):
            main(
                [
                    "--policy",
                    "regime",
                    "entry-grid",
                    "--strategy",
                    "default",
                    "--start",
                    days[0].isoformat(),
                    "--end",
                    days[-1].isoformat(),
                    "--db",
                    str(db_path),
                    "--output",
                    str(tmp_path / "entry-grid.md"),
                ]
            )


@pytest.mark.usefixtures("two_symbol_universe")
class TestEarningsGuardWiring:
    """`--policy regime+earnings` supplies a real earnings calendar (Issue #201).

    Before this, `build_entry_policy` was always called without
    `earnings_guard_fn`, so the earnings gate could only ever report 0.
    """

    @staticmethod
    def _seed_filings(db_path: Path, filed_dates: Sequence[date]) -> None:
        store = MarketStore(Database(db_path), parquet_root=db_path.parent / "bars")
        store.upsert_fundamentals(
            [
                FundamentalsRecord(
                    accession_no=f"acc-{filed_on.isoformat()}",
                    symbol="AAA",
                    form="10-Q",
                    fiscal_period_end=filed_on - timedelta(days=30),
                    filed_at=pd.Timestamp(filed_on, tz="UTC").to_pydatetime(),
                    revenue=1.0,
                    net_income=1.0,
                    fcf=1.0,
                    equity=1.0,
                    assets=2.0,
                    shares=1.0,
                    source_url="https://www.sec.gov/example",
                    fetched_at=pd.Timestamp("2027-01-20", tz="UTC").to_pydatetime(),
                )
                for filed_on in filed_dates
            ]
        )

    @staticmethod
    def _capture_policy_kwargs(
        monkeypatch: pytest.MonkeyPatch,
    ) -> list[_EarningsGuardFn | None]:
        captured: list[_EarningsGuardFn | None] = []

        def recording(*args, **kwargs):
            captured.append(kwargs.get("earnings_guard_fn"))
            return build_entry_policy(*args, **kwargs)

        monkeypatch.setattr(cli_module, "build_entry_policy", recording)
        return captured

    def _argv(
        self, db_path: Path, days: list[date], output: Path, policy: str
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
            str(output),
            "--policy",
            policy,
        ]

    def test_regime_risk_arm_receives_the_filing_derived_calendar(
        self, seeded_db, tmp_path, capsys, monkeypatch
    ):
        db_path, days = seeded_db
        self._seed_filings(db_path, [days[0] - timedelta(days=91), days[0]])
        captured = self._capture_policy_kwargs(monkeypatch)

        main(self._argv(db_path, days, tmp_path / "policy.md", "regime+earnings"))

        assert len(captured) == 1
        assert captured[0] is not None
        # The coverage line must say how many symbols could be derived at all:
        # a 0-count earnings gate over an empty calendar means something very
        # different from one over a covered universe.
        assert (
            "決算ゲート: 提出履歴（10-K/10-Q）から1/2 銘柄" in capsys.readouterr().out
        )

    def test_the_supplied_lookup_is_point_in_time(
        self, seeded_db, tmp_path, monkeypatch
    ):
        db_path, days = seeded_db
        filed = [days[0] - timedelta(days=91), days[0]]
        self._seed_filings(db_path, filed)
        captured = self._capture_policy_kwargs(monkeypatch)

        main(self._argv(db_path, days, tmp_path / "policy.md", "regime+earnings"))

        guard_fn = captured[0]
        assert guard_fn is not None
        before = guard_fn(days[0] - timedelta(days=1), ("AAA",))
        at = guard_fn(days[0], ("AAA",))
        assert before.lookups_by_symbol["AAA"].recent_event is not None
        assert before.lookups_by_symbol["AAA"].recent_event.earnings_date == filed[0]
        assert at.lookups_by_symbol["AAA"].recent_event is not None
        assert at.lookups_by_symbol["AAA"].recent_event.earnings_date == filed[1]

    def test_arms_that_cannot_use_the_gate_never_read_the_filing_history(
        self, seeded_db, tmp_path, capsys, monkeypatch
    ):
        db_path, days = seeded_db
        self._seed_filings(db_path, [days[0] - timedelta(days=91), days[0]])
        captured = self._capture_policy_kwargs(monkeypatch)

        main(self._argv(db_path, days, tmp_path / "regime.md", "none,regime"))

        assert captured == [None, None]
        assert "決算ゲート" not in capsys.readouterr().out
