"""Daily batch orchestrator, all eight steps (FR-12).

Wires price update -> fundamentals update -> screening -> risk check ->
text collection -> LLM analysis -> notify -> CLI/Markdown output with
explicit `as_of`/`run_id` semantics. Steps 1-4 are fatal on failure
(screening cannot meaningfully proceed without them,
`docs/03_basic_design.md` 7): any of them failing aborts the run
(`runs.status=failed`, nonzero exit code) without touching steps 5-8.
Steps 5 (text) and 6 (LLM) are fail-soft: their failure degrades the run
(`runs.status=degraded`) but never aborts it. Notification is optional and
the local output step always attempts to produce a screening-only brief.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from swing_copilot.clock import SystemClock
from swing_copilot.config import (
    load_secrets,
    load_settings,
    load_strategies,
    require_secrets,
)
from swing_copilot.data.earnings_finnhub import FinnhubEarningsClient
from swing_copilot.data.edgar import EdgarClient
from swing_copilot.data.yfinance_provider import YFinanceProvider
from swing_copilot.exceptions import ConfigError
from swing_copilot.llm.client import LLMClient
from swing_copilot.llm.decision_context import (
    format_market_regime,
    format_performance_summary,
    format_risk_constraints,
    format_score_breakdown,
)
from swing_copilot.llm.filings_analysis import FilingAnalysisRequest, analyze_filing
from swing_copilot.llm.pricing import ModelPricing
from swing_copilot.llm.summarize import NewsSummaryRequest, summarize_news
from swing_copilot.models import (
    DailyRunOptions,
    DailyRunResult,
    RunMode,
    RunStatus,
    StepStatus,
)
from swing_copilot.paper.excursions import update_position_excursions
from swing_copilot.paper.journal import PaperJournal
from swing_copilot.pipeline.earnings import collect_earnings_calendar
from swing_copilot.pipeline.postmortem import run_postmortem_step
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
from swing_copilot.report.discord_notify import DiscordNotifier
from swing_copilot.report.markdown_report import write_markdown_report
from swing_copilot.report.terminal_report import render_terminal
from swing_copilot.risk.checks import (
    EarningsGuardInput,
    PortfolioHeatResult,
    RiskChecker,
    RiskRunContext,
    calculate_portfolio_heat,
)
from swing_copilot.risk.circuit_breaker import (
    CircuitBreakerResult,
    CircuitThresholds,
    RealizedTrade,
    evaluate_circuit_breaker,
    evaluation_time_for_as_of,
)
from swing_copilot.screening.base import ScreeningInput
from swing_copilot.screening.pipeline import ScreeningPipeline
from swing_copilot.storage.audit_records import ScreeningRunMeta
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.text.calendar_fred import FredCalendarClient
from swing_copilot.text.edgar_filings import fetch_recent_filings_text
from swing_copilot.text.news_finnhub import FinnhubNewsClient
from swing_copilot.universe import UniverseFetchOptions, get_sp500_universe

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from uuid import UUID

    import pandas as pd
    from pydantic import BaseModel

    from swing_copilot.clock import Clock
    from swing_copilot.config import Secrets, Settings, StrategiesConfig
    from swing_copilot.data.base import BarFetchResult, DataProvider
    from swing_copilot.data.earnings import EarningsCalendarClient
    from swing_copilot.llm.client import AnalyzeRequest
    from swing_copilot.llm.schemas import FilingAnalysis, NewsSummary
    from swing_copilot.paper.journal import PerformanceSummary
    from swing_copilot.pipeline.postmortem import SignalPerformanceRow
    from swing_copilot.report.discord_notify import Notifier
    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.screening.base import Candidate, RejectionRecord
    from swing_copilot.storage.market_store import FundamentalsRecord
    from swing_copilot.text.base import TextItem
    from swing_copilot.universe import UniverseMember

_PRICE_HISTORY_LOOKBACK_DAYS = 400  # enough for SMA200 warmup; unrelated to edgar.py's own fundamentals-fetch lookback constant
_TEXT_LOOKBACK_DAYS = 14
_FILING_FORM_TYPES = ["8-K", "10-Q"]
_TEXT_SYMBOL_LIMIT = (
    30  # held + candidates, capped per NFR-03 (docs/04_detailed_design.md 3.14)
)
_FUNDAMENTALS_PROGRESS_LOG_INTERVAL = 25
_DECISION_HISTORY_LIMIT = 3


class _EdgarClientLike(Protocol):
    """Structural stand-in for `data.edgar.EdgarClient`, for fake injection."""

    def fetch_fundamentals(
        self, symbol: str, as_of: datetime
    ) -> list[FundamentalsRecord]:
        """Fetch normalized fundamentals filed on or before `as_of`."""
        # pragma: no cover

    def fetch_filing_texts(
        self, symbol: str, form_types: list[str], *, as_of: datetime
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

    def fetch_calendar_events(self, start: date, end: date) -> list[TextItem]:
        """Fetch economic release events in `[start, end]`."""
        # pragma: no cover


class _LLMClientLike(Protocol):
    """Structural stand-in for `llm.client.LLMClient`, for fake injection."""

    def analyze(self, request: AnalyzeRequest) -> BaseModel:
        """Call Claude and return schema-validated structured output."""
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
    # Injectable monotonic time source for the NFR-03 run-timeout budget.
    # Deliberately separate from `clock` (calendar/business time): this is
    # wall-clock elapsed-time measurement, never a substitute for `as_of`.
    monotonic: Callable[[], float] = time.perf_counter
    edgar_client: _EdgarClientLike | None = None
    earnings_client: EarningsCalendarClient | None = None
    news_client: _NewsClientLike | None = None
    calendar_client: _CalendarClientLike | None = None
    llm_client: _LLMClientLike | None = None
    notifier: Notifier | None = None
    provider_name: str = "yfinance"
    strategy_key: str = "default"
    output_dir: str = "reports"


def _compute_performance_summary(
    deps: DailyDependencies, as_of: date
) -> PerformanceSummary | None:
    """P2-12 (REQ-003): compute P1-06's portfolio-wide summary once per run.

    Called from step 6 (LLM) itself, inside its existing NFR-03 time-budget
    gate -- not a new gate. Defensive: `PaperJournal.summarize_performance()`
    reads real store/market data and could raise on an unexpected storage
    failure; a failure here must degrade the (already fail-soft) LLM step by
    omitting the performance block, never crash the whole run.

    Args:
        deps: Run dependencies (`state_store`/`market_store`).
        as_of: Point-in-time cutoff for the summary.

    Returns:
        The computed summary, or `None` if it could not be computed.
    """
    try:
        return PaperJournal(deps.state_store).summarize_performance(
            deps.market_store, as_of
        )
    except Exception:
        logger.exception(
            "summarize_performance failed; continuing without performance context"
        )
        return None


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
    risk_assessments: list[RiskAssessment]
    portfolio_heat: PortfolioHeatResult
    circuit_breaker: CircuitBreakerResult
    earnings_guard_notice: str | None
    held_symbols: frozenset[str]
    regime_snapshot: RegimeSnapshot
    exposure_decision: ExposureDecision
    ftd_snapshot: FtdSnapshot
    # P2-12 (REQ-003): portfolio-wide P1-06 performance summary. Not
    # screening-derived like the other fields -- computed once inside step 6
    # (LLM) itself via `_compute_performance_summary()` -- but reusing this
    # dataclass (`dataclasses.replace()`) avoids adding a 6th parameter to
    # `_summarize_news_per_candidate()`/`_analyze_filings_per_candidate()`.
    performance_summary: PerformanceSummary | None = None


@dataclass(frozen=True, slots=True)
class _OutputContext:
    """Grouped inputs for the final local-output step."""

    run: _RunContext
    news_summaries: list[NewsSummary] | None
    filing_analyses: list[FilingAnalysis] | None
    signal_performance: tuple[SignalPerformanceRow, ...]
    notices: tuple[str, ...]
    status: RunStatus


def _config_hash(settings: Settings) -> str:
    payload = json.dumps(settings.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


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
    if limit is None:
        return [member.symbol for member in universe]
    limited = [member.symbol for member in universe[:limit]]
    return sorted({*limited, *held_symbols})


def _text_target_symbols(
    held_symbols: frozenset[str], candidates: list[Candidate]
) -> list[str]:
    """Text/LLM target symbols: held positions + today's candidates (`docs/04_detailed_design.md` 3.14).

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


