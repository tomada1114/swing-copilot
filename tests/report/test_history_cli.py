"""Read-only history CLI contracts: `copilot-history` (P1-05).

Covers each subcommand against a populated fixture DB and an empty DB,
Example 3's traceback-free non-zero exit for an unknown `run_id`, and
REQ-007's read-only guarantee (no subcommand mutates any table).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pandas as pd
import pytest

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ANALYSIS_RESULT_FILENAME,
)
from swing_copilot.models import Position, RunMode
from swing_copilot.paper.journal import PaperJournal
from swing_copilot.report.history_cli import main
from swing_copilot.report.incomplete_runs import ANALYSIS_INCOMPLETE_EXIT_CODE
from swing_copilot.risk.checks import RiskAssessment
from swing_copilot.screening.base import (
    Candidate,
    RejectionReasonCode,
    RejectionRecord,
    RejectionStage,
)
from swing_copilot.storage.audit_records import ScreeningRunMeta
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.paper_records import (
    PositionExcursionRecord,
    TradeDecisionRecord,
)

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.storage.database import Database
    from swing_copilot.storage.state_store import StateStore

_TABLES = (
    "universe_membership",
    "runs",
    "run_steps",
    "signals",
    "candidates",
    "screening_rejections",
    "risk_assessments",
    "positions",
    "trades_journal",
    "text_items",
)


def _db_path(state_store: StateStore) -> str:
    return str(state_store._database.db_path)  # noqa: SLF001


def _write_run_archive(tmp_path: Path, run_date: date, *, has_result: bool) -> UUID:
    """Build one `reports/<date>/<run_id>/` archive under an isolated root.

    Detection is an existence check, so the documents' contents are
    irrelevant here; what matters is which of the two files is present.
    """
    run_id = uuid4()
    directory = tmp_path / "reports" / run_date.isoformat() / str(run_id)
    directory.mkdir(parents=True)
    (directory / ANALYSIS_INPUT_FILENAME).write_text("{}", encoding="utf-8")
    if has_result:
        (directory / ANALYSIS_RESULT_FILENAME).write_text("{}", encoding="utf-8")
    return run_id


def _insert_run_row(state_store: StateStore, run_id: UUID, run_date: date) -> None:
    """Record the archive's run as a finished deterministic pipeline."""
    with state_store._database.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
            "started_at) VALUES (?, ?, 'live', 'cfg', 'success', ?)",
            [str(run_id), run_date, datetime(2026, 8, 10, 18, 30, tzinfo=UTC)],
        )


def _candidate(symbol: str = "AAPL", rank: int = 1) -> Candidate:
    return Candidate(
        symbol,
        date(2026, 7, 20),
        ("trend_sma",),
        {"close": 100.0, "score": 0.5},
        rank,
    )


def _populate(state_store: StateStore) -> UUID:
    """Example 1's shape: 1 run with 2 candidates, 1 rejection, 1 decision."""
    run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "cfg")
    state_store.record_screening_results(
        [_candidate("AAPL", 1), _candidate("MSFT", 2)],
        [
            RejectionRecord(
                symbol="JPM",
                stage=RejectionStage.TECHNICAL_SIGNAL,
                reason_code=RejectionReasonCode.SIGNAL_TREND_NOT_MET,
                detail={"rsi14": 70.0},
            )
        ],
        [],
        ScreeningRunMeta(run_id, "default", date(2026, 7, 20), 5),
    )
    state_store.record_risk_assessments(
        [
            RiskAssessment(
                symbol="AAPL",
                status="approved",
                max_shares=10,
                entry_price=100.0,
                stop_price=95.0,
                reasons=(),
                binding_constraint="trade_risk",
            )
        ],
        run_id,
    )
    state_store.record_trade_decision(
        TradeDecisionRecord(
            run_id=run_id,
            symbol="AAPL",
            strategy_key="default",
            position_id=None,
            decision="followed",
            reason_memo="出来高増加",
            virtual_fill_price=100.0,
        )
    )
    return run_id


def _snapshot(database: Database) -> dict[str, list[tuple[str, ...]]]:
    with database.connect() as conn:
        return {
            table: sorted(
                tuple(str(value) for value in row)
                for row in conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            )
            for table in _TABLES
        }


