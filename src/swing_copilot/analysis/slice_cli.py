"""`copilot-export-slices`: cut the skill's per-expert input slices (#260).

Kept out of `copilot-daily` on purpose. The daily pipeline's job ends when it
has exported `analysis_input.json`; which experts run, and over which symbols,
is the orchestrating skill's decision and can change without a pipeline
release. This command is therefore read-mostly and cheap to re-run: it opens no
network connection, touches no database, re-runs no screening, and writes
nothing but the slices in the directory it was given.

`--out-dir` is required rather than defaulted beside the input, because the
CI-only `swing-daily` workflow puts slices in the repository's ignored
`.swing-daily-scratch/` sibling, never in `<WORKDIR>`: a slice written into the
run directory would sit next to the fragments and could be merged as one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swing_copilot.analysis.export import ANALYSIS_INPUT_FILENAME
from swing_copilot.analysis.slices import (
    SliceExportError,
    build_slices,
    write_slices,
)
from swing_copilot.analysis.validate import (
    AnalysisIngestError,
    read_analysis_document,
)
from swing_copilot.cli_support import ExitPolicy, run_cli

if TYPE_CHECKING:
    from swing_copilot.analysis.slices import SliceDocument

#: One non-zero code: every failure here means "no slices were produced".
_EXIT_POLICY = ExitPolicy(
    errors=(AnalysisIngestError, SliceExportError, OSError),
    code=1,
    format_message=lambda exc: f"slice export failed: {exc}",
)


def export_slices(input_path: Path, out_dir: Path) -> list[tuple[SliceDocument, Path]]:
    """Cut every slice out of one analysis input and write it to `out_dir`.

    Args:
        input_path: `analysis_input.json`, or the directory holding it.
        out_dir: Where the slices are written; created when absent.

    Returns:
        Each slice paired with the absolute path it was written to, ordered by
        expert and then by the input's own candidate order.

    Raises:
        AnalysisIngestError: The input document is missing or is not JSON.
        SliceExportError: The document is not a valid `analysis_input.json`, a
            slice violates its own strict schema, or `out_dir` would put the
            slices in a run directory.
        OSError: The slices could not be written.
    """
    resolved = (
        input_path / ANALYSIS_INPUT_FILENAME if input_path.is_dir() else input_path
    )
    _verify_out_dir(resolved, out_dir)
    payload = _read_input(resolved)
    documents = build_slices(payload)
    written = write_slices(documents, out_dir)
    return list(zip(documents, written, strict=True))


def _verify_out_dir(input_path: Path, out_dir: Path) -> None:
    """Refuse a destination that is (or contains) operator-owned output.

    Requiring `--out-dir` states where slices belong; it does not stop the
    caller from naming the run directory it just read the input from. That
    would drop `slice-*.json` into `reports/<date>/<run-id>/`, which the
    workflow is forbidden to clean up, so the mess would accumulate one run at
    a time. The `slice-` prefix keeps such a file from ever being merged as a
    fragment, so this guard is about the operator's tree, not correctness.

    Three shapes are refused, all of them decidable from the paths alone: the
    run directory itself, anywhere beneath it, and any directory that already
    holds an `analysis_input.json` (another run's). A directory *above* the run
    -- `reports/`, or the repository root -- is refused for the same reason: it
    is the tree the run directory lives in.

    Deliberately no "is this inside a git checkout?" test: an installed wheel
    cannot tell the operator's repository from any other checkout, and a false
    refusal would cost a whole unattended day. The shapes above cover the
    destination a caller actually reaches for by mistake -- the path it was
    just given.

    Args:
        input_path: The resolved `analysis_input.json` being sliced.
        out_dir: The requested destination, which need not exist yet.

    Raises:
        SliceExportError: The destination is one of the refused shapes.
    """
    destination = out_dir.resolve()
    run_dir = input_path.resolve().parent
    if destination == run_dir or run_dir in destination.parents:
        msg = (
            f"--out-dir {out_dir} is inside the run directory {run_dir}; "
            "slices are CI scratch and must not be written to the "
            "operator's report output"
        )
        raise SliceExportError(msg)
    if destination in run_dir.parents:
        msg = (
            f"--out-dir {out_dir} contains the run directory {run_dir}; "
            "write slices to the repository's CI scratch directory instead"
        )
        raise SliceExportError(msg)
    if (destination / ANALYSIS_INPUT_FILENAME).is_file():
        msg = (
            f"--out-dir {out_dir} is a run directory of its own (it holds "
            f"{ANALYSIS_INPUT_FILENAME}); write slices to the session "
            "CI scratch directory instead"
        )
        raise SliceExportError(msg)


def _read_input(
    path: Path,
) -> dict[str, Any]:  # Any: raw JSON object from the document reader
    """Return the document exactly as written, for verbatim copying.

    The parsed JSON rather than an `AnalysisInput`: re-serializing a model
    would rewrite datetimes and key order, and the slices have to reproduce the
    input's own bytes for `source_id`s and bodies.
    """
    payload = read_analysis_document(path)
    if not isinstance(payload, dict):
        msg = f"Analysis document is not a JSON object: {path}"
        raise AnalysisIngestError(msg)
    # Any: a verbatim JSON document; `build_slices` proves its shape.
    return payload


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="copilot-export-slices",
        description=(
            "Cut the swing-daily skill's per-expert input slices out of an "
            "exported analysis_input.json, deterministically and verbatim. "
            "Reads only: performs no network access, no screening, and writes "
            "nothing outside --out-dir."
        ),
    )
    parser.add_argument(
        "input",
        type=Path,
        help=(f"Path to {ANALYSIS_INPUT_FILENAME} (or the directory containing it)."),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help=(
            "Directory to write the slices into, created when absent. The "
            "swing-daily workflow uses the ignored repository sibling "
            ".swing-daily-scratch/slices; a destination inside, above, or equal "
            "to a run directory is refused."
        ),
    )
    return parser.parse_args(argv)


def _render(exported: list[tuple[SliceDocument, Path]]) -> str:
    """Render one tab-separated line per slice, then a per-expert count.

    Tab-separated so the orchestrator can assign symbols to subagents straight
    from this output: `source_chars` is the body text the slice carries, which
    is what SKILL.md Step 2's per-agent ceiling is stated over.
    """
    lines = [
        f"{path}\t{document.kind}\t{document.symbol}\t{document.source_chars}"
        for document, path in exported
    ]
    counts = {
        kind: sum(1 for document, _ in exported if document.kind == kind)
        for kind in ("news", "filings", "screening")
    }
    summary = " ".join(f"{kind}={count}" for kind, count in counts.items())
    lines.append(f"{len(exported)} slice(s) written: {summary}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: cut, write, list what was written, exit `0`.

    Args:
        argv: Argument list, or `None` to use `sys.argv[1:]`.
    """
    args = _parse_args(argv)
    exported = run_cli(lambda: export_slices(args.input, args.out_dir), _EXIT_POLICY)
    sys.stdout.write(_render(exported))
    raise SystemExit(0)
