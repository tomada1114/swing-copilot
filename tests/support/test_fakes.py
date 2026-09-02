"""Pins each shared fake against the real Protocol it stands in for (Issue #398).

Each assertion below is a typed assignment, not `isinstance`: these Protocols
are structural, and mypy strict already checks assignment compatibility, so a
fake that drifts from its Protocol's shape fails `just lint`'s mypy pass on
this file before any test that constructs one would even notice.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pandas as pd

from swing_copilot.data.base import BARS_COLUMNS, DataProvider
from tests.support.fakes import (
    FixedClock,
    StubCalendarClient,
    StubDataProvider,
    StubEdgarClient,
    StubNewsClient,
)

if TYPE_CHECKING:
    from swing_copilot.clock import Clock
    from swing_copilot.pipeline.daily import (
        _CalendarClientLike,
        _EdgarClientLike,
        _NewsClientLike,
    )

_AS_OF = date(2027, 3, 1)
_NOW = datetime(2027, 3, 1, 12, tzinfo=UTC)


def test_fixed_clock_satisfies_clock() -> None:
    fixed = FixedClock(_AS_OF, _NOW)
    clock: Clock = fixed
    assert clock.today() == _AS_OF
    assert clock.now() == _NOW


def test_stub_data_provider_satisfies_data_provider() -> None:
    stub = StubDataProvider(pd.DataFrame(columns=BARS_COLUMNS))
    provider: DataProvider = stub
    result = provider.get_daily_bars(["AAPL"], date(2027, 1, 1), date(2027, 1, 2))
    assert result.failures == ()
    assert provider.get_latest_bars(["AAPL"], _AS_OF).bars.empty


def test_stub_news_client_satisfies_news_client_like() -> None:
    stub = StubNewsClient()
    news_client: _NewsClientLike = stub
    items = news_client.fetch_company_news("AAPL", date(2027, 2, 1), as_of=_AS_OF)
    assert [item.symbol for item in items] == ["AAPL"]


def test_stub_calendar_client_satisfies_calendar_client_like() -> None:
    stub = StubCalendarClient()
    calendar_client: _CalendarClientLike = stub
    assert (
        calendar_client.fetch_calendar_events(
            date(2027, 2, 1), date(2027, 2, 5), as_of=_AS_OF
        )
        == []
    )


def test_stub_edgar_client_satisfies_edgar_client_like() -> None:
    stub = StubEdgarClient()
    edgar_client: _EdgarClientLike = stub
    assert edgar_client.fetch_fundamentals("AAPL", _NOW) == []
    assert edgar_client.fetch_filing_texts("AAPL", ["10-Q"], as_of=_NOW) == []
