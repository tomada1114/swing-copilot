"""`copilot-export-slices`: cut the skill's per-expert input slices (#260).

Kept out of `copilot-daily` on purpose. The daily pipeline's job ends when it
has exported `analysis_input.json`; which experts run, and over which symbols,
is the orchestrating skill's decision and can change without a pipeline
release. This command is therefore read-mostly and cheap to re-run: it opens no
network connection, touches no database, re-runs no screening, and writes
nothing but the slices in the directory it was given.

`--out-dir` is required rather than defaulted beside the input, because
SKILL.md ("一時ファイルと後始末") puts slices in the session scratchpad and
nowhere near `<WORKDIR>`: a slice written into the run directory would sit
next to the fragments and could be merged as one.
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
        SliceExportError: The document is not a valid `analysis_input.json`, or
            a slice violates its own strict schema.
        OSError: A slice could not be written.
    """
    resolved = (
        input_path / ANALYSIS_INPUT_FILENAME if input_path.is_dir() else input_path
    )
    payload = _read_input(resolved)
    documents = build_slices(payload)
    written = write_slices(documents, out_dir)
    return list(zip(documents, written, strict=True))


def _read_input(path: Path) -> dict[str, Any]:
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
            "Directory to write the slices into, created when absent. Use the "
            "session scratchpad: slices are working files and must not sit in "
            "the run directory or the repository."
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
