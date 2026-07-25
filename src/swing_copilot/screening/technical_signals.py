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
from swing_copilot.screening.indicators import sma, symbol_bars, wilder_atr, wilder_rsi
from swing_copilot.screening.vcp import (
    VcpThresholds,
    detect_atr_zigzag,
    extract_pattern,
    is_chasing_pivot,
    validate_contractions,
)

if TYPE_CHECKING:
    from swing_copilot.config import Settings

_PULLBACK_SMA_WINDOW = 50
_MINERVINI_SMA50_WINDOW = 50
_MINERVINI_SMA150_WINDOW = 150
_MINERVINI_SMA200_WINDOW = 200
_MINERVINI_52_WEEK_WINDOW = 252
_MINERVINI_MIN_52_WEEK_BARS = 200
_MINERVINI_RS_PERIODS = (63, 126, 189, 252)


@register_signal("vcp_breakout")
class VcpBreakoutSignal:
    """Volatility Contraction Pattern signal, enabled only by its own strategy."""

    name = "vcp_breakout"

    def __init__(self, settings: Settings) -> None:
        """Create the signal from explicitly configurable P5-24 thresholds."""
        config = settings.technical_signals.vcp
        self._thresholds = VcpThresholds(**config.model_dump())

    def evaluate(self, data: ScreeningInput, symbols: set[str]) -> list[SignalHit]:
        """Return non-chasing valid VCP setups with quantitative evidence."""
        hits: list[SignalHit] = []
        for symbol in sorted(symbols):
            series = symbol_bars(data.bars, symbol, data.as_of)
            if series is None:
                continue
            atr = wilder_atr(series["high"], series["low"], series["close"])
            swings = detect_atr_zigzag(
                series["close"], atr, self._thresholds.zigzag_atr_multiplier
            )
            pattern = extract_pattern(swings, series["volume"])
            if pattern is None or pattern.dry_up_ratio is None:
                continue
            validation = validate_contractions(
                list(pattern.depths),
                pattern.pattern_days,
                is_small_cap=False,
                thresholds=self._thresholds,
            )
            close = float(series["close"].iloc[-1])
            if not validation.is_valid or is_chasing_pivot(
                close, pattern.pivot, self._thresholds
            ):
                continue
            metrics = {
                "close": close,
                "vcp_contraction_count": float(len(pattern.depths)),
                "vcp_pattern_days": float(pattern.pattern_days),
                "vcp_dry_up_ratio": pattern.dry_up_ratio,
                "vcp_pivot": pattern.pivot,
            }
            metrics.update(
                {
                    f"vcp_depth_{index}": depth
                    for index, depth in enumerate(pattern.depths, start=1)
                }
            )
            hits.append(
                SignalHit(
                    symbol=symbol,
                    signal_name=self.name,
                    direction="long",
                    strength=min(1.0, len(pattern.depths) / 3.0),
                    metrics=metrics,
                )
            )
        return hits


