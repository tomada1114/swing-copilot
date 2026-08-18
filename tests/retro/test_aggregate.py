"""P8-31: the retrospective's aggregate metrics (design §3.4).

Every expected value here is hand-calculated from the fixture rows in the
test's own docstring or comment, so a changed formula fails with a wrong
number rather than silently agreeing with itself.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date
from uuid import UUID, uuid4

import pytest

from swing_copilot.analysis.news_supply import DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS
from swing_copilot.config import PostmortemConfig
from swing_copilot.retro.aggregate import (
    PROCEED_SEVERE_MISS_WATCH_RATE,
    UNTAGGED_VERDICT_BASIS,
    compute_basis_contribution,
    compute_human_alignment,
    compute_news_supply_mix,
    compute_proceed_severe_miss_rate,
    compute_separation,
    compute_separation_paired,
    compute_separation_paired_excess,
    compute_skip_hit_rate,
    compute_source_contribution,
    compute_tracked_performance,
    compute_verdict_mix,
    wilson_interval,
)
from swing_copilot.storage.tracking_records import (
    VerdictPosition,
    VerdictPositionMark,
)
from swing_copilot.storage.verdict_records import (
    NewsSupplyRecord,
    VerdictCitationRow,
    VerdictDecisionRow,
    VerdictOutcomeRecord,
    VerdictReasonBasisRow,
    VerdictRow,
)

MATURITY = date(2027, 3, 15)
THRESHOLDS = PostmortemConfig()
RUN_A = UUID("11111111-1111-1111-1111-111111111111")


def _outcome(
    symbol: str,
    recommendation: str,
    forward_return_pct: float,
    classification: str,
    *,
    horizon_days: int = 5,
) -> VerdictOutcomeRecord:
    return VerdictOutcomeRecord(
        run_id=RUN_A,
        symbol=symbol,
        horizon_days=horizon_days,
        as_of=MATURITY,
        recommendation=recommendation,
        forward_return_pct=forward_return_pct,
        classification=classification,
    )


#: 5d: proceed +2.0 (HIT) / proceed -3.0 (MISS_SEVERE) / skip -1.0 (HIT)
#: 20d: proceed +1.0 (HIT) / skip +4.0 (MISS_SEVERE)
def _mixed_outcomes() -> tuple[VerdictOutcomeRecord, ...]:
    return (
        _outcome("AAA", "proceed", 2.0, "HIT"),
        _outcome("BBB", "proceed", -3.0, "MISS_SEVERE"),
        _outcome("CCC", "skip", -1.0, "HIT"),
        _outcome("AAA", "proceed", 1.0, "HIT", horizon_days=20),
        _outcome("CCC", "skip", 4.0, "MISS_SEVERE", horizon_days=20),
    )


def _by_id(rows):
    return {row.metric_id: row for row in rows}


class TestSeparation:
    def test_computes_the_proceed_minus_skip_mean_per_horizon(self) -> None:
        # 5d: mean(proceed) = (2.0 + -3.0)/2 = -0.5, mean(skip) = -1.0 -> +0.5
        # 20d: mean(proceed) = 1.0, mean(skip) = 4.0 -> -3.0
        rows = _by_id(compute_separation(_mixed_outcomes(), THRESHOLDS))

        assert rows["metric:separation:5d"].value == pytest.approx(0.5)
        assert rows["metric:separation:20d"].value == pytest.approx(-3.0)

    def test_composes_the_headline_with_the_postmortem_horizon_weights(self) -> None:
        # 0.6 * 0.5 + 0.4 * -3.0 = -0.9
        composed = _by_id(compute_separation(_mixed_outcomes(), THRESHOLDS))[
            "metric:separation:composed"
        ]

        assert composed.horizon_days is None
        assert composed.value == pytest.approx(-0.9)
        assert composed.sample_size == 5

    def test_renormalizes_the_weights_when_one_horizon_has_no_value(self) -> None:
        # Only 5d has both groups, so the composed headline is that horizon's
        # own value rather than a value shrunk toward zero by a missing 20d.
        outcomes = (
            _outcome("AAA", "proceed", 2.0, "HIT"),
            _outcome("CCC", "skip", -1.0, "HIT"),
            _outcome("AAA", "proceed", 1.0, "HIT", horizon_days=20),
        )

        rows = _by_id(compute_separation(outcomes, THRESHOLDS))

        assert rows["metric:separation:20d"].value is None
        assert rows["metric:separation:composed"].value == pytest.approx(3.0)

    def test_reports_no_value_when_one_side_of_the_comparison_is_empty(self) -> None:
        rows = _by_id(
            compute_separation((_outcome("AAA", "proceed", 2.0, "HIT"),), THRESHOLDS)
        )

        assert rows["metric:separation:5d"].value is None
        assert rows["metric:separation:5d"].sample_size == 1

    def test_reports_an_empty_window_as_no_value_rather_than_zero(self) -> None:
        rows = compute_separation((), THRESHOLDS)

        assert [(row.value, row.sample_size, row.is_preliminary) for row in rows] == [
            (None, 0, True),
            (None, 0, True),
            (None, 0, True),
        ]


class TestPreliminaryFlag:
    """The n<20 floor (`preliminary_sample_threshold`) and its boundary."""

    @pytest.mark.parametrize(
        ("sample_size", "expected"),
        [(19, True), (20, False), (21, False)],
    )
    def test_flags_only_below_the_configured_threshold(
        self, sample_size: int, expected: bool
    ) -> None:
        outcomes = tuple(
            _outcome(f"S{index}", "proceed", 1.0, "HIT") for index in range(sample_size)
        )

        rows = _by_id(compute_separation(outcomes, THRESHOLDS))

        assert rows["metric:separation:5d"].sample_size == sample_size
        assert rows["metric:separation:5d"].is_preliminary is expected

    def test_follows_a_lowered_threshold(self) -> None:
        thresholds = PostmortemConfig(preliminary_sample_threshold=2)
        outcomes = (_outcome("AAA", "proceed", 1.0, "HIT"),)

        rows = _by_id(compute_separation(outcomes, thresholds))

        assert rows["metric:separation:5d"].is_preliminary is True
        assert rows["metric:separation:20d"].sample_size == 0


class TestProceedSevereMissRate:
    def test_computes_the_rate_and_the_all_candidate_baseline(self) -> None:
        # 5d proceed rows = 2, of which 1 is MISS_SEVERE -> 0.5.
        # Baseline over all 3 rows at 5d: only -3.0 is <= -2.0% -> 1/3.
        rows = _by_id(compute_proceed_severe_miss_rate(_mixed_outcomes(), THRESHOLDS))

        assert rows["metric:proceed_severe_miss_rate:5d"].value == pytest.approx(0.5)
        assert rows["metric:proceed_severe_miss_rate:5d"].baseline_value == (
            pytest.approx(1 / 3)
        )
        assert rows["metric:proceed_severe_miss_rate:5d"].sample_size == 2

    def test_composes_the_headline_from_weighted_counts(self) -> None:
        # (0.6*1 + 0.4*0) / (0.6*2 + 0.4*1) = 0.6 / 1.6 = 0.375
        composed = _by_id(
            compute_proceed_severe_miss_rate(_mixed_outcomes(), THRESHOLDS)
        )["metric:proceed_severe_miss_rate:composed"]

        assert composed.value == pytest.approx(0.375)
        assert composed.sample_size == 3

    def test_flags_a_rate_above_the_watch_level(self) -> None:
        assert pytest.approx(0.15) == PROCEED_SEVERE_MISS_WATCH_RATE
        rows = _by_id(compute_proceed_severe_miss_rate(_mixed_outcomes(), THRESHOLDS))

        assert rows["metric:proceed_severe_miss_rate:5d"].is_flagged is True

    def test_flags_a_rate_below_the_watch_level_that_is_worse_than_baseline(
        self,
    ) -> None:
        # 10 proceed rows with 1 severe miss (0.10, under the 0.15 watch
        # level) against a baseline of 1/11: the filter is doing worse than
        # the candidate pool it filters, which is a flag on its own.
        outcomes = (
            *(_outcome(f"P{index}", "proceed", 1.0, "HIT") for index in range(9)),
            _outcome("P9", "proceed", -3.0, "MISS_SEVERE"),
            _outcome("S0", "skip", 1.0, "MISS_MILD"),
        )

        row = _by_id(compute_proceed_severe_miss_rate(outcomes, THRESHOLDS))[
            "metric:proceed_severe_miss_rate:5d"
        ]

        assert row.value == pytest.approx(0.1)
        assert row.baseline_value == pytest.approx(1 / 11)
        assert row.is_flagged is True

    def test_does_not_flag_a_rate_at_or_below_both_bars(self) -> None:
        outcomes = tuple(
            _outcome(f"P{index}", "proceed", 1.0, "HIT") for index in range(10)
        )

        row = _by_id(compute_proceed_severe_miss_rate(outcomes, THRESHOLDS))[
            "metric:proceed_severe_miss_rate:5d"
        ]

        assert row.value == pytest.approx(0.0)
        assert row.is_flagged is False

    def test_reports_an_empty_window_as_no_value_and_no_flag(self) -> None:
        rows = compute_proceed_severe_miss_rate((), THRESHOLDS)

        assert [(row.value, row.baseline_value, row.is_flagged) for row in rows] == [
            (None, None, False),
            (None, None, False),
            (None, None, False),
        ]


class TestSkipHitRate:
    def test_computes_the_rate_over_non_neutral_skips(self) -> None:
        # 5d skip rows = [-1.0] -> non-neutral 1, HIT 1 -> 1.0.
        # Baseline over 5d rows whose |return| >= 0.5: 3 rows, 2 declines.
        rows = _by_id(compute_skip_hit_rate(_mixed_outcomes(), THRESHOLDS))

        assert rows["metric:skip_hit_rate:5d"].value == pytest.approx(1.0)
        assert rows["metric:skip_hit_rate:5d"].baseline_value == pytest.approx(2 / 3)
        assert rows["metric:skip_hit_rate:5d"].is_flagged is False

    def test_excludes_neutral_skips_from_the_denominator(self) -> None:
        outcomes = (
            _outcome("AAA", "skip", -1.0, "HIT"),
            _outcome("BBB", "skip", 0.2, "NEUTRAL"),
            _outcome("CCC", "skip", 1.0, "MISS_MILD"),
        )

        row = _by_id(compute_skip_hit_rate(outcomes, THRESHOLDS))[
            "metric:skip_hit_rate:5d"
        ]

        assert row.value == pytest.approx(0.5)
        assert row.sample_size == 2

    def test_flags_a_skip_hit_rate_below_the_period_baseline(self) -> None:
        # skip hit rate 1/2 = 0.5 against a decline rate of 3/4 among the
        # window's non-noise moves: skipping selected worse than the pool.
        outcomes = (
            _outcome("AAA", "skip", -1.0, "HIT"),
            _outcome("BBB", "skip", 1.0, "MISS_MILD"),
            _outcome("CCC", "proceed", -1.0, "MISS_MILD"),
            _outcome("DDD", "proceed", -1.0, "MISS_MILD"),
        )

        row = _by_id(compute_skip_hit_rate(outcomes, THRESHOLDS))[
            "metric:skip_hit_rate:5d"
        ]

        assert row.value == pytest.approx(0.5)
        assert row.baseline_value == pytest.approx(0.75)
        assert row.is_flagged is True

    def test_composes_the_headline_from_weighted_counts(self) -> None:
        # 5d: 1 hit / 1 non-neutral. 20d: 0 hits / 1 non-neutral.
        # (0.6*1 + 0.4*0) / (0.6*1 + 0.4*1) = 0.6
        composed = _by_id(compute_skip_hit_rate(_mixed_outcomes(), THRESHOLDS))[
            "metric:skip_hit_rate:composed"
        ]

        assert composed.value == pytest.approx(0.6)

    def test_reports_an_empty_window_as_no_value(self) -> None:
        rows = compute_skip_hit_rate((), THRESHOLDS)

        assert [(row.value, row.sample_size) for row in rows] == [
            (None, 0),
            (None, 0),
            (None, 0),
        ]


class TestHumanAlignment:
    def _decision(
        self,
        decision: str,
        recommendation: str,
        forward_return_pct: float,
        *,
        classification: str = "HIT",
        horizon_days: int = 5,
    ) -> VerdictDecisionRow:
        return VerdictDecisionRow(
            run_id=RUN_A,
            symbol="AAA",
            strategy_key="default",
            decision=decision,
            recommendation=recommendation,
            horizon_days=horizon_days,
            forward_return_pct=forward_return_pct,
            classification=classification,
        )

    def test_cross_tabs_decision_by_recommendation_by_horizon(self) -> None:
        rows = compute_human_alignment(
            (
                self._decision("followed", "proceed", 2.0),
                self._decision("followed", "proceed", 4.0),
                self._decision("ignored", "skip", -3.0),
                self._decision("followed", "proceed", 1.0, horizon_days=20),
            )
        )

        assert [
            (
                row.decision,
                row.recommendation,
                row.horizon_days,
                row.count,
                row.mean_forward_return_pct,
            )
            for row in rows
        ] == [
            ("followed", "proceed", 5, 2, 3.0),
            ("followed", "proceed", 20, 1, 1.0),
            ("ignored", "skip", 5, 1, -3.0),
        ]

    def test_counts_hits_and_severe_misses_inside_each_cell(self) -> None:
        rows = compute_human_alignment(
            (
                self._decision("modified", "proceed", 2.0),
                self._decision(
                    "modified", "proceed", -3.0, classification="MISS_SEVERE"
                ),
                self._decision("modified", "proceed", -1.0, classification="MISS_MILD"),
            )
        )

        assert [(row.count, row.hit_count, row.severe_miss_count) for row in rows] == [
            (3, 1, 1)
        ]
        assert rows[0].cell_id == "metric:human_alignment:modified:proceed:5d"

    def test_reports_an_empty_journal_as_no_cells(self) -> None:
        assert compute_human_alignment(()) == ()


class TestSourceContribution:
    def _citation(
        self, source_id: str, source_type: str, *, symbol: str = "AAA"
    ) -> VerdictCitationRow:
        return VerdictCitationRow(
            run_id=RUN_A,
            symbol=symbol,
            source_id=source_id,
            source_type=source_type,
            source_url=None,
        )

    def test_groups_citations_by_source_type_and_provider(self) -> None:
        citations = (
            self._citation("finnhub:1", "news"),
            self._citation("finnhub:2", "news"),
            self._citation("edgar:1", "filing"),
        )
        outcomes = (_outcome("AAA", "proceed", 2.0, "HIT"),)

        rows = compute_source_contribution(citations, outcomes)

        assert [
            (row.source_type, row.provider, row.citation_count) for row in rows
        ] == [("filing", "edgar", 1), ("news", "finnhub", 2)]
        assert rows[0].contribution_id == "metric:source_contribution:filing:edgar"

    def test_splits_each_citation_across_the_horizons_that_matured(self) -> None:
        # One citation, two horizons: HIT at 5d and MISS_SEVERE at 20d, so
        # the provider's hit ratio over non-neutral outcomes is 1/2.
        citations = (self._citation("finnhub:1", "news"),)
        outcomes = (
            _outcome("AAA", "proceed", 2.0, "HIT"),
            _outcome("AAA", "proceed", -3.0, "MISS_SEVERE", horizon_days=20),
        )

        rows = compute_source_contribution(citations, outcomes)

        assert (rows[0].hit_citation_count, rows[0].miss_citation_count) == (1, 1)
        assert rows[0].hit_citation_ratio == pytest.approx(0.5)

    def test_excludes_neutral_outcomes_from_the_ratio(self) -> None:
        citations = (self._citation("finnhub:1", "news"),)
        outcomes = (
            _outcome("AAA", "skip", 0.1, "NEUTRAL"),
            _outcome("AAA", "skip", -1.0, "HIT", horizon_days=20),
        )

        rows = compute_source_contribution(citations, outcomes)

        assert rows[0].neutral_citation_count == 1
        assert rows[0].hit_citation_ratio == pytest.approx(1.0)

    def test_keeps_a_source_whose_symbol_has_no_matured_outcome(self) -> None:
        citations = (self._citation("finnhub:1", "news", symbol="ZZZ"),)

        rows = compute_source_contribution(citations, ())

        assert (rows[0].citation_count, rows[0].hit_citation_ratio) == (1, None)

    def test_counts_the_same_source_cited_by_two_symbols_twice(self) -> None:
        citations = (
            self._citation("finnhub:1", "news", symbol="AAA"),
            self._citation("finnhub:1", "news", symbol="BBB"),
        )
        outcomes = (
            _outcome("AAA", "proceed", 2.0, "HIT"),
            _outcome("BBB", "proceed", -3.0, "MISS_SEVERE"),
        )

        rows = compute_source_contribution(citations, outcomes)

        assert rows[0].citation_count == 2
        assert rows[0].hit_citation_ratio == pytest.approx(0.5)

    def test_scopes_outcomes_to_their_own_run(self) -> None:
        other_run = uuid4()
        citations = (self._citation("finnhub:1", "news"),)
        outcomes = (
            replace(_outcome("AAA", "proceed", -3.0, "MISS_SEVERE"), run_id=other_run),
        )

        rows = compute_source_contribution(citations, outcomes)

        assert rows[0].miss_citation_count == 0

    def test_reports_an_empty_window_as_no_rows(self) -> None:
        assert compute_source_contribution((), ()) == ()


def _verdict(symbol: str, recommendation: str, *, run_id: UUID = RUN_A) -> VerdictRow:
    return VerdictRow(
        run_id=run_id, symbol=symbol, as_of=MATURITY, recommendation=recommendation
    )


class TestVerdictMix:
    """P8-120: whether proceed itself has gone structurally silent."""

    def test_metric_id_is_the_literal_verdict_mix(self) -> None:
        assert compute_verdict_mix(()).metric_id == "verdict_mix"

    def test_empty_window_reports_no_ratio_and_no_flag(self) -> None:
        summary = compute_verdict_mix(())

        assert summary.verdict_count == 0
        assert summary.proceed_ratio is None
        assert summary.is_flagged is False

    def test_nineteen_verdicts_all_skip_does_not_flag(self) -> None:
        verdicts = tuple(_verdict(f"S{i}", "skip") for i in range(19))

        summary = compute_verdict_mix(verdicts)

        assert summary.verdict_count == 19
        assert summary.proceed_count == 0
        assert summary.is_flagged is False

    def test_twenty_verdicts_all_skip_flags_at_the_inclusive_threshold(self) -> None:
        verdicts = tuple(_verdict(f"S{i}", "skip") for i in range(20))

        summary = compute_verdict_mix(verdicts)

        assert summary.verdict_count == 20
        assert summary.proceed_count == 0
        assert summary.proceed_ratio == pytest.approx(0.0)
        assert summary.is_flagged is True

    def test_twenty_verdicts_with_one_proceed_does_not_flag(self) -> None:
        verdicts = (
            *(_verdict(f"S{i}", "skip") for i in range(19)),
            _verdict("P1", "proceed"),
        )

        summary = compute_verdict_mix(verdicts)

        assert summary.verdict_count == 20
        assert summary.proceed_count == 1
        assert summary.is_flagged is False

    def test_all_proceed_reports_full_ratio_and_no_flag(self) -> None:
        verdicts = tuple(_verdict(f"P{i}", "proceed") for i in range(25))

        summary = compute_verdict_mix(verdicts)

        assert summary.proceed_ratio == pytest.approx(1.0)
        assert summary.skip_count == 0
        assert summary.is_flagged is False

    def test_run_count_is_distinct_run_ids_not_verdict_count(self) -> None:
        verdicts = tuple(_verdict(f"S{i}", "skip", run_id=RUN_A) for i in range(10))

        summary = compute_verdict_mix(verdicts)

        assert summary.run_count == 1
        assert summary.verdict_count == 10

    def test_run_count_counts_every_distinct_run(self) -> None:
        other_run = uuid4()
        verdicts = (
            *(_verdict(f"A{i}", "skip", run_id=RUN_A) for i in range(3)),
            *(_verdict(f"B{i}", "skip", run_id=other_run) for i in range(2)),
        )

        summary = compute_verdict_mix(verdicts)

        assert summary.run_count == 2
        assert summary.verdict_count == 5


def _measured(
    symbol: str,
    recommendation: str,
    mentions: int,
    level: str,
    *,
    run_id: UUID = RUN_A,
) -> VerdictRow:
    """A verdict row carrying Issue #130's archived supply measurement."""
    return replace(
        _verdict(symbol, recommendation, run_id=run_id),
        news_supply=NewsSupplyRecord(
            collected_items=20,
            exported_items=15,
            symbol_mention_items=mentions,
            level=level,
        ),
    )


