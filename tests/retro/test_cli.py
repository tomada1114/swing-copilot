"""P8-30..P8-32: `copilot-retro` CLI surface.

`collect`, `evaluate`, `export`, the `prepare` umbrella, and `ingest`. Since
Issue #189 every subcommand touches the database: `ingest` accumulates the
verified narrations there so the L2 qualitative gate has something to count.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from swing_copilot.config import Secrets
from swing_copilot.retro.cli import main
from swing_copilot.retro.schemas import RetroInput
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.storage.verdict_records import VerdictRecord
from tests.analysis.conftest import result_payload, symbol_payload
from tests.retro.conftest import bars, retro_input_payload, retro_result_payload

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

RUN_DATE = date(2027, 3, 1)
CALENDAR = [RUN_DATE + timedelta(days=offset) for offset in range(30)]


def _rows(db_path: Path, sql: str) -> list[tuple[object, ...]]:
    with Database(db_path).connect() as conn:
        return conn.execute(sql).fetchall()


def _archive(write_run: Callable[..., Path]) -> None:
    """Write one archived run: one symbol with prices and one without.

    The second symbol proves a missing bar degrades to a skip instead of
    breaking the batch.
    """
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


def _seed_prices(db_path: Path) -> None:
    """Give the calendar benchmark and one symbol the bars evaluation needs."""
    market_store = MarketStore(Database(db_path), parquet_root=db_path.parent / "bars")
    market_store.write_bars(bars("SPY", dict.fromkeys(CALENDAR, 100.0)))
    market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0, CALENDAR[5]: 101.5}))


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
        # An *existing but empty* bars root: zero matured slices stays
        # fail-soft, only a missing root is fatal (Issue #221).
        (tmp_path / "bars").mkdir()
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

    def test_a_db_without_its_sibling_bars_root_fails_instead_of_evaluating(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A DuckDB copy whose `bars/` was left behind is fatal (Issue #221).

        Without the guard every forward return is computed from zero bars, so
        nothing matures and the run reports "評価 0 slice" as though the
        window simply held no verdict worth classifying.
        """
        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "evaluate",
                    "--as-of",
                    CALENDAR[10].isoformat(),
                    "--db",
                    str(tmp_path / "retro.duckdb"),
                ]
            )

        message = str(excinfo.value)
        assert "Parquetディレクトリが見つかりません" in message
        assert str(tmp_path / "bars") in message
        assert "評価" not in capsys.readouterr().out


class TestCollectThenEvaluate:
    """The roadmap P8-30 動作確認 bullets, driven through the CLI end to end."""

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
        _archive(write_run)
        _seed_prices(db_path)

        self._run_both(reports_root, db_path)

        verdicts, sources, outcomes = self._counts(db_path)
        assert (verdicts, sources, outcomes) == (2, 2, 1)

    def test_rerunning_the_whole_flow_does_not_duplicate_rows(
        self, tmp_path: Path, reports_root: Path, write_run: Callable[..., Path]
    ) -> None:
        db_path = tmp_path / "retro.duckdb"
        _archive(write_run)
        _seed_prices(db_path)

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
        _archive(write_run)
        _seed_prices(db_path)

        self._run_both(reports_root, db_path)

        assert _rows(db_path, "SELECT symbol FROM verdict_outcomes") == [("AAPL",)]
        assert "NOBAR" in capsys.readouterr().out


