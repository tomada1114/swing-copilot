"""P8-30: `verdicts` / `verdict_sources` / `verdict_outcomes` write contracts.

Both writers are full replacements in a single transaction (design.md §4):
a natural-key rerun must pick up corrections *and* drop rows that are absent
from the replacement, and a failure after at least one successful statement
must leave the previous state intact.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import duckdb
import pytest

from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.storage.verdict_records import (
    ACCOUNT_INDEPENDENT_EXPORT_SINCE,
    ACCOUNT_INDEPENDENT_VERDICT_CUTOFF,
    AnalysisSourceCoverageRecord,
    CollectedRunRecords,
    NewsSupplyRecord,
    PriorVerdictOutcome,
    VerdictOutcomeRecord,
    VerdictReasonRecord,
    VerdictRecord,
    VerdictSourceRecord,
    is_reason_text_visible,
    reason_text_visible_sql,
)
from swing_copilot.text.base import TextItem
from tests.support.runs import seed_run

if TYPE_CHECKING:
    from swing_copilot.storage.state_store import StateStore

AS_OF = date(2026, 7, 20)


def _verdict(
    run_id: UUID,
    symbol: str,
    recommendation: str = "proceed",
    *,
    as_of: date = AS_OF,
    reasons: tuple[VerdictReasonRecord, ...] = (),
) -> VerdictRecord:
    return VerdictRecord(
        run_id=run_id,
        symbol=symbol,
        as_of=as_of,
        strategy_key="default",
        recommendation=recommendation,
        reasons=reasons,
        no_trade=False,
    )


def _verdict_measured(
    run_id: UUID, symbol: str, *, level: str = "sparse"
) -> VerdictRecord:
    """A verdict carrying Issue #154's archived news-supply measurement."""
    return replace(_verdict(run_id, symbol), news_supply=_news_supply(level=level))


def _news_supply(*, level: str = "sparse") -> NewsSupplyRecord:
    return NewsSupplyRecord(
        collected_items=20,
        exported_items=12,
        symbol_mention_items=4,
        level=level,
    )


def _source(
    run_id: UUID, symbol: str, source_id: str, source_type: str = "news"
) -> VerdictSourceRecord:
    return VerdictSourceRecord(
        run_id=run_id, symbol=symbol, source_id=source_id, source_type=source_type
    )


def _outcome(
    run_id: UUID,
    symbol: str,
    horizon_days: int = 5,
    *,
    forward_return_pct: float = 1.5,
    classification: str = "HIT",
) -> VerdictOutcomeRecord:
    return VerdictOutcomeRecord(
        run_id=run_id,
        symbol=symbol,
        horizon_days=horizon_days,
        as_of=AS_OF,
        recommendation="proceed",
        forward_return_pct=forward_return_pct,
        classification=classification,
    )


def _coverage(
    run_id: UUID,
    symbol: str = "AAPL",
    source_id: str = "edgar:1",
    *,
    exported_chars: int = 120_000,
    exhibit_truncated: bool | None = None,
) -> AnalysisSourceCoverageRecord:
    return AnalysisSourceCoverageRecord(
        run_id=run_id,
        symbol=symbol,
        source_id=source_id,
        original_chars=180_000,
        exported_chars=exported_chars,
        is_truncated=True,
        selection_mode="section_priority",
        sections=(("part_i_item_2", "full"), ("part_ii_item_1a", "partial")),
        exhibit_truncated=exhibit_truncated,
    )


def _rows(
    state_store: StateStore, sql: str, parameters: list[object] | None = None
) -> list[tuple[object, ...]]:
    with state_store.database.connect() as conn:
        return conn.execute(sql, parameters or []).fetchall()


