"""CLI entry point `copilot-backtest` (P2-08/P2-09/P2-10, roadmap §5 P2-08-10).

Wires the real `MarketStore`/S&P 500 universe into `backtest.runner.run_backtest`
and renders P2-07's risk-adjusted metrics to terminal (Rich) and an atomically
written markdown report, promoting the backtester from tests-only to a daily
tool (diagnosis D5's execution side). `--pessimistic` (P2-09) additionally runs
a higher-slippage scenario and renders a normal-vs-pessimistic comparison. The
`grid` subcommand (P2-10) runs a 25-cell ATR-stop x max-hold sensitivity grid
and classifies it as spike/plateau/inconclusive. `entry-grid` (Issue #357)
runs the fixed entry-limit ATR-multiple sensitivity values.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from swing_copilot.backtest.candidate_stream import (
    CandidateStreamError,
    compute_cache_key,
    generate_candidate_stream,
    load_candidate_stream,
    load_market_frame,
    save_candidate_stream,
)
from swing_copilot.backtest.earnings_history import (
    EARNINGS_FILING_FORMS,
    load_derived_earnings_calendar,
)
from swing_copilot.backtest.policy import (
    EntryPolicyArm,
    EntryPolicyError,
    build_entry_policy,
    parse_policy_arms,
)
from swing_copilot.backtest.render import (
    ReportMeta,
    render_entry_grid_markdown,
    render_entry_grid_terminal,
    render_grid_markdown,
    render_grid_terminal,
    render_markdown,
    render_markdown_comparison,
    render_policy_comparison_markdown,
    render_policy_comparison_terminal,
    render_terminal,
    render_terminal_comparison,
)
from swing_copilot.backtest.runner import (
    BacktestCostOverrides,
    BacktestDependencies,
    BacktestRequest,
    run_backtest,
)
from swing_copilot.backtest.sensitivity import (
    GridCell,
    entry_limit_grid_values,
    grid_param_values,
    judge_grid,
)
from swing_copilot.cli_support import ExitPolicy, run_cli
from swing_copilot.config import load_settings, load_strategies
from swing_copilot.exceptions import ConfigError, StorageSchemaError, SwingCopilotError
from swing_copilot.io_atomic import write_text_atomically
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import (
    MarketStore,
    ParquetRootNotFoundError,
    resolve_parquet_root,
)
from swing_copilot.storage.state_store import StateStore
from swing_copilot.universe import (
    UniverseFetchOptions,
    get_sp500_universe,
    select_persisted_universe,
)
from swing_copilot.universe_sampling import select_universe_sample

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from swing_copilot.backtest.candidate_stream import CandidateStream, MarketFrame
    from swing_copilot.config import Settings, StrategiesConfig
    from swing_copilot.risk.checks import EarningsGuardInput
    from swing_copilot.universe_sampling import UniverseSample

_DEFAULT_OUTPUT_DIR = Path("reports/backtests")
# Overridable so a configuration variant can be compared against the baseline
# without editing the repository's own settings.yaml (`tracking/cli.py` sets
# the same precedent).
DEFAULT_SETTINGS_PATH = "config/settings.yaml"
# Ranking score_weights live here, not in settings.yaml, so comparing a
# weighting variant needs its own override alongside --settings.
DEFAULT_STRATEGIES_PATH = "config/strategies.yaml"
_CONSOLE_WIDTH = 200
#: `--policy` default: the pre-Issue-#184 behaviour, so an existing command
#: line keeps measuring what it used to measure.
_DEFAULT_POLICY = EntryPolicyArm.NONE.value
#: An unusable settings/strategies file: one line on stderr, exit 1.
_CONFIG_EXIT = ExitPolicy(errors=(ConfigError,), code=1)


class BacktestCliError(SwingCopilotError):
    """Raised for fail-fast argument/strategy errors, before any backtest runs."""


def _add_common_args(
    parser: argparse.ArgumentParser, *, is_subcommand: bool = False
) -> None:
    # Not `required=True`: with subparsers, argparse enforces a *parent*
    # parser's own required options even when a subcommand (e.g. `grid`)
    # consumes the actual values, since they're set on the shared Namespace
    # only after the parent's own requirements are checked. `_validate_args`
    # enforces presence explicitly instead, uniformly for both commands.
    #
    # The subcommand copy uses `SUPPRESS` for every default: argparse parses a
    # subcommand into a fresh namespace and then copies *all* of it onto the
    # shared one, so a real default here would overwrite a value the operator
    # already passed before the subcommand. `--strategy`/`--start`/`--end`
    # would merely be reset to `None` and caught by `_validate_args`, but
    # `--settings`/`--strategies` would silently snap back to the repository
    # defaults and the grid would measure the baseline while reporting the
    # variant. `SUPPRESS` leaves the key out of the sub-namespace entirely
    # unless it was actually given, so the parent's value survives.
    def default(value: object) -> object:
        return argparse.SUPPRESS if is_subcommand else value

    parser.add_argument("--strategy", default=default(None))
    parser.add_argument("--start", type=date.fromisoformat, default=default(None))
    parser.add_argument("--end", type=date.fromisoformat, default=default(None))
    parser.add_argument("--limit", type=int, default=default(None))
    parser.add_argument("--output", type=Path, default=default(None))
    parser.add_argument("--db", type=Path, default=default(DEFAULT_DB_PATH))
    parser.add_argument("--settings", default=default(DEFAULT_SETTINGS_PATH))
    parser.add_argument("--strategies", default=default(DEFAULT_STRATEGIES_PATH))
    parser.add_argument("--candidate-cache", type=Path, default=default(None))
    # Issue #184: a comma-separated list turns one invocation into an A/B over
    # the same candidate stream, which is the only way to answer "did the
    # regime gate improve the result?".
    parser.add_argument("--policy", default=default(_DEFAULT_POLICY))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="copilot-backtest")
    _add_common_args(parser)
    parser.add_argument("--pessimistic", action="store_true")

    subparsers = parser.add_subparsers(dest="command")
    grid_parser = subparsers.add_parser(
        "grid", help="パラメータ感応度グリッド（ATRストップ倍率 x 最大保有日数）"
    )
    _add_common_args(grid_parser, is_subcommand=True)
    entry_grid_parser = subparsers.add_parser(
        "entry-grid", help="指値エントリー倍率（k）の感応度グリッド"
    )
    _add_common_args(entry_grid_parser, is_subcommand=True)
    parser.set_defaults(command="run")

    return parser.parse_args(argv)


def _validate_args(args: argparse.Namespace, strategies: StrategiesConfig) -> None:
    """Fail-fast checks that must run before any I/O (REQ-021/022)."""
    missing = [
        flag
        for flag, value in (
            ("--strategy", args.strategy),
            ("--start", args.start),
            ("--end", args.end),
        )
        if value is None
    ]
    if missing:
        msg = f"必須引数が指定されていません: {', '.join(missing)}"
        raise BacktestCliError(msg)
    if args.start > args.end:
        msg = f"--start ({args.start}) は --end ({args.end}) より後ろにできません。"
        raise BacktestCliError(msg)
    if args.limit is not None and args.limit <= 0:
        msg = "--limit は1以上の整数で指定してください。"
        raise BacktestCliError(msg)
    if args.strategy not in strategies.strategies:
        available = ", ".join(sorted(strategies.strategies))
        msg = f"戦略 '{args.strategy}' は見つかりません。利用可能: {available}"
        raise BacktestCliError(msg)
    arms = _policy_arms(args)
    if args.pessimistic and len(arms) > 1:
        msg = (
            "--pessimistic と複数アームの --policy は同時に指定できません"
            "（比較軸が2つになり、どちらの効果か読めなくなるため）。"
        )
        raise BacktestCliError(msg)


def _policy_arms(args: argparse.Namespace) -> tuple[EntryPolicyArm, ...]:
    """Parse `--policy`, re-raising as the CLI's own fail-fast error."""
    try:
        return parse_policy_arms(args.policy)
    except EntryPolicyError as exc:
        raise BacktestCliError(str(exc)) from exc


