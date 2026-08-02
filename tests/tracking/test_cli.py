"""`copilot-track` CLI surface: update / list / show / close / note."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from swing_copilot.clock import SystemClock
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore
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
        # entry 100.00, unrealized +2.00% on the 102.00 close, 1 of 60 sessions
        # held, so 59 remain.
        assert "+2.00%" in out
        assert "1/60" in out
        assert "59" in out

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


def test_the_console_script_entry_point_is_this_module_main() -> None:
    assert cli_module.main is main
