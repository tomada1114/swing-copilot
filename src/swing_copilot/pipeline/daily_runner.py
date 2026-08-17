"""Run lifecycle for the daily batch.

This module owns the imperative sequencing and terminal-state decisions.  The
individual step implementations remain in :mod:`daily`, while CLI parsing and
real-adapter composition live in :mod:`daily_composition`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from swing_copilot.exceptions import PreflightAbort
from swing_copilot.models import DailyRunOptions, DailyRunResult, RunStatus
from swing_copilot.pipeline.daily import (
    _TIME_BUDGET_STEP_OUTCOME,
    ACCOUNT_EQUITY_UNSET_NOTICE,
    DailyDependencies,
    _config_hash,
    _OutputCompletion,
    _OutputContext,
    _record_exposure_decision,
    _record_ftd_snapshot,
    _record_regime_snapshot,
    _record_step,
    _RiskStepRequest,
    _run_mae_mfe_soft_step,
    _run_metadata,
    _run_mode,
    _run_step_analysis_export,
    _run_step_fundamentals,
    _run_step_notify,
    _run_step_output,
    _run_step_postmortem,
    _run_step_prices,
    _run_step_retro_collect,
    _run_step_retro_evaluate,
    _run_step_risk,
    _run_step_screening,
    _run_step_track_update,
    _run_text_soft_step,
    _RunContext,
    _screening_lookback_days,
    _select_symbols,
    _step_started,
    _StepOutcome,
    _text_target_symbols,
    _warn_stale_runs,
)
from swing_copilot.report.daily_brief import MARKET_STRIP_SYMBOLS
from swing_copilot.storage.tracking_records import OPEN

logger = logging.getLogger(__name__)
_HISTORICAL_POSITION_NOTICE = (
    "NO_POSITION_DATA: historical replay does not use current position state"
)

__all__ = ["DailyDependencies", "run_daily"]

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from swing_copilot.data.base import BarFetchResult
    from swing_copilot.models import Position
    from swing_copilot.regime.exposure import ExposureDecision
    from swing_copilot.regime.ftd import FtdSnapshot
    from swing_copilot.regime.gate import RegimeSnapshot
    from swing_copilot.report.daily_brief import SignalPerformanceRow
    from swing_copilot.risk.checks import PortfolioHeatResult, RiskAssessment
    from swing_copilot.risk.circuit_breaker import CircuitBreakerResult
    from swing_copilot.screening.base import (
        Candidate,
        RejectionRecord,
        TruncatedCandidate,
    )


def _held_symbols(
    deps: DailyDependencies,
    portfolio: Sequence[Position],
    *,
    is_historical: bool,
) -> set[str]:
    """Union the real open positions with the tracked open virtual ones.

    This set only decides what gets *collected and analysed* -- the held-first
    text/filing targets of `docs/04_detailed_design.md` 3.14. It is deliberately
    not the `portfolio` passed to the risk step: a virtual position must never
    reach sizing, concentration, or correlation as if the account held it.

    Until real trading starts, `positions` is always empty, and the only record
    of what is notionally held is `verdict_positions`, the virtual ledger of
    past `proceed` verdicts. Without reading it, held-symbol news collection
    never fires at all, and Finnhub company-news cannot be fetched
    retroactively, so every missed day is permanent data loss.

    A historical replay (`--as-of`) deliberately skips the ledger and keeps the
    held set to the (empty) real positions: the ledger records the *current*
    position state with no point-in-time history, so reading it would leak
    today's knowledge into a past as-of date.

    A ledger read failure is fail-soft: it is logged and the virtual side is
    treated as empty rather than failing the run over an analysis-scope input.

    Args:
        deps: Dependency bundle supplying the `state_store` to read.
        portfolio: Real open positions (empty on a historical replay).
        is_historical: Whether this run replays an explicit `--as-of` date.

    Returns:
        Symbols to prioritise as held for collection and analysis.
    """
    symbols = {position.symbol for position in portfolio}
    if is_historical:
        return symbols
    try:
        tracked = deps.state_store.get_verdict_positions(OPEN)
    except Exception:
        logger.exception(
            "verdict tracking ledger unreadable: continuing without virtual positions"
        )
        return symbols
    return symbols | {position.symbol for position in tracked}


def run_daily(  # noqa: PLR0915 - the documented batch lifecycle is intentionally linear
    options: DailyRunOptions, deps: DailyDependencies
) -> DailyRunResult:
    """Run the full eight-step daily batch.

    Required steps 1--4 fail the run. Optional source, analysis-export,
    notification, and report-context failures preserve any local run artifact
    and return a degraded terminal state.
    """
    run_started_at = deps.monotonic()
    budget_s = deps.settings.schedule.timeout_minutes * 60
    deadline = run_started_at + budget_s

    mode = _run_mode(options)
    fetch_cutoff = options.as_of or deps.clock.today()
    is_historical = options.as_of is not None
    portfolio = (
        [] if is_historical else deps.state_store.get_open_positions(is_paper=True)
    )
    held_symbols = _held_symbols(deps, portfolio, is_historical=is_historical)
    symbols = _select_symbols(deps.universe, held_symbols, options.limit)
    # The market strip is never screened but must be fetched for report context.
    price_symbols = sorted({*symbols, *MARKET_STRIP_SYMBOLS})

    prefetched_prices: BarFetchResult | None = None
    prefetch_error: str | None = None
    run_date = fetch_cutoff
    if options.as_of is None:
        try:
            start = fetch_cutoff - timedelta(days=_screening_lookback_days(deps))
            prefetched_prices = deps.data_provider.get_daily_bars(
                price_symbols, start, fetch_cutoff + timedelta(days=1)
            )
            if not prefetched_prices.bars.empty:
                latest = max(prefetched_prices.bars["date"])
                run_date = latest.date() if isinstance(latest, datetime) else latest
        except Exception as exc:
            prefetch_error = f"unexpected error: {exc}"

    if not options.allow_same_day_rerun:
        existing = deps.state_store.get_successful_run(run_date)
        if existing is not None:
            report = existing.report_path
            msg = (
                f"preflight abort: {run_date.isoformat()} に対して成功済みの run が"
                f"既にあります (run_id={existing.run_id}, "
                f"report={report if report is not None else '不明'})。"
                "再実行するには --allow-same-day-rerun を指定してください。"
            )
            raise PreflightAbort(msg, reason="same_day_rerun")

    config_hash = _config_hash(deps.settings, deps.strategies_config, deps.strategy_key)
    run_id = deps.state_store.start_run(
        run_date,
        mode,
        config_hash,
        metadata=_run_metadata(deps),
    )
    logger.info(
        "run %s started: mode=%s run_date=%s symbols=%d",
        run_id,
        mode.value,
        run_date,
        len(symbols),
    )

    if deps.universe_warning is not None:
        logger.warning(
            "run %s universe data quality: %s", run_id, deps.universe_warning
        )
        _record_step(
            deps,
            run_id,
            "0_universe",
            _StepOutcome(False, deps.universe_warning),
            time.perf_counter(),
        )

    stale_cutoff = deps.clock.now() - timedelta(seconds=budget_s)
    stale_run_ids = deps.state_store.mark_stale_running_runs(stale_cutoff, run_id)
    _warn_stale_runs(run_id, stale_run_ids)

    empty_run_data: tuple[
        list[Candidate],
        list[RejectionRecord],
        list[TruncatedCandidate],
        list[RiskAssessment],
    ] = ([], [], [], [])
    candidates, rejections, truncated, risk_assessments = empty_run_data
    regime_snapshot: RegimeSnapshot | None = None
    exposure_decision: ExposureDecision | None = None
    ftd_snapshot: FtdSnapshot | None = None
    portfolio_heat: PortfolioHeatResult | None = None
    circuit_breaker: CircuitBreakerResult | None = None
    earnings_guard_notice: str | None = None

    def _step_screening() -> _StepOutcome:
        nonlocal candidates, rejections, truncated
        outcome, screening = _run_step_screening(deps, symbols, run_date, run_id)
        candidates = screening.candidates
        rejections = screening.rejections
        truncated = screening.truncated
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
        ) = _run_step_risk(
            deps,
            _RiskStepRequest(
                candidates,
                portfolio,
                run_id,
                run_date,
                exposure_decision,
                is_historical,
            ),
        )
        return outcome

    def _step_prices() -> _StepOutcome:
        if prefetch_error is not None:
            return _StepOutcome(False, prefetch_error)
        return _run_step_prices(deps, price_symbols, run_date, prefetched_prices)

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
        logger.debug("step %s starting", step_name)
        _step_started(deps, step_name)
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

    ctx = _RunContext(
        run_id=run_id,
        run_date=run_date,
        candidates=candidates,
        rejections=rejections,
        truncated=truncated,
        risk_assessments=risk_assessments,
        portfolio_heat=cast("PortfolioHeatResult", portfolio_heat),
        circuit_breaker=cast("CircuitBreakerResult", circuit_breaker),
        earnings_guard_notice=earnings_guard_notice,
        held_symbols=frozenset(held_symbols),
        regime_snapshot=cast("RegimeSnapshot", regime_snapshot),
        exposure_decision=cast("ExposureDecision", exposure_decision),
        ftd_snapshot=cast("FtdSnapshot", ftd_snapshot),
    )
    return _run_soft_steps(options, deps, ctx, deadline)


def _run_soft_steps(
    options: DailyRunOptions,
    deps: DailyDependencies,
    ctx: _RunContext,
    deadline: float,
) -> DailyRunResult:
    """Run fail-soft local and optional steps after required steps finish."""
    degraded = deps.universe_warning is not None
    text_symbols = _text_target_symbols(ctx.held_symbols, ctx.candidates)

    excursion_outcome = _run_mae_mfe_soft_step(
        deps,
        ctx.run_id,
        ctx.run_date,
        is_historical=options.as_of is not None,
    )
    degraded = degraded or not excursion_outcome.success

    text_outcome, text_items = _run_text_soft_step(
        options, deps, ctx, deadline, text_symbols
    )
    degraded = degraded or not text_outcome.success

    started_at = time.perf_counter()
    signal_performance: tuple[SignalPerformanceRow, ...]
    if deps.monotonic() >= deadline:
        logger.warning("step 6_analysis_export skipped: time budget exceeded")
        export_outcome, analysis_input_path, analysis_input_digest = (
            _TIME_BUDGET_STEP_OUTCOME,
            None,
            None,
        )
    else:
        logger.debug("step 6_analysis_export starting")
        _step_started(deps, "6_analysis_export")
        export_outcome, analysis_input_path, analysis_input_digest = (
            _run_step_analysis_export(
                deps,
                ctx,
                text_items,
                include_decision_history=(
                    not options.is_dry_run and options.as_of is None
                ),
            )
        )
    _record_step(deps, ctx.run_id, "6_analysis_export", export_outcome, started_at)
    degraded = degraded or not export_outcome.success

    started_at = time.perf_counter()
    if deps.monotonic() >= deadline:
        logger.warning("step postmortem skipped: time budget exceeded")
        postmortem_outcome, signal_performance = _TIME_BUDGET_STEP_OUTCOME, ()
    else:
        logger.debug("step postmortem starting")
        postmortem_outcome, signal_performance = _run_step_postmortem(
            deps, ctx.run_date
        )
    _record_step(deps, ctx.run_id, "postmortem", postmortem_outcome, started_at)
    degraded = degraded or not postmortem_outcome.success

    degraded = _run_retro_soft_steps(deps, ctx, deadline) or degraded

    started_at = time.perf_counter()
    if deps.monotonic() >= deadline:
        logger.warning("step 7_notify skipped: time budget exceeded")
        notify_outcome = _TIME_BUDGET_STEP_OUTCOME
    else:
        logger.debug("step 7_notify starting")
        _step_started(deps, "7_notify")
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
        ((deps.universe_warning,) if deps.universe_warning is not None else ())
        + ((_HISTORICAL_POSITION_NOTICE,) if options.as_of is not None else ())
        + (
            (ACCOUNT_EQUITY_UNSET_NOTICE,)
            if deps.settings.risk.account_equity_usd is None
            else ()
        )
        + ((ctx.earnings_guard_notice,) if ctx.earnings_guard_notice else ())
        + tuple(
            f"{label}: {outcome.detail}"
            for label, outcome in (
                ("MAE/MFE", excursion_outcome),
                ("text", text_outcome),
                ("analysis export", export_outcome),
                ("postmortem", postmortem_outcome),
                ("notification", notify_outcome),
            )
            if outcome.detail is not None
            and (not outcome.success or not outcome.is_skipped)
        )
    )
    started_at = time.perf_counter()
    logger.debug("step 8_output starting")
    _step_started(deps, "8_output")
    output_outcome, report_path, brief = _run_step_output(
        deps,
        _OutputContext(
            run=ctx,
            analysis_input_path=analysis_input_path,
            analysis_input_digest=analysis_input_digest,
            signal_performance=signal_performance,
            notices=notices,
            status=status_before_output,
        ),
    )
    _record_step(deps, ctx.run_id, "8_output", output_outcome, started_at)
    return _finalize_output(
        deps,
        ctx,
        _OutputCompletion(
            outcome=output_outcome,
            report_path=report_path,
            brief=brief,
            analysis_input_path=analysis_input_path,
            text_outcome=text_outcome,
            export_outcome=export_outcome,
        ),
        degraded,
    )


def _run_retro_soft_steps(
    deps: DailyDependencies,
    ctx: _RunContext,
    deadline: float,
) -> bool:
    """Run the offline, idempotent verdict-observation steps in the daily batch.

    Only `collect` and `evaluate` belong here out of the retrospective: both
    are offline and idempotent, and running them daily stops an un-evaluated
    run from ageing out of the evaluation window while also backing up the
    archived `analysis_result.json` into DuckDB. `export` (which fetches
    freshness data over the network) and the `swing-retro` skill stay manual.

    `track_update` joins them for the same reasons -- offline, idempotent,
    reading only already-persisted bars -- but answers a different question:
    it carries each `proceed` verdict's virtual position forward under the
    backtest's exit rules rather than classifying a matured horizon.

    Each step runs even when an earlier one failed: the verdicts collected on
    previous days remain evaluable, and the tracked positions remain
    advanceable, regardless of today's scan.

    Returns:
        Whether any step degraded the run.
    """
    started_at = time.perf_counter()
    if deps.monotonic() >= deadline:
        logger.warning("step retro_collect skipped: time budget exceeded")
        collect_outcome = _TIME_BUDGET_STEP_OUTCOME
    else:
        logger.debug("step retro_collect starting")
        collect_outcome = _run_step_retro_collect(deps)
    _record_step(deps, ctx.run_id, "retro_collect", collect_outcome, started_at)

    started_at = time.perf_counter()
    if deps.monotonic() >= deadline:
        logger.warning("step retro_evaluate skipped: time budget exceeded")
        evaluate_outcome = _TIME_BUDGET_STEP_OUTCOME
    else:
        logger.debug("step retro_evaluate starting")
        evaluate_outcome = _run_step_retro_evaluate(deps, ctx.run_date)
    _record_step(deps, ctx.run_id, "retro_evaluate", evaluate_outcome, started_at)

    started_at = time.perf_counter()
    if deps.monotonic() >= deadline:
        logger.warning("step track_update skipped: time budget exceeded")
        track_outcome = _TIME_BUDGET_STEP_OUTCOME
    else:
        logger.debug("step track_update starting")
        track_outcome = _run_step_track_update(deps, ctx.run_date)
    _record_step(deps, ctx.run_id, "track_update", track_outcome, started_at)

    return (
        not collect_outcome.success
        or not evaluate_outcome.success
        or not track_outcome.success
    )


def _finalize_output(
    deps: DailyDependencies,
    ctx: _RunContext,
    completion: _OutputCompletion,
    degraded: bool,
) -> DailyRunResult:
    """Persist and return the only terminal state compatible with step 8."""
    missing_sources = _missing_sources(
        deps, completion.text_outcome, completion.export_outcome
    )
    if not completion.outcome.success and completion.report_path is None:
        deps.state_store.complete_run(
            ctx.run_id,
            RunStatus.FAILED,
            error_summary=completion.outcome.detail,
        )
        logger.error(
            "run %s failed to produce a local report: %s",
            ctx.run_id,
            completion.outcome.detail,
        )
        return DailyRunResult(
            ctx.run_id,
            ctx.run_date,
            RunStatus.FAILED,
            exit_code=1,
            brief=completion.brief,
            analysis_input_path=completion.analysis_input_path,
            provider_name=deps.provider_name,
            data_tier=deps.data_tier,
            missing_sources=missing_sources,
        )

    final_degraded = degraded or not completion.outcome.success
    final_status = RunStatus.DEGRADED if final_degraded else RunStatus.SUCCESS
    deps.state_store.complete_run(
        ctx.run_id, final_status, report_path=completion.report_path
    )
    logger.info("run %s completed: status=%s", ctx.run_id, final_status.value)
    return DailyRunResult(
        ctx.run_id,
        ctx.run_date,
        final_status,
        exit_code=0,
        report_path=completion.report_path,
        brief=completion.brief,
        analysis_input_path=completion.analysis_input_path,
        provider_name=deps.provider_name,
        data_tier=deps.data_tier,
        missing_sources=missing_sources,
    )


def _missing_sources(
    deps: DailyDependencies,
    text_outcome: _StepOutcome,
    export_outcome: _StepOutcome,
) -> tuple[str, ...]:
    """List unavailable source boundaries without conflating notifications."""
    return tuple(
        label
        for label, outcome in (
            ("universe", _StepOutcome(deps.universe_warning is None)),
            ("text", text_outcome),
            ("analysis input", export_outcome),
        )
        if not outcome.success
    )