def _reject_grid_policy(args: argparse.Namespace) -> None:
    """Refuse `grid --policy <non-default>` instead of ignoring it silently.

    Ignoring the flag would print a report labelled as one thing and measured
    as another.
    """
    if _policy_arms(args) != (EntryPolicyArm.NONE,):
        msg = (
            "grid サブコマンドは --policy に対応していません"
            f"（--policy {_DEFAULT_POLICY} のみ）。"
        )
        raise BacktestCliError(msg)


def _reject_entry_grid_policy(args: argparse.Namespace) -> None:
    """Refuse entry-grid with a non-default policy instead of ignoring it."""
    if _policy_arms(args) != (EntryPolicyArm.NONE,):
        msg = (
            "entry-grid サブコマンドは --policy に対応していません"
            f"（--policy {_DEFAULT_POLICY} のみ）。"
        )
        raise BacktestCliError(msg)


def _output_path(args: argparse.Namespace) -> Path:
    output: Path | None = args.output
    if output is not None:
        return output
    return _DEFAULT_OUTPUT_DIR / f"{args.end.isoformat()}-{args.strategy}.md"


def _grid_output_path(args: argparse.Namespace) -> Path:
    output: Path | None = args.output
    if output is not None:
        return output
    return _DEFAULT_OUTPUT_DIR / f"{args.end.isoformat()}-{args.strategy}-grid.md"


