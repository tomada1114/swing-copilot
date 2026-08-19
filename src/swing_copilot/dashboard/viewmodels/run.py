"""View model for the run overview page."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing_copilot.dashboard import formatting as fmt
from swing_copilot.dashboard import guidance
from swing_copilot.dashboard.models import (
    Badge,
    RegimePanel,
    RejectionGroup,
    RunOverview,
    Stat,
    SymbolRow,
)
from swing_copilot.dashboard.viewmodels import common

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pandas as pd

    from swing_copilot.dashboard.models import RunRef

#: Tokens this page can render, defined in its own footer legend.
LEGEND_KEYS = ("not_ingested", "immature", "absent", "unrecorded", "no_snapshot")

#: Screening stages in the order a symbol passes through them.
_STAGE_ORDER = ("data_quality", "fundamental_filter", "technical_signal")


@dataclass(frozen=True, slots=True)
class RunSources:
    """The raw frames one run overview is built from."""

    run: RunRef
    regime: pd.DataFrame
    candidates: pd.DataFrame
    scorecard: pd.DataFrame
    rejections: pd.DataFrame
    is_analysis_missing: bool


def build_run_overview(sources: RunSources) -> RunOverview:
    """Assemble everything `/runs/{run_id}` renders.

    Args:
        sources: Frames already narrowed to this run.

    Returns:
        A fully resolved `RunOverview`; no template branches on NULL.
    """
    entries = common.aggregate_scorecard(sources.scorecard)
    rows = _symbol_rows(sources.candidates, entries)
    recommendations = [
        common.optional_text(entry.value("recommendation"))
        for entry in entries.values()
    ]
    groups = _rejection_groups(sources.rejections)
    return RunOverview(
        run=sources.run,
        status_badge=common.status_badge(sources.run.status),
        regime=_regime_panel(sources.regime, sources.run.run_id),
        regime_hint=guidance.REGIME,
        rows=rows,
        verdict_hint=(
            None if sources.is_analysis_missing else guidance.VERDICT_INGESTION
        ),
        outcome_hint=guidance.OUTCOME,
        proceed_count=recommendations.count("proceed"),
        skip_count=recommendations.count("skip"),
        no_trade=any(bool(entry.value("no_trade")) for entry in entries.values()),
        analysis_pending_note=(
            guidance.ANALYSIS_PENDING if sources.is_analysis_missing else None
        ),
        rejection_groups=groups,
        rejection_total=sum(group.count for group in groups),
        legend=fmt.legend(LEGEND_KEYS),
    )


def _symbol_rows(
    candidates: pd.DataFrame,
    entries: Mapping[tuple[str, str], common.ScorecardEntry],
) -> tuple[SymbolRow, ...]:
    """Merge the run's candidates with whatever verdict rows exist.

    The two populations have deliberately different coverage: candidates are
    written by the run itself, while verdicts are archived by the next run's
    retro `collect`. A symbol may therefore appear in either alone — a fresh
    run has candidates and no verdicts, and a candidate the analysis skipped
    keeps a candidate row with no verdict — so both sides are kept.
    """
    rows: list[SymbolRow] = []
    seen: set[tuple[str, str]] = set()
    for record in common.to_records(candidates):
        key = (str(record.get("symbol", "")), str(record.get("strategy_key", "")))
        seen.add(key)
        rows.append(_symbol_row(key, record, entries.get(key)))
    for key, entry in sorted(entries.items()):
        if key not in seen:
            rows.append(_symbol_row(key, None, entry))
    return tuple(rows)


def _symbol_row(
    key: tuple[str, str],
    candidate: Mapping[str, object] | None,
    entry: common.ScorecardEntry | None,
) -> SymbolRow:
    values: Mapping[str, object] = candidate or {}
    verdict = common.verdict_badge(
        None if entry is None else common.optional_text(entry.value("recommendation"))
    )
    risk = (
        common.NOT_INGESTED
        if entry is None
        else common.risk_badge(entry.value("risk_status"))
    )
    binding = (
        fmt.missing("not_ingested")
        if entry is None
        else fmt.text(entry.value("binding_constraint"), key="absent")
    )
    return SymbolRow(
        symbol=key[0],
        strategy_key=key[1],
        verdict=verdict,
        rank=fmt.integer(values.get("rank"), key="absent"),
        score=fmt.number(values.get("score"), digits=3, key="absent"),
        score_components=common.score_component_stats(candidate),
        risk_status=risk,
        binding_constraint=binding,
        outcomes=() if entry is None else entry.outcomes,
        outcomes_fallback=common.outcomes_fallback(entry),
        is_candidate=candidate is not None,
    )


def _regime_panel(frame: pd.DataFrame, run_id: str) -> RegimePanel | None:
    """Build the regime panel, or `None` when this run recorded no snapshot."""
    record = next(
        (
            row
            for row in common.to_records(frame)
            if str(row.get("run_id", "")) == run_id
        ),
        None,
    )
    if record is None:
        return None
    gate = common.optional_text(record.get("gate_verdict"))
    dd_level = common.optional_text(record.get("dd_level"))
    return RegimePanel(
        gate=Badge(
            text=gate or fmt.NULL_TOKENS["no_snapshot"].label,
            tone=fmt.tone_of(fmt.GATE_TONES, gate),
        ),
        dd_level=Badge(
            text=dd_level or fmt.NULL_TOKENS["no_snapshot"].label,
            tone=fmt.tone_of(fmt.DD_LEVEL_TONES, dd_level),
        ),
        data_quality=fmt.text(record.get("data_quality"), key="no_snapshot"),
        stats=(
            Stat("VIX 終値", fmt.number(record.get("vix_close"), key="no_snapshot")),
            Stat(
                "SPY 25日DD回数",
                fmt.number(record.get("dd_count_spy"), digits=0, key="no_snapshot"),
            ),
            Stat(
                "QQQ 25日DD回数",
                fmt.number(record.get("dd_count_qqq"), digits=0, key="no_snapshot"),
            ),
            Stat(
                "SPY 15日/5日DD",
                _dd_pair(record.get("dd15_spy"), record.get("dd5_spy")),
            ),
            Stat("SPY 終値", fmt.number(record.get("spy_close"), key="no_snapshot")),
            Stat("SPY EMA", fmt.number(record.get("spy_ema"), key="no_snapshot")),
            Stat(
                "終値 vs EMA",
                _ema_gap(record.get("spy_close"), record.get("spy_ema")),
                note="正ならトレンドゲート通過側",
            ),
        ),
    )


def _dd_pair(dd15: object, dd5: object) -> fmt.Cell:
    """Render the 15/5-session drawdown counts as one `15 / 5` cell."""
    long_window = fmt.as_float(dd15)
    short_window = fmt.as_float(dd5)
    if long_window is None or short_window is None:
        return fmt.missing("no_snapshot")
    return fmt.Cell(text=f"{long_window:.0f} / {short_window:.0f}")


def _ema_gap(close: object, ema: object) -> fmt.Cell:
    """Render the SPY close-versus-EMA gap in percent."""
    price = fmt.as_float(close)
    trend = fmt.as_float(ema)
    if price is None or not trend:
        return fmt.missing("no_snapshot")
    return fmt.number((price / trend - 1.0) * 100.0, suffix="%", signed=True)


def _rejection_groups(frame: pd.DataFrame) -> tuple[RejectionGroup, ...]:
    """Group rejections by (stage, reason_code), largest group first."""
    buckets: dict[tuple[str, str], list[str]] = {}
    for record in common.to_records(frame):
        key = (str(record.get("stage", "")), str(record.get("reason_code", "")))
        buckets.setdefault(key, []).append(str(record.get("symbol", "")))
    groups = [
        RejectionGroup(
            stage=stage,
            stage_label=fmt.STAGE_LABELS.get(stage, stage),
            reason_code=reason_code,
            count=len(symbols),
            symbols=tuple(sorted(symbols)),
        )
        for (stage, reason_code), symbols in buckets.items()
    ]
    groups.sort(
        key=lambda group: (
            _STAGE_ORDER.index(group.stage)
            if group.stage in _STAGE_ORDER
            else len(_STAGE_ORDER),
            -group.count,
            group.reason_code,
        )
    )
    return tuple(groups)
