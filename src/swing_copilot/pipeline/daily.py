"""Daily batch orchestrator, all eight steps (FR-12).

Wires price update -> fundamentals update -> screening -> risk check ->
text collection -> analysis-input export -> notify -> CLI/Markdown output with
explicit `as_of`/`run_id` semantics. Steps 1-4 are fatal on failure
(screening cannot meaningfully proceed without them,
`docs/03_basic_design.md` 7): any of them failing aborts the run
(`runs.status=failed`, nonzero exit code) without touching steps 5-8.
Steps 5 (text) and 6 (analysis export) are fail-soft: their failure degrades
the run (`runs.status=degraded`) but never aborts it. Notification is optional
and the local output step always attempts to produce a screening-only brief.

Qualitative analysis itself is not performed here. Step 6 only exports
`analysis_input.json`; a Claude Code skill reads it and `copilot-ingest-analysis`
verifies the answer and re-renders the report. Reports produced by this module
therefore always show the qualitative sections as pending.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from swing_copilot import __version__
from swing_copilot.analysis.export import (
    ExportCandidate,
    ExportRequest,
    TextExportLimits,
    build_analysis_input,
    write_analysis_input,
)
from swing_copilot.analysis.snapshot import ReportContext, write_report_context
from swing_copilot.exceptions import ConfigError
from swing_copilot.models import (
    DailyRunOptions,
    DataTier,
    RunMode,
    RunStatus,
    StepStatus,
)
from swing_copilot.pipeline.earnings import collect_earnings_calendar
from swing_copilot.pipeline.postmortem import run_postmortem_step
from swing_copilot.pipeline.progress import NullProgressReporter, ProgressReporter
from swing_copilot.regime.distribution import DistributionThresholds
from swing_copilot.regime.exposure import ExposureDecision, determine_exposure
from swing_copilot.regime.ftd import FtdSnapshot, FtdThresholds, calculate_ftd_snapshot
from swing_copilot.regime.gate import (
    GateThresholds,
    RegimeSnapshot,
    RegimeThresholds,
    calculate_regime_snapshot,
)
from swing_copilot.report.daily_brief import (
    MARKET_STRIP_SYMBOLS,
    DailyBrief,
    DailyBriefContext,
    build_daily_brief,
)
from swing_copilot.report.markdown_report import (
    LatestMarkdownUpdateError,
    write_markdown_report,
)
from swing_copilot.report.rejections import RejectionsArtifact, write_rejections
from swing_copilot.retro.collect import collect_verdicts
from swing_copilot.retro.evaluate import EvaluationRequest, evaluate_verdicts
from swing_copilot.risk.checks import (
    EarningsGuardInput,
    PortfolioHeatResult,
    RiskChecker,
    RiskRunContext,
    calculate_portfolio_heat,
)
from swing_copilot.screening.base import ScreeningInput
from swing_copilot.screening.pipeline import (
    PRICE_HISTORY_LOOKBACK_DAYS,
    ScreeningPipeline,
    price_history_lookback_days,
    strategy_required_bars,
)
from swing_copilot.storage.audit_records import ScreeningRunMeta
from swing_copilot.storage.database import DEFAULT_DB_PATH
from swing_copilot.storage.market_store import FundamentalsFetchStamp
from swing_copilot.text.edgar_filings import (
    FilingLookbackBounds,
    fetch_recent_filings_text,
)
from swing_copilot.tracking.update import update_tracking
from swing_copilot.universe_sampling import select_universe_sample

logger = logging.getLogger(__name__)

_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from uuid import UUID

    import pandas as pd

    from swing_copilot.clock import Clock
    from swing_copilot.config import Settings
    from swing_copilot.data.base import BarFetchResult, DataProvider
    from swing_copilot.data.earnings import EarningsCalendarClient
    from swing_copilot.models import DailyRunResult, Position
    from swing_copilot.report.daily_brief import SignalPerformanceRow
    from swing_copilot.report.discord_notify import Notifier
    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.screening.base import (
        Candidate,
        RejectionRecord,
        ScreeningResult,
        TruncatedCandidate,
    )
    from swing_copilot.storage.market_store import (
        FundamentalsFetchState,
        FundamentalsRecord,
        MarketStore,
    )
    from swing_copilot.storage.state_store import StateStore
    from swing_copilot.text.base import TextItem
    from swing_copilot.universe import UniverseMember

_TEXT_LOOKBACK_DAYS = 14
#: Forms whose *full text* step 5 collects. This governs collection only; it
#: does **not** gate the fundamentals freshness trigger, which accepts any
#: collected filing regardless of form (`_FundamentalsFreshness`). Widening it
#: would widen the analysis export and add EDGAR requests, so the trigger is
#: deliberately decoupled from it rather than sharing it.
_FILING_FORM_TYPES = ["8-K", "10-Q"]
_TEXT_SYMBOL_LIMIT = (
    30  # held + candidates, capped per NFR-03 (docs/04_detailed_design.md 3.14)
)
#: `docs/03_basic_design.md` 8.3's "weekly" fundamentals refresh, in days.
#: Not a config knob: it is a fixed property of the NFR-03 time-budget design
#: (S&P 500 companies report quarterly, so a weekly poll cannot miss a
#: reporting cycle). It is also the width of the new-filing trigger's retry
#: window, which keeps that window from ever outliving the backstop.
_FUNDAMENTALS_REFRESH_INTERVAL_DAYS = 7
#: Retry gaps, in days, after 1/2/3 consecutive fetches that returned nothing;
#: from the fourth on, the ordinary interval applies again. This is what makes
#: an empty answer *converge* (Issue #258 review, second round): a transient
#: universe-wide empty response is retried tomorrow, while a symbol that will
#: never have XBRL facts -- a delisted shell, a 20-F foreign private issuer, a
#: trust -- backs off to the weekly cadence within a week instead of costing
#: one request every single day forever. The gaps sum to exactly
#: `_FUNDAMENTALS_REFRESH_INTERVAL_DAYS`, so the escalation never overshoots
#: the backstop it converges to.
_FUNDAMENTALS_EMPTY_BACKOFF_DAYS = (1, 2, 4)
#: SEC forms `EdgarClient.fetch_fundamentals` normalizes into a
#: `FundamentalsRecord` (mirrors `data/edgar.py`'s `_FUNDAMENTALS_FORMS`).
#: Used to ask "has the filing we know about actually landed in
#: `fundamentals` yet?", which is what disarms the new-filing trigger. 10-K is
#: included even though step 5 never collects 10-K *text*: what matters here
#: is which forms produce an ingested record, not which produce collected text.
_FUNDAMENTALS_INGESTED_FORMS = ("10-K", "10-Q")
#: How many symbols a fundamentals `run_steps.detail` enumerates before it
#: summarizes the rest as a count (`_summarize_symbols`).
_FUNDAMENTALS_DETAIL_SYMBOL_LIMIT = 10
#: How many of a symbol's earlier verdicts the export feeds back (Issue #191).
_PRIOR_VERDICT_LIMIT = 3
#: How many of a retro step's fail-soft notes fit in one `run_steps.detail`.
_RETRO_NOTE_DETAIL_LIMIT = 3
#: P8-117: shared between `daily_composition.py`'s preflight warning log and
#: `daily_runner.py`'s report `## Warnings` line, so both say the same thing.
ACCOUNT_EQUITY_UNSET_NOTICE = (
    "account_equity_usd が未設定です。株数と portfolio heat は not_calculable に"
    "なります。決済済みポジションを記録するとサーキットブレーカーが全候補を停止します。"
)
_VISIBLE_PIPELINE_STEPS = (
    "1_prices",
    "2_fundamentals",
    "3_screening",
    "4_risk",
    "5_text",
    "6_analysis_export",
    "7_notify",
    "8_output",
)


class _EdgarClientLike(Protocol):
    """Structural stand-in for `data.edgar.EdgarClient`, for fake injection."""

    def fetch_fundamentals(
        self, symbol: str, as_of: datetime
    ) -> list[FundamentalsRecord]:
        """Fetch normalized fundamentals filed on or before `as_of`."""
        # pragma: no cover

    def fetch_filing_texts(
        self,
        symbol: str,
        form_types: list[str],
        *,
        as_of: datetime,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[TextItem]:
        """Fetch recent filings' full text, normalized for text collection."""
        # pragma: no cover


class _NewsClientLike(Protocol):
    """Structural stand-in for `text.news_finnhub.FinnhubNewsClient`."""

    def fetch_company_news(
        self, symbol: str, since: date, *, as_of: date
    ) -> list[TextItem]:
        """Fetch recent news for `symbol` published on or after `since`."""
        # pragma: no cover


class _CalendarClientLike(Protocol):
    """Structural stand-in for `text.calendar_fred.FredCalendarClient`."""

    def fetch_calendar_events(
        self, start: date, end: date, *, as_of: date
    ) -> list[TextItem]:
        """Fetch economic release events in `[start, end]`, valued at `as_of`."""
        # pragma: no cover