def _entry_grid_output_path(args: argparse.Namespace) -> Path:
    output: Path | None = args.output
    if output is not None:
        return output
    return _DEFAULT_OUTPUT_DIR / f"{args.end.isoformat()}-{args.strategy}-entry-grid.md"


def _missing_data_symbols(
    market_store: MarketStore, symbols: Sequence[str], start: date, end: date
) -> list[str]:
    """Symbols with zero bars anywhere in [start, end] (REQ-020's fail-soft note)."""
    if not symbols:
        return []
    bars = market_store.read_bars(list(symbols), start, end, as_of=end)
    present = set(bars["symbol"].unique()) if not bars.empty else set()
    return sorted(set(symbols) - present)


#: What a bars-root-less backtest produced instead of failing (Issue #217).
_MISSING_BARS_CONSEQUENCE = (
    "このまま実行すると全銘柄がデータ不足となり、取引ゼロのレポートを"
    "正常終了として書いてしまう。"
)


def _resolve_parquet_root(db_path: Path) -> Path:
    """Resolve `--db`'s sibling bars root, failing fast when it is absent (Issue #217).

    Thin adapter over `storage.market_store.resolve_parquet_root`, which the
    other `--db`-taking CLIs share since Issue #221: the check and its message
    are one implementation, and each command only supplies its own
    consequence sentence and converts to its own error type.

    Args:
        db_path: The `--db` value.

    Returns:
        The `bars/` directory next to `db_path`.

    Raises:
        BacktestCliError: The resolved `bars/` is not an existing directory.
    """
    try:
        return resolve_parquet_root(db_path, consequence=_MISSING_BARS_CONSEQUENCE)
    except ParquetRootNotFoundError as exc:
        raise BacktestCliError(str(exc)) from exc


