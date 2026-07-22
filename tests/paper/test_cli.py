"""Decision-recording CLI behavior backed by the existing paper journal."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from swing_copilot.models import RunMode, RunStatus
from swing_copilot.paper.cli import (
    DecisionCommand,
    DecisionCommandError,
    record_decision_command,
)
from swing_copilot.report.markdown_report import DECISIONS_END, DECISIONS_START
from swing_copilot.screening.base import Candidate

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.storage.state_store import StateStore

RUN_ID = UUID("11111111-2222-3333-4444-555555555555")
NEWER_RUN_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _candidate() -> Candidate:
    return Candidate("AAPL", date(2026, 7, 22), ("trend_sma",), {"close": 100.0}, 1)


def test_records_decision_and_refreshes_generated_markdown(
    state_store: StateStore, tmp_path: Path
) -> None:
    with state_store._database.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (run_id, run_date, mode, config_hash, status, started_at) "
            "VALUES (?, ?, 'live', 'cfg', 'running', now())",
            [str(RUN_ID), date(2026, 7, 22)],
        )
    state_store.record_candidates([_candidate()], RUN_ID, "default")
    report_path = tmp_path / "brief.md"
    report_path.write_text(
        f"# Brief\n\n{DECISIONS_START}\n_No decisions recorded._\n{DECISIONS_END}\n",
        encoding="utf-8",
    )
    state_store.complete_run(RUN_ID, RunStatus.SUCCESS, report_path=report_path)

    recorded = record_decision_command(
        state_store,
        DecisionCommand(
            run_id=RUN_ID,
            symbol="aapl",
            decision="ignored",
            reason="相関リスクが高いため",
        ),
    )

    assert recorded.strategy_key == "default"
    with state_store._database.connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT decision, reason_memo FROM trades_journal WHERE run_id = ?",
            [str(RUN_ID)],
        ).fetchone()
    assert row == ("ignored", "相関リスクが高いため")
    markdown = report_path.read_text(encoding="utf-8")
    assert "AAPL" in markdown
    assert "ignored" in markdown
    assert "相関リスクが高いため" in markdown


def test_rejects_symbol_that_was_not_a_candidate(state_store: StateStore) -> None:
    run_id = state_store.start_run(date(2026, 7, 22), RunMode.LIVE, "cfg")
    state_store.record_candidates([_candidate()], run_id, "default")

    with pytest.raises(DecisionCommandError, match="candidate"):
        record_decision_command(
            state_store,
            DecisionCommand(run_id=run_id, symbol="MSFT", decision="ignored"),
        )


def test_recording_an_older_run_does_not_rewrite_latest_markdown(
    state_store: StateStore, tmp_path: Path
) -> None:
    older_report = tmp_path / "2026-07-21" / f"{RUN_ID}.md"
    older_report.parent.mkdir()
    older_report.write_text(
        f"# Older\n\n{DECISIONS_START}\n_No decisions recorded._\n{DECISIONS_END}\n",
        encoding="utf-8",
    )
    latest_report = tmp_path / "latest.md"
    latest_content = "# Newer run\n"
    latest_report.write_text(latest_content, encoding="utf-8")

    with state_store._database.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
            "started_at, completed_at, report_path) "
            "VALUES (?, ?, 'live', 'cfg', 'success', now(), now() - INTERVAL 1 SECOND, ?)",
            [str(RUN_ID), date(2026, 7, 21), str(older_report)],
        )
        conn.execute(
            "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
            "started_at, completed_at, report_path) "
            "VALUES (?, ?, 'live', 'cfg', 'success', now(), now(), ?)",
            [
                str(NEWER_RUN_ID),
                date(2026, 7, 22),
                str(tmp_path / "2026-07-22" / f"{NEWER_RUN_ID}.md"),
            ],
        )
    state_store.record_candidates([_candidate()], RUN_ID, "default")

    record_decision_command(
        state_store,
        DecisionCommand(run_id=RUN_ID, symbol="AAPL", decision="ignored"),
    )

    assert "ignored" in older_report.read_text(encoding="utf-8")
    assert latest_report.read_text(encoding="utf-8") == latest_content