def _run_step_prices(
    deps: DailyDependencies,
    symbols: list[str],
    as_of: date,
    prefetched: BarFetchResult | None = None,
) -> _StepOutcome:
    if prefetched is None:
        start = as_of - timedelta(days=_PRICE_HISTORY_LOOKBACK_DAYS)
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


def _fetch_or_skip_fundamentals(
    market_store: MarketStore,
    edgar_client: _EdgarClientLike,
    symbol: str,
    today: date,
    as_of_cutoff: datetime,
) -> tuple[list[FundamentalsRecord], bool, bool]:
    """Fetch one symbol's fundamentals, or skip a same-day rerun.

    Args:
        market_store: Used for the same-day-fetch check and, by the caller,
            the eventual upsert.
        edgar_client: Configured EDGAR client (never `None` here).
        symbol: Ticker to fetch.
        today: Wall-clock calendar date (from the injected `Clock`), compared
            against `fetched_at`'s date to detect a same-day rerun. Must not
            be `as_of` -- `fetched_at` is a real fetch timestamp, so a past
            `--as-of` would never match it and every run would refetch (P6-25).
        as_of_cutoff: `as_of` widened to end-of-day UTC for the filing cutoff.

    Returns:
        `(records, failed, was_skipped)`. `failed` is `True` only if the
        network fetch itself raised; a same-day skip is never a failure.
    """
    if market_store.has_fundamentals_fetched_on(symbol, today):
        logger.debug(
            "fundamentals: %s already fetched today (%s), skipping fetch",
            symbol,
            today,
        )
        return [], False, True
    try:
        records = list(edgar_client.fetch_fundamentals(symbol, as_of_cutoff))
    except Exception:
        logger.exception("fundamentals fetch failed for %s", symbol)
        return [], True, False
    return records, False, False


def _log_fundamentals_progress(position: int, total: int) -> None:
    if position % _FUNDAMENTALS_PROGRESS_LOG_INTERVAL == 0:
        logger.info("fundamentals: %d/%d symbols processed", position, total)
    else:
        logger.debug("fundamentals: %d/%d symbols processed", position, total)


