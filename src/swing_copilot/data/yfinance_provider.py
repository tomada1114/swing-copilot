"""yfinance-backed `DataProvider` for prototyping (P1-P3, CON-02).

Not for production use — yfinance is an unofficial wrapper with no SLA.
`yfinance.download(..., auto_adjust=False, actions=True,
multi_level_index=True)` returns split-adjusted OHLCV plus `Dividends` and
`Stock Splits` columns in a `(field, ticker)` MultiIndex-columns DataFrame
regardless of symbol count; `_normalize` flattens that into the tidy
`BARS_COLUMNS` schema every `DataProvider` returns, and clamps to
`[start, end)` explicitly rather than trusting yfinance's own end-date
handling.

`auto_adjust=False, actions=True` is what makes point-in-time storage
possible at all: the corporate actions come back in the same request (no
extra call), and `data/adjustments.unadjust_yahoo_bars` uses the response's
own `Stock Splits` column to undo Yahoo's split adjustment and emit
**as-traded** bars. A symbol whose adjustment basis cannot be resolved --
Issue #413's MNST, whose response mixed adjusted and unadjusted rows -- is
failed non-retryably rather than stored half-normalized.

`_normalize` also *validates* every OHLCV cell it emits, because a per-symbol
data-quality problem has to leave here as a `BarFetchResult.failures` entry
and never as an exception (`data/base.py`'s contract). Three kinds of trouble
are read differently, deliberately:

* **`Close` is NaN** — that row is not a trading row for this symbol. A bulk
  `yfinance.download` unions every requested symbol's calendar, so a symbol
  that did not trade on a date another symbol did gets an all-NaN row.
  Skipped, as it always has been.
* **`Close` is a real price but another field is not finite** — the feed
  claims a bar exists yet hands over an unusable field. A NaN `Volume` on a
  thin or trading-halted name is the realistic case (Issue #249), and it used
  to escape as `int(nan)`'s `ValueError`. The *symbol* is now reported in
  `failures` and none of its rows are emitted.
* **The response's timestamp index has a duplicate** — `.loc[timestamp]`
  returns a `Series` instead of a scalar for a duplicated key, and
  `pd.isna(...)` on that `Series` used in an `if` used to escape as
  `ValueError: The truth value of a Series is ambiguous` (Issue #294). No
  observed `yfinance.download(..., auto_adjust=True, multi_level_index=True)`
  response path produces a duplicate `DatetimeIndex` today, so this is
  validated defensively rather than silently collapsed to one row: a quiet
  "pick the last row" rule would misreport a genuinely broken price window if
  that ever changed. The symbol fails with a reason naming the date and the
  duplicate count; none of its rows are emitted.

Neither of the latter two cases is a silent per-row drop: a hole punched into
a price window is invisible to every downstream indicator that averages over
N bars, while a failure is named in the run's report. Neither is retryable —
both are validation errors, so the retry loop leaves them alone.
`MarketStore.write_bars`' batch-wide rejection (Issue #227) stays the layer
*under* this one, unchanged.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from datetime import date, timedelta
from typing import TYPE_CHECKING, Protocol, TypedDict

import pandas as pd
import yfinance as yf

from swing_copilot.data.adjustments import (
    NormalizationRejection,
    SplitEvent,
    unadjust_yahoo_bars,
)
from swing_copilot.data.base import (
    ACTIONS_COLUMNS,
    BARS_COLUMNS,
    BarFetchResult,
    FetchFailure,
    empty_actions_frame,
)
from swing_copilot.retry import RETRY_DELAYS_SECONDS, is_retryable_external_error

if TYPE_CHECKING:
    from collections.abc import Callable

_REQUIRED_FIELDS = ("Open", "High", "Low", "Close", "Volume")
#: yfinance's `actions=True` columns, mapped to `ACTIONS_COLUMNS`' `kind`.
#: Absent from a response only when `actions=True` was not honored (a fake in
#: a test that predates the switch), which reads as "no corporate action".
_ACTION_FIELDS = {"Stock Splits": "split", "Dividends": "dividend"}
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


def _finite_value(value: object) -> float | None:
    """One OHLCV cell as a real, finite number, or `None` if it is not usable.

    Mirrors `MarketStore._reject_non_finite_bars`' notion of "usable", so the
    two layers cannot disagree about what counts as a broken value: a
    non-numeric cell is non-finite here too, and fails at this boundary
    instead of as a `ValueError` out of `float()`/`int()`.

    The parsed number is handed back rather than a bool so the caller builds
    the row out of it. Re-reading the raw cell would reopen the same hole one
    field over: `int("2100.5")` raises `ValueError` on a numeric *string*
    `Volume` that `float()` accepted, which is exactly the escape this guard
    exists to close.
    """
    try:
        numeric = float(value)  # type: ignore[arg-type] # any numeric-like cell
    except TypeError, ValueError:
        return None
    return numeric if math.isfinite(numeric) else None


def _symbol_bars(
    symbol: str,
    fields: dict[str, pd.Series],
    index: pd.Index,
    start: date,
    end: date,
) -> tuple[list[dict[str, object]], str | None]:
    """Build one symbol's rows over `[start, end)`, or say why it is unusable.

    Args:
        symbol: The ticker these rows belong to.
        fields: That symbol's `Open`/`High`/`Low`/`Close`/`Volume` series.
        index: The response's shared timestamp index.
        start: Inclusive range start.
        end: Exclusive range end.

    Returns:
        The rows and `None`; or an empty list and the operator-facing reason
        the symbol must be failed. Rows are discarded rather than partially
        emitted, so no caller persists half of a window whose feed is broken.
    """
    rows: list[dict[str, object]] = []
    # `.loc[timestamp]` below assumes a unique index (see module docstring,
    # Issue #294) -- check for duplicates before ever indexing with it.
    timestamp_counts = Counter(index)
    for timestamp in index:
        bar_date = timestamp.date()
        if not (start <= bar_date < end):
            continue
        duplicate_count = timestamp_counts[timestamp]
        if duplicate_count > 1:
            return [], (
                f"duplicate timestamp in response on {bar_date.isoformat()} "
                f"({duplicate_count} rows)"
            )
        if pd.isna(fields["Close"].loc[timestamp]):
            continue
        values = {name: fields[name].loc[timestamp] for name in _REQUIRED_FIELDS}
        numeric: dict[str, float] = {}
        invalid: list[str] = []
        for name in _REQUIRED_FIELDS:
            parsed = _finite_value(values[name])
            if parsed is None:
                invalid.append(name)
            else:
                numeric[name] = parsed
        if invalid:
            detail = ", ".join(f"{name}={values[name]}" for name in invalid)
            return [], f"non-finite OHLCV value on {bar_date.isoformat()} ({detail})"
        rows.append(
            {
                "symbol": symbol,
                "date": bar_date,
                "open": numeric["Open"],
                "high": numeric["High"],
                "low": numeric["Low"],
                "close": numeric["Close"],
                "volume": int(numeric["Volume"]),
            }
        )
    return rows, None


class _ActionRow(TypedDict):
    """One `ACTIONS_COLUMNS` row, kept typed until it becomes a frame."""

    symbol: str
    ex_date: date
    kind: str
    value: float


def _symbol_actions(
    symbol: str, raw: pd.DataFrame, start: date, end: date
) -> list[_ActionRow]:
    """Build one symbol's `ACTIONS_COLUMNS` rows from the response's actions.

    Only called after `_symbol_bars` has cleared the symbol, so the response
    index is known to be duplicate-free and `.loc[timestamp]` is a scalar.

    Args:
        symbol: The ticker these actions belong to.
        raw: The whole `(field, ticker)` MultiIndex response.
        start: Inclusive range start.
        end: Exclusive range end.

    Returns:
        Rows ordered by ex-date then kind, so two runs over one response
        produce the same frame. A zero value is *no* action, which is how
        yfinance fills the columns on an ordinary day.
    """
    rows: list[_ActionRow] = []
    for column, kind in _ACTION_FIELDS.items():
        try:
            series = raw[(column, symbol)]
        except KeyError:
            continue
        for timestamp in raw.index:
            ex_date = timestamp.date()
            if not (start <= ex_date < end):
                continue
            value = _finite_value(series.loc[timestamp])
            if value is None or value == 0.0:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "ex_date": ex_date,
                    "kind": kind,
                    "value": value,
                }
            )
    rows.sort(key=lambda row: (row["ex_date"], row["kind"]))
    return rows


def _splits_from(action_rows: list[_ActionRow]) -> list[SplitEvent]:
    """The split events among `action_rows`, ascending by ex-date."""
    return [
        SplitEvent(ex_date=row["ex_date"], factor=row["value"])
        for row in action_rows
        if row["kind"] == "split"
    ]


def _normalize(
    raw: pd.DataFrame, symbols: list[str], start: date, end: date
) -> BarFetchResult:
    if raw.empty:
        empty_failures = tuple(
            FetchFailure(symbol=symbol, reason="no data returned", retryable=True)
            for symbol in symbols
        )
        return BarFetchResult(bars=_empty_bars_frame(), failures=empty_failures)

    frames: list[pd.DataFrame] = []
    action_rows: list[_ActionRow] = []
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

        symbol_rows, corrupt_reason = _symbol_bars(
            symbol, fields, raw.index, start, end
        )
        if corrupt_reason is not None:
            # Not retryable: a malformed value is a validation error, and a
            # refetch would only spend the attempt budget (AGENTS.md, "Do not
            # retry validation/programming errors").
            failures.append(
                FetchFailure(symbol=symbol, reason=corrupt_reason, retryable=False)
            )
            continue

        symbol_actions = _symbol_actions(symbol, raw, start, end)
        unadjusted = unadjust_yahoo_bars(
            symbol,
            pd.DataFrame(symbol_rows, columns=list(BARS_COLUMNS)),
            _splits_from(symbol_actions),
        )
        if isinstance(unadjusted, NormalizationRejection):
            # Also a validation error, and one a refetch cannot fix: the
            # provider's own history is internally inconsistent (Issue #413).
            failures.append(
                FetchFailure(symbol=symbol, reason=unadjusted.reason, retryable=False)
            )
            continue
        if not unadjusted.empty:
            frames.append(unadjusted)
        action_rows.extend(symbol_actions)

    bars = pd.concat(frames, ignore_index=True) if frames else _empty_bars_frame()
    actions = (
        pd.DataFrame(action_rows, columns=list(ACTIONS_COLUMNS))
        if action_rows
        else empty_actions_frame()
    )
    return BarFetchResult(bars=bars, failures=tuple(failures), actions=actions)


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
        actions: list[pd.DataFrame] = []
        failures_by_symbol: dict[str, FetchFailure] = {}

        for delay in (*RETRY_DELAYS_SECONDS, None):
            try:
                raw = self._download_fn(
                    remaining_symbols,
                    start=start,
                    end=end,
                    auto_adjust=False,
                    actions=True,
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
            if not result.actions.empty:
                actions.append(result.actions)
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
        merged_actions = (
            pd.concat(actions, ignore_index=True) if actions else empty_actions_frame()
        )
        failures = tuple(
            failures_by_symbol[symbol]
            for symbol in symbols
            if symbol in failures_by_symbol
        )
        return BarFetchResult(
            bars=merged_bars, failures=failures, actions=merged_actions
        )

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
                actions=result.actions,
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
            bars=result.bars,
            failures=result.failures + extra_failures,
            actions=result.actions,
        )