@dataclass(frozen=True, slots=True)
class DailyDependencies:
    """Real collaborators `run_daily` composes together."""

    data_provider: DataProvider
    market_store: MarketStore
    state_store: StateStore
    settings: Settings
    universe: tuple[UniverseMember, ...]
    strategies_config: dict[str, Any]  # Any: arbitrary-depth parsed YAML
    clock: Clock
    # The exact persisted universe snapshot selected for this run. Manual
    # inclusions/exclusions are already reflected in `universe` and therefore
    # in the metadata identity below, but never mutate the raw snapshot.
    universe_snapshot_date: date | None = None
    # Present only when a live refresh failed and an older persisted snapshot
    # was selected. It is data quality, not a reason to skip screening.
    universe_warning: str | None = None
    # Injectable monotonic time source for the NFR-03 run-timeout budget.
    # Deliberately separate from `clock` (calendar/business time): this is
    # wall-clock elapsed-time measurement, never a substitute for `as_of`.
    monotonic: Callable[[], float] = time.perf_counter
    edgar_client: _EdgarClientLike | None = None
    earnings_client: EarningsCalendarClient | None = None
    news_client: _NewsClientLike | None = None
    calendar_client: _CalendarClientLike | None = None
    notifier: Notifier | None = None
    provider_name: str = "yfinance"
    data_tier: DataTier = DataTier.PROTOTYPE
    strategy_key: str = "default"
    output_dir: str = "reports"
    progress: ProgressReporter = field(default_factory=NullProgressReporter)


@dataclass(frozen=True, slots=True)
class _StepOutcome:
    success: bool
    detail: str | None = None
    is_skipped: bool = False


# A step skipped because the NFR-03 time budget is already exhausted: unlike
# an ordinary "not configured" skip (`success=True, is_skipped=True`), this
# degrades the run (`success=False`) even though it is recorded as `skipped`
# in `run_steps` rather than `failed`.
_TIME_BUDGET_STEP_OUTCOME = _StepOutcome(False, "time budget exceeded", is_skipped=True)


@dataclass(frozen=True, slots=True)
class _RunContext:
    """Screening-derived state steps 5-9 share (keeps step functions under 5 args)."""

    run_id: UUID
    run_date: date
    candidates: list[Candidate]
    rejections: list[RejectionRecord]
    # Symbols that cleared every stage but lost the `candidate_limit` cut.
    # They are in neither `candidates` nor `rejections`, so `rejections.json`
    # is the only place a run ever reports them.
    truncated: list[TruncatedCandidate]
    risk_assessments: list[RiskAssessment]
    portfolio_heat: PortfolioHeatResult
    earnings_guard_notice: str | None
    held_symbols: frozenset[str]
    regime_snapshot: RegimeSnapshot
    exposure_decision: ExposureDecision
    ftd_snapshot: FtdSnapshot
    # #273: earlier runs whose qualitative analysis never landed
    # (`_prior_analysis_gaps()`), threaded through so `_run_soft_steps` can
    # turn them into an operator-facing `brief.notices` line.
    analysis_gaps: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _RiskStepRequest:
    """Point-in-time inputs for one risk step."""

    candidates: list[Candidate]
    run_id: UUID
    as_of: date
    exposure: ExposureDecision
    is_historical: bool


@dataclass(frozen=True, slots=True)
class _OutputContext:
    """Grouped inputs for the final local-output step."""

    run: _RunContext
    # Set only when step 6 exported one; `_run_step_output` archives the
    # matching `report_context.json` beside it so ingest can re-render.
    analysis_input_path: Path | None
    analysis_input_digest: str | None
    signal_performance: tuple[SignalPerformanceRow, ...]
    notices: tuple[str, ...]
    status: RunStatus


@dataclass(frozen=True, slots=True)
class _OutputCompletion:
    """Everything needed to record the final run state after step 8."""

    outcome: _StepOutcome
    report_path: Path | None
    brief: DailyBrief | None
    analysis_input_path: Path | None
    text_outcome: _StepOutcome
    export_outcome: _StepOutcome


_RUN_METADATA_SCHEMA_VERSION = "run-metadata-v1"