def _run_step_fundamentals(
    deps: DailyDependencies, symbols: list[str], as_of: date, deadline: float
) -> _StepOutcome:
    """Fetch/upsert fundamentals for `symbols`, filed on or before `as_of`.

    Two fail-soft/efficiency behaviors beyond a plain per-symbol fetch:

    - Same-day rerun skip: a symbol already fetched today (`fetched_at`'s
      date == `deps.clock.today()`) is not re-fetched over the network. This
      is deliberately the injected `Clock`'s wall-clock date, not `as_of`:
      `fetched_at` is a real fetch timestamp, so comparing it against a
      possibly-past `as_of` would never match and every rerun would refetch
      over the network regardless of `--as-of` (P6-25). Point-in-time
      correctness is unaffected -- callers still read fundamentals filtered
      by `as_of`, never by `fetched_at`. Correction semantics are also
      unaffected — the next day's run always re-fetches and upserts by
      `accession_no`.
    - NFR-03 time budget: once `deps.monotonic() >= deadline`, fetching
      stops early with whatever records were already gathered upserted, and
      the step still succeeds (not fatal) with a detail explaining the
      partial completion, mirroring the existing partial-per-symbol-failure
      convention below.
    """
    edgar_client = deps.edgar_client
    if edgar_client is None:
        return _StepOutcome(
            True, "skipped: no EDGAR client configured", is_skipped=True
        )

    today = deps.clock.today()
    as_of_cutoff = datetime.combine(as_of, datetime.max.time(), tzinfo=UTC)
    total = len(symbols)
    records: list[FundamentalsRecord] = []
    failed_symbols: list[str] = []
    skipped_same_day = 0
    budget_detail: str | None = None

    for index, symbol in enumerate(symbols):
        if deps.monotonic() >= deadline:
            budget_detail = f"time budget exceeded after {index}/{total} symbols"
            logger.warning("fundamentals step stopping early: %s", budget_detail)
            break
        symbol_records, failed, was_skipped = _fetch_or_skip_fundamentals(
            deps.market_store, edgar_client, symbol, today, as_of_cutoff
        )
        records.extend(symbol_records)
        failed_symbols.extend([symbol] if failed else [])
        skipped_same_day += 1 if was_skipped else 0
        _log_fundamentals_progress(index + 1, total)

    if skipped_same_day:
        logger.info(
            "fundamentals: skipped %d/%d symbol(s) already fetched today",
            skipped_same_day,
            total,
        )
    if records:
        deps.market_store.upsert_fundamentals(records)

    if failed_symbols and not records and budget_detail is None:
        return _StepOutcome(
            False, f"EDGAR fetch failed for every symbol: {failed_symbols}"
        )

    details = []
    if failed_symbols:
        details.append(f"failed symbols: {failed_symbols}")
    if budget_detail:
        details.append(budget_detail)
    return _StepOutcome(True, "; ".join(details) if details else None)


def _run_step_screening(
    deps: DailyDependencies, symbols: list[str], as_of: date, run_id: UUID
) -> tuple[_StepOutcome, list[Candidate], list[RejectionRecord]]:
    fundamentals = deps.market_store.read_fundamentals(as_of)
    start = as_of - timedelta(days=_PRICE_HISTORY_LOOKBACK_DAYS)
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
        result.candidates,
        result.rejections,
        ScreeningRunMeta(run_id, pipeline.strategy_key, as_of),
    )
    return _StepOutcome(True), result.candidates, result.rejections


def _run_step_risk(
    deps: DailyDependencies,
    candidates: list[Candidate],
    run_id: UUID,
    as_of: date,
    exposure: ExposureDecision,
) -> tuple[
    _StepOutcome,
    list[RiskAssessment],
    PortfolioHeatResult,
    CircuitBreakerResult,
    str | None,
]:
    portfolio = deps.state_store.get_open_positions(is_paper=True)
    closed = deps.state_store.get_closed_positions(is_paper=True, as_of=as_of)
    circuit_config = deps.settings.risk
    circuit_breaker = evaluate_circuit_breaker(
        [
            RealizedTrade(
                position.close_at,
                (
                    (position.close_price - position.entry_price) * position.shares
                    if position.close_price is not None
                    else None
                ),
            )
            for position in closed
        ],
        circuit_config.account_equity_usd,
        as_of,
        evaluation_time_for_as_of(as_of),
        CircuitThresholds(
            circuit_config.circuit_daily_loss_pct,
            circuit_config.circuit_weekly_loss_pct,
            circuit_config.circuit_monthly_loss_pct,
            circuit_config.circuit_consecutive_losses,
            circuit_config.circuit_cooldown_hours,
        ),
    )
    earnings = collect_earnings_calendar(
        deps.earnings_client,
        sorted(
            {
                *(position.symbol for position in portfolio),
                *(candidate.symbol for candidate in candidates),
            }
        ),
        as_of,
        deps.state_store,
    )
    checker = RiskChecker(
        deps.settings,
        deps.universe,
        deps.market_store,
        RiskRunContext(
            earnings_guard=EarningsGuardInput(
                earnings.is_enabled, earnings.events_by_symbol
            ),
            circuit_breaker=circuit_breaker,
        ),
    )
    assessments = checker.check(
        candidates,
        portfolio,
        deps.settings.risk.account_equity_usd,
        exposure,
    )
    deps.state_store.record_risk_assessments(assessments, run_id)
    base_heat = calculate_portfolio_heat(
        portfolio, deps.settings.risk.account_equity_usd
    )
    final_heat = (
        replace(base_heat, heat_pct=assessments[-1].portfolio_heat_pct)
        if base_heat.status == "calculated" and assessments
        else base_heat
    )
    return (
        _StepOutcome(True),
        assessments,
        final_heat,
        circuit_breaker,
        earnings.notice,
    )