class TestNewsSupplyMix:
    """Issue #154: is the `sufficient` threshold borne out by the verdicts?"""

    def test_reports_the_threshold_the_levels_were_graded_at(self) -> None:
        # Copied into the document so a proposal to move the boundary can cite
        # the value it is changing, instead of the reader guessing it.
        summary = compute_news_supply_mix(())

        assert summary.sufficient_threshold == DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS
        assert summary.metric_id == "metric:news_supply"

    def test_empty_window_reports_no_cells(self) -> None:
        summary = compute_news_supply_mix(())

        assert summary.cells == ()
        assert (summary.verdict_count, summary.recorded_verdict_count) == (0, 0)

    def test_crosses_each_level_against_what_the_verdict_said(self) -> None:
        # Two sparse candidates were still waved through, one was not: exactly
        # the count the issue asks for.
        verdicts = (
            _measured("A", "proceed", 4, "sparse"),
            _measured("B", "proceed", 3, "sparse"),
            _measured("C", "skip", 1, "sparse"),
            _measured("D", "proceed", 9, "sufficient"),
        )

        summary = compute_news_supply_mix(verdicts)

        assert [
            (cell.level, cell.recommendation, cell.verdict_count)
            for cell in summary.cells
        ] == [
            ("sparse", "proceed", 2),
            ("sparse", "skip", 1),
            ("sufficient", "proceed", 1),
        ]

    def test_cell_ids_name_the_level_and_the_recommendation(self) -> None:
        summary = compute_news_supply_mix((_measured("A", "proceed", 0, "none"),))

        assert summary.cells[0].cell_id == "metric:news_supply:none:proceed"

    def test_summarizes_the_mention_counts_the_threshold_is_judged_on(self) -> None:
        # 4, 2, 3 -> min 2, max 4, mean 3.0.
        verdicts = (
            _measured("A", "proceed", 4, "sparse"),
            _measured("B", "proceed", 2, "sparse"),
            _measured("C", "proceed", 3, "sparse"),
        )

        cell = compute_news_supply_mix(verdicts).cells[0]

        assert (cell.min_symbol_mention_items, cell.max_symbol_mention_items) == (2, 4)
        assert cell.mean_symbol_mention_items == pytest.approx(3.0)

    def test_unmeasured_verdicts_form_their_own_level_not_none(self) -> None:
        # A row collected from a pre-#130 archive never saw the threshold, so
        # folding it into `none` would let it argue about a grade it never got.
        verdicts = (
            _verdict("A", "proceed"),
            _measured("B", "proceed", 0, "none"),
        )

        summary = compute_news_supply_mix(verdicts)

        assert [(cell.level, cell.verdict_count) for cell in summary.cells] == [
            ("none", 1),
            ("unrecorded", 1),
        ]
        assert (summary.recorded_verdict_count, summary.unrecorded_verdict_count) == (
            1,
            1,
        )

    def test_the_unrecorded_cell_states_no_mention_statistics(self) -> None:
        cell = compute_news_supply_mix((_verdict("A", "proceed"),)).cells[0]

        assert cell.min_symbol_mention_items is None
        assert cell.max_symbol_mention_items is None
        assert cell.mean_symbol_mention_items is None

    def test_counts_the_same_symbol_from_two_runs_separately(self) -> None:
        other_run = uuid4()
        verdicts = (
            _measured("A", "proceed", 4, "sparse"),
            _measured("A", "proceed", 4, "sparse", run_id=other_run),
        )

        assert compute_news_supply_mix(verdicts).cells[0].verdict_count == 2