class TestExportCommand:
    """P8-31: `export` writes the dossier; `prepare` runs the whole chain."""

    @pytest.fixture(autouse=True)
    def _offline_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No API keys: the CLI must build no live client in the test suite.

        `_env_file=None` isolates this from whatever `.env` a developer has,
        which is what keeps the export offline here -- a real key would
        otherwise construct a real adapter (mirrors `tests/test_config.py`).
        """
        monkeypatch.setattr(
            "swing_copilot.retro.cli.load_secrets",
            lambda: Secrets(_env_file=None),  # type: ignore[call-arg]
        )

    def test_writes_the_dossier_under_the_reports_root(
        self, tmp_path: Path, reports_root: Path, write_run: Callable[..., Path]
    ) -> None:
        db_path = tmp_path / "retro.duckdb"
        _archive(write_run)
        _seed_prices(db_path)
        self._collect_and_evaluate(reports_root, db_path)

        main(
            [
                "export",
                "--as-of",
                CALENDAR[10].isoformat(),
                "--db",
                str(db_path),
                "--reports-dir",
                str(reports_root),
            ]
        )

        destination = (
            reports_root / "retro" / CALENDAR[10].isoformat() / "retro_input.json"
        )
        document = RetroInput.model_validate(
            json.loads(destination.read_text(encoding="utf-8"))
        )
        assert document.as_of == CALENDAR[10]
        assert document.evaluation.lookback_window_days == 90

    def test_reports_the_export_summary_on_stdout(
        self,
        tmp_path: Path,
        reports_root: Path,
        write_run: Callable[..., Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        db_path = tmp_path / "retro.duckdb"
        _archive(write_run)
        _seed_prices(db_path)
        self._collect_and_evaluate(reports_root, db_path)

        main(
            [
                "export",
                "--as-of",
                CALENDAR[10].isoformat(),
                "--db",
                str(db_path),
                "--reports-dir",
                str(reports_root),
            ]
        )

        assert "retro_input.json" in capsys.readouterr().out

    def test_exits_when_the_proposal_ledger_cannot_be_read(
        self, tmp_path: Path, reports_root: Path, write_run: Callable[..., Path]
    ) -> None:
        # `export` names the closed proposals a re-proposal must justify, so an
        # unreadable ledger has to stop it with a message rather than a
        # traceback -- the same treatment `ingest` already gives it.
        db_path = tmp_path / "retro.duckdb"
        _archive(write_run)
        _seed_prices(db_path)
        self._collect_and_evaluate(reports_root, db_path)
        ledger = tmp_path / "proposals.md"
        ledger.write_bytes("| RP-001 | 却下済み | rejected |\n".encode("shift_jis"))

        with pytest.raises(SystemExit, match="Proposal ledger could not be read"):
            main(
                [
                    "export",
                    "--as-of",
                    CALENDAR[10].isoformat(),
                    "--db",
                    str(db_path),
                    "--reports-dir",
                    str(reports_root),
                    "--ledger",
                    str(ledger),
                ]
            )

    def test_prepare_runs_collect_evaluate_and_export_in_one_pass(
        self, tmp_path: Path, reports_root: Path, write_run: Callable[..., Path]
    ) -> None:
        db_path = tmp_path / "retro.duckdb"
        _archive(write_run)
        _seed_prices(db_path)

        main(
            [
                "prepare",
                "--as-of",
                CALENDAR[10].isoformat(),
                "--db",
                str(db_path),
                "--reports-dir",
                str(reports_root),
            ]
        )

        assert _rows(db_path, "SELECT symbol FROM verdict_outcomes") == [("AAPL",)]
        assert (
            reports_root / "retro" / CALENDAR[10].isoformat() / "retro_input.json"
        ).is_file()

    def test_export_on_an_empty_database_still_writes_a_valid_dossier(
        self, tmp_path: Path, reports_root: Path
    ) -> None:
        db_path = tmp_path / "retro.duckdb"
        (tmp_path / "bars").mkdir()

        main(
            [
                "export",
                "--as-of",
                CALENDAR[10].isoformat(),
                "--db",
                str(db_path),
                "--reports-dir",
                str(reports_root),
            ]
        )

        destination = (
            reports_root / "retro" / CALENDAR[10].isoformat() / "retro_input.json"
        )
        document = RetroInput.model_validate(
            json.loads(destination.read_text(encoding="utf-8"))
        )
        assert document.surprises.items == []

    def test_reads_the_proposal_ledger_it_was_pointed_at(
        self, tmp_path: Path, reports_root: Path
    ) -> None:
        ledger = tmp_path / "proposals.md"
        ledger.write_text(
            "| RP-ID | status |\n|---|---|\n| RP-007 | rejected |\n",
            encoding="utf-8",
        )
        (tmp_path / "bars").mkdir()

        main(
            [
                "export",
                "--as-of",
                CALENDAR[10].isoformat(),
                "--db",
                str(tmp_path / "retro.duckdb"),
                "--reports-dir",
                str(reports_root),
                "--ledger",
                str(ledger),
            ]
        )

        destination = (
            reports_root / "retro" / CALENDAR[10].isoformat() / "retro_input.json"
        )
        document = RetroInput.model_validate(
            json.loads(destination.read_text(encoding="utf-8"))
        )
        assert document.proposals_ledger.rejected_proposal_ids == ["RP-007"]

    @staticmethod
    def _collect_and_evaluate(reports_root: Path, db_path: Path) -> None:
        main(["collect", "--reports-dir", str(reports_root), "--db", str(db_path)])
        main(["evaluate", "--as-of", CALENDAR[10].isoformat(), "--db", str(db_path)])


class TestSubcommandSurface:
    def test_requires_a_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            main([])

    def test_ingest_requires_the_retrospective_directory(self) -> None:
        with pytest.raises(SystemExit):
            main(["ingest"])


class TestIngest:
    def _retro_dir(self, tmp_path: Path, **overrides: object) -> Path:
        directory = tmp_path / "reports" / "retro" / "2027-03-29"
        directory.mkdir(parents=True)
        (directory / "retro_input.json").write_text(
            json.dumps(retro_input_payload()), encoding="utf-8"
        )
        (directory / "retro_result.json").write_text(
            json.dumps(retro_result_payload(**overrides)), encoding="utf-8"
        )
        return directory

    def _ingest_argv(self, directory: Path, tmp_path: Path) -> list[str]:
        return [
            "ingest",
            str(directory),
            "--ledger",
            str(tmp_path / "docs" / "retro" / "proposals.md"),
            "--db",
            str(tmp_path / "copilot.duckdb"),
        ]

    def test_writes_the_report_and_generates_the_ledger(self, tmp_path: Path) -> None:
        directory = self._retro_dir(tmp_path)
        ledger = tmp_path / "docs" / "retro" / "proposals.md"

        main(self._ingest_argv(directory, tmp_path))

        assert (directory / "retro_report.md").is_file()
        assert "| RP-001 |" in ledger.read_text(encoding="utf-8")

    def test_reports_the_recorded_proposal(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        directory = self._retro_dir(tmp_path)

        main(self._ingest_argv(directory, tmp_path))

        assert "RP-001" in capsys.readouterr().out

    def test_accumulates_the_verified_narrations_in_the_database(
        self, tmp_path: Path
    ) -> None:
        """Issue #189: the failure_class must outlive the gitignored report."""
        directory = self._retro_dir(tmp_path)
        db_path = tmp_path / "copilot.duckdb"

        main(self._ingest_argv(directory, tmp_path))

        store = StateStore(Database(db_path))
        narrations = store.get_retro_narrations(date(2027, 3, 29))
        assert [(row.symbol, row.failure_class) for row in narrations] == [
            ("AAPL", "information_absent")
        ]

    def test_exits_when_the_result_answers_another_export(self, tmp_path: Path) -> None:
        directory = self._retro_dir(tmp_path, as_of="2027-04-30")

        with pytest.raises(SystemExit, match="as_of"):
            main(self._ingest_argv(directory, tmp_path))

        assert not (directory / "retro_report.md").exists()
        store = StateStore(Database(tmp_path / "copilot.duckdb"))
        assert store.get_retro_narrations(date(2027, 3, 29)) == ()
