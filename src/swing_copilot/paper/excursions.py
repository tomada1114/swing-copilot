"""Point-in-time daily MAE/MFE updates for paper positions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing_copilot.storage.paper_records import PositionExcursionRecord

if TYPE_CHECKING:
    from datetime import date

    import pandas as pd

    from swing_copilot.models import Position
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore


@dataclass(frozen=True, slots=True)
class ExcursionUpdateSummary:
    """Outcome of one day's best-effort position updates."""

    updated_count: int
    missing_symbols: tuple[str, ...] = ()


def _excursions(
    position: Position, bars: pd.DataFrame
) -> tuple[float | None, float | None]:
    lows = [
        float(value)
        for value in bars["low"].tolist()
        if value is not None and math.isfinite(float(value))
    ]
    highs = [
        float(value)
        for value in bars["high"].tolist()
        if value is not None and math.isfinite(float(value))
    ]
    if not lows or not highs:
        return None, None
    mae = min(0.0, min(lows) - position.entry_price)
    mfe = max(0.0, max(highs) - position.entry_price)
    return mae, mfe


def update_position_excursions(
    state_store: StateStore, market_store: MarketStore, as_of: date
) -> ExcursionUpdateSummary:
    """Recompute cumulative daily excursions without reading beyond `as_of`."""
    positions = [
        *state_store.get_open_positions(is_paper=True),
        *state_store.get_closed_positions(is_paper=True, as_of=None),
    ]
    targets = [
        position
        for position in positions
        if position.entry_date <= as_of
        and (position.close_date is None or as_of <= position.close_date)
    ]
    by_id = {position.position_id: position for position in targets}
    targets = list(by_id.values())
    if not targets:
        return ExcursionUpdateSummary(0)

    start = min(position.entry_date for position in targets)
    bars = market_store.read_bars(
        sorted({position.symbol for position in targets}), start, as_of, as_of
    )
    records: list[PositionExcursionRecord] = []
    missing_symbols: list[str] = []
    for position in targets:
        position_bars = bars[
            (bars["symbol"] == position.symbol)
            & (bars["date"] >= position.entry_date)
            & (bars["date"] <= as_of)
        ]
        today = position_bars[position_bars["date"] == as_of]
        has_today = (
            not today.empty
            and today["high"].notna().all()
            and today["low"].notna().all()
        )
        if not has_today:
            missing_symbols.append(position.symbol)
        mae, mfe = _excursions(position, position_bars)
        records.append(
            PositionExcursionRecord(
                position.position_id,
                as_of,
                mae,
                mfe,
                "OK" if has_today else "MISSING_BAR",
            )
        )
    state_store.upsert_position_excursions(records)
    return ExcursionUpdateSummary(len(records), tuple(sorted(set(missing_symbols))))
