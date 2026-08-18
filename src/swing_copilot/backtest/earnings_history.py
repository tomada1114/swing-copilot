"""Point-in-time earnings calendar derived from the filing history (Issue #201).

Issue #184 shipped the injection seam `build_entry_policy(...,
earnings_guard_fn=...)`, but nothing could be plugged into it: the simulator
has no historical earnings calendar, and the live one (`earnings_calendar`,
filled by `FinnhubEarningsClient`) is keyed by `symbol` alone — it holds the
*current* next event, not a history, so replaying 2020 with it would be pure
look-ahead. The gate therefore reported 0 rather than fabricating dates.

The one point-in-time filing history the application does keep is the
`fundamentals` table: one row per SEC filing, natural-keyed by
`accession_no`, carrying `form` and `filed_at` (the acceptance timestamp).
`filed_at <= as_of` is exactly the visibility rule `AGENTS.md` mandates for
filings, so a calendar built from it can be replayed honestly.

**What this is, precisely.** A company's periodic reports arrive on a stable
cadence, so the next one can be *projected* from the ones already visible:
`last visible filing + the median gap between consecutive visible filings`.
That is an estimate, and it is labelled as one — never presented as a known
date:

- With fewer than two visible filings, or no plausible cadence among them,
  the symbol reports `fetch_failed` ("we do not know"), which warns and never
  blocks. This is the honest degradation for a symbol the collector has not
  covered yet, and it is the common case at the start of a backtest window.
- A projection that `as_of` has already passed means the report did not
  arrive when the cadence said it would, so the estimate has been overtaken by
  events and is likewise reported as `fetch_failed`. A stale estimate must
  not keep blocking a symbol indefinitely.
- A projection beyond the configured lookahead window is `none_in_window`,
  the same answer the live client gives when it searched and found nothing.

**Known limits**, which the operator must read the resulting gate counts
against (`docs/reference.md`):

- `filed_at` is the *filing* date, not the announcement date. Issuers
  typically release results (8-K Item 2.02) days before the 10-Q is accepted,
  so a projection built from filing dates is systematically *late* relative
  to the event the gate exists to avoid. It is internally consistent — the
  cadence and the projection are both measured in filing dates — but the
  block window sits a few days later than a true earnings calendar's would.
- Coverage is whatever `pipeline/daily.py`'s fundamentals step has collected,
  which is candidate symbols from the day the project started running, not a
  full historical panel. Symbols and periods it never fetched simply have no
  calendar, and say so.
- Only the forms `data/edgar.py` normalizes into `fundamentals` (`10-K`,
  `10-Q`) count as reporting events. The Q4 release, announced weeks before
  the 10-K is filed, is covered only by the projection, never observed.
"""

from __future__ import annotations

import statistics
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING

from swing_copilot.data.earnings import EarningsEvent, EarningsLookup
from swing_copilot.risk.checks import EarningsGuardInput

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

    from swing_copilot.storage.market_store import MarketStore

#: SEC forms treated as one reporting event. Matches `data/edgar.py`'s
#: `_FUNDAMENTALS_FORMS`, which is what the `fundamentals` table can contain
#: at all — naming a form outside it would silently match nothing.
EARNINGS_FILING_FORMS = ("10-K", "10-Q")

#: A projected event carries no announcement session (before/after the bell);
#: `EarningsEvent.session` is metadata the risk core never reads, so the
#: derivation states its ignorance rather than guessing "amc".
DERIVED_SESSION = "unknown"

#: Plausible calendar-day span between two consecutive periodic reports.
#: A quarterly cadence is ~91 days, and the Q3-report -> next-Q1-report gap is
#: ~182 when the annual report is missing from the collected history. Gaps
#: outside this band come from backfilled or irregular filings and would
#: poison the median, so they are dropped from the cadence estimate.
_MIN_REPORTING_GAP_DAYS = 45
_MAX_REPORTING_GAP_DAYS = 200

#: Two visible filings are the minimum from which a gap — and therefore a
#: cadence — can be measured at all.
_MIN_FILINGS_FOR_PROJECTION = 2


