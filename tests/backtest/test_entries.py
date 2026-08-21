"""Tests for the shared planned-entry price and daily limit approximation."""

from __future__ import annotations

import pytest

from swing_copilot.backtest.entries import entry_limit_price, evaluate_entry_fill


def test_entry_limit_price_is_close_plus_atr_multiple() -> None:
    assert entry_limit_price(100.0, 2.0, 0.5) == pytest.approx(101.0)


@pytest.mark.parametrize(
    ("open_price", "low", "expected"),
    [
        (100.0, 99.0, 100.1),  # open at/below the limit: existing slippage path
        (105.0, 100.0, 101.0),  # gap up, then an intraday touch at the limit
        (105.0, 102.0, None),  # the Day order was never reached
    ],
)
def test_evaluate_entry_fill_uses_daily_limit_semantics(
    open_price: float, low: float, expected: float | None
) -> None:
    actual = evaluate_entry_fill(
        open_price=open_price,
        low=low,
        limit_price=101.0,
        slippage_pct=0.001,
    )
    if expected is None:
        assert actual is None
    else:
        assert actual == pytest.approx(expected)
