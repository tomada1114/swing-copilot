"""P8-30..P8-32: `copilot-retro` CLI surface.

`collect`, `evaluate`, `export`, the `prepare` umbrella, and `ingest`. Since
Issue #189 every subcommand touches the database: `ingest` accumulates the
verified narrations there so the L2 qualitative gate has something to count.
"""

from __future__ import annotations

import io
import json
import logging
import re
from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from rich.console import Console

from swing_copilot.config import Secrets
from swing_copilot.retro.cli import _print_notes, main
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


@pytest.fixture(autouse=True)
def _offline_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """No API keys: `main()` now loads secrets for every subcommand (Issue #381).

    `_env_file=None` isolates every test in this file from whatever `.env` a
    developer has locally -- previously only `export`/`prepare` needed this
    (they build real Finnhub/EDGAR clients from it), but `main()` now loads
    secrets up front, before dispatch, so `logging.getLogger` configuration
    would otherwise pick up real secret values too (mirrors
    `tests/test_config.py`). A test that needs a specific secret value (e.g.
    a redaction test) overrides this via its own `monkeypatch.setattr` call.
    """
    monkeypatch.setattr(
        "swing_copilot.retro.cli.load_secrets",
        lambda: Secrets(_env_file=None),  # type: ignore[call-arg]
    )


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

    def test_an_unreadable_archive_emits_collect_unreadable_on_stderr(
        self,
        tmp_path: Path,
        reports_root: Path,
        write_run: Callable[..., Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Issue #374: the exit code stays 0; the tag is what makes it visible.

        CI's `push` step gates on `success()`, so `collect` failing on a
        broken archive would take that day's price/fundamental sync down with
        it -- the fail-soft-per-run contract does not change. The tag exists
        so a skipped archive shows up as more than a note buried in stdout.
        """
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

        assert "COLLECT_UNREADABLE[1]:" in capsys.readouterr().err

    def test_a_fully_readable_scan_emits_nothing_on_stderr(
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

        assert capsys.readouterr().err == ""

    def test_a_note_containing_markup_like_text_prints_verbatim_without_raising(
        self,
    ) -> None:
        """Issue #376 review: a note can carry arbitrary exception text.

        Before this fix, `_print_notes` interpolated each note into
        `f"[yellow]{note}[/yellow]"` and let Rich parse the result as markup.
        A note containing something that looks like a closing tag -- e.g. an
        operator-supplied `--reports-dir` path surfaced via `OSError`, or a
        document's own failure message -- would raise
        `rich.errors.MarkupError` and turn the deliberately fail-soft
        `collect` command into a hard crash.
        """
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=False, width=200)
        note = "archive[/]broken: フィールド (extra_forbidden)"

        _print_notes(console, (note,))

        assert note in buffer.getvalue()


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
    """P8-31: `export` writes the dossier; `prepare` runs the whole chain.

    Offline secrets come from the module-level `_offline_secrets` autouse
    fixture above.
    """

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


class _RecordingHandler(logging.Handler):
    """Collects delivered records, for asserting a level actually filtered."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _raise_runtime_error(message: str) -> None:
    raise RuntimeError(message)


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """Undo whatever `configure_cli_logging` mutates on the loggers below.

    `configure_cli_logging` is process-global by design (it configures
    `logging.root`), so ANY test that calls `main()` must not leak
    handlers, levels, or filters into whatever runs next in the suite.
    Restoring `root_logger.handlers` alone is not enough: a handler
    object that already existed before this test (e.g. pytest's own log
    capture handler) is reused, not replaced, across `main()` calls, so
    `configure_cli_logging` mutates that SAME object's `.filters` list in
    place. Each pre-existing handler's own filter list is snapshotted and
    restored too, or a redaction filter added by one test could survive
    into the next and shadow the one that test installs.
    """
    root_logger = logging.getLogger()
    application_logger = logging.getLogger("swing_copilot")
    saved_handlers = list(root_logger.handlers)
    saved_root_filters = list(root_logger.filters)
    saved_handler_filters = [list(handler.filters) for handler in saved_handlers]
    saved_root_level = root_logger.level
    saved_application_level = application_logger.level
    try:
        yield
    finally:
        root_logger.handlers = saved_handlers
        root_logger.filters = saved_root_filters
        for handler, filters in zip(saved_handlers, saved_handler_filters, strict=True):
            handler.filters = filters
        root_logger.setLevel(saved_root_level)
        application_logger.setLevel(saved_application_level)


def _patch_secrets(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Point `retro/cli.py`'s `load_secrets()` at isolated, offline `Secrets`.

    `_env_file=None` isolates this from whatever `.env` a developer has
    locally, mirroring `TestExportCommand._offline_secrets` -- a real key
    would otherwise let `_freshness_sources()` build a real network client.
    """
    monkeypatch.setattr(
        "swing_copilot.retro.cli.load_secrets",
        lambda: Secrets(_env_file=None, **overrides),  # type: ignore[call-arg]
    )


class TestLoggingConfiguration:
    """Issue #381: `main()` must configure logging before any other work.

    Before this fix `retro/cli.py` never called any of `logging.basicConfig`/
    `dictConfig`/`addHandler`, so every `logger.exception(...)` in the
    `retro` package fell through to `logging.lastResort`: WARNING-and-above
    only, unformatted, uncontrollable by `--log-level`, and -- because
    `export`/`prepare` make authenticated Finnhub/EDGAR calls via
    `_freshness_sources()` -- unredacted. `copilot-retro collect` runs in the
    daily CI job under `continue-on-error: true`, so this log is the only
    forensic trail a failure there leaves.
    """

    def test_a_record_reaches_a_configured_handler_not_lastresort(
        self, tmp_path: Path, reports_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_secrets(monkeypatch)
        root_logger = logging.getLogger()
        # Start from no handlers at all, i.e. the state that makes
        # `logging.lastResort` the fallback -- proves `main()` itself is what
        # installs a real handler, not some earlier test in the session.
        root_logger.handlers = []
        root_logger.filters = []

        main(
            [
                "collect",
                "--reports-dir",
                str(reports_root),
                "--db",
                str(tmp_path / "retro.duckdb"),
            ]
        )

        assert root_logger.handlers, "main() must install a root handler"
        handler = root_logger.handlers[0]
        assert handler is not logging.lastResort
        record = logging.LogRecord(
            name="swing_copilot.retro.cli",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="boom",
            args=(),
            exc_info=None,
        )
        formatted = handler.format(record)
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} WARNING "
            r"swing_copilot\.retro\.cli: boom",
            formatted,
        ), formatted

    def test_default_log_level_filters_out_debug_records(
        self, tmp_path: Path, reports_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_secrets(monkeypatch)

        main(
            [
                "collect",
                "--reports-dir",
                str(reports_root),
                "--db",
                str(tmp_path / "retro.duckdb"),
            ]
        )

        assert self._debug_records_delivered() == []

    def test_log_level_flag_lets_debug_records_through(
        self, tmp_path: Path, reports_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_secrets(monkeypatch)

        main(
            [
                "--log-level",
                "DEBUG",
                "collect",
                "--reports-dir",
                str(reports_root),
                "--db",
                str(tmp_path / "retro.duckdb"),
            ]
        )

        assert len(self._debug_records_delivered()) == 1

    @staticmethod
    def _debug_records_delivered() -> list[logging.LogRecord]:
        """Emit one DEBUG record and report whether a handler received it."""
        capture = _RecordingHandler()
        logger = logging.getLogger("swing_copilot.retro.cli.test")
        logger.addHandler(capture)
        try:
            logger.debug("only visible with --log-level DEBUG")
        finally:
            logger.removeHandler(capture)
        return capture.records

    def test_redacts_a_configured_secret_from_message_and_traceback(
        self,
        tmp_path: Path,
        reports_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _patch_secrets(monkeypatch, finnhub_api_key="finnhub-sekrit-retro")
        # A handler's filters are cumulative across every earlier call to
        # `configure_cli_logging` in this process (each `main()` call adds
        # one, it never replaces). `SecretRedactionFilter.filter()` nulls
        # `record.exc_info` once it has redacted a traceback, so an unrelated
        # filter left over from another test -- e.g. real secrets picked up
        # from a developer's local `.env` by a test elsewhere in the suite
        # that does not patch `load_secrets` -- would claim the record first
        # and this test's own filter would never see `exc_info` at all.
        # Starting from a clean filter set isolates the assertion from that.
        for existing_handler in logging.root.handlers:
            existing_handler.filters = []

        main(
            [
                "collect",
                "--reports-dir",
                str(reports_root),
                "--db",
                str(tmp_path / "retro.duckdb"),
            ]
        )
        logger = logging.getLogger("swing_copilot.retro.cli.test")

        with caplog.at_level(logging.ERROR):
            try:
                _raise_runtime_error("401 for token=finnhub-sekrit-retro")
            except RuntimeError:
                logger.exception("fetch failed")

        assert "finnhub-sekrit-retro" not in caplog.text
        assert "[REDACTED]" in caplog.text
        record = caplog.records[-1]
        assert record.exc_text is not None
        assert "finnhub-sekrit-retro" not in record.exc_text
        assert "[REDACTED]" in record.exc_text
