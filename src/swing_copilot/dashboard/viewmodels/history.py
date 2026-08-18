"""View model for the history page.

Every aggregate here is stratified by `recommendation`. Since Issue #190 the
tracking ledger shadow-tracks `skip` verdicts as a counterfactual population,
so a pooled win rate or a pooled classification mix would average a decision
together with its own control group and mean nothing.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing_copilot.dashboard import formatting as fmt
from swing_copilot.dashboard.models import (
    Badge,
    ClassificationBar,
    ClassificationPanel,
    ClosedSummary,
    HistoryView,
    LedgerRow,
    RegimePoint,
)
from swing_copilot.dashboard.viewmodels import common

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

    import pandas as pd

#: Tokens this page can render, defined in its own footer legend.
LEGEND_KEYS = ("immature", "untracked", "none")

#: Verdict sides, in the order the page presents them. `proceed` first: it is
#: the decision, `skip` is its counterfactual.
RECOMMENDATIONS = ("proceed", "skip")

#: HIT/MISS classifications ordered from best to worst outcome. The charts
#: stack in this order so severity reads left-to-right in the legend.
CLASSIFICATION_ORDER = ("HIT", "NEUTRAL", "MISS_MILD", "MISS_SEVERE")

#: Position lifecycle state → badge modifier.
_POSITION_TONES = {"open": "info", "closed": "quiet"}

_OPEN = "open"
_CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class HistorySources:
    """The raw frames the history page is built from."""

    scorecard: pd.DataFrame
    regime: pd.DataFrame
    positions: pd.DataFrame


def build_history(sources: HistorySources) -> HistoryView:
    """Assemble everything `/history` renders."""
    records = common.to_records(sources.scorecard)
    positions = common.to_records(sources.positions)
    return HistoryView(
        panels=_classification_panels(records),
        regime_points=_regime_points(sources.regime),
        open_positions=_ledger_rows(positions, status=_OPEN),
        closed_summaries=_closed_summaries(positions),
        legend=fmt.legend(LEGEND_KEYS),
    )


def _classification_panels(
    records: Sequence[Mapping[str, object]],
) -> tuple[ClassificationPanel, ...]:
    """One stacked-bar facet per (recommendation, matured horizon)."""
    buckets: dict[tuple[str, int], dict[date, Counter[str]]] = {}
    for record in records:
        classification = common.optional_text(record.get("classification"))
        horizon = fmt.as_int(record.get("horizon_days"))
        run_date = fmt.as_date(record.get("run_date"))
        recommendation = common.optional_text(record.get("recommendation"))
        if (
            classification is None
            or horizon is None
            or run_date is None
            # `recommendation` is a closed vocabulary; anything else would be
            # a schema change this page has not been taught to stratify.
            or recommendation not in RECOMMENDATIONS
        ):
            continue  # an immature verdict has nothing to score yet
        key = (recommendation, horizon)
        buckets.setdefault(key, {}).setdefault(run_date, Counter())[classification] += 1

    panels: list[ClassificationPanel] = []
    for recommendation in RECOMMENDATIONS:
        for key in sorted(k for k in buckets if k[0] == recommendation):
            by_date = buckets[key]
            bars = tuple(
                ClassificationBar(
                    run_date=run_date,
                    counts=tuple(
                        (name, counter[name])
                        for name in CLASSIFICATION_ORDER
                        if counter[name]
                    ),
                    total=sum(counter.values()),
                )
                for run_date, counter in sorted(by_date.items())
            )
            panels.append(
                ClassificationPanel(
                    recommendation=recommendation,
                    horizon_days=key[1],
                    bars=bars,
                    total=sum(bar.total for bar in bars),
                )
            )
    return tuple(panels)


def _regime_points(frame: pd.DataFrame) -> tuple[RegimePoint, ...]:
    """The regime timeline, oldest run first."""
    points = [
        RegimePoint(
            run_date=run_date,
            vix_close=fmt.as_float(record.get("vix_close")),
            dd_level=common.optional_text(record.get("dd_level")),
            gate_verdict=common.optional_text(record.get("gate_verdict")),
        )
        for record in common.to_records(frame)
        if (run_date := fmt.as_date(record.get("run_date"))) is not None
    ]
    points.sort(key=lambda point: point.run_date)
    return tuple(points)


def _ledger_rows(
    records: Sequence[Mapping[str, object]], *, status: str
) -> tuple[LedgerRow, ...]:
    """Ledger rows in one lifecycle state, `proceed` side listed first."""
    rows = [
        _ledger_row(record)
        for record in records
        if common.optional_text(record.get("status")) == status
    ]
    rows.sort(key=lambda row: (row.recommendation.text != "proceed", row.symbol))
    return tuple(rows)


def _ledger_row(record: Mapping[str, object]) -> LedgerRow:
    status = common.optional_text(record.get("status")) or "unknown"
    return LedgerRow(
        run_date=fmt.day(record.get("run_date"), key="none"),
        symbol=str(record.get("symbol", "")),
        run_id=str(record.get("run_id", "")),
        recommendation=common.verdict_badge(
            common.optional_text(record.get("recommendation"))
        ),
        entry_date=fmt.day(record.get("entry_date"), key="untracked"),
        entry_price=fmt.number(record.get("entry_price"), key="untracked"),
        stop_price=fmt.number(record.get("stop_price"), key="untracked"),
        days_held=fmt.integer(record.get("days_held"), key="untracked"),
        status=Badge(text=status, tone=_POSITION_TONES.get(status, "quiet")),
        exit_date=fmt.day(record.get("exit_date"), key="none"),
        exit_reason=fmt.text(record.get("exit_reason"), key="none"),
        realized_return=fmt.number(
            record.get("realized_return_pct"), suffix="%", key="none", signed=True
        ),
    )


def _closed_summaries(
    records: Sequence[Mapping[str, object]],
) -> tuple[ClosedSummary, ...]:
    """Realized results per verdict side, never pooled across sides."""
    returns: dict[str, list[float]] = {name: [] for name in RECOMMENDATIONS}
    for record in records:
        recommendation = common.optional_text(record.get("recommendation"))
        realized = fmt.as_float(record.get("realized_return_pct"))
        is_closed = common.optional_text(record.get("status")) == _CLOSED
        if not is_closed or recommendation not in returns or realized is None:
            continue
        returns[recommendation].append(realized)

    summaries: list[ClosedSummary] = []
    for recommendation in RECOMMENDATIONS:
        values = returns[recommendation]
        wins = sum(1 for value in values if value > 0)
        summaries.append(
            ClosedSummary(
                recommendation=recommendation,
                closed=len(values),
                wins=wins,
                win_rate=(
                    fmt.number(100.0 * wins / len(values), digits=1, suffix="%")
                    if values
                    else fmt.missing("none")
                ),
                mean_return=(
                    fmt.number(
                        statistics.fmean(values), suffix="%", signed=True, digits=2
                    )
                    if values
                    else fmt.missing("none")
                ),
                median_return=(
                    fmt.number(
                        statistics.median(values), suffix="%", signed=True, digits=2
                    )
                    if values
                    else fmt.missing("none")
                ),
            )
        )
    return tuple(summaries)