@dataclass(frozen=True, slots=True)
class DerivedEarningsCalendar:
    """Projects each symbol's next reporting date from its own filing history.

    `lookup` has the exact shape `build_entry_policy(...,
    earnings_guard_fn=...)` expects, so an instance is wired straight into the
    `regime+risk` arm.
    """

    #: `{symbol: distinct filing dates, ascending}`, as
    #: `MarketStore.read_filing_dates` returns them. Dates after a given
    #: `as_of` may be present (the whole window is loaded once); `lookup`
    #: trims them, so this is never read past its own cutoff.
    filing_dates_by_symbol: Mapping[str, tuple[date, ...]]
    #: How far ahead a projected date still counts as "upcoming", mirroring
    #: the live client's search window (`risk.earnings_lookahead_days`).
    lookahead_days: int

    def lookup(self, as_of: date, symbols: Sequence[str]) -> EarningsGuardInput:
        """Resolve every symbol's earnings-guard input as of `as_of`.

        Args:
            as_of: The signal day. Filings accepted on this day are visible;
                later ones are not.
            symbols: Candidate symbols under consideration on that day.

        Returns:
            An enabled `EarningsGuardInput` covering exactly `symbols`. Every
            symbol is present, including those with no usable history — a
            missing key and a `fetch_failed` lookup mean the same thing to
            `RiskChecker`, but only the explicit one is auditable.
        """
        return EarningsGuardInput(
            is_enabled=True,
            lookups_by_symbol={
                symbol: self._lookup_symbol(symbol, as_of) for symbol in symbols
            },
        )

    @property
    def projectable_symbols(self) -> tuple[str, ...]:
        """Symbols whose loaded history could ever support a projection.

        An upper bound, not a promise: two filings are the minimum a cadence
        can be measured from, but on any given `as_of` fewer of them may be
        visible yet. Reported so a 0-count earnings gate can be told apart
        from a gate that had no calendar to fire on.
        """
        return tuple(
            sorted(
                symbol
                for symbol, dates in self.filing_dates_by_symbol.items()
                if len(dates) >= _MIN_FILINGS_FOR_PROJECTION
            )
        )

    def _lookup_symbol(self, symbol: str, as_of: date) -> EarningsLookup:
        filed = self.filing_dates_by_symbol.get(symbol, ())
        # Inclusive cutoff: `bisect_right` puts a filing dated exactly `as_of`
        # inside the visible slice, one dated `as_of + 1 day` outside it.
        visible = filed[: bisect_right(filed, as_of)]
        if not visible:
            return EarningsLookup("fetch_failed", None, None)
        recent_event = _event(symbol, visible[-1])
        projected = _project_next(visible)
        if projected is None or projected < as_of:
            # No cadence, or a cadence the calendar has already outrun.
            return EarningsLookup("fetch_failed", None, recent_event)
        if projected > as_of + timedelta(days=self.lookahead_days):
            return EarningsLookup("none_in_window", None, recent_event)
        return EarningsLookup("found", _event(symbol, projected), recent_event)


def load_derived_earnings_calendar(
    market_store: MarketStore,
    symbols: Sequence[str],
    *,
    as_of: date,
    lookahead_days: int,
) -> DerivedEarningsCalendar:
    """Load the filing history once and wrap it as a replayable calendar.

    Args:
        market_store: Repository owning the `fundamentals` filing history.
        symbols: Symbols the backtest will consider.
        as_of: The backtest window's end. Loading the whole window at once
            mirrors how bars are loaded; `DerivedEarningsCalendar.lookup`
            re-applies the per-day cutoff, so no simulated day ever sees a
            filing from its own future.
        lookahead_days: `risk.earnings_lookahead_days`.

    Returns:
        The calendar to hand to `build_entry_policy(..., earnings_guard_fn=)`.
    """
    return DerivedEarningsCalendar(
        market_store.read_filing_dates(symbols, EARNINGS_FILING_FORMS, as_of),
        lookahead_days,
    )


def _event(symbol: str, earnings_date: date) -> EarningsEvent:
    # `fetched_at` is derived from the date itself, never from a wall clock:
    # a backtest must produce the same result whenever it is run.
    return EarningsEvent(
        symbol=symbol,
        earnings_date=earnings_date,
        session=DERIVED_SESSION,
        fetched_at=datetime.combine(earnings_date, time.min, tzinfo=UTC),
    )


def _project_next(visible: Sequence[date]) -> date | None:
    """Project the next reporting date from the visible cadence, or `None`."""
    if len(visible) < _MIN_FILINGS_FOR_PROJECTION:
        return None
    gaps = [
        days
        for earlier, later in pairwise(visible)
        if _MIN_REPORTING_GAP_DAYS
        <= (days := (later - earlier).days)
        <= _MAX_REPORTING_GAP_DAYS
    ]
    if not gaps:
        return None
    return visible[-1] + timedelta(days=round(statistics.median(gaps)))
