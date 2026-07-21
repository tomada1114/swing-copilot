"""Daily batch orchestrator, steps 1-4 (FR-12).

Wires price update -> fundamentals update -> screening -> risk check with
explicit `as_of`/`run_id` and fatal-error semantics: any of these four
steps failing outright aborts the run (`runs.status=failed`) since
screening cannot meaningfully proceed without them
(`docs/03_basic_design.md` 7). Steps 5-9 (text, LLM, report, notify,
browser-open) are wired in at P2-6, once their modules exist.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from swing_copilot.models import DailyRunResult, RunMode, RunStatus, StepStatus
from swing_copilot.risk.checks import RiskChecker
from swing_copilot.screening.base import ScreeningInput
from swing_copilot.screening.pipeline import ScreeningPipeline

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date
    from uuid import UUID

    import pandas as pd

    from swing_copilot.clock import Clock
    from swing_copilot.config import Settings
    from swing_copilot.data.base import BarFetchResult, DataProvider
    from swing_copilot.models import DailyRunOptions
    from swing_copilot.screening.base import Candidate
    from swing_copilot.storage.market_store import FundamentalsRecord, MarketStore
    from swing_copilot.storage.state_store import StateStore
    from swing_copilot.universe import UniverseMember

_FUNDAMENTALS_LOOKBACK_DAYS = 400  # enough for SMA200 warmup / recent filings


class _EdgarClientLike(Protocol):
    """Structural stand-in for `data.edgar.EdgarClient`, for fake injection."""

    def fetch_fundamentals(
        self, symbol: str, as_of: datetime
    ) -> list[FundamentalsRecord]:
        """Fetch normalized fundamentals filed on or before `as_of`."""
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
    provider_name: str = "yfinance"
    strategy_key: str = "default"


@dataclass(frozen=True, slots=True)
class _StepOutcome:
    success: bool
    detail: str | None = None


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
        return _StepOutcome(True, "skipped: no EDGAR client configured")

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
) -> _StepOutcome:
    portfolio = deps.state_store.get_open_positions(is_paper=True)
    checker = RiskChecker(deps.settings, deps.universe, deps.market_store)
    assessments = checker.check(
        candidates, portfolio, deps.settings.risk.account_equity_usd
    )
    deps.state_store.record_risk_assessments(assessments, run_id)
    return _StepOutcome(True)


def _record_step(
    deps: DailyDependencies,
    run_id: UUID,
    step: str,
    outcome: _StepOutcome,
    started_at: float,
) -> None:
    duration = time.perf_counter() - started_at
    status = StepStatus.SUCCESS if outcome.success else StepStatus.FAILED
    deps.state_store.record_run_step(run_id, step, status, outcome.detail, duration)


def run_daily(options: DailyRunOptions, deps: DailyDependencies) -> DailyRunResult:
    """Run daily batch steps 1-4: prices, fundamentals, screening, risk.

    Args:
        options: Parsed CLI options (`--as-of`, `--limit`, `--dry-run`, ...).
        deps: Real (or fake, for dry-run/tests) collaborators.

    Returns:
        The run outcome. `exit_code` is 0 only if all four steps succeeded.
    """
    mode = RunMode.DRY_RUN if options.is_dry_run else RunMode.LIVE
    fetch_cutoff = options.as_of or deps.clock.today()
    held_symbols = {
        position.symbol for position in deps.state_store.get_open_positions()
    }
    symbols = _select_symbols(deps.universe, held_symbols, options.limit)

    prefetched_prices: BarFetchResult | None = None
    prefetch_error: str | None = None
    run_date = fetch_cutoff
    if options.as_of is None:
        try:
            start = fetch_cutoff - timedelta(days=_FUNDAMENTALS_LOOKBACK_DAYS)
            prefetched_prices = deps.data_provider.get_daily_bars(
                symbols, start, fetch_cutoff + timedelta(days=1)
            )
            if not prefetched_prices.bars.empty:
                latest = max(prefetched_prices.bars["date"])
                run_date = latest.date() if isinstance(latest, datetime) else latest
        except Exception as exc:
            prefetch_error = f"unexpected error: {exc}"

    run_id = deps.state_store.start_run(run_date, mode, _config_hash(deps.settings))

    candidates: list[Candidate] = []

    def _step_screening() -> _StepOutcome:
        nonlocal candidates
        outcome, candidates = _run_step_screening(deps, symbols, run_date, run_id)
        return outcome

    def _step_prices() -> _StepOutcome:
        if prefetch_error is not None:
            return _StepOutcome(False, prefetch_error)
        return _run_step_prices(deps, symbols, run_date, prefetched_prices)

    steps: list[tuple[str, Callable[[], _StepOutcome]]] = [
        ("1_prices", _step_prices),
        ("2_fundamentals", lambda: _run_step_fundamentals(deps, symbols, run_date)),
        ("3_screening", _step_screening),
        ("4_risk", lambda: _run_step_risk(deps, candidates, run_id)),
    ]

    for step_name, step_fn in steps:
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

    deps.state_store.complete_run(run_id, RunStatus.SUCCESS)
    return DailyRunResult(run_id, run_date, RunStatus.SUCCESS, exit_code=0)
