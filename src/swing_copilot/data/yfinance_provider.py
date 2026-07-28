"""yfinance-backed `DataProvider` for prototyping (P1-P3, CON-02).

Not for production use — yfinance is an unofficial wrapper with no SLA.
`yfinance.download(..., auto_adjust=True, multi_level_index=True)` returns
adjusted OHLCV in a `(field, ticker)` MultiIndex-columns DataFrame regardless
of symbol count; `_normalize` flattens that into the tidy `BARS_COLUMNS`
schema every `DataProvider` returns, and clamps to `[start, end)` explicitly
rather than trusting yfinance's own end-date handling.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

import pandas as pd
import yfinance as yf

from swing_copilot.data.base import BARS_COLUMNS, BarFetchResult, FetchFailure
from swing_copilot.retry import RETRY_DELAYS_SECONDS, is_retryable_external_error

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

_REQUIRED_FIELDS = ("Open", "High", "Low", "Close", "Volume")
_LATEST_BAR_LOOKBACK_DAYS = 10
_REQUEST_TIMEOUT_SECONDS = 10


class _DownloadFn(Protocol):
    def __call__(
        self, symbols: list[str], *, start: date, end: date, **kwargs: object
    ) -> pd.DataFrame:
        """Match `yfinance.download`'s call shape closely enough to fake it."""
        ...  # pragma: no cover


def _empty_bars_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(BARS_COLUMNS))


def _normalize(
    raw: pd.DataFrame, symbols: list[str], start: date, end: date
) -> BarFetchResult:
    if raw.empty:
        empty_failures = tuple(
            FetchFailure(symbol=symbol, reason="no data returned", retryable=True)
            for symbol in symbols
        )
        return BarFetchResult(bars=_empty_bars_frame(), failures=empty_failures)

    rows: list[dict[str, object]] = []
    failures: list[FetchFailure] = []

    for symbol in symbols:
        try:
            fields = {field: raw[(field, symbol)] for field in _REQUIRED_FIELDS}
        except KeyError:
            failures.append(
                FetchFailure(
                    symbol=symbol,
                    reason="symbol not present in provider response",
                    retryable=True,
                )
            )
            continue

        if fields["Close"].isna().all():
            failures.append(
                FetchFailure(
                    symbol=symbol,
                    reason="no data returned (possibly delisted)",
                    retryable=False,
                )
            )
            continue

        for timestamp in raw.index:
            bar_date = timestamp.date()
            if not (start <= bar_date < end) or pd.isna(fields["Close"].loc[timestamp]):
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "date": bar_date,
                    "open": float(fields["Open"].loc[timestamp]),
                    "high": float(fields["High"].loc[timestamp]),
                    "low": float(fields["Low"].loc[timestamp]),
                    "close": float(fields["Close"].loc[timestamp]),
                    "volume": int(fields["Volume"].loc[timestamp]),
                }
            )

    bars = pd.DataFrame(rows, columns=list(BARS_COLUMNS))
    return BarFetchResult(bars=bars, failures=tuple(failures))


class YFinanceProvider:
    """Prototype `DataProvider` backed by `yfinance.download` (CON-02)."""

    def __init__(
        self,
        download_fn: _DownloadFn = yf.download,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create a provider.

        Args:
            download_fn: Injectable stand-in for `yfinance.download`, used by
                tests to avoid real network calls.
            sleep_fn: Injectable delay function used between retry attempts.
        """
        self._download_fn = download_fn
        self._sleep_fn = sleep_fn

    def get_daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> BarFetchResult:
        """See `DataProvider.get_daily_bars`."""
        if not symbols:
            return BarFetchResult(bars=_empty_bars_frame(), failures=())

        remaining_symbols = list(symbols)
        bars: list[pd.DataFrame] = []
        failures_by_symbol: dict[str, FetchFailure] = {}

        for delay in (*RETRY_DELAYS_SECONDS, None):
            try:
                raw = self._download_fn(
                    remaining_symbols,
                    start=start,
                    end=end,
                    auto_adjust=True,
                    multi_level_index=True,
                    progress=False,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                retryable = is_retryable_external_error(exc)
                result = BarFetchResult(
                    bars=_empty_bars_frame(),
                    failures=tuple(
                        FetchFailure(
                            symbol=symbol,
                            reason=str(exc),
                            retryable=retryable,
                        )
                        for symbol in remaining_symbols
                    ),
                )
            else:
                result = _normalize(raw, remaining_symbols, start, end)

            if not result.bars.empty:
                bars.append(result.bars)
            failed_symbols = {failure.symbol for failure in result.failures}
            for failure in result.failures:
                failures_by_symbol[failure.symbol] = failure
            for symbol in set(remaining_symbols) - failed_symbols:
                failures_by_symbol.pop(symbol, None)

            retryable_symbols = [
                failure.symbol for failure in result.failures if failure.retryable
            ]
            if not retryable_symbols or delay is None:
                break
            remaining_symbols = retryable_symbols
            self._sleep_fn(delay)

        merged_bars = (
            pd.concat(bars, ignore_index=True) if bars else _empty_bars_frame()
        )
        failures = tuple(
            failures_by_symbol[symbol]
            for symbol in symbols
            if symbol in failures_by_symbol
        )
        return BarFetchResult(bars=merged_bars, failures=failures)

    def get_latest_bars(self, symbols: list[str], as_of: date) -> BarFetchResult:
        """See `DataProvider.get_latest_bars`."""
        window_start = as_of - timedelta(days=_LATEST_BAR_LOOKBACK_DAYS)
        result = self.get_daily_bars(symbols, window_start, as_of + timedelta(days=1))

        if result.bars.empty:
            found_symbols: set[str] = set()
        else:
            result = BarFetchResult(
                bars=(
                    result.bars.sort_values("date")
                    .groupby("symbol", as_index=False)
                    .tail(1)
                    .reset_index(drop=True)
                ),
                failures=result.failures,
            )
            found_symbols = set(result.bars["symbol"])

        already_failed = {failure.symbol for failure in result.failures}
        missing = [
            symbol
            for symbol in symbols
            if symbol not in found_symbols and symbol not in already_failed
        ]
        extra_failures = tuple(
            FetchFailure(
                symbol=symbol,
                reason="no bar on or before as_of within lookback window",
                retryable=True,
            )
            for symbol in missing
        )
        return BarFetchResult(
            bars=result.bars, failures=result.failures + extra_failures
        )
