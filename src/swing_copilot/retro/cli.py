"""`copilot-retro`: the retrospective mechanism's command line (P8-30).

Two subcommands exist so far, matching the two halves of this phase:

* `collect` scans `reports/` for archived `analysis_result.json` documents and
  brings each run's verdicts into DuckDB.
* `evaluate` classifies the collected verdicts whose horizons have matured.

`prepare` / `export` / `ingest` belong to later phases and are deliberately
absent rather than stubbed (E30.1) -- a subcommand that parses but does
nothing is harder to notice than one that does not exist.

Like every other entry point here, this one only observes: it writes
observation tables and never rewrites configuration, code, or any
deterministic screening/sizing/ranking value (design §10).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from rich.console import Console

from swing_copilot.config import load_settings
from swing_copilot.exceptions import ConfigError
from swing_copilot.retro.collect import collect_verdicts
from swing_copilot.retro.evaluate import evaluate_verdicts
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore

logger = logging.getLogger(__name__)

#: `pipeline/daily.py`'s default `output_dir`: where run archives are written.
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_SETTINGS_PATH = "config/settings.yaml"

_CONSOLE_WIDTH = 200


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="copilot-retro",
        description=(
            "LLM verdict の正本化と当否評価（観測専用）。"
            "設定・コードの書き換えは一切行わない。"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser(
        "collect", help="reports/ を走査して verdict を DuckDB へ取り込む"
    )
    collect_parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    collect_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="満期を迎えた verdict の forward return を分類する"
    )
    evaluate_parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    evaluate_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    evaluate_parser.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)

    return parser.parse_args(argv)


def _run_collect(state_store: StateStore, reports_dir: Path, console: Console) -> None:
    summary = collect_verdicts(state_store, reports_dir)
    console.print(
        f"走査 {summary.scanned_run_count} run / "
        f"取り込み {summary.collected_run_count} run / "
        f"verdict {summary.verdict_count} 件 / "
        f"source {summary.source_count} 件"
    )
    _print_notes(console, summary.notes)


def _run_evaluate(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    try:
        settings = load_settings(args.settings)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    # Parquet bars live alongside the DuckDB file, mirroring the
    # DEFAULT_DB_PATH/DEFAULT_PARQUET_ROOT pairing -- `--db` moves both.
    market_store = MarketStore(
        state_store.database, parquet_root=Path(args.db).parent / "bars"
    )
    summary = evaluate_verdicts(
        market_store,
        state_store,
        args.as_of,
        settings.postmortem,
        settings.backtest.benchmark,
    )
    console.print(
        f"評価 {summary.evaluated_slice_count} slice / "
        f"未満期 {summary.pending_slice_count} slice / "
        f"outcome {summary.outcome_count} 件"
    )
    _print_notes(console, summary.notes)


def _print_notes(console: Console, notes: tuple[str, ...]) -> None:
    for note in notes:
        console.print(f"[yellow]{note}[/yellow]")


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: dispatch to `collect` or `evaluate`.

    Args:
        argv: Argument vector, defaulting to `sys.argv[1:]`.

    Raises:
        SystemExit: Argument parsing failed, or the settings file named by
            `--settings` is missing or invalid.
    """
    args = _parse_args(argv)
    state_store = StateStore(Database(args.db))
    state_store.init_schema()
    console = Console(file=sys.stdout, width=_CONSOLE_WIDTH)
    if args.command == "collect":
        _run_collect(state_store, args.reports_dir, console)
    else:
        _run_evaluate(state_store, args, console)


if __name__ == "__main__":  # pragma: no cover
    main()
