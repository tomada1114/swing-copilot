"""`DataProvider` contract: daily *raw* OHLCV + corporate actions (FR-02, NFR-07).

Concrete providers (yfinance today, EODHD at P4) normalize their own
provider-specific column layouts and adjustment conventions into this single
tidy schema (`symbol, date, open, high, low, close, volume`) so downstream
code never depends on a provider's raw shape. Per-symbol failures are
returned via `BarFetchResult.failures`, never raised, so a batch fetch always
completes with whatever symbols succeeded.

Bars are **as-traded** (raw): neither split- nor dividend-adjusted, i.e. the
prices and volumes that actually printed on the day. Corporate actions travel
beside them in `BarFetchResult.actions` (`ACTIONS_COLUMNS`), and split
adjustment happens on *read*, as of an explicit `as_of`
(`storage/market_store.MarketStore.read_bars`). Storing an adjusted price
would make a stored bar depend on when it was fetched -- the defect Issue
#413 is: one provider response mixed adjusted and unadjusted rows for the
same symbol, and the store faithfully preserved the mixture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import pandas as pd

if TYPE_CHECKING:
    from datetime import date

BARS_COLUMNS = ("symbol", "date", "open", "high", "low", "close", "volume")
#: Corporate actions a provider reports alongside the bars. `kind` is
#: `"split"` (`value` is the split factor: 2.0 for a 2:1, 0.1 for a 1:10
#: reverse split) or `"dividend"` (`value` is cash per share).
ACTIONS_COLUMNS = ("symbol", "ex_date", "kind", "value")


def empty_actions_frame() -> pd.DataFrame:
    """An empty, correctly-shaped `ACTIONS_COLUMNS` frame."""
    return pd.DataFrame(columns=list(ACTIONS_COLUMNS))


@dataclass(frozen=True, slots=True)
class FetchFailure:
    """One symbol's fetch failure within a batch request."""

    symbol: str
    reason: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class BarFetchResult:
    """Batch fetch outcome: raw bars, corporate actions, and any failures.

    `actions` defaults to an empty frame so a caller that only produces bars
    (a test fake, `get_latest_bars`' re-wrapping) stays valid without
    restating it.
    """

    bars: pd.DataFrame
    failures: tuple[FetchFailure, ...]
    actions: pd.DataFrame = field(default_factory=empty_actions_frame)


class DataProvider(Protocol):
    """Daily OHLCV fetch, abstracted over the underlying price data source."""

    def get_daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> BarFetchResult:
        """Fetch daily OHLCV for `symbols` over `[start, end)`.

        Args:
            symbols: Ticker symbols to fetch.
            start: Inclusive range start.
            end: Exclusive range end — returned bars never include `end`.

        Returns:
            Raw (as-traded) OHLCV bars (columns: `symbol, date, open, high,
            low, close, volume`) for the symbols that succeeded, the
            corporate actions observed in the same window
            (`ACTIONS_COLUMNS`), plus failures for the rest.
        """
        ...  # pragma: no cover

    def get_latest_bars(self, symbols: list[str], as_of: date) -> BarFetchResult:
        """Return each symbol's most recent daily bar on or before `as_of`.

        Args:
            symbols: Ticker symbols to fetch.
            as_of: Latest trading date to consider.

        Returns:
            At most one raw bar per symbol, plus failures for symbols with no
            bar available on or before `as_of`. The newest bar is always
            as-traded, so it needs no adjustment to be comparable with a
            price quoted today.
        """
        ...  # pragma: no cover