class TestRuns:
    def test_populated_db_shows_run_summary_row(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_id = _populate(state_store)

        main(["runs", "--db", _db_path(state_store)])

        output = capsys.readouterr().out
        assert str(run_id) in output
        assert "2026-07-20" in output
        # Example 1: 2 candidates, 1 rejection, 1 decision.
        assert "2" in output
        assert "1" in output

    def test_empty_db_shows_no_record_message_without_exception(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["runs", "--db", _db_path(state_store)])

        assert "記録なし" in capsys.readouterr().out

    def test_limit_greater_than_actual_rows_shows_all_without_error(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate(state_store)

        main(["runs", "--limit", "50", "--db", _db_path(state_store)])

        assert "記録なし" not in capsys.readouterr().out


class TestRunDetail:
    def test_known_run_id_shows_candidates_risk_and_decisions(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_id = _populate(state_store)

        main(["run", "--run-id", str(run_id), "--db", _db_path(state_store)])

        output = capsys.readouterr().out
        assert "AAPL" in output
        assert "trade_risk" in output
        assert "followed" in output

    def test_unknown_run_id_exits_nonzero_without_traceback(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "run",
                    "--run-id",
                    "nonexistent-id",
                    "--db",
                    _db_path(state_store),
                ]
            )

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert "Traceback" not in captured.out
        assert "Traceback" not in captured.err

    def test_syntactically_valid_but_unrecorded_run_id_exits_nonzero(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "run",
                    "--run-id",
                    str(uuid4()),
                    "--db",
                    _db_path(state_store),
                ]
            )

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert "Traceback" not in captured.out
        assert "Traceback" not in captured.err


class TestSymbol:
    def test_known_symbol_shows_candidacy_and_decision_history(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate(state_store)

        main(["symbol", "AAPL", "--db", _db_path(state_store)])

        output = capsys.readouterr().out
        assert "AAPL" in output
        assert "followed" in output

    def test_symbol_never_a_candidate_shows_no_record_message_exit_zero(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate(state_store)

        main(["symbol", "ZZZZ", "--db", _db_path(state_store)])

        assert "ZZZZの記録はありません" in capsys.readouterr().out

    def test_empty_db_shows_no_record_message_without_exception(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["symbol", "AAPL", "--db", _db_path(state_store)])

        assert "AAPLの記録はありません" in capsys.readouterr().out

    def test_symbol_is_case_normalized(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate(state_store)

        main(["symbol", "aapl", "--db", _db_path(state_store)])

        assert "AAPLの記録はありません" not in capsys.readouterr().out


class TestRejections:
    def test_known_run_id_shows_rejections_table(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_id = _populate(state_store)

        main(["rejections", "--run-id", str(run_id), "--db", _db_path(state_store)])

        output = capsys.readouterr().out
        assert "JPM" in output
        assert "SIGNAL_TREND_NOT_MET" in output

    def test_unknown_run_id_exits_nonzero_without_traceback(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "rejections",
                    "--run-id",
                    str(uuid4()),
                    "--db",
                    _db_path(state_store),
                ]
            )

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert "Traceback" not in captured.out
        assert "Traceback" not in captured.err

    def test_run_with_zero_rejections_shows_no_record_message(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_id = state_store.start_run(date(2026, 7, 20), RunMode.LIVE, "cfg")

        main(["rejections", "--run-id", str(run_id), "--db", _db_path(state_store)])

        assert "記録なし" in capsys.readouterr().out


class TestPerformance:
    def test_closed_positions_show_summary_stats(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state_store.upsert_position(
            Position(
                position_id=uuid4(),
                symbol="AAPL",
                is_paper=True,
                entry_date=date(2026, 7, 1),
                entry_price=100.0,
                shares=10,
                status="closed",
                close_date=date(2026, 7, 10),
                close_price=110.0,
                exit_reason="target",
            )
        )

        main(["performance", "--db", _db_path(state_store)])

        output = capsys.readouterr().out
        assert "1" in output  # closed_trade_count

    def test_empty_db_shows_no_record_message_without_exception(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["performance", "--db", _db_path(state_store)])

        assert "記録なし" in capsys.readouterr().out

    def test_renders_p1_06_extended_summary_fields_not_just_the_old_four(
        self,
        state_store: StateStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """REQ-006: the CLI must render P1-06's full extended summary.

        win_rate, expectancy, profit_factor, avg_r_multiple, the
        r_multiple_omitted warning, by_exit_reason/by_strategy breakdowns,
        and spy_return_pct -- not merely the pre-P1-06 4-field summary.
        """
        # `_run_performance` builds `MarketStore(database)` with the library
        # default `parquet_root` ("data/bars", CWD-relative), the same
        # convention `pipeline/daily.py` uses -- so bars must be written
        # under a chdir'd CWD to be found, mirroring
        # `tests/pipeline/test_cli.py`'s `monkeypatch.chdir(tmp_path)`.
        monkeypatch.chdir(tmp_path)
        journal = PaperJournal(state_store)
        database = state_store._database  # noqa: SLF001
        market_store = MarketStore(database, parquet_root=tmp_path / "data" / "bars")

        winner = Position(
            position_id=uuid4(),
            symbol="AAPL",
            is_paper=True,
            entry_date=date(2026, 7, 1),
            entry_price=100.0,
            shares=10,
            status="open",
            stop_price=90.0,  # risk_per_share=10, pnl=+100 -> r=+1.0
        )
        loser = Position(
            position_id=uuid4(),
            symbol="MSFT",
            is_paper=True,
            entry_date=date(2026, 7, 1),
            entry_price=100.0,
            shares=10,
            status="open",
            stop_price=90.0,  # risk_per_share=10, pnl=-100 -> r=-1.0
        )
        state_store.upsert_position(winner)
        state_store.upsert_position(loser)
        run_id = uuid4()
        journal.record_decision(
            run_id,
            "AAPL",
            "trend_follow",
            "followed",
            None,
            100.0,
            position_id=winner.position_id,
        )
        journal.record_decision(
            run_id,
            "MSFT",
            "mean_revert",
            "followed",
            None,
            100.0,
            position_id=loser.position_id,
        )
        journal.close_position(winner.position_id, date(2026, 7, 10), 110.0, "target")
        journal.close_position(loser.position_id, date(2026, 7, 10), 90.0, "stop_loss")
        state_store.upsert_position_excursions(
            [
                PositionExcursionRecord(
                    winner.position_id, date(2026, 7, 10), -2.0, 15.0, "OK"
                ),
                PositionExcursionRecord(
                    loser.position_id, date(2026, 7, 10), -15.0, 2.0, "OK"
                ),
            ]
        )
        days = pd.date_range(date(2026, 7, 1), date(2026, 7, 20), freq="D")
        rows = [
            {
                "symbol": "SPY",
                "date": day.date(),
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": 1_000_000,
                "provider": "yfinance",
                "fetched_at": datetime(2026, 7, 20, tzinfo=UTC),
            }
            for day, price in zip(
                days, [500.0] * (len(days) - 1) + [550.0], strict=True
            )
        ]
        market_store.write_bars(pd.DataFrame(rows))

        main(["performance", "--db", _db_path(state_store)])

        output = capsys.readouterr().out
        # win_rate=0.5, expectancy=0.0, profit_factor=100/100=1.0,
        # avg_r_multiple=(1.0 + -1.0)/2=0.0, no omitted r-multiples,
        # spy_return_pct=(550-500)/500*100=+10.00%.
        assert "Win rate: +50.00%" in output
        assert "Expectancy: $0.00" in output
        assert "Profit factor: 1.000" in output
        assert "Avg R-multiple: 0.000" in output
        assert "Avg MAE: $-85.00" in output
        assert "Avg MFE: $85.00" in output
        assert "可能性" in output
        assert "r_multiple_omitted" not in output.lower()
        assert "SPY buy-and-hold: +10.00%" in output
        assert "By exit reason" in output
        assert "target" in output
        assert "stop_loss" in output
        assert "By strategy" in output
        assert "trend_follow" in output
        assert "mean_revert" in output


class TestIncomplete:
    """Issue #129: surface runs whose analysis phase never finished."""

    def test_actionable_gap_is_listed_and_exits_with_the_agreed_code(
        self,
        state_store: StateStore,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_id = _write_run_archive(tmp_path, date(2026, 8, 10), has_result=False)
        _insert_run_row(state_store, run_id, date(2026, 8, 10))

        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "incomplete",
                    "--reports-dir",
                    str(tmp_path / "reports"),
                    "--db",
                    _db_path(state_store),
                ]
            )

        assert exc_info.value.code == ANALYSIS_INCOMPLETE_EXIT_CODE
        output = capsys.readouterr().out
        assert str(run_id) in output
        assert "分析未完" in output

    def test_all_runs_finished_prints_the_clear_message_and_exits_zero(
        self,
        state_store: StateStore,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_id = _write_run_archive(tmp_path, date(2026, 8, 11), has_result=True)
        _insert_run_row(state_store, run_id, date(2026, 8, 11))

        main(
            [
                "incomplete",
                "--reports-dir",
                str(tmp_path / "reports"),
                "--db",
                _db_path(state_store),
            ]
        )

        assert "分析フェーズ未完のrunはありません" in capsys.readouterr().out

    def test_same_day_duplicate_is_listed_without_raising_the_exit_code(
        self,
        state_store: StateStore,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        finished = _write_run_archive(tmp_path, date(2026, 8, 3), has_result=True)
        leftover = _write_run_archive(tmp_path, date(2026, 8, 3), has_result=False)
        _insert_run_row(state_store, finished, date(2026, 8, 3))
        _insert_run_row(state_store, leftover, date(2026, 8, 3))

        main(
            [
                "incomplete",
                "--reports-dir",
                str(tmp_path / "reports"),
                "--db",
                _db_path(state_store),
            ]
        )

        output = capsys.readouterr().out
        assert str(leftover) in output
        assert "同日重複" in output
        assert "対処が必要な未完runはありません" in output

    def test_since_narrows_the_window_to_the_dates_that_still_matter(
        self,
        state_store: StateStore,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        stale = _write_run_archive(tmp_path, date(2026, 8, 3), has_result=False)
        _insert_run_row(state_store, stale, date(2026, 8, 3))

        main(
            [
                "incomplete",
                "--since",
                "2026-08-10",
                "--reports-dir",
                str(tmp_path / "reports"),
                "--db",
                _db_path(state_store),
            ]
        )

        assert "分析フェーズ未完のrunはありません" in capsys.readouterr().out


class TestReadOnly:
    """REQ-007: no `copilot-history` subcommand may mutate any table."""

    def _assert_no_mutation(self, state_store: StateStore, argv: list[str]) -> None:
        before = _snapshot(state_store._database)  # noqa: SLF001
        main([*argv, "--db", _db_path(state_store)])
        after = _snapshot(state_store._database)  # noqa: SLF001
        assert before == after

    def test_runs_does_not_mutate_any_table(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate(state_store)
        self._assert_no_mutation(state_store, ["runs"])
        capsys.readouterr()  # drain, keep output out of the test log

    def test_run_detail_does_not_mutate_any_table(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_id = _populate(state_store)
        self._assert_no_mutation(state_store, ["run", "--run-id", str(run_id)])
        capsys.readouterr()

    def test_symbol_does_not_mutate_any_table(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate(state_store)
        self._assert_no_mutation(state_store, ["symbol", "AAPL"])
        capsys.readouterr()

    def test_rejections_does_not_mutate_any_table(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_id = _populate(state_store)
        self._assert_no_mutation(state_store, ["rejections", "--run-id", str(run_id)])
        capsys.readouterr()

    def test_performance_does_not_mutate_any_table(
        self, state_store: StateStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _populate(state_store)
        self._assert_no_mutation(state_store, ["performance"])
        capsys.readouterr()

    def test_incomplete_does_not_mutate_any_table(
        self,
        state_store: StateStore,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Exercised through the exit-3 path, where the scan does the most
        # work, to prove even that branch stays `SELECT`-only (REQ-007).
        run_id = _write_run_archive(tmp_path, date(2026, 8, 10), has_result=False)
        _insert_run_row(state_store, run_id, date(2026, 8, 10))
        before = _snapshot(state_store._database)  # noqa: SLF001

        with pytest.raises(SystemExit):
            main(
                [
                    "incomplete",
                    "--reports-dir",
                    str(tmp_path / "reports"),
                    "--db",
                    _db_path(state_store),
                ]
            )

        assert _snapshot(state_store._database) == before  # noqa: SLF001
        capsys.readouterr()