def _compose_dependencies(
    args: argparse.Namespace, settings: Settings, strategies: StrategiesConfig
) -> tuple[BacktestDependencies, UniverseSample, list[str]]:
    """Wire real collaborators (composition root); returns deps, sample, missing data."""
    parquet_root = _resolve_parquet_root(Path(args.db))
    db_path = Path(args.db)
    if not db_path.is_file():
        msg = (
            f"バックテスト用DuckDBが見つかりません: {db_path}。"
            "先に data-pull またはデータ収集を実行して、初期化済みのDBを用意してください。"
        )
        raise BacktestCliError(msg)

    database = Database(args.db, read_only=True)
    market_store = MarketStore(database, parquet_root=parquet_root)
    state_store = StateStore(database)
    try:
        state_store.validate_read_only_schema()
        market_store.validate_read_only_schema()
    except StorageSchemaError as exc:
        msg = (
            f"バックテスト用DuckDBのスキーマが未初期化です: {db_path}。"
            f"{exc}。書き込み可能な実行（data-pull またはデータ収集）で"
            "スキーマを初期化してから再実行してください。"
        )
        raise BacktestCliError(msg) from exc
    universe_options = UniverseFetchOptions(
        snapshot_path=settings.universe.snapshot_path,
        manual_include=settings.universe.manual_include,
        manual_exclude=settings.universe.manual_exclude,
    )
    persisted_universe = select_persisted_universe(
        args.end, state_store, options=universe_options
    )
    universe = (
        persisted_universe.members
        if persisted_universe is not None
        else tuple(
            get_sp500_universe(
                args.end,
                options=universe_options,
            )
        )
    )
    sample = select_universe_sample(universe, args.limit)
    missing_data_symbols = _missing_data_symbols(
        market_store, sample.symbols, args.start, args.end
    )
    deps = BacktestDependencies(
        market_store=market_store,
        universe=universe,
        settings=settings,
        strategies_config=strategies,
    )
    return deps, sample, missing_data_symbols


def _resolve_candidate_stream(
    request: BacktestRequest,
    deps: BacktestDependencies,
    frame: MarketFrame,
    cache_path: Path | None,
) -> CandidateStream:
    """Reuse the persisted candidate stream when it matches, else screen anew.

    The cache key covers only what screening reads, so a cache written by a
    baseline run stays valid across an exit-parameter or cost sweep and is
    invalidated the moment the universe, window, strategy, screening settings,
    or price data move. A cache that cannot be read is a miss, not a failure.

    Args:
        request: What to backtest.
        deps: Real collaborators (store, universe, settings, strategies).
        frame: The already-loaded market frame.
        cache_path: `--candidate-cache`; `None` disables persistence.

    Returns:
        The stream to hand to every `run_backtest` call for this invocation.
    """
    expected_key = compute_cache_key(request, deps, frame)
    if cache_path is not None and cache_path.exists():
        try:
            cached = load_candidate_stream(cache_path)
        except CandidateStreamError as exc:
            sys.stdout.write(
                f"候補ストリームキャッシュを読めませんでした（{exc}）。再生成します。\n"
            )
        else:
            if cached.cache_key == expected_key:
                sys.stdout.write(f"候補ストリームキャッシュを再利用: {cache_path}\n")
                return cached
            sys.stdout.write(
                "候補ストリームキャッシュのキーが一致しません。再生成して上書きします: "
                f"{cache_path}\n"
            )

    stream = generate_candidate_stream(request, deps, frame)
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        save_candidate_stream(stream, cache_path)
        sys.stdout.write(f"候補ストリームキャッシュを保存: {cache_path}\n")
    return stream


def _earnings_guard_fn(
    deps: BacktestDependencies,
    sample: UniverseSample,
    arms: Sequence[EntryPolicyArm],
    *,
    as_of: date,
) -> Callable[[date, tuple[str, ...]], EarningsGuardInput] | None:
    """Build the point-in-time earnings lookup, when an arm can use one (#201).

    Only `regime+earnings` consults the earnings guard, so the filing history is
    read only for that arm — a `none`/`regime` run must not pay for a query
    whose answer it would discard, nor print a coverage line about a gate it
    never applies.

    Args:
        deps: Real collaborators; supplies the store and `risk.*` settings.
        sample: The symbols this run backtests.
        arms: The `--policy` arms about to run.
        as_of: The backtest window's end; the calendar re-applies the cutoff
            per simulated day.

    Returns:
        The lookup, or `None` when no arm applies the earnings gate.
    """
    if EntryPolicyArm.REGIME_EARNINGS not in arms:
        return None
    calendar = load_derived_earnings_calendar(
        deps.market_store,
        sample.symbols,
        as_of=as_of,
        lookahead_days=deps.settings.risk.earnings_lookahead_days,
    )
    projectable = len(calendar.projectable_symbols)
    sys.stdout.write(
        f"決算ゲート: 提出履歴（{'/'.join(EARNINGS_FILING_FORMS)}）から"
        f"{projectable}/{len(sample.symbols)} 銘柄の決算日を推定します"
        "（提出日は発表日より遅れる。docs/reference.md 参照）\n"
    )
    return calendar.lookup


