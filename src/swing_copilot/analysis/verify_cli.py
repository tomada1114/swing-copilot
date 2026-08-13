"""`copilot-verify-analysis`: the shared pre-flight check for skill artifacts.

Kept out of `analysis/cli.py` on purpose. That entry point exists to *rewrite
the day's report*; this one only reads and reports, so a skill can run it as
often as it likes, on a fragment that has not been merged yet, without any risk
of touching `reports/`.

It answers one question for two kinds of document:

* an `analysis_work/<kind>-<SYMBOL>.json` fragment, through
  `analysis/fragment.py`;
* a merged `analysis_result.json`, as a dry run of `copilot-ingest-analysis` --
  the same strict schema, the same run-identity comparison against the input,
  and the same per-symbol verification, stopping short of `report_context.json`
  and of writing anything.

Both documents are told apart by `schema_version`, which the result schema
requires and the fragment schema forbids, and each is then parsed strictly
under its own schema -- so neither can be checked as the other. Like
`copilot-ingest-analysis`, this command opens no network connection, touches no
database, and re-runs no screening.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
)
from swing_copilot.analysis.fragment import (
    AnalysisFragment,
    fragment_filename_error,
    verify_fragment,
)
from swing_copilot.analysis.snapshot import REPORT_CONTEXT_FILENAME
from swing_copilot.analysis.validate import (
    AnalysisIngestError,
    ArtifactIdentity,
    load_analysis_input,
    load_analysis_result,
    read_analysis_document,
    validate_analysis,
    validate_artifact_identity,
)
from swing_copilot.report.rejections import REJECTIONS_FILENAME

if TYPE_CHECKING:
    from swing_copilot.analysis.schemas import AnalysisInput

logger = logging.getLogger(__name__)

#: Code-owned documents that share a run directory with the checkable ones. A
#: directory argument skips them rather than failing on them.
_CODE_OWNED_FILENAMES = frozenset(
    {ANALYSIS_INPUT_FILENAME, REPORT_CONTEXT_FILENAME, REJECTIONS_FILENAME}
)


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """One checked document's verdict.

    `errors` is empty exactly when the document would survive ingest. A result
    document can report one error per withheld symbol, so this is a list rather
    than the single reason a fragment yields.
    """

    path: Path
    label: str
    errors: tuple[str, ...]

    @property
    def is_ok(self) -> bool:
        """Whether nothing about this document would be withheld or rejected."""
        return not self.errors


def verify_document(analysis_input: AnalysisInput, path: Path) -> VerificationReport:
    """Check one fragment or one `analysis_result.json` against its input.

    Args:
        analysis_input: The exported input the document claims to answer.
        path: The document to check.

    Returns:
        The document's report; unreadable and malformed documents come back as
        a failing report rather than an exception, so one bad file in a
        directory does not hide the verdicts of its siblings.
    """
    try:
        payload = _read_json(path)
        if "schema_version" in payload:
            return _verify_result(analysis_input, path)
        return _verify_fragment_file(analysis_input, path, payload)
    except AnalysisIngestError as exc:
        return VerificationReport(path, "rejected", (str(exc),))


def _read_json(path: Path) -> dict[str, object]:
    """Return the document's top-level object, for dispatch only.

    Reading and JSON-parsing go through the ingest reader, so an unreadable or
    wrongly encoded file is judged here exactly as `copilot-ingest-analysis`
    would judge it. Both failures stay `AnalysisIngestError` because neither
    may end this command's whole run: `verify_document` turns them into one
    failing report, and the sibling documents still have verdicts worth
    reporting.
    """
    payload = read_analysis_document(path)
    if not isinstance(payload, dict):
        msg = f"Analysis document is not a JSON object: {path}"
        raise AnalysisIngestError(msg)
    return payload


def _verify_fragment_file(
    analysis_input: AnalysisInput, path: Path, payload: dict[str, object]
) -> VerificationReport:
    try:
        fragment = AnalysisFragment.model_validate(payload)
    except ValueError as exc:
        msg = f"Fragment failed schema validation: {path}\n{exc}"
        raise AnalysisIngestError(msg) from exc
    label = f"fragment {fragment.kind}/{fragment.symbol}"
    error = fragment_filename_error(path, fragment) or verify_fragment(
        analysis_input, fragment
    )
    return VerificationReport(path, label, () if error is None else (error,))


def _verify_result(analysis_input: AnalysisInput, path: Path) -> VerificationReport:
    """Dry-run the ingest checks over a merged result, writing nothing.

    `report_context.json` is deliberately not consulted: it is written by
    `copilot-daily` from the same run, so the identity comparison that can
    actually fail here is result-against-input. Passing the input's own values
    as the context half keeps that comparison running through the production
    checker instead of a second copy of it.
    """
    result = load_analysis_result(path)
    validate_artifact_identity(
        analysis_input,
        result,
        ArtifactIdentity(
            run_id=analysis_input.run_id,
            as_of=analysis_input.as_of,
            strategy_key=analysis_input.strategy_key,
            input_digest=analysis_input.input_digest,
        ),
    )
    validated = validate_analysis(analysis_input, result)
    errors = tuple(
        f"{outcome.symbol}: {outcome.error}"
        for outcome in validated.outcomes
        if outcome.error is not None
    )
    return VerificationReport(path, "result", errors)


def expand_targets(paths: list[Path]) -> list[Path]:
    """Return the documents to check, expanding every directory argument.

    A directory contributes its own `*.json` files in sorted order, minus the
    code-owned documents a run directory also holds. That makes
    `<WORKDIR>/analysis_work` mean "every fragment" and `<WORKDIR>` mean "the
    merged result".

    Args:
        paths: Files and directories named on the command line.

    Returns:
        Existing file paths, de-duplicated in first-seen order.

    Raises:
        AnalysisIngestError: A named path does not exist.
    """
    targets: list[Path] = []
    for path in paths:
        if path.is_dir():
            targets.extend(
                child
                for child in sorted(path.glob("*.json"))
                if child.name not in _CODE_OWNED_FILENAMES
            )
        elif path.exists():
            targets.append(path)
        else:
            msg = f"Analysis document could not be read: {path}"
            raise AnalysisIngestError(msg)
    return list(dict.fromkeys(targets))


def resolve_input_path(target: Path) -> Path:
    """Find the `analysis_input.json` a target answers to.

    Looks in the target's own directory and then its parent, which covers both
    `<WORKDIR>/analysis_result.json` and `<WORKDIR>/analysis_work/*.json`.

    Args:
        target: A document to check, as returned by `expand_targets`.

    Returns:
        The located input path.

    Raises:
        AnalysisIngestError: Neither directory holds an `analysis_input.json`.
    """
    for candidate_dir in (target.parent, target.parent.parent):
        candidate = candidate_dir / ANALYSIS_INPUT_FILENAME
        if candidate.is_file():
            return candidate
    msg = (
        f"No {ANALYSIS_INPUT_FILENAME} beside {target} or in its parent "
        "directory; pass --input explicitly"
    )
    raise AnalysisIngestError(msg)


def verify_paths(
    paths: list[Path], input_path: Path | None
) -> list[VerificationReport]:
    """Check every named document, resolving each one's input as needed.

    Args:
        paths: Files and directories to check.
        input_path: An explicit `analysis_input.json`, or `None` to locate one
            per target.

    Returns:
        One report per checked document, in target order.

    Raises:
        AnalysisIngestError: A path does not exist, or an input could not be
            located or parsed. These are usage failures rather than contract
            violations, so they stop the run instead of becoming a report.
    """
    targets = expand_targets(paths)
    inputs: dict[Path, AnalysisInput] = {}
    reports: list[VerificationReport] = []
    for target in targets:
        resolved = input_path or resolve_input_path(target)
        if resolved not in inputs:
            inputs[resolved] = load_analysis_input(resolved)
        reports.append(verify_document(inputs[resolved], target))
    return reports


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="copilot-verify-analysis",
        description=(
            "Check an analysis_work fragment or a merged analysis_result.json "
            "against the exported analysis_input.json, using the same schema, "
            "provenance, evidence-quote and CON-03 checks that "
            "copilot-ingest-analysis applies. Reads only: performs no network "
            "access, no screening, and writes no report."
        ),
    )
    parser.add_argument(
        "paths",
        type=Path,
        nargs="+",
        help=(
            "Documents to check. A directory expands to its *.json files, so "
            "<WORKDIR>/analysis_work checks every fragment."
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help=(
            f"Path to {ANALYSIS_INPUT_FILENAME}. Defaults to the copy beside "
            "each target, or in its parent directory."
        ),
    )
    return parser.parse_args(argv)


def _render(reports: list[VerificationReport]) -> str:
    lines: list[str] = []
    for report in reports:
        if report.is_ok:
            lines.append(f"PASS {report.path} ({report.label})")
            continue
        lines.extend(
            f"FAIL {report.path} ({report.label}): {error}" for error in report.errors
        )
    failed = sum(1 for report in reports if not report.is_ok)
    lines.append(
        f"{len(reports)} document(s) checked, "
        f"{len(reports) - failed} passed, {failed} failed"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: check, report, exit `0` when everything passes.

    Args:
        argv: Argument list, or `None` to use `sys.argv[1:]`.
    """
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    try:
        reports = verify_paths(args.paths, args.input)
    except AnalysisIngestError as exc:
        logger.error("verification could not run: %s", exc)  # noqa: TRY400 - user-facing
        raise SystemExit(2) from exc
    sys.stdout.write(_render(reports))
    raise SystemExit(0 if all(report.is_ok for report in reports) else 1)