def _calculate_regime_snapshot(deps: DailyDependencies, as_of: date) -> RegimeSnapshot:
    """Calculate the code-owned market regime from point-in-time store reads."""
    history_start = as_of - timedelta(days=2 * _PRICE_HISTORY_LOOKBACK_DAYS)
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
    history_start = as_of - timedelta(days=2 * _PRICE_HISTORY_LOOKBACK_DAYS)
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
                    deps.edgar_client, symbol, _FILING_FORM_TYPES, as_of
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
                as_of, as_of + timedelta(days=14)
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

    if items:
        deps.state_store.record_text_items(items)

    return _text_step_outcome(items, failed_symbols, calendar_failed, len(symbols))


def _run_step_llm(
    deps: DailyDependencies,
    ctx: _RunContext,
    text_items: list[TextItem] | None,
    *,
    skip: bool,
    include_decision_history: bool,
) -> tuple[_StepOutcome, list[NewsSummary] | None, list[FilingAnalysis] | None]:
    llm_client = deps.llm_client
    if skip:
        return _StepOutcome(True, "skipped: --skip-llm", is_skipped=True), None, None
    if llm_client is None:
        return (
            _StepOutcome(True, "skipped: no LLM client configured", is_skipped=True),
            None,
            None,
        )
    if text_items is None:
        return (
            _StepOutcome(True, "skipped: step 5 produced no text", is_skipped=True),
            None,
            None,
        )

    # P2-12 (REQ-003): computed once per run, inside this step's existing
    # NFR-03 time-budget gate (reused, not a new gate) -- portfolio-wide, so
    # it is not recomputed per candidate below.
    ctx = replace(
        ctx, performance_summary=_compute_performance_summary(deps, ctx.run_date)
    )

    news_summaries, failed_news_symbols = _summarize_news_per_candidate(
        llm_client, deps, ctx, text_items, include_decision_history
    )
    filing_analyses, failed_filing_symbols = _analyze_filings_per_candidate(
        llm_client, deps, ctx, text_items, include_decision_history
    )
    failed_symbols = sorted({*failed_news_symbols, *failed_filing_symbols})

    if not failed_symbols:
        return _StepOutcome(True), news_summaries, filing_analyses
    if news_summaries or filing_analyses:
        return (
            _StepOutcome(False, f"failed symbols: {failed_symbols}"),
            news_summaries,
            filing_analyses,
        )
    return (
        _StepOutcome(
            False, f"LLM analysis failed for every candidate: {failed_symbols}"
        ),
        None,
        None,
    )


def _decision_context_blocks(
    candidate: Candidate,
    risk_by_symbol: dict[str, RiskAssessment],
    performance_summary: PerformanceSummary | None,
) -> str:
    """P2-12 (REQ-001/002/003): one candidate's score/risk/performance blocks.

    `risk_by_symbol[candidate.symbol]` is a plain (not `.get()`) lookup:
    `RiskChecker.check()` guarantees one `RiskAssessment` per candidate, same
    order as `candidates` (`risk/checks.py`), so `ctx.risk_assessments`
    always covers every `ctx.candidates` entry -- the same invariant
    `_run_step_risk()` relies on to zip them together.
    """
    return "".join(
        (
            format_score_breakdown(candidate),
            format_risk_constraints(risk_by_symbol[candidate.symbol]),
            format_performance_summary(performance_summary),
        )
    )


def _summarize_news_per_candidate(
    llm_client: _LLMClientLike,
    deps: DailyDependencies,
    ctx: _RunContext,
    text_items: list[TextItem],
    include_decision_history: bool,
) -> tuple[list[NewsSummary], list[str]]:
    summaries = []
    failed_symbols = []
    risk_by_symbol = {
        assessment.symbol: assessment for assessment in ctx.risk_assessments
    }
    for candidate in ctx.candidates:
        news_items = tuple(
            item
            for item in text_items
            if item.symbol == candidate.symbol and item.source_type == "news"
        )
        if not news_items:
            continue
        request = NewsSummaryRequest(
            run_id=ctx.run_id,
            symbol=candidate.symbol,
            period=f"{ctx.run_date - timedelta(days=_TEXT_LOOKBACK_DAYS)}..{ctx.run_date}",
            news_items=news_items,
            model=deps.settings.llm.models.news_summary,
            max_tokens=deps.settings.llm.max_tokens,
            schema_version=deps.settings.llm.schema_version,
            max_items=deps.settings.llm.max_news_items_per_symbol,
            max_chars_per_item=deps.settings.llm.max_news_chars_per_item,
            decision_history=tuple(
                deps.state_store.get_decision_history(
                    candidate.symbol,
                    deps.strategy_key,
                    ctx.run_date,
                    _DECISION_HISTORY_LIMIT,
                )
            )
            if include_decision_history
            else (),
            decision_context_blocks=_decision_context_blocks(
                candidate, risk_by_symbol, ctx.performance_summary
            ),
            market_regime=format_market_regime(
                ctx.regime_snapshot, ctx.exposure_decision
            ),
        )
        try:
            summaries.append(summarize_news(llm_client, request))
        except Exception:
            failed_symbols.append(candidate.symbol)
    return summaries, failed_symbols


