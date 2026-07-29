"""P8-30: `copilot-retro collect` -- reports/ scan into `verdicts`/`verdict_sources`.

The scan is fail-soft by design (E30.2/E30.4): one unusable run directory,
or one unresolvable `source_id` inside an otherwise valid one, must not stop
the batch. Zero scanned runs is a normal success, not an error -- no
`analysis_result.json` exists in this repo's `reports/` yet (research R0).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

from swing_copilot.retro.collect import collect_verdicts
from tests.analysis.conftest import (
    AS_OF,
    CALENDAR_ID,
    FILING_ID,
    NEWS_ID,
    RUN_ID,
    input_payload,
    result_payload,
    symbol_payload,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from swing_copilot.storage.state_store import StateStore


def _rows(
    state_store: StateStore, sql: str, parameters: list[object] | None = None
) -> list[tuple[object, ...]]:
    with state_store._database.connect() as conn:  # noqa: SLF001
        return conn.execute(sql, parameters or []).fetchall()


class TestCollectHappyPath:
    def test_persists_the_verdict_with_code_owned_run_metadata(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        write_run()

        summary = collect_verdicts(state_store, reports_root)

        assert (summary.scanned_run_count, summary.collected_run_count) == (1, 1)
        assert summary.verdict_count == 1
        assert summary.notes == ()
        assert _rows(
            state_store,
            "SELECT run_id, symbol, as_of, strategy_key, recommendation, no_trade "
            "FROM verdicts",
        ) == [(UUID(RUN_ID), "AAPL", AS_OF, "default", "proceed", False)]

    def test_persists_every_cited_source_with_its_input_resolved_type(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        # A reason citing the run-wide calendar event: admitted for any symbol.
        symbol = symbol_payload(
            verdict={
                "recommendation": "proceed",
                "reasons": [
                    {"text": "マクロ指標は中立圏。", "source_ids": [CALENDAR_ID]}
                ],
            }
        )
        write_run(result=result_payload(symbols=[symbol]))

        summary = collect_verdicts(state_store, reports_root)

        assert summary.source_count == 3
        assert _rows(
            state_store,
            "SELECT source_id, source_type FROM verdict_sources ORDER BY source_id",
        ) == sorted(
            [
                (NEWS_ID, "news"),
                (FILING_ID, "filing"),
                (CALENDAR_ID, "calendar"),
            ]
        )

    def test_persists_the_verdict_reasons_verbatim(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        write_run()

        collect_verdicts(state_store, reports_root)

        rows = _rows(state_store, "SELECT reasons_json FROM verdicts")
        assert json.loads(str(rows[0][0])) == [
            {"text": "No contradicting disclosure.", "source_ids": [FILING_ID]}
        ]

    def test_persists_the_run_level_no_trade_flag(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        write_run(
            result=result_payload(
                no_trade=True, no_trade_reason="レジームがリスクオフのため。"
            )
        )

        collect_verdicts(state_store, reports_root)

        assert _rows(state_store, "SELECT no_trade FROM verdicts") == [(True,)]

    def test_persists_a_skip_recommendation(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        symbol = symbol_payload(verdict={"recommendation": "skip", "reasons": []})
        write_run(result=result_payload(symbols=[symbol]))

        collect_verdicts(state_store, reports_root)

        assert _rows(state_store, "SELECT recommendation FROM verdicts") == [("skip",)]


class TestCollectIdempotence:
    def test_reingesting_a_corrected_result_updates_without_duplicating(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        write_run()
        collect_verdicts(state_store, reports_root)

        corrected = symbol_payload(
            verdict={"recommendation": "skip", "reasons": []},
        )
        write_run(result=result_payload(symbols=[corrected]))
        collect_verdicts(state_store, reports_root)

        assert _rows(state_store, "SELECT symbol, recommendation FROM verdicts") == [
            ("AAPL", "skip")
        ]

    def test_rerunning_an_unchanged_scan_leaves_one_row_per_symbol(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        write_run()

        collect_verdicts(state_store, reports_root)
        collect_verdicts(state_store, reports_root)

        assert _rows(state_store, "SELECT count(*) FROM verdicts") == [(1,)]
        assert _rows(state_store, "SELECT count(*) FROM verdict_sources") == [(2,)]


class TestCollectZeroScan:
    def test_an_empty_reports_root_is_a_normal_success(
        self, state_store: StateStore, reports_root: Path
    ) -> None:
        summary = collect_verdicts(state_store, reports_root)

        assert (summary.scanned_run_count, summary.collected_run_count) == (0, 0)
        assert summary.notes == ()

    def test_a_missing_reports_root_is_a_normal_success(
        self, state_store: StateStore, tmp_path: Path
    ) -> None:
        summary = collect_verdicts(state_store, tmp_path / "absent")

        assert summary.scanned_run_count == 0

    def test_non_run_entries_are_ignored_without_a_note(
        self, state_store: StateStore, reports_root: Path
    ) -> None:
        # `pipeline/daily.py` also archives per-run Markdown next to the
        # date directories; neither it nor a non-date directory is a run.
        (reports_root / "2027-03-01.md").write_text("archive", encoding="utf-8")
        (reports_root / "not-a-date").mkdir()
        (reports_root / "2027-03-02").mkdir()
        (reports_root / "2027-03-02" / "not-a-uuid").mkdir()
        (reports_root / "2027-03-02" / "abc.md").write_text("x", encoding="utf-8")

        summary = collect_verdicts(state_store, reports_root)

        assert (summary.scanned_run_count, summary.notes) == (0, ())


class TestCollectFailSoft:
    def test_a_run_missing_its_analysis_input_is_skipped_with_a_note(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        write_run(analysis_input=None)

        summary = collect_verdicts(state_store, reports_root)

        assert (summary.scanned_run_count, summary.collected_run_count) == (1, 0)
        assert len(summary.notes) == 1
        assert "analysis_input.json" in summary.notes[0]
        assert _rows(state_store, "SELECT count(*) FROM verdicts") == [(0,)]

    def test_a_run_missing_its_analysis_result_is_skipped_with_a_note(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        write_run(result=None)

        summary = collect_verdicts(state_store, reports_root)

        assert summary.collected_run_count == 0
        assert "analysis_result.json" in summary.notes[0]

    def test_an_unparsable_document_is_skipped_with_a_note(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        write_run(result="{not json")

        summary = collect_verdicts(state_store, reports_root)

        assert summary.collected_run_count == 0
        assert len(summary.notes) == 1
        assert _rows(state_store, "SELECT count(*) FROM verdicts") == [(0,)]

    def test_a_result_whose_run_id_disagrees_with_its_directory_is_skipped(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        foreign = "00000000-0000-4000-8000-000000000001"
        write_run(run_id=foreign)

        summary = collect_verdicts(state_store, reports_root)

        assert summary.collected_run_count == 0
        assert "run_id" in summary.notes[0]
        assert _rows(state_store, "SELECT count(*) FROM verdicts") == [(0,)]

    def test_an_unresolvable_source_id_is_dropped_but_the_verdict_survives(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        symbol = symbol_payload(
            news_summary={
                "facts": [{"text": "出所不明の記述。", "source_ids": ["ghost-1"]}],
                "interpretation": [],
                "risk_flags": [],
            }
        )
        write_run(result=result_payload(symbols=[symbol]))

        summary = collect_verdicts(state_store, reports_root)

        assert summary.collected_run_count == 1
        assert len(summary.notes) == 1
        assert "ghost-1" in summary.notes[0]
        assert _rows(state_store, "SELECT count(*) FROM verdicts") == [(1,)]
        assert _rows(
            state_store, "SELECT source_id FROM verdict_sources ORDER BY source_id"
        ) == [(FILING_ID,)]

    def test_one_broken_run_does_not_stop_a_healthy_sibling(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        healthy_run_id = "00000000-0000-4000-8000-00000000000a"
        write_run(result="{not json")
        write_run(
            analysis_input=input_payload(run_id=healthy_run_id),
            result=result_payload(run_id=healthy_run_id),
            run_id=healthy_run_id,
        )

        summary = collect_verdicts(state_store, reports_root)

        assert (summary.scanned_run_count, summary.collected_run_count) == (2, 1)
        assert _rows(state_store, "SELECT run_id FROM verdicts") == [
            (UUID(healthy_run_id),)
        ]
