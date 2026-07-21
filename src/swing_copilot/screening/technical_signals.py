"""Stage 2 technical signals and the liquidity filter (FR-05, pandas)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from swing_copilot.screening.base import (
    ScreeningInput,
    SignalHit,
    register_filter,
    register_signal,
)
from swing_copilot.screening.indicators import sma, symbol_bars, wilder_rsi

if TYPE_CHECKING:
    from swing_copilot.config import Settings

_PULLBACK_SMA_WINDOW = 50


@register_signal("trend_sma")
class TrendSMASignal:
    """Trend: close > SMA(long) and SMA(short) > SMA(long)."""

    name = "trend_sma"

    def __init__(self, settings: Settings) -> None:
        """Create the signal.

        Args:
            settings: Loaded application settings.
        """
        self._config = settings.technical_signals.trend

    def evaluate(self, data: ScreeningInput, symbols: set[str]) -> list[SignalHit]:
        """Return trend hits among `symbols`.

        Args:
            data: Point-in-time screening input.
            symbols: Candidate symbols to evaluate (already filter-narrowed).

        Returns:
            Hits for symbols in an established uptrend.
        """
        hits = []
        for symbol in sorted(symbols):
            series = symbol_bars(data.bars, symbol, data.as_of)
            if series is None:
                continue
            sma_short = sma(series["close"], self._config.sma_short)
            sma_long = sma(series["close"], self._config.sma_long)
            if pd.isna(sma_short.iloc[-1]) or pd.isna(sma_long.iloc[-1]):
                continue
            last_close = series["close"].iloc[-1]
            if (
                last_close > sma_long.iloc[-1]
                and sma_short.iloc[-1] > sma_long.iloc[-1]
            ):
                hits.append(
                    SignalHit(
                        symbol=symbol,
                        signal_name=self.name,
                        direction="long",
                        strength=1.0,
                        metrics={
                            "sma_short": float(sma_short.iloc[-1]),
                            "sma_long": float(sma_long.iloc[-1]),
                        },
                    )
                )
        return hits


@register_signal("pullback_rsi")
class PullbackRSISignal:
    """Pullback: Wilder RSI below threshold and close within an SMA50 band."""

    name = "pullback_rsi"

    def __init__(self, settings: Settings) -> None:
        """Create the signal.

        Args:
            settings: Loaded application settings.
        """
        self._config = settings.technical_signals.pullback

    def evaluate(self, data: ScreeningInput, symbols: set[str]) -> list[SignalHit]:
        """Return pullback hits among `symbols`.

        Args:
            data: Point-in-time screening input.
            symbols: Candidate symbols to evaluate (already filter-narrowed).

        Returns:
            Hits for symbols pulling back toward their SMA50.
        """
        hits = []
        for symbol in sorted(symbols):
            series = symbol_bars(data.bars, symbol, data.as_of)
            if series is None:
                continue
            rsi = wilder_rsi(series["close"], self._config.rsi_period)
            sma50 = sma(series["close"], _PULLBACK_SMA_WINDOW)
            if pd.isna(rsi.iloc[-1]) or pd.isna(sma50.iloc[-1]):
                continue
            last_close = series["close"].iloc[-1]
            last_sma50 = float(sma50.iloc[-1])
            within_band = (
                abs(last_close - last_sma50) / last_sma50 <= self._config.sma_band_pct
            )
            if rsi.iloc[-1] < self._config.rsi_threshold and within_band:
                hits.append(
                    SignalHit(
                        symbol=symbol,
                        signal_name=self.name,
                        direction="long",
                        strength=1.0,
                        metrics={"rsi14": float(rsi.iloc[-1]), "sma50": last_sma50},
                    )
                )
        return hits


@register_filter("volume_min")
class MinAverageVolumeFilter:
    """Liquidity filter: N-day average volume above a floor."""

    name = "volume_min"

    def __init__(self, settings: Settings) -> None:
        """Create the filter.

        Args:
            settings: Loaded application settings.
        """
        self._config = settings.technical_signals.volume

    def apply(self, data: ScreeningInput) -> set[str]:
        """Return symbols whose average volume clears the floor.

        Args:
            data: Point-in-time screening input.

        Returns:
            Qualifying symbols.
        """
        passing: set[str] = set()
        for member in data.universe:
            series = symbol_bars(data.bars, member.symbol, data.as_of)
            if series is None or len(series) < self._config.avg_volume_days:
                continue
            avg_volume = series["volume"].tail(self._config.avg_volume_days).mean()
            if avg_volume > self._config.min_avg_volume:
                passing.add(member.symbol)
        return passing
