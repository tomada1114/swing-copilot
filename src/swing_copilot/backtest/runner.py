"""CLI-facing backtest entry point.

Wires the real `MarketStore`/`ScreeningPipeline` into `BacktestEngine` (FR-10).
Candidate generation itself lives in `backtest/candidate_stream.py`, so one
screening pass can feed many engine runs (Issue #185).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from swing_copilot.backtest.candidate_stream import (
    CandidateStreamMismatchError,
    compute_cache_key,
    generate_candidate_stream,
    load_market_frame,
)
from swing_copilot.backtest.engine import BacktestEngine, BacktestResult

if TYPE_CHECKING:
    from datetime import date

    from swing_copilot.backtest.candidate_stream import CandidateStream, MarketFrame
    from swing_copilot.backtest.policy import EntryPolicy
    from swing_copilot.config import Settings
    from swing_copilot.screening.base import Candidate
    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.universe import UniverseMember


@dataclass(frozen=True, slots=True)
class BacktestDependencies:
    """Real collaborators `run_backtest` composes together."""

    market_store: MarketStore
    universe: tuple[UniverseMember, ...]
    settings: Settings
    strategies_config: dict[str, Any]  # Any: arbitrary-depth parsed YAML


@dataclass(frozen=True, slots=True)
class BacktestRequest:
    """What to backtest: universe, window, and starting cash."""

    symbols: list[str]
    start: date
    end: date
    initial_cash: float
    strategy_key: str = "default"


@dataclass(frozen=True, slots=True)
class BacktestCostOverrides:
    """Cost/benchmark overrides, defaulting to `settings.yaml`'s own values."""

    commission_pct: float | None = None
    slippage_pct: float | None = None
    benchmark_symbol: str | None = None
    slippage_multiplier: float | None = None
    entry_limit_atr_multiple: float | None = None  # Issue #326: entry sensitivity
    exit_atr_multiple: float | None = None  # P2-10: sensitivity grid parameter
    max_hold_days: int | None = None  # P2-10: sensitivity grid parameter


def run_backtest(  # noqa: PLR0913 - the three keyword-only injection seams
    # (stream, frame, policy) are independent optional reuse points; folding
    # them into one object would force every caller to build it.
    request: BacktestRequest,
    deps: BacktestDependencies,
    overrides: BacktestCostOverrides | None = None,
    *,
    candidate_stream: CandidateStream | None = None,
    market_frame: MarketFrame | None = None,
    entry_policy: EntryPolicy | None = None,
) -> BacktestResult:
    """Run a deterministic multi-symbol backtest using production screening logic.

    Args:
        request: What to backtest (symbols, window, starting cash).
        deps: Real collaborators (store, universe, settings, strategies).
        overrides: Cost/benchmark overrides; defaults to `settings.backtest`'s
            own commission/slippage and `"SPY"`.
        entry_policy: Production entry gates to apply between candidate and
            fill (`backtest/policy.build_entry_policy`). `None` is the
            `--policy none` arm. The policy is an engine-side concern only, so
            an A/B over arms reuses one candidate stream unchanged.
        candidate_stream: A stream already screened for exactly these inputs
            (`candidate_stream.generate_candidate_stream`). Supplying it is
            what lets a sensitivity grid or a `--pessimistic` pair screen once
            and run the engine many times; its cache key is re-verified here,
            so a stale stream fails loudly instead of quietly measuring the
            wrong universe. Omitted, one is generated internally.
        market_frame: Bars/fundamentals/trading days already loaded for this
            window, so a sweep reads storage once.

    Returns:
        The full trade log, equity curves, and survivorship bias note.

    Raises:
        CandidateStreamMismatchError: `candidate_stream` or `market_frame` was
            built from different screening inputs than this request implies.
    """
    overrides = overrides or BacktestCostOverrides()
    benchmark_symbol = overrides.benchmark_symbol or deps.settings.backtest.benchmark
    commission_pct = (
        overrides.commission_pct
        if overrides.commission_pct is not None
        else deps.settings.backtest.commission_pct
    )
    slippage_pct = (
        overrides.slippage_pct
        if overrides.slippage_pct is not None
        else deps.settings.backtest.slippage_pct
    )
    slippage_multiplier = (
        overrides.slippage_multiplier
        if overrides.slippage_multiplier is not None
        else deps.settings.backtest.slippage_multiplier
    )
    entry_limit_atr_multiple = (
        overrides.entry_limit_atr_multiple
        if overrides.entry_limit_atr_multiple is not None
        else deps.settings.backtest.entry_limit_atr_multiple
    )
    exit_atr_multiple = (
        overrides.exit_atr_multiple
        if overrides.exit_atr_multiple is not None
        else deps.settings.backtest.exit_atr_multiple
    )
    max_hold_days = (
        overrides.max_hold_days
        if overrides.max_hold_days is not None
        else deps.settings.backtest.max_hold_days
    )

    effective_settings = deps.settings.model_copy(
        update={
            "backtest": deps.settings.backtest.model_copy(
                update={
                    "commission_pct": commission_pct,
                    "slippage_pct": slippage_pct,
                    "slippage_multiplier": slippage_multiplier,
                    "entry_limit_atr_multiple": entry_limit_atr_multiple,
                    "exit_atr_multiple": exit_atr_multiple,
                    "max_hold_days": max_hold_days,
                    "benchmark": benchmark_symbol,
                }
            )
        }
    )

    frame = market_frame or load_market_frame(
        request, deps, benchmark_symbol=benchmark_symbol
    )
    if frame.benchmark_symbol != benchmark_symbol:
        msg = (
            "渡された market_frame のベンチマーク "
            f"'{frame.benchmark_symbol}' が今回の '{benchmark_symbol}' と一致しません。"
            "取引日カレンダーが変わるため、フレームを読み直してください。"
        )
        raise CandidateStreamMismatchError(msg)
    stream = _resolve_stream(request, deps, frame, candidate_stream)

    # The pipeline behind `stream` was built from `deps.settings`, not from
    # `effective_settings`: screening reads only `technical_signals` and
    # `fundamental_filters`, so the cost/exit overrides applied above cannot
    # change a candidate. That equivalence is what makes one stream reusable
    # across a whole parameter sweep, and it is pinned by
    # `tests/backtest/test_candidate_stream.py`.
    def candidates_fn(day: date) -> list[Candidate]:
        return list(stream.candidates_by_day.get(day, ()))

    engine = BacktestEngine(effective_settings, entry_policy)
    return engine.run(
        list(frame.trading_days),
        frame.bars,
        candidates_fn,
        request.initial_cash,
        benchmark_symbol,
    )


def _resolve_stream(
    request: BacktestRequest,
    deps: BacktestDependencies,
    frame: MarketFrame,
    candidate_stream: CandidateStream | None,
) -> CandidateStream:
    """Verify a supplied stream, or screen one now.

    Verification is cheap: `frame` already carries its content digests, so
    this re-derives the key without touching storage or re-screening.
    """
    if candidate_stream is None:
        return generate_candidate_stream(request, deps, frame)
    expected = compute_cache_key(request, deps, frame)
    if candidate_stream.cache_key != expected:
        msg = (
            "渡された候補ストリームは今回のスクリーニング入力から生成されたものでは"
            "ありません（cache_key 不一致）。銘柄・期間・戦略・設定・価格データの"
            "いずれかが変わっています。ストリームを再生成してください。"
        )
        raise CandidateStreamMismatchError(msg)
    return candidate_stream
