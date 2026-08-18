"""`copilot-ingest-analysis`: verify skill output and re-render the day's report.

This entry point is deliberately inert with respect to the deterministic
pipeline: it never opens a network connection and never re-runs screening,
risk, or ranking. It reads three local JSON files -- the exported analysis
input, the skill's answer, and the archived report context -- verifies the
answer against the input, and rewrites the same Markdown archive the daily run
produced, with only the qualitative sections filled in.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ANALYSIS_RESULT_FILENAME,
)
from swing_copilot.analysis.snapshot import (
    REPORT_CONTEXT_FILENAME,
    read_report_context,
)
from swing_copilot.analysis.validate import (
    AnalysisIngestError,
    ArtifactIdentity,
    ValidatedAnalysis,
    load_analysis_input,
    load_analysis_result,
    validate_analysis,
    validate_artifact_identity,
)
from swing_copilot.cli_support import ExitPolicy, run_cli
from swing_copilot.report.daily_brief import DailyBrief, build_analysis_brief
from swing_copilot.report.markdown_report import write_markdown_report
from swing_copilot.report.terminal_report import TerminalPaths, render_terminal

logger = logging.getLogger(__name__)

_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

#: This command reports through logging (its stderr already carries the log
#: stream), so the failure message is logged rather than written directly.
_EXIT_POLICY = ExitPolicy(
    errors=(AnalysisIngestError, OSError),
    code=1,
    format_message=lambda exc: f"analysis ingest failed: {exc}",
    report=logger.error,
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="copilot-ingest-analysis",
        description=(
            "Verify a swing-daily skill's analysis_result.json against the "
            "exported analysis_input.json and re-render the day's report. "
            "Performs no network access and no screening."
        ),
    )
    parser.add_argument(
        "result",
        type=Path,
        help=(f"Path to {ANALYSIS_RESULT_FILENAME} (or the directory containing it)."),
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help=(
            f"Path to {ANALYSIS_INPUT_FILENAME}. "
            "Defaults to the sibling of the result file."
        ),
    )
    parser.add_argument(
        "--context",
        type=Path,
        default=None,
        help=(
            f"Path to {REPORT_CONTEXT_FILENAME}. "
            "Defaults to the sibling of the result file."
        ),
    )
    parser.add_argument("--log-level", choices=tuple(_LOG_LEVELS), default=None)
    return parser.parse_args(argv)


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    """Return `(input, result, context)` paths, allowing a directory argument."""
    result: Path = args.result
    if result.is_dir():
        result = result / ANALYSIS_RESULT_FILENAME
    directory = result.parent
    analysis_input: Path = args.input or directory / ANALYSIS_INPUT_FILENAME
    context: Path = args.context or directory / REPORT_CONTEXT_FILENAME
    return analysis_input, result, context


def ingest(analysis_input_path: Path, result_path: Path, context_path: Path) -> Path:
    """Verify one analysis result and rewrite the corresponding report.

    Args:
        analysis_input_path: The `analysis_input.json` the skill was given.
        result_path: The skill-produced `analysis_result.json`.
        context_path: The `report_context.json` archived by the daily run.

    Returns:
        The path of the rewritten Markdown report.

    Raises:
        AnalysisIngestError: Any document is unreadable, malformed, or
            describes a different `as_of` than the input.
        OSError: The report could not be rewritten.
    """
    analysis_input = load_analysis_input(analysis_input_path)
    result = load_analysis_result(result_path)
    context = read_report_context(context_path)
    validate_artifact_identity(
        analysis_input,
        result,
        ArtifactIdentity(
            run_id=context.brief.run_id,
            as_of=context.brief.run_date,
            strategy_key=context.strategy_key,
            input_digest=context.input_digest,
        ),
    )
    validated = validate_analysis(analysis_input, result)
    brief = _rebuild_brief(context.brief, validated)
    report_path = write_markdown_report(brief, context.status, context.output_dir)
    width = shutil.get_terminal_size(fallback=(120, 24)).columns
    sys.stdout.write(
        render_terminal(
            brief,
            context.status,
            width=width,
            color=sys.stdout.isatty(),
            paths=TerminalPaths(report=report_path),
        )
    )
    return report_path


def _rebuild_brief(brief: DailyBrief, validated: ValidatedAnalysis) -> DailyBrief:
    """Return `brief` with only its qualitative sections replaced.

    Every deterministic field (scores, sizing, execution state, rejections,
    regime) is carried over untouched: an analysis may add narrative and a
    verdict, never edit the numbers.
    """
    candidates = tuple(
        replace(candidate, analysis=build_analysis_brief(candidate.symbol, validated))
        for candidate in brief.candidates
    )
    return replace(
        brief,
        candidates=candidates,
        no_trade=validated.no_trade,
        no_trade_reason=validated.no_trade_reason,
    )


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: verify, re-render, exit.

    Args:
        argv: Argument list, or `None` to use `sys.argv[1:]`.
    """
    args = _parse_args(argv)
    logging.basicConfig(
        level=_LOG_LEVELS[args.log_level] if args.log_level else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    analysis_input_path, result_path, context_path = _resolve_paths(args)
    run_cli(
        lambda: ingest(analysis_input_path, result_path, context_path), _EXIT_POLICY
    )
    raise SystemExit(0)