class TestBasisContribution:
    """Issue #191: hit rate per evidence kind, not merely per provider."""

    def _basis(
        self, basis: str | None, *, symbol: str = "AAA"
    ) -> VerdictReasonBasisRow:
        return VerdictReasonBasisRow(run_id=RUN_A, symbol=symbol, basis=basis)

    def test_it_separates_reasoning_kinds_a_provider_tally_cannot(self) -> None:
        """Both bases may cite the same provider, so provider cannot split them."""
        bases = (
            self._basis("filing_fundamental"),
            self._basis("technical_score", symbol="BBB"),
        )
        outcomes = (
            _outcome("AAA", "proceed", -6.0, "MISS_SEVERE"),
            _outcome("BBB", "proceed", 2.0, "HIT"),
        )

        rows = compute_basis_contribution(bases, outcomes)

        assert [(row.basis, row.hit_citation_ratio) for row in rows] == [
            ("filing_fundamental", 0.0),
            ("technical_score", 1.0),
        ]
        assert rows[0].basis_id == "metric:basis_contribution:filing_fundamental"

    def test_splits_each_basis_across_the_horizons_that_matured(self) -> None:
        bases = (self._basis("news_catalyst"),)
        outcomes = (
            _outcome("AAA", "proceed", 2.0, "HIT"),
            _outcome("AAA", "proceed", -3.0, "MISS_SEVERE", horizon_days=20),
        )

        rows = compute_basis_contribution(bases, outcomes)

        assert (rows[0].hit_count, rows[0].miss_count) == (1, 1)
        assert rows[0].hit_citation_ratio == pytest.approx(0.5)

    def test_excludes_neutral_outcomes_from_the_ratio(self) -> None:
        bases = (self._basis("market_regime"),)
        outcomes = (
            _outcome("AAA", "skip", 0.1, "NEUTRAL"),
            _outcome("AAA", "skip", -1.0, "HIT", horizon_days=20),
        )

        rows = compute_basis_contribution(bases, outcomes)

        assert rows[0].neutral_count == 1
        assert rows[0].hit_citation_ratio == pytest.approx(1.0)

    def test_keeps_a_basis_whose_verdict_has_no_matured_outcome(self) -> None:
        """A basis used but never measurable is itself worth seeing."""
        rows = compute_basis_contribution(
            (self._basis("risk_sizing", symbol="ZZZ"),), ()
        )

        assert (rows[0].verdict_count, rows[0].hit_citation_ratio) == (1, None)

    def test_untagged_reasons_are_bucketed_rather_than_dropped(self) -> None:
        """How much of the window is untagged is what qualifies the other rows."""
        bases = (self._basis(None), self._basis("peer_relative", symbol="BBB"))
        outcomes = (
            _outcome("AAA", "proceed", 2.0, "HIT"),
            _outcome("BBB", "proceed", 2.0, "HIT"),
        )

        rows = compute_basis_contribution(bases, outcomes)

        assert [row.basis for row in rows] == ["peer_relative", UNTAGGED_VERDICT_BASIS]
        assert rows[1].verdict_count == 1

    def test_an_empty_window_yields_no_rows_rather_than_zero_filled_ones(self) -> None:
        assert compute_basis_contribution((), ()) == ()


