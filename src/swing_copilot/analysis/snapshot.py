"""Persist and reload one run's rendered-report context (`report_context.json`).

`copilot-ingest-analysis` must re-render exactly the report `copilot-daily`
produced, with only the qualitative sections replaced. Rather than re-running
screening (which would not be point-in-time reproducible and would touch the
network), the daily run archives its presentation-neutral `DailyBrief` beside
`analysis_input.json`, and ingest reloads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from swing_copilot.analysis.export import write_json_atomically
from swing_copilot.analysis.schemas import (
    NonBlankText,
    Sha256Digest,
    canonical_json_digest,
)
from swing_copilot.analysis.validate import AnalysisIngestError
from swing_copilot.documents import read_json_document
from swing_copilot.models import RunStatus
from swing_copilot.report.daily_brief import DailyBrief

if TYPE_CHECKING:
    from typing import Any

REPORT_CONTEXT_FILENAME = "report_context.json"
CONTEXT_SCHEMA_VERSION = "report-context-v2"

# Pydantic evaluates a dataclass's annotation strings against its *defining*
# module's globals, so `report/daily_brief.py` imports `date`/`datetime`/`UUID`
# at runtime for this adapter's benefit.
_BRIEF_ADAPTER: TypeAdapter[DailyBrief] = TypeAdapter(DailyBrief)


class _ReportContextDocument(BaseModel):
    """Strict serialized envelope for an archived report context."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["report-context-v2"]
    run_id: UUID
    as_of: date
    strategy_key: NonBlankText
    input_digest: Sha256Digest
    context_digest: Sha256Digest
    status: str
    output_dir: str
    brief: dict[str, object]

    @model_validator(mode="before")
    @classmethod
    def _verify_context_digest(cls, value: object) -> object:
        """Reject a context whose identity envelope was changed after writing."""
        if not isinstance(value, dict):
            return value
        actual = value.get("context_digest")
        if isinstance(actual, str) and actual != canonical_json_digest(
            value, excluded_field="context_digest"
        ):
            msg = "context_digest does not match canonical report context JSON"
            raise ValueError(msg)
        return value


@dataclass(frozen=True, slots=True)
class ReportContext:
    """The archived state needed to re-render one run's reports."""

    brief: DailyBrief
    status: RunStatus
    output_dir: Path
    strategy_key: str
    input_digest: str


def write_report_context(context: ReportContext, destination_dir: Path) -> Path:
    """Archive `context` as `report_context.json` via atomic replacement.

    Args:
        context: The run's brief, status, analysis identity, and report root.
        destination_dir: The run's dated report directory (the same place
            `analysis_input.json` is written to).

    Returns:
        The written file's path.

    Raises:
        OSError: Writing or replacing failed; the previous file is preserved.
    """
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / REPORT_CONTEXT_FILENAME
    unsigned_payload: dict[str, object] = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "run_id": str(context.brief.run_id),
        "as_of": context.brief.run_date.isoformat(),
        "strategy_key": context.strategy_key,
        "input_digest": context.input_digest,
        "status": context.status.value,
        "output_dir": str(context.output_dir),
        "brief": _BRIEF_ADAPTER.dump_python(context.brief, mode="json"),
    }
    document = _ReportContextDocument.model_validate(
        {
            **unsigned_payload,
            "context_digest": canonical_json_digest(
                unsigned_payload, excluded_field="context_digest"
            ),
        }
    )
    write_json_atomically(destination, document.model_dump(mode="json"))
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
    try:
        document = _ReportContextDocument.model_validate(payload)
        status = RunStatus(document.status)
        brief = _BRIEF_ADAPTER.validate_python(document.brief)
        # Required, not defaulted: silently falling back to the process CWD
        # would rewrite the report somewhere other than the run's archive.
        output_dir = Path(document.output_dir)
    except (KeyError, ValueError, ValidationError) as exc:
        msg = f"Report context failed validation: {path}\n{exc}"
        raise AnalysisIngestError(msg) from exc
    if document.run_id != brief.run_id:
        msg = f"Report context run_id does not match its brief: {path}"
        raise AnalysisIngestError(msg)
    if document.as_of != brief.run_date:
        msg = f"Report context as_of does not match its brief: {path}"
        raise AnalysisIngestError(msg)
    return ReportContext(
        brief=brief,
        status=status,
        output_dir=output_dir,
        strategy_key=document.strategy_key,
        input_digest=document.input_digest,
    )


def _read_payload(path: Path) -> dict[str, Any]:
    """Return the archived envelope, or fail as a broken artifact.

    Reading goes through the shared ingest reader so a context file that is not
    UTF-8 arrives as `AnalysisIngestError` rather than a raw
    `UnicodeDecodeError` (Issue #164). Only the top-level object check is local:
    the envelope is addressed by key, unlike the documents
    `read_analysis_document` returns as-is.
    """
    payload = read_json_document(
        path, label="Report context", error_type=AnalysisIngestError
    )
    if not isinstance(payload, dict):
        msg = f"Report context must be a JSON object: {path}"
        raise AnalysisIngestError(msg)
    return payload
