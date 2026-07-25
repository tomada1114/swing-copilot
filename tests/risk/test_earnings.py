"""Business-day boundaries for the earnings-proximity guard (P4-18)."""

from __future__ import annotations

from datetime import date

import pytest

from swing_copilot.risk.earnings import evaluate_earnings_proximity


@pytest.mark.parametrize(
    ("earnings_date", "expected_days", "expected_status"),
    [
        pytest.param(date(2026, 7, 22), 1, "block", id="one-business-day"),
        pytest.param(date(2026, 7, 23), 2, "block", id="two-business-days"),
        pytest.param(date(2026, 7, 24), 3, "warn", id="three-business-days"),
        pytest.param(date(2026, 7, 28), 5, "warn", id="five-business-days"),
        pytest.param(date(2026, 7, 29), 6, "clear", id="six-business-days"),
    ],
)
def test_one_two_three_five_six_business_day_boundaries(
    earnings_date, expected_days, expected_status
):
    result = evaluate_earnings_proximity(
        date(2026, 7, 21),
        earnings_date,
        block_business_days=2,
        warn_business_days=5,
    )

    assert result.business_days == expected_days
    assert result.status == expected_status


def test_unknown_date_is_explicit_warning():
    result = evaluate_earnings_proximity(
        date(2026, 7, 21),
        None,
        block_business_days=2,
        warn_business_days=5,
    )
    assert result.status == "unknown"
    assert result.business_days is None
