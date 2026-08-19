"""Conversions shared by more than one page's view model.

This is where the accumulated history's semantics are interpreted exactly
once: the scorecard's (verdict x matured horizon) grain is collapsed to one
entry per verdict, and each column's NULL is resolved into the specific token
that says *why* it is absent. Nothing downstream re-derives either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from swing_copilot.dashboard import formatting as fmt
from swing_copilot.dashboard.models import Badge, OutcomeCell, RunRef, Stat

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pandas as pd

#: `v_candidates` / `v_verdict_scorecard` score components, in the order the
#: ranking formula composes them.
SCORE_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("score_rsi_pullback", "RSI押し目"),
    ("score_trend_quality", "トレンド質"),
    ("score_liquidity", "流動性"),
    ("score_atr_pct", "ATR%"),
)

#: The verdict badge shown when the run has no `verdicts` row at all. Not
#: "skip", and not blank: `verdicts` is archived by the *next* run's retro
#: `collect` step, so the newest run legitimately has none yet.
NOT_INGESTED = Badge(
    text=fmt.NULL_TOKENS["not_ingested"].label,
    tone="absent",
    title=fmt.NULL_TOKENS["not_ingested"].explanation,
)


@dataclass(frozen=True, slots=True)
class ScorecardEntry:
    """One verdict, with its matured-horizon rows collapsed into `outcomes`.

    `v_verdict_scorecard` repeats every non-horizon column identically across
    a verdict's rows — only `horizon_days`, `forward_return_pct` and
    `classification` vary — so `values` is the first row's projection and
    loses nothing.
    """

    symbol: str
    strategy_key: str
    values: Mapping[str, object]
    outcomes: tuple[OutcomeCell, ...]

    def value(self, column: str) -> object:
        """Raw value of one collapsed column, or `None` when unselected."""
        return self.values.get(column)


def to_records(frame: pd.DataFrame) -> tuple[Mapping[str, object], ...]:
    """Convert a DataFrame to plain per-row mappings.

    Working in mappings rather than in pandas from here on keeps the view
    models testable with hand-written rows and keeps NumPy scalar semantics
    out of the templates.
    """
    if frame.empty:
        return ()
    return tuple(cast("list[Mapping[str, object]]", frame.to_dict(orient="records")))


def run_refs(frame: pd.DataFrame) -> tuple[RunRef, ...]:
    """Convert the `runs` frame into switcher entries."""
    return tuple(_run_ref(record) for record in to_records(frame))


def _run_ref(record: Mapping[str, object]) -> RunRef:
    return RunRef(
        run_id=str(record.get("run_id", "")),
        run_date=fmt.as_date(record.get("run_date")),
        mode=_text(record.get("mode")),
        status=_text(record.get("status")),
        status_tone=fmt.tone_of(fmt.RUN_STATUS_TONES, _text(record.get("status"))),
        config_hash=_text(record.get("config_hash")),
    )


def _text(value: object) -> str:
    return "" if fmt.is_missing(value) else str(value)


def optional_text(value: object) -> str | None:
    """Return a stripped string, or `None` when the value is absent/blank."""
    if fmt.is_missing(value):
        return None
    rendered = str(value).strip()
    return rendered or None


def status_badge(status: str) -> Badge:
    """Badge for one `runs.status` value."""
    return Badge(text=status or "不明", tone=fmt.tone_of(fmt.RUN_STATUS_TONES, status))


def verdict_badge(recommendation: str | None) -> Badge:
    """Badge for a `recommendation`, or the not-yet-archived token."""
    if recommendation is None:
        return NOT_INGESTED
    return Badge(
        text=recommendation,
        tone=fmt.tone_of(fmt.RECOMMENDATION_TONES, recommendation),
    )


def classification_badge(classification: str) -> Badge:
    """Badge for a matured HIT/MISS classification.

    Only called for a row that has one: `verdict_outcomes.classification` is
    `NOT NULL`, so a scorecard row with a `horizon_days` always has it, and a
    row without one is an immature verdict handled by `outcomes_fallback`.
    """
    return Badge(
        text=classification,
        tone=fmt.tone_of(fmt.CLASSIFICATION_TONES, classification),
    )


def risk_badge(status: object) -> Badge:
    """Badge for a `risk_assessments.status`, or the not-yet-archived token."""
    resolved = optional_text(status)
    if resolved is None:
        return NOT_INGESTED
    return Badge(text=resolved, tone=fmt.tone_of(fmt.RISK_STATUS_TONES, resolved))


def outcomes_fallback(entry: ScorecardEntry | None) -> fmt.Cell:
    """Which absence an empty outcome list means.

    No verdict row at all reads as `not_ingested` (the archive runs a day
    behind); a verdict whose horizons have not matured reads as `immature`.
    Collapsing the two would claim the analysis said nothing when it may
    simply not have been archived yet.
    """
    return fmt.missing("immature" if entry is not None else "not_ingested")


def score_component_stats(values: Mapping[str, object] | None) -> tuple[Stat, ...]:
    """The four ranking-score components as labelled figures.

    A `None` mapping means the symbol has no candidate row in this run at
    all, which reads as `absent` rather than as a zero contribution.
    """
    source: Mapping[str, object] = values or {}
    return tuple(
        Stat(
            label=label,
            value=fmt.number(source.get(column), digits=3, key="absent"),
        )
        for column, label in SCORE_COMPONENTS
    )


def aggregate_scorecard(frame: pd.DataFrame) -> dict[tuple[str, str], ScorecardEntry]:
    """Collapse `v_verdict_scorecard` to one entry per (symbol, strategy).

    The view's grain is (verdict x matured horizon): an immature verdict
    keeps one row with NULL horizon columns, and a matured one gains a row
    per horizon (5 and 20 sessions). Rendering that grain directly would
    double-count a symbol, so the horizon rows become `outcomes` and
    everything else is taken once.

    Args:
        frame: Rows from `queries.scorecard_for_run`.

    Returns:
        A mapping keyed by `(symbol, strategy_key)`.
    """
    entries: dict[tuple[str, str], ScorecardEntry] = {}
    outcomes: dict[tuple[str, str], list[OutcomeCell]] = {}
    for record in to_records(frame):
        key = (_text(record.get("symbol")), _text(record.get("strategy_key")))
        if key not in entries:
            entries[key] = ScorecardEntry(
                symbol=key[0], strategy_key=key[1], values=record, outcomes=()
            )
            outcomes[key] = []
        horizon = fmt.as_int(record.get("horizon_days"))
        classification = optional_text(record.get("classification"))
        if horizon is not None and classification is not None:
            outcomes[key].append(
                OutcomeCell(
                    horizon_days=horizon,
                    classification=classification_badge(classification),
                    forward_return=fmt.number(
                        record.get("forward_return_pct"),
                        suffix="%",
                        key="immature",
                        signed=True,
                    ),
                )
            )
    return {
        key: ScorecardEntry(
            symbol=entry.symbol,
            strategy_key=entry.strategy_key,
            values=entry.values,
            outcomes=tuple(sorted(outcomes[key], key=lambda o: o.horizon_days)),
        )
        for key, entry in entries.items()
    }
