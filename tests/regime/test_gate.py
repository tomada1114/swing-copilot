"""Contract tests for Issue #22's market gate."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from swing_copilot.regime.distribution import DataQuality, DistributionLevel
from swing_copilot.regime.gate import (
    GateVerdict,
    calculate_regime_snapshot,
    evaluate_market_gate,
)
from swing_copilot.screening.indicators import ema


class TestEma:
    def test_seeds_at_first_complete_sma_and_requires_two_periods(self) -> None:
        prices = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])

        result = ema(prices, period=3)

        assert result.iloc[:5].isna().all()
        assert result.iloc[5] == pytest.approx(5.0)


class TestMarketGate:
    @pytest.mark.parametrize(
        ("spy_close", "sma200", "vix_close", "expected"),
        [
            (501.0, 500.0, 19.9, GateVerdict.BULL),
            (500.0, 500.0, 19.0, GateVerdict.BULL),
            (490.0, 500.0, 20.0, GateVerdict.NEUTRAL),
            (485.0, 500.0, 25.0, GateVerdict.NEUTRAL),
            (484.9, 500.0, 25.0, GateVerdict.BEAR),
            (510.0, 500.0, 30.0, GateVerdict.BULL),
            (510.0, 500.0, 30.1, GateVerdict.BULL),
        ],
    )
    def test_classifies_gate_with_strict_boundaries(
        self, spy_close: float, sma200: float, vix_close: float, expected: GateVerdict
    ) -> None:
        gate = evaluate_market_gate(spy_close, sma200, vix_close)
        assert gate.verdict is expected
        assert gate.is_panic == (vix_close > 30.0)

    def test_preserves_panic_flag_when_trend_input_is_missing(self) -> None:
        gate = evaluate_market_gate(None, None, 30.1)

        assert gate.verdict is GateVerdict.UNKNOWN
        assert gate.is_panic


def _index_bars(symbol: str, closes: list[float], *, start: date) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": symbol,
            "date": [start + timedelta(days=index) for index in range(len(closes))],
            "close": closes,
            "volume": [1_000_000 + index for index in range(len(closes))],
        }
    )


class TestRegimeSnapshot:
    def test_sma_uses_before_exact_and_after_as_of_rows(self) -> None:
        start = date(2026, 1, 1)
        dates = [start + timedelta(days=index) for index in range(202)]
        spy = _index_bars("SPY", [100.0] * 200 + [90.0, 100.0], start=start)
        qqq = _index_bars("QQQ", [100.0] * 202, start=start)
        vix = _index_bars("^VIX", [15.0] * 202, start=start)

        before = calculate_regime_snapshot(spy, qqq, vix, dates[199])
        exact = calculate_regime_snapshot(spy, qqq, vix, dates[200])
        after = calculate_regime_snapshot(spy, qqq, vix, dates[201])

        assert before.gate.verdict is GateVerdict.BULL
        assert exact.gate.verdict is GateVerdict.BEAR
        assert after.gate.verdict is GateVerdict.BULL

    def test_uses_only_as_of_data_and_combines_strictest_index_level(self) -> None:
        start = date(2026, 1, 1)
        spy = _index_bars(
            "SPY", [float(100 + index) for index in range(220)], start=start
        )
        qqq_closes = [100.0] * 220
        qqq_volumes = [100] * 220
        for index in (210, 212, 214):
            qqq_closes[index] = 99.8
            qqq_volumes[index] = 101 + index
        qqq = _index_bars("QQQ", qqq_closes, start=start)
        qqq["volume"] = qqq_volumes
        vix = pd.concat(
            [
                _index_bars("^VIX", [15.0] * 220, start=start),
                pd.DataFrame(
                    {
                        "symbol": ["^VIX"],
                        "date": [start + timedelta(days=220)],
                        "close": [35.0],
                        "volume": [9_999_999],
                    }
                ),
            ],
            ignore_index=True,
        )

        snapshot = calculate_regime_snapshot(spy, qqq, vix, start + timedelta(days=219))

        assert snapshot.gate.verdict is GateVerdict.BULL
        assert snapshot.qqq_distribution.level is DistributionLevel.HIGH
        assert snapshot.dd_level is DistributionLevel.HIGH
        assert snapshot.data_quality is DataQuality.OK

    def test_missing_index_data_returns_unknown_and_insufficient(self) -> None:
        empty = pd.DataFrame(columns=["symbol", "date", "close", "volume"])
        snapshot = calculate_regime_snapshot(empty, empty, empty, date(2026, 1, 1))

        assert snapshot.gate.verdict is GateVerdict.UNKNOWN
        assert snapshot.dd_level is DistributionLevel.UNKNOWN
        assert snapshot.data_quality is DataQuality.INSUFFICIENT