class TestCollectedRunDigest:
    """Issue #209: the fingerprint that lets a scan prove a run unchanged."""

    def test_a_digest_written_with_the_rows_is_read_back_per_run(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        state_store.replace_collected_run(
            CollectedRunRecords(
                run_id=run_id,
                verdicts=[_verdict(run_id, "AAPL")],
                document_digest="sha256:aapl",
            )
        )

        assert state_store.get_verdict_collection_digests() == {run_id: "sha256:aapl"}

    def test_a_replacement_without_a_digest_clears_the_previous_one(
        self, state_store: StateStore
    ) -> None:
        # Rows written from documents the caller cannot name must never let a
        # later scan skip the run.
        run_id = uuid4()
        state_store.replace_collected_run(
            CollectedRunRecords(
                run_id=run_id,
                verdicts=[_verdict(run_id, "AAPL")],
                document_digest="sha256:aapl",
            )
        )

        state_store.replace_run_verdicts(run_id, [_verdict(run_id, "AAPL")], [])

        assert state_store.get_verdict_collection_digests() == {}

    def test_a_failed_replacement_rolls_the_digest_back_with_the_rows(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_collected_run(
            CollectedRunRecords(
                run_id=run_id,
                verdicts=[_verdict(run_id, "AAPL")],
                document_digest="sha256:original",
            )
        )

        with pytest.raises(duckdb.Error):
            state_store.replace_collected_run(
                CollectedRunRecords(
                    run_id=run_id,
                    verdicts=[_verdict(run_id, "MSFT", recommendation="invalid")],
                    document_digest="sha256:corrected",
                )
            )

        assert state_store.get_verdict_collection_digests() == {
            run_id: "sha256:original"
        }
        assert _rows(state_store, "SELECT symbol FROM verdicts") == [("AAPL",)]


class TestReplaceRunVerdicts:
    def test_persists_verdicts_and_their_cited_sources(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        state_store.replace_run_verdicts(
            run_id,
            [
                _verdict(
                    run_id,
                    "AAPL",
                    reasons=(
                        VerdictReasonRecord(text="堅調な受注", source_ids=("news-1",)),
                    ),
                )
            ],
            [_source(run_id, "AAPL", "news-1")],
        )

        rows = _rows(
            state_store,
            "SELECT symbol, strategy_key, as_of, recommendation, no_trade, "
            "reasons_json FROM verdicts",
        )
        assert [row[:5] for row in rows] == [
            ("AAPL", "default", AS_OF, "proceed", False)
        ]
        assert json.loads(str(rows[0][5])) == [
            {"text": "堅調な受注", "source_ids": ["news-1"], "basis": None}
        ]
        assert _rows(
            state_store,
            "SELECT symbol, source_id, source_type FROM verdict_sources",
        ) == [("AAPL", "news-1", "news")]

    def test_persists_the_run_level_no_trade_flag(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        state_store.replace_run_verdicts(
            run_id,
            [
                VerdictRecord(
                    run_id=run_id,
                    symbol="AAPL",
                    as_of=AS_OF,
                    strategy_key="default",
                    recommendation="skip",
                    reasons=(),
                    no_trade=True,
                )
            ],
            [],
        )

        assert _rows(state_store, "SELECT no_trade FROM verdicts") == [(True,)]

    def test_persists_and_replaces_filing_coverage(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id,
            [_verdict(run_id, "AAPL")],
            [],
            [_coverage(run_id)],
        )

        rows = state_store.get_analysis_source_coverages(run_id, "AAPL")
        assert len(rows) == 1
        assert rows[0].exported_chars == 120_000
        assert rows[0].sections == (
            ("part_i_item_2", "full"),
            ("part_ii_item_1a", "partial"),
        )

        state_store.replace_run_verdicts(
            run_id,
            [_verdict(run_id, "AAPL")],
            [],
            [_coverage(run_id, exported_chars=100_000)],
        )

        replaced = state_store.get_analysis_source_coverages(run_id, "AAPL")
        assert len(replaced) == 1
        assert replaced[0].exported_chars == 100_000

    @pytest.mark.parametrize(
        "exhibit_truncated",
        [
            pytest.param(True, id="exhibit-cut-at-collection"),
            pytest.param(False, id="no-marker-in-the-text"),
            pytest.param(None, id="row-written-before-the-column-existed"),
        ],
    )
    def test_round_trips_the_collection_stage_exhibit_signal(
        self, state_store: StateStore, exhibit_truncated: bool | None
    ) -> None:
        # Issue #157: `None` must survive as `None`. Collapsing it to `False`
        # on the way in or out would restate "not recorded" as "no exhibit was
        # cut", which is the misreading the field exists to prevent.
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id,
            [_verdict(run_id, "AAPL")],
            [],
            [_coverage(run_id, exhibit_truncated=exhibit_truncated)],
        )

        rows = state_store.get_analysis_source_coverages(run_id, "AAPL")

        assert rows[0].exhibit_truncated is exhibit_truncated

    def test_rerun_replaces_corrected_rows_without_duplicating(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id,
            [_verdict(run_id, "AAPL", "proceed")],
            [_source(run_id, "AAPL", "news-1")],
        )

        # The skill's answer was re-ingested with a corrected recommendation.
        state_store.replace_run_verdicts(
            run_id,
            [_verdict(run_id, "AAPL", "skip")],
            [_source(run_id, "AAPL", "news-1")],
        )

        assert _rows(state_store, "SELECT symbol, recommendation FROM verdicts") == [
            ("AAPL", "skip")
        ]
        assert _rows(state_store, "SELECT count(*) FROM verdict_sources") == [(1,)]

    def test_replacement_drops_rows_absent_from_the_new_set(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id,
            [_verdict(run_id, "AAPL"), _verdict(run_id, "MSFT")],
            [_source(run_id, "AAPL", "news-1"), _source(run_id, "MSFT", "news-2")],
        )

        state_store.replace_run_verdicts(
            run_id, [_verdict(run_id, "AAPL")], [_source(run_id, "AAPL", "news-1")]
        )

        assert _rows(state_store, "SELECT symbol FROM verdicts") == [("AAPL",)]
        assert _rows(state_store, "SELECT symbol FROM verdict_sources") == [("AAPL",)]

    def test_an_empty_replacement_clears_the_run(self, state_store: StateStore) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id, [_verdict(run_id, "AAPL")], [_source(run_id, "AAPL", "news-1")]
        )

        state_store.replace_run_verdicts(run_id, [], [])

        assert _rows(state_store, "SELECT count(*) FROM verdicts") == [(0,)]
        assert _rows(state_store, "SELECT count(*) FROM verdict_sources") == [(0,)]

    def test_other_runs_are_untouched(self, state_store: StateStore) -> None:
        kept, replaced = uuid4(), uuid4()
        state_store.replace_run_verdicts(kept, [_verdict(kept, "AAPL")], [])
        state_store.replace_run_verdicts(replaced, [_verdict(replaced, "MSFT")], [])

        state_store.replace_run_verdicts(replaced, [], [])

        assert _rows(state_store, "SELECT symbol FROM verdicts") == [("AAPL",)]

    def test_rejects_records_belonging_to_another_run(
        self, state_store: StateStore
    ) -> None:
        run_id, other_run_id = uuid4(), uuid4()

        with pytest.raises(ValueError, match="must match the replacement run_id"):
            state_store.replace_run_verdicts(
                run_id, [_verdict(other_run_id, "AAPL")], []
            )

    def test_rejects_sources_belonging_to_another_run(
        self, state_store: StateStore
    ) -> None:
        run_id, other_run_id = uuid4(), uuid4()

        with pytest.raises(ValueError, match="must match the replacement run_id"):
            state_store.replace_run_verdicts(
                run_id, [], [_source(other_run_id, "AAPL", "news-1")]
            )

    def test_rejects_a_recommendation_outside_the_schema_check(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        with pytest.raises(duckdb.ConstraintException):
            state_store.replace_run_verdicts(
                run_id, [_verdict(run_id, "AAPL", "maybe")], []
            )

    def test_rejects_a_source_type_outside_the_schema_check(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        with pytest.raises(duckdb.ConstraintException):
            state_store.replace_run_verdicts(
                run_id, [], [_source(run_id, "AAPL", "x-1", "rumor")]
            )


class _FlakyVerdictConnection:
    """Wraps a real connection; raises on the Nth `INSERT INTO <table>`.

    Lets the rollback tests inject a failure *after* at least one row of the
    same logical write has already been inserted inside the transaction.
    """

    def __init__(
        self, real_conn: duckdb.DuckDBPyConnection, table: str, fail_on_call: int
    ):
        self._real = real_conn
        self._prefix = f"INSERT INTO {table}"
        self._fail_on_call = fail_on_call
        self._insert_calls = 0

    def execute(self, sql, parameters=None):
        if sql.lstrip().startswith(self._prefix):
            self._insert_calls += 1
            if self._insert_calls == self._fail_on_call:
                msg = "simulated failure on a later verdict insert"
                raise RuntimeError(msg)
        if parameters is None:
            return self._real.execute(sql)
        return self._real.execute(sql, parameters)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


def _inject_failure(
    state_store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    fail_on_call: int,
) -> None:
    real_connect = state_store.database.connect
    monkeypatch.setattr(
        state_store.database,
        "connect",
        lambda: _FlakyVerdictConnection(real_connect(), table, fail_on_call),
    )


class TestReplaceRunVerdictsAtomicity:
    def test_a_failure_after_an_earlier_insert_rolls_the_whole_write_back(
        self, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = uuid4()
        _inject_failure(state_store, monkeypatch, "verdicts", fail_on_call=2)

        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.replace_run_verdicts(
                run_id, [_verdict(run_id, "AAPL"), _verdict(run_id, "MSFT")], []
            )

        monkeypatch.undo()
        assert _rows(state_store, "SELECT count(*) FROM verdicts") == [(0,)]

    def test_a_failure_preserves_the_previously_committed_run(
        self, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id,
            [_verdict(run_id, "AAPL", "proceed")],
            [_source(run_id, "AAPL", "news-1")],
        )
        _inject_failure(state_store, monkeypatch, "verdicts", fail_on_call=1)

        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.replace_run_verdicts(
                run_id, [_verdict(run_id, "AAPL", "skip")], []
            )

        monkeypatch.undo()
        # The DELETE ran before the failing INSERT: rollback must restore it.
        assert _rows(state_store, "SELECT symbol, recommendation FROM verdicts") == [
            ("AAPL", "proceed")
        ]
        assert _rows(state_store, "SELECT count(*) FROM verdict_sources") == [(1,)]

    def test_a_source_insert_failure_also_rolls_the_verdicts_back(
        self, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = uuid4()
        _inject_failure(state_store, monkeypatch, "verdict_sources", fail_on_call=2)

        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.replace_run_verdicts(
                run_id,
                [_verdict(run_id, "AAPL")],
                [_source(run_id, "AAPL", "news-1"), _source(run_id, "AAPL", "news-2")],
            )

        monkeypatch.undo()
        assert _rows(state_store, "SELECT count(*) FROM verdicts") == [(0,)]
        assert _rows(state_store, "SELECT count(*) FROM verdict_sources") == [(0,)]

    def test_a_rerun_after_a_failure_succeeds(
        self, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = uuid4()
        _inject_failure(state_store, monkeypatch, "verdicts", fail_on_call=1)
        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.replace_run_verdicts(run_id, [_verdict(run_id, "AAPL")], [])
        monkeypatch.undo()

        state_store.replace_run_verdicts(run_id, [_verdict(run_id, "AAPL")], [])

        assert _rows(state_store, "SELECT symbol FROM verdicts") == [("AAPL",)]


class TestNormalizedVerdictReasons:
    """Issue #192: `reasons_json` projected into queryable rows."""

    def test_a_non_iterable_source_ids_fails_as_a_domain_error(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        with state_store.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO verdicts (
                    run_id, symbol, as_of, strategy_key, recommendation,
                    reasons_json, no_trade
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    "AAPL",
                    AS_OF,
                    "default",
                    "proceed",
                    json.dumps([{"text": "壊れた理由", "source_ids": 123}]),
                    False,
                ],
            )

        with pytest.raises(SwingCopilotError, match="source_ids"):
            state_store.get_run_verdicts(run_id)

    def test_writes_one_row_per_reason_with_its_index_and_citation_count(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        reasons = (
            VerdictReasonRecord("guidance raised", ("news-1", "news-2"), "filing"),
            VerdictReasonRecord("momentum intact", (), None),
        )

        state_store.replace_run_verdicts(
            run_id, [_verdict(run_id, "AAPL", reasons=reasons)], []
        )

        assert _rows(
            state_store,
            "SELECT symbol, reason_index, text, basis, source_id_count "
            "FROM verdict_reasons ORDER BY reason_index",
        ) == [
            ("AAPL", 0, "guidance raised", "filing", 2),
            ("AAPL", 1, "momentum intact", None, 0),
        ]
        assert _rows(
            state_store,
            "SELECT reason_index, source_id FROM verdict_reason_sources "
            "ORDER BY reason_index, source_id",
        ) == [(0, "news-1"), (0, "news-2")]

    def test_reasons_json_stays_the_record_of_truth(
        self, state_store: StateStore
    ) -> None:
        """The rows are a projection; the document itself is still written."""
        run_id = uuid4()
        reasons = (VerdictReasonRecord("guidance raised", ("news-1",), "filing"),)

        state_store.replace_run_verdicts(
            run_id, [_verdict(run_id, "AAPL", reasons=reasons)], []
        )

        assert state_store.get_run_verdicts(run_id)[0].reasons == reasons

    def test_a_reinsertion_drops_reasons_of_a_symbol_no_longer_analyzed(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id,
            [
                _verdict(
                    run_id, "AAPL", reasons=(VerdictReasonRecord("kept", ("n-1",)),)
                ),
                _verdict(
                    run_id, "MSFT", reasons=(VerdictReasonRecord("dropped", ("n-2",)),)
                ),
            ],
            [],
        )

        state_store.replace_run_verdicts(
            run_id,
            [_verdict(run_id, "AAPL", reasons=(VerdictReasonRecord("rewritten", ()),))],
            [],
        )

        assert _rows(state_store, "SELECT symbol, text FROM verdict_reasons") == [
            ("AAPL", "rewritten")
        ]
        assert _rows(state_store, "SELECT count(*) FROM verdict_reason_sources") == [
            (0,)
        ]

    def test_a_failure_after_an_earlier_reason_rolls_the_whole_write_back(
        self, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = uuid4()
        _inject_failure(state_store, monkeypatch, "verdict_reasons", fail_on_call=2)

        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.replace_run_verdicts(
                run_id,
                [
                    _verdict(
                        run_id,
                        "AAPL",
                        reasons=(
                            VerdictReasonRecord("first", ()),
                            VerdictReasonRecord("second", ()),
                        ),
                    )
                ],
                [],
            )

        monkeypatch.undo()
        assert _rows(state_store, "SELECT count(*) FROM verdicts") == [(0,)]
        assert _rows(state_store, "SELECT count(*) FROM verdict_reasons") == [(0,)]

    def test_a_duplicate_source_id_in_one_reason_is_recorded_once(
        self, state_store: StateStore
    ) -> None:
        """`source_id_count` counts the citation list; the rows are a set."""
        run_id = uuid4()
        reasons = (VerdictReasonRecord("cited twice", ("news-1", "news-1")),)

        state_store.replace_run_verdicts(
            run_id, [_verdict(run_id, "AAPL", reasons=reasons)], []
        )

        assert _rows(state_store, "SELECT source_id FROM verdict_reason_sources") == [
            ("news-1",)
        ]
        assert _rows(state_store, "SELECT source_id_count FROM verdict_reasons") == [
            (2,)
        ]


class TestReplaceVerdictOutcomes:
    def test_persists_one_row_per_symbol_and_horizon(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "AAPL")])

        assert _rows(
            state_store,
            "SELECT symbol, horizon_days, as_of, recommendation, "
            "forward_return_pct, classification FROM verdict_outcomes",
        ) == [("AAPL", 5, AS_OF, "proceed", 1.5, "HIT")]

    def test_round_trips_the_benchmark_return_of_the_same_span(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        outcome = replace(_outcome(run_id, "AAPL"), benchmark_return_pct=2.25)

        state_store.replace_verdict_outcomes(run_id, 5, [outcome])

        stored = state_store.get_verdict_outcomes_in_window(AS_OF, AS_OF)
        assert [row.benchmark_return_pct for row in stored] == [2.25]

    def test_an_unmeasured_benchmark_round_trips_as_none_not_zero(
        self, state_store: StateStore
    ) -> None:
        # Issue #190: NULL means "not measured" (a row classified before the
        # column existed, or one whose benchmark bars were missing) and must
        # never come back as a flat market.
        run_id = uuid4()

        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "AAPL")])

        stored = state_store.get_verdict_outcomes_in_window(AS_OF, AS_OF)
        assert [row.benchmark_return_pct for row in stored] == [None]

    def test_round_trips_the_closes_the_classification_was_computed_from(
        self, state_store: StateStore
    ) -> None:
        # Issue #413: audit columns, so a repaired store can still answer
        # "which prices was this classified at" for the rows written before
        # the repair.
        run_id = uuid4()
        outcome = replace(
            _outcome(run_id, "MNST"), entry_close=48.615, maturity_close=47.81
        )

        state_store.replace_verdict_outcomes(run_id, 5, [outcome])

        stored = state_store.get_verdict_outcomes_in_window(AS_OF, AS_OF)
        assert [(row.entry_close, row.maturity_close) for row in stored] == [
            (48.615, 47.81)
        ]

    def test_unrecorded_closes_round_trip_as_none(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "AAPL")])

        stored = state_store.get_verdict_outcomes_in_window(AS_OF, AS_OF)
        assert [(row.entry_close, row.maturity_close) for row in stored] == [
            (None, None)
        ]

    @pytest.mark.parametrize(
        ("entry_close", "maturity_close"),
        [
            pytest.param(float("nan"), 47.81, id="entry"),
            pytest.param(48.615, float("inf"), id="maturity"),
        ],
    )
    def test_a_non_finite_audit_close_is_rejected_before_the_slice_is_touched(
        self, state_store: StateStore, entry_close: float, maturity_close: float
    ) -> None:
        # DuckDB's NaN is not NULL, so a stored NaN would read back as "this
        # is the price it was classified at" rather than "not recorded".
        run_id = uuid4()
        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "KEPT")])
        broken = replace(
            _outcome(run_id, "AAPL"),
            entry_close=entry_close,
            maturity_close=maturity_close,
        )

        with pytest.raises(ValueError, match="must be finite"):
            state_store.replace_verdict_outcomes(run_id, 5, [broken])

        assert _rows(state_store, "SELECT symbol FROM verdict_outcomes") == [("KEPT",)]

    def test_rerun_with_corrected_prices_updates_rather_than_duplicates(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "AAPL")])

        state_store.replace_verdict_outcomes(
            run_id,
            5,
            [
                _outcome(
                    run_id,
                    "AAPL",
                    forward_return_pct=-5.0,
                    classification="MISS_SEVERE",
                )
            ],
        )

        assert _rows(
            state_store,
            "SELECT forward_return_pct, classification FROM verdict_outcomes",
        ) == [(-5.0, "MISS_SEVERE")]

    def test_replacement_is_scoped_to_one_horizon(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "AAPL", 5)])
        state_store.replace_verdict_outcomes(run_id, 20, [_outcome(run_id, "AAPL", 20)])

        state_store.replace_verdict_outcomes(run_id, 5, [])

        assert _rows(state_store, "SELECT horizon_days FROM verdict_outcomes") == [
            (20,)
        ]

    def test_rejects_records_from_another_run_or_horizon(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        with pytest.raises(ValueError, match="must match the replacement"):
            state_store.replace_verdict_outcomes(
                run_id, 5, [_outcome(run_id, "AAPL", 20)]
            )

    def test_rejects_a_classification_outside_the_schema_check(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        with pytest.raises(duckdb.ConstraintException):
            state_store.replace_verdict_outcomes(
                run_id, 5, [_outcome(run_id, "AAPL", classification="MAYBE")]
            )

    @pytest.mark.parametrize(
        "bad_value",
        [
            pytest.param(float("nan"), id="nan"),
            pytest.param(float("inf"), id="inf"),
            pytest.param(float("-inf"), id="-inf"),
        ],
    )
    def test_rejects_a_non_finite_forward_return(
        self, state_store: StateStore, bad_value: float
    ) -> None:
        """Issue #227: `DOUBLE NOT NULL` cannot say "a measured, finite return".

        DuckDB's NaN is not NULL, so without this the row would persist as
        neither a win nor a loss and skew every aggregate over it.
        """
        run_id = uuid4()

        with pytest.raises(ValueError, match="must be finite"):
            state_store.replace_verdict_outcomes(
                run_id, 5, [_outcome(run_id, "AAPL", forward_return_pct=bad_value)]
            )

    def test_rejects_a_non_finite_benchmark_return(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        outcome = replace(_outcome(run_id, "AAPL"), benchmark_return_pct=float("inf"))

        with pytest.raises(ValueError, match="must be finite"):
            state_store.replace_verdict_outcomes(run_id, 5, [outcome])

    def test_a_non_finite_record_leaves_the_previous_slice_intact(
        self, state_store: StateStore
    ) -> None:
        """The check runs before the transaction, so the DELETE never fires."""
        run_id = uuid4()
        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "KEPT")])

        with pytest.raises(ValueError, match="must be finite"):
            state_store.replace_verdict_outcomes(
                run_id,
                5,
                [
                    _outcome(run_id, "AAPL"),
                    _outcome(run_id, "MSFT", forward_return_pct=float("nan")),
                ],
            )

        assert _rows(state_store, "SELECT symbol FROM verdict_outcomes") == [("KEPT",)]

    def test_a_failure_after_an_earlier_insert_rolls_the_whole_write_back(
        self, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = uuid4()
        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "KEPT")])
        _inject_failure(state_store, monkeypatch, "verdict_outcomes", fail_on_call=2)

        with pytest.raises(RuntimeError, match="simulated failure"):
            state_store.replace_verdict_outcomes(
                run_id, 5, [_outcome(run_id, "AAPL"), _outcome(run_id, "MSFT")]
            )

        monkeypatch.undo()
        assert _rows(state_store, "SELECT symbol FROM verdict_outcomes") == [("KEPT",)]


