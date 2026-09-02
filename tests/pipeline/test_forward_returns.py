"""P8-30: shared trading-calendar / forward-return primitives.

The backward lookup (`find_target_trading_day`) is `pipeline/postmortem.py`'s
existing behavior moved verbatim; the forward lookup
(`find_maturity_trading_day`) is new for `retro/evaluate.py`. Both index the
same benchmark-derived calendar, so the round-trip tests below are the
contract that keeps the two directions from drifting apart.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from swing_copilot.pipeline.forward_returns import (
    compute_forward_return,
    find_maturity_trading_day,
    find_target_trading_day,
)
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from tests.conftest import plant_non_finite_bars

BENCHMARK = "SPY"


def _bars(symbol: str, prices: dict[date, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": symbol,
                "date": bar_date,
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": 1_000_000,
                "provider": "test",
                "fetched_at": datetime(2026, 7, 24, tzinfo=UTC),
            }
            for bar_date, price in prices.items()
        ]
    )


@pytest.fixture
def market_store(tmp_path):
    return MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )


def _seed_calendar(market_store: MarketStore, days: list[date]) -> None:
    market_store.write_bars(_bars(BENCHMARK, dict.fromkeys(days, 100.0)))


def _consecutive_days(anchor: date, count: int) -> list[date]:
    """`count` consecutive calendar dates starting at `anchor` (ascending)."""
    return [anchor + timedelta(days=offset) for offset in range(count)]


def _weekday_days(anchor: date, count: int) -> list[date]:
    """`count` weekday-only dates starting at `anchor` (ascending).

    A calendar with weekend gaps proves the lookups count *sessions*, not
    calendar days.
    """
    days: list[date] = []
    cursor = anchor
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


# --- find_target_trading_day (backward, behavior preserved) ------------------


class TestFindTargetTradingDay:
    def test_returns_the_session_horizon_days_before_as_of(
        self, market_store: MarketStore
    ) -> None:
        days = _consecutive_days(date(2026, 7, 1), 30)
        _seed_calendar(market_store, days)

        assert find_target_trading_day(market_store, BENCHMARK, days[-1], 5) == days[-6]

    def test_counts_sessions_not_calendar_days(self, market_store: MarketStore) -> None:
        days = _weekday_days(date(2026, 7, 1), 30)
        _seed_calendar(market_store, days)

        assert find_target_trading_day(market_store, BENCHMARK, days[-1], 5) == days[-6]

    def test_returns_none_when_the_calendar_is_shorter_than_the_horizon(
        self, market_store: MarketStore
    ) -> None:
        days = _consecutive_days(date(2026, 7, 1), 5)
        _seed_calendar(market_store, days)

        # 5 sessions back needs 6 distinct days; only 5 exist.
        assert find_target_trading_day(market_store, BENCHMARK, days[-1], 5) is None

    def test_returns_none_when_no_benchmark_bars_exist(
        self, market_store: MarketStore
    ) -> None:
        assert (
            find_target_trading_day(market_store, BENCHMARK, date(2026, 7, 24), 5)
            is None
        )


# --- find_maturity_trading_day (forward, new) -------------------------------


class TestFindMaturityTradingDay:
    def test_returns_the_session_horizon_days_after_run_date(
        self, market_store: MarketStore
    ) -> None:
        days = _consecutive_days(date(2026, 7, 1), 30)
        _seed_calendar(market_store, days)

        maturity = find_maturity_trading_day(
            market_store, BENCHMARK, days[0], 5, as_of=days[-1]
        )

        assert maturity == days[5]

    def test_counts_sessions_not_calendar_days(self, market_store: MarketStore) -> None:
        days = _weekday_days(date(2026, 7, 1), 30)
        _seed_calendar(market_store, days)

        maturity = find_maturity_trading_day(
            market_store, BENCHMARK, days[0], 5, as_of=days[-1]
        )

        assert maturity == days[5]

    def test_returns_none_when_maturity_has_not_been_reached_by_as_of(
        self, market_store: MarketStore
    ) -> None:
        days = _consecutive_days(date(2026, 7, 1), 30)
        _seed_calendar(market_store, days)

        # `as_of` sits one session before the 5-day maturity of days[0].
        assert (
            find_maturity_trading_day(
                market_store, BENCHMARK, days[0], 5, as_of=days[4]
            )
            is None
        )

    def test_returns_the_maturity_exactly_at_the_as_of_boundary(
        self, market_store: MarketStore
    ) -> None:
        days = _consecutive_days(date(2026, 7, 1), 30)
        _seed_calendar(market_store, days)

        assert (
            find_maturity_trading_day(
                market_store, BENCHMARK, days[0], 5, as_of=days[5]
            )
            == days[5]
        )

    def test_ignores_bars_dated_after_as_of(self, market_store: MarketStore) -> None:
        days = _consecutive_days(date(2026, 7, 1), 30)
        _seed_calendar(market_store, days)

        # Sessions exist well past `as_of`, but the point-in-time clamp must
        # hide them, so the 5d maturity of days[3] is not yet observable.
        assert (
            find_maturity_trading_day(
                market_store, BENCHMARK, days[3], 5, as_of=days[7]
            )
            is None
        )

    def test_returns_none_when_run_date_is_not_itself_a_trading_day(
        self, market_store: MarketStore
    ) -> None:
        days = _weekday_days(date(2026, 7, 1), 30)
        _seed_calendar(market_store, days)
        saturday = date(2026, 7, 4)
        assert saturday.weekday() == 5

        assert (
            find_maturity_trading_day(
                market_store, BENCHMARK, saturday, 5, as_of=days[-1]
            )
            is None
        )

    def test_returns_none_when_no_benchmark_bars_exist(
        self, market_store: MarketStore
    ) -> None:
        assert (
            find_maturity_trading_day(
                market_store, BENCHMARK, date(2026, 7, 1), 5, as_of=date(2026, 8, 1)
            )
            is None
        )


class TestCalendarRoundTrip:
    @pytest.mark.parametrize("horizon_days", [5, 20])
    def test_maturity_then_target_returns_the_original_run_date(
        self, market_store: MarketStore, horizon_days: int
    ) -> None:
        days = _weekday_days(date(2026, 3, 2), 80)
        _seed_calendar(market_store, days)
        run_date = days[0]
        as_of = days[-1]

        maturity = find_maturity_trading_day(
            market_store, BENCHMARK, run_date, horizon_days, as_of=as_of
        )
        assert maturity is not None

        assert (
            find_target_trading_day(market_store, BENCHMARK, maturity, horizon_days)
            == run_date
        )

    @pytest.mark.parametrize("horizon_days", [5, 20])
    def test_target_then_maturity_returns_the_original_as_of(
        self, market_store: MarketStore, horizon_days: int
    ) -> None:
        days = _weekday_days(date(2026, 3, 2), 80)
        _seed_calendar(market_store, days)
        as_of = days[-1]

        target = find_target_trading_day(market_store, BENCHMARK, as_of, horizon_days)
        assert target is not None

        assert (
            find_maturity_trading_day(
                market_store, BENCHMARK, target, horizon_days, as_of=as_of
            )
            == as_of
        )


# --- compute_forward_return --------------------------------------------------


class TestComputeForwardReturn:
    def test_returns_the_hand_calculated_percentage(
        self, market_store: MarketStore
    ) -> None:
        run_date, end = date(2026, 7, 1), date(2026, 7, 10)
        market_store.write_bars(_bars("AAPL", {run_date: 100.0, end: 101.5}))

        assert compute_forward_return(market_store, "AAPL", run_date, end) == (
            pytest.approx(1.5)
        )

    def test_ignores_a_bar_dated_after_the_endpoint(
        self, market_store: MarketStore
    ) -> None:
        run_date, end = date(2026, 7, 1), date(2026, 7, 10)
        market_store.write_bars(
            _bars(
                "AAPL",
                {run_date: 100.0, end: 101.5, end + timedelta(days=1): 999_999.0},
            )
        )

        assert compute_forward_return(market_store, "AAPL", run_date, end) == (
            pytest.approx(1.5)
        )

    def test_returns_none_when_the_symbol_has_no_bars(
        self, market_store: MarketStore
    ) -> None:
        assert (
            compute_forward_return(
                market_store, "MISSING", date(2026, 7, 1), date(2026, 7, 10)
            )
            is None
        )

    def test_returns_none_when_either_endpoint_bar_is_missing(
        self, market_store: MarketStore
    ) -> None:
        run_date, end = date(2026, 7, 1), date(2026, 7, 10)
        market_store.write_bars(_bars("GAPPY", {run_date + timedelta(days=1): 100.0}))

        assert compute_forward_return(market_store, "GAPPY", run_date, end) is None

    def test_returns_none_when_the_run_date_close_is_zero(
        self, market_store: MarketStore
    ) -> None:
        run_date, end = date(2026, 7, 1), date(2026, 7, 10)
        market_store.write_bars(_bars("ZERO", {run_date: 0.0, end: 10.0}))

        assert compute_forward_return(market_store, "ZERO", run_date, end) is None

    @pytest.mark.parametrize(
        "bad_close",
        [
            pytest.param(float("nan"), id="nan"),
            pytest.param(float("inf"), id="inf"),
            pytest.param(float("-inf"), id="-inf"),
        ],
    )
    @pytest.mark.parametrize("bad_endpoint", ["run_date", "as_of"])
    def test_returns_none_when_either_close_is_not_finite(
        self, market_store: MarketStore, bad_close: float, bad_endpoint: str
    ) -> None:
        """A row that *exists* with a non-finite close is a data-quality skip.

        The row-presence checks pass here, so without the finite guard the
        function returns a `NaN`/`inf` float that `verdict_outcomes`'
        `DOUBLE NOT NULL` column happily stores (Issue #206).

        The row is planted past `write_bars`' own finite guard (Issue #227):
        since that guard exists, storage can only hold such a row as history
        written before it, which is precisely what this reader defends.
        """
        run_date, end = date(2026, 7, 1), date(2026, 7, 10)
        prices = {run_date: 100.0, end: 101.5}
        prices[run_date if bad_endpoint == "run_date" else end] = bad_close
        plant_non_finite_bars(market_store, _bars("BROKEN", prices))

        assert compute_forward_return(market_store, "BROKEN", run_date, end) is None


class TestForwardReturnAcrossASplit:
    """Issue #413: a split inside the horizon must not read as a -50% day.

    No production change was needed for this — `read_bars` now returns the
    basis visible at the maturity date, so both endpoints of the ratio are
    quoted the same way. The test exists because the arithmetic silently
    depended on that and nothing pinned it.
    """

    def test_a_split_between_run_date_and_maturity_yields_the_real_return(
        self, market_store: MarketStore
    ) -> None:
        run_date, maturity = date(2026, 7, 29), date(2026, 8, 26)
        # As-traded: 97.23 before the 2:1, 47.81 after -- economically about
        # -1.7%, not the -50.8% a mixed basis produced.
        market_store.write_bars(_bars("MNST", {run_date: 97.23, maturity: 47.81}))
        market_store.write_corporate_actions(
            pd.DataFrame(
                {
                    "symbol": ["MNST"],
                    "ex_date": [date(2026, 8, 11)],
                    "kind": ["split"],
                    "value": [2.0],
                }
            ),
            provider="test",
            fetched_at=datetime(2026, 8, 26, tzinfo=UTC),
        )

        assert compute_forward_return(
            market_store, "MNST", run_date, maturity
        ) == pytest.approx((47.81 - 97.23 / 2) / (97.23 / 2) * 100)

    def test_before_the_ex_date_matures_the_return_is_computed_raw(
        self, market_store: MarketStore
    ) -> None:
        """Same rows, a maturity before the split: no adjustment applies."""
        run_date, maturity = date(2026, 7, 29), date(2026, 8, 10)
        market_store.write_bars(_bars("MNST", {run_date: 97.23, maturity: 90.36}))
        market_store.write_corporate_actions(
            pd.DataFrame(
                {
                    "symbol": ["MNST"],
                    "ex_date": [date(2026, 8, 11)],
                    "kind": ["split"],
                    "value": [2.0],
                }
            ),
            provider="test",
            fetched_at=datetime(2026, 8, 26, tzinfo=UTC),
        )

        assert compute_forward_return(
            market_store, "MNST", run_date, maturity
        ) == pytest.approx((90.36 - 97.23) / 97.23 * 100)
