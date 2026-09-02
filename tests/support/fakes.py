"""Fakes shared across the test suite (Issue #398).

Each class here used to exist as several near-verbatim copies pasted into
individual test modules -- already drifted in at least one case:
`tests/pipeline/test_failsoft.py`'s `FakeDataProvider` silently dropped the
`failures` constructor argument that `tests/pipeline/test_daily_core.py`'s
and `tests/test_e2e_smoke.py`'s copies both had, so any `failures`-dependent
behavior went untested in the fail-soft suite. `tests/support/test_fakes.py`
pins each fake's shape against the real Protocol it stands in for.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from swing_copilot.data.base import BarFetchResult, FetchFailure
from swing_copilot.storage.market_store import FundamentalsRecord
from swing_copilot.text.base import TextItem

if TYPE_CHECKING:
    import pandas as pd


class FixedClock:
    """`Clock` returning a fixed `as_of` date and `now` instant.

    Replaces five verbatim `FakeClock` copies (all hard-coding the same
    2027-03-01 date). A caller that needs a different fixed instant passes
    it explicitly instead of pasting a sixth copy.
    """

    def __init__(self, as_of: date, now: datetime) -> None:
        self._as_of = as_of
        self._now = now

    def today(self) -> date:
        """Return the fixed `as_of` date."""
        return self._as_of

    def now(self) -> datetime:
        """Return the fixed `now` instant."""
        return self._now


class StubDataProvider:
    """`DataProvider` returning fixed bars for both fetch methods.

    `failures` defaults to `()` but is always threaded through to both
    methods' `BarFetchResult` -- the `test_failsoft.py` copy this replaces
    dropped it entirely (Issue #398 finding B). Every `get_daily_bars` call's
    requested symbols are recorded in `requested_symbols`, for the tests that
    assert on fetch scope.
    """

    def __init__(
        self, bars: pd.DataFrame, failures: tuple[FetchFailure, ...] = ()
    ) -> None:
        self._bars = bars
        self._failures = failures
        self.requested_symbols: list[list[str]] = []

    def get_daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> BarFetchResult:
        """Record `symbols`, then return the fixed bars and failures."""
        del start, end
        self.requested_symbols.append(list(symbols))
        return BarFetchResult(bars=self._bars, failures=self._failures)

    def get_latest_bars(self, symbols: list[str], as_of: date) -> BarFetchResult:
        """Return the fixed bars and failures, ignoring `symbols`/`as_of`."""
        del symbols, as_of
        return BarFetchResult(bars=self._bars, failures=self._failures)


class StubNewsClient:
    """`_NewsClientLike` returning one synthesized news item per symbol."""

    def fetch_company_news(
        self, symbol: str, since: date, *, as_of: date
    ) -> list[TextItem]:
        """Return one news `TextItem` for `symbol`, dated `as_of`."""
        del since
        stamp = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)
        return [
            TextItem(
                source_id=f"news:{symbol}",
                symbol=symbol,
                source_type="news",
                published_at=stamp,
                title=f"{symbol} news",
                source_url=f"https://example.com/{symbol}",
                content_text=f"{symbol} announced a new product line.",
                fetched_at=stamp,
            )
        ]


#: A calendar event factory, given the same `(start, end, as_of)` triple
#: `fetch_calendar_events` received.
CalendarEventsFactory = Callable[[date, date, date], Sequence[TextItem]]


class StubCalendarClient:
    """`_CalendarClientLike` returning caller-supplied (or no) events.

    `events` is either a fixed sequence (returned verbatim on every call --
    the shape `tests/test_e2e_smoke.py`'s copy needed, defaulting to no
    events) or a factory computed from the call's own `(start, end, as_of)`
    (the shape `tests/pipeline/test_failsoft.py`'s copy needed, whose one
    synthesized event's timestamp is derived from `start`).
    """

    def __init__(self, events: Sequence[TextItem] | CalendarEventsFactory = ()) -> None:
        self._events = events

    def fetch_calendar_events(
        self, start: date, end: date, *, as_of: date
    ) -> list[TextItem]:
        """Return the configured events, ignoring `start`/`end`/`as_of` if fixed."""
        if callable(self._events):
            return list(self._events(start, end, as_of))
        del start, end, as_of
        return list(self._events)


#: A per-call fundamentals/filing-text factory, given `(symbol, as_of)`.
FundamentalsFactory = Callable[[str, datetime], Sequence[FundamentalsRecord]]
FilingTextsFactory = Callable[[str, datetime], Sequence[TextItem]]


class StubEdgarClient:
    """`_EdgarClientLike` returning caller-supplied fundamentals/filing text.

    Both `fundamentals` and `filing_texts` accept either a fixed sequence
    (returned verbatim, ignoring the call's `symbol`/`as_of` -- the shape
    `tests/text/test_edgar_filings.py`'s copy needed) or a factory computed
    from `(symbol, as_of)` (the shape `tests/test_e2e_smoke.py`'s copy
    needed, whose fundamentals/filing text both embed the requested
    `symbol`). Both default to empty, for a caller exercising only the other
    method. Every `fetch_filing_texts` call's arguments are recorded in
    `calls`, for the one test module that asserted on them.
    """

    def __init__(
        self,
        filing_texts: Sequence[TextItem] | FilingTextsFactory = (),
        *,
        fundamentals: Sequence[FundamentalsRecord] | FundamentalsFactory = (),
    ) -> None:
        self._filing_texts = filing_texts
        self._fundamentals = fundamentals
        self.calls: list[
            tuple[str, list[str], datetime, datetime | None, int | None]
        ] = []

    def fetch_fundamentals(
        self, symbol: str, as_of: datetime
    ) -> list[FundamentalsRecord]:
        """Return the configured fundamentals for `symbol` as of `as_of`."""
        source = self._fundamentals
        return list(source(symbol, as_of)) if callable(source) else list(source)

    def fetch_filing_texts(
        self,
        symbol: str,
        form_types: list[str],
        *,
        as_of: datetime,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[TextItem]:
        """Record the call, then return the configured filing texts."""
        self.calls.append((symbol, form_types, as_of, since, limit))
        source = self._filing_texts
        return list(source(symbol, as_of)) if callable(source) else list(source)