def _run_backtest_command(
    args: argparse.Namespace, settings: Settings, strategies: StrategiesConfig
) -> None:
    try:
        _validate_args(args, strategies)
        deps, sample, missing_data_symbols = _compose_dependencies(
            args, settings, strategies
        )
        request = BacktestRequest(
            symbols=list(sample.symbols),
            start=args.start,
            end=args.end,
            initial_cash=settings.backtest.initial_cash_usd,
            strategy_key=args.strategy,
        )
        # Screening is identical across the normal/pessimistic pair -- only
        # slippage differs, and no Filter or Signal reads it -- so both
        # scenarios share one frame and one stream.
        frame = load_market_frame(request, deps)
        stream = _resolve_candidate_stream(request, deps, frame, args.candidate_cache)
        # One stream, one frame, N arms: the whole point of the A/B is that
        # nothing but the gates differs between the columns (Issue #184).
        arms = _policy_arms(args)
        earnings_guard_fn = _earnings_guard_fn(deps, sample, arms, as_of=args.end)
        policies = [
            build_entry_policy(
                arm,
                settings,
                frame.bars,
                earnings_guard_fn=earnings_guard_fn,
            )
            for arm in arms
        ]
        if args.pessimistic:
            normal_result = run_backtest(
                request,
                deps,
                BacktestCostOverrides(slippage_multiplier=1.0),
                candidate_stream=stream,
                market_frame=frame,
                entry_policy=policies[0],
            )
            pessimistic_result = run_backtest(
                request,
                deps,
                BacktestCostOverrides(
                    slippage_multiplier=settings.backtest.pessimistic_slippage_multiplier
                ),
                candidate_stream=stream,
                market_frame=frame,
                entry_policy=policies[0],
            )
        else:
            arm_results = [
                (
                    arm.value,
                    run_backtest(
                        request,
                        deps,
                        candidate_stream=stream,
                        market_frame=frame,
                        entry_policy=policy,
                    ),
                )
                for arm, policy in zip(arms, policies, strict=True)
            ]
    except (BacktestCliError, CandidateStreamError, EntryPolicyError) as exc:
        # Stays an inline `try` rather than a `run_cli()` call (Issue #193):
        # the block produces several locals the rest of the function reads,
        # and moving the conversion out to `main()` would widen the catch over
        # the rendering that follows. Same convention as everywhere else --
        # the message is the exit status.
        raise SystemExit(str(exc)) from exc

    meta = ReportMeta(
        strategy=args.strategy,
        start=args.start,
        end=args.end,
        missing_data_symbols=missing_data_symbols,
        universe_sample=sample,
    )
    if args.pessimistic:
        terminal_text = render_terminal_comparison(
            normal_result, pessimistic_result, meta
        )
        markdown_text = render_markdown_comparison(
            normal_result, pessimistic_result, meta
        )
    elif len(arm_results) > 1:
        terminal_text = render_policy_comparison_terminal(arm_results, meta)
        markdown_text = render_policy_comparison_markdown(arm_results, meta)
    else:
        terminal_text = render_terminal(arm_results[0][1], meta)
        markdown_text = render_markdown(arm_results[0][1], meta)

    sys.stdout.write(terminal_text)

    output_path = _output_path(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(output_path, markdown_text)
    sys.stdout.write(f"\nReport written to {output_path}\n")


def _run_grid_command(
    args: argparse.Namespace, settings: Settings, strategies: StrategiesConfig
) -> None:
    try:
        _validate_args(args, strategies)
        _reject_grid_policy(args)
        deps, sample, missing_data_symbols = _compose_dependencies(
            args, settings, strategies
        )
        request = BacktestRequest(
            symbols=list(sample.symbols),
            start=args.start,
            end=args.end,
            initial_cash=settings.backtest.initial_cash_usd,
            strategy_key=args.strategy,
        )
        # The 25 cells vary only `exit_atr_multiple`/`max_hold_days`, which
        # the engine consumes and screening never reads: one frame and one
        # candidate stream serve the whole grid (Issue #185).
        frame = load_market_frame(request, deps)
        stream = _resolve_candidate_stream(request, deps, frame, args.candidate_cache)
    except (BacktestCliError, CandidateStreamError) as exc:
        # Inline for the same reason as `_run_backtest_command` above: the
        # prepared frame/stream feed the grid loop, and the stream is consumed
        # lazily there, so a wider catch would convert failures that reach the
        # operator as a traceback today.
        raise SystemExit(str(exc)) from exc

    cells: list[GridCell] = []
    for atr_pct, max_hold_pct, atr_value, max_hold_value in grid_param_values(
        settings.trade_plan.exit_atr_multiple, settings.trade_plan.max_hold_days
    ):
        cell_result = run_backtest(
            request,
            deps,
            BacktestCostOverrides(
                exit_atr_multiple=atr_value, max_hold_days=max_hold_value
            ),
            candidate_stream=stream,
            market_frame=frame,
        )
        cells.append(
            GridCell(
                atr_multiplier_pct=atr_pct,
                max_hold_pct=max_hold_pct,
                expectancy_per_trade=cell_result.expectancy_per_trade,
                trade_count=cell_result.trade_count,
            )
        )
    grid_result = judge_grid(cells, settings.backtest)

    meta = ReportMeta(
        strategy=args.strategy,
        start=args.start,
        end=args.end,
        missing_data_symbols=missing_data_symbols,
        universe_sample=sample,
    )
    gray_threshold = settings.backtest.insufficient_trade_count_threshold

    sys.stdout.write(render_grid_terminal(grid_result, meta, gray_threshold))

    output_path = _grid_output_path(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(
        output_path, render_grid_markdown(grid_result, meta, gray_threshold)
    )
    sys.stdout.write(f"\nReport written to {output_path}\n")


def _run_entry_grid_command(
    args: argparse.Namespace, settings: Settings, strategies: StrategiesConfig
) -> None:
    try:
        _validate_args(args, strategies)
        _reject_entry_grid_policy(args)
        deps, sample, missing_data_symbols = _compose_dependencies(
            args, settings, strategies
        )
        request = BacktestRequest(
            symbols=list(sample.symbols),
            start=args.start,
            end=args.end,
            initial_cash=settings.backtest.initial_cash_usd,
            strategy_key=args.strategy,
        )
        # The entry-limit multiplier is an engine-only input, so one frame and
        # one candidate stream serve every k value (Issue #357).
        frame = load_market_frame(request, deps)
        stream = _resolve_candidate_stream(request, deps, frame, args.candidate_cache)
    except (BacktestCliError, CandidateStreamError) as exc:
        raise SystemExit(str(exc)) from exc

    results = [
        (
            k_value,
            run_backtest(
                request,
                deps,
                BacktestCostOverrides(entry_limit_atr_multiple=k_value),
                candidate_stream=stream,
                market_frame=frame,
            ),
        )
        for k_value in entry_limit_grid_values()
    ]

    meta = ReportMeta(
        strategy=args.strategy,
        start=args.start,
        end=args.end,
        missing_data_symbols=missing_data_symbols,
        universe_sample=sample,
    )
    sys.stdout.write(render_entry_grid_terminal(results, meta))

    output_path = _entry_grid_output_path(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(output_path, render_entry_grid_markdown(results, meta))
    sys.stdout.write(f"\nReport written to {output_path}\n")


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: parse args, run the backtest or grid, print + write the report."""
    args = _parse_args(argv)
    settings = run_cli(lambda: load_settings(args.settings), _CONFIG_EXIT)
    strategies = run_cli(lambda: load_strategies(args.strategies), _CONFIG_EXIT)

    if args.command == "grid":
        _run_grid_command(args, settings, strategies)
    elif args.command == "entry-grid":
        _run_entry_grid_command(args, settings, strategies)
    else:
        _run_backtest_command(args, settings, strategies)


if __name__ == "__main__":  # pragma: no cover
    main()