DAY_A = date(2027, 3, 10)
DAY_B = date(2027, 3, 11)


def _dated(  # noqa: PLR0913 - one outcome row's own columns
    symbol: str,
    recommendation: str,
    forward_return_pct: float,
    maturity: date,
    *,
    benchmark_return_pct: float | None = None,
    horizon_days: int = 5,
) -> VerdictOutcomeRecord:
    return VerdictOutcomeRecord(
        run_id=RUN_A,
        symbol=symbol,
        horizon_days=horizon_days,
        as_of=maturity,
        recommendation=recommendation,
        forward_return_pct=forward_return_pct,
        benchmark_return_pct=benchmark_return_pct,
        classification="HIT",
    )


class TestPairedSeparation:
    """Issue #190: difference inside each run day, then average the days."""

    def test_each_run_days_gap_is_averaged_rather_than_the_pooled_means(self) -> None:
        # Day A: proceed +10, skip +8 -> +2. Day B: proceed -4, skip -5 -> +1.
        # Mean of the daily gaps is +1.5. The pooled version would answer
        # (10 - 4)/2 - (8 - 5)/2 = +1.5 here only because both days are
        # balanced; the next test breaks that.
        rows = (
            _dated("A", "proceed", 10.0, DAY_A),
            _dated("B", "skip", 8.0, DAY_A),
            _dated("C", "proceed", -4.0, DAY_B),
            _dated("D", "skip", -5.0, DAY_B),
        )

        summary = _five_day(compute_separation_paired(rows, THRESHOLDS))

        assert summary.value == pytest.approx(1.5)
        assert summary.sample_size == 4
        assert summary.excluded_day_count == 0

    def test_a_strong_day_full_of_proceeds_no_longer_inflates_the_gap(self) -> None:
        # Day A (a strong day): proceed +10, skip +9 -> +1.
        # Day B (a weak day): proceed -10, skip -11 -> +1.
        # Paired: +1. Pooled: mean(proceed +10, +10, -10) - mean(skip +9, -11)
        # = 3.333 - (-1.0) = +4.333, which is mostly the day effect.
        rows = (
            _dated("A", "proceed", 10.0, DAY_A),
            _dated("B", "proceed", 10.0, DAY_A),
            _dated("C", "skip", 9.0, DAY_A),
            _dated("D", "proceed", -10.0, DAY_B),
            _dated("E", "skip", -11.0, DAY_B),
        )

        assert _five_day(compute_separation_paired(rows, THRESHOLDS)).value == (
            pytest.approx(1.0)
        )
        assert _five_day(compute_separation(rows, THRESHOLDS)).value == pytest.approx(
            10.0 / 3 + 1.0
        )

    def test_a_day_with_only_one_side_is_excluded_and_counted(self) -> None:
        rows = (
            _dated("A", "proceed", 10.0, DAY_A),
            _dated("B", "skip", 8.0, DAY_A),
            _dated("C", "proceed", 99.0, DAY_B),
        )

        summary = _five_day(compute_separation_paired(rows, THRESHOLDS))

        assert summary.value == pytest.approx(2.0)
        assert summary.excluded_day_count == 1

    def test_a_window_with_no_two_sided_day_states_no_value(self) -> None:
        rows = (_dated("A", "proceed", 10.0, DAY_A),)

        summary = _five_day(compute_separation_paired(rows, THRESHOLDS))

        assert summary.value is None
        assert summary.stderr is None
        assert (summary.ci_low, summary.ci_high) == (None, None)


