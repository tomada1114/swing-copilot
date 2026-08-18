"""View model for the per-symbol detail page."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing_copilot.dashboard import formatting as fmt
from swing_copilot.dashboard.models import (
    Badge,
    ReasonRow,
    Stat,
    SymbolDetail,
    TrackingPanel,
)
from swing_copilot.dashboard.viewmodels import common

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pandas as pd

    from swing_copilot.dashboard.models import RunRef

#: Tokens this page can render, defined in its own footer legend.
LEGEND_KEYS = (
    "not_ingested",
    "immature",
    "absent",
    "unrecorded",
    "untracked",
    "pre_measurement",
    "pre_tagging",
)

#: Position lifecycle state → badge modifier.
_POSITION_TONES = {"open": "info", "closed": "quiet"}


@dataclass(frozen=True, slots=True)
class SymbolSources:
    """The raw frames one symbol detail page is built from."""

    run: RunRef
    symbol: str
    candidates: pd.DataFrame
    scorecard: pd.DataFrame
    reasons: pd.DataFrame
    positions: pd.DataFrame


def build_symbol_detail(sources: SymbolSources) -> SymbolDetail | None:
    """Assemble `/runs/{run_id}/symbols/{symbol}`.

    Args:
        sources: Frames for the run; the symbol filter happens here so the
            route stays a parameter check.

    Returns:
        The page's view model, or `None` when this run knows nothing about
        the symbol — neither a candidate row nor a verdict.
    """
    candidate = _first_matching(sources.candidates, sources.symbol)
    entry = next(
        (
            value
            for key, value in common.aggregate_scorecard(sources.scorecard).items()
            if key[0] == sources.symbol
        ),
        None,
    )
    if candidate is None and entry is None:
        return None
    strategy_key = _strategy_key(candidate, entry)
    values: Mapping[str, object] = candidate or {}
    scorecard_values: Mapping[str, object] = {} if entry is None else entry.values
    return SymbolDetail(
        run=sources.run,
        status_badge=common.status_badge(sources.run.status),
        symbol=sources.symbol,
        strategy_key=strategy_key,
        gics_sector=fmt.text(scorecard_values.get("gics_sector"), key="not_ingested"),
        verdict=common.verdict_badge(
            None
            if entry is None
            else common.optional_text(entry.value("recommendation"))
        ),
        news_supply_level=fmt.text(
            scorecard_values.get("news_supply_level"), key="pre_measurement"
        ),
        reasons=_reason_rows(sources.reasons),
        score_components=common.score_component_stats(candidate),
        technicals=_technicals(values, scorecard_values),
        execution=_execution(values, scorecard_values),
        risk=_risk(entry),
        tracking=_tracking(sources.positions, sources.symbol),
        outcomes=() if entry is None else entry.outcomes,
        outcomes_fallback=common.outcomes_fallback(entry),
        legend=fmt.legend(LEGEND_KEYS),
    )


def _first_matching(frame: pd.DataFrame, symbol: str) -> Mapping[str, object] | None:
    return next(
        (
            record
            for record in common.to_records(frame)
            if str(record.get("symbol", "")) == symbol
        ),
        None,
    )


def _strategy_key(
    candidate: Mapping[str, object] | None, entry: common.ScorecardEntry | None
) -> str:
    if candidate is not None:
        return str(candidate.get("strategy_key", ""))
    return "" if entry is None else entry.strategy_key


def _pick(
    primary: Mapping[str, object], fallback: Mapping[str, object], column: str
) -> object:
    """Prefer the candidate row's value, falling back to the scorecard's.

    Both views project the same technical columns, but only one of them may
    have a row for this symbol.
    """
    value = primary.get(column)
    return fallback.get(column) if fmt.is_missing(value) else value


def _technicals(
    values: Mapping[str, object], scorecard_values: Mapping[str, object]
) -> tuple[Stat, ...]:
    return (
        Stat(
            "終値", fmt.number(_pick(values, scorecard_values, "close"), key="absent")
        ),
        Stat(
            "RSI14", fmt.number(_pick(values, scorecard_values, "rsi14"), key="absent")
        ),
        Stat(
            "ATR14", fmt.number(_pick(values, scorecard_values, "atr14"), key="absent")
        ),
        Stat("SMA50", fmt.number(values.get("sma50"), key="absent")),
        Stat("SMA200", fmt.number(values.get("sma200"), key="absent")),
        Stat(
            "平均出来高",
            fmt.number(
                _pick(values, scorecard_values, "avg_volume"), digits=0, key="absent"
            ),
        ),
    )


def _execution(
    values: Mapping[str, object], scorecard_values: Mapping[str, object]
) -> tuple[Stat, ...]:
    """Execution bucket columns, whose NULL means "never recorded".

    `execution_state`/`execution_distance` have no JSON fallback in
    `v_candidates`: a row written before the columns existed is
    unrecoverable, and must not be shown as the `UNKNOWN` bucket.
    """
    return (
        Stat(
            "実行状態",
            fmt.text(
                _pick(values, scorecard_values, "execution_state"), key="unrecorded"
            ),
        ),
        Stat(
            "実行距離",
            fmt.number(
                _pick(values, scorecard_values, "execution_distance"), key="unrecorded"
            ),
        ),
    )


def _risk(entry: common.ScorecardEntry | None) -> tuple[Stat, ...]:
    if entry is None:
        return (
            Stat("リスク判定", fmt.missing("not_ingested")),
            Stat("バインド制約", fmt.missing("not_ingested")),
        )
    return (
        Stat("リスク判定", fmt.text(entry.value("risk_status"), key="not_ingested")),
        Stat("バインド制約", fmt.text(entry.value("binding_constraint"), key="absent")),
    )


def _reason_rows(frame: pd.DataFrame) -> tuple[ReasonRow, ...]:
    """Verdict reasons in the order the analysis wrote them."""
    return tuple(
        ReasonRow(
            index=common.as_int(record.get("reason_index")) or 0,
            text=str(record.get("text", "")),
            basis=fmt.text(record.get("basis"), key="pre_tagging"),
            source_id_count=fmt.integer(record.get("source_id_count"), key="none"),
        )
        for record in common.to_records(frame)
    )


def _tracking(frame: pd.DataFrame, symbol: str) -> TrackingPanel | None:
    """The virtual position for this verdict, or `None` when untracked."""
    record = _first_matching(frame, symbol)
    if record is None:
        return None
    recommendation = common.optional_text(record.get("recommendation"))
    status = common.optional_text(record.get("status")) or "unknown"
    return TrackingPanel(
        recommendation=common.verdict_badge(recommendation),
        status=Badge(text=status, tone=_POSITION_TONES.get(status, "quiet")),
        stats=(
            Stat("建玉日", fmt.text(record.get("entry_date"), key="untracked")),
            Stat("建値", fmt.number(record.get("entry_price"), key="untracked")),
            Stat("ストップ", fmt.number(record.get("stop_price"), key="untracked")),
            Stat("経過営業日", fmt.integer(record.get("days_held"), key="untracked")),
            Stat("手仕舞日", fmt.text(record.get("exit_date"), key="none")),
            Stat(
                "実現リターン",
                fmt.number(
                    record.get("realized_return_pct"),
                    suffix="%",
                    key="none",
                    signed=True,
                ),
            ),
        ),
        exit_reason=fmt.text(record.get("exit_reason"), key="none"),
    )