class TestGetVerdictOutcomesForSlice:
    """Issue #424: what `_evaluate_slice` reads back.

    So it can carry an existing row forward instead of dropping it when it
    cannot recompute that row this round.
    """

    def test_returns_the_recorded_rows_for_one_run_and_horizon(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_verdict_outcomes(
            run_id, 5, [_outcome(run_id, "AAPL"), _outcome(run_id, "MSFT")]
        )

        rows = state_store.get_verdict_outcomes_for_slice(run_id, 5)

        assert [row.symbol for row in rows] == ["AAPL", "MSFT"]

    def test_is_scoped_to_the_given_horizon(self, state_store: StateStore) -> None:
        run_id = uuid4()
        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "AAPL", 5)])
        state_store.replace_verdict_outcomes(run_id, 20, [_outcome(run_id, "AAPL", 20)])

        rows = state_store.get_verdict_outcomes_for_slice(run_id, 5)

        assert [row.horizon_days for row in rows] == [5]

    def test_is_scoped_to_the_given_run(self, state_store: StateStore) -> None:
        run_id, other_run_id = uuid4(), uuid4()
        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "AAPL")])
        state_store.replace_verdict_outcomes(
            other_run_id, 5, [_outcome(other_run_id, "MSFT")]
        )

        rows = state_store.get_verdict_outcomes_for_slice(run_id, 5)

        assert [row.symbol for row in rows] == ["AAPL"]

    def test_an_unrecorded_slice_returns_no_rows(self, state_store: StateStore) -> None:
        assert state_store.get_verdict_outcomes_for_slice(uuid4(), 5) == ()