class TestPairedSeparationExcess:
    def test_the_benchmarks_own_move_is_removed_from_both_sides(self) -> None:
        # Day A: proceed +10 vs benchmark +6 -> +4; skip +8 vs +6 -> +2.
        # Gap +2, identical to the raw pairing (a common benchmark cancels),
        # which is exactly why disagreement between the two is informative.
        rows = (
            _dated("A", "proceed", 10.0, DAY_A, benchmark_return_pct=6.0),
            _dated("B", "skip", 8.0, DAY_A, benchmark_return_pct=6.0),
        )

        assert _five_day(compute_separation_paired_excess(rows, THRESHOLDS)).value == (
            pytest.approx(2.0)
        )

    def test_a_row_without_a_measured_benchmark_contributes_nothing(self) -> None:
        rows = (
            _dated("A", "proceed", 10.0, DAY_A, benchmark_return_pct=6.0),
            _dated("B", "skip", 8.0, DAY_A, benchmark_return_pct=6.0),
            _dated("C", "proceed", 99.0, DAY_B),
            _dated("D", "skip", -99.0, DAY_B),
        )

        summary = _five_day(compute_separation_paired_excess(rows, THRESHOLDS))

        assert summary.value == pytest.approx(2.0)
        assert summary.sample_size == 2
        # Day B lost both its rows, so it is a day that stated no difference.
        assert summary.excluded_day_count == 1

    def test_an_archive_with_no_benchmark_column_is_not_measurable(self) -> None:
        rows = (
            _dated("A", "proceed", 10.0, DAY_A),
            _dated("B", "skip", 8.0, DAY_A),
        )

        assert _five_day(compute_separation_paired_excess(rows, THRESHOLDS)).value is (
            None
        )