@register_signal("minervini_stage2")
class MinerviniStage2Signal:
    """Seven-condition Minervini Stage 2 trend template (P5-21)."""

    name = "minervini_stage2"

    def __init__(self, settings: Settings, *, min_criteria: int = 6) -> None:
        """Create the signal with its strategy-specific inclusive pass line."""
        self._config = settings.technical_signals.minervini
        self._min_criteria = min_criteria

    def evaluate(self, data: ScreeningInput, symbols: set[str]) -> list[SignalHit]:
        """Return symbols satisfying at least `min_criteria` of seven conditions."""
        rs_percentiles = self._rs_percentiles(data, symbols)
        hits: list[SignalHit] = []
        for symbol in sorted(symbols):
            series = symbol_bars(data.bars, symbol, data.as_of)
            if series is None:
                continue
            metrics = self._metrics(series, rs_percentiles.get(symbol))
            criteria_met = int(metrics["minervini_criteria_met"])
            if criteria_met >= self._min_criteria:
                hits.append(
                    SignalHit(
                        symbol=symbol,
                        signal_name=self.name,
                        direction="long",
                        strength=criteria_met / 7.0,
                        metrics=metrics,
                    )
                )
        return hits

    def _rs_percentiles(
        self, data: ScreeningInput, symbols: set[str]
    ) -> dict[str, float]:
        """Return deterministic universe-relative weighted-return percentiles.

        A one-member population has the same fixed 50.0 midpoint convention
        as P1-01 liquidity percentiles. A 252-day return needs 253 closes;
        shorter histories intentionally receive no RS value and therefore do
        not satisfy condition seven.
        """
        weights = (
            self._config.rs_weight_63d,
            self._config.rs_weight_126d,
            self._config.rs_weight_189d,
            self._config.rs_weight_252d,
        )
        values: list[tuple[str, float]] = []
        for symbol in sorted(symbols):
            series = symbol_bars(data.bars, symbol, data.as_of)
            if series is None or len(series) < max(_MINERVINI_RS_PERIODS) + 1:
                continue
            closes = series["close"]
            returns: list[float] = []
            for period in _MINERVINI_RS_PERIODS:
                start = float(closes.iloc[-period - 1])
                end = float(closes.iloc[-1])
                if start <= 0.0:
                    returns = []
                    break
                returns.append(end / start - 1.0)
            if returns:
                values.append(
                    (
                        symbol,
                        sum(
                            weight * value
                            for weight, value in zip(weights, returns, strict=True)
                        ),
                    )
                )

        count = len(values)
        if count == 1:
            return {values[0][0]: 50.0}
        if count == 0:
            return {}
        return {
            symbol: rank / (count - 1) * 100.0
            for rank, (symbol, _) in enumerate(
                sorted(values, key=lambda item: (item[1], item[0]))
            )
        }

    def _metrics(
        self, series: pd.DataFrame, rs_percentile: float | None
    ) -> dict[str, float]:
        close_series = series["close"]
        sma50 = sma(close_series, _MINERVINI_SMA50_WINDOW)
        sma150 = sma(close_series, _MINERVINI_SMA150_WINDOW)
        sma200 = sma(close_series, _MINERVINI_SMA200_WINDOW)
        latest_close = float(close_series.iloc[-1])
        latest_sma50 = sma50.iloc[-1]
        latest_sma150 = sma150.iloc[-1]
        latest_sma200 = sma200.iloc[-1]
        has_moving_averages = not any(
            pd.isna(value) for value in (latest_sma50, latest_sma150, latest_sma200)
        )
        rising_days = _consecutive_rising_days(sma200)
        price_window = close_series.tail(_MINERVINI_52_WEEK_WINDOW)
        has_52_week_window = len(price_window) >= _MINERVINI_MIN_52_WEEK_BARS
        low_52w = float(price_window.min()) if has_52_week_window else float("nan")
        high_52w = float(price_window.max()) if has_52_week_window else float("nan")
        condition_values = (
            has_moving_averages
            and latest_close > latest_sma150
            and latest_close > latest_sma200,
            has_moving_averages and latest_sma150 > latest_sma200,
            rising_days >= self._config.sma200_rising_days,
            has_moving_averages and latest_close > latest_sma50,
            has_52_week_window
            and latest_close >= low_52w * self._config.min_low_multiple,
            has_52_week_window
            and latest_close >= high_52w * self._config.min_high_multiple,
            rs_percentile is not None
            and rs_percentile >= self._config.min_rs_percentile,
        )
        criteria_met = sum(condition_values)
        metrics = {
            "close": latest_close,
            "minervini_sma200_rising_days": float(rising_days),
            "minervini_criteria_met": float(criteria_met),
            **{
                f"minervini_condition_{number}": float(value)
                for number, value in enumerate(condition_values, start=1)
            },
        }
        # Candidate metrics are persisted as strict JSON. An unavailable RS
        # value (or unavailable moving average/high-low) is an unmet
        # condition, not a NaN value to serialize.
        for name, value in (
            ("sma50", latest_sma50),
            ("sma150", latest_sma150),
            ("sma200", latest_sma200),
        ):
            if not pd.isna(value):
                metrics[name] = float(value)
        if has_52_week_window:
            metrics["minervini_52_week_low"] = low_52w
            metrics["minervini_52_week_high"] = high_52w
        if rs_percentile is not None:
            metrics["minervini_rs_percentile"] = rs_percentile
        return metrics


def _consecutive_rising_days(values: pd.Series) -> int:
    """Count latest consecutive strictly-rising day-over-day values."""
    count = 0
    clean = values.dropna()
    for index in range(len(clean) - 1, 0, -1):
        if clean.iloc[index] <= clean.iloc[index - 1]:
            break
        count += 1
    return count


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