class TestGetVerdictsInWindow:
    def test_returns_rows_inside_the_inclusive_window_only(
        self, state_store: StateStore
    ) -> None:
        before, start, inside, end, after = (
            date(2026, 7, 9),
            date(2026, 7, 10),
            date(2026, 7, 15),
            date(2026, 7, 20),
            date(2026, 7, 21),
        )
        for index, run_date in enumerate((before, start, inside, end, after)):
            run_id = uuid4()
            state_store.replace_run_verdicts(
                run_id, [_verdict(run_id, f"S{index}", as_of=run_date)], []
            )

        rows = state_store.get_verdicts_in_window(start, end)

        assert [(row.symbol, row.as_of) for row in rows] == [
            ("S1", start),
            ("S2", inside),
            ("S3", end),
        ]

    def test_carries_the_recommendation_needed_for_classification(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(run_id, [_verdict(run_id, "AAPL", "skip")], [])

        rows = state_store.get_verdicts_in_window(AS_OF, AS_OF)

        assert [(row.run_id, row.symbol, row.recommendation) for row in rows] == [
            (run_id, "AAPL", "skip")
        ]

    def test_carries_the_archived_news_supply_for_the_threshold_review(
        self, state_store: StateStore
    ) -> None:
        # Issue #154: the window read is what the retrospective crosses the
        # supply level against, so the measurement has to travel with the row.
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id,
            [_verdict_measured(run_id, "AAPL")],
            [],
        )

        rows = state_store.get_verdicts_in_window(AS_OF, AS_OF)

        assert rows[0].news_supply == NewsSupplyRecord(
            collected_items=20,
            exported_items=12,
            symbol_mention_items=4,
            level="sparse",
        )

    def test_returns_nothing_for_an_empty_database(
        self, state_store: StateStore
    ) -> None:
        assert state_store.get_verdicts_in_window(AS_OF, AS_OF) == ()


