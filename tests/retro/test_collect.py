"""P8-30: `copilot-retro collect` -- reports/ scan into `verdicts`/`verdict_sources`.

The scan is fail-soft by design (E30.2/E30.4): one unusable run directory,
or one unresolvable `source_id` inside an otherwise valid one, must not stop
the batch. Zero scanned runs is a normal success, not an error -- no
`analysis_result.json` exists in this repo's `reports/` yet (research R0).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from swing_copilot.analysis.schemas import canonical_json_digest
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


def _input_declaring_exhibit_truncation(
    exhibit_truncated: bool | None,
) -> dict[str, Any]:
    """An `analysis_input.json` payload stating -- or omitting -- the field.

    `None` drops the key entirely, which is the shape of every archive written
    before Issue #157 added it.
    """
    payload = input_payload()
    coverage = payload["candidates"][0]["filings"][0]["coverage"]
    if exhibit_truncated is None:
        del coverage["exhibit_truncated"]
    else:
        coverage["exhibit_truncated"] = exhibit_truncated
    payload["input_digest"] = canonical_json_digest(
        payload, excluded_field="input_digest"
    )
    return payload


def _insert_run(
    state_store: StateStore, run_id: str, run_date: date, started_at: datetime
) -> None:
    """Insert a minimal `runs` row so `get_run_started_at` can resolve it."""
    with state_store._database.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
            "started_at) VALUES (?, ?, 'live', 'cfg', 'success', ?)",
            [run_id, run_date, started_at],
        )


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
        assert summary.coverage_count == 1
        assert summary.notes == ()
        assert _rows(
            state_store,
            "SELECT run_id, symbol, as_of, strategy_key, recommendation, no_trade "
            "FROM verdicts",
        ) == [(UUID(RUN_ID), "AAPL", AS_OF, "default", "proceed", False)]
        coverage = state_store.get_analysis_source_coverages(UUID(RUN_ID), "AAPL")
        assert len(coverage) == 1
        assert coverage[0].source_id == FILING_ID
        assert coverage[0].selection_mode == "full"
        assert coverage[0].exhibit_truncated is False

    def test_persists_a_collection_stage_exhibit_cut_the_archive_declared(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        write_run(_input_declaring_exhibit_truncation(True))

        collect_verdicts(state_store, reports_root)

        coverage = state_store.get_analysis_source_coverages(UUID(RUN_ID), "AAPL")
        assert coverage[0].exhibit_truncated is True

    def test_an_archive_predating_the_field_is_stored_as_not_recorded(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        # Issue #157: `FilingCoverage.exhibit_truncated` defaults to `False`,
        # so storing the parsed value would turn "the document never said"
        # into "the document said no exhibit was cut", and the retrospective
        # would then count that run's input as known to be complete.
        write_run(_input_declaring_exhibit_truncation(None))

        collect_verdicts(state_store, reports_root)

        coverage = state_store.get_analysis_source_coverages(UUID(RUN_ID), "AAPL")
        assert coverage[0].exhibit_truncated is None

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


class TestSameDayDeduplication:
    """P8-119: multiple archived run directories sharing one `run_date`."""

    def test_two_collectable_runs_adopt_the_later_started_at(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        older = "00000000-0000-4000-8000-0000000000a1"
        newer = "00000000-0000-4000-8000-0000000000a2"
        write_run(
            analysis_input=input_payload(run_id=older),
            result=result_payload(run_id=older),
            run_id=older,
        )
        write_run(
            analysis_input=input_payload(run_id=newer),
            result=result_payload(run_id=newer),
            run_id=newer,
        )
        _insert_run(state_store, older, AS_OF, datetime(2027, 3, 1, 15, 6, tzinfo=UTC))
        _insert_run(state_store, newer, AS_OF, datetime(2027, 3, 1, 16, 52, tzinfo=UTC))

        summary = collect_verdicts(state_store, reports_root)

        assert (summary.scanned_run_count, summary.collected_run_count) == (2, 1)
        assert _rows(state_store, "SELECT run_id FROM verdicts") == [(UUID(newer),)]

    def test_notes_name_the_skipped_and_adopted_run_ids(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        older = "00000000-0000-4000-8000-0000000000b1"
        newer = "00000000-0000-4000-8000-0000000000b2"
        write_run(
            analysis_input=input_payload(run_id=older),
            result=result_payload(run_id=older),
            run_id=older,
        )
        write_run(
            analysis_input=input_payload(run_id=newer),
            result=result_payload(run_id=newer),
            run_id=newer,
        )
        _insert_run(state_store, older, AS_OF, datetime(2027, 3, 1, 15, 6, tzinfo=UTC))
        _insert_run(state_store, newer, AS_OF, datetime(2027, 3, 1, 16, 52, tzinfo=UTC))

        summary = collect_verdicts(state_store, reports_root)

        assert len(summary.notes) == 1
        note = summary.notes[0]
        assert AS_OF.isoformat() in note
        assert older in note
        assert newer in note

    def test_a_broken_newer_directory_falls_back_to_the_older_collectable_one(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        """Example 2: a failed later rerun must not hide the earlier good run."""
        older = "00000000-0000-4000-8000-0000000000c1"
        newer = "00000000-0000-4000-8000-0000000000c2"
        write_run(
            analysis_input=input_payload(run_id=older),
            result=result_payload(run_id=older),
            run_id=older,
        )
        write_run(result=None, run_id=newer)  # analysis_result.json missing
        _insert_run(state_store, older, AS_OF, datetime(2027, 3, 1, 10, 0, tzinfo=UTC))
        _insert_run(state_store, newer, AS_OF, datetime(2027, 3, 1, 18, 30, tzinfo=UTC))

        summary = collect_verdicts(state_store, reports_root)

        assert summary.collected_run_count == 1
        assert _rows(state_store, "SELECT run_id FROM verdicts") == [(UUID(older),)]

    def test_both_uncollectable_same_day_directories_collect_neither(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        first = "00000000-0000-4000-8000-0000000000a9"
        second = "00000000-0000-4000-8000-0000000000aa"
        write_run(result=None, run_id=first)
        write_run(result="{not json", run_id=second)

        summary = collect_verdicts(state_store, reports_root)

        assert summary.collected_run_count == 0
        assert len(summary.notes) == 2
        assert _rows(state_store, "SELECT count(*) FROM verdicts") == [(0,)]

    def test_a_single_candidate_with_no_runs_row_is_collected_without_a_note(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        # REQ-007: no `runs` row for RUN_ID at all, so get_run_started_at
        # resolves to None -- but with only one collectable directory that
        # day there is nothing to dedupe against, so behavior (and notes)
        # must match what the pre-existing suite already asserts (`notes ==
        # ()` in TestCollectHappyPath): resolving started_at is skipped
        # entirely rather than producing a note no prior run ever emitted.
        write_run()

        summary = collect_verdicts(state_store, reports_root)

        assert summary.collected_run_count == 1
        assert summary.notes == ()

    def test_a_sibling_with_no_runs_row_loses_to_a_resolvable_one_with_a_note(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        # REQ-005, in the context dedup actually runs: an unresolvable
        # started_at only produces a note once there is a same-day sibling
        # to compare against, and never wins that comparison.
        resolvable = "00000000-0000-4000-8000-0000000000d3"
        unresolvable = "00000000-0000-4000-8000-0000000000d4"
        write_run(
            analysis_input=input_payload(run_id=resolvable),
            result=result_payload(run_id=resolvable),
            run_id=resolvable,
        )
        write_run(
            analysis_input=input_payload(run_id=unresolvable),
            result=result_payload(run_id=unresolvable),
            run_id=unresolvable,
        )
        _insert_run(
            state_store, resolvable, AS_OF, datetime(2027, 3, 1, 15, 6, tzinfo=UTC)
        )
        # No `runs` row inserted for `unresolvable`.

        summary = collect_verdicts(state_store, reports_root)

        assert _rows(state_store, "SELECT run_id FROM verdicts") == [
            (UUID(resolvable),)
        ]
        assert any("started_at" in note for note in summary.notes)
        assert any(unresolvable in note for note in summary.notes)

    def test_the_skipped_runs_previously_written_verdicts_are_left_untouched(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        older = "00000000-0000-4000-8000-0000000000d1"
        newer = "00000000-0000-4000-8000-0000000000d2"
        write_run(
            analysis_input=input_payload(run_id=older),
            result=result_payload(run_id=older),
            run_id=older,
        )
        _insert_run(state_store, older, AS_OF, datetime(2027, 3, 1, 15, 6, tzinfo=UTC))
        collect_verdicts(state_store, reports_root)
        assert _rows(state_store, "SELECT run_id FROM verdicts") == [(UUID(older),)]

        write_run(
            analysis_input=input_payload(run_id=newer),
            result=result_payload(run_id=newer),
            run_id=newer,
        )
        _insert_run(state_store, newer, AS_OF, datetime(2027, 3, 1, 16, 52, tzinfo=UTC))

        collect_verdicts(state_store, reports_root)

        assert _rows(state_store, "SELECT run_id FROM verdicts ORDER BY run_id") == [
            (UUID(older),),
            (UUID(newer),),
        ]

    def test_equal_started_at_ties_break_on_the_greater_run_id_string(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        low = "00000000-0000-4000-8000-0000000000e1"
        high = "00000000-0000-4000-8000-0000000000e2"
        write_run(
            analysis_input=input_payload(run_id=low),
            result=result_payload(run_id=low),
            run_id=low,
        )
        write_run(
            analysis_input=input_payload(run_id=high),
            result=result_payload(run_id=high),
            run_id=high,
        )
        same_time = datetime(2027, 3, 1, 15, 0, tzinfo=UTC)
        _insert_run(state_store, low, AS_OF, same_time)
        _insert_run(state_store, high, AS_OF, same_time)

        collect_verdicts(state_store, reports_root)

        assert _rows(state_store, "SELECT run_id FROM verdicts") == [(UUID(high),)]

    def test_three_or_more_same_day_directories_adopt_exactly_one(
        self,
        state_store: StateStore,
        reports_root: Path,
        write_run: Callable[..., Path],
    ) -> None:
        run_ids = [
            "00000000-0000-4000-8000-0000000000f1",
            "00000000-0000-4000-8000-0000000000f2",
            "00000000-0000-4000-8000-0000000000f3",
        ]
        for offset, run_id in enumerate(run_ids):
            write_run(
                analysis_input=input_payload(run_id=run_id),
                result=result_payload(run_id=run_id),
                run_id=run_id,
            )
            _insert_run(
                state_store, run_id, AS_OF, datetime(2027, 3, 1, 15, offset, tzinfo=UTC)
            )

        summary = collect_verdicts(state_store, reports_root)

        assert summary.collected_run_count == 1
        assert _rows(state_store, "SELECT run_id FROM verdicts") == [
            (UUID(run_ids[-1]),)
        ]
