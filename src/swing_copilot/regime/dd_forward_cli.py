"""`copilot-dd-forward`: does a Distribution Day level predict what follows?

`config/settings.yaml` ships `regime.dd_*` marked 要検証 (roadmap §5 P3-13).
This command remains the historical DD-level explorer: it measures whether
those levels separate subsequent price action, while the live Issue #252
exposure gate also considers SMA200, VIX, and FTD. The archived DD-only sweep
must therefore not be read as the live gate's six-branch behavior.

This replays the stored history one `as_of` at a time, classifies each date
exactly as `pipeline/daily.py::_calculate_regime_snapshot` would, and reports the
return and drawdown that actually followed -- per level, per target, per horizon.
The forward window is an evaluation-only look-ahead; classification never sees
past its own date, and the whole scan is bounded by `--as-of`.

Strictly offline and read-only, following `screening/filter_matrix_cli.py`: bars
come from the existing repository with its own point-in-time cutoff, no schema
migration runs, an absent `--db` is an error rather than a fresh database, and
`--json` is the only intended write (through the analysis boundary's atomic
replacement helper). `MarketStore` still opens the shared DuckDB read-write for
its own view, so DuckDB's single-writer lock applies: do not run this while
`copilot-daily` holds the file.
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser, Namespace
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
from rich.console import Console
from rich.table import Table

from swing_copilot.cli_support import ExitPolicy, run_cli
from swing_copilot.config import load_settings
from swing_copilot.exceptions import ConfigError, SwingCopilotError
from swing_copilot.io_atomic import write_json_atomically
from swing_copilot.regime.dd_forward import (
    DEFAULT_HORIZONS,
    INDEX_TARGETS,
    ForwardScan,
    ForwardScanRequest,
    LevelStats,
    level_series,
    scan_forward,
    summarise_levels,
)
from swing_copilot.regime.dd_forward_sweep import (
    BOUNDARY_NAMES,
    GRID_RANGES,
    ExposureBoundaries,
    GridAxis,
    GridFilters,
    ScanFrame,
    SweepPoint,
    dd_only_exposure,
    score,
    sweep_boundary,
    sweep_grid,
)
from swing_copilot.regime.distribution import (
    DistributionLevel,
    DistributionThresholds,
    distribution_severity,
)
from swing_copilot.regime.exposure import ExposureVerdict
from swing_copilot.regime.ftd import FtdThresholds
from swing_copilot.regime.gate import GateThresholds, RegimeThresholds
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import (
    MarketStore,
    ParquetRootNotFoundError,
    resolve_parquet_root,
)
from swing_copilot.storage.state_store import StateStore
from swing_copilot.universe import UniverseFetchOptions, select_persisted_universe

#: The strip `_calculate_regime_snapshot` itself reads. `^TNX` is in
#: `MARKET_STRIP_SYMBOLS` for the report header but never reaches the regime
#: calculation, so it is not read here.
REGIME_SYMBOLS = ("SPY", "QQQ", "^VIX")

if TYPE_CHECKING:
    import pandas as pd

    from swing_copilot.config import RegimeConfig, Settings

DEFAULT_SETTINGS_PATH = "config/settings.yaml"
#: History read before `--start` so the earliest observation has a full
#: Distribution Day window and a seeded gate SMA, matching the daily run's own
#: `2 * PRICE_HISTORY_LOOKBACK_DAYS` warm-up.
WARMUP_DAYS = 800
#: Same fixed width as `screening/filter_matrix_cli.py`: Rich must never
#: ellipsize a column because of the invoking terminal's actual size.
_CONSOLE_WIDTH = 200


class DdForwardCliError(SwingCopilotError):
    """Raised for argument, database, or coverage errors, before rendering."""


#: An unusable settings file: one line on stderr, exit 1.
_CONFIG_EXIT = ExitPolicy(errors=(ConfigError,), code=1)
#: A bad argument: the argparse convention (message as the exit status).
_ARGUMENT_EXIT = ExitPolicy(errors=(DdForwardCliError,))
#: The replay also rejects an impossible window through `ValueError`.
_SCAN_EXIT = ExitPolicy(errors=(DdForwardCliError, ValueError))

#: What a bars-root-less scan produced instead of failing (Issue #221).
_MISSING_BARS_CONSEQUENCE = (
    "このまま実行しても全銘柄が NO_DATA となり、"
    "本物の結果と同じ体裁の診断を正常終了として出してしまう。"
)


def _parse_args(argv: list[str] | None = None) -> Namespace:
    parser = ArgumentParser(
        prog="copilot-dd-forward",
        description=(
            "Distribution Day の水準ごとに、その後の指数・ユニバースの"
            "先行きリターンとドローダウンを集計する"
            "（読み取り専用の診断。settings.yaml もDBも書き換えない）。"
        ),
    )
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        help="最初の観測日（既定: 保存されている履歴の先頭）",
    )
    parser.add_argument(
        "--horizons",
        default=",".join(str(days) for days in DEFAULT_HORIZONS),
        help="先行きリターンの保有営業日数（カンマ区切り）",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="閾値を1つずつ動かしたときの露出クラス分布と先行きリターンを追加表示",
    )
    parser.add_argument(
        "--grid",
        action="store_true",
        help="順序制約を満たす閾値グリッドを全走査し、上位候補を追加表示",
    )
    parser.add_argument(
        "--score-target",
        default=INDEX_TARGETS[0],
        help="--sweep / --grid が採点に使う対象",
    )
    parser.add_argument(
        "--score-horizon", type=int, default=10, help="--sweep / --grid の採点保有日数"
    )
    parser.add_argument("--grid-top", type=int, default=15, help="--grid の表示件数")
    parser.add_argument(
        "--grid-min-episodes",
        type=int,
        default=GridFilters().min_episodes,
        help="--grid が候補に要求する CASH_PRIORITY エピソードの下限",
    )
    parser.add_argument(
        "--grid-max-cash-share",
        type=float,
        default=GridFilters().max_cash_share,
        help="--grid が許容する CASH_PRIORITY 日数比率の上限",
    )
    parser.add_argument(
        "--json", dest="json_path", type=Path, help="機械可読な集計の書き出し先"
    )
    return parser.parse_args(argv)


def _horizons(raw: str) -> tuple[int, ...]:
    """Parse `--horizons`.

    Raises:
        DdForwardCliError: A field is not a positive integer.
    """
    try:
        parsed = tuple(int(field) for field in raw.split(",") if field.strip())
    except ValueError as exc:
        msg = f"--horizons は整数のカンマ区切りで指定してください: {raw}"
        raise DdForwardCliError(msg) from exc
    if not parsed or any(days < 1 for days in parsed):
        msg = f"--horizons は1以上の整数が1つ以上必要です: {raw}"
        raise DdForwardCliError(msg)
    return parsed


def thresholds_from(config: RegimeConfig) -> RegimeThresholds:
    """Build the scan's thresholds exactly as `_calculate_regime_snapshot` does.

    Restating the mapping keeps the diagnostic honest only if it stays identical
    to the daily run's; `tests/regime/test_dd_forward_cli.py` pins the two
    against each other field by field.
    """
    return RegimeThresholds(
        gate=GateThresholds(
            sma_period=config.sma_period,
            bear_spy_sma_ratio=config.bear_spy_sma_ratio,
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
        ftd=FtdThresholds(
            correction_decline_pct=config.ftd_correction_decline_pct,
            correction_down_days=config.ftd_correction_down_days,
            ftd_gain_pct=config.ftd_gain_pct,
        ),
    )


def _basket_symbols(
    state_store: StateStore, args: Namespace, settings: Settings
) -> list[str]:
    """Universe members visible at `--as-of`, for the equal-weight basket.

    Uses the persisted snapshot rather than a live membership, for the same
    reason `screening/filter_matrix_cli.py` does: refetching would be a network
    call, and today's membership is not the membership `--as-of` saw. Returns an
    empty list when no snapshot predates `--as-of`; the index targets still
    measure, and the basket is simply absent from the report.
    """
    resolution = select_persisted_universe(
        args.as_of,
        state_store,
        options=UniverseFetchOptions(
            snapshot_path=settings.universe.snapshot_path,
            manual_include=settings.universe.manual_include,
            manual_exclude=settings.universe.manual_exclude,
        ),
    )
    if resolution is None:
        return []
    return [member.symbol for member in resolution.members]


def _read_bars(args: Namespace, settings: Settings) -> tuple[pd.DataFrame, date]:
    """Read the index strip and the `--as-of` universe, bounded at `--as-of`.

    Returns:
        The bars and the resolved first observation date.

    Raises:
        DdForwardCliError: `--db` is absent or unreadable, its sibling `bars/`
            is absent, or it holds no bars at or before `--as-of`.
    """
    db_path: Path = args.db
    if not db_path.exists():
        msg = (
            f"データベース {db_path} がありません。"
            "先に copilot-daily / copilot-backfill を実行してください"
            "（この診断は作成しません）。"
        )
        raise DdForwardCliError(msg)

    # Parquet bars live alongside the DuckDB file, mirroring `backtest/cli.py`:
    # `--db` overrides both together, never just the database. Validated
    # before `Database` is opened, so the mistake costs neither the DuckDB
    # write lock nor a diagnostic table (Issue #221).
    try:
        parquet_root = resolve_parquet_root(
            db_path, consequence=_MISSING_BARS_CONSEQUENCE
        )
    except ParquetRootNotFoundError as exc:
        raise DdForwardCliError(str(exc)) from exc
    database = Database(db_path)
    store = MarketStore(database, parquet_root=parquet_root)
    as_of: date = args.as_of
    start: date = args.start or date.min
    try:
        symbols = sorted(
            {*REGIME_SYMBOLS, *_basket_symbols(StateStore(database), args, settings)}
        )
        history_start = (
            date.min if start == date.min else start - timedelta(days=WARMUP_DAYS)
        )
        bars = store.read_bars(symbols, history_start, as_of, as_of)
    except duckdb.Error as exc:
        msg = f"{db_path} を読めません: {exc}"
        raise DdForwardCliError(msg) from exc
    if bars.empty:
        msg = f"{as_of.isoformat()} 以前のバーが1本もありません。"
        raise DdForwardCliError(msg)
    return bars, max(start, min(bars["date"]))


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    return "N/A" if value is None else f"{value * 100:+.{digits}f}%"


def _fmt_share(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1%}"


def _render_header(
    console: Console, scan: ForwardScan, thresholds: RegimeThresholds
) -> None:
    boundaries = ExposureBoundaries.from_thresholds(thresholds.distribution)
    console.print(
        f"[bold]copilot-dd-forward[/bold] "
        f"{scan.start.isoformat()}〜{scan.as_of.isoformat()} "
        f"観測日={len(scan.observations)} 除外(履歴不足)={scan.warmup_skipped} "
        f"horizons={','.join(str(days) for days in scan.horizons)}"
    )
    console.print(
        f"閾値 severe_d25/severe_d15/high_d25/high_d15/high_d5 = {boundaries.label()}"
        f"、caution_d25 = {thresholds.distribution.caution_d25}"
        f"、window={thresholds.distribution.window_days}"
    )
    console.print(
        f"等加重バスケット構成銘柄 = {scan.universe_symbols}"
        "（保存済みの現在メンバーなので生存バイアスがある。"
        "水準間の比較にのみ使い、水準の絶対水準の根拠にはしない）"
        if scan.universe_symbols
        else "等加重バスケット = なし（指数バーのみが保存されている）"
    )


def _render_distribution(
    console: Console, scan: ForwardScan, thresholds: DistributionThresholds
) -> None:
    levels = level_series(scan, thresholds)
    table = Table(
        title="水準の分布と、旧DD単独モデルが課す露出上限", header_style="bold"
    )
    table.add_column("水準")
    table.add_column("露出上限(DD単独)")
    table.add_column("日数", justify="right")
    table.add_column("構成比", justify="right")
    table.add_column("エピソード", justify="right")
    ordered = sorted(set(levels), key=distribution_severity, reverse=True)
    for level in ordered:
        days = sum(entry is level for entry in levels)
        episodes = sum(
            1
            for index, entry in enumerate(levels)
            if entry is level and (index == 0 or levels[index - 1] is not level)
        )
        table.add_row(
            level.value,
            dd_only_exposure(level).value,
            str(days),
            _fmt_share(days / len(levels) if levels else None),
            str(episodes),
        )
    console.print(table)
    console.print(
        "露出上限は市場ゲートを BULL に固定した旧DD単独の比較モデル。"
        "本番の6分岐は別にSMA200・VIX・FTDを評価する。",
    )


def _stats_row(stats: LevelStats) -> tuple[str, ...]:
    return (
        stats.level.value,
        str(stats.sample_size),
        str(stats.episode_count),
        _fmt_pct(stats.mean_return),
        _fmt_pct(stats.median_return),
        _fmt_share(stats.positive_rate),
        _fmt_pct(stats.mean_drawdown),
        _fmt_pct(stats.median_drawdown),
        _fmt_pct(stats.worst_drawdown),
    )


def _render_forward(
    console: Console, scan: ForwardScan, thresholds: DistributionThresholds
) -> None:
    for target in scan.targets:
        for horizon in scan.horizons:
            stats = summarise_levels(scan, thresholds, (target, horizon))
            if not stats:
                continue
            table = Table(
                title=f"{target} の先行き（保有 {horizon} 営業日）", header_style="bold"
            )
            table.add_column("水準")
            table.add_column("N(日)", justify="right")
            table.add_column("N(エピソード)", justify="right")
            table.add_column("平均", justify="right")
            table.add_column("中央値", justify="right")
            table.add_column("勝率", justify="right")
            table.add_column("平均DD", justify="right")
            table.add_column("中央値DD", justify="right")
            table.add_column("最悪DD", justify="right")
            for entry in stats:
                table.add_row(*_stats_row(entry))
            console.print(table)


def _render_gate_cross(
    console: Console,
    scan: ForwardScan,
    thresholds: DistributionThresholds,
    horizon: int,
) -> None:
    levels = level_series(scan, thresholds)
    table = Table(
        title=f"市場ゲート・水準の組み合わせ（SPY 保有 {horizon} 営業日の平均リターン / 日数）",
        header_style="bold",
    )
    ordered = sorted(set(levels), key=distribution_severity, reverse=True)
    table.add_column("ゲート")
    for level in ordered:
        table.add_column(level.value, justify="right")
    for gate in sorted({observation.gate for observation in scan.observations}):
        cells = []
        for level in ordered:
            returns = [
                outcome.total_return
                for observation, entry in zip(scan.observations, levels, strict=True)
                if entry is level
                and observation.gate is gate
                and (outcome := observation.outcome(INDEX_TARGETS[0], horizon))
                is not None
            ]
            mean = sum(returns) / len(returns) if returns else None
            cells.append(f"{_fmt_pct(mean)} / {len(returns)}")
        table.add_row(gate.value, *cells)
    console.print(table)


def _render_severe_triggers(
    console: Console, scan: ForwardScan, thresholds: DistributionThresholds
) -> None:
    """Attribute each SEVERE day to the boundary that produced it."""
    counts = {"d25 のみ": 0, "d15 のみ": 0, "両方": 0}
    for observation in scan.observations:
        if observation.level(thresholds) is not DistributionLevel.SEVERE:
            continue
        by_d25 = any(
            index.d25 >= thresholds.severe_d25
            for index in (observation.spy, observation.qqq)
        )
        by_d15 = any(
            index.d15 >= thresholds.severe_d15
            for index in (observation.spy, observation.qqq)
        )
        key = "両方" if by_d25 and by_d15 else ("d25 のみ" if by_d25 else "d15 のみ")
        counts[key] += 1
    table = Table(title="SEVERE を出した条件の内訳", header_style="bold")
    table.add_column("発火条件")
    table.add_column("日数", justify="right")
    for label, days in counts.items():
        table.add_row(label, str(days))
    console.print(table)


def _sweep_cells(point: SweepPoint) -> tuple[str, ...]:
    """The scored columns of one candidate, without its label.

    Every one of the three DD-driven ceilings gets a column: the `high_*`
    boundaries cannot move a single `CASH_PRIORITY` day, so a CASH-only table
    would render their sweeps as flat and say nothing about them.
    """
    blocked = point.stats(ExposureVerdict.CASH_PRIORITY)
    reduced = point.stats(ExposureVerdict.REDUCE_ONLY)
    allowed = point.stats(ExposureVerdict.NEW_ENTRY_ALLOWED)
    return (
        _fmt_share(point.cash_share),
        str(blocked.episodes if blocked else 0),
        _fmt_pct(blocked.mean_return if blocked else None),
        _fmt_share(reduced.share if reduced else None),
        str(reduced.episodes if reduced else 0),
        _fmt_pct(reduced.mean_return if reduced else None),
        _fmt_share(allowed.share if allowed else None),
        _fmt_pct(allowed.mean_return if allowed else None),
        _fmt_pct(point.return_gap),
        _fmt_pct(point.drawdown_gap),
    )


def _sweep_columns(table: Table) -> None:
    table.add_column("設定")
    table.add_column("CASH比率", justify="right")
    table.add_column("CASHエピソード", justify="right")
    table.add_column("CASH平均", justify="right")
    table.add_column("REDUCE比率", justify="right")
    table.add_column("REDUCEエピソード", justify="right")
    table.add_column("REDUCE平均", justify="right")
    table.add_column("ALLOWED比率", justify="right")
    table.add_column("ALLOWED平均", justify="right")
    table.add_column("差(非CASH-CASH)", justify="right")
    table.add_column("DD差", justify="right")


def _render_sweep(console: Console, frame: ScanFrame, base: ExposureBoundaries) -> None:
    console.print(
        f"[bold]閾値の一変数感度[/bold]（採点対象 {frame.target} / "
        f"保有 {frame.horizon_days} 営業日、他の4つは現行値で固定）"
    )
    for name in BOUNDARY_NAMES:
        points = sweep_boundary(frame, base, name, GRID_RANGES[name])
        if not points:
            continue
        table = Table(title=name, header_style="bold")
        _sweep_columns(table)
        for point in points:
            table.add_row(
                f"{name}={getattr(point.boundaries, name)}", *_sweep_cells(point)
            )
        console.print(table)
    console.print(
        "dd_caution_d25 は掃引しない。`_base_exposure` が CAUTION と NORMAL を"
        "同じ分岐に落とすため、露出上限を1日も動かせない（表示ラベル専用）",
    )


def _render_grid(
    console: Console, frame: ScanFrame, base: ExposureBoundaries, args: Namespace
) -> None:
    filters = GridFilters(
        min_episodes=args.grid_min_episodes, max_cash_share=args.grid_max_cash_share
    )
    console.print(
        f"[bold]閾値グリッド[/bold]（採点対象 {frame.target} / "
        f"保有 {frame.horizon_days} 営業日、"
        f"CASHエピソード>={filters.min_episodes}、"
        f"CASH比率<={filters.max_cash_share:.0%}）"
    )
    current = score(frame, base)
    titles = {
        GridAxis.CASH: (
            "CASH_PRIORITY 軸（severe_d25/severe_d15 が決める。"
            "差=非CASH平均-CASH平均 の降順）"
        ),
        GridAxis.REDUCE: (
            "REDUCE_ONLY 軸（high_d25/high_d15/high_d5 が決める。"
            "ALLOWED平均-REDUCE平均 の降順）"
        ),
    }
    for axis, title in titles.items():
        result = sweep_grid(frame, axis, filters=filters)
        console.print(
            f"順序制約を満たす候補 {result.evaluated} 件のうち "
            f"{result.filtered_out} 件を下限/上限で除外し、"
            f"{result.collapsed} 件はこの軸で既出と同じ分類なので畳んで "
            f"{len(result.points)} 通りが残った"
        )
        table = Table(title=title, header_style="bold")
        _sweep_columns(table)
        for point in result.points[: args.grid_top]:
            table.add_row(point.boundaries.label(), *_sweep_cells(point))
        table.add_section()
        table.add_row(f"{base.label()} (現行)", *_sweep_cells(current))
        console.print(table)
    console.print(
        "順位は同じ履歴の in-sample スコア。候補の絞り込みには使えるが、"
        "これ自体は out-of-sample の検証ではない",
    )


def render_terminal(
    scan: ForwardScan, thresholds: RegimeThresholds, args: Namespace
) -> str:
    """Render the whole diagnostic as Rich terminal text."""
    buffer = StringIO()
    console = Console(file=buffer, width=_CONSOLE_WIDTH)
    distribution = thresholds.distribution
    _render_header(console, scan, thresholds)
    _render_distribution(console, scan, distribution)
    _render_severe_triggers(console, scan, distribution)
    _render_forward(console, scan, distribution)
    _render_gate_cross(console, scan, distribution, args.score_horizon)
    if args.sweep or args.grid:
        frame = ScanFrame.build(scan, args.score_target, args.score_horizon)
        base = ExposureBoundaries.from_thresholds(distribution)
        if args.sweep:
            _render_sweep(console, frame, base)
        if args.grid:
            _render_grid(console, frame, base, args)
    return buffer.getvalue()


# Any: the CLI JSON payload combines heterogeneous regime statistics.
def _stats_payload(stats: LevelStats) -> dict[str, Any]:
    return {
        "level": stats.level.value,
        "target": stats.target,
        "horizon_days": stats.horizon_days,
        "sample_size": stats.sample_size,
        "episode_count": stats.episode_count,
        "mean_return": stats.mean_return,
        "median_return": stats.median_return,
        "positive_rate": stats.positive_rate,
        "mean_drawdown": stats.mean_drawdown,
        "median_drawdown": stats.median_drawdown,
        "worst_drawdown": stats.worst_drawdown,
    }


# Any: this machine-readable CLI document contains heterogeneous JSON values.
def build_payload(scan: ForwardScan, thresholds: RegimeThresholds) -> dict[str, Any]:
    """Build the `--json` document (the machine-readable form of the tables)."""
    distribution = thresholds.distribution
    levels = level_series(scan, distribution)
    return {
        "start": scan.start.isoformat(),
        "as_of": scan.as_of.isoformat(),
        "horizons": list(scan.horizons),
        "observation_count": len(scan.observations),
        "warmup_skipped": scan.warmup_skipped,
        "universe_symbols": scan.universe_symbols,
        "thresholds": {
            "severe_d25": distribution.severe_d25,
            "severe_d15": distribution.severe_d15,
            "high_d25": distribution.high_d25,
            "high_d15": distribution.high_d15,
            "high_d5": distribution.high_d5,
            "caution_d25": distribution.caution_d25,
            "window_days": distribution.window_days,
        },
        "level_days": {
            level.value: sum(entry is level for entry in levels)
            for level in sorted(set(levels), key=distribution_severity, reverse=True)
        },
        "level_stats": [
            _stats_payload(entry)
            for target in scan.targets
            for horizon in scan.horizons
            for entry in summarise_levels(scan, distribution, (target, horizon))
        ],
        "observations": [
            {
                "as_of": observation.as_of.isoformat(),
                "gate": observation.gate.value,
                "level": observation.level(distribution).value,
                "spy_d25": observation.spy.d25,
                "spy_d15": observation.spy.d15,
                "spy_d5": observation.spy.d5,
                "qqq_d25": observation.qqq.d25,
                "qqq_d15": observation.qqq.d15,
                "qqq_d5": observation.qqq.d5,
                "outcomes": [
                    {
                        "target": outcome.target,
                        "horizon_days": outcome.horizon_days,
                        "total_return": outcome.total_return,
                        "max_drawdown": outcome.max_drawdown,
                    }
                    for outcome in observation.outcomes
                ],
            }
            for observation in scan.observations
        ],
    }


def _validate_scoring(args: Namespace, scan: ForwardScan) -> None:
    """Reject a scoring axis the scan cannot answer on.

    `--score-horizon` and `--score-target` select the column the sweep, the
    grid, and the gate cross-tab are ranked on. A value outside what was
    actually measured renders empty cells rather than failing, so it is caught
    here instead.

    Raises:
        DdForwardCliError: The requested horizon or target was not measured.
    """
    if args.score_horizon not in scan.horizons:
        measured = ",".join(str(days) for days in scan.horizons)
        msg = (
            f"--score-horizon {args.score_horizon} は測定していません。"
            f"--horizons に含まれる値を指定してください（現在: {measured}）。"
        )
        raise DdForwardCliError(msg)
    if args.score_target not in scan.targets:
        measured = ", ".join(scan.targets)
        msg = (
            f"--score-target {args.score_target} は測定していません"
            f"（利用可能: {measured}）。"
        )
        raise DdForwardCliError(msg)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: replay the history, print the tables, optionally write JSON."""
    args = _parse_args(argv)
    settings = run_cli(lambda: load_settings(args.settings), _CONFIG_EXIT)

    thresholds = thresholds_from(settings.regime)
    horizons = run_cli(lambda: _horizons(args.horizons), _SCAN_EXIT)
    bars, start = run_cli(lambda: _read_bars(args, settings), _SCAN_EXIT)
    scan = run_cli(
        lambda: scan_forward(
            ForwardScanRequest(
                bars=bars,
                start=start,
                as_of=args.as_of,
                thresholds=thresholds,
                horizons=horizons,
            )
        ),
        _SCAN_EXIT,
    )
    if not scan.observations:
        msg = (
            f"{start.isoformat()}〜{args.as_of.isoformat()} に"
            "分類できる観測日がありません（履歴が短すぎます）。"
        )
        raise SystemExit(msg)
    run_cli(lambda: _validate_scoring(args, scan), _ARGUMENT_EXIT)

    sys.stdout.write(render_terminal(scan, thresholds, args))

    json_path: Path | None = args.json_path
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomically(json_path, build_payload(scan, thresholds))
        sys.stdout.write(f"\nJSON written to {json_path}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
