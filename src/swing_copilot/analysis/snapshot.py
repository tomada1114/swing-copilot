"""Persist and reload one run's rendered-report context (`report_context.json`).

`copilot-ingest-analysis` must re-render exactly the report `copilot-daily`
produced, with only the qualitative sections replaced. Rather than re-running
screening (which would not be point-in-time reproducible and would touch the
network), the daily run archives its presentation-neutral `DailyBrief` beside
`analysis_input.json`, and ingest reloads it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from swing_copilot.analysis.export import write_json_atomically
from swing_copilot.analysis.validate import AnalysisIngestError
from swing_copilot.models import RunStatus
from swing_copilot.report.daily_brief import DailyBrief

if TYPE_CHECKING:
    from typing import Any

REPORT_CONTEXT_FILENAME = "report_context.json"
CONTEXT_SCHEMA_VERSION = "report-context-v1"

# Pydantic evaluates a dataclass's annotation strings against its *defining*
# module's globals, so `report/daily_brief.py` imports `date`/`datetime`/`UUID`
# at runtime for this adapter's benefit.
_BRIEF_ADAPTER: TypeAdapter[DailyBrief] = TypeAdapter(DailyBrief)


@dataclass(frozen=True, slots=True)
class ReportContext:
    """The archived state needed to re-render one run's reports."""

    brief: DailyBrief
    status: RunStatus
    output_dir: Path


def write_report_context(context: ReportContext, destination_dir: Path) -> Path:
    """Archive `context` as `report_context.json` via atomic replacement.

    Args:
        context: The run's brief, status, and report output root.
        destination_dir: The run's dated report directory (the same place
            `analysis_input.json` is written to).

    Returns:
        The written file's path.

    Raises:
        OSError: Writing or replacing failed; the previous file is preserved.
    """
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / REPORT_CONTEXT_FILENAME
    write_json_atomically(
        destination,
        {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "status": context.status.value,
            "output_dir": str(context.output_dir),
            "brief": _BRIEF_ADAPTER.dump_python(context.brief, mode="json"),
        },
    )
    return destination


def read_report_context(path: Path) -> ReportContext:
    """Reload an archived report context.

    Args:
        path: Path to `report_context.json`.

    Returns:
        The reconstructed brief, run status, and output directory.

    Raises:
        AnalysisIngestError: The file is missing, malformed, written by an
            incompatible version, or carries an unknown run status.
    """
    payload = _read_payload(path)
    version = payload.get("schema_version")
    if version != CONTEXT_SCHEMA_VERSION:
        msg = (
            f"Unsupported report context schema_version {version!r} in {path} "
            f"(expected {CONTEXT_SCHEMA_VERSION!r})"
        )
        raise AnalysisIngestError(msg)
    try:
        status = RunStatus(payload["status"])
        brief = _BRIEF_ADAPTER.validate_python(payload["brief"])
        # Required, not defaulted: silently falling back to the process CWD
        # would rewrite the report somewhere other than the run's archive.
        output_dir = Path(str(payload["output_dir"]))
    except (KeyError, ValueError, ValidationError) as exc:
        msg = f"Report context failed validation: {path}\n{exc}"
        raise AnalysisIngestError(msg) from exc
    return ReportContext(brief=brief, status=status, output_dir=output_dir)


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        msg = f"Report context could not be read: {path}"
        raise AnalysisIngestError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"Report context is not valid JSON: {path}"
        raise AnalysisIngestError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"Report context must be a JSON object: {path}"
        raise AnalysisIngestError(msg)
    return payload
