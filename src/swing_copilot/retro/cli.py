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
  appends the surviving proposals to the ledger, and accumulates the verified
  narrations in DuckDB (Issue #189, so the L2 qualitative gate has something
  to count).

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

from swing_copilot.cli_support import (
    LOG_LEVELS,
    ExitPolicy,
    configure_cli_logging,
    run_cli,
)
from swing_copilot.clock import SystemClock
from swing_copilot.config import Secrets, Settings, load_secrets, load_settings
from swing_copilot.data.edgar import EdgarClient
from swing_copilot.exceptions import ConfigError
from swing_copilot.retro.collect import collect_verdicts
from swing_copilot.retro.evaluate import EvaluationRequest, evaluate_verdicts
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
from swing_copilot.storage.market_store import (
    MarketStore,
    ParquetRootNotFoundError,
    resolve_parquet_root,
)
from swing_copilot.storage.state_store import StateStore
from swing_copilot.text.news_finnhub import FinnhubNewsClient

logger = logging.getLogger(__name__)

#: `pipeline/daily.py`'s default `output_dir`: where run archives are written.
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_SETTINGS_PATH = "config/settings.yaml"

_CONSOLE_WIDTH = 200

#: Every failure this command converts follows the argparse convention: the
#: message itself is the exit status (printed to stderr, exit 1).
_CONFIG_EXIT = ExitPolicy(errors=(ConfigError,))
_INGEST_EXIT = ExitPolicy(errors=(RetroIngestError,))
_BARS_EXIT = ExitPolicy(errors=(ParquetRootNotFoundError,))

#: What a bars-root-less evaluation produced instead of failing (Issue #221).
_MISSING_BARS_CONSEQUENCE = (
    "このまま実行してもバー0件から forward return を計算することになり、"
    "満期スライスが1件も評価されないまま正常終了してしまう。"
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="copilot-retro",
        description=(
            "LLM verdict の正本化と当否評価（観測専用）。"
            "設定・コードの書き換えは一切行わない。"
        ),
    )
    parser.add_argument("--log-level", choices=tuple(LOG_LEVELS), default=None)
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
    ingest_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

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
        f"解析 {summary.parsed_run_count} run / "
        f"無変更 {summary.unchanged_run_count} run / "
        f"解析不能 {summary.unreadable_run_count} run / "
        f"取り込み {summary.collected_run_count} run / "
        f"verdict {summary.verdict_count} 件 / "
        f"source {summary.source_count} 件 / "
        f"coverage {summary.coverage_count} 件"
    )
    _print_notes(console, summary.notes)
    _emit_unreadable_tag(summary.unreadable_run_count)


def _emit_unreadable_tag(unreadable_run_count: int) -> None:
    """Write the machine-readable `COLLECT_UNREADABLE[<count>]:` stderr line.

    Issue #374: `collect_verdicts` is intentionally fail-soft per run -- one
    corrupt or unparsable `analysis_input.json`/`analysis_result.json` must
    not stop the rest of the scan from being collected, and the exit code
    stays 0 either way (CI's `push` step gates on `success()`, so failing
    `collect` would take that day's price/fundamental sync down with it). But
    a silently skipped run previously showed up only as a missing verdict
    months later. This line makes the skip itself visible in the same
    grep-for-a-tag idiom `daily_composition.py`'s `PREFLIGHT_ABORT[<reason>]:`
    and `daily_runner.py`'s `ANALYSIS_GAP[<reason>]:` already use, without
    changing what the command returns. Emitted only when at least one run was
    unreadable; a fully successful scan writes nothing here.

    Issue #376: the note this line points to now actually names which
    document (`analysis_input.json`/`analysis_result.json`) and, for a schema
    failure, which field(s) were rejected (`collect._describe_load_failure`)
    -- previously the note read only "解析文書を読めなかったためスキップ",
    so this guidance pointed at a note with nothing in it. The guidance line
    and the note are both written via `_print_notes`/`console.print`, i.e.
    stdout -- the same stream `logger.exception`'s traceback does *not* use
    (this CLI configures no logging handler, so that traceback falls through
    to `logging.lastResort` on stderr instead).

    Args:
        unreadable_run_count: `CollectSummary.unreadable_run_count` from the
            scan just run.
    """
    if unreadable_run_count <= 0:
        return
    sys.stderr.write(
        f"COLLECT_UNREADABLE[{unreadable_run_count}]: "
        f"{unreadable_run_count} 件の run ディレクトリを解析できず取り込みを"
        "スキップした（対象文書とフィールドは標準出力の note を参照）。"
        "終了コードは変えない。\n"
    )


def _load_settings(path: str) -> Settings:
    return run_cli(lambda: load_settings(path), _CONFIG_EXIT)


def _market_store(state_store: StateStore, db_path: Path) -> MarketStore:
    """Bars source for `--db`.

    Parquet bars live alongside the DuckDB file, mirroring the
    DEFAULT_DB_PATH/DEFAULT_PARQUET_ROOT pairing -- `--db` moves both. The
    root's absence is fatal here (Issue #221): `evaluate` would compute every
    forward return from zero bars, mature nothing, and report "評価 0 slice"
    as though the window simply held no matured verdict.
    """
    parquet_root = run_cli(
        lambda: resolve_parquet_root(db_path, consequence=_MISSING_BARS_CONSEQUENCE),
        _BARS_EXIT,
    )
    return MarketStore(state_store.database, parquet_root=parquet_root)


