"""Daily batch orchestrator, all nine steps (FR-12).

Wires price update -> fundamentals update -> screening -> risk check ->
text collection -> LLM analysis -> report -> notify -> browser-open with
explicit `as_of`/`run_id` semantics. Steps 1-4 are fatal on failure
(screening cannot meaningfully proceed without them,
`docs/03_basic_design.md` 7): any of them failing aborts the run
(`runs.status=failed`, nonzero exit code) without touching steps 5-9.
Steps 5 (text) and 6 (LLM) are fail-soft: their failure degrades the run
(`runs.status=degraded`) but never aborts it — steps 7 (report), 8 (Discord
notify), and 9 (browser auto-open) always attempt to complete, rendering a
screening-only report when `news_summaries`/`filing_analyses` are `None`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import webbrowser
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from swing_copilot.clock import SystemClock
from swing_copilot.config import (
    load_secrets,
    load_settings,
    load_strategies,
    require_secrets,
)
from swing_copilot.data.edgar import EdgarClient
from swing_copilot.data.yfinance_provider import YFinanceProvider
from swing_copilot.llm.client import LLMClient
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
from swing_copilot.report.discord_notify import DiscordNotifier
from swing_copilot.report.html_report import (
    MARKET_STRIP_SYMBOLS,
    ReportContext,
    render_report,
)
from swing_copilot.risk.checks import RiskChecker
from swing_copilot.screening.base import ScreeningInput
from swing_copilot.screening.pipeline import ScreeningPipeline
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.text.calendar_fred import FredCalendarClient
from swing_copilot.text.edgar_filings import fetch_recent_filings_text
from swing_copilot.text.news_finnhub import FinnhubNewsClient
from swing_copilot.universe import UniverseFetchOptions, get_sp500_universe

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from uuid import UUID

    import pandas as pd
    from pydantic import BaseModel

    from swing_copilot.clock import Clock
    from swing_copilot.config import Settings, StrategiesConfig
    from swing_copilot.data.base import BarFetchResult, DataProvider
    from swing_copilot.llm.client import AnalyzeRequest
    from swing_copilot.llm.schemas import FilingAnalysis, NewsSummary
    from swing_copilot.report.discord_notify import Notifier
    from swing_copilot.risk.checks import RiskAssessment
    from swing_copilot.screening.base import Candidate
    from swing_copilot.storage.market_store import FundamentalsRecord
    from swing_copilot.text.base import TextItem
    from swing_copilot.universe import UniverseMember

_FUNDAMENTALS_LOOKBACK_DAYS = 400  # enough for SMA200 warmup / recent filings
_TEXT_LOOKBACK_DAYS = 14
_FILING_FORM_TYPES = ["8-K", "10-Q"]


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
    edgar_client: _EdgarClientLike | None = None
    news_client: _NewsClientLike | None = None
    calendar_client: _CalendarClientLike | None = None
    llm_client: _LLMClientLike | None = None
    notifier: Notifier | None = None
    provider_name: str = "yfinance"
    strategy_key: str = "default"
    templates_dir: str = "templates"
    output_dir: str = "reports"


@dataclass(frozen=True, slots=True)
class _StepOutcome:
    success: bool
    detail: str | None = None
    is_skipped: bool = False


@dataclass(frozen=True, slots=True)
class _RunContext:
    """Screening-derived state steps 5-9 share (keeps step functions under 5 args)."""

    run_id: UUID
    run_date: date
    candidates: list[Candidate]
    risk_assessments: list[RiskAssessment]


def _config_hash(settings: Settings) -> str:
    payload = json.dumps(settings.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _select_symbols(
    universe: tuple[UniverseMember, ...], held_symbols: set[str], limit: int | None
) -> list[str]:
    if limit is None:
        return [member.symbol for member in universe]
    limited = [member.symbol for member in universe[:limit]]
    return sorted({*limited, *held_symbols})


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
        start = as_of - timedelta(days=_FUNDAMENTALS_LOOKBACK_DAYS)
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


def _run_step_fundamentals(
    deps: DailyDependencies, symbols: list[str], as_of: date
) -> _StepOutcome:
    if deps.edgar_client is None:
        return _StepOutcome(
            True, "skipped: no EDGAR client configured", is_skipped=True
        )

    as_of_cutoff = datetime.combine(as_of, datetime.max.time(), tzinfo=UTC)
    records = []
    failed_symbols = []
    for symbol in symbols:
        try:
            records.extend(deps.edgar_client.fetch_fundamentals(symbol, as_of_cutoff))
        except Exception:
            failed_symbols.append(symbol)

    if records:
        deps.market_store.upsert_fundamentals(records)
    if failed_symbols and not records:
        return _StepOutcome(
            False, f"EDGAR fetch failed for every symbol: {failed_symbols}"
        )
    detail = f"failed symbols: {failed_symbols}" if failed_symbols else None
    return _StepOutcome(True, detail)


def _run_step_screening(
    deps: DailyDependencies, symbols: list[str], as_of: date, run_id: UUID
) -> tuple[_StepOutcome, list[Candidate]]:
    fundamentals = deps.market_store.read_fundamentals(as_of)
    start = as_of - timedelta(days=_FUNDAMENTALS_LOOKBACK_DAYS)
    bars = deps.market_store.read_bars(symbols, start, as_of, as_of)

    data = ScreeningInput(
        as_of=as_of, universe=deps.universe, fundamentals=fundamentals, bars=bars
    )
    pipeline = ScreeningPipeline(
        deps.strategies_config, deps.market_store, deps.settings, deps.strategy_key
    )
    candidates = pipeline.run(data)
    deps.state_store.record_candidates(candidates, run_id, pipeline.strategy_key)
    return _StepOutcome(True), candidates


def _run_step_risk(
    deps: DailyDependencies, candidates: list[Candidate], run_id: UUID
) -> tuple[_StepOutcome, list[RiskAssessment]]:
    portfolio = deps.state_store.get_open_positions(is_paper=True)
    checker = RiskChecker(deps.settings, deps.universe, deps.market_store)
    assessments = checker.check(
        candidates, portfolio, deps.settings.risk.account_equity_usd
    )
    deps.state_store.record_risk_assessments(assessments, run_id)
    return _StepOutcome(True), assessments


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
    as_of_cutoff = datetime.combine(as_of, datetime.max.time(), tzinfo=UTC)
    try:
        items: list[TextItem] = []
        for symbol in symbols:
            if deps.news_client is not None:
                items.extend(
                    deps.news_client.fetch_company_news(symbol, since, as_of=as_of)
                )
            if deps.edgar_client is not None:
                items.extend(
                    fetch_recent_filings_text(
                        deps.edgar_client, symbol, _FILING_FORM_TYPES, as_of
                    )
                )
        if deps.calendar_client is not None:
            items.extend(
                deps.calendar_client.fetch_calendar_events(
                    as_of, as_of + timedelta(days=14)
                )
            )
    except Exception as exc:
        return _StepOutcome(False, f"unexpected error: {exc}"), None

    deps.state_store.record_text_items(items)
    _ = as_of_cutoff  # kept for symmetry with edgar's as_of_cutoff convention
    return _StepOutcome(True), items


def _run_step_llm(
    deps: DailyDependencies,
    ctx: _RunContext,
    text_items: list[TextItem] | None,
    *,
    skip: bool,
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

    try:
        news_summaries = _summarize_news_per_candidate(
            llm_client, deps, ctx, text_items
        )
        filing_analyses = _analyze_filings_per_candidate(
            llm_client, deps, ctx, text_items
        )
    except Exception as exc:
        return _StepOutcome(False, f"unexpected error: {exc}"), None, None
    return _StepOutcome(True), news_summaries, filing_analyses


def _summarize_news_per_candidate(
    llm_client: _LLMClientLike,
    deps: DailyDependencies,
    ctx: _RunContext,
    text_items: list[TextItem],
) -> list[NewsSummary]:
    summaries = []
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
        )
        summaries.append(summarize_news(llm_client, request))
    return summaries


def _analyze_filings_per_candidate(
    llm_client: _LLMClientLike,
    deps: DailyDependencies,
    ctx: _RunContext,
    text_items: list[TextItem],
) -> list[FilingAnalysis]:
    analyses = []
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
            )
            analyses.append(analyze_filing(llm_client, request))
    return analyses


def _run_step_report(
    deps: DailyDependencies,
    ctx: _RunContext,
    news_summaries: list[NewsSummary] | None,
    filing_analyses: list[FilingAnalysis] | None,
) -> tuple[_StepOutcome, Path | None]:
    context = ReportContext(
        run_id=ctx.run_id,
        run_date=ctx.run_date,
        generated_at=deps.clock.now(),
        universe=deps.universe,
        candidates=ctx.candidates,
        risk_assessments=ctx.risk_assessments,
        news_summaries=news_summaries,
        filing_analyses=filing_analyses,
    )
    try:
        report_path = render_report(
            context,
            deps.market_store,
            deps.state_store,
            templates_dir=deps.templates_dir,
            output_dir=deps.output_dir,
        )
    except Exception as exc:
        return _StepOutcome(False, f"unexpected error: {exc}"), None
    return _StepOutcome(True), report_path


def _notification_summary(candidates: list[Candidate], run_date: date) -> str:
    return (
        f"[swing-copilot] {run_date.isoformat()}: {len(candidates)} candidate(s) today"
    )


def _run_step_notify(
    deps: DailyDependencies,
    report_path: Path | None,
    candidates: list[Candidate],
    run_date: date,
) -> _StepOutcome:
    if not deps.settings.notification.enabled or deps.notifier is None:
        return _StepOutcome(True, "skipped: notification disabled", is_skipped=True)
    if report_path is None:
        return _StepOutcome(True, "skipped: no report was generated", is_skipped=True)

    sent = deps.notifier.notify(
        _notification_summary(candidates, run_date), report_path
    )
    if not sent:
        return _StepOutcome(False, "Discord webhook notification failed")
    return _StepOutcome(True)


def _maybe_open_report(report_path: Path, options: DailyRunOptions) -> bool:
    """Auto-open `report_path` in the default browser for a local live run.

    Never invoked during `--dry-run`, `--no-open`, or any CI environment
    (`docs/05_ui_design.md` 10.3).

    Args:
        report_path: Path to the generated report.
        options: Parsed CLI options.

    Returns:
        Whether `webbrowser.open()` reported success. A `False` return does
        not fail report generation — the caller records it as a warning.
    """
    if options.is_dry_run or options.no_open or os.environ.get("CI"):
        return False
    return webbrowser.open(report_path.resolve().as_uri())


def _run_step_open(
    deps: DailyDependencies, report_path: Path | None, options: DailyRunOptions
) -> _StepOutcome:
    if report_path is None:
        return _StepOutcome(True, "skipped: no report was generated", is_skipped=True)
    opened = _maybe_open_report(report_path, options)
    detail = None if opened else "browser not opened (dry-run/no-open/CI, or refused)"
    _ = deps  # kept for symmetry with the other step functions
    return _StepOutcome(True, detail)


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
    deps.state_store.record_run_step(run_id, step, status, outcome.detail, duration)


def run_daily(options: DailyRunOptions, deps: DailyDependencies) -> DailyRunResult:
    """Run the full nine-step daily batch.

    Args:
        options: Parsed CLI options (`--as-of`, `--limit`, `--dry-run`, ...).
        deps: Real (or fake, for dry-run/tests) collaborators.

    Returns:
        The run outcome. `exit_code` is nonzero only if one of steps 1-4
        (prices, fundamentals, screening, risk) failed outright; steps 5-9
        degrade the run (`RunStatus.DEGRADED`) but keep `exit_code == 0`.
    """
    mode = RunMode.DRY_RUN if options.is_dry_run else RunMode.LIVE
    fetch_cutoff = options.as_of or deps.clock.today()
    held_symbols = {
        position.symbol for position in deps.state_store.get_open_positions()
    }
    symbols = _select_symbols(deps.universe, held_symbols, options.limit)
    # The market strip (SPY/QQQ/^VIX/^TNX) is never part of the screening
    # universe, but its bars still need fetching here so the report has
    # something to read (docs/05_ui_design.md 7.2).
    price_symbols = sorted({*symbols, *MARKET_STRIP_SYMBOLS})

    prefetched_prices: BarFetchResult | None = None
    prefetch_error: str | None = None
    run_date = fetch_cutoff
    if options.as_of is None:
        try:
            start = fetch_cutoff - timedelta(days=_FUNDAMENTALS_LOOKBACK_DAYS)
            prefetched_prices = deps.data_provider.get_daily_bars(
                price_symbols, start, fetch_cutoff + timedelta(days=1)
            )
            if not prefetched_prices.bars.empty:
                latest = max(prefetched_prices.bars["date"])
                run_date = latest.date() if isinstance(latest, datetime) else latest
        except Exception as exc:
            prefetch_error = f"unexpected error: {exc}"

    run_id = deps.state_store.start_run(run_date, mode, _config_hash(deps.settings))

    candidates: list[Candidate] = []
    risk_assessments: list[RiskAssessment] = []

    def _step_screening() -> _StepOutcome:
        nonlocal candidates
        outcome, candidates = _run_step_screening(deps, symbols, run_date, run_id)
        return outcome

    def _step_risk() -> _StepOutcome:
        nonlocal risk_assessments
        outcome, risk_assessments = _run_step_risk(deps, candidates, run_id)
        return outcome

    def _step_prices() -> _StepOutcome:
        if prefetch_error is not None:
            return _StepOutcome(False, prefetch_error)
        return _run_step_prices(deps, price_symbols, run_date, prefetched_prices)

    fatal_steps: list[tuple[str, Callable[[], _StepOutcome]]] = [
        ("1_prices", _step_prices),
        ("2_fundamentals", lambda: _run_step_fundamentals(deps, symbols, run_date)),
        ("3_screening", _step_screening),
        ("4_risk", _step_risk),
    ]

    for step_name, step_fn in fatal_steps:
        started_at = time.perf_counter()
        try:
            outcome = step_fn()
        except Exception as exc:
            outcome = _StepOutcome(False, f"unexpected error: {exc}")
        _record_step(deps, run_id, step_name, outcome, started_at)
        if not outcome.success:
            deps.state_store.complete_run(
                run_id, RunStatus.FAILED, error_summary=outcome.detail
            )
            return DailyRunResult(run_id, run_date, RunStatus.FAILED, exit_code=1)

    ctx = _RunContext(
        run_id=run_id,
        run_date=run_date,
        candidates=candidates,
        risk_assessments=risk_assessments,
    )
    return _run_soft_steps(options, deps, ctx)


def _run_soft_steps(
    options: DailyRunOptions, deps: DailyDependencies, ctx: _RunContext
) -> DailyRunResult:
    """Run steps 5-9: fail-soft text/LLM, then report/notify/browser-open."""
    degraded = False
    text_symbols = sorted({candidate.symbol for candidate in ctx.candidates})

    started_at = time.perf_counter()
    text_outcome, text_items = _run_step_text(
        deps, text_symbols, ctx.run_date, skip=options.skip_text
    )
    _record_step(deps, ctx.run_id, "5_text", text_outcome, started_at)
    degraded = degraded or not text_outcome.success

    started_at = time.perf_counter()
    llm_outcome, news_summaries, filing_analyses = _run_step_llm(
        deps, ctx, text_items, skip=options.skip_llm
    )
    _record_step(deps, ctx.run_id, "6_llm", llm_outcome, started_at)
    degraded = degraded or not llm_outcome.success

    started_at = time.perf_counter()
    report_outcome, report_path = _run_step_report(
        deps, ctx, news_summaries, filing_analyses
    )
    _record_step(deps, ctx.run_id, "7_report", report_outcome, started_at)
    degraded = degraded or not report_outcome.success

    started_at = time.perf_counter()
    notify_outcome = _run_step_notify(deps, report_path, ctx.candidates, ctx.run_date)
    _record_step(deps, ctx.run_id, "8_notify", notify_outcome, started_at)
    degraded = degraded or not notify_outcome.success

    started_at = time.perf_counter()
    open_outcome = _run_step_open(deps, report_path, options)
    _record_step(deps, ctx.run_id, "9_open", open_outcome, started_at)
    degraded = degraded or not open_outcome.success

    final_status = RunStatus.DEGRADED if degraded else RunStatus.SUCCESS
    deps.state_store.complete_run(ctx.run_id, final_status, report_path=report_path)
    return DailyRunResult(
        ctx.run_id, ctx.run_date, final_status, exit_code=0, report_path=report_path
    )


def _parse_args(argv: list[str] | None = None) -> DailyRunOptions:
    parser = argparse.ArgumentParser(prog="copilot-daily")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-text", action="store_true")
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    return DailyRunOptions(
        as_of=args.as_of,
        is_dry_run=args.dry_run,
        skip_text=args.skip_text,
        skip_llm=args.skip_llm,
        limit=args.limit,
        no_open=args.no_open,
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
    secrets = load_secrets()
    require_secrets(secrets, _required_features(options, settings))

    database = Database()
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
        news_client=news_client,
        calendar_client=calendar_client,
        llm_client=llm_client,
        notifier=notifier,
    )


def _monthly_budget_cap(settings: Settings) -> float:
    return settings.budget.monthly_cap_usd_prototype


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: parse args, compose real dependencies, run, exit.

    Args:
        argv: Argument list, or `None` to use `sys.argv[1:]`.
    """
    options = _parse_args(argv)
    settings = load_settings()
    strategies = load_strategies()
    deps = _compose_dependencies(options, settings, strategies)
    result = run_daily(options, deps)
    raise SystemExit(result.exit_code)


if __name__ == "__main__":  # pragma: no cover
    main()