class TestDispersion:
    def test_pooled_separation_carries_a_welch_interval(self) -> None:
        # proceed {2, 4}: mean 3, variance 2. skip {0, 2}: mean 1, variance 2.
        # diff +2; stderr sqrt(2/2 + 2/2) = sqrt(2).
        rows = (
            _dated("A", "proceed", 2.0, DAY_A),
            _dated("B", "proceed", 4.0, DAY_A),
            _dated("C", "skip", 0.0, DAY_A),
            _dated("D", "skip", 2.0, DAY_A),
        )

        summary = _five_day(compute_separation(rows, THRESHOLDS))

        assert summary.value == pytest.approx(2.0)
        assert summary.stderr == pytest.approx(math.sqrt(2.0))
        assert summary.ci_low == pytest.approx(2.0 - 1.959963984540054 * math.sqrt(2))
        assert summary.ci_high == pytest.approx(2.0 + 1.959963984540054 * math.sqrt(2))

    def test_a_single_observation_per_side_has_no_defined_spread(self) -> None:
        rows = (
            _dated("A", "proceed", 2.0, DAY_A),
            _dated("C", "skip", 0.0, DAY_A),
        )

        summary = _five_day(compute_separation(rows, THRESHOLDS))

        assert summary.value == pytest.approx(2.0)
        assert summary.stderr is None
        assert (summary.ci_low, summary.ci_high) == (None, None)

    def test_the_weight_composed_headline_publishes_no_interval(self) -> None:
        # The two horizons measure the same runs, so an interval across them
        # would claim independence the data does not have.
        composed = next(
            row
            for row in compute_separation(_mixed_outcomes(), THRESHOLDS)
            if row.horizon_days is None
        )

        assert composed.value is not None
        assert (composed.stderr, composed.ci_low, composed.ci_high) == (
            None,
            None,
            None,
        )

    def test_a_rate_carries_a_wilson_interval_that_stays_inside_zero_and_one(
        self,
    ) -> None:
        # 0 severe misses out of 3 proceeds: a Wald interval would collapse to
        # the point [0, 0]; Wilson keeps a real upper bound.
        rows = tuple(
            _dated(symbol, "proceed", 1.0, DAY_A) for symbol in ("A", "B", "C")
        )

        summary = _five_day(compute_proceed_severe_miss_rate(rows, THRESHOLDS))

        assert summary.value == 0.0
        assert summary.ci_low == pytest.approx(0.0, abs=1e-12)
        assert 0.0 < summary.ci_high < 1.0

    def test_a_rates_composed_headline_publishes_no_interval(self) -> None:
        composed = next(
            row
            for row in compute_proceed_severe_miss_rate(_mixed_outcomes(), THRESHOLDS)
            if row.horizon_days is None
        )

        assert (composed.ci_low, composed.ci_high) == (None, None)

    def test_wilson_needs_at_least_one_trial(self) -> None:
        assert wilson_interval(0, 0) == (None, None)


