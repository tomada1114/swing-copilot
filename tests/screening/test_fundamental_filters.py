"""Tests for the profitability/FCF/equity fundamental filter (FR-04)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from swing_copilot.screening.base import ScreeningInput
from swing_copilot.screening.fundamental_filters import (
    ProfitablePositiveFCFEquityFilter,
)
from tests.screening.conftest import FundamentalsSpec, make_fundamentals_row

AS_OF = date(2026, 7, 20)


_QUARTER_ENDS = [
    date(2025, 3, 31),
    date(2025, 6, 30),
    date(2025, 9, 30),
    date(2025, 12, 31),
]
_QUARTER_FILED_ATS = [
    datetime(2025, 4, 15, tzinfo=UTC),
    datetime(2025, 7, 15, tzinfo=UTC),
    datetime(2025, 10, 15, tzinfo=UTC),
    datetime(2026, 1, 15, tzinfo=UTC),
]


def _quarterly_rows(
    symbol: str, net_incomes: list[float], **overrides: float
) -> list[dict[str, object]]:
    rows = []
    for i, net_income in enumerate(net_incomes):
        spec = FundamentalsSpec(
            accession_no=f"acc-{symbol}-{i}",
            fiscal_period_end=_QUARTER_ENDS[i],
            filed_at=_QUARTER_FILED_ATS[i],
            net_income=net_income,
            fcf=overrides.get("fcf", 10.0),
            equity=overrides.get("equity", 60.0),
            assets=overrides.get("assets", 100.0),
        )
        rows.append(make_fundamentals_row(symbol, spec))
    return rows


@pytest.fixture
def base_input(settings):
    def _build(fundamentals_rows: list[dict[str, object]]) -> ScreeningInput:
        return ScreeningInput(
            as_of=AS_OF,
            universe=(),
            fundamentals=pd.DataFrame(fundamentals_rows),
            bars=pd.DataFrame(
                columns=["symbol", "date", "open", "high", "low", "close", "volume"]
            ),
        )

    return _build


class TestProfitablePositiveFCFEquityFilter:
    def test_passes_when_all_conditions_met(self, settings, base_input):
        rows = _quarterly_rows("AAPL", [10.0, 10.0, 10.0, 10.0])
        data = base_input(rows)
        result = ProfitablePositiveFCFEquityFilter(settings).apply(data)
        assert result == {"AAPL"}

    def test_fails_when_a_quarter_is_unprofitable(self, settings, base_input):
        rows = _quarterly_rows("AAPL", [10.0, -5.0, 10.0, 10.0])
        data = base_input(rows)
        result = ProfitablePositiveFCFEquityFilter(settings).apply(data)
        assert result == set()

    def test_fails_when_fewer_than_required_quarters_available(
        self, settings, base_input
    ):
        rows = _quarterly_rows("AAPL", [10.0, 10.0])
        data = base_input(rows)
        result = ProfitablePositiveFCFEquityFilter(settings).apply(data)
        assert result == set()

    def test_fails_when_latest_fcf_is_negative(self, settings, base_input):
        rows = _quarterly_rows("AAPL", [10.0, 10.0, 10.0, 10.0], fcf=-1.0)
        data = base_input(rows)
        result = ProfitablePositiveFCFEquityFilter(settings).apply(data)
        assert result == set()

    def test_fails_when_equity_ratio_at_or_below_threshold(self, settings, base_input):
        # equity/assets = 30/100 = 0.30, threshold is > 0.30 (strictly greater)
        rows = _quarterly_rows(
            "AAPL", [10.0, 10.0, 10.0, 10.0], equity=30.0, assets=100.0
        )
        data = base_input(rows)
        result = ProfitablePositiveFCFEquityFilter(settings).apply(data)
        assert result == set()

    def test_fails_when_assets_are_zero(self, settings, base_input):
        rows = _quarterly_rows("AAPL", [10.0, 10.0, 10.0, 10.0], equity=0.0, assets=0.0)
        data = base_input(rows)
        result = ProfitablePositiveFCFEquityFilter(settings).apply(data)
        assert result == set()

    def test_passes_just_above_equity_ratio_threshold(self, settings, base_input):
        rows = _quarterly_rows(
            "AAPL", [10.0, 10.0, 10.0, 10.0], equity=30.1, assets=100.0
        )
        data = base_input(rows)
        result = ProfitablePositiveFCFEquityFilter(settings).apply(data)
        assert result == {"AAPL"}

    def test_excludes_filings_filed_after_as_of(self, settings, base_input):
        rows = _quarterly_rows("AAPL", [10.0, 10.0, 10.0, 10.0])
        rows.append(
            make_fundamentals_row(
                "AAPL",
                FundamentalsSpec(
                    accession_no="acc-future",
                    fiscal_period_end=date(2026, 6, 30),
                    filed_at=datetime(2026, 7, 25, tzinfo=UTC),
                    net_income=-999.0,
                    fcf=-999.0,
                    equity=1.0,
                    assets=1000.0,
                ),
            )
        )
        data = base_input(rows)
        result = ProfitablePositiveFCFEquityFilter(settings).apply(data)
        assert result == {"AAPL"}

    def test_empty_fundamentals_returns_empty_set(self, settings, base_input):
        data = base_input([])
        result = ProfitablePositiveFCFEquityFilter(settings).apply(data)
        assert result == set()

    def test_amended_filing_for_same_period_replaces_original(
        self, settings, base_input
    ):
        rows = _quarterly_rows("AAPL", [10.0, 10.0, 10.0, -5.0])
        # Amend the most recent (unprofitable) period with a corrected, profitable value.
        rows.append(
            make_fundamentals_row(
                "AAPL",
                FundamentalsSpec(
                    accession_no="acc-AAPL-3-amended",
                    fiscal_period_end=_QUARTER_ENDS[-1],
                    filed_at=datetime(2026, 6, 1, tzinfo=UTC),
                    net_income=5.0,
                    fcf=10.0,
                    equity=60.0,
                    assets=100.0,
                ),
            )
        )
        data = base_input(rows)
        result = ProfitablePositiveFCFEquityFilter(settings).apply(data)
        assert result == {"AAPL"}