def _analyze_filings_per_candidate(
    llm_client: _LLMClientLike,
    deps: DailyDependencies,
    ctx: _RunContext,
    text_items: list[TextItem],
    include_decision_history: bool,
) -> tuple[list[FilingAnalysis], list[str]]:
    analyses = []
    failed_symbols = []
    risk_by_symbol = {
        assessment.symbol: assessment for assessment in ctx.risk_assessments
    }
    for candidate in ctx.candidates:
        filing_items = [
            item
            for item in text_items
            if item.symbol == candidate.symbol and item.source_type == "filing"
        ]
        for filing_item in filing_items:
            filing_type = (filing_item.title or "unknown").split(" - ")[0]
            request = FilingAnalysisRequest(
                run_id=ctx.run_id,
                symbol=candidate.symbol,
                filing_type=filing_type,
                filing_text=filing_item,
                model=deps.settings.llm.models.filing_analysis,
                max_tokens=deps.settings.llm.max_tokens,
                schema_version=deps.settings.llm.schema_version,
                chunk_chars=deps.settings.llm.filing_chunk_chars,
                max_chunks=deps.settings.llm.max_filing_chunks,
                decision_history=tuple(
                    deps.state_store.get_decision_history(
                        candidate.symbol,
                        deps.strategy_key,
                        ctx.run_date,
                        _DECISION_HISTORY_LIMIT,
                    )
                )
                if include_decision_history
                else (),
                decision_context_blocks=_decision_context_blocks(
                    candidate, risk_by_symbol, ctx.performance_summary
                ),
                market_regime=format_market_regime(
                    ctx.regime_snapshot, ctx.exposure_decision
                ),
            )
            try:
                analyses.append(analyze_filing(llm_client, request))
            except Exception:
                failed_symbols.append(candidate.symbol)
    return analyses, failed_symbols


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


def _run_step_excursions(deps: DailyDependencies, as_of: date) -> _StepOutcome:
    """Update daily MAE/MFE snapshots without making the run fatal."""
    summary = update_position_excursions(deps.state_store, deps.market_store, as_of)
    if not summary.missing_symbols:
        return _StepOutcome(True)
    return _StepOutcome(
        True,
        "MAE_MFE_MISSING_BAR: " + ", ".join(summary.missing_symbols),
    )


def _run_mae_mfe_soft_step(
    deps: DailyDependencies, run_id: UUID, as_of: date
) -> _StepOutcome:
    """Execute and audit MAE/MFE without crossing the fatal boundary."""
    started_at = time.perf_counter()
    logger.info("step mae_mfe starting")
    try:
        outcome = _run_step_excursions(deps, as_of)
    except Exception as exc:
        logger.exception("MAE/MFE step raised unexpectedly")
        outcome = _StepOutcome(False, f"unexpected error: {exc}")
    _record_step(deps, run_id, "mae_mfe", outcome, started_at)
    return outcome


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
        logger.info("step 5_text starting")
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
        news_summaries=output.news_summaries,
        filing_analyses=output.filing_analyses,
        strategy_key=deps.strategy_key,
        rejections=output.run.rejections,
        notices=output.notices,
        signal_performance=output.signal_performance,
        max_trade_risk_pct=deps.settings.risk.max_trade_risk_pct,
        max_position_pct=deps.settings.risk.max_position_pct,
        regime_snapshot=output.run.regime_snapshot,
        exposure_decision=output.run.exposure_decision,
        circuit_breaker=output.run.circuit_breaker,
        ftd_snapshot=output.run.ftd_snapshot,
        portfolio_heat=output.run.portfolio_heat,
        max_portfolio_heat_pct=deps.settings.risk.max_portfolio_heat_pct,
    )
    try:
        brief = build_daily_brief(context, deps.market_store, deps.state_store)
    except Exception as exc:
        return _StepOutcome(False, f"brief construction failed: {exc}"), None, None
    try:
        report_path = write_markdown_report(brief, output.status, deps.output_dir)
    except Exception as exc:
        return _StepOutcome(False, f"Markdown archive failed: {exc}"), None, brief
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
    logger.info(
        "step %s finished: status=%s duration=%.2fs%s",
        step,
        status.value,
        duration,
        f" detail={outcome.detail}" if outcome.detail else "",
    )
    deps.state_store.record_run_step(run_id, step, status, outcome.detail, duration)


def _warn_stale_runs(run_id: UUID, stale_run_ids: list[UUID]) -> None:
    """Log NFR-03 stuck-run detection results, if any were found and marked failed."""
    if stale_run_ids:
        logger.warning(
            "run %s: marked %d stale running run(s) as failed: %s",
            run_id,
            len(stale_run_ids),
            stale_run_ids,
        )


