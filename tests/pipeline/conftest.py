"""Shared fixtures and fakes for the daily-pipeline test split (Issue #400).

Split out of the former tests/pipeline/test_daily_core.py: content used by
both tests/pipeline/test_daily_steps.py (step behavior) and
tests/pipeline/test_daily_runner.py (lifecycle).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from swing_copilot.config import (
    StrategiesConfig,
)
from swing_copilot.pipeline.daily import (
    DailyDependencies,
)
from swing_copilot.screening import (
    fundamental_filters as _fundamental_filters,  # noqa: F401 - imported for its @register_filter side effect
)
from swing_copilot.screening import (
    technical_signals as _technical_signals,  # noqa: F401 - imported for its @register_signal side effect
)
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import (
    MarketStore,
)
from swing_copilot.storage.state_store import StateStore
from swing_copilot.universe import UniverseMember
from tests.support.fakes import FixedClock, StubDataProvider

AS_OF = date(2027, 3, 1)
#: The fixed `now()` every `FixedClock(AS_OF, _NOW)` below returns.
_NOW = datetime(2027, 3, 1, 12, tzinfo=UTC)


class FakeMonotonic:
    """Returns each value in order, then repeats the last one forever.

    Mirrors a real monotonic clock: once "time" has passed a fixed point
    (e.g. the NFR-03 deadline), it never goes back before it.
    """

    def __init__(self, *values: float):
        self._values = list(values)
        self._index = 0

    def __call__(self) -> float:
        value = self._values[min(self._index, len(self._values) - 1)]
        self._index += 1
        return value


def _bars_for(
    symbols: list[str], as_of: date, days: int = 210, volume: int = 2_000_000
) -> pd.DataFrame:
    rows = []
    for symbol in symbols:
        for i in range(days):
            bar_date = as_of - timedelta(days=days - i)
            price = 100.0 + i * 0.1
            rows.append(
                {
                    "symbol": symbol,
                    "date": bar_date,
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price,
                    "volume": volume,
                }
            )
    return pd.DataFrame(rows)


def _member(symbol: str) -> UniverseMember:
    return UniverseMember(
        symbol=symbol,
        company_name=symbol,
        gics_sector="Information Technology",
        source_symbol=symbol,
    )


STRATEGIES_CONFIG = StrategiesConfig.model_validate(
    {
        "strategies": {
            "default": {
                "filters_all": ["volume_min"],
                "signals_all": ["trend_sma"],
                "candidate_limit": 10,
            }
        }
    }
)


@pytest.fixture
def market_store(tmp_path):
    return MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )


@pytest.fixture
def state_store(tmp_path):
    store = StateStore(Database(tmp_path / "copilot.duckdb"))
    store.init_schema()
    return store


@pytest.fixture
def deps(settings, market_store, state_store, tmp_path):
    universe = (_member("AAPL"), _member("MSFT"))
    bars = _bars_for(["AAPL", "MSFT"], AS_OF)
    return DailyDependencies(
        data_provider=StubDataProvider(bars),
        market_store=market_store,
        state_store=state_store,
        settings=settings,
        universe=universe,
        strategies_config=STRATEGIES_CONFIG,
        clock=FixedClock(AS_OF, _NOW),
        edgar_client=None,
        output_dir=str(tmp_path / "reports"),
    )
