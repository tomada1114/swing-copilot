"""Stage 2 technical signals and the liquidity filter (FR-05, pandas)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from swing_copilot.screening.base import (
    ScreeningInput,
    SignalHit,
    register_filter,
    register_signal,
)
from swing_copilot.screening.indicators import (
    SymbolWindow,
    percentile_ranks,
    symbol_bars,
    symbol_window,
)
from swing_copilot.screening.vcp import (
    VCP_WARMUP_BARS,
    VcpThresholds,
    evaluate_vcp,
)

if TYPE_CHECKING:
    from swing_copilot.config import MinerviniSignalConfig, Settings

_PULLBACK_SMA_WINDOW = 50
#: ATR period of `pullback.band_atr_multiple`'s band. Was `wilder_atr`'s
#: default before the band read its ATR from a precomputed column (#214);
#: named here so the column key is explicit rather than implied by a default.
_PULLBACK_BAND_ATR_PERIOD = 14
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
        self.required_bars = self._thresholds.pattern_days_max + VCP_WARMUP_BARS

    def evaluate(self, data: ScreeningInput, symbols: set[str]) -> list[SignalHit]:
        """Return non-chasing valid VCP setups with quantitative evidence.

        The verdict itself lives in `vcp.evaluate_vcp` (including the
        fixed-width window that keeps the daily and backtest callers'
        different lookbacks from flipping it, Issue #186), so the rejection
        classifier explains a miss with the very same computation instead of
        a second copy that can drift (Issue #188).
        """
        hits: list[SignalHit] = []
        for symbol in sorted(symbols):
            series = symbol_bars(data.bars, symbol, data.as_of)
            if series is None:
                continue
            evaluation = evaluate_vcp(series, self._thresholds)
            pattern = evaluation.pattern
            if not evaluation.is_hit or pattern is None or pattern.dry_up_ratio is None:
                continue
            metrics = {
                "close": evaluation.close,
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

    #: A 252-day RS return needs 253 closes — the longest of the seven
    #: conditions' inputs.
    required_bars = max(_MINERVINI_RS_PERIODS) + 1

    def __init__(self, settings: Settings, *, min_criteria: int = 6) -> None:
        """Create the signal with its strategy-specific inclusive pass line."""
        self._config = settings.technical_signals.minervini
        self._min_criteria = min_criteria

    def evaluate(self, data: ScreeningInput, symbols: set[str]) -> list[SignalHit]:
        """Return symbols satisfying at least `min_criteria` of seven conditions."""
        # Issue #224: one window per symbol, built once in sorted order and
        # shared with the RS pass, instead of that pass looking the same
        # symbol up a second time. Insertion order is the previous
        # `sorted(symbols)` order, so RS percentiles and hits are unchanged.
        windows = {
            symbol: window
            for symbol in sorted(symbols)
            if (window := symbol_window(data.bars, symbol, data.as_of)) is not None
        }
        rs_percentiles = self._rs_percentiles(windows)
        hits: list[SignalHit] = []
        for symbol, window in windows.items():
            metrics = self._metrics(window, rs_percentiles.get(symbol))
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

    def _rs_percentiles(self, windows: dict[str, SymbolWindow]) -> dict[str, float]:
        """Return deterministic universe-relative weighted-return percentiles.

        A one-member population has the same fixed 50.0 midpoint convention
        as P1-01 liquidity percentiles. A 252-day return needs 253 closes;
        shorter histories intentionally receive no RS value and therefore do
        not satisfy condition seven.

        Args:
            windows: One `as_of` window per symbol with any history at all,
                in the deterministic symbol order the percentiles are ranked
                in.
        """
        weights = (
            self._config.rs_weight_63d,
            self._config.rs_weight_126d,
            self._config.rs_weight_189d,
            self._config.rs_weight_252d,
        )
        values: list[tuple[str, float]] = []
        for symbol, window in windows.items():
            if window.bar_count < max(_MINERVINI_RS_PERIODS) + 1:
                continue
            closes = window.bars["close"]
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

        return {
            symbol: percentile * 100.0
            for symbol, percentile in percentile_ranks(dict(values)).items()
        }

    def _metrics(
        self, window: SymbolWindow, rs_percentile: float | None
    ) -> dict[str, float]:
        template = minervini_template(window, rs_percentile, self._config)
        criteria_met = sum(template.conditions)
        metrics = {
            "close": template.close,
            "minervini_sma200_rising_days": float(template.sma200_rising_days),
            "minervini_criteria_met": float(criteria_met),
            **{
                f"minervini_condition_{number}": float(value)
                for number, value in enumerate(template.conditions, start=1)
            },
        }
        # Candidate metrics are persisted as strict JSON. An unavailable RS
        # value (or unavailable moving average/high-low) is an unmet
        # condition, not a NaN value to serialize.
        metrics.update(
            {
                name: value
                for name, value in (
                    ("sma50", template.sma50),
                    ("sma150", template.sma150),
                    ("sma200", template.sma200),
                )
                if not math.isnan(value)
            }
        )
        if template.low_52_week is not None and template.high_52_week is not None:
            metrics["minervini_52_week_low"] = template.low_52_week
            metrics["minervini_52_week_high"] = template.high_52_week
        if rs_percentile is not None:
            metrics["minervini_rs_percentile"] = rs_percentile
        return metrics


@dataclass(frozen=True, slots=True)
class MinerviniTemplate:
    """One symbol's Stage 2 trend-template evaluation, condition by condition.

    Extracted as a shared value by Issue #188: the signal keeps only the
    aggregate `criteria_met`, so a rejected symbol used to reach the ledger
    as a bare `SIGNAL_TREND_NOT_MET` even though *which* of the seven
    conditions failed had just been computed. Both the signal and the
    rejection classifier now read the same evaluation.

    `conditions` is indexed 0-based but numbered 1..7 in the template's own
    terms. `low_52_week`/`high_52_week` are `None` when the window is too
    short to define them, which is distinct from a computed extreme.
    """

    close: float
    sma50: float
    sma150: float
    sma200: float
    sma200_rising_days: int
    low_52_week: float | None
    high_52_week: float | None
    conditions: tuple[bool, ...]


def minervini_template(
    window: SymbolWindow,
    rs_percentile: float | None,
    config: MinerviniSignalConfig,
) -> MinerviniTemplate:
    """Evaluate the seven Stage 2 conditions over one symbol's window.

    Args:
        window: The symbol's point-in-time bars and cached indicators.
        rs_percentile: The universe-relative RS percentile, or `None` when
            history is too short to compute one. `None` fails condition
            seven -- it is an unmet condition, not a missing measurement, in
            the signal's own terms.
        config: The configured thresholds (rising days, high/low multiples,
            minimum RS percentile).

    Returns:
        The condition results plus the inputs they were decided on.
    """
    latest_close = window.close
    latest_sma50 = window.sma(_MINERVINI_SMA50_WINDOW)
    latest_sma150 = window.sma(_MINERVINI_SMA150_WINDOW)
    latest_sma200 = window.sma(_MINERVINI_SMA200_WINDOW)
    has_moving_averages = not any(
        math.isnan(value) for value in (latest_sma50, latest_sma150, latest_sma200)
    )
    rising_days = _consecutive_rising_days(window.sma_history(_MINERVINI_SMA200_WINDOW))
    price_window = window.bars["close"].tail(_MINERVINI_52_WEEK_WINDOW)
    has_52_week_window = len(price_window) >= _MINERVINI_MIN_52_WEEK_BARS
    low_52w = float(price_window.min()) if has_52_week_window else float("nan")
    high_52w = float(price_window.max()) if has_52_week_window else float("nan")
    return MinerviniTemplate(
        close=latest_close,
        sma50=latest_sma50,
        sma150=latest_sma150,
        sma200=latest_sma200,
        sma200_rising_days=rising_days,
        low_52_week=low_52w if has_52_week_window else None,
        high_52_week=high_52w if has_52_week_window else None,
        conditions=(
            has_moving_averages
            and latest_close > latest_sma150
            and latest_close > latest_sma200,
            has_moving_averages and latest_sma150 > latest_sma200,
            rising_days >= config.sma200_rising_days,
            has_moving_averages and latest_close > latest_sma50,
            has_52_week_window and latest_close >= low_52w * config.min_low_multiple,
            has_52_week_window and latest_close >= high_52w * config.min_high_multiple,
            rs_percentile is not None and rs_percentile >= config.min_rs_percentile,
        ),
    )


def _consecutive_rising_days(values: np.ndarray) -> int:
    """Count latest consecutive strictly-rising day-over-day values."""
    count = 0
    clean = values[~np.isnan(values)]
    for index in range(len(clean) - 1, 0, -1):
        if clean[index] <= clean[index - 1]:
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
        self.required_bars = max(self._config.sma_short, self._config.sma_long)

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
            window = symbol_window(data.bars, symbol, data.as_of)
            if window is None:
                continue
            sma_short = window.sma(self._config.sma_short)
            sma_long = window.sma(self._config.sma_long)
            if math.isnan(sma_short) or math.isnan(sma_long):
                continue
            if window.close > sma_long and sma_short > sma_long:
                hits.append(
                    SignalHit(
                        symbol=symbol,
                        signal_name=self.name,
                        direction="long",
                        strength=1.0,
                        metrics={
                            "sma_short": sma_short,
                            "sma_long": sma_long,
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
        self.required_bars = max(self._config.rsi_period, _PULLBACK_SMA_WINDOW)

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
            window = symbol_window(data.bars, symbol, data.as_of)
            if window is None:
                continue
            rsi = window.rsi(self._config.rsi_period)
            last_sma50 = window.sma(_PULLBACK_SMA_WINDOW)
            if math.isnan(rsi) or math.isnan(last_sma50):
                continue
            within_band = self._within_band(window, window.close, last_sma50)
            if rsi < self._config.rsi_threshold and within_band:
                hits.append(
                    SignalHit(
                        symbol=symbol,
                        signal_name=self.name,
                        direction="long",
                        strength=1.0,
                        metrics={"rsi14": rsi, "sma50": last_sma50},
                    )
                )
        return hits

    def _within_band(
        self, window: SymbolWindow, last_close: float, last_sma50: float
    ) -> bool:
        """Whether the close sits inside the pullback band around SMA50.

        Two exclusive modes. The default measures the gap as a fixed
        percentage of SMA50; `band_atr_multiple` instead measures it in ATR14
        units, matching how `execution.fair_max_d` already reasons about
        distance from SMA50 elsewhere in the pipeline. An ATR that is NaN or
        zero leaves the distance undefined, so the band closes rather than
        admitting a symbol whose volatility cannot be measured.
        """
        distance = abs(last_close - last_sma50)
        multiple = self._config.band_atr_multiple
        if multiple is None:
            return distance / last_sma50 <= self._config.sma_band_pct

        atr14 = window.atr(_PULLBACK_BAND_ATR_PERIOD)
        if math.isnan(atr14) or atr14 <= 0:
            return False
        return distance / atr14 <= multiple


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
        days = self._config.avg_volume_days
        for member in data.universe:
            window = symbol_window(data.bars, member.symbol, data.as_of)
            if window is None or window.bar_count < days:
                continue
            if window.mean_volume(days) > self._config.min_avg_volume:
                passing.add(member.symbol)
        return passing