def _five_day(summaries):
    return next(row for row in summaries if row.horizon_days == 5)


ENTRY_DATE = date(2027, 3, 1)


def _tracked(  # noqa: PLR0913 - one shadow position's own columns
    symbol: str,
    recommendation: str,
    *,
    exit_price: float | None = None,
    exit_reason: str | None = "stop",
    days_held: int = 4,
    entry_price: float = 100.0,
) -> VerdictPosition:
    """Build one shadow position, closed unless `exit_price` is omitted."""
    is_closed = exit_price is not None
    return VerdictPosition(
        run_id=RUN_A,
        symbol=symbol,
        strategy_key="default",
        recommendation=recommendation,
        no_trade=False,
        entry_date=ENTRY_DATE,
        entry_price=entry_price,
        stop_price=95.0,
        days_held=days_held,
        status="closed" if is_closed else "open",
        exit_date=date(2027, 3, 8) if is_closed else None,
        exit_price=exit_price,
        exit_reason=exit_reason if is_closed else None,
        realized_return_pct=(
            None
            if exit_price is None
            else (exit_price - entry_price) / entry_price * 100
        ),
        last_marked_date=ENTRY_DATE,
    )


def _entry_marks(
    *positions: VerdictPosition, stop_price: float | None = 90.0
) -> dict[tuple[UUID, str], VerdictPositionMark]:
    return {
        (position.run_id, position.symbol): VerdictPositionMark(
            run_id=position.run_id,
            symbol=position.symbol,
            as_of_date=ENTRY_DATE,
            close=position.entry_price,
            stop_price=stop_price,
            unrealized_return_pct=0.0,
        )
        for position in positions
    }