def _run_evaluate(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    settings = _load_settings(args.settings)
    market_store = _market_store(state_store, args.db)
    # `only_pending` stays off here: the manual batch is where a price
    # correction is meant to reach `verdict_outcomes`, so it re-classifies
    # every matured slice in the window (Issue #209).
    summary = evaluate_verdicts(
        market_store,
        state_store,
        EvaluationRequest(
            as_of=args.as_of,
            thresholds=settings.postmortem,
            benchmark_symbol=settings.backtest.benchmark,
        ),
    )
    console.print(
        f"評価 {summary.evaluated_slice_count} slice / "
        f"未満期 {summary.pending_slice_count} slice / "
        f"outcome {summary.outcome_count} 件"
    )
    _print_notes(console, summary.notes)


def _run_export(
    state_store: StateStore,
    args: argparse.Namespace,
    console: Console,
    secrets: Secrets,
) -> None:
    settings = _load_settings(args.settings)
    # `export` reads the proposal ledger to name the closed proposals a
    # re-proposal must justify. An unreadable ledger is an operator-facing
    # message here, exactly as it is in `ingest`.
    summary = run_cli(
        lambda: export_retro_input(
            RetroExportDependencies(
                market_store=_market_store(state_store, args.db),
                state_store=state_store,
                settings=settings,
                clock=SystemClock(),
                freshness=_freshness_sources(secrets),
            ),
            RetroExportRequest(
                as_of=args.as_of, reports_root=args.reports_dir, ledger_path=args.ledger
            ),
        ),
        _INGEST_EXIT,
    )
    console.print(
        f"評価 {summary.outcome_count} 行 / "
        f"サプライズ {summary.surprise_count} 件（上限超過 "
        f"{summary.dropped_surprise_count} 件）→ {summary.path}"
    )
    _print_notes(console, summary.notes)


def _freshness_sources(secrets: Secrets) -> FreshnessSources:
    """Build the freshness adapters the available API keys allow.

    A missing key yields no client, and the dossier then carries no freshness
    for that side rather than failing: the retrospective's core evidence is
    already in the database (design §5.3, E31.3).

    Args:
        secrets: Loaded once by `main()`, not reloaded here (Issue #381) --
            this is also the boundary that makes `logger.exception` on a
            failed `FinnhubNewsClient`/`EdgarClient` call safe to configure
            redaction for, since `main()` configures logging from the same
            `Secrets` value before any subcommand runs.
    """
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
    state_store: StateStore,
    args: argparse.Namespace,
    console: Console,
    secrets: Secrets,
) -> None:
    """Run the whole chain, which is what the skill's preflight calls (E31.4)."""
    _run_collect(state_store, args.reports_dir, console)
    _run_evaluate(state_store, args, console)
    _run_export(state_store, args, console, secrets)


def _run_ingest(
    state_store: StateStore, args: argparse.Namespace, console: Console
) -> None:
    """Verify the skill's answer, record it, and accumulate its narrations."""
    summary = run_cli(
        lambda: ingest_retro_result(
            RetroIngestRequest(
                retro_dir=args.retro_dir,
                ledger_path=args.ledger,
                state_store=state_store,
            )
        ),
        _INGEST_EXIT,
    )
    accumulated = f"へ蓄積（{args.db}）" if summary.are_narrations_persisted else ""
    console.print(
        f"提案 {len(summary.recorded)} 件を台帳（{summary.ledger_path}）へ記録 / "
        f"非表示 {len(summary.withheld)} 件 / "
        f"叙述 {summary.narration_count} 件{accumulated} → {summary.report_path}"
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
    """Print each note in yellow without interpreting it as Rich markup.

    A note can carry arbitrary exception text -- including an
    operator-supplied `--reports-dir` path via `OSError`, or (Issue #376) a
    document's own failure message -- so an embedded closing tag such as
    `[/]` must not raise `rich.errors.MarkupError` and turn a deliberately
    fail-soft command into a hard crash. `markup=False` disables tag parsing
    while `style="yellow"` still applies the colour.
    """
    for note in notes:
        console.print(note, style="yellow", markup=False)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: dispatch to one of the five subcommands.

    Args:
        argv: Argument vector, defaulting to `sys.argv[1:]`.

    Raises:
        SystemExit: Argument parsing failed, the settings file named by
            `--settings` is missing or invalid, the proposal ledger exists but
            cannot be read, or `ingest` was given documents it cannot trust.
    """
    args = _parse_args(argv)
    # Configured before anything else touches the database or the network:
    # `collect`'s `logger.exception` per unreadable archive, and `export`'s
    # authenticated Finnhub/EDGAR calls, must never fall through to
    # `logging.lastResort` (unformatted, uncontrollable, and -- for
    # `export`/`prepare` -- unredacted). `secrets` is loaded exactly once here
    # and threaded into `_run_export`/`_run_prepare` rather than reloaded.
    secrets = load_secrets()
    configure_cli_logging(secrets, level=args.log_level)
    console = Console(file=sys.stdout, width=_CONSOLE_WIDTH)
    state_store = StateStore(Database(args.db))
    state_store.init_schema()
    if args.command == "collect":
        _run_collect(state_store, args.reports_dir, console)
    elif args.command == "evaluate":
        _run_evaluate(state_store, args, console)
    elif args.command == "export":
        _run_export(state_store, args, console, secrets)
    elif args.command == "ingest":
        _run_ingest(state_store, args, console)
    else:
        _run_prepare(state_store, args, console, secrets)


if __name__ == "__main__":  # pragma: no cover
    main()