def _canonical_json(payload: object) -> str:
    """Encode an audit payload deterministically before hashing or storage."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _config_hash(
    settings: Settings, strategies_config: dict[str, Any], strategy_key: str
) -> str:
    """Return the full effective-run fingerprint required for reconstruction.

    `Settings` and `StrategiesConfig` have already passed their strict
    Pydantic validation in the composition root. The pipeline receives the
    model-dumped selected strategy so the same exact values drive both the
    fingerprint and `ScreeningPipeline` without preserving a second config
    representation.
    """
    try:
        selected_strategy = strategies_config["strategies"][strategy_key]
    except (KeyError, TypeError) as exc:
        msg = f"strategy {strategy_key!r} is missing from validated strategies"
        raise ConfigError(msg) from exc
    payload = {
        "settings": settings.model_dump(mode="json"),
        "strategy_key": strategy_key,
        "strategy_spec": selected_strategy,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _universe_snapshot_identity(universe: tuple[UniverseMember, ...]) -> str:
    """Return a stable digest of the effective, point-in-time universe."""
    members = sorted(
        (
            {
                "symbol": member.symbol,
                "source_symbol": member.source_symbol,
                "company_name": member.company_name,
                "gics_sector": member.gics_sector,
            }
            for member in universe
        ),
        key=lambda member: member["symbol"],
    )
    return hashlib.sha256(_canonical_json(members).encode("utf-8")).hexdigest()


def _run_metadata(deps: DailyDependencies) -> dict[str, object]:
    """Build non-secret data required to reproduce a stored daily run."""
    return {
        "schema_version": _RUN_METADATA_SCHEMA_VERSION,
        "app_version": __version__,
        "provider": {
            "name": deps.provider_name,
            "data_tier": deps.data_tier.value,
        },
        "universe_snapshot": {
            "snapshot_date": (
                deps.universe_snapshot_date.isoformat()
                if deps.universe_snapshot_date is not None
                else None
            ),
            "identity": _universe_snapshot_identity(deps.universe),
        },
    }


def _run_mode(options: DailyRunOptions) -> RunMode:
    """Whether this invocation is `live` or `dry_run`.

    Shared by `run_daily` and `_compose_dependencies`, which both need it
    before a `run_id` exists.
    """
    return RunMode.DRY_RUN if options.is_dry_run else RunMode.LIVE


def _paths_for_mode(mode: RunMode) -> tuple[Path, str]:
    """Return the isolated `(db_path, output_dir)` to compose for `mode`.

    A `--dry-run` invocation must never touch the live DuckDB file or
    overwrite `reports/latest.md`: it gets its own DB file and its own
    report subdirectory. `runs.mode` still distinguishes dry/live rows
    *within* whichever DB a run wrote to (`docs/04_detailed_design.md` 3.21).

    Args:
        mode: Whether this run is `live` or `dry_run`.

    Returns:
        The DuckDB path and report output directory to compose with.
    """
    if mode is RunMode.DRY_RUN:
        return Path("data/copilot_dry_run.duckdb"), "reports/dry_run"
    return DEFAULT_DB_PATH, "reports"


def _select_symbols(
    universe: tuple[UniverseMember, ...], held_symbols: set[str], limit: int | None
) -> list[str]:
    """Symbols this run screens: the universe (or a `--limit` sample) plus holdings.

    `--limit` is a smoke/validation flag, but `universe[:limit]` made it mean
    "the N tickers starting with A" (the universe is `ORDER BY symbol`). That
    is not a subset of the S&P 500 in any useful sense: its sector mix is
    arbitrary, and Minervini's RS percentile (condition 7) ranks candidates
    *within the set it is given*, so a smoke run was evaluating a different
    check than production does. The backtest CLI already samples instead of
    truncating (Issue #194); this path shares that sampler (Issue #205).

    `held_symbols` is unioned in on *both* branches. Only the `--limit` branch
    used to do it, so the branch production actually runs (the 18:30 routine
    passes no `--limit`) silently dropped any holding that had left the
    universe snapshot -- and this return value is the sole input to the daily
    price fetch, so that symbol got no bar at all and its trailing-stop /
    max-hold checks ran on stale prices, exactly when an index deletion makes
    an exit decision most urgent (Issue #212). This adds symbols to the
    *fetch* set only; `_run_step_screening()` still intersects the screening
    universe with `deps.universe`, so a holding outside the snapshot cannot
    re-enter as a fresh entry candidate.

    Args:
        universe: Resolved universe membership for the run's `as_of`.
        held_symbols: Open holdings, always screened regardless of `--limit`.
        limit: `--limit`, or `None` for the whole universe. `0` selects no
            universe candidate at all and leaves only `held_symbols`.

    Returns:
        The symbols to fetch and screen, alphabetically ordered on both
        branches so a run's ordering stays reproducible. Sorting the full
        universe is a no-op in practice (`get_latest_universe_membership()`
        reads `ORDER BY symbol`); it only fixes the placement of
        `universe.manual_include` entries and of holdings outside the
        snapshot. Nothing downstream reads this order as data: screening
        receives it as a `set` and re-derives its own universe order from
        `deps.universe`, and the one step whose progress makes an order
        observable -- the NFR-03-budgeted fundamentals fetch -- re-orders it
        held-first for itself (`_fundamentals_fetch_order`).
    """
    if limit is None:
        return sorted({member.symbol for member in universe} | held_symbols)
    sample = select_universe_sample(universe, limit)
    if sample.is_stratified_sample:
        logger.info(
            "universe sampled by --limit: %s / %s",
            *sample.summary_lines(),
        )
    return sorted({*sample.symbols, *held_symbols})


def _text_target_symbols(
    held_symbols: frozenset[str], candidates: list[Candidate]
) -> list[str]:
    """Text/analysis target symbols: held positions + today's candidates (`docs/04_detailed_design.md` 3.14).

    Held-first ordering so a symbol truncated by the NFR-03 cap is always a
    candidate-only one, never a position the account actually holds.
    """
    candidate_symbols = {candidate.symbol for candidate in candidates}
    ordered = [*sorted(held_symbols), *sorted(candidate_symbols - held_symbols)]
    return ordered[:_TEXT_SYMBOL_LIMIT]


def _stamp_bars(
    bars: pd.DataFrame, provider_name: str, fetched_at: datetime
) -> pd.DataFrame:
    stamped = bars.copy()
    stamped["provider"] = provider_name
    stamped["fetched_at"] = fetched_at
    return stamped


def _screening_lookback_days(deps: DailyDependencies) -> int:
    """Calendar days of price history the configured strategy screens over.

    A broken strategy configuration (unknown key, unregistered component,
    invalid shape) stays the screening step's fatal error to report; the
    price fetch falls back to the floor lookback instead of failing first.
    """
    try:
        required = strategy_required_bars(
            deps.strategies_config, deps.settings, deps.strategy_key
        )
    except KeyError, ValidationError:
        return PRICE_HISTORY_LOOKBACK_DAYS
    return price_history_lookback_days(required)


def _run_step_prices(
    deps: DailyDependencies,
    symbols: list[str],
    as_of: date,
    prefetched: BarFetchResult | None = None,
) -> _StepOutcome:
    if prefetched is None:
        start = as_of - timedelta(days=_screening_lookback_days(deps))
        result = deps.data_provider.get_daily_bars(
            symbols, start, as_of + timedelta(days=1)
        )
    else:
        result = prefetched
    if result.bars.empty:
        return _StepOutcome(False, "no price data returned for any symbol")
    deps.market_store.write_bars(
        _stamp_bars(result.bars, deps.provider_name, deps.clock.now())
    )
    detail = (
        f"failed symbols: {[f.symbol for f in result.failures]}"
        if result.failures
        else None
    )
    return _StepOutcome(True, detail)


@dataclass(frozen=True, slots=True)
class _FundamentalsFreshness:
    """Which symbols the incremental fundamentals refresh still owes a fetch.

    Implements `docs/03_basic_design.md` 8.3's rule -- "weekly, and only for
    symbols with a new filing since the last fetch" -- as a decision made
    once per run from three batched reads, so the per-symbol loop adds no
    query and (crucially) no extra EDGAR request. A symbol is fetched when
    **any** of these holds; otherwise its network fetch is skipped:

    - it has never been fetched (nothing recorded, so nothing can be
      assumed stale *or* fresh);
    - its data horizon is `_FUNDAMENTALS_REFRESH_INTERVAL_DAYS` or more days
      old, or unknown (the "weekly" half, and the universal backstop -- it is
      the only rule that covers a symbol text collection never touched);
    - a filing we already know about has not landed in `fundamentals` yet
      (the "new filing" half, `_has_pending_filing`).

    ...unless EDGAR was already polled for it *today*, which short-circuits
    all of the above. That clause is P6-25's same-day rerun skip and it keys
    on the wall-clock fetch day rather than the horizon, so it survives any
    `--as-of`. Splitting the two facts apart is the whole reason
    `fundamentals_fetch_log` carries both `last_fetched_at` and
    `fetched_through`: a replay of a date older than the refresh interval did
    poll EDGAR today (do not poll again on a rerun) yet only reached a stale
    horizon (do not let tomorrow's real run think it is fresh). One column
    could serve only one of those, and the earlier revision of this branch
    picked each in turn and broke the other.

    The pending-filing trigger is a *bounded retry window*, not a one-shot
    edge (Issue #258 review finding 1). EDGAR's bulk company-facts endpoint
    publishes a filing's XBRL some time after the filing itself is
    retrievable, so the first fetch a filing triggers frequently returns
    nothing new. An edge that fired only on the day after the filing would
    stamp that empty fetch and then sit out the whole backstop interval,
    which is precisely the "held + candidates are picked up by the trigger"
    promise failing quietly. So the trigger stays armed every run until the
    record actually lands -- and no longer than
    `_FUNDAMENTALS_REFRESH_INTERVAL_DAYS` after the filing date, at which
    point the backstop owns the symbol anyway.

    The window's upper bound is what keeps this from reopening the hole the
    fetch log exists to close: an 8-K never becomes a `FundamentalsRecord`,
    so "retry until it lands" without a bound would poll that symbol on
    every run forever. Bounded, a symbol costs at most one request a day for
    one week per filing. The set that can be armed at once is *not* one
    run's 30 collected symbols -- `text_items` persists, so it is drawn from
    roughly a week of collection sets, on the order of 150-210 symbols -- but
    only those whose newest collected filing is both inside the window and
    not yet ingested are actually armed, which in steady state is the handful
    that filed that week.

    Comparisons are at *day* granularity and inclusive. A 10-Q accepted at
    16:30 ET is stored as that day's date at midnight UTC, i.e. earlier than
    the same day's fetch timestamp, so a strict instant comparison would
    silently drop a filing submitted after the run.

    Freshness is bookkeeping only. Nothing here loosens the point-in-time
    contract: `filed_at <= as_of` still decides what a fetch may return and
    what any reader may see -- every input below is already `as_of`-bounded
    by the query that produced it.
    """

    #: Wall-clock day (injected `Clock`). Used *only* for the two questions
    #: that are genuinely about the clock on the wall: has EDGAR already been
    #: polled for this symbol today, and has enough real time passed to poll
    #: again. Never for staleness -- see `as_of`.
    today: date
    #: The run's evaluation date, and the same coordinate `fetched_through`
    #: is recorded in. Staleness is measured here rather than against
    #: `today` because the two are systematically offset: a scheduled evening
    #: run resolves `run_date` to the newest bar it fetched -- the previous
    #: trading day, or the previous Friday on a Monday -- so a fetch made
    #: today records a horizon of yesterday. Comparing that horizon against
    #: the wall clock would count that offset as age on every single run and
    #: quietly shorten the refresh interval from seven days to four or six,
    #: depending on where the weekend fell. Both sides in `as_of` keeps the
    #: interval exactly seven days of evaluation time.
    as_of: date
    fetch_state: dict[str, FundamentalsFetchState]
    #: Newest filing date per symbol among filings already *collected* as
    #: text (`text_items`). Deliberately form-agnostic: whatever step 5
    #: collected counts, so this is strictly broader than
    #: `_FILING_FORM_TYPES` and a collected 10-K would arm the trigger too.
    latest_filing_on: dict[str, date]
    #: Newest filing date per symbol already *ingested* into `fundamentals`.
    #: Comparing the two is what tells a landed filing from a pending one.
    latest_ingested_on: dict[str, date]

    def needs_fetch(self, symbol: str) -> bool:
        """Return whether `symbol` still needs a network fetch this run.

        Two independent questions, in order: is the symbol *due* (its data is
        stale, or a filing it needs has not landed), and is another poll
        *allowed* yet (a run of empty answers throttles the rate). Keeping
        them apart is what bounds the worst case -- see `_is_due` for why the
        due-check must not read the poll clock, and `_poll_allowed` for why
        the throttle must not read the data clock.
        """
        state = self.fetch_state.get(symbol)
        if state is None:
            return True
        if state.last_fetched_on == self.today:
            # P6-25's same-day rerun skip, first because it is unconditional.
            # It keys on the *wall-clock* fetch day, so it holds for every
            # `--as-of` value -- including a replay far outside the refresh
            # interval, which the staleness rule would otherwise re-fetch on
            # every single rerun.
            return False
        return self._is_due(state, symbol) and self._poll_allowed(state)

    def _is_due(self, state: FundamentalsFetchState, symbol: str) -> bool:
        """Return whether `symbol`'s stored fundamentals want refreshing.

        Measured in `as_of` time against `fetched_through`, which only ever
        advances on a fetch that actually produced records. A fetch that came
        back empty moved no data, so it must not restart this clock: doing so
        let the pending-filing retries -- which exist precisely because
        EDGAR's bulk company-facts lags the filing -- push the backstop out by
        however long the retry window ran, and a symbol whose trigger fired
        ended up *staler* than one whose never did.
        """
        through = state.fetched_through_on
        if (
            through is None
            or (self.as_of - through).days >= _FUNDAMENTALS_REFRESH_INTERVAL_DAYS
        ):
            return True
        return self._has_pending_filing(symbol)

    def _poll_allowed(self, state: FundamentalsFetchState) -> bool:
        """Return whether enough time has passed since the last poll.

        Only a run of empty answers throttles anything: it widens the gap
        toward `_FUNDAMENTALS_REFRESH_INTERVAL_DAYS`, so a symbol that will
        never have XBRL facts converges on the ordinary cadence instead of
        being polled daily forever. Because that ceiling *is* the refresh
        interval, no symbol ever goes longer than one interval between polls
        -- the throttle can delay a poll, never suppress it indefinitely.
        """
        if state.consecutive_empty == 0:
            return True
        elapsed = (self.today - state.last_fetched_on).days
        return elapsed >= _refresh_interval_days(state.consecutive_empty)

    def has_ingested_filings(self, symbol: str) -> bool:
        """Return whether `fundamentals` already holds a filing for `symbol`."""
        return symbol in self.latest_ingested_on

    def consecutive_empty(self, symbol: str) -> int:
        """Return how many fetches in a row have come back empty for `symbol`."""
        state = self.fetch_state.get(symbol)
        return 0 if state is None else state.consecutive_empty

    def _has_pending_filing(self, symbol: str) -> bool:
        """Return whether a known filing is still missing from `fundamentals`.

        Also in `as_of` time: a filing date and the run's evaluation date are
        both data dates, and mixing in the wall clock would make the retry
        window a day shorter on every ordinary evening run.
        """
        filing_on = self.latest_filing_on.get(symbol)
        if filing_on is None:
            return False
        if (self.as_of - filing_on).days >= _FUNDAMENTALS_REFRESH_INTERVAL_DAYS:
            # Retry window closed. The backstop covers the symbol from here,
            # so an 8-K (which never produces a record) stops costing
            # requests instead of arming the trigger forever.
            return False
        ingested_on = self.latest_ingested_on.get(symbol)
        return ingested_on is None or ingested_on < filing_on


def _refresh_interval_days(consecutive_empty: int) -> int:
    """Return how many days `consecutive_empty` earns before the next fetch.

    Zero (the last fetch returned records) means the ordinary weekly
    backstop. A run of empty answers shortens the gap and then widens it back
    out along `_FUNDAMENTALS_EMPTY_BACKOFF_DAYS`, so the two requirements
    that pull in opposite directions are both met:

    - A universe-wide empty response -- the P6-25 incident shape -- is
      retried the very next day rather than freezing fundamentals for a week.
    - A symbol that never has XBRL facts converges on the weekly cadence
      instead of costing a request a day in perpetuity. Its total extra cost
      is the three shortened gaps, once, ever.

    Args:
        consecutive_empty: Fetches in a row that returned no record.

    Returns:
        Days that must have elapsed since the fetch horizon for the symbol to
        be due again.
    """
    if not 0 < consecutive_empty <= len(_FUNDAMENTALS_EMPTY_BACKOFF_DAYS):
        return _FUNDAMENTALS_REFRESH_INTERVAL_DAYS
    return _FUNDAMENTALS_EMPTY_BACKOFF_DAYS[consecutive_empty - 1]


def _load_fundamentals_freshness(
    deps: DailyDependencies, symbols: list[str], as_of: date, today: date
) -> _FundamentalsFreshness:
    """Batch-read the three inputs the incremental refresh rule decides from.

    All three are single batched queries against storage this run already
    owns, so the per-symbol loop adds no query and -- the point of the whole
    change -- no extra EDGAR request.

    Args:
        deps: Run dependencies (`market_store` fetch log and ingested filing
            history, `state_store` collected filing metadata).
        symbols: The step's full symbol list.
        as_of: The run's evaluation date -- both the point-in-time cutoff on
            the filing reads and the coordinate staleness is measured in.
        today: Wall-clock day, for the same-day skip and the poll throttle.

    Returns:
        The freshness decision object; see `_FundamentalsFreshness`.
    """
    return _FundamentalsFreshness(
        today=today,
        as_of=as_of,
        fetch_state=deps.market_store.read_fundamentals_fetch_state(symbols),
        latest_filing_on=deps.state_store.latest_filing_dates(symbols, as_of=as_of),
        latest_ingested_on=deps.market_store.read_latest_filing_dates(
            symbols, _FUNDAMENTALS_INGESTED_FORMS, as_of
        ),
    )


def _fetch_or_skip_fundamentals(
    freshness: _FundamentalsFreshness,
    edgar_client: _EdgarClientLike,
    symbol: str,
    as_of_cutoff: datetime,
) -> tuple[list[FundamentalsRecord], bool, bool]:
    """Fetch one symbol's fundamentals, or skip it as still fresh.

    Args:
        freshness: This run's precomputed incremental-refresh decision.
        edgar_client: Configured EDGAR client (never `None` here).
        symbol: Ticker to fetch.
        as_of_cutoff: `as_of` widened to end-of-day UTC for the filing cutoff.

    Returns:
        `(records, failed, was_skipped)`. `failed` is `True` only if the
        network fetch itself raised; a freshness skip is never a failure, and
        a fetch that legitimately returned no record is neither.
    """
    if not freshness.needs_fetch(symbol):
        logger.debug(
            "fundamentals: %s still fresh (%s), skipping fetch",
            symbol,
            freshness.fetch_state.get(symbol),
        )
        return [], False, True
    try:
        records = list(edgar_client.fetch_fundamentals(symbol, as_of_cutoff))
    except Exception:
        logger.exception("fundamentals fetch failed for %s", symbol)
        return [], True, False
    return records, False, False


def _fetch_horizon(fetched_at: datetime, as_of_cutoff: datetime) -> datetime:
    """Return how current this run's fundamentals fetch actually made a symbol.

    A fetch only ever retrieves filings with `filed_at <= as_of`, so what it
    buys is knowledge up to `as_of` -- not up to now. Recording the wall clock
    as the horizon would let one `--as-of <past date>` replay declare the
    whole universe fresh and suppress the operator's real refresh for a full
    `_FUNDAMENTALS_REFRESH_INTERVAL_DAYS`. Under the pre-#258 same-day-only
    skip the same mistake cost one day; the incremental rule turns it into a
    week, so the replay must not be able to make that claim.

    This is only half of the replay story. The horizon governs *staleness*;
    the same-day rerun skip governs *duplication* and is recorded separately
    as `last_fetched_at`, unclamped. Clamping both -- what the first attempt
    at this fix did -- made every rerun of an `as_of` older than the interval
    re-fetch the whole universe, because a stale horizon reads as "due".

    The clamp is **not** inert on the production path, contrary to what this
    said at first. `daily_runner` resolves an ordinary run's `run_date` to
    the newest bar it fetched, so the 18:30 JST schedule evaluates the
    previous trading day and every normal run records a horizon a day or a
    weekend behind the wall clock. That is the honest horizon -- the fetch
    really could not see a filing accepted after `as_of` -- and it is exactly
    why `_FundamentalsFreshness` measures staleness in `as_of` time rather
    than against `today`: comparing this horizon to the wall clock would bank
    that offset as age on every run and silently shorten the refresh interval
    to four or six days.

    Args:
        fetched_at: Wall-clock instant of the fetch, from the injected
            `Clock`.
        as_of_cutoff: `as_of` widened to end-of-day UTC -- the same value the
            fetch itself was bounded by.

    Returns:
        `min(fetched_at, as_of_cutoff)`.
    """
    return min(fetched_at, as_of_cutoff)


def _summarize_symbols(symbols: Sequence[str]) -> str:
    """Render a symbol list for `run_steps.detail` without flooding it.

    An outage or a systemic empty response can name every symbol in the
    universe, and a detail nobody can read is a detail nobody reads. The
    count is always exact; only the enumeration is cut.
    """
    if len(symbols) <= _FUNDAMENTALS_DETAIL_SYMBOL_LIMIT:
        return f"{len(symbols)} ({', '.join(symbols)})"
    shown = ", ".join(symbols[:_FUNDAMENTALS_DETAIL_SYMBOL_LIMIT])
    return f"{len(symbols)} ({shown}, +{len(symbols) - _FUNDAMENTALS_DETAIL_SYMBOL_LIMIT} more)"


def _log_fundamentals_progress(position: int, total: int) -> None:
    logger.debug("fundamentals: %d/%d symbols processed", position, total)


def _fundamentals_fetch_order(
    symbols: list[str], held_symbols: frozenset[str]
) -> list[str]:
    """Order the fundamentals fetch held-first, mirroring `_text_target_symbols`.

    The NFR-03 time budget truncates this step mid-sequence, so whatever sorts
    last is what silently loses today's fundamentals. Plain lexicographic order
    made that an arbitrary draw, and a held position sorting after the
    candidates lost its refresh to alphabetically-earlier candidates (Issue
    #219) -- the exact outcome `_text_target_symbols` already prevents on the
    text side. Both blocks keep the incoming (lexicographic) order, so the
    reordered sequence stays reproducible.

    Args:
        symbols: `_select_symbols()`'s return value, whose own lexicographic
            order contract is unchanged; the reorder is local to this step.
        held_symbols: Open holdings (real + virtual ledger), 3.14's held set.

    Returns:
        The same symbols, holdings first, each block in stable order.
    """
    held = [symbol for symbol in symbols if symbol in held_symbols]
    rest = [symbol for symbol in symbols if symbol not in held_symbols]
    return [*held, *rest]


def _run_step_fundamentals(
    deps: DailyDependencies,
    symbols: list[str],
    as_of: date,
    deadline: float,
    *,
    held_symbols: frozenset[str],
) -> _StepOutcome:
    """Fetch/upsert fundamentals for `symbols`, filed on or before `as_of`.

    Fail-soft/efficiency behaviors beyond a plain per-symbol fetch:

    - Incremental refresh (`_FundamentalsFreshness`, Issue #258): a symbol is
      re-fetched only when it has never been fetched, when its data horizon
      is `_FUNDAMENTALS_REFRESH_INTERVAL_DAYS` or more days old, or while a
      filing we already know about has still not landed in `fundamentals` --
      `docs/03_basic_design.md` 8.3's weekly/incremental rule. Ages are
      measured against the injected `Clock`'s wall-clock date, not `as_of`.
      Point-in-time correctness is unaffected: callers still read
      fundamentals filtered by `as_of`, never by fetch bookkeeping, and every
      fetch still upserts by `accession_no`.
    - **Never fatal.** A symbol whose fetch fails is recorded in the step
      detail and retried by the next run; the run keeps going. Making "every
      attempted symbol failed" fatal looked right while the step always
      attempted the whole universe, but under the incremental rule a typical
      day attempts nought to a couple of symbols, so a single permanently
      broken ticker would take the entire run down (exit 1, no report) every
      single day. A total EDGAR outage is instead visible in the detail --
      and escalates on its own, because failures are never stamped, so the
      backstop makes the whole universe due within a week and the detail then
      names every symbol.
    - Unexpectedly empty results: a fetch that returns no record for a symbol
      whose filing history we already hold contradicts what is on file, so it
      is treated like a failure -- not stamped, retried next run, named in
      the detail. Without that, one systemic empty response (the P6-25
      incident shape) would mark the whole universe fresh and freeze
      fundamentals for a week in silence. A symbol with *no* ingested filings
      is the opposite case: an empty result is the honest answer there, so it
      is stamped, which is what keeps such symbols from being re-polled on
      every run forever.
    - NFR-03 time budget: once `deps.monotonic() >= deadline`, fetching
      stops early with whatever records were already gathered upserted, and
      the step still succeeds with a detail explaining the partial
      completion.
    - Held-first fetch order (`_fundamentals_fetch_order`): because that
      budget cut is what makes the order observable at all, `held_symbols`
      goes first, so a truncated symbol is always a candidate-only one. This
      changes the fetch order only -- never the `filed_at <= as_of` cutoff
      applied to what is fetched.
    """
    edgar_client = deps.edgar_client
    if edgar_client is None:
        return _StepOutcome(
            True, "skipped: no EDGAR client configured", is_skipped=True
        )

    today = deps.clock.today()
    freshness = _load_fundamentals_freshness(deps, symbols, as_of, today)
    as_of_cutoff = datetime.combine(as_of, datetime.max.time(), tzinfo=UTC)
    total = len(symbols)
    records: list[FundamentalsRecord] = []
    failed_symbols: list[str] = []
    empty_symbols: list[str] = []
    # The subset of `empty_symbols` whose emptiness contradicts filings we
    # already hold; that is the operator-actionable signal, whereas a symbol
    # that has simply never had facts is expected to answer empty.
    contradicting_symbols: list[str] = []
    fetched_symbols: list[str] = []
    skipped_fresh = 0
    budget_detail: str | None = None

    for index, symbol in enumerate(_fundamentals_fetch_order(symbols, held_symbols)):
        if deps.monotonic() >= deadline:
            budget_detail = f"time budget exceeded after {index}/{total} symbols"
            logger.warning("fundamentals step stopping early: %s", budget_detail)
            break
        symbol_records, failed, was_skipped = _fetch_or_skip_fundamentals(
            freshness, edgar_client, symbol, as_of_cutoff
        )
        records.extend(symbol_records)
        if was_skipped:
            skipped_fresh += 1
        elif failed:
            failed_symbols.append(symbol)
        elif not symbol_records:
            # EDGAR answered, but with nothing. The symbol *is* stamped --
            # leaving it unstamped is what made the retry unbounded -- but
            # its `consecutive_empty` shortens the next gap and then backs it
            # off (`_refresh_interval_days`), so a systemic empty response is
            # retried tomorrow while a permanently factless symbol converges
            # on the weekly cadence. It is only *reported* when it also
            # contradicts filings we already hold, since that is the case an
            # operator can act on.
            empty_symbols.append(symbol)
            if freshness.has_ingested_filings(symbol):
                logger.warning(
                    "fundamentals: %s returned no records despite stored filings",
                    symbol,
                )
                contradicting_symbols.append(symbol)
        else:
            fetched_symbols.append(symbol)
        _log_fundamentals_progress(index + 1, total)
        deps.progress.substep(index + 1, total, "fundamentals")

    if skipped_fresh:
        logger.debug(
            "fundamentals: skipped %d/%d symbol(s) still fresh",
            skipped_fresh,
            total,
        )
    if records:
        deps.market_store.upsert_fundamentals(records)
    # Stamped only after the upsert commits, and only for symbols whose fetch
    # actually reached EDGAR: a failed fetch must be retried by the next run,
    # and a failed upsert must not leave the symbols it lost marked as
    # fetched. An empty answer *is* stamped -- with a bumped
    # `consecutive_empty` and a `None` horizon, so it throttles the retry
    # without restarting the staleness clock it moved nothing on.
    fetched_at = deps.clock.now()
    fetched_through = _fetch_horizon(fetched_at, as_of_cutoff)
    deps.market_store.record_fundamentals_fetches(
        [
            FundamentalsFetchStamp(
                symbol=symbol,
                last_fetched_at=fetched_at,
                fetched_through=None if is_empty else fetched_through,
                consecutive_empty=(
                    freshness.consecutive_empty(symbol) + 1 if is_empty else 0
                ),
            )
            for symbol, is_empty in (
                *((symbol, False) for symbol in fetched_symbols),
                *((symbol, True) for symbol in empty_symbols),
            )
        ]
    )

    return _StepOutcome(
        True,
        _fundamentals_detail(
            _FundamentalsStepTally(
                failed_symbols,
                len(empty_symbols),
                contradicting_symbols,
                fetched_symbols,
                budget_detail,
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class _FundamentalsStepTally:
    """What one fundamentals step attempted, for `run_steps.detail`."""

    failed: list[str]
    #: Every symbol whose fetch came back with no record. Counted, not named:
    #: a symbol that has never had facts answers empty every time it comes
    #: due, and listing it would be pure noise.
    empty_count: int
    #: The empty answers worth naming -- the ones that contradict filings
    #: already on file, which is what an operator can act on.
    contradicting: list[str]
    fetched: list[str]
    budget_detail: str | None


def _fundamentals_detail(tally: _FundamentalsStepTally) -> str | None:
    """Summarize the step for `run_steps.detail`, or `None` if unremarkable.

    This detail is the operator's only signal that fundamentals did not
    refresh, because the step is never fatal (see `_run_step_fundamentals`).
    So it names the "nothing at all got through" case explicitly rather than
    leaving it to be inferred from a list of symbols.
    """
    attempted = len(tally.failed) + tally.empty_count + len(tally.fetched)
    details: list[str] = []
    if attempted and not tally.fetched and tally.budget_detail is None:
        message = (
            f"EDGAR refreshed nothing: all {attempted} attempted symbol(s) "
            f"failed or returned no records"
        )
        logger.error("fundamentals step: %s", message)
        details.append(message)
    if tally.failed:
        details.append(f"failed symbols: {_summarize_symbols(tally.failed)}")
    if tally.contradicting:
        details.append(
            f"no records despite stored filings: "
            f"{_summarize_symbols(tally.contradicting)}"
        )
    if tally.budget_detail:
        details.append(tally.budget_detail)
    return "; ".join(details) if details else None


def _run_step_screening(
    deps: DailyDependencies, symbols: list[str], as_of: date, run_id: UUID
) -> tuple[_StepOutcome, ScreeningResult]:
    fundamentals = deps.market_store.read_fundamentals(as_of)
    pipeline = ScreeningPipeline(
        deps.strategies_config, deps.market_store, deps.settings, deps.strategy_key
    )
    start = as_of - timedelta(days=price_history_lookback_days(pipeline.required_bars))
    bars = deps.market_store.read_bars(symbols, start, as_of, as_of)

    # Scope `ScreeningInput.universe` to this run's actual `symbols` (which
    # `--limit` may have narrowed from `deps.universe`), not the full
    # membership. `Filter`/`Signal.apply()`/`.evaluate()` already tolerate
    # universe members with no fetched bars/fundamentals (they're simply
    # never added to a passing set), so this was always harmless for
    # `Candidate` output -- but P1-02's rejection classifier now iterates
    # every `data.universe` member, so an unscoped universe would otherwise
    # misclassify hundreds of never-fetched `--limit`-excluded symbols as
    # genuine rejections (e.g. spurious DATA_INSUFFICIENT_HISTORY).
    symbol_set = set(symbols)
    universe = tuple(member for member in deps.universe if member.symbol in symbol_set)
    data = ScreeningInput(
        as_of=as_of, universe=universe, fundamentals=fundamentals, bars=bars
    )
    pipeline = ScreeningPipeline(
        deps.strategies_config, deps.market_store, deps.settings, deps.strategy_key
    )
    result = pipeline.run_with_rejections(data)
    deps.state_store.record_screening_results(
        result,
        ScreeningRunMeta(
            run_id, pipeline.strategy_key, as_of, pipeline.candidate_limit
        ),
    )
    return _StepOutcome(True), result


def _run_step_risk(
    deps: DailyDependencies,
    request: _RiskStepRequest,
) -> tuple[_StepOutcome, list[RiskAssessment], PortfolioHeatResult, str | None]:
    # The account holds nothing this process knows about: the real-trade record
    # feature (FR-11/CON-04's `positions`) was removed in 2026-08, so sizing,
    # concentration, correlation and portfolio heat are all computed against an
    # empty book. The virtual verdict ledger is deliberately *not* substituted
    # here -- a position nobody actually took must never reach risk as if the
    # account held it (`daily_runner._held_symbols` says the same thing from
    # the collection side).
    portfolio: list[Position] = []
    # The realized-P&L circuit breaker had exactly one input source, closed
    # rows in `positions`, and it went with them. `None` is the checker's
    # existing "not evaluated" value, behaviourally identical to
    # `TRADING_ALLOWED` in `RiskChecker._apply_circuit_breaker`, which is what
    # an empty journal produced before. The breaker itself survives in
    # `backtest/policy.py`, fed by the simulator's own realized trades.
    earnings = collect_earnings_calendar(
        deps.earnings_client,
        sorted(
            {
                *(position.symbol for position in portfolio),
                *(candidate.symbol for candidate in request.candidates),
            }
        ),
        request.as_of,
        deps.state_store,
        lookahead_days=deps.settings.risk.earnings_lookahead_days,
        is_historical=request.is_historical,
    )
    checker = RiskChecker(
        deps.settings,
        deps.universe,
        deps.market_store,
        RiskRunContext(
            earnings_guard=EarningsGuardInput(
                earnings.is_enabled, earnings.lookups_by_symbol
            ),
            circuit_breaker=None,
        ),
    )
    assessments = checker.check(
        request.candidates,
        portfolio,
        deps.settings.risk.account_equity_usd,
        request.exposure,
    )
    deps.state_store.record_risk_assessments(assessments, request.run_id)
    base_heat = calculate_portfolio_heat(
        portfolio,
        deps.settings.risk.account_equity_usd,
    )
    final_heat = (
        replace(base_heat, heat_pct=assessments[-1].portfolio_heat_pct)
        if base_heat.status == "calculated" and assessments
        else base_heat
    )
    return _StepOutcome(True), assessments, final_heat, earnings.notice


def _calculate_regime_snapshot(deps: DailyDependencies, as_of: date) -> RegimeSnapshot:
    """Calculate the code-owned market regime from point-in-time store reads."""
    history_start = as_of - timedelta(days=2 * PRICE_HISTORY_LOOKBACK_DAYS)
    bars = deps.market_store.read_bars(
        list(MARKET_STRIP_SYMBOLS), history_start, as_of, as_of
    )
    config = deps.settings.regime
    thresholds = RegimeThresholds(
        gate=GateThresholds(
            ema_period=config.ema_period,
            bull_vix_max=config.bull_vix_max,
            bear_spy_ema_ratio=config.bear_spy_ema_ratio,
            bear_vix_min=config.bear_vix_min,
        ),
        distribution=DistributionThresholds(
            window_days=config.distribution_window_days,
            dd_decline_pct=config.dd_decline_pct,
            stall_abs_change_pct=config.stall_abs_change_pct,
            recovery_pct=config.recovery_pct,
            severe_d25=config.dd_severe_d25,
            severe_d15=config.dd_severe_d15,
            high_d25=config.dd_high_d25,
            high_d15=config.dd_high_d15,
            high_d5=config.dd_high_d5,
            caution_d25=config.dd_caution_d25,
        ),
    )
    return calculate_regime_snapshot(
        bars.loc[bars["symbol"] == "SPY"],
        bars.loc[bars["symbol"] == "QQQ"],
        bars.loc[bars["symbol"] == "^VIX"],
        as_of,
        thresholds=thresholds,
    )


def _record_regime_snapshot(
    deps: DailyDependencies, run_id: UUID, as_of: date
) -> RegimeSnapshot:
    """Compute and persist one run's deterministic market-regime state."""
    snapshot = _calculate_regime_snapshot(deps, as_of)
    deps.state_store.record_regime_snapshot(run_id, snapshot)
    return snapshot


def _record_exposure_decision(
    deps: DailyDependencies, run_id: UUID, snapshot: RegimeSnapshot
) -> ExposureDecision:
    """Derive and persist one immutable-in-run Exposure Ceiling decision."""
    decision = determine_exposure(
        snapshot,
        reduce_only_risk_multiplier=deps.settings.regime.reduce_only_risk_multiplier,
    )
    deps.state_store.record_exposure_decision(run_id, decision)
    return decision


def _record_ftd_snapshot(
    deps: DailyDependencies, run_id: UUID, as_of: date
) -> FtdSnapshot:
    """Calculate and persist display-only FTD transitions for both indices."""
    history_start = as_of - timedelta(days=2 * PRICE_HISTORY_LOOKBACK_DAYS)
    bars = deps.market_store.read_bars(["SPY", "QQQ"], history_start, as_of, as_of)
    config = deps.settings.regime
    snapshot = calculate_ftd_snapshot(
        bars.loc[bars["symbol"] == "SPY"],
        bars.loc[bars["symbol"] == "QQQ"],
        as_of,
        thresholds=FtdThresholds(
            correction_decline_pct=config.ftd_correction_decline_pct,
            correction_down_days=config.ftd_correction_down_days,
            ftd_gain_pct=config.ftd_gain_pct,
        ),
    )
    deps.state_store.record_ftd_history(run_id, snapshot)
    return snapshot


def _fetch_symbol_text_items(
    deps: DailyDependencies, symbol: str, since: date, as_of: date
) -> tuple[list[TextItem], bool]:
    """Fetch one symbol's news + filing text; `False` if either source raised."""
    items: list[TextItem] = []
    symbol_ok = True
    if deps.news_client is not None:
        try:
            items.extend(
                deps.news_client.fetch_company_news(symbol, since, as_of=as_of)
            )
        except Exception:
            logger.exception("news fetch failed for %s", symbol)
            symbol_ok = False
    if deps.edgar_client is not None:
        try:
            items.extend(
                fetch_recent_filings_text(
                    deps.edgar_client,
                    symbol,
                    _FILING_FORM_TYPES,
                    as_of,
                    FilingLookbackBounds(
                        lookback_days=deps.settings.analysis.filing_lookback_days,
                        limit=deps.settings.analysis.max_filings_per_symbol,
                    ),
                )
            )
        except Exception:
            logger.exception("filings text fetch failed for %s", symbol)
            symbol_ok = False
    return items, symbol_ok


def _fetch_calendar_items(
    deps: DailyDependencies, as_of: date
) -> tuple[list[TextItem], bool]:
    """Fetch calendar events; second element is `True` if the client raised."""
    if deps.calendar_client is None:
        return [], False
    try:
        return list(
            deps.calendar_client.fetch_calendar_events(
                as_of, as_of + timedelta(days=14), as_of=as_of
            )
        ), False
    except Exception:
        logger.exception("calendar events fetch failed")
        return [], True


def _text_step_outcome(
    items: list[TextItem],
    failed_symbols: list[str],
    calendar_failed: bool,
    symbol_count: int,
) -> tuple[_StepOutcome, list[TextItem] | None]:
    if not (failed_symbols or calendar_failed):
        return _StepOutcome(True), items
    if items:
        detail = f"failed symbols: {failed_symbols}"
        if calendar_failed:
            detail += "; calendar events fetch failed"
        return _StepOutcome(False, detail), items

    # Nothing was collected at all: state truthfully *why*, distinguishing a
    # calendar-only failure with no target symbols from genuine per-symbol
    # failures -- a prior "text collection failed for every symbol/event"
    # wording was misleading on a day with 0 candidates/0 positions and one
    # transient calendar failure.
    if symbol_count == 0:
        detail = (
            "calendar fetch failed; no target symbols (0 candidates, 0 held positions)"
        )
    elif failed_symbols and calendar_failed:
        detail = (
            f"calendar fetch failed and per-symbol fetch failed for "
            f"{len(failed_symbols)}/{symbol_count} target symbol(s): {failed_symbols}"
        )
    elif failed_symbols:
        detail = (
            f"per-symbol fetch failed for {len(failed_symbols)}/{symbol_count} "
            f"target symbol(s): {failed_symbols}"
        )
    else:
        detail = (
            f"calendar fetch failed; {symbol_count} target symbol(s) "
            "returned no text items"
        )
    return _StepOutcome(False, detail), None


def _deduplicate_text_items(items: list[TextItem]) -> list[TextItem]:
    """Keep one item per `source_id`: the first symbol that collected it.

    Finnhub's company-news feed returns the same article under more than one
    ticker (sector round-ups, peer comparisons), and `TextItem.source_id`
    carries no symbol component. Without this, one article reaches two
    candidates' `news` arrays as if each had independent coverage, and
    `text_items` (`PRIMARY KEY (source_id)`) keeps whichever symbol happened to
    be written last. `_text_target_symbols` orders held positions first and
    then alphabetically, so a symbol the account actually holds keeps the
    shared article; otherwise the alphabetically first candidate does.

    Args:
        items: Collected text in fetch order.

    Returns:
        The same items, minus later occurrences of an already-seen `source_id`.
    """
    seen: set[str] = set()
    unique: list[TextItem] = []
    for item in items:
        if item.source_id in seen:
            continue
        seen.add(item.source_id)
        unique.append(item)
    return unique


def _run_step_text(
    deps: DailyDependencies, symbols: list[str], as_of: date, *, skip: bool
) -> tuple[_StepOutcome, list[TextItem] | None]:
    if skip or (
        deps.news_client is None
        and deps.calendar_client is None
        and deps.edgar_client is None
    ):
        return (
            _StepOutcome(True, "skipped: no text clients configured", is_skipped=True),
            None,
        )

    since = as_of - timedelta(days=_TEXT_LOOKBACK_DAYS)
    items: list[TextItem] = []
    failed_symbols: list[str] = []
    for symbol in symbols:
        symbol_items, symbol_ok = _fetch_symbol_text_items(deps, symbol, since, as_of)
        items.extend(symbol_items)
        if not symbol_ok:
            failed_symbols.append(symbol)

    calendar_items, calendar_failed = _fetch_calendar_items(deps, as_of)
    items.extend(calendar_items)
    items = _deduplicate_text_items(items)

    if items:
        deps.state_store.record_text_items(items)

    return _text_step_outcome(items, failed_symbols, calendar_failed, len(symbols))


def _run_output_dir(deps: DailyDependencies, run_date: date, run_id: UUID) -> Path:
    """Return the dedicated artifact directory for one immutable daily run.

    Markdown archives remain under their dated directory for stable report
    links, while input/context/result/work artifacts live below their exact
    UUID. This preserves every same-day run without trusting a shared path.
    """
    return Path(deps.output_dir) / run_date.isoformat() / str(run_id)


def _run_step_analysis_export(
    deps: DailyDependencies,
    ctx: _RunContext,
    text_items: list[TextItem] | None,
    *,
    include_prior_verdicts: bool,
) -> tuple[_StepOutcome, Path | None, str | None]:
    """Export `analysis_input.json` for the qualitative-analysis skill.

    No model is called here, so this step is cheap and unconditional -- it is
    skipped only when there is genuinely nothing to analyze.
    `include_prior_verdicts` carries the existing point-in-time invariant: the
    analysis layer's own earlier judgements are injected only for a live run of
    the current day, never for a `--dry-run` or an `--as-of` replay.

    Args:
        deps: Run dependencies.
        ctx: Screening/risk state for this run.
        text_items: Step 5's collected text, or `None` if it produced none.
        include_prior_verdicts: Whether prior verdicts may be injected.

    Returns:
        The step outcome and, on success, the exported file's absolute path.
    """
    if not ctx.candidates:
        return (
            _StepOutcome(True, "skipped: no candidates to analyze", is_skipped=True),
            None,
            None,
        )
    if text_items is None:
        return (
            _StepOutcome(True, "skipped: step 5 produced no text", is_skipped=True),
            None,
            None,
        )

    try:
        payload = build_analysis_input(
            _export_request(
                deps, ctx, text_items, include_prior_verdicts=include_prior_verdicts
            )
        )
        path = write_analysis_input(
            payload, _run_output_dir(deps, ctx.run_date, ctx.run_id)
        )
    except Exception as exc:
        # Fail-soft like every other step here: an export problem (disk, or
        # unexpectedly unserializable state) degrades the run and still lets
        # step 8 produce a screening-only report.
        logger.exception("analysis input export failed")
        return _StepOutcome(False, f"analysis input export failed: {exc}"), None, None
    logger.info("analysis input exported: %s", path)
    return _StepOutcome(True, f"exported {path}"), path, payload.input_digest


def _export_request(
    deps: DailyDependencies,
    ctx: _RunContext,
    text_items: list[TextItem],
    *,
    include_prior_verdicts: bool,
) -> ExportRequest:
    """Group one run's export inputs, one `ExportCandidate` per candidate.

    `risk_by_symbol[candidate.symbol]` is a plain (not `.get()`) lookup:
    `RiskChecker.check()` guarantees one `RiskAssessment` per candidate, in
    the same order as `candidates` (`risk/checks.py`), so `ctx.risk_assessments`
    always covers every `ctx.candidates` entry.

    Calendar/macro `TextItem`s (`symbol is None`) never match any candidate's
    filter below, so they are collected separately as `ExportRequest.calendar_events`
    -- run-wide context every candidate's analysis may cite (`analysis/validate.py`).
    """
    risk_by_symbol = {
        assessment.symbol: assessment for assessment in ctx.risk_assessments
    }
    analysis_config = deps.settings.analysis
    return ExportRequest(
        as_of=ctx.run_date,
        run_id=ctx.run_id,
        strategy_key=deps.strategy_key,
        generated_at=deps.clock.now(),
        regime_snapshot=ctx.regime_snapshot,
        exposure_decision=ctx.exposure_decision,
        candidates=tuple(
            ExportCandidate(
                candidate=candidate,
                risk_assessment=risk_by_symbol[candidate.symbol],
                text_items=tuple(
                    item for item in text_items if item.symbol == candidate.symbol
                ),
                prior_verdicts=deps.state_store.get_prior_verdicts(
                    candidate.symbol,
                    deps.strategy_key,
                    ctx.run_date,
                    _PRIOR_VERDICT_LIMIT,
                )
                if include_prior_verdicts
                else (),
            )
            for candidate in ctx.candidates
        ),
        limits=TextExportLimits(
            max_news_items=analysis_config.max_news_items_per_symbol,
            max_news_chars=analysis_config.max_news_chars_per_item,
            max_filing_chars=analysis_config.max_filing_chars,
            max_filing_chars_per_symbol=(analysis_config.max_filing_chars_per_symbol),
            max_calendar_events=analysis_config.max_calendar_events,
            max_calendar_chars=analysis_config.max_calendar_chars_per_item,
            sufficient_news_mention_items=(
                analysis_config.sufficient_news_mention_items
            ),
        ),
        calendar_events=tuple(
            item for item in text_items if item.source_type == "calendar"
        ),
    )


def _run_step_postmortem(
    deps: DailyDependencies, run_date: date
) -> tuple[_StepOutcome, tuple[SignalPerformanceRow, ...]]:
    """P2-11: classify past candidates' forward returns, then aggregate (fail-soft).

    `run_postmortem_step` itself never raises for expected conditions (no
    prior run at a horizon, missing bars) -- only a genuinely unexpected
    exception (e.g. a DB connectivity failure) would reach this wrapper,
    which converts it into a degraded (not fatal) step outcome, mirroring
    the fatal-steps loop's own `try/except Exception` conversion in
    `run_daily`.
    """
    try:
        note, performance = run_postmortem_step(
            deps.market_store,
            deps.state_store,
            run_date,
            deps.settings.postmortem,
            deps.settings.backtest.benchmark,
        )
    except Exception as exc:
        logger.exception("postmortem step raised unexpectedly")
        return _StepOutcome(False, f"unexpected error: {exc}"), ()
    return _StepOutcome(True, note), performance


def _retro_step_detail(summary_line: str, notes: tuple[str, ...]) -> str:
    """Join a retro step's counts with a bounded excerpt of its notes.

    `collect`/`evaluate` are fail-soft per run and per symbol, so their notes
    carry the real data-quality signal (a run archived without
    `analysis_result.json`, an unresolvable `source_id`, a missing bar). They
    must not be swallowed, but `run_steps.detail` is a single audit column: the
    full list is logged, and only the first few notes plus a remainder count
    are stored.
    """
    for note in notes:
        logger.info("retro step note: %s", note)
    if not notes:
        return summary_line
    excerpt = list(notes[:_RETRO_NOTE_DETAIL_LIMIT])
    remainder = len(notes) - len(excerpt)
    if remainder > 0:
        excerpt.append(f"(+{remainder} more)")
    return f"{summary_line} / notes: " + "; ".join(excerpt)


def _run_step_retro_collect(deps: DailyDependencies) -> _StepOutcome:
    """P8-30: archive the day's verdicts into DuckDB (fail-soft).

    `reports/<date>/<run_id>/analysis_result.json` is the only artifact the
    daily loop never writes to the database, so running the retrospective's
    own collector here keeps the archive backed up and stops a run from aging
    out of the evaluation window while nobody triggers `copilot-retro`
    manually. The scan is offline and idempotent (run-scoped
    DELETE-then-INSERT), so a daily repetition changes nothing but recency.

    The step runs ahead of step 6 so that the previous run's verdicts are in
    the database before the export builds `<prior_verdicts>` (Issue #207); at
    that point the current run's own directory does not exist yet, and its
    `analysis_result.json` would in any case only be written later by the
    skill's ingest. Today's run is therefore simply not scanned, and any run
    whose skill answer was never ingested becomes a note -- the collector's
    normal fail-soft outcome, not a degradation.

    Issue #209: the scan re-parses and re-writes only the archives whose
    documents changed, so the cost it adds in front of the export stops
    growing with the length of the history.
    """
    try:
        summary = collect_verdicts(deps.state_store, Path(deps.output_dir))
    except Exception as exc:
        logger.exception("retro collect step raised unexpectedly")
        return _StepOutcome(False, f"unexpected error: {exc}")
    return _StepOutcome(
        True,
        _retro_step_detail(
            f"collected {summary.collected_run_count}/{summary.scanned_run_count} run(s), "
            f"{summary.unchanged_run_count} unchanged, "
            f"{summary.verdict_count} verdict(s)",
            summary.notes,
        ),
    )


def _run_step_retro_evaluate(deps: DailyDependencies, as_of: date) -> _StepOutcome:
    """P8-30: classify the verdicts whose horizons matured by `as_of` (fail-soft).

    Deterministic and idempotent: each slice's row is keyed by its own
    maturity session (`verdict_outcomes.as_of`, decision D7), so evaluating
    daily produces exactly the rows a manual batch would, without missed or
    double-counted slices. Reads only prices dated `<= as_of`.

    Issue #209: the daily pass runs `only_pending`, ahead of the export, so
    the work in front of the run's only skill handoff follows the number of
    slices that have newly matured rather than the whole evaluation window.
    Re-classifying an already-recorded slice after a *price* correction stays
    the manual `copilot-retro evaluate` / `prepare` batch's job -- it is the
    same command the correction itself is noticed from.
    """
    try:
        summary = evaluate_verdicts(
            deps.market_store,
            deps.state_store,
            EvaluationRequest(
                as_of=as_of,
                thresholds=deps.settings.postmortem,
                benchmark_symbol=deps.settings.backtest.benchmark,
                only_pending=True,
            ),
        )
    except Exception as exc:
        logger.exception("retro evaluate step raised unexpectedly")
        return _StepOutcome(False, f"unexpected error: {exc}")
    return _StepOutcome(
        True,
        _retro_step_detail(
            f"evaluated {summary.evaluated_slice_count} slice(s), "
            f"{summary.pending_slice_count} pending, "
            f"{summary.recorded_slice_count} already recorded, "
            f"{summary.outcome_count} outcome(s)",
            summary.notes,
        ),
    )


def _run_step_track_update(deps: DailyDependencies, as_of: date) -> _StepOutcome:
    """Carry the verdict-tracking ledger forward to `as_of` (fail-soft).

    Runs right after `retro_evaluate` for the same reason both of those do:
    it is offline, idempotent, and only reads bars the price step already
    persisted. Keeping it daily means a `proceed` verdict's virtual position
    is opened on the day it was made and marked every session afterwards,
    instead of only when someone remembers to run `copilot-track update`.

    Today's own verdict is not yet collected at this point (the skill writes
    `analysis_result.json` later), so it is picked up by tomorrow's run --
    the same one-day lag `retro_collect` already has, and harmless because the
    entry price is the run day's close either way.
    """
    try:
        summary = update_tracking(
            deps.state_store, deps.market_store, deps.settings.backtest, as_of=as_of
        )
    except Exception as exc:
        logger.exception("track update step raised unexpectedly")
        return _StepOutcome(False, f"unexpected error: {exc}")
    return _StepOutcome(
        True,
        _retro_step_detail(
            f"opened {summary.opened_count}, "
            f"advanced {summary.advanced_count}, "
            f"closed {summary.closed_count} position(s)",
            summary.notes,
        ),
    )


def _run_text_soft_step(
    options: DailyRunOptions,
    deps: DailyDependencies,
    ctx: _RunContext,
    deadline: float,
    text_symbols: list[str],
) -> tuple[_StepOutcome, list[TextItem] | None]:
    """Execute and audit the time-budgeted text step."""
    started_at = time.perf_counter()
    if deps.monotonic() >= deadline:
        logger.warning("step 5_text skipped: time budget exceeded")
        outcome, items = _TIME_BUDGET_STEP_OUTCOME, None
    else:
        logger.debug("step 5_text starting")
        _step_started(deps, "5_text")
        outcome, items = _run_step_text(
            deps, text_symbols, ctx.run_date, skip=options.skip_text
        )
    _record_step(deps, ctx.run_id, "5_text", outcome, started_at)
    return outcome, items


def _run_step_output(
    deps: DailyDependencies,
    output: _OutputContext,
) -> tuple[_StepOutcome, Path | None, DailyBrief | None]:
    context = DailyBriefContext(
        run_id=output.run.run_id,
        run_date=output.run.run_date,
        generated_at=deps.clock.now(),
        universe=deps.universe,
        candidates=output.run.candidates,
        risk_assessments=output.run.risk_assessments,
        analysis=None,
        strategy_key=deps.strategy_key,
        rejections=output.run.rejections,
        notices=output.notices,
        signal_performance=output.signal_performance,
        max_trade_risk_pct=deps.settings.risk.max_trade_risk_pct,
        max_position_pct=deps.settings.risk.max_position_pct,
        regime_snapshot=output.run.regime_snapshot,
        exposure_decision=output.run.exposure_decision,
        ftd_snapshot=output.run.ftd_snapshot,
        portfolio_heat=output.run.portfolio_heat,
        max_portfolio_heat_pct=deps.settings.risk.max_portfolio_heat_pct,
        provider_name=deps.provider_name,
        data_tier=deps.data_tier.value,
    )
    try:
        brief = build_daily_brief(context, deps.market_store)
    except Exception as exc:
        return _StepOutcome(False, f"brief construction failed: {exc}"), None, None
    try:
        report_path = write_markdown_report(brief, output.status, deps.output_dir)
    except LatestMarkdownUpdateError as exc:
        return (
            _StepOutcome(False, f"latest Markdown update failed: {exc}"),
            exc.report_path,
            brief,
        )
    except Exception as exc:
        return _StepOutcome(False, f"Markdown archive failed: {exc}"), None, brief
    if (
        output.analysis_input_path is not None
        and output.analysis_input_digest is not None
    ):
        try:
            write_report_context(
                ReportContext(
                    brief,
                    output.status,
                    Path(deps.output_dir),
                    deps.strategy_key,
                    output.analysis_input_digest,
                ),
                _run_output_dir(deps, output.run.run_date, output.run.run_id),
            )
        except Exception as exc:
            return (
                _StepOutcome(False, f"report context archive failed: {exc}"),
                report_path,
                brief,
            )
    try:
        write_rejections(
            RejectionsArtifact(
                run_id=output.run.run_id,
                as_of=output.run.run_date,
                strategy_key=deps.strategy_key,
                rejections=output.run.rejections,
                truncated=output.run.truncated,
            ),
            _run_output_dir(deps, output.run.run_date, output.run.run_id),
        )
    except Exception as exc:
        # Fail-soft like `report_context.json`: the Markdown archive is
        # already durable, so a diagnostic artifact cannot cost the run.
        logger.exception("rejections artifact archive failed")
        return (
            _StepOutcome(False, f"rejections archive failed: {exc}"),
            report_path,
            brief,
        )
    return _StepOutcome(True), report_path, brief


def _notification_summary(
    candidates: list[Candidate], run_date: date, exposure: ExposureDecision
) -> str:
    """One-line Discord summary, led by the exposure decision."""
    return (
        f"[swing-copilot] Exposure Ceiling: {exposure.verdict.value} "
        f"(Gate: {exposure.gate.value}, DD: {exposure.dd_level.value}, "
        f"Data quality: {exposure.data_quality.value})\n"
        f"{run_date.isoformat()}: {len(candidates)} candidate(s) today"
    )


def _run_step_notify(
    deps: DailyDependencies,
    candidates: list[Candidate],
    run_date: date,
    exposure: ExposureDecision,
    *,
    is_dry_run: bool,
) -> _StepOutcome:
    if is_dry_run:
        return _StepOutcome(True, "skipped: dry-run mode", is_skipped=True)
    if not deps.settings.notification.enabled or deps.notifier is None:
        return _StepOutcome(True, "skipped: notification disabled", is_skipped=True)
    sent = deps.notifier.notify(
        _notification_summary(candidates, run_date, exposure), None
    )
    if not sent:
        return _StepOutcome(False, "Discord webhook notification failed")
    return _StepOutcome(True)


def _record_step(
    deps: DailyDependencies,
    run_id: UUID,
    step: str,
    outcome: _StepOutcome,
    started_at: float,
) -> None:
    duration = time.perf_counter() - started_at
    if outcome.is_skipped:
        status = StepStatus.SKIPPED
    else:
        status = StepStatus.SUCCESS if outcome.success else StepStatus.FAILED
    logger.debug(
        "step %s finished: status=%s duration=%.2fs%s",
        step,
        status.value,
        duration,
        f" detail={outcome.detail}" if outcome.detail else "",
    )
    deps.state_store.record_run_step(run_id, step, status, outcome.detail, duration)
    if step in _VISIBLE_PIPELINE_STEPS:
        index = _VISIBLE_PIPELINE_STEPS.index(step) + 1
        progress_status = "ok" if outcome.success else status.value
        deps.progress.step_finished(
            index, len(_VISIBLE_PIPELINE_STEPS), step, progress_status, duration
        )


def _step_started(deps: DailyDependencies, step: str) -> None:
    """Notify the injected reporter when a user-visible step begins."""
    index = _VISIBLE_PIPELINE_STEPS.index(step) + 1
    deps.progress.step_started(index, len(_VISIBLE_PIPELINE_STEPS), step)


def _warn_stale_runs(run_id: UUID, stale_run_ids: list[UUID]) -> None:
    """Log NFR-03 stuck-run detection results, if any were found and marked failed."""
    if stale_run_ids:
        logger.warning(
            "run %s: marked %d stale running run(s) as failed: %s",
            run_id,
            len(stale_run_ids),
            stale_run_ids,
        )


# Compatibility facade: the public console-script target remains this module.
# Step implementations and shared dependency values above intentionally stay
# importable here; lifecycle and composition have explicit module boundaries.
__all__ = ["DailyDependencies", "main", "run_daily"]


def run_daily(options: DailyRunOptions, deps: DailyDependencies) -> DailyRunResult:
    """Run the lifecycle implementation while preserving the historic API."""
    from swing_copilot.pipeline.daily_runner import (  # noqa: PLC0415
        run_daily as run_lifecycle,
    )

    return run_lifecycle(options, deps)


def main(argv: list[str] | None = None) -> None:
    """Run the CLI composition implementation at the historic script target."""
    from swing_copilot.pipeline.daily_composition import (  # noqa: PLC0415
        main as compose_and_run,
    )

    compose_and_run(argv)


if __name__ == "__main__":  # pragma: no cover
    main()