def run_daily(  # noqa: PLR0915 - the documented batch lifecycle is intentionally linear
    options: DailyRunOptions, deps: DailyDependencies
) -> DailyRunResult:
    """Run the full eight-step daily batch.

    Args:
        options: Parsed CLI options (`--as-of`, `--limit`, `--dry-run`, ...).
        deps: Real (or fake, for dry-run/tests) collaborators.

    Returns:
        The run outcome. `exit_code` is nonzero only if one of steps 1-4
        (prices, fundamentals, screening, risk) failed outright; steps 5-8
        degrade the run (`RunStatus.DEGRADED`) but keep `exit_code == 0`.
    """
    run_started_at = deps.monotonic()
    budget_s = deps.settings.schedule.timeout_minutes * 60
    deadline = run_started_at + budget_s

    mode = _run_mode(options)
    fetch_cutoff = options.as_of or deps.clock.today()
    held_symbols = {
        position.symbol for position in deps.state_store.get_open_positions()
    }
    symbols = _select_symbols(deps.universe, held_symbols, options.limit)
    # The market strip (SPY/QQQ/^VIX/^TNX) is never part of the screening
    # universe, but its bars still need fetching here so the brief has market
    # context to show.
    price_symbols = sorted({*symbols, *MARKET_STRIP_SYMBOLS})

    prefetched_prices: BarFetchResult | None = None
    prefetch_error: str | None = None
    run_date = fetch_cutoff
    if options.as_of is None:
        try:
            start = fetch_cutoff - timedelta(days=_PRICE_HISTORY_LOOKBACK_DAYS)
            prefetched_prices = deps.data_provider.get_daily_bars(
                price_symbols, start, fetch_cutoff + timedelta(days=1)
            )
            if not prefetched_prices.bars.empty:
                latest = max(prefetched_prices.bars["date"])
                run_date = latest.date() if isinstance(latest, datetime) else latest
        except Exception as exc:
            prefetch_error = f"unexpected error: {exc}"

    run_id = deps.state_store.start_run(run_date, mode, _config_hash(deps.settings))
    logger.info(
        "run %s started: mode=%s run_date=%s symbols=%d",
        run_id,
        mode.value,
        run_date,
        len(symbols),
    )

    # NFR-03 stuck-run detection: a run that crashed mid-execution never
    # reaches `complete_run()` and would sit in `status='running'` forever.
    stale_cutoff = deps.clock.now() - timedelta(seconds=budget_s)
    stale_run_ids = deps.state_store.mark_stale_running_runs(stale_cutoff, run_id)
    _warn_stale_runs(run_id, stale_run_ids)

    empty_run_data: tuple[
        list[Candidate], list[RejectionRecord], list[RiskAssessment]
    ] = ([], [], [])
    candidates, rejections, risk_assessments = empty_run_data
    regime_snapshot: RegimeSnapshot | None = None
    exposure_decision: ExposureDecision | None = None
    ftd_snapshot: FtdSnapshot | None = None
    portfolio_heat: PortfolioHeatResult | None = None
    circuit_breaker: CircuitBreakerResult | None = None
    earnings_guard_notice: str | None = None

    def _step_screening() -> _StepOutcome:
        nonlocal candidates, rejections
        outcome, candidates, rejections = _run_step_screening(
            deps, symbols, run_date, run_id
        )
        return outcome

    def _step_risk() -> _StepOutcome:
        nonlocal circuit_breaker, earnings_guard_notice, exposure_decision
        nonlocal ftd_snapshot, portfolio_heat
        nonlocal regime_snapshot, risk_assessments
        regime_snapshot = _record_regime_snapshot(deps, run_id, run_date)
        ftd_snapshot = _record_ftd_snapshot(deps, run_id, run_date)
        exposure_decision = _record_exposure_decision(deps, run_id, regime_snapshot)
        (
            outcome,
            risk_assessments,
            portfolio_heat,
            circuit_breaker,
            earnings_guard_notice,
        ) = _run_step_risk(deps, candidates, run_id, run_date, exposure_decision)
        return outcome

    def _step_prices() -> _StepOutcome:
        if prefetch_error is not None:
            return _StepOutcome(False, prefetch_error)
        return _run_step_prices(deps, price_symbols, run_date, prefetched_prices)

    # Time-budget policy for steps 1-4: only fundamentals (2) is gated (per
    # symbol, inside `_run_step_fundamentals`) since it is the dominant
    # network cost. Prices (1), screening (3), and risk (4) always run --
    # prices is needed to establish `run_date`/bars at all, and screening/risk
    # are cheap, local, and required to produce a report.
    fatal_steps: list[tuple[str, Callable[[], _StepOutcome]]] = [
        ("1_prices", _step_prices),
        (
            "2_fundamentals",
            lambda: _run_step_fundamentals(deps, symbols, run_date, deadline),
        ),
        ("3_screening", _step_screening),
        ("4_risk", _step_risk),
    ]

    for step_name, step_fn in fatal_steps:
        logger.info("step %s starting", step_name)
        started_at = time.perf_counter()
        try:
            outcome = step_fn()
        except Exception as exc:
            logger.exception("step %s raised unexpectedly", step_name)
            outcome = _StepOutcome(False, f"unexpected error: {exc}")
        _record_step(deps, run_id, step_name, outcome, started_at)
        if not outcome.success:
            deps.state_store.complete_run(
                run_id, RunStatus.FAILED, error_summary=outcome.detail
            )
            logger.error(
                "run %s failed at step %s: %s", run_id, step_name, outcome.detail
            )
            return DailyRunResult(run_id, run_date, RunStatus.FAILED, exit_code=1)

    regime_snapshot = cast("RegimeSnapshot", regime_snapshot)
    exposure_decision = cast("ExposureDecision", exposure_decision)
    ftd_snapshot = cast("FtdSnapshot", ftd_snapshot)
    portfolio_heat = cast("PortfolioHeatResult", portfolio_heat)
    circuit_breaker = cast("CircuitBreakerResult", circuit_breaker)

    ctx = _RunContext(
        run_id=run_id,
        run_date=run_date,
        candidates=candidates,
        rejections=rejections,
        risk_assessments=risk_assessments,
        portfolio_heat=portfolio_heat,
        circuit_breaker=circuit_breaker,
        earnings_guard_notice=earnings_guard_notice,
        held_symbols=frozenset(held_symbols),
        regime_snapshot=regime_snapshot,
        exposure_decision=exposure_decision,
        ftd_snapshot=ftd_snapshot,
    )
    return _run_soft_steps(options, deps, ctx, deadline)


