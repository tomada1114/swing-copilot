"""Stage 1 fundamental quality filter (FR-04)."""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import TYPE_CHECKING

import pandas as pd

from swing_copilot.screening.base import ScreeningInput, register_filter

if TYPE_CHECKING:
    from swing_copilot.config import Settings


@register_filter("profitable_positive_fcf_equity")
class ProfitablePositiveFCFEquityFilter:
    """Profitable for N quarters, positive FCF, and a healthy equity ratio.

    Thresholds come from `settings.yaml`'s `fundamental_filters.*`.
    """

    name = "profitable_positive_fcf_equity"

    def __init__(self, settings: Settings) -> None:
        """Create the filter.

        Args:
            settings: Loaded application settings.
        """
        self._config = settings.fundamental_filters

    def apply(self, data: ScreeningInput) -> set[str]:
        """Return symbols meeting the profitability/FCF/equity thresholds.

        Args:
            data: Point-in-time screening input.

        Returns:
            Qualifying symbols.
        """
        if data.fundamentals.empty:
            return set()

        as_of_cutoff = datetime.combine(data.as_of, time.max, tzinfo=UTC)
        available = data.fundamentals[data.fundamentals["filed_at"] <= as_of_cutoff]

        passing: set[str] = set()
        for symbol, group in available.groupby("symbol"):
            recent = (
                group.sort_values("filed_at")
                .drop_duplicates(subset="fiscal_period_end", keep="last")
                .sort_values("fiscal_period_end", ascending=False)
                .head(self._config.min_profitable_quarters)
            )
            if len(recent) < self._config.min_profitable_quarters:
                continue
            if not (recent["net_income"] > 0).all():
                continue

            latest = recent.iloc[0]
            if self._config.require_positive_fcf and not (
                pd.notna(latest["fcf"]) and latest["fcf"] > 0
            ):
                continue
            if pd.isna(latest["assets"]) or latest["assets"] == 0:
                continue
            if (
                pd.isna(latest["equity"])
                or (latest["equity"] / latest["assets"])
                <= self._config.min_equity_ratio
            ):
                continue

            passing.add(str(symbol))
        return passing
