"""`copilot-retro`: the retrospective mechanism's command line (P8-30..P8-32).

Five subcommands, in the order one retrospective uses them:

* `collect` scans `reports/` for archived `analysis_result.json` documents and
  brings each run's verdicts into DuckDB.
* `evaluate` classifies the collected verdicts whose horizons have matured.
* `export` aggregates the matured window into `retro_input.json`, the dossier
  the `swing-retro` skill reads.
* `prepare` runs those three in order -- the one command the skill's preflight
  invokes (E31.4).
* `ingest` verifies the skill's `retro_result.json`, renders `retro_report.md`,
  and appends the surviving proposals to the ledger. It is the only subcommand
  that needs no database at all.

Like every other entry point here, this one only observes: it writes
observation tables, a report, and a ledger entry, and never rewrites
configuration, code, or any deterministic screening/sizing/ranking value
(design §10).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from rich.console import Console

from swing_copilot.clock import SystemClock
from swing_copilot.config import Settings, load_secrets, load_settings
from swing_copilot.data.edgar import EdgarClient
from swing_copilot.exceptions import ConfigError
from swing_copilot.retro.collect import collect_verdicts
from swing_copilot.retro.evaluate import evaluate_verdicts
from swing_copilot.retro.export import (
    DEFAULT_LEDGER_PATH,
    RetroExportDependencies,
    RetroExportRequest,
    export_retro_input,
)
from swing_copilot.retro.ingest import RetroIngestRequest, ingest_retro_result
from swing_copilot.retro.surprises import FreshnessSources
from swing_copilot.retro.validate import RetroIngestError
from swing_copilot.storage.database import DEFAULT_DB_PATH, Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.text.news_finnhub import FinnhubNewsClient

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

    export_parser = subparsers.add_parser(
        "export", help="集約と証拠一式を retro_input.json へ書き出す"
    )
    _add_export_arguments(export_parser)

    prepare_parser = subparsers.add_parser(
        "prepare", help="collect → evaluate → export をまとめて実行する"
    )
    _add_export_arguments(prepare_parser)

    ingest_parser = subparsers.add_parser(
        "ingest", help="retro_result.json を検証しレポートと提案台帳へ反映する"
    )
    ingest_parser.add_argument(
        "retro_dir",
        type=Path,
        help="retro_input.json と retro_result.json を置いた reports/retro/<as_of>/",
    )
    ingest_parser.add_argument("--ledger", type=Path, default=Path(DEFAULT_LEDGER_PATH))

    return parser.parse_args(argv)


def _add_export_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the arguments `export` and its `prepare` umbrella share."""
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)
    parser.add_argument("--ledger", type=Path, default=Path(DEFAULT_LEDGER_PATH))


def _run_collect(state_store: StateStore, reports_dir: Path, console: Console) -> None:
    summary = collect_verdicts(state_store, reports_dir)
    console.print(
        f"走査 {summary.scanned_run_count} run / "
        f"取り込み {summary.collected_run_count} run / "
        f"verdict {summary.verdict_count} 件 / "
        f"source {summary.source_count} 件 / "
        f"coverage {summary.coverage_count} 件"
    )
    _print_notes(console, summary.notes)


def _load_settings(path: str) -> Settings:
    try:
        return load_settings(path)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc


def _market_store(state_store: StateStore, db_path: Path) -> MarketStore:
    """Bars source for `--db`.

    Parquet bars live alongside the DuckDB file, mirroring the
    DEFAULT_DB_PATH/DEFAULT_PARQUET_ROOT pairing -- `--db` moves both.
    """
    return MarketStore(state_store.database, parquet_root=Path(db_path).parent / "bars")


def _run_evaluate(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    settings = _load_settings(args.settings)
    market_store = _market_store(state_store, args.db)
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


def _run_export(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    settings = _load_settings(args.settings)
    summary = export_retro_input(
        RetroExportDependencies(
            market_store=_market_store(state_store, args.db),
            state_store=state_store,
            settings=settings,
            clock=SystemClock(),
            freshness=_freshness_sources(),
        ),
        RetroExportRequest(
            as_of=args.as_of, reports_root=args.reports_dir, ledger_path=args.ledger
        ),
    )
    console.print(
        f"評価 {summary.outcome_count} 行 / "
        f"サプライズ {summary.surprise_count} 件（上限超過 "
        f"{summary.dropped_surprise_count} 件）→ {summary.path}"
    )
    _print_notes(console, summary.notes)


def _freshness_sources() -> FreshnessSources:
    """Build the freshness adapters the available API keys allow.

    A missing key yields no client, and the dossier then carries no freshness
    for that side rather than failing: the retrospective's core evidence is
    already in the database (design §5.3, E31.3).
    """
    secrets = load_secrets()
    return FreshnessSources(
        news_client=(
            FinnhubNewsClient(secrets.finnhub_api_key)
            if secrets.finnhub_api_key
            else None
        ),
        edgar_client=(
            EdgarClient(secrets.edgar_identity) if secrets.edgar_identity else None
        ),
    )


def _run_prepare(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    """Run the whole chain, which is what the skill's preflight calls (E31.4)."""
    _run_collect(state_store, args.reports_dir, console)
    _run_evaluate(state_store, args, console)
    _run_export(state_store, args, console)


def _run_ingest(args: argparse.Namespace, console: Console) -> None:
    """Verify the skill's answer and record it (no database is touched)."""
    try:
        summary = ingest_retro_result(
            RetroIngestRequest(retro_dir=args.retro_dir, ledger_path=args.ledger)
        )
    except RetroIngestError as exc:
        raise SystemExit(str(exc)) from exc
    console.print(
        f"提案 {len(summary.recorded)} 件を台帳（{summary.ledger_path}）へ記録 / "
        f"非表示 {len(summary.withheld)} 件 / "
        f"叙述 {summary.narration_count} 件 → {summary.report_path}"
    )
    for item in summary.recorded:
        console.print(f"  {item.rp_id} [{item.proposal.level}] {item.proposal.title}")
    _print_notes(
        console,
        tuple(
            f"非表示 {item.kind} {item.identifier or ''}: {item.reason}"
            for item in summary.withheld
        ),
    )


def _print_notes(console: Console, notes: tuple[str, ...]) -> None:
    for note in notes:
        console.print(f"[yellow]{note}[/yellow]")


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: dispatch to one of the five subcommands.

    Args:
        argv: Argument vector, defaulting to `sys.argv[1:]`.

    Raises:
        SystemExit: Argument parsing failed, the settings file named by
            `--settings` is missing or invalid, or `ingest` was given documents
            it cannot trust.
    """
    args = _parse_args(argv)
    console = Console(file=sys.stdout, width=_CONSOLE_WIDTH)
    if args.command == "ingest":
        # The only subcommand with no database side: two files in, two out.
        _run_ingest(args, console)
        return
    state_store = StateStore(Database(args.db))
    state_store.init_schema()
    if args.command == "collect":
        _run_collect(state_store, args.reports_dir, console)
    elif args.command == "evaluate":
        _run_evaluate(state_store, args, console)
    elif args.command == "export":
        _run_export(state_store, args, console)
    else:
        _run_prepare(state_store, args, console)


if __name__ == "__main__":  # pragma: no cover
    main()