def _run_soft_steps(
    options: DailyRunOptions,
    deps: DailyDependencies,
    ctx: _RunContext,
    deadline: float,
) -> DailyRunResult:
    """Run fail-soft local/optional steps after the fatal steps.

    Fail-soft MAE/MFE, text/LLM/postmortem, notification, then local output.

    NFR-03 time-budget policy: steps 5 (text), 6 (LLM), postmortem, and 7
    (Discord notify) are skipped outright once `deps.monotonic() >=
    deadline`, recorded via `_TIME_BUDGET_STEP_OUTCOME` (`is_skipped=True`
    but `success=False`) -- a *degrading* skip, distinct from the ordinary
    "not configured" skip, so the run still ends up `RunStatus.DEGRADED`
    even though nothing here technically raised. Postmortem itself is local
    (DB/Parquet, not network), but is gated the same way for consistency and
    so a run already over budget doesn't spend more time on it. Step 8 is
    cheap/local and always attempts to complete regardless of budget, so a
    timed-out run still produces terminal output and a Markdown archive.
    """
    degraded = False
    text_symbols = _text_target_symbols(ctx.held_symbols, ctx.candidates)

    excursion_outcome = _run_mae_mfe_soft_step(deps, ctx.run_id, ctx.run_date)
    degraded = degraded or not excursion_outcome.success

    text_outcome, text_items = _run_text_soft_step(
        options, deps, ctx, deadline, text_symbols
    )
    degraded = degraded or not text_outcome.success

    started_at = time.perf_counter()
    if deps.monotonic() >= deadline:
        logger.warning("step 6_llm skipped: time budget exceeded")
        llm_outcome, news_summaries, filing_analyses = (
            _TIME_BUDGET_STEP_OUTCOME,
            None,
            None,
        )
    else:
        logger.info("step 6_llm starting")
        llm_outcome, news_summaries, filing_analyses = _run_step_llm(
            deps,
            ctx,
            text_items,
            skip=options.skip_llm,
            include_decision_history=(not options.is_dry_run and options.as_of is None),
        )
    _record_step(deps, ctx.run_id, "6_llm", llm_outcome, started_at)
    degraded = degraded or not llm_outcome.success

    started_at = time.perf_counter()
    signal_performance: tuple[SignalPerformanceRow, ...]
    if deps.monotonic() >= deadline:
        logger.warning("step postmortem skipped: time budget exceeded")
        postmortem_outcome, signal_performance = _TIME_BUDGET_STEP_OUTCOME, ()
    else:
        logger.info("step postmortem starting")
        postmortem_outcome, signal_performance = _run_step_postmortem(
            deps, ctx.run_date
        )
    _record_step(deps, ctx.run_id, "postmortem", postmortem_outcome, started_at)
    degraded = degraded or not postmortem_outcome.success

    started_at = time.perf_counter()
    if deps.monotonic() >= deadline:
        logger.warning("step 7_notify skipped: time budget exceeded")
        notify_outcome = _TIME_BUDGET_STEP_OUTCOME
    else:
        logger.info("step 7_notify starting")
        notify_outcome = _run_step_notify(
            deps,
            ctx.candidates,
            ctx.run_date,
            ctx.exposure_decision,
            is_dry_run=options.is_dry_run,
        )
    _record_step(deps, ctx.run_id, "7_notify", notify_outcome, started_at)
    degraded = degraded or not notify_outcome.success

    status_before_output = RunStatus.DEGRADED if degraded else RunStatus.SUCCESS
    notices = (
        (ctx.earnings_guard_notice,) if ctx.earnings_guard_notice else ()
    ) + tuple(
        f"{label}: {outcome.detail}"
        for label, outcome in (
            ("MAE/MFE", excursion_outcome),
            ("text", text_outcome),
            ("LLM", llm_outcome),
            ("postmortem", postmortem_outcome),
            ("notification", notify_outcome),
        )
        if outcome.detail is not None
        and (not outcome.success or not outcome.is_skipped)
    )
    started_at = time.perf_counter()
    logger.info("step 8_output starting")
    output_outcome, report_path, brief = _run_step_output(
        deps,
        _OutputContext(
            run=ctx,
            news_summaries=news_summaries,
            filing_analyses=filing_analyses,
            signal_performance=signal_performance,
            notices=notices,
            status=status_before_output,
        ),
    )
    _record_step(deps, ctx.run_id, "8_output", output_outcome, started_at)
    degraded = degraded or not output_outcome.success

    final_status = RunStatus.DEGRADED if degraded else RunStatus.SUCCESS
    deps.state_store.complete_run(ctx.run_id, final_status, report_path=report_path)
    logger.info("run %s completed: status=%s", ctx.run_id, final_status.value)
    return DailyRunResult(
        ctx.run_id,
        ctx.run_date,
        final_status,
        exit_code=0,
        report_path=report_path,
        brief=brief,
    )


def _parse_args(argv: list[str] | None = None) -> DailyRunOptions:
    parser = argparse.ArgumentParser(prog="copilot-daily")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-text", action="store_true")
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--strategy", default="default")
    args = parser.parse_args(argv)
    return DailyRunOptions(
        as_of=args.as_of,
        is_dry_run=args.dry_run,
        skip_text=args.skip_text,
        skip_llm=args.skip_llm,
        limit=args.limit,
        strategy_key=args.strategy,
    )


def _required_features(options: DailyRunOptions, settings: Settings) -> set[str]:
    features = {"edgar"}
    if not options.skip_text:
        features |= {"finnhub", "fred"}
    if not options.skip_llm:
        features.add("llm")
    if settings.notification.enabled:
        features.add("discord")
    return features


