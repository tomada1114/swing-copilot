"""P8-30: `copilot-retro` CLI surface.

Only `collect` and `evaluate` exist in this phase (E30.1): the later
`prepare` / `export` / `ingest` subcommands must not be pre-announced in
argparse, because a subcommand that parses but does nothing is worse than one
that plainly does not exist yet.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from swing_copilot.retro.cli import main
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.storage.verdict_records import VerdictRecord
from tests.analysis.conftest import result_payload, symbol_payload
from tests.retro.conftest import bars

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

RUN_DATE = date(2027, 3, 1)
CALENDAR = [RUN_DATE + timedelta(days=offset) for offset in range(30)]


def _rows(db_path: Path, sql: str) -> list[tuple[object, ...]]:
    with Database(db_path).connect() as conn:
        return conn.execute(sql).fetchall()


class TestCollectCommand:
    def test_scans_the_given_reports_directory_into_the_given_database(
        self, tmp_path: Path, reports_root: Path, write_run: Callable[..., Path]
    ) -> None:
        write_run()
        db_path = tmp_path / "retro.duckdb"

        main(["collect", "--reports-dir", str(reports_root), "--db", str(db_path)])

        assert _rows(db_path, "SELECT symbol FROM verdicts") == [("AAPL",)]

    def test_reports_the_scan_summary_on_stdout(
        self,
        tmp_path: Path,
        reports_root: Path,
        write_run: Callable[..., Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        write_run()

        main(
            [
                "collect",
                "--reports-dir",
                str(reports_root),
                "--db",
                str(tmp_path / "retro.duckdb"),
            ]
        )

        assert "1" in capsys.readouterr().out

    def test_an_empty_reports_directory_exits_successfully(
        self, tmp_path: Path, reports_root: Path
    ) -> None:
        main(
            [
                "collect",
                "--reports-dir",
                str(reports_root),
                "--db",
                str(tmp_path / "retro.duckdb"),
            ]
        )

    def test_skip_notes_are_surfaced_to_the_operator(
        self,
        tmp_path: Path,
        reports_root: Path,
        write_run: Callable[..., Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        write_run(analysis_input=None)

        main(
            [
                "collect",
                "--reports-dir",
                str(reports_root),
                "--db",
                str(tmp_path / "retro.duckdb"),
            ]
        )

        assert "analysis_input.json" in capsys.readouterr().out


class TestEvaluateCommand:
    @staticmethod
    def _seed(db_path: Path) -> None:
        database = Database(db_path)
        state_store = StateStore(database)
        state_store.init_schema()
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id,
            [
                VerdictRecord(
                    run_id=run_id,
                    symbol="AAPL",
                    as_of=RUN_DATE,
                    strategy_key="default",
                    recommendation="proceed",
                    reasons=(),
                    no_trade=False,
                )
            ],
            [],
        )
        market_store = MarketStore(database, parquet_root=db_path.parent / "bars")
        market_store.write_bars(bars("SPY", dict.fromkeys(CALENDAR, 100.0)))
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, CALENDAR[5]: 101.5}))

    def test_classifies_matured_verdicts_into_verdict_outcomes(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "retro.duckdb"
        self._seed(db_path)

        main(["evaluate", "--as-of", CALENDAR[10].isoformat(), "--db", str(db_path)])

        assert _rows(
            db_path,
            "SELECT symbol, horizon_days, as_of, classification FROM verdict_outcomes",
        ) == [("AAPL", 5, CALENDAR[5], "HIT")]

    def test_reports_the_evaluation_summary_on_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "retro.duckdb"
        self._seed(db_path)

        main(["evaluate", "--as-of", CALENDAR[10].isoformat(), "--db", str(db_path)])

        assert "1" in capsys.readouterr().out

    def test_a_fresh_database_evaluates_nothing_without_raising(
        self, tmp_path: Path
    ) -> None:
        main(
            [
                "evaluate",
                "--as-of",
                CALENDAR[10].isoformat(),
                "--db",
                str(tmp_path / "fresh.duckdb"),
            ]
        )

    def test_requires_an_explicit_as_of(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            main(["evaluate", "--db", str(tmp_path / "retro.duckdb")])

    def test_rejects_a_missing_settings_file(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            main(
                [
                    "evaluate",
                    "--as-of",
                    CALENDAR[10].isoformat(),
                    "--db",
                    str(tmp_path / "retro.duckdb"),
                    "--settings",
                    str(tmp_path / "absent.yaml"),
                ]
            )


class TestCollectThenEvaluate:
    """The roadmap P8-30 動作確認 bullets, driven through the CLI end to end."""

    @staticmethod
    def _archive(write_run: Callable[..., Path]) -> None:
        # One symbol with prices and one without: the second proves a missing
        # bar degrades to a skip instead of breaking the batch.
        write_run(
            result=result_payload(
                symbols=[
                    symbol_payload(),
                    symbol_payload(
                        symbol="NOBAR",
                        news_summary=None,
                        filing_analyses=[],
                        verdict={"recommendation": "skip", "reasons": []},
                    ),
                ]
            )
        )

    @staticmethod
    def _seed_prices(db_path: Path) -> None:
        market_store = MarketStore(
            Database(db_path), parquet_root=db_path.parent / "bars"
        )
        market_store.write_bars(bars("SPY", dict.fromkeys(CALENDAR, 100.0)))
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, CALENDAR[5]: 101.5}))

    @staticmethod
    def _counts(db_path: Path) -> tuple[int, ...]:
        counts: list[int] = []
        with Database(db_path).connect() as conn:
            for table in ("verdicts", "verdict_sources", "verdict_outcomes"):
                row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608 - fixed literal table names
                assert row is not None
                counts.append(int(row[0]))
        return tuple(counts)

    def _run_both(self, reports_root: Path, db_path: Path) -> None:
        main(["collect", "--reports-dir", str(reports_root), "--db", str(db_path)])
        main(["evaluate", "--as-of", CALENDAR[10].isoformat(), "--db", str(db_path)])

    def test_collect_then_evaluate_populates_all_three_tables(
        self, tmp_path: Path, reports_root: Path, write_run: Callable[..., Path]
    ) -> None:
        db_path = tmp_path / "retro.duckdb"
        self._archive(write_run)
        self._seed_prices(db_path)

        self._run_both(reports_root, db_path)

        verdicts, sources, outcomes = self._counts(db_path)
        assert (verdicts, sources, outcomes) == (2, 2, 1)

    def test_rerunning_the_whole_flow_does_not_duplicate_rows(
        self, tmp_path: Path, reports_root: Path, write_run: Callable[..., Path]
    ) -> None:
        db_path = tmp_path / "retro.duckdb"
        self._archive(write_run)
        self._seed_prices(db_path)

        self._run_both(reports_root, db_path)
        first = self._counts(db_path)
        self._run_both(reports_root, db_path)

        assert self._counts(db_path) == first

    def test_a_symbol_without_bars_is_skipped_without_breaking_the_batch(
        self,
        tmp_path: Path,
        reports_root: Path,
        write_run: Callable[..., Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        db_path = tmp_path / "retro.duckdb"
        self._archive(write_run)
        self._seed_prices(db_path)

        self._run_both(reports_root, db_path)

        assert _rows(db_path, "SELECT symbol FROM verdict_outcomes") == [("AAPL",)]
        assert "NOBAR" in capsys.readouterr().out


class TestSubcommandSurface:
    def test_requires_a_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            main([])

    @pytest.mark.parametrize("command", ["prepare", "export", "ingest"])
    def test_later_phase_subcommands_are_not_registered_yet(self, command: str) -> None:
        # P8-31/P8-32 add these; until then they must fail loudly rather than
        # parse into a silent no-op.
        with pytest.raises(SystemExit):
            main([command])