class TestTrackedPerformance:
    """Issue #190: proceed vs skip under identical exit rules, plus the pool."""

    def test_reports_the_two_sides_and_the_pooled_arm_in_a_fixed_order(self) -> None:
        rows = compute_tracked_performance((), {})

        assert [row.recommendation for row in rows] == ["proceed", "skip", "all"]
        assert [row.metric_id for row in rows] == [
            "metric:tracked_performance:proceed",
            "metric:tracked_performance:skip",
            "metric:tracked_performance:all",
        ]

    def test_an_empty_stratum_states_no_rate_rather_than_zero(self) -> None:
        rows = {row.recommendation: row for row in compute_tracked_performance((), {})}

        assert rows["skip"].closed_count == 0
        assert rows["skip"].win_rate is None
        assert rows["skip"].profit_factor is None
        assert rows["skip"].expectancy_pct is None
        assert rows["skip"].avg_holding_days is None

    def test_each_side_is_measured_only_against_its_own_positions(self) -> None:
        # proceed: +10% and -5% -> win rate 1/2, expectancy +2.5%,
        # profit factor 10/5 = 2.0. skip: -20% alone -> win rate 0.
        positions = (
            _tracked("A", "proceed", exit_price=110.0),
            _tracked("B", "proceed", exit_price=95.0),
            _tracked("C", "skip", exit_price=80.0),
        )
        rows = {
            row.recommendation: row
            for row in compute_tracked_performance(positions, _entry_marks(*positions))
        }

        assert rows["proceed"].win_rate == pytest.approx(0.5)
        assert rows["proceed"].expectancy_pct == pytest.approx(2.5)
        assert rows["proceed"].profit_factor == pytest.approx(2.0)
        assert rows["skip"].win_rate == 0.0
        assert rows["skip"].expectancy_pct == pytest.approx(-20.0)
        # The pooled arm is every screened candidate: (10 - 5 - 20) / 3.
        assert rows["all"].expectancy_pct == pytest.approx(-5.0)

    def test_profit_and_loss_is_scale_free_across_differently_priced_symbols(
        self,
    ) -> None:
        # A $400 stock and a $20 stock each gaining 10% must contribute
        # equally; a share-count-based P&L would let the expensive one
        # dominate purely because of its price.
        positions = (
            _tracked("RICH", "proceed", entry_price=400.0, exit_price=440.0),
            _tracked("CHEAP", "proceed", entry_price=20.0, exit_price=22.0),
        )
        rows = {
            row.recommendation: row
            for row in compute_tracked_performance(positions, _entry_marks(*positions))
        }

        assert rows["proceed"].expectancy_pct == pytest.approx(10.0)

    def test_the_r_multiple_reads_the_entry_session_stop_not_the_trailed_one(
        self,
    ) -> None:
        # Entry 100, stop *at entry* 90 (from the mark), exit 110: R = +1.0.
        # The position row's own stop_price has since ratcheted to 95, which
        # would report +2.0 and overstate the edge.
        position = _tracked("A", "proceed", exit_price=110.0)
        rows = {
            row.recommendation: row
            for row in compute_tracked_performance((position,), _entry_marks(position))
        }

        assert rows["proceed"].avg_r_multiple == pytest.approx(1.0)

    def test_a_position_without_any_mark_contributes_no_r_multiple(self) -> None:
        position = _tracked("A", "proceed", exit_price=110.0)

        rows = {
            row.recommendation: row
            for row in compute_tracked_performance((position,), {})
        }

        assert rows["proceed"].closed_count == 1
        assert rows["proceed"].avg_r_multiple is None

    def test_open_positions_are_counted_but_never_rated(self) -> None:
        positions = (
            _tracked("A", "proceed", exit_price=110.0),
            _tracked("B", "proceed"),
        )
        rows = {
            row.recommendation: row
            for row in compute_tracked_performance(positions, _entry_marks(*positions))
        }

        assert (rows["proceed"].closed_count, rows["proceed"].open_count) == (1, 1)
        assert rows["proceed"].win_rate == 1.0

    def test_exit_reasons_are_zero_filled_from_the_trackers_own_vocabulary(
        self,
    ) -> None:
        position = _tracked("A", "proceed", exit_price=110.0, exit_reason="manual")
        rows = {
            row.recommendation: row
            for row in compute_tracked_performance((position,), _entry_marks(position))
        }

        assert [
            (cell.reason, cell.count) for cell in rows["proceed"].exit_reason_counts
        ] == [("manual", 1), ("max_hold", 0), ("stop", 0)]

    def test_reports_the_median_holding_period_in_sessions(self) -> None:
        positions = (
            _tracked("A", "proceed", exit_price=110.0, days_held=2),
            _tracked("B", "proceed", exit_price=110.0, days_held=4),
            _tracked("C", "proceed", exit_price=110.0, days_held=12),
        )
        rows = {
            row.recommendation: row
            for row in compute_tracked_performance(positions, _entry_marks(*positions))
        }

        assert rows["proceed"].avg_holding_days == pytest.approx(4.0)