class TestNewsSupplyColumns:
    """Issue #154: the supply a verdict was made under, stored beside it."""

    def test_round_trips_every_count_and_the_graded_level(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id,
            [_verdict_measured(run_id, "AAPL")],
            [],
        )

        assert state_store.get_run_verdicts(run_id)[0].news_supply == _news_supply()

    def test_an_unmeasured_verdict_stores_null_rather_than_zero(
        self, state_store: StateStore
    ) -> None:
        # An archive written before Issue #130 measured nothing. Writing zeros
        # would read back as a measured `none`, which is a different claim.
        run_id = uuid4()
        state_store.replace_run_verdicts(run_id, [_verdict(run_id, "AAPL")], [])

        assert _rows(
            state_store,
            "SELECT news_supply_collected_items, news_supply_exported_items, "
            "news_supply_symbol_mention_items, news_supply_level FROM verdicts",
        ) == [(None, None, None, None)]
        assert state_store.get_run_verdicts(run_id)[0].news_supply is None

    def test_a_recollected_run_picks_up_a_measurement_it_lacked(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(run_id, [_verdict(run_id, "AAPL")], [])

        state_store.replace_run_verdicts(
            run_id,
            [_verdict_measured(run_id, "AAPL", level="sufficient")],
            [],
        )

        stored = state_store.get_run_verdicts(run_id)[0].news_supply
        assert stored is not None
        assert stored.level == "sufficient"

    def test_rejects_a_level_outside_the_schema_check(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()

        with pytest.raises(duckdb.ConstraintException):
            state_store.replace_run_verdicts(
                run_id,
                [_verdict_measured(run_id, "AAPL", level="plenty")],
                [],
            )


class TestGetVerdictOutcomesInWindow:
    """P8-31: the aggregate metrics' source rows, scoped by *maturity* date."""

    def test_returns_rows_inside_the_inclusive_maturity_window_only(
        self, state_store: StateStore
    ) -> None:
        before, start, end, after = (
            date(2026, 7, 9),
            date(2026, 7, 10),
            date(2026, 7, 20),
            date(2026, 7, 21),
        )
        for index, maturity in enumerate((before, start, end, after)):
            run_id = uuid4()
            state_store.replace_verdict_outcomes(
                run_id,
                5,
                [
                    VerdictOutcomeRecord(
                        run_id=run_id,
                        symbol=f"S{index}",
                        horizon_days=5,
                        as_of=maturity,
                        recommendation="proceed",
                        forward_return_pct=1.0,
                        classification="HIT",
                    )
                ],
            )

        rows = state_store.get_verdict_outcomes_in_window(start, end)

        assert [(row.symbol, row.as_of) for row in rows] == [("S1", start), ("S2", end)]

    def test_returns_every_evaluated_horizon_of_one_run(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        for horizon_days in (5, 20):
            state_store.replace_verdict_outcomes(
                run_id,
                horizon_days,
                [
                    _outcome(
                        run_id,
                        "AAPL",
                        horizon_days,
                        forward_return_pct=-3.0,
                        classification="MISS_SEVERE",
                    )
                ],
            )

        rows = state_store.get_verdict_outcomes_in_window(AS_OF, AS_OF)

        assert [(row.horizon_days, row.classification) for row in rows] == [
            (5, "MISS_SEVERE"),
            (20, "MISS_SEVERE"),
        ]

    def test_returns_nothing_for_an_empty_database(
        self, state_store: StateStore
    ) -> None:
        assert state_store.get_verdict_outcomes_in_window(AS_OF, AS_OF) == ()


class TestGetRunVerdicts:
    """P8-31: one surprise symbol's verdict as it was written at the time."""

    def test_returns_the_run_s_verdicts_with_their_reasons(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id,
            [
                _verdict(
                    run_id,
                    "MSFT",
                    "skip",
                    reasons=(
                        VerdictReasonRecord(text="供給制約の懸念", source_ids=("n-1",)),
                        VerdictReasonRecord(text="決算前", source_ids=()),
                    ),
                )
            ],
            [],
        )

        rows = state_store.get_run_verdicts(run_id)

        assert len(rows) == 1
        assert (rows[0].symbol, rows[0].recommendation) == ("MSFT", "skip")
        assert rows[0].strategy_key == "default"
        assert rows[0].as_of == AS_OF
        assert rows[0].no_trade is False
        assert rows[0].reasons == (
            VerdictReasonRecord(text="供給制約の懸念", source_ids=("n-1",)),
            VerdictReasonRecord(text="決算前", source_ids=()),
        )

    def test_returns_nothing_for_an_unknown_run(self, state_store: StateStore) -> None:
        assert state_store.get_run_verdicts(uuid4()) == ()


class TestGetVerdictCitationsInWindow:
    """P8-31: `verdict_sources` x `verdict_outcomes` x `text_items` (design §5.3)."""

    def test_returns_one_row_per_cited_source_with_its_recorded_url(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.record_text_items(
            [
                TextItem(
                    source_id="finnhub:1",
                    symbol="AAPL",
                    source_type="news",
                    published_at=datetime(2026, 7, 15, tzinfo=UTC),
                    title="headline",
                    source_url="https://example.test/1",
                    content_text="body",
                    fetched_at=datetime(2026, 7, 15, tzinfo=UTC),
                )
            ]
        )
        state_store.replace_run_verdicts(
            run_id, [_verdict(run_id, "AAPL")], [_source(run_id, "AAPL", "finnhub:1")]
        )
        for horizon_days in (5, 20):
            state_store.replace_verdict_outcomes(
                run_id, horizon_days, [_outcome(run_id, "AAPL", horizon_days)]
            )

        rows = state_store.get_verdict_citations_in_window(AS_OF, AS_OF)

        # One row per citation, not one per horizon: the same source cited
        # once must not be counted twice because two horizons matured.
        assert [
            (row.symbol, row.source_id, row.source_type, row.source_url) for row in rows
        ] == [("AAPL", "finnhub:1", "news", "https://example.test/1")]

    def test_keeps_a_citation_whose_text_item_was_never_recorded(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id, [_verdict(run_id, "AAPL")], [_source(run_id, "AAPL", "edgar:9")]
        )
        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, "AAPL")])

        rows = state_store.get_verdict_citations_in_window(AS_OF, AS_OF)

        assert [(row.source_id, row.source_url) for row in rows] == [("edgar:9", None)]

    def test_omits_citations_whose_verdict_has_not_matured_in_the_window(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id, [_verdict(run_id, "AAPL")], [_source(run_id, "AAPL", "finnhub:1")]
        )

        assert state_store.get_verdict_citations_in_window(AS_OF, AS_OF) == ()


class TestReasonTextVisiblePredicate:
    """Issue #389: two substrates for the same rule must never disagree.

    `reason_text_visible_sql` (SQL text) and `is_reason_text_visible` (plain
    Python) encode the exact same predicate -- what the dashboard shows and
    what `get_prior_verdicts` re-injects must never disagree, so both must
    return the same boolean for the same inputs.
    """

    @pytest.mark.parametrize(
        ("started_at", "run_date", "expected"),
        [
            pytest.param(None, date(2026, 8, 20), False, id="no_run_before_cutoff"),
            pytest.param(None, date(2026, 8, 21), True, id="no_run_at_cutoff"),
            pytest.param(
                ACCOUNT_INDEPENDENT_EXPORT_SINCE - timedelta(seconds=1),
                date(2026, 8, 20),
                False,
                id="started_at_just_before_export_since_run_date_before_cutoff",
            ),
            pytest.param(
                ACCOUNT_INDEPENDENT_EXPORT_SINCE,
                date(2020, 1, 1),
                True,
                id="started_at_at_export_since_overrides_an_old_run_date",
            ),
            pytest.param(
                ACCOUNT_INDEPENDENT_EXPORT_SINCE - timedelta(days=400),
                ACCOUNT_INDEPENDENT_VERDICT_CUTOFF,
                True,
                id="run_date_at_cutoff_overrides_an_old_started_at",
            ),
        ],
    )
    def test_the_python_mirror_agrees_with_the_sql_predicate(
        self,
        started_at: datetime | None,
        run_date: date,
        expected: bool,
    ) -> None:
        assert (
            is_reason_text_visible(started_at=started_at, run_date=run_date) is expected
        )

        with duckdb.connect(":memory:") as conn:
            conn.execute("CREATE TABLE t (started_at TIMESTAMPTZ, run_date DATE)")
            conn.execute("INSERT INTO t VALUES (?, ?)", [started_at, run_date])
            predicate = reason_text_visible_sql()
            row = conn.execute(
                f"SELECT {predicate} FROM t",  # noqa: S608 - fixed predicate, no interpolated input
                [ACCOUNT_INDEPENDENT_EXPORT_SINCE, ACCOUNT_INDEPENDENT_VERDICT_CUTOFF],
            ).fetchone()
        assert row is not None
        assert bool(row[0]) is expected


class TestGetPriorVerdicts:
    """Issue #191: a repeat candidate's own earlier verdicts, fed back in.

    Every verdict date in this class is on or after
    `ACCOUNT_INDEPENDENT_VERDICT_CUTOFF` (2026-08-21) unless a test is
    specifically exercising that cutoff (Issue #385): a verdict dated before
    it is never visible through `get_prior_verdicts` regardless of the
    point-in-time `before_date` argument, so an older date would make an
    unrelated assertion pass for the wrong reason.
    """

    #: A `before_date` comfortably after the account-dependent cutoff, so the
    #: point-in-time (`as_of < before_date`) tests below exercise only that
    #: rule and never trip the account-dependent one by accident.
    _BEFORE_DATE = date(2026, 8, 25)

    @staticmethod
    def _write(
        state_store: StateStore,
        symbol: str,
        as_of: date,
        *,
        reasons: tuple[VerdictReasonRecord, ...] = (),
        strategy_key: str = "default",
    ) -> UUID:
        """Write a verdict with no `runs` row for it (the degrade-to-`as_of` case).

        Every pre-existing test in this class relies on this: without a
        `runs` row, `get_prior_verdicts`' `LEFT JOIN runs` reads `started_at`
        as `NULL`, so `reason_text_visible_sql`'s first term is never true and
        visibility falls through to `as_of >= ACCOUNT_INDEPENDENT_VERDICT_CUTOFF`
        alone -- the pre-#389 rule, unchanged.
        """
        run_id = uuid4()
        verdict = replace(
            _verdict(run_id, symbol, as_of=as_of, reasons=reasons),
            strategy_key=strategy_key,
        )
        state_store.replace_run_verdicts(run_id, [verdict], [])
        return run_id

    @staticmethod
    def _write_with_run(
        state_store: StateStore, symbol: str, as_of: date, started_at: datetime
    ) -> UUID:
        """Write a verdict whose owning `runs` row has an explicit `started_at`.

        Simulates `--as-of` (Issue #389): `run_date`/`as_of` is the replayed
        date, `started_at` is the instant the run actually executed, and the
        two may disagree by months.
        """
        run_id = uuid4()
        verdict = _verdict(run_id, symbol, as_of=as_of)
        state_store.replace_run_verdicts(run_id, [verdict], [])
        seed_run(state_store, run_id, as_of, started_at=started_at)
        return run_id

    def test_returns_the_reasons_and_their_matured_outcomes(
        self, state_store: StateStore
    ) -> None:
        run_id = self._write(
            state_store,
            "AAPL",
            date(2026, 8, 21),
            reasons=(
                VerdictReasonRecord(
                    text="受注が伸びている",
                    source_ids=("edgar:1",),
                    basis="filing_fundamental",
                ),
            ),
        )
        state_store.replace_verdict_outcomes(
            run_id,
            5,
            [
                _outcome(
                    run_id,
                    "AAPL",
                    5,
                    forward_return_pct=-6.0,
                    classification="MISS_SEVERE",
                )
            ],
        )

        prior = state_store.get_prior_verdicts("AAPL", "default", self._BEFORE_DATE, 3)

        assert len(prior) == 1
        assert prior[0].recommendation == "proceed"
        assert prior[0].reasons[0].basis == "filing_fundamental"
        assert prior[0].outcomes == (
            PriorVerdictOutcome(
                horizon_days=5, classification="MISS_SEVERE", forward_return_pct=-6.0
            ),
        )

    def test_a_verdict_whose_horizons_are_still_open_returns_no_outcomes(
        self, state_store: StateStore
    ) -> None:
        """Not an error and not a neutral result -- just not measurable yet."""
        self._write(state_store, "AAPL", date(2026, 8, 21))

        prior = state_store.get_prior_verdicts("AAPL", "default", self._BEFORE_DATE, 3)

        assert len(prior) == 1
        assert prior[0].outcomes == ()

    @pytest.mark.parametrize(
        ("verdict_date", "is_visible"),
        [
            pytest.param(date(2026, 8, 24), True, id="day_before_cutoff"),
            pytest.param(_BEFORE_DATE, False, id="exactly_at_cutoff"),
            pytest.param(date(2026, 8, 26), False, id="day_after_cutoff"),
        ],
    )
    def test_the_point_in_time_cutoff_is_strictly_exclusive(
        self, state_store: StateStore, verdict_date: date, is_visible: bool
    ) -> None:
        """Today's own verdict can never be fed back into today's own input."""
        self._write(state_store, "AAPL", verdict_date)

        prior = state_store.get_prior_verdicts("AAPL", "default", self._BEFORE_DATE, 3)

        assert bool(prior) is is_visible

    @pytest.mark.parametrize(
        ("verdict_date", "is_visible"),
        [
            pytest.param(
                date(2026, 8, 20), False, id="day_before_account_dependent_cutoff"
            ),
            pytest.param(
                ACCOUNT_INDEPENDENT_VERDICT_CUTOFF,
                True,
                id="exactly_at_account_dependent_cutoff",
            ),
            pytest.param(
                date(2026, 8, 22), True, id="day_after_account_dependent_cutoff"
            ),
        ],
    )
    def test_the_account_dependent_cutoff_is_inclusive(
        self, state_store: StateStore, verdict_date: date, is_visible: bool
    ) -> None:
        """Issue #385: pre-#352 verdicts may quote reader-account share counts."""
        self._write(state_store, "AAPL", verdict_date)

        prior = state_store.get_prior_verdicts("AAPL", "default", self._BEFORE_DATE, 3)

        assert bool(prior) is is_visible

    def test_a_symbol_with_only_pre_cutoff_verdicts_returns_empty(
        self, state_store: StateStore
    ) -> None:
        """UNH-shaped case: a symbol not re-candidated since before the cutoff."""
        self._write(state_store, "AAPL", date(2026, 7, 1))
        self._write(state_store, "AAPL", date(2026, 8, 20))

        assert (
            state_store.get_prior_verdicts("AAPL", "default", self._BEFORE_DATE, 3)
            == ()
        )

    def test_an_as_of_replay_of_a_pre_cutoff_date_is_still_fed_back(
        self, state_store: StateStore
    ) -> None:
        """Issue #389: an `--as-of` replay's `started_at` outruns its `as_of`.

        `copilot-daily --as-of <old date>` writes `as_of` to the replayed
        date but `started_at` to the real (much later) wall clock. The
        verdict it produced came from an already account-independent export
        and must still be re-injected, unlike a genuinely old run.
        """
        self._write_with_run(
            state_store,
            "AAPL",
            date(2026, 5, 1),
            ACCOUNT_INDEPENDENT_EXPORT_SINCE + timedelta(days=10),
        )

        prior = state_store.get_prior_verdicts("AAPL", "default", self._BEFORE_DATE, 3)

        assert len(prior) == 1
        assert prior[0].as_of == date(2026, 5, 1)

    def test_a_run_that_genuinely_predates_the_export_cutoff_stays_hidden(
        self, state_store: StateStore
    ) -> None:
        """A real pre-#352 run: both `started_at` and `as_of` predate the cutoffs."""
        self._write_with_run(
            state_store,
            "AAPL",
            date(2026, 8, 20),
            ACCOUNT_INDEPENDENT_EXPORT_SINCE - timedelta(days=1),
        )

        assert (
            state_store.get_prior_verdicts("AAPL", "default", self._BEFORE_DATE, 3)
            == ()
        )

    @pytest.mark.parametrize(
        ("started_at", "is_visible"),
        [
            pytest.param(
                ACCOUNT_INDEPENDENT_EXPORT_SINCE - timedelta(seconds=1),
                False,
                id="one_second_before_export_since",
            ),
            pytest.param(
                ACCOUNT_INDEPENDENT_EXPORT_SINCE, True, id="exactly_export_since"
            ),
            pytest.param(
                ACCOUNT_INDEPENDENT_EXPORT_SINCE + timedelta(seconds=1),
                True,
                id="one_second_after_export_since",
            ),
        ],
    )
    def test_the_started_at_boundary_is_inclusive(
        self, state_store: StateStore, started_at: datetime, is_visible: bool
    ) -> None:
        """Isolates the `started_at` term of the predicate.

        `as_of` (2026-08-20) stays before `ACCOUNT_INDEPENDENT_VERDICT_CUTOFF`
        throughout, so only `started_at` can move the result here.
        """
        self._write_with_run(state_store, "AAPL", date(2026, 8, 20), started_at)

        prior = state_store.get_prior_verdicts("AAPL", "default", self._BEFORE_DATE, 3)

        assert bool(prior) is is_visible

    def test_a_verdict_whose_run_is_unresolved_degrades_to_the_as_of_rule(
        self, state_store: StateStore
    ) -> None:
        """Degrades to the pre-#389 rule when a `runs` row cannot be found.

        No `runs` row (an archive recovered without one, or a data gap):
        `started_at` reads `NULL` and only
        `as_of >= ACCOUNT_INDEPENDENT_VERDICT_CUTOFF` decides visibility.
        """
        self._write(state_store, "AAPL", ACCOUNT_INDEPENDENT_VERDICT_CUTOFF)

        prior = state_store.get_prior_verdicts("AAPL", "default", self._BEFORE_DATE, 3)

        assert len(prior) == 1

    def test_only_the_same_strategys_verdicts_are_comparable_feedback(
        self, state_store: StateStore
    ) -> None:
        self._write(
            state_store, "AAPL", date(2026, 8, 21), strategy_key="mean_reversion"
        )

        assert (
            state_store.get_prior_verdicts("AAPL", "default", self._BEFORE_DATE, 3)
            == ()
        )

    def test_another_symbols_verdict_is_never_returned(
        self, state_store: StateStore
    ) -> None:
        self._write(state_store, "MSFT", date(2026, 8, 21))

        assert (
            state_store.get_prior_verdicts("AAPL", "default", self._BEFORE_DATE, 3)
            == ()
        )

    def test_the_newest_verdicts_are_kept_when_the_limit_bites(
        self, state_store: StateStore
    ) -> None:
        for day in (21, 22, 23):
            self._write(state_store, "AAPL", date(2026, 8, day))

        prior = state_store.get_prior_verdicts("AAPL", "default", self._BEFORE_DATE, 2)

        assert [record.as_of for record in prior] == [
            date(2026, 8, 23),
            date(2026, 8, 22),
        ]

    def test_a_non_positive_limit_reads_nothing_at_all(
        self, state_store: StateStore
    ) -> None:
        self._write(state_store, "AAPL", date(2026, 8, 21))

        assert (
            state_store.get_prior_verdicts("AAPL", "default", self._BEFORE_DATE, 0)
            == ()
        )

    def test_a_symbol_with_no_archived_verdict_returns_empty(
        self, state_store: StateStore
    ) -> None:
        assert (
            state_store.get_prior_verdicts("AAPL", "default", self._BEFORE_DATE, 3)
            == ()
        )


class TestGetVerdictReasonBasesInWindow:
    """Issue #191: the `basis` counterpart of the citation window read."""

    @staticmethod
    def _matured(
        state_store: StateStore,
        symbol: str,
        reasons: tuple[VerdictReasonRecord, ...],
    ) -> UUID:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id, [_verdict(run_id, symbol, reasons=reasons)], []
        )
        state_store.replace_verdict_outcomes(run_id, 5, [_outcome(run_id, symbol)])
        return run_id

    def test_a_basis_repeated_within_one_verdict_is_counted_once(
        self, state_store: StateStore
    ) -> None:
        """Measures which evidence decided a verdict, not how verbose it was."""
        self._matured(
            state_store,
            "AAPL",
            (
                VerdictReasonRecord("a", (), "filing_fundamental"),
                VerdictReasonRecord("b", (), "filing_fundamental"),
                VerdictReasonRecord("c", (), "news_catalyst"),
            ),
        )

        rows = state_store.get_verdict_reason_bases_in_window(AS_OF, AS_OF)

        assert [row.basis for row in rows] == ["filing_fundamental", "news_catalyst"]

    def test_an_untagged_reason_is_reported_rather_than_dropped(
        self, state_store: StateStore
    ) -> None:
        self._matured(state_store, "AAPL", (VerdictReasonRecord("a", ()),))

        rows = state_store.get_verdict_reason_bases_in_window(AS_OF, AS_OF)

        assert [row.basis for row in rows] == [None]

    def test_untagged_reasons_sort_after_every_tagged_one(
        self, state_store: StateStore
    ) -> None:
        self._matured(
            state_store,
            "AAPL",
            (
                VerdictReasonRecord("a", ()),
                VerdictReasonRecord("b", (), "technical_score"),
            ),
        )

        rows = state_store.get_verdict_reason_bases_in_window(AS_OF, AS_OF)

        assert [row.basis for row in rows] == ["technical_score", None]

    def test_a_verdict_that_never_matured_is_outside_the_window(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.replace_run_verdicts(
            run_id,
            [
                _verdict(
                    run_id,
                    "AAPL",
                    reasons=(VerdictReasonRecord("a", (), "technical_score"),),
                )
            ],
            [],
        )

        assert state_store.get_verdict_reason_bases_in_window(AS_OF, AS_OF) == ()
