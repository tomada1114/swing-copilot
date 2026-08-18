"""Tests for the filing-derived earnings calendar (Issue #201).

The contract under test has two halves. The first is *honesty*: a projected
reporting date is an estimate, so the calendar must say "found" only for a
forward-looking projection inside the lookahead window, and must say "I do not
know" — never a fabricated date — whenever the collected filing history cannot
support one. The second is *point-in-time discipline*: what a simulated day
may see is decided by `filed_at <= as_of` alone, tested immediately before, at,
and immediately after the cutoff.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from swing_copilot.backtest.earnings_history import (
    DERIVED_SESSION,
    EARNINGS_FILING_FORMS,
    DerivedEarningsCalendar,
    load_derived_earnings_calendar,
)
from swing_copilot.backtest.engine import BacktestEngine
from swing_copilot.backtest.metrics import ENTRY_BLOCK_EARNINGS
from swing_copilot.backtest.policy import (
    EntryPolicy,
    EntryPolicyArm,
    EntryPolicyRequest,
    build_entry_policy,
)
from swing_copilot.screening.base import Candidate
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import FundamentalsRecord, MarketStore
from swing_copilot.universe import UniverseMember
from tests.backtest.conftest import bar_row, bars_frame, flat_bars

if TYPE_CHECKING:
    from swing_copilot.config import Settings

_LOOKAHEAD_DAYS = 45
_QUARTER_DAYS = 91
_AS_OF = date(2027, 4, 30)


def _calendar(
    dates: tuple[date, ...], *, symbol: str = "AAA"
) -> DerivedEarningsCalendar:
    return DerivedEarningsCalendar({symbol: dates}, _LOOKAHEAD_DAYS)


def _quarterly_through(last: date, *, count: int = 3) -> tuple[date, ...]:
    """`count` filings on a clean quarterly cadence, ending on `last`."""
    return tuple(
        last - timedelta(days=_QUARTER_DAYS * offset)
        for offset in reversed(range(count))
    )


class TestVisibilityCutoff:
    """`filed_at <= as_of`, tested immediately before / at / immediately after."""

    #: Quarterly filings, the last of them on the day under test, so the
    #: visible slice — and therefore the projection — changes by one filing
    #: as the cutoff moves across it.
    _FILINGS = _quarterly_through(_AS_OF)

    def test_filing_from_the_day_after_as_of_is_not_visible(self):
        lookup = (
            _calendar(self._FILINGS)
            .lookup(_AS_OF - timedelta(days=1), ["AAA"])
            .lookups_by_symbol["AAA"]
            .recent_event
        )

        assert lookup is not None
        assert lookup.earnings_date == self._FILINGS[-2]

    def test_filing_dated_exactly_as_of_is_visible(self):
        recent = (
            _calendar(self._FILINGS)
            .lookup(_AS_OF, ["AAA"])
            .lookups_by_symbol["AAA"]
            .recent_event
        )

        assert recent is not None
        assert recent.earnings_date == _AS_OF

    def test_filing_from_the_day_before_as_of_is_visible(self):
        recent = (
            _calendar(self._FILINGS)
            .lookup(_AS_OF + timedelta(days=1), ["AAA"])
            .lookups_by_symbol["AAA"]
            .recent_event
        )

        assert recent is not None
        assert recent.earnings_date == _AS_OF

    def test_the_projection_itself_never_reads_past_the_cutoff(self):
        # The day before the last filing lands, the cadence says a report is
        # due today; the moment it lands, the same calendar object looks a
        # quarter ahead instead. A projection that leaked the future would
        # answer identically on both days.
        calendar = _calendar(self._FILINGS)

        before = calendar.lookup(_AS_OF - timedelta(days=1), ["AAA"]).lookups_by_symbol[
            "AAA"
        ]
        at = calendar.lookup(_AS_OF, ["AAA"]).lookups_by_symbol["AAA"]

        assert before.status == "found"
        assert before.event is not None
        assert before.event.earnings_date == _AS_OF
        assert at.status == "none_in_window"
        assert at.event is None


class TestProjection:
    def test_next_report_is_projected_from_the_median_visible_gap(self):
        lookup = (
            _calendar(_quarterly_through(_AS_OF - timedelta(days=60)))
            .lookup(_AS_OF, ["AAA"])
            .lookups_by_symbol["AAA"]
        )

        assert lookup.status == "found"
        assert lookup.event is not None
        assert lookup.event.earnings_date == _AS_OF - timedelta(days=60) + timedelta(
            days=_QUARTER_DAYS
        )
        assert lookup.event.session == DERIVED_SESSION

    def test_projection_beyond_the_lookahead_window_is_none_in_window(self):
        # Reported yesterday: the next report is a quarter away, which the
        # 45-day window does not reach. The live client answers the same way.
        lookup = (
            _calendar(_quarterly_through(_AS_OF - timedelta(days=1)))
            .lookup(_AS_OF, ["AAA"])
            .lookups_by_symbol["AAA"]
        )

        assert lookup.status == "none_in_window"
        assert lookup.event is None
        assert lookup.recent_event is not None

    def test_projection_the_calendar_has_outrun_is_reported_as_unknown(self):
        # The cadence said the report was due 10 days ago and it never came,
        # so the estimate has been overtaken by events. Reporting it as a
        # known date would block the symbol for as long as the drift lasts.
        stale = _quarterly_through(_AS_OF - timedelta(days=_QUARTER_DAYS + 10))
        lookup = _calendar(stale).lookup(_AS_OF, ["AAA"]).lookups_by_symbol["AAA"]

        assert lookup.status == "fetch_failed"
        assert lookup.event is None
        assert lookup.recent_event is not None

    def test_a_single_visible_filing_cannot_establish_a_cadence(self):
        lookup = (
            _calendar((_AS_OF - timedelta(days=5),))
            .lookup(_AS_OF, ["AAA"])
            .lookups_by_symbol["AAA"]
        )

        assert lookup.status == "fetch_failed"
        assert lookup.event is None
        assert lookup.recent_event is not None

    def test_symbol_with_no_collected_filings_reports_unknown(self):
        lookup = _calendar(()).lookup(_AS_OF, ["AAA"]).lookups_by_symbol["AAA"]

        assert lookup.status == "fetch_failed"
        assert lookup.event is None
        assert lookup.recent_event is None

    def test_symbol_absent_from_the_history_is_still_answered_explicitly(self):
        guard = _calendar(_quarterly_through(_AS_OF)).lookup(_AS_OF, ["AAA", "ZZZ"])

        assert guard.is_enabled is True
        assert set(guard.lookups_by_symbol) == {"AAA", "ZZZ"}
        assert guard.lookups_by_symbol["ZZZ"].status == "fetch_failed"

    def test_implausible_gaps_are_excluded_from_the_cadence(self):
        # A filing a week apart from another is not cadence evidence; without
        # the plausibility band its 7-day gap would drag the median down to
        # 49 days and project a report that is not due for another quarter.
        last = _AS_OF - timedelta(days=60)
        filings = (
            last - timedelta(days=_QUARTER_DAYS + 7),
            last - timedelta(days=_QUARTER_DAYS),
            last,
        )

        lookup = _calendar(filings).lookup(_AS_OF, ["AAA"]).lookups_by_symbol["AAA"]

        assert lookup.event is not None
        assert lookup.event.earnings_date == last + timedelta(days=_QUARTER_DAYS)

    def test_no_plausible_gap_at_all_reports_unknown(self):
        filings = (_AS_OF - timedelta(days=7), _AS_OF)

        lookup = _calendar(filings).lookup(_AS_OF, ["AAA"]).lookups_by_symbol["AAA"]

        assert lookup.status == "fetch_failed"
        assert lookup.event is None

    def test_recent_event_is_the_latest_visible_filing(self):
        filings = _quarterly_through(_AS_OF - timedelta(days=3))

        recent = (
            _calendar(filings)
            .lookup(_AS_OF, ["AAA"])
            .lookups_by_symbol["AAA"]
            .recent_event
        )

        assert recent is not None
        assert recent.earnings_date == _AS_OF - timedelta(days=3)
        # Deterministic metadata: a wall clock would make two runs of the
        # same backtest differ.
        assert recent.fetched_at.date() == recent.earnings_date


class TestLoadFromTheFilingHistory:
    @pytest.fixture
    def market_store(self, tmp_path):
        return MarketStore(
            Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
        )

    @staticmethod
    def _record(
        filed_on: date, period_end: date, form: str = "10-Q"
    ) -> FundamentalsRecord:
        return FundamentalsRecord(
            accession_no=f"acc-{form}-{filed_on.isoformat()}",
            symbol="AAA",
            form=form,
            fiscal_period_end=period_end,
            filed_at=datetime.combine(filed_on, datetime.min.time(), tzinfo=UTC),
            revenue=1.0,
            net_income=1.0,
            fcf=1.0,
            equity=1.0,
            assets=2.0,
            shares=1.0,
            source_url="https://www.sec.gov/example",
            fetched_at=datetime(2027, 1, 1, tzinfo=UTC),
        )

    def test_history_is_loaded_and_projects_the_next_report(self, market_store):
        filings = _quarterly_through(_AS_OF - timedelta(days=60))
        market_store.upsert_fundamentals(
            [
                self._record(filed_on, filed_on - timedelta(days=30))
                for filed_on in filings
            ]
        )

        calendar = load_derived_earnings_calendar(
            market_store, ["AAA"], as_of=_AS_OF, lookahead_days=_LOOKAHEAD_DAYS
        )

        assert calendar.projectable_symbols == ("AAA",)
        lookup = calendar.lookup(_AS_OF, ["AAA"]).lookups_by_symbol["AAA"]
        assert lookup.status == "found"
        assert lookup.event is not None
        assert lookup.event.earnings_date == filings[-1] + timedelta(days=_QUARTER_DAYS)

    def test_a_single_collected_filing_is_not_counted_as_projectable(
        self, market_store
    ):
        market_store.upsert_fundamentals(
            [self._record(_AS_OF - timedelta(days=5), _AS_OF - timedelta(days=35))]
        )

        calendar = load_derived_earnings_calendar(
            market_store, ["AAA"], as_of=_AS_OF, lookahead_days=_LOOKAHEAD_DAYS
        )

        assert calendar.projectable_symbols == ()
        assert (
            calendar.lookup(_AS_OF, ["AAA"]).lookups_by_symbol["AAA"].status
            == "fetch_failed"
        )

    def test_uncollected_symbol_yields_no_calendar_rather_than_a_guess(
        self, market_store
    ):
        calendar = load_derived_earnings_calendar(
            market_store, ["AAA"], as_of=_AS_OF, lookahead_days=_LOOKAHEAD_DAYS
        )

        assert calendar.projectable_symbols == ()
        assert calendar.filing_dates_by_symbol == {}
        assert (
            calendar.lookup(_AS_OF, ["AAA"]).lookups_by_symbol["AAA"].status
            == "fetch_failed"
        )

    def test_amended_refilings_are_not_read_as_reporting_events(self, market_store):
        # `EARNINGS_FILING_FORMS` is matched exactly, so a `10-Q/A` correction
        # filed weeks later does not become a second event and shorten the
        # estimated cadence.
        filings = _quarterly_through(_AS_OF - timedelta(days=30))
        market_store.upsert_fundamentals(
            [
                *(
                    self._record(filed_on, filed_on - timedelta(days=30))
                    for filed_on in filings
                ),
                self._record(
                    _AS_OF - timedelta(days=5),
                    filings[-1] - timedelta(days=30),
                    "10-Q/A",
                ),
            ]
        )

        calendar = load_derived_earnings_calendar(
            market_store, ["AAA"], as_of=_AS_OF, lookahead_days=_LOOKAHEAD_DAYS
        )

        assert calendar.filing_dates_by_symbol == {"AAA": filings}
        assert EARNINGS_FILING_FORMS == ("10-K", "10-Q")


# --- The gate, end to end -------------------------------------------------

_DAYS = [date(2027, 1, 1) + timedelta(days=index) for index in range(120)]
_SIGNAL_DAY = _DAYS[-2]
_INITIAL_CASH = 100_000.0
_UNIVERSE = (
    UniverseMember(
        symbol="AAA",
        company_name="AAA Inc.",
        gics_sector="Information Technology",
        source_symbol="AAA",
    ),
)


def _rising(symbol: str, start_price: float) -> list[dict[str, object]]:
    return [
        bar_row(
            symbol,
            day,
            (
                start_price + index,
                start_price + index + 1,
                start_price + index - 1,
                start_price + index,
            ),
        )
        for index, day in enumerate(_DAYS)
    ]


def _market_bars() -> list[dict[str, object]]:
    """A calm, rising index strip: the regime gate allows new entries."""
    return [
        *_rising("SPY", 400.0),
        *_rising("QQQ", 350.0),
        *flat_bars("^VIX", _DAYS, 12.0),
    ]


def _candidate() -> Candidate:
    return Candidate(
        symbol="AAA",
        as_of=_SIGNAL_DAY,
        signal_names=("trend_sma",),
        metrics={"close": 100.0, "atr14": 2.0},
        rank=1,
    )


class TestTheGateFiresOnDerivedHistory:
    """DoD: the earnings gate actually fires, and the count reaches the result."""

    #: Quarterly filings whose projection lands exactly on the signal day, so
    #: `earnings_block_business_days` (2) is satisfied with 0 business days.
    _DUE_NOW = _quarterly_through(_SIGNAL_DAY - timedelta(days=_QUARTER_DAYS))
    #: The same history shifted so the next report is a full quarter away.
    _NOT_DUE = _quarterly_through(_SIGNAL_DAY)

    def _policy(self, settings: Settings, filings: tuple[date, ...]) -> EntryPolicy:
        calendar = DerivedEarningsCalendar(
            {"AAA": filings}, settings.risk.earnings_lookahead_days
        )
        policy = build_entry_policy(
            EntryPolicyArm.REGIME_RISK,
            settings,
            _UNIVERSE,
            bars_frame([*_market_bars(), *flat_bars("AAA", _DAYS, 100.0)]),
            earnings_guard_fn=calendar.lookup,
        )
        assert policy is not None
        return policy

    def _request(self) -> EntryPolicyRequest:
        return EntryPolicyRequest(
            as_of=_SIGNAL_DAY,
            candidates=(_candidate(),),
            open_positions=(),
            equity=_INITIAL_CASH,
        )

    def test_projected_report_within_the_block_window_rejects_the_candidate(
        self, settings
    ):
        decision = self._policy(settings, self._DUE_NOW).decide(self._request())["AAA"]

        assert decision.is_allowed is False
        assert decision.reject_reason == ENTRY_BLOCK_EARNINGS

    def test_the_same_symbol_is_allowed_once_the_report_is_a_quarter_away(
        self, settings
    ):
        decision = self._policy(settings, self._NOT_DUE).decide(self._request())["AAA"]

        assert decision.is_allowed is True

    def test_a_symbol_without_a_derivable_calendar_is_never_blocked(self, settings):
        decision = self._policy(settings, ()).decide(self._request())["AAA"]

        assert decision.is_allowed is True

    def test_the_block_is_counted_under_earnings_in_the_backtest_result(self, settings):
        rows = [*_market_bars(), *flat_bars("AAA", _DAYS, 100.0)]
        candidates = {_SIGNAL_DAY: [_candidate()]}

        result = BacktestEngine(settings, self._policy(settings, self._DUE_NOW)).run(
            _DAYS,
            bars_frame(rows),
            lambda day: candidates.get(day, []),
            _INITIAL_CASH,
        )

        assert result.trades == ()
        assert dict(result.entry_block_counts)[ENTRY_BLOCK_EARNINGS] == 1
        assert dict(result.entry_block_days)[ENTRY_BLOCK_EARNINGS] == 1
