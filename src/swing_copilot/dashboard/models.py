"""View-model types the templates render.

These carry `Cell`s, not raw DataFrame values: by the time a template sees a
number, the question "is this NULL, and *why*" has already been answered in
the view-model layer (`dashboard/viewmodels/`). Templates therefore never
branch on missingness, and a new NULL meaning is added in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

    from swing_copilot.dashboard.formatting import Cell, NullToken


@dataclass(frozen=True, slots=True)
class Badge:
    """A short state label plus the tone that colours it."""

    text: str
    tone: str
    title: str = ""


@dataclass(frozen=True, slots=True)
class RunRef:
    """One row of `runs`, as the header and the run switcher show it."""

    run_id: str
    run_date: date | None
    mode: str
    status: str
    status_tone: str
    config_hash: str

    @property
    def short_hash(self) -> str:
        """First 8 characters of `config_hash`; the whole hash is unreadable."""
        return self.config_hash[:8]

    @property
    def label(self) -> str:
        """`run_date` as the switcher lists it."""
        return "日付不明" if self.run_date is None else self.run_date.isoformat()


@dataclass(frozen=True, slots=True)
class Stat:
    """One labelled figure in the regime panel."""

    label: str
    value: Cell
    note: str = ""


@dataclass(frozen=True, slots=True)
class RegimePanel:
    """The deterministic market-regime state recorded for a run."""

    gate: Badge
    dd_level: Badge
    data_quality: Cell
    stats: tuple[Stat, ...]


@dataclass(frozen=True, slots=True)
class OutcomeCell:
    """One matured horizon's forward return and classification."""

    horizon_days: int
    classification: Badge
    forward_return: Cell


@dataclass(frozen=True, slots=True)
class SymbolRow:
    """One symbol's line on the run overview.

    The base population is the run's `v_candidates` rows — the deterministic
    screening output, which exists from the moment the pipeline finishes.
    `verdict`, `risk` and `outcomes` come from `v_verdict_scorecard`, which
    is populated one run later (the retro `collect` step archives run N's
    verdicts during run N+1), so they are frequently absent on the newest
    run and say so with the `not_ingested` token rather than a blank.
    """

    symbol: str
    strategy_key: str
    verdict: Badge
    rank: Cell
    score: Cell
    score_components: tuple[Stat, ...]
    risk_status: Badge
    binding_constraint: Cell
    outcomes: tuple[OutcomeCell, ...]
    #: Shown in place of an empty `outcomes`. Distinguishes "the horizon has
    #: not matured" from "this run has no verdict row at all".
    outcomes_fallback: Cell
    is_candidate: bool


@dataclass(frozen=True, slots=True)
class RejectionGroup:
    """Rejected symbols sharing one (stage, reason_code)."""

    stage: str
    stage_label: str
    reason_code: str
    count: int
    symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunOverview:
    """Everything `/runs/{run_id}` renders."""

    run: RunRef
    status_badge: Badge
    regime: RegimePanel | None
    rows: tuple[SymbolRow, ...]
    proceed_count: int
    skip_count: int
    no_trade: bool
    analysis_pending_note: str | None
    rejection_groups: tuple[RejectionGroup, ...]
    rejection_total: int
    legend: tuple[NullToken, ...]


@dataclass(frozen=True, slots=True)
class ReasonRow:
    """One verdict reason, in the order the analysis wrote it."""

    index: int
    text: str
    basis: Cell
    source_id_count: Cell


@dataclass(frozen=True, slots=True)
class TrackingPanel:
    """The virtual position the tracking ledger carries for one verdict."""

    recommendation: Badge
    status: Badge
    stats: tuple[Stat, ...]
    exit_reason: Cell


@dataclass(frozen=True, slots=True)
class SymbolDetail:
    """Everything `/runs/{run_id}/symbols/{symbol}` renders."""

    run: RunRef
    status_badge: Badge
    symbol: str
    strategy_key: str
    gics_sector: Cell
    verdict: Badge
    news_supply_level: Cell
    reasons: tuple[ReasonRow, ...]
    score_components: tuple[Stat, ...]
    technicals: tuple[Stat, ...]
    execution: tuple[Stat, ...]
    risk: tuple[Stat, ...]
    tracking: TrackingPanel | None
    outcomes: tuple[OutcomeCell, ...]
    outcomes_fallback: Cell
    legend: tuple[NullToken, ...]


@dataclass(frozen=True, slots=True)
class ClassificationBar:
    """One run date's classification counts inside one chart panel."""

    run_date: date
    counts: tuple[tuple[str, int], ...]
    total: int


@dataclass(frozen=True, slots=True)
class ClassificationPanel:
    """Matured classifications for one (recommendation, horizon) facet.

    Faceting by `recommendation` is mandatory, not cosmetic: since Issue
    #190 the ledger shadow-tracks `skip` verdicts as a counterfactual
    population, so a pooled average would mix a decision with its own
    control group.
    """

    recommendation: str
    horizon_days: int
    bars: tuple[ClassificationBar, ...]
    total: int


@dataclass(frozen=True, slots=True)
class RegimePoint:
    """One run's regime reading on the history timeline."""

    run_date: date
    vix_close: float | None
    dd_level: str | None
    gate_verdict: str | None


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One virtual position in the tracking ledger."""

    run_date: Cell
    symbol: str
    run_id: str
    recommendation: Badge
    entry_date: Cell
    entry_price: Cell
    stop_price: Cell
    days_held: Cell
    status: Badge
    exit_date: Cell
    exit_reason: Cell
    realized_return: Cell


@dataclass(frozen=True, slots=True)
class ClosedSummary:
    """Realized results for one `recommendation` stratum."""

    recommendation: str
    closed: int
    wins: int
    win_rate: Cell
    mean_return: Cell
    median_return: Cell


@dataclass(frozen=True, slots=True)
class HistoryView:
    """Everything `/history` renders."""

    panels: tuple[ClassificationPanel, ...]
    regime_points: tuple[RegimePoint, ...]
    open_positions: tuple[LedgerRow, ...]
    closed_summaries: tuple[ClosedSummary, ...]
    legend: tuple[NullToken, ...]