def _compose_dependencies(
    options: DailyRunOptions, settings: Settings, strategies: StrategiesConfig
) -> DailyDependencies:
    """Wire real adapters for a live (non-test) run (composition root, FR-12)."""
    if options.strategy_key not in strategies.strategies:
        available = ", ".join(sorted(strategies.strategies))
        msg = f"Unknown strategy {options.strategy_key!r}; available: {available}"
        raise ConfigError(msg)
    secrets = load_secrets()
    require_secrets(secrets, _required_features(options, settings))

    mode = _run_mode(options)
    db_path, output_dir = _paths_for_mode(mode)
    database = Database(db_path)
    market_store = MarketStore(database)
    state_store = StateStore(database)
    state_store.init_schema()
    clock = SystemClock()

    universe = tuple(
        get_sp500_universe(
            clock.today(),
            options=UniverseFetchOptions(
                snapshot_path=settings.universe.snapshot_path,
                manual_include=settings.universe.manual_include,
                manual_exclude=settings.universe.manual_exclude,
            ),
        )
    )

    edgar_client = (
        EdgarClient(secrets.edgar_identity) if secrets.edgar_identity else None
    )
    news_client = (
        FinnhubNewsClient(secrets.finnhub_api_key)
        if secrets.finnhub_api_key and not options.skip_text
        else None
    )
    earnings_client = (
        FinnhubEarningsClient(secrets.finnhub_api_key)
        if secrets.finnhub_api_key
        else None
    )
    calendar_client = (
        FredCalendarClient(secrets.fred_api_key)
        if secrets.fred_api_key and not options.skip_text
        else None
    )
    llm_client = (
        LLMClient(
            secrets.anthropic_api_key,
            state_store,
            ModelPricing(),
            _monthly_budget_cap(settings),
        )
        if secrets.anthropic_api_key and not options.skip_llm
        else None
    )
    notifier = (
        DiscordNotifier(secrets.discord_webhook_url)
        if settings.notification.enabled and secrets.discord_webhook_url
        else None
    )

    return DailyDependencies(
        data_provider=YFinanceProvider(),
        market_store=market_store,
        state_store=state_store,
        settings=settings,
        universe=universe,
        strategies_config=strategies.model_dump(),
        clock=clock,
        edgar_client=edgar_client,
        earnings_client=earnings_client,
        news_client=news_client,
        calendar_client=calendar_client,
        llm_client=llm_client,
        notifier=notifier,
        output_dir=output_dir,
        strategy_key=options.strategy_key,
    )


def _monthly_budget_cap(settings: Settings) -> float:
    return settings.budget.monthly_cap_usd_prototype


class _SecretRedactionFilter(logging.Filter):
    """Replaces configured secret values with `"[REDACTED]"` in every record.

    `text/calendar_fred.py` and `text/news_finnhub.py` send their API keys as
    URL query parameters; a non-retried `httpx.HTTPStatusError` embeds the
    full request URL in its message, so an uncaught traceback logged via
    `logger.exception(...)` (`daily.py`'s fundamentals/news/filings/calendar
    fetch steps) would otherwise print the real secret to stderr. Attached to
    every root handler by `_configure_logging` so this applies regardless of
    which module's logger emitted the record (AGENTS.md: "never log secrets").
    """

    def __init__(self, secrets: Iterable[str | None]) -> None:
        super().__init__()
        # Empty/`None` values are dropped, not just falsy-skipped at replace
        # time: an empty pattern would otherwise match (and mangle) every
        # message. Longest-first so a secret that is a substring of another
        # configured secret is still fully redacted.
        self._secrets = tuple(
            sorted({secret for secret in secrets if secret}, key=len, reverse=True)
        )

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True

        record.msg = self._redact(record.getMessage())
        record.args = ()

        if record.exc_info is not None:
            formatted = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = self._redact(formatted)
            record.exc_info = None

        return True

    def _redact(self, value: str) -> str:
        redacted = value
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted


def _configure_logging(secrets: Secrets) -> None:
    """Configure root logging (INFO, to stderr) with secret redaction.

    A live run's step progress and failures are always visible this way --
    this batch otherwise has no other output surface until the final brief is
    rendered. A `_SecretRedactionFilter` seeded from every configured secret is
    attached to each root handler so a leaked API key/webhook URL (record
    message or exception traceback) never reaches stderr in the clear.

    Factored out of `main()` so tests can exercise the redaction behavior
    without invoking the whole CLI.

    Args:
        secrets: Loaded secrets to redact from now on. Unset (`None`/empty)
            values are ignored.
    """
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    redaction_filter = _SecretRedactionFilter(
        (
            secrets.finnhub_api_key,
            secrets.fred_api_key,
            secrets.anthropic_api_key,
            secrets.discord_webhook_url,
        )
    )
    for handler in logging.root.handlers:
        handler.addFilter(redaction_filter)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: parse args, compose real dependencies, run, exit.

    Args:
        argv: Argument list, or `None` to use `sys.argv[1:]`.
    """
    _configure_logging(load_secrets())
    options = _parse_args(argv)
    settings = load_settings()
    strategies = load_strategies()
    deps = _compose_dependencies(options, settings, strategies)
    result = run_daily(options, deps)
    if result.brief is not None:
        width = shutil.get_terminal_size(fallback=(120, 24)).columns
        sys.stdout.write(
            render_terminal(
                result.brief,
                result.status,
                width=width,
                color=sys.stdout.isatty(),
            )
        )
    raise SystemExit(result.exit_code)


if __name__ == "__main__":  # pragma: no cover
    main()
