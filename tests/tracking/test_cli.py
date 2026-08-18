"""`copilot-track` CLI surface: update / list / show / stats / close / note."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from swing_copilot.clock import SystemClock
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.storage.tracking_records import VerdictPosition
from swing_copilot.tracking import cli as cli_module
from swing_copilot.tracking.cli import main
from tests.tracking.conftest import (
    DAY_1,
    ENTRY_DATE,
    RUN_ID,
    SYMBOL,
    bar,
    flat_prelude,
    seed_risk,
    seed_verdict,
    write_bars,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A database seeded with one tradeable `proceed` verdict and its bars."""
    path = tmp_path / "copilot.duckdb"
    state_store = StateStore(Database(path))
    state_store.init_schema()
    seed_verdict(state_store)
    seed_risk(state_store)
    market_store = MarketStore(Database(path), parquet_root=path.parent / "bars")
    write_bars(market_store, flat_prelude())
    write_bars(
        market_store,
        [bar(DAY_1, open_price=101.0, high=103.0, low=101.0, close=102.0)],
    )
    return path


def _store(db_path: Path) -> StateStore:
    return StateStore(Database(db_path))


class TestUpdateCommand:
    def test_it_opens_and_advances_the_ledger_up_to_the_given_as_of(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["update", "--as-of", DAY_1.isoformat(), "--db", str(db_path)])

        assert "新規 1 件" in capsys.readouterr().out
        position = _store(db_path).get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.last_marked_date == DAY_1

    def test_an_omitted_as_of_falls_back_to_the_system_clock(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(SystemClock, "today", lambda _self: ENTRY_DATE)

        main(["update", "--db", str(db_path)])

        position = _store(db_path).get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.last_marked_date == ENTRY_DATE

    def test_it_prints_the_data_quality_notes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "bare.duckdb"
        # An *existing but empty* bars root: "this symbol has no bar" stays
        # fail-soft, only a missing root is fatal (Issue #221).
        (tmp_path / "bars").mkdir()
        state_store = StateStore(Database(path))
        state_store.init_schema()
        seed_verdict(state_store)

        main(["update", "--as-of", ENTRY_DATE.isoformat(), "--db", str(path)])

        assert "エントリー価格を解決できない" in capsys.readouterr().out

    def test_an_unreadable_settings_file_exits_with_its_message(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        missing = tmp_path / "nope.yaml"

        with pytest.raises(SystemExit) as excinfo:
            main(["update", "--db", str(db_path), "--settings", str(missing)])

        assert str(missing) in str(excinfo.value)

    def test_a_db_without_its_sibling_bars_root_fails_before_touching_the_ledger(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A DuckDB copy whose `bars/` was left behind is fatal (Issue #221).

        Without the guard the run reads no price at all and reports
        "新規 0 件 / 更新 0 件 / 手仕舞い 0 件" as if the ledger were quiet.
        """
        copied = tmp_path / "copy"
        copied.mkdir()
        path = copied / "copilot.duckdb"
        state_store = StateStore(Database(path))
        state_store.init_schema()
        seed_verdict(state_store)

        with pytest.raises(SystemExit) as excinfo:
            main(["update", "--as-of", ENTRY_DATE.isoformat(), "--db", str(path)])

        message = str(excinfo.value)
        assert "Parquetディレクトリが見つかりません" in message
        assert str(copied / "bars") in message
        assert "新規" not in capsys.readouterr().out
        assert _store(path).get_verdict_position(RUN_ID, SYMBOL) is None


class TestListCommand:
    def test_an_empty_ledger_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["list", "--db", str(tmp_path / "empty.duckdb")])

        assert "追跡中の仮想ポジションはない" in capsys.readouterr().out

    def test_an_open_position_shows_its_stop_and_remaining_sessions(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["update", "--as-of", DAY_1.isoformat(), "--db", str(db_path)])
        capsys.readouterr()

        main(["list", "--db", str(db_path)])

        out = capsys.readouterr().out
        assert SYMBOL in out
        # entry 100.00, unrealized +2.00% on the 102.00 close, 1 of 25 sessions
        # held, so 24 remain.
        assert "+2.00%" in out
        assert "1/25" in out
        assert "24" in out

    def test_open_rows_precede_closed_ones_with_ties_broken_by_symbol(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state_store = _store(db_path)
        for symbol, status in (("BBB", "open"), ("AAA", "open"), ("CCC", "closed")):
            state_store.upsert_verdict_position(
                VerdictPosition(
                    run_id=RUN_ID,
                    symbol=symbol,
                    strategy_key="default",
                    recommendation="proceed",
                    no_trade=False,
                    entry_date=ENTRY_DATE,
                    entry_price=100.0,
                    stop_price=95.0,
                    days_held=0,
                    status=status,
                    exit_date=DAY_1 if status == "closed" else None,
                    exit_reason="manual" if status == "closed" else None,
                    last_marked_date=ENTRY_DATE,
                )
            )

        main(["list", "--db", str(db_path)])

        out = capsys.readouterr().out
        assert out.index("AAA") < out.index("BBB") < out.index("CCC")

    def test_a_no_trade_position_is_flagged_in_the_ledger(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # CASH_PRIORITY (or any regime the run itself calls no_trade) must
        # still show up in `list`, but visibly marked apart from an ordinary
        # proceed that was actually on offer as a buy.
        path = tmp_path / "no_trade.duckdb"
        state_store = StateStore(Database(path))
        state_store.init_schema()
        seed_verdict(state_store, symbol="NTR", no_trade=True)
        seed_risk(state_store, symbol="NTR")
        market_store = MarketStore(Database(path), parquet_root=path.parent / "bars")
        write_bars(market_store, flat_prelude(symbol="NTR"))
        main(["update", "--as-of", ENTRY_DATE.isoformat(), "--db", str(path)])
        capsys.readouterr()

        main(["list", "--db", str(path)])

        out = capsys.readouterr().out
        assert "NTR" in out
        assert "no_trade" in out

    def test_an_ordinary_position_shows_no_no_trade_flag(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["update", "--as-of", DAY_1.isoformat(), "--db", str(db_path)])
        capsys.readouterr()

        main(["list", "--db", str(db_path)])

        out = capsys.readouterr().out
        assert "no_trade" not in out

    def test_status_open_hides_a_closed_position(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["update", "--as-of", DAY_1.isoformat(), "--db", str(db_path)])
        main(
            [
                "close",
                "--run-id",
                str(RUN_ID),
                "--symbol",
                SYMBOL,
                "--as-of",
                DAY_1.isoformat(),
                "--db",
                str(db_path),
            ]
        )
        capsys.readouterr()

        main(["list", "--status", "open", "--db", str(db_path)])

        assert "追跡中の仮想ポジションはない" in capsys.readouterr().out


class TestShowCommand:
    def test_it_prints_the_verdict_reasons_marks_and_notes(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["update", "--as-of", DAY_1.isoformat(), "--db", str(db_path)])
        main(
            [
                "note",
                "--run-id",
                str(RUN_ID),
                "--symbol",
                SYMBOL,
                "--text",
                "想定内の推移",
                "--date",
                DAY_1.isoformat(),
                "--db",
                str(db_path),
            ]
        )
        capsys.readouterr()

        main(
            ["show", "--symbol", SYMBOL, "--run-id", str(RUN_ID), "--db", str(db_path)]
        )

        out = capsys.readouterr().out
        assert "押し目が浅い" in out
        assert DAY_1.isoformat() in out
        assert "想定内の推移" in out

    def test_a_position_whose_verdict_row_is_gone_still_renders(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["update", "--as-of", DAY_1.isoformat(), "--db", str(db_path)])
        with Database(db_path).connect() as conn:
            conn.execute("DELETE FROM verdicts")
        capsys.readouterr()

        main(["show", "--symbol", SYMBOL, "--db", str(db_path)])

        out = capsys.readouterr().out
        assert SYMBOL in out
        assert "verdict:" not in out

    def test_a_no_trade_position_shows_the_run_level_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "no_trade.duckdb"
        state_store = StateStore(Database(path))
        state_store.init_schema()
        seed_verdict(state_store, symbol="NTR", no_trade=True)
        seed_risk(state_store, symbol="NTR")
        market_store = MarketStore(Database(path), parquet_root=path.parent / "bars")
        write_bars(market_store, flat_prelude(symbol="NTR"))
        main(["update", "--as-of", ENTRY_DATE.isoformat(), "--db", str(path)])
        capsys.readouterr()

        main(["show", "--symbol", "NTR", "--db", str(path)])

        out = capsys.readouterr().out
        assert "no_trade run" in out

    def test_an_unknown_symbol_says_it_is_not_tracked(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["show", "--symbol", "ZZZ", "--db", str(db_path)])

        assert "ZZZ の追跡ポジションはない" in capsys.readouterr().out

    def test_a_closed_position_shows_its_realized_result(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["update", "--as-of", DAY_1.isoformat(), "--db", str(db_path)])
        main(
            [
                "close",
                "--run-id",
                str(RUN_ID),
                "--symbol",
                SYMBOL,
                "--as-of",
                DAY_1.isoformat(),
                "--db",
                str(db_path),
            ]
        )
        capsys.readouterr()

        main(["show", "--symbol", SYMBOL, "--db", str(db_path)])

        out = capsys.readouterr().out
        assert "手仕舞い" in out
        assert "+2.00%" in out


class TestCloseCommand:
    def test_it_records_a_manual_exit_with_the_note(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["update", "--as-of", DAY_1.isoformat(), "--db", str(db_path)])
        capsys.readouterr()

        main(
            [
                "close",
                "--run-id",
                str(RUN_ID),
                "--symbol",
                SYMBOL,
                "--as-of",
                DAY_1.isoformat(),
                "--note",
                "決算をまたぎたくない",
                "--db",
                str(db_path),
            ]
        )

        assert "手仕舞い" in capsys.readouterr().out
        state_store = _store(db_path)
        position = state_store.get_verdict_position(RUN_ID, SYMBOL)
        assert position is not None
        assert position.exit_reason == "manual"
        assert [
            note.note for note in state_store.get_verdict_position_notes(RUN_ID, SYMBOL)
        ] == ["決算をまたぎたくない"]

    def test_closing_an_untracked_position_exits_with_the_reason(
        self, db_path: Path
    ) -> None:
        with pytest.raises(SystemExit, match="存在しない"):
            main(
                [
                    "close",
                    "--run-id",
                    str(RUN_ID),
                    "--symbol",
                    SYMBOL,
                    "--as-of",
                    DAY_1.isoformat(),
                    "--db",
                    str(db_path),
                ]
            )


class TestNoteCommand:
    def test_an_omitted_date_falls_back_to_the_system_clock(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main(["update", "--as-of", DAY_1.isoformat(), "--db", str(db_path)])
        monkeypatch.setattr(SystemClock, "today", lambda _self: date(2027, 4, 1))

        main(
            [
                "note",
                "--run-id",
                str(RUN_ID),
                "--symbol",
                SYMBOL,
                "--text",
                "上昇一服",
                "--db",
                str(db_path),
            ]
        )

        notes = _store(db_path).get_verdict_position_notes(RUN_ID, SYMBOL)
        assert [note.note_date for note in notes] == [date(2027, 4, 1)]

    def test_a_note_on_an_untracked_position_exits_with_the_reason(
        self, db_path: Path
    ) -> None:
        with pytest.raises(SystemExit, match="存在しない"):
            main(
                [
                    "note",
                    "--run-id",
                    str(RUN_ID),
                    "--symbol",
                    SYMBOL,
                    "--text",
                    "所感",
                    "--db",
                    str(db_path),
                ]
            )


class TestRecommendationDisplayFilter:
    """Issue #190: skip shadows are tracked, but never in the default view."""

    @pytest.fixture
    def both_sides_db(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
        path = tmp_path / "both.duckdb"
        state_store = StateStore(Database(path))
        state_store.init_schema()
        # Distinct runs: `replace_run_verdicts` replaces a run wholesale, so
        # seeding both symbols under one run_id would keep only the second.
        skip_run_id = uuid4()
        seed_verdict(state_store, symbol="PRO")
        seed_risk(state_store, symbol="PRO")
        seed_verdict(
            state_store, run_id=skip_run_id, symbol="SKP", recommendation="skip"
        )
        seed_risk(state_store, run_id=skip_run_id, symbol="SKP")
        market_store = MarketStore(Database(path), parquet_root=path.parent / "bars")
        write_bars(market_store, flat_prelude(symbol="PRO"))
        write_bars(market_store, flat_prelude(symbol="SKP"))
        main(["update", "--as-of", ENTRY_DATE.isoformat(), "--db", str(path)])
        capsys.readouterr()
        return path

    def test_list_shows_only_the_proceed_side_by_default(
        self, both_sides_db: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["list", "--db", str(both_sides_db)])

        out = capsys.readouterr().out
        assert "PRO" in out
        assert "SKP" not in out

    def test_list_shows_the_skip_side_only_when_asked(
        self, both_sides_db: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["list", "--recommendation", "all", "--db", str(both_sides_db)])

        out = capsys.readouterr().out
        assert "PRO" in out
        assert "SKP" in out

    def test_show_hides_a_skip_position_by_default(
        self, both_sides_db: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["show", "--symbol", "SKP", "--db", str(both_sides_db)])

        assert "追跡ポジションはない" in capsys.readouterr().out

    def test_show_prints_a_skip_position_when_asked(
        self, both_sides_db: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(
            [
                "show",
                "--symbol",
                "SKP",
                "--recommendation",
                "skip",
                "--db",
                str(both_sides_db),
            ]
        )

        assert "SKP" in capsys.readouterr().out


class TestStatsCommand:
    def test_it_reports_every_stratum_by_default(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["update", "--as-of", DAY_1.isoformat(), "--db", str(db_path)])
        capsys.readouterr()

        main(["stats", "--db", str(db_path)])

        out = capsys.readouterr().out
        assert "proceed" in out
        assert "skip" in out
        assert "all" in out
        # The one tracked position is still open, so nothing is rated yet.
        assert "勝率" in out

    def test_it_reports_the_realized_record_of_a_closed_position(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["update", "--as-of", DAY_1.isoformat(), "--db", str(db_path)])
        main(
            [
                "close",
                "--run-id",
                str(RUN_ID),
                "--symbol",
                SYMBOL,
                "--as-of",
                DAY_1.isoformat(),
                "--db",
                str(db_path),
            ]
        )
        capsys.readouterr()

        main(["stats", "--recommendation", "proceed", "--db", str(db_path)])

        out = capsys.readouterr().out
        # Entry 100.00 -> manual close at the 102.00 mark: one win, +2.00%.
        assert "+100.00%" in out
        assert "+2.00%" in out
        assert "manual=1" in out

    def test_a_single_stratum_can_be_selected(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["stats", "--recommendation", "skip", "--db", str(db_path)])

        out = capsys.readouterr().out
        assert "skip" in out
        assert "proceed" not in out

    def test_it_labels_the_skip_side_as_a_counterfactual_not_a_suggestion(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["stats", "--db", str(db_path)])

        assert "実際に提案された建玉ではない" in capsys.readouterr().out


def test_the_console_script_entry_point_is_this_module_main() -> None:
    assert cli_module.main is main
