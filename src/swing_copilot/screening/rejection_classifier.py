"""Independently classify why non-candidate universe symbols were rejected.

Mirrors — deliberately does not reuse — `ProfitablePositiveFCFEquityFilter`,
`MinAverageVolumeFilter`, `TrendSMASignal`, and `PullbackRSISignal`'s own
threshold logic, the same way `ScreeningPipeline._ranking_metrics()`
independently recomputes ranking metrics from raw bars rather than reusing
signal internals (`docs/04_detailed_design.md` 2.1 #4). This keeps rejection
detail available even for symbols no configured signal ever touches.

This module is deliberately hardcoded to today's two Filters
(`profitable_positive_fcf_equity`, `volume_min`) and two Signals
(`trend_sma`, `pullback_rsi`) — exactly the strategies the closed
`RejectionReasonCode` enum's members imply (Issue #11, P1-02). It does not
generalize to arbitrary future Filters/Signals; extending the enum or adding
a new Filter/Signal to `strategies.yaml` requires extending this classifier
too (out of scope for this issue's "Not in scope" section).
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import TYPE_CHECKING

import pandas as pd

from swing_copilot.screening.base import (
    RejectionReasonCode,
    RejectionRecord,
    RejectionStage,
)
from swing_copilot.screening.indicators import sma, symbol_bars, wilder_rsi

if TYPE_CHECKING:
    from collections.abc import Sequence

    from swing_copilot.config import Settings
    from swing_copilot.screening.base import ScreeningInput, SignalHit

# PullbackRSISignal's own SMA window (technical_signals.py::_PULLBACK_SMA_WINDOW)
# is not settings-driven; duplicated here rather than imported across the
# module boundary, matching this module's "mirror, don't reuse" contract.
_PULLBACK_SMA_WINDOW = 50


def classify_rejections(
    data: ScreeningInput,
    settings: Settings,
    *,
    candidate_symbols: set[str],
    signal_order: Sequence[str],
    hits_by_signal: Sequence[list[SignalHit]],
) -> list[RejectionRecord]:
    """Classify every universe symbol not in `candidate_symbols`.

    Args:
        data: Point-in-time screening input (same as the pipeline's run).
        settings: Loaded application settings (same sub-configs each mirrored
            Filter/Signal reads).
        candidate_symbols: Symbols that passed every configured Filter and
            every configured Signal, *before* ranking/candidate_limit
            truncation. Symbols excluded from the final candidate list only
            by that later truncation (or, defensively, by a NaN ranking
            metric) are intentionally omitted here too: none of the closed
            enum's reason codes describes "ranked out", so they land in
            neither `candidates` nor `rejections` for this run.
        signal_order: Configured signal keys, in `strategies.yaml` order.
        hits_by_signal: Each signal's hits, same order as `signal_order`
            (`ScreeningPipeline` already computes this once per run).

    Returns:
        One `RejectionRecord` per non-candidate universe symbol. Empty when
        no signals are configured (mirrors `ScreeningPipeline.run()`'s own
        "no signals -> no candidates at all" rule: with nothing to hit,
        there is nothing this classifier's fixed buckets can meaningfully
        explain).
    """
    if not signal_order:
        return []

    hit_sets = [{hit.symbol for hit in hits} for hits in hits_by_signal]
    return [
        _classify_symbol(member.symbol, data, settings, signal_order, hit_sets)
        for member in data.universe
        if member.symbol not in candidate_symbols
    ]


def _classify_symbol(
    symbol: str,
    data: ScreeningInput,
    settings: Settings,
    signal_order: Sequence[str],
    hit_sets: list[set[str]],
) -> RejectionRecord:
    fundamentals_result = _classify_fundamentals(symbol, data, settings)
    if fundamentals_result is not None:
        return fundamentals_result

    liquidity_result = _classify_liquidity(symbol, data, settings)
    if liquidity_result is not None:
        return liquidity_result

    for signal_name, hits in zip(signal_order, hit_sets, strict=True):
        if symbol not in hits:
            return _classify_signal(symbol, data, settings, signal_name)

    # Unreachable by construction: the symbol passed the mirrored fundamental
    # and liquidity checks and hit every configured signal, yet was passed
    # in as a non-candidate. That means the classifier's buckets above are
    # incomplete for this symbol -- a bug in this module, not a case to
    # silently drop (see the module and `classify_rejections` docstrings for
    # the one legitimate reason a passing symbol can still be absent: ranking
    # truncation, which callers must exclude from `candidate_symbols` before
    # calling this function).
    msg = f"{symbol!r} matched no rejection rule; classifier bucket is incomplete"
    raise AssertionError(msg)


def _classify_fundamentals(
    symbol: str, data: ScreeningInput, settings: Settings
) -> RejectionRecord | None:
    """Mirror `ProfitablePositiveFCFEquityFilter.apply()`'s branch order."""
    config = settings.fundamental_filters
    fundamentals = data.fundamentals
    if fundamentals.empty:
        recent = fundamentals
    else:
        as_of_cutoff = datetime.combine(data.as_of, time.max, tzinfo=UTC)
        available = fundamentals[
            (fundamentals["symbol"] == symbol)
            & (fundamentals["filed_at"] <= as_of_cutoff)
        ]
        recent = (
            available.sort_values("filed_at")
            .drop_duplicates(subset="fiscal_period_end", keep="last")
            .sort_values("fiscal_period_end", ascending=False)
            .head(config.min_profitable_quarters)
        )

    if len(recent) < config.min_profitable_quarters:
        return RejectionRecord(
            symbol,
            RejectionStage.DATA_QUALITY,
            RejectionReasonCode.DATA_INSUFFICIENT_HISTORY,
            {
                "available_quarters": len(recent),
                "required_quarters": config.min_profitable_quarters,
            },
        )

    latest = recent.iloc[0]
    if not (recent["net_income"] > 0).all():
        # A NaN net_income (real data gap, distinct from a genuinely negative
        # value) must report as null, not a non-finite float reaching
        # json_guard.dumps_safe() — same convention as fcf/equity_ratio below.
        net_income = (
            float(latest["net_income"]) if pd.notna(latest["net_income"]) else None
        )
        return RejectionRecord(
            symbol,
            RejectionStage.FUNDAMENTAL_FILTER,
            RejectionReasonCode.FILTER_NEGATIVE_NET_INCOME,
            {"net_income": net_income, "threshold": 0},
        )
    if config.require_positive_fcf and not (
        pd.notna(latest["fcf"]) and latest["fcf"] > 0
    ):
        fcf = float(latest["fcf"]) if pd.notna(latest["fcf"]) else None
        return RejectionRecord(
            symbol,
            RejectionStage.FUNDAMENTAL_FILTER,
            RejectionReasonCode.FILTER_NEGATIVE_FCF,
            {"fcf": fcf, "threshold": 0},
        )
    if pd.isna(latest["assets"]) or latest["assets"] == 0:
        return RejectionRecord(
            symbol,
            RejectionStage.FUNDAMENTAL_FILTER,
            RejectionReasonCode.FILTER_LOW_EQUITY_RATIO,
            {"equity_ratio": None, "threshold": config.min_equity_ratio},
        )
    equity_ratio = (
        None
        if pd.isna(latest["equity"])
        else float(latest["equity"]) / float(latest["assets"])
    )
    if equity_ratio is None or equity_ratio <= config.min_equity_ratio:
        return RejectionRecord(
            symbol,
            RejectionStage.FUNDAMENTAL_FILTER,
            RejectionReasonCode.FILTER_LOW_EQUITY_RATIO,
            {"equity_ratio": equity_ratio, "threshold": config.min_equity_ratio},
        )
    return None


def _classify_liquidity(
    symbol: str, data: ScreeningInput, settings: Settings
) -> RejectionRecord | None:
    """Mirror `MinAverageVolumeFilter.apply()`'s branch order."""
    config = settings.technical_signals.volume
    series = symbol_bars(data.bars, symbol, data.as_of)
    if series is None or len(series) < config.avg_volume_days:
        available_bars = 0 if series is None else len(series)
        return RejectionRecord(
            symbol,
            RejectionStage.DATA_QUALITY,
            RejectionReasonCode.DATA_INSUFFICIENT_HISTORY,
            {"available_bars": available_bars, "required_bars": config.avg_volume_days},
        )

    avg_volume = float(series["volume"].tail(config.avg_volume_days).mean())
    if avg_volume <= config.min_avg_volume:
        return RejectionRecord(
            symbol,
            RejectionStage.FUNDAMENTAL_FILTER,
            RejectionReasonCode.FILTER_LOW_LIQUIDITY,
            {"avg_volume": avg_volume, "threshold": config.min_avg_volume},
        )
    return None


def _classify_signal(
    symbol: str, data: ScreeningInput, settings: Settings, signal_name: str
) -> RejectionRecord:
    if signal_name == "trend_sma":
        return _classify_trend_sma(symbol, data, settings)
    if signal_name == "pullback_rsi":
        return _classify_pullback_rsi(symbol, data, settings)
    if signal_name == "minervini_stage2":
        return _classify_minervini_stage2(symbol, data)
    # Any other configured signal key has no mirrored logic here (module
    # docstring: hardcoded to the current two signals, not generalized).
    msg = f"rejection_classifier has no mirrored logic for signal {signal_name!r}"
    raise NotImplementedError(msg)


def _classify_trend_sma(
    symbol: str, data: ScreeningInput, settings: Settings
) -> RejectionRecord:
    """Mirror `TrendSMASignal.evaluate()`'s condition."""
    config = settings.technical_signals.trend
    required_bars = max(config.sma_short, config.sma_long)
    series = symbol_bars(data.bars, symbol, data.as_of)
    if series is None:
        # Unreachable via `classify_rejections`: `_classify_liquidity` reads
        # the same `data.bars` and already returns DATA_INSUFFICIENT_HISTORY
        # for a None series before any signal is ever checked. Kept for
        # structural symmetry with `TrendSMASignal.evaluate()`'s own guard.
        return _insufficient_history(symbol, 0, required_bars)  # pragma: no cover

    sma_short = sma(series["close"], config.sma_short)
    sma_long = sma(series["close"], config.sma_long)
    if pd.isna(sma_short.iloc[-1]) or pd.isna(sma_long.iloc[-1]):
        return _insufficient_history(symbol, len(series), required_bars)

    return RejectionRecord(
        symbol,
        RejectionStage.TECHNICAL_SIGNAL,
        RejectionReasonCode.SIGNAL_TREND_NOT_MET,
        {
            "close": float(series["close"].iloc[-1]),
            "sma_long": float(sma_long.iloc[-1]),
        },
    )


def _classify_pullback_rsi(
    symbol: str, data: ScreeningInput, settings: Settings
) -> RejectionRecord:
    """Mirror `PullbackRSISignal.evaluate()`'s condition."""
    config = settings.technical_signals.pullback
    required_bars = max(config.rsi_period, _PULLBACK_SMA_WINDOW)
    series = symbol_bars(data.bars, symbol, data.as_of)
    if series is None:
        # Unreachable via `classify_rejections`: see the identical note in
        # `_classify_trend_sma` above.
        return _insufficient_history(symbol, 0, required_bars)  # pragma: no cover

    rsi = wilder_rsi(series["close"], config.rsi_period)
    sma50 = sma(series["close"], _PULLBACK_SMA_WINDOW)
    if pd.isna(rsi.iloc[-1]) or pd.isna(sma50.iloc[-1]):
        return _insufficient_history(symbol, len(series), required_bars)

    return RejectionRecord(
        symbol,
        RejectionStage.TECHNICAL_SIGNAL,
        RejectionReasonCode.SIGNAL_RSI_NOT_MET,
        {"rsi14": float(rsi.iloc[-1]), "threshold": config.rsi_threshold},
    )


def _classify_minervini_stage2(symbol: str, data: ScreeningInput) -> RejectionRecord:
    """Classify Minervini history failures before its ordinary non-hit path.

    The Stage 2 signal evaluates the 52-week high/low over a 252-bar window
    and treats fewer than 200 rows as data-quality failure (P5-21 decision).
    Remaining misses can be ordinary conditions (including RS). The existing
    constrained rejection ledger has no generic technical code, so they use
    `SIGNAL_TREND_NOT_MET` with the precise signal name in the detail.
    """
    series = symbol_bars(data.bars, symbol, data.as_of)
    available = len(series) if series is not None else 0
    required = 200
    if available < required:
        return _insufficient_history(symbol, available, required)
    return RejectionRecord(
        symbol,
        RejectionStage.TECHNICAL_SIGNAL,
        RejectionReasonCode.SIGNAL_TREND_NOT_MET,
        {
            "signal": "minervini_stage2",
            "available_bars": available,
            "required_52_week_bars": required,
        },
    )


def _insufficient_history(
    symbol: str, available_bars: int, required_bars: int
) -> RejectionRecord:
    return RejectionRecord(
        symbol,
        RejectionStage.DATA_QUALITY,
        RejectionReasonCode.DATA_INSUFFICIENT_HISTORY,
        {"available_bars": available_bars, "required_bars": required_bars},
    )
