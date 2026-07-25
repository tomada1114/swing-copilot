"""Tests for the independent rejection classifier (Issue #11, P1-02)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from swing_copilot.screening.base import (
    RejectionReasonCode,
    RejectionStage,
    ScreeningInput,
    SignalHit,
)
from swing_copilot.screening.rejection_classifier import (
    RejectionPlan,
    classify_rejections,
)
from swing_copilot.universe import UniverseMember
from tests.screening.conftest import FundamentalsSpec, make_bars, make_fundamentals_row

if TYPE_CHECKING:
    from collections.abc import Sequence

    from swing_copilot.config import Settings
    from swing_copilot.screening.base import RejectionRecord

AS_OF = date(2026, 7, 21)

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


def _member(symbol: str) -> UniverseMember:
    return UniverseMember(
        symbol=symbol,
        company_name=symbol,
        gics_sector="Information Technology",
        source_symbol=symbol,
    )


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


def _healthy_fundamentals(symbol: str) -> list[dict[str, object]]:
    return _quarterly_rows(symbol, [10.0, 10.0, 10.0, 10.0])


def _uptrend_closes(days: int, base: float = 100.0) -> list[float]:
    return [base + 0.5 * i for i in range(days)]


def _liquid_bars(symbol: str, days: int = 210, base: float = 100.0) -> pd.DataFrame:
    return make_bars(
        symbol,
        _uptrend_closes(days, base=base),
        start=date(2026, 1, 1),
        volume=2_000_000,
    )


def _input(
    universe: tuple[UniverseMember, ...],
    fundamentals_rows: list[dict[str, object]],
    bars: pd.DataFrame,
) -> ScreeningInput:
    return ScreeningInput(
        as_of=AS_OF,
        universe=universe,
        fundamentals=pd.DataFrame(fundamentals_rows),
        bars=bars,
    )


def _classify(
    data: ScreeningInput,
    settings: Settings,
    *,
    candidate_symbols: set[str] | None = None,
    signal_order: Sequence[str] = ("trend_sma", "pullback_rsi"),
    hits_by_signal: list[list[SignalHit]] | None = None,
) -> list[RejectionRecord]:
    return classify_rejections(
        data,
        settings,
        candidate_symbols=candidate_symbols or set(),
        plan=RejectionPlan(
            filter_order=("profitable_positive_fcf_equity", "volume_min"),
            signal_order=tuple(signal_order),
            hits_by_signal=tuple(
                tuple(hits)
                for hits in (hits_by_signal if hits_by_signal is not None else [[], []])
            ),
        ),
    )


class TestDataInsufficientHistoryFundamentals:
    # REQ-001, REQ-003: data_quality/DATA_INSUFFICIENT_HISTORY from too few
    # filed quarters.
    def test_fewer_than_required_quarters_is_data_quality(self, settings):
        rows = _quarterly_rows("XYZ", [10.0, 10.0])
        data = _input((_member("XYZ"),), rows, _liquid_bars("XYZ"))

        [rejection] = _classify(data, settings)

        assert rejection.symbol == "XYZ"
        assert rejection.stage is RejectionStage.DATA_QUALITY
        assert rejection.reason_code is RejectionReasonCode.DATA_INSUFFICIENT_HISTORY
        assert rejection.detail == {"available_quarters": 2, "required_quarters": 4}

    def test_no_fundamentals_rows_at_all_is_data_quality(self, settings):
        data = _input((_member("XYZ"),), [], _liquid_bars("XYZ"))

        [rejection] = _classify(data, settings)

        assert rejection.reason_code is RejectionReasonCode.DATA_INSUFFICIENT_HISTORY
        assert rejection.detail == {"available_quarters": 0, "required_quarters": 4}

    def test_filing_exactly_at_as_of_cutoff_counts(self, settings):
        # as-of boundary: filed_at exactly at day-end of `as_of` is included,
        # so the quarter-count reason must not fire (whatever else the
        # symbol is rejected for, e.g. no signal hits configured here).
        rows = _quarterly_rows("XYZ", [10.0, 10.0, 10.0, 10.0])
        rows[-1]["filed_at"] = datetime(2026, 7, 21, 23, 59, 59, tzinfo=UTC)
        data = _input((_member("XYZ"),), rows, _liquid_bars("XYZ"))

        [rejection] = _classify(data, settings)

        assert "available_quarters" not in rejection.detail

    def test_filing_one_second_after_as_of_cutoff_is_excluded(self, settings):
        rows = _quarterly_rows("XYZ", [10.0, 10.0, 10.0, 10.0])
        rows[-1]["filed_at"] = datetime(2026, 7, 22, 0, 0, 0, tzinfo=UTC)
        data = _input((_member("XYZ"),), rows, _liquid_bars("XYZ"))

        [rejection] = _classify(data, settings)

        assert rejection.reason_code is RejectionReasonCode.DATA_INSUFFICIENT_HISTORY
        assert rejection.detail == {"available_quarters": 3, "required_quarters": 4}


class TestFundamentalFilterReasons:
    def test_negative_net_income_uses_latest_value(self, settings):
        # Issue Example 1.
        rows = _quarterly_rows("XYZ", [10.0, 10.0, 10.0, -500000.0])
        data = _input((_member("XYZ"),), rows, _liquid_bars("XYZ"))

        [rejection] = _classify(data, settings)

        assert rejection.stage is RejectionStage.FUNDAMENTAL_FILTER
        assert rejection.reason_code is RejectionReasonCode.FILTER_NEGATIVE_NET_INCOME
        assert rejection.detail == {"net_income": -500000.0, "threshold": 0}

    def test_missing_net_income_reports_null_not_nan(self, settings):
        # Regression: a real EDGAR data gap (NaN net_income on the latest
        # filed quarter) must not reach json_guard.dumps_safe() as a raw
        # non-finite float — mirror the fcf/equity_ratio null-reporting
        # convention already used for the same situation just below.
        rows = _quarterly_rows("XYZ", [10.0, 10.0, 10.0, float("nan")])
        data = _input((_member("XYZ"),), rows, _liquid_bars("XYZ"))

        [rejection] = _classify(data, settings)

        assert rejection.reason_code is RejectionReasonCode.FILTER_NEGATIVE_NET_INCOME
        assert rejection.detail == {"net_income": None, "threshold": 0}

    def test_negative_fcf(self, settings):
        rows = _healthy_fundamentals("XYZ")
        for row in rows:
            row["fcf"] = -1.0
        data = _input((_member("XYZ"),), rows, _liquid_bars("XYZ"))

        [rejection] = _classify(data, settings)

        assert rejection.reason_code is RejectionReasonCode.FILTER_NEGATIVE_FCF
        assert rejection.detail == {"fcf": -1.0, "threshold": 0}

    def test_missing_fcf_reports_null(self, settings):
        rows = _healthy_fundamentals("XYZ")
        for row in rows:
            row["fcf"] = float("nan")
        data = _input((_member("XYZ"),), rows, _liquid_bars("XYZ"))

        [rejection] = _classify(data, settings)

        assert rejection.reason_code is RejectionReasonCode.FILTER_NEGATIVE_FCF
        assert rejection.detail == {"fcf": None, "threshold": 0}

    def test_low_equity_ratio_matches_issue_worked_example(self, settings):
        # Issue's own Key Values example: equity_ratio 0.24, threshold 0.30.
        rows = _quarterly_rows(
            "XYZ", [10.0, 10.0, 10.0, 10.0], equity=24.0, assets=100.0
        )
        data = _input((_member("XYZ"),), rows, _liquid_bars("XYZ"))

        [rejection] = _classify(data, settings)

        assert rejection.reason_code is RejectionReasonCode.FILTER_LOW_EQUITY_RATIO
        assert rejection.detail == {
            "equity_ratio": pytest.approx(0.24),
            "threshold": 0.30,
        }

    def test_equity_ratio_exactly_at_threshold_is_rejected(self, settings):
        rows = _quarterly_rows(
            "XYZ", [10.0, 10.0, 10.0, 10.0], equity=30.0, assets=100.0
        )
        data = _input((_member("XYZ"),), rows, _liquid_bars("XYZ"))

        [rejection] = _classify(data, settings)

        assert rejection.reason_code is RejectionReasonCode.FILTER_LOW_EQUITY_RATIO
        assert rejection.detail["equity_ratio"] == pytest.approx(0.30)

    def test_zero_assets_reports_null_equity_ratio(self, settings):
        rows = _quarterly_rows("XYZ", [10.0, 10.0, 10.0, 10.0], equity=0.0, assets=0.0)
        data = _input((_member("XYZ"),), rows, _liquid_bars("XYZ"))

        [rejection] = _classify(data, settings)

        assert rejection.reason_code is RejectionReasonCode.FILTER_LOW_EQUITY_RATIO
        assert rejection.detail == {"equity_ratio": None, "threshold": 0.30}


class TestLiquidityDivergenceReason:
    # FILTER_LOW_LIQUIDITY: the deliberate divergence from the issue's enum.
    def test_low_average_volume_is_fundamental_filter_stage(self, settings):
        rows = _healthy_fundamentals("XYZ")
        bars = make_bars("XYZ", _uptrend_closes(30), start=date(2026, 1, 1), volume=100)
        data = _input((_member("XYZ"),), rows, bars)

        [rejection] = _classify(data, settings)

        assert rejection.stage is RejectionStage.FUNDAMENTAL_FILTER
        assert rejection.reason_code is RejectionReasonCode.FILTER_LOW_LIQUIDITY
        assert rejection.detail == {"avg_volume": 100.0, "threshold": 1_000_000}

    def test_avg_volume_exactly_at_threshold_is_rejected(self, settings):
        rows = _healthy_fundamentals("XYZ")
        bars = make_bars(
            "XYZ", _uptrend_closes(30), start=date(2026, 1, 1), volume=1_000_000
        )
        data = _input((_member("XYZ"),), rows, bars)

        [rejection] = _classify(data, settings)

        assert rejection.reason_code is RejectionReasonCode.FILTER_LOW_LIQUIDITY

    def test_insufficient_bars_for_liquidity_is_data_quality(self, settings):
        rows = _healthy_fundamentals("XYZ")
        bars = make_bars("XYZ", _uptrend_closes(5), start=date(2026, 1, 1))
        data = _input((_member("XYZ"),), rows, bars)

        [rejection] = _classify(data, settings)

        assert rejection.stage is RejectionStage.DATA_QUALITY
        assert rejection.reason_code is RejectionReasonCode.DATA_INSUFFICIENT_HISTORY
        assert rejection.detail == {"available_bars": 5, "required_bars": 20}

    def test_no_bars_at_all_for_liquidity_is_data_quality(self, settings):
        rows = _healthy_fundamentals("XYZ")
        data = _input(
            (_member("XYZ"),),
            rows,
            pd.DataFrame(
                columns=["symbol", "date", "open", "high", "low", "close", "volume"]
            ),
        )

        [rejection] = _classify(data, settings)

        assert rejection.detail == {"available_bars": 0, "required_bars": 20}


class TestSignalReasons:
    def test_trend_not_met_reports_close_and_sma_long(self, settings):
        rows = _healthy_fundamentals("XYZ")
        # Downtrend: fails trend_sma (close < sma_long) but has enough
        # history for both signals.
        closes = list(reversed(_uptrend_closes(210)))
        bars = make_bars("XYZ", closes, start=date(2026, 1, 1), volume=2_000_000)
        data = _input((_member("XYZ"),), rows, bars)

        [rejection] = _classify(data, settings)

        assert rejection.stage is RejectionStage.TECHNICAL_SIGNAL
        assert rejection.reason_code is RejectionReasonCode.SIGNAL_TREND_NOT_MET
        assert set(rejection.detail) == {"close", "sma_long"}

    def test_rsi_not_met_matches_issue_example_4(self, settings, monkeypatch):
        # Issue Example 4: rsi14=52 (>= threshold 45) -> SIGNAL_RSI_NOT_MET.
        rows = _healthy_fundamentals("ABC")
        bars = _liquid_bars("ABC")
        # Symbol hits trend_sma (so signal_order[0] passes) but not
        # pullback_rsi (rsi14 pinned above threshold).
        hit_for_trend = [SignalHit("ABC", "trend_sma", "long", 1.0, {})]
        monkeypatch.setattr(
            "swing_copilot.screening.rejection_classifier.wilder_rsi",
            lambda series, _period: pd.Series([52.0] * len(series), index=series.index),
        )
        data = _input((_member("ABC"),), rows, bars)

        [rejection] = _classify(
            data,
            settings,
            hits_by_signal=[hit_for_trend, []],
        )

        assert rejection.reason_code is RejectionReasonCode.SIGNAL_RSI_NOT_MET
        assert rejection.detail == {"rsi14": 52.0, "threshold": 45.0}

    def test_insufficient_bars_for_trend_signal_is_data_quality(self, settings):
        rows = _healthy_fundamentals("XYZ")
        bars = make_bars(
            "XYZ", _uptrend_closes(30), start=date(2026, 1, 1), volume=2_000_000
        )
        data = _input((_member("XYZ"),), rows, bars)

        [rejection] = _classify(data, settings)

        # 30 days of bars is enough to pass volume_min (needs 20) but not
        # enough for trend_sma's SMA200.
        assert rejection.stage is RejectionStage.DATA_QUALITY
        assert rejection.reason_code is RejectionReasonCode.DATA_INSUFFICIENT_HISTORY
        assert rejection.detail == {"available_bars": 30, "required_bars": 200}

    def test_insufficient_bars_for_pullback_signal_is_data_quality(self, settings):
        rows = _healthy_fundamentals("XYZ")
        # 30 days passes volume_min (needs 20) but not pullback_rsi's SMA50
        # window -- only reachable when pullback_rsi is checked before
        # trend_sma (whose own SMA200 requirement would otherwise classify
        # first).
        bars = make_bars(
            "XYZ", _uptrend_closes(30), start=date(2026, 1, 1), volume=2_000_000
        )
        data = _input((_member("XYZ"),), rows, bars)

        [rejection] = _classify(
            data, settings, signal_order=("pullback_rsi", "trend_sma")
        )

        assert rejection.stage is RejectionStage.DATA_QUALITY
        assert rejection.reason_code is RejectionReasonCode.DATA_INSUFFICIENT_HISTORY
        assert rejection.detail == {"available_bars": 30, "required_bars": 50}


class TestPriorityOrder:
    # REQ-040: decisive priority; the first applicable reason wins,
    # deterministically, across repeated runs.
    def test_data_quality_wins_over_fundamental_filter(self, settings):
        # Too few quarters AND (if it had enough) would also fail net_income.
        rows = _quarterly_rows("XYZ", [-5.0, -5.0])
        data = _input((_member("XYZ"),), rows, _liquid_bars("XYZ"))

        [rejection] = _classify(data, settings)

        assert rejection.reason_code is RejectionReasonCode.DATA_INSUFFICIENT_HISTORY

    def test_fundamental_filter_wins_over_liquidity(self, settings):
        rows = _quarterly_rows("XYZ", [10.0, 10.0, 10.0, -5.0])
        bars = make_bars("XYZ", _uptrend_closes(30), start=date(2026, 1, 1), volume=1)
        data = _input((_member("XYZ"),), rows, bars)

        [rejection] = _classify(data, settings)

        assert rejection.reason_code is RejectionReasonCode.FILTER_NEGATIVE_NET_INCOME

    def test_liquidity_wins_over_signal(self, settings):
        rows = _healthy_fundamentals("XYZ")
        # Downtrend (fails trend_sma) AND illiquid.
        closes = list(reversed(_uptrend_closes(210)))
        bars = make_bars("XYZ", closes, start=date(2026, 1, 1), volume=1)
        data = _input((_member("XYZ"),), rows, bars)

        [rejection] = _classify(data, settings)

        assert rejection.reason_code is RejectionReasonCode.FILTER_LOW_LIQUIDITY

    def test_signal_order_determines_which_reason_is_recorded(self, settings):
        # Fails both trend_sma and pullback_rsi; signal_order says trend_sma
        # is evaluated first, so that reason always wins, deterministically.
        rows = _healthy_fundamentals("XYZ")
        closes = list(reversed(_uptrend_closes(210)))
        bars = make_bars("XYZ", closes, start=date(2026, 1, 1), volume=2_000_000)
        data = _input((_member("XYZ"),), rows, bars)

        results = [_classify(data, settings) for _ in range(5)]

        for result in results:
            [rejection] = result
            assert rejection.reason_code is RejectionReasonCode.SIGNAL_TREND_NOT_MET

    def test_reversed_signal_order_changes_the_winning_reason(self, settings):
        # Same symbol, same failure on both signals: with pullback_rsi first
        # in the configured order, that reason now wins instead.
        rows = _healthy_fundamentals("XYZ")
        closes = list(reversed(_uptrend_closes(210)))
        bars = make_bars("XYZ", closes, start=date(2026, 1, 1), volume=2_000_000)
        data = _input((_member("XYZ"),), rows, bars)

        [rejection] = _classify(
            data, settings, signal_order=("pullback_rsi", "trend_sma")
        )

        assert rejection.reason_code is RejectionReasonCode.SIGNAL_RSI_NOT_MET


class TestBoundaryConditions:
    def test_no_signals_configured_returns_no_rejections(self, settings):
        rows = _healthy_fundamentals("XYZ")
        data = _input((_member("XYZ"),), rows, _liquid_bars("XYZ"))

        result = _classify(data, settings, signal_order=())

        assert result == []

    def test_candidate_symbols_are_skipped(self, settings):
        rows = _healthy_fundamentals("XYZ")
        data = _input((_member("XYZ"),), rows, _liquid_bars("XYZ"))

        result = _classify(data, settings, candidate_symbols={"XYZ"})

        assert result == []

    def test_ranked_out_symbol_is_neither_candidate_nor_rejection(self, settings):
        # Judgment call (see rejection_classifier module docstring): a
        # symbol passed to classify_rejections via `candidate_symbols` is
        # never classified, even if the caller's final candidate_limit later
        # excludes it from the actual candidates list.
        rows = _healthy_fundamentals("XYZ")
        bars = _liquid_bars("XYZ")
        data = _input((_member("XYZ"),), rows, bars)
        hits = [SignalHit("XYZ", "trend_sma", "long", 1.0, {})]

        result = _classify(
            data,
            settings,
            candidate_symbols={"XYZ"},
            hits_by_signal=[hits, hits],
        )

        assert result == []

    def test_multiple_universe_symbols_each_get_their_own_record(self, settings):
        rows = [*_healthy_fundamentals("A"), *_quarterly_rows("B", [10.0, 10.0])]
        bars = pd.concat([_liquid_bars("A"), _liquid_bars("B")])
        data = _input((_member("A"), _member("B")), rows, bars)

        result = _classify(data, settings)

        by_symbol = {rejection.symbol: rejection for rejection in result}
        assert set(by_symbol) == {"A", "B"}
        assert (
            by_symbol["B"].reason_code is RejectionReasonCode.DATA_INSUFFICIENT_HISTORY
        )

    def test_unknown_signal_name_raises_not_implemented_error(self, settings):
        rows = _healthy_fundamentals("XYZ")
        data = _input((_member("XYZ"),), rows, _liquid_bars("XYZ"))

        with pytest.raises(NotImplementedError, match="no mirrored logic"):
            _classify(data, settings, signal_order=("made_up_signal",))

    def test_symbol_missing_after_signals_is_classified_as_ranking_data_quality(
        self, settings
    ):
        rows = _healthy_fundamentals("XYZ")
        bars = _liquid_bars("XYZ")
        data = _input((_member("XYZ"),), rows, bars)
        hits = [SignalHit("XYZ", "trend_sma", "long", 1.0, {})]

        [rejection] = _classify(
            data, settings, candidate_symbols=set(), hits_by_signal=[hits, hits]
        )

        assert rejection.stage is RejectionStage.DATA_QUALITY
        assert rejection.reason_code is RejectionReasonCode.DATA_INSUFFICIENT_HISTORY
        assert rejection.detail["ranking_metrics"] == "unavailable"
