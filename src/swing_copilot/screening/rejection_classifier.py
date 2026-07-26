"""Independently classify why non-candidate universe symbols were rejected.

Mirrors — deliberately does not reuse — `ProfitablePositiveFCFEquityFilter`,
`MinAverageVolumeFilter`, `TrendSMASignal`, and `PullbackRSISignal`'s own
threshold logic, the same way `ScreeningPipeline._ranking_metrics()`
independently recomputes ranking metrics from raw bars rather than reusing
signal internals (`docs/04_detailed_design.md` 2.1 #4). This keeps rejection
detail available even for symbols no configured signal ever touches.

This module deliberately mirrors the currently registered strategy building
blocks rather than generalizing arbitrary future Filters/Signals. Adding a
new configured component requires extending this classifier too.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    from swing_copilot.config import Settings
    from swing_copilot.screening.base import ScreeningInput, SignalHit

# PullbackRSISignal's own SMA window (technical_signals.py::_PULLBACK_SMA_WINDOW)
# is not settings-driven; duplicated here rather than imported across the
# module boundary, matching this module's "mirror, don't reuse" contract.
_PULLBACK_SMA_WINDOW = 50
_RANKING_SMA_LONG_WINDOW = 200


@dataclass(frozen=True, slots=True)
class RejectionPlan:
    """Configured component order and already-computed signal hits."""

    filter_order: tuple[str, ...]
    signal_order: tuple[str, ...]
    hits_by_signal: tuple[tuple[SignalHit, ...], ...]


def classify_rejections(
    data: ScreeningInput,
    settings: Settings,
    *,
    candidate_symbols: set[str],
    plan: RejectionPlan,
) -> list[RejectionRecord]:
    """Classify every universe symbol not in `candidate_symbols`.

    Args:
        data: Point-in-time screening input (same as the pipeline's run).
        settings: Loaded application settings (same sub-configs each mirrored
            Filter/Signal reads).
        candidate_symbols: Symbols that passed every configured Filter and
            every configured Signal and have valid ranking metrics, before
            candidate_limit truncation. Symbols excluded only by that later
            truncation are intentionally omitted here.
        plan: Configured filter/signal keys and each signal's hits
            (`ScreeningPipeline` already computes this once per run).

    Returns:
        One `RejectionRecord` per non-candidate universe symbol. Empty when
        no signals are configured (mirrors `ScreeningPipeline.run()`'s own
        "no signals -> no candidates at all" rule: with nothing to hit,
        there is nothing this classifier's fixed buckets can meaningfully
        explain).
    """
    if not plan.signal_order:
        return []

    hit_sets = [{hit.symbol for hit in hits} for hits in plan.hits_by_signal]
    return [
        _classify_symbol(
            member.symbol,
            data,
            settings,
            plan,
            hit_sets,
        )
        for member in data.universe
        if member.symbol not in candidate_symbols
    ]


def _classify_symbol(
    symbol: str,
    data: ScreeningInput,
    settings: Settings,
    plan: RejectionPlan,
    hit_sets: list[set[str]],
) -> RejectionRecord:
    for filter_name in plan.filter_order:
        filter_result = _classify_filter(symbol, data, settings, filter_name)
        if filter_result is not None:
            return filter_result

    for signal_name, hits in zip(plan.signal_order, hit_sets, strict=True):
        if symbol not in hits:
            return _classify_signal(symbol, data, settings, signal_name)

    series = symbol_bars(data.bars, symbol, data.as_of)
    available = len(series) if series is not None else 0
    return RejectionRecord(
        symbol,
        RejectionStage.DATA_QUALITY,
        RejectionReasonCode.DATA_INSUFFICIENT_HISTORY,
        {
            "available_bars": available,
            "required_bars": _RANKING_SMA_LONG_WINDOW,
            "ranking_metrics": "unavailable",
        },
    )


def _classify_filter(
    symbol: str,
    data: ScreeningInput,
    settings: Settings,
    filter_name: str,
) -> RejectionRecord | None:
    if filter_name == "profitable_positive_fcf_equity":
        return _classify_fundamentals(symbol, data, settings)
    if filter_name == "volume_min":
        return _classify_liquidity(symbol, data, settings)
    msg = f"rejection_classifier has no mirrored logic for filter {filter_name!r}"
    raise NotImplementedError(msg)


def _isoformat_date(value: object) -> str:
    """Normalize a fundamentals-row date cell to a plain ISO date string.

    `fundamentals` rows come from tests as `datetime.date` and from
    `MarketStore.read_fundamentals()` (DuckDB `.df()`) as `pandas.Timestamp`;
    both need coercing to a JSON-safe `str` before reaching
    `json_guard.dumps_safe()`, which cannot serialize either type directly.
    """
    return pd.Timestamp(value).date().isoformat()  # type: ignore[arg-type]  # Any: object cell from a DataFrame row


def _classify_net_income(symbol: str, recent: pd.DataFrame) -> RejectionRecord:
    """Classify a `min_profitable_quarters` net-income failure (P6-25).

    `recent` is sorted newest-first; this reports the most recent quarter
    that actually failed the `> 0` requirement rather than always the
    newest quarter — an older failing quarter must not be misreported as
    the current (possibly perfectly healthy) quarter's value. A `NaN`
    net_income is a real data gap (the filing lacked/failed to normalize
    the concept), distinct from a genuinely negative result, and must not
    be reported as a business rejection under `FILTER_NEGATIVE_NET_INCOME`.
    """
    failing = recent[~(recent["net_income"] > 0)]
    offending = failing.iloc[0]
    fiscal_period_end = _isoformat_date(offending["fiscal_period_end"])
    if pd.isna(offending["net_income"]):
        return RejectionRecord(
            symbol,
            RejectionStage.DATA_QUALITY,
            RejectionReasonCode.DATA_MISSING_NET_INCOME,
            {"fiscal_period_end": fiscal_period_end, "net_income": None},
        )
    return RejectionRecord(
        symbol,
        RejectionStage.FUNDAMENTAL_FILTER,
        RejectionReasonCode.FILTER_NEGATIVE_NET_INCOME,
        {
            "fiscal_period_end": fiscal_period_end,
            "net_income": float(offending["net_income"]),
            "threshold": 0,
        },
    )


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
        return _classify_net_income(symbol, recent)
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
    if signal_name == "vcp_breakout":
        return _classify_vcp_breakout(symbol, data)
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


def _classify_vcp_breakout(symbol: str, data: ScreeningInput) -> RejectionRecord:
    """Classify VCP's 50-bar volume-baseline prerequisite without crashing.

    The ledger's fixed reason-code set predates VCP. Keep its established
    generic technical code while preserving the real signal key in detail.
    """
    series = symbol_bars(data.bars, symbol, data.as_of)
    available = len(series) if series is not None else 0
    required = 50
    if available < required:
        return _insufficient_history(symbol, available, required)
    return RejectionRecord(
        symbol,
        RejectionStage.TECHNICAL_SIGNAL,
        RejectionReasonCode.SIGNAL_TREND_NOT_MET,
        {"signal": "vcp_breakout", "available_bars": available},
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
