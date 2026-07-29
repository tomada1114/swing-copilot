"""`copilot-retro ingest`: verify a retrospective result and record it (§5.4).

The closing half of the retrospective loop. It reads the dossier the export
wrote and the answer the `swing-retro` skill produced, proves the answer is
trustworthy (`retro/validate.py`), renders `retro_report.md` beside the
dossier, and appends the surviving proposals to the ledger as `proposed`
(`retro/ledger.py`).

What this module deliberately cannot do:

* change configuration or code. Applying a proposal is the skill's job, on a
  branch, behind a verification plan and a PR (design §10). Ingest only writes
  a report and a ledger entry.
* move a proposal past `proposed`. Every later status is recorded by whoever
  applies, rejects, or defers it (D10).
* touch the database or the network. The whole step is two files in and two
  files out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from swing_copilot.analysis.export import write_text_atomically
from swing_copilot.retro.export import RETRO_INPUT_FILENAME
from swing_copilot.retro.ledger import read_ledger, record_proposals
from swing_copilot.retro.validate import (
    load_retro_input,
    load_retro_result,
    validate_retro_identity,
    validate_retro_result,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from swing_copilot.retro.ledger import RecordedProposal
    from swing_copilot.retro.schemas import (
        MetricEntry,
        RateMetricEntry,
        RetroInput,
        SurpriseNarration,
    )
    from swing_copilot.retro.validate import ValidatedRetro, WithheldItem

logger = logging.getLogger(__name__)

RETRO_RESULT_FILENAME: Final = "retro_result.json"
RETRO_REPORT_FILENAME: Final = "retro_report.md"

_WITHHELD_IDENTIFIER_PLACEHOLDER: Final = "（識別子非表示）"


@dataclass(frozen=True, slots=True)
class RetroIngestRequest:
    """One ingest's inputs: a retrospective directory and the ledger."""

    #: `reports/retro/<as_of>/`, holding both JSON documents and receiving the
    #: rendered report.
    retro_dir: Path
    ledger_path: Path


@dataclass(frozen=True, slots=True)
class IngestSummary:
    """What one ingest produced, for the CLI to report."""

    report_path: Path
    ledger_path: Path
    recorded: tuple[RecordedProposal, ...]
    withheld: tuple[WithheldItem, ...]
    narration_count: int


def ingest_retro_result(request: RetroIngestRequest) -> IngestSummary:
    """Verify `retro_result.json`, render the report, and append to the ledger.

    Args:
        request: The retrospective directory and the ledger to append to.

    Returns:
        Where both artifacts landed, the proposals recorded, and every item
        withheld fail-closed.

    Raises:
        RetroIngestError: A document is missing, unparsable, or answers a
            different export. Nothing is written in that case.
        OSError: Writing the ledger, a proposal's full text, or the report
            failed. Previous artifacts are left untouched.
    """
    retro_input = load_retro_input(request.retro_dir / RETRO_INPUT_FILENAME)
    result = load_retro_result(request.retro_dir / RETRO_RESULT_FILENAME)
    validate_retro_identity(retro_input, result)

    state = read_ledger(request.ledger_path)
    validated = validate_retro_result(retro_input, result, state.closed_proposal_keys())
    recorded = record_proposals(
        request.ledger_path, validated.proposals, retro_input.as_of
    )
    destination = request.retro_dir / RETRO_REPORT_FILENAME
    write_text_atomically(
        destination, render_retro_report(retro_input, validated, recorded)
    )
    logger.info(
        "retro ingest: %d proposal(s) recorded, %d item(s) withheld",
        len(recorded),
        len(validated.withheld),
    )
    return IngestSummary(
        report_path=destination.resolve(),
        ledger_path=request.ledger_path,
        recorded=recorded,
        withheld=validated.withheld,
        narration_count=len(validated.narrations),
    )


def render_retro_report(
    retro_input: RetroInput,
    validated: ValidatedRetro,
    recorded: Sequence[RecordedProposal],
) -> str:
    """Render `retro_report.md` from the dossier and what survived verification.

    Every string here is either code-owned (the dossier's own numbers and
    notes) or skill-authored text that already passed CON-03 and the evidence
    check. A withheld item contributes only its reason -- and, when CON-03 is
    what withheld it, not even its identifier.

    Args:
        retro_input: The dossier under review.
        validated: The verified narrations, proposals, and withheld items.
        recorded: The ledger entries the surviving proposals received.

    Returns:
        The complete markdown document.
    """
    symbols = {item.surprise_id: item.symbol for item in retro_input.surprises.items}
    sections = [
        f"# 振り返りレポート {retro_input.as_of.isoformat()}",
        "",
        f"- 対象期間: {retro_input.window_start.isoformat()} 〜 "
        f"{retro_input.as_of.isoformat()}",
        f"- dossier 生成: {retro_input.generated_at.isoformat()}",
        f"- サプライズ: {len(retro_input.surprises.items)} 件"
        f"（上限超過で除外 {retro_input.surprises.dropped_count} 件）",
        "",
        "## 構造的観察の自問",
        "",
        validated.structural_review_note,
        "",
        *_aggregate_section(retro_input),
        *_narration_section(validated.narrations, symbols),
        *_proposal_section(recorded),
        *_withheld_section(validated.withheld),
        *_notes_section(retro_input.notes),
    ]
    return "\n".join(sections).rstrip("\n") + "\n"


def _aggregate_section(retro_input: RetroInput) -> list[str]:
    aggregates = retro_input.aggregates
    rows = [
        *(_metric_row("separation", entry) for entry in aggregates.separation),
        *(
            _rate_row("proceed_severe_miss_rate", entry)
            for entry in aggregates.proceed_severe_miss_rate
        ),
        *(_rate_row("skip_hit_rate", entry) for entry in aggregates.skip_hit_rate),
    ]
    if not rows:
        return ["## 集約指標", "", "満期を迎えた verdict がまだ無い。", ""]
    return [
        "## 集約指標",
        "",
        "| 指標 | ホライズン | 値 | ベースライン | n | 暫定 |",
        "|---|---|---|---|---|---|",
        *rows,
        "",
    ]


def _metric_row(name: str, entry: MetricEntry) -> str:
    return (
        f"| {name} | {_horizon(entry.horizon_days)} | {_number(entry.value)} | - "
        f"| {entry.sample_size} | {_flag(entry.is_preliminary)} |"
    )


def _rate_row(name: str, entry: RateMetricEntry) -> str:
    flagged = " ⚠" if entry.is_flagged else ""
    return (
        f"| {name}{flagged} | {_horizon(entry.horizon_days)} | {_number(entry.value)} "
        f"| {_number(entry.baseline_value)} | {entry.sample_size} "
        f"| {_flag(entry.is_preliminary)} |"
    )


def _horizon(horizon_days: int | None) -> str:
    return "合成" if horizon_days is None else f"{horizon_days}日"


def _number(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _flag(is_set: bool) -> str:
    return "はい" if is_set else "いいえ"


def _narration_section(
    narrations: Sequence[SurpriseNarration], symbols: dict[str, str]
) -> list[str]:
    if not narrations:
        return []
    lines = ["## サプライズの再読", ""]
    for narration in narrations:
        # Every verified narration names an exported surprise: one that does
        # not was already withheld, so this lookup cannot miss.
        symbol = symbols[narration.surprise_id]
        lines.extend(
            [
                f"### {symbol}",
                "",
                f"- 敗因分類: `{narration.failure_class}`",
                f"- 証拠: {_references(narration.evidence_refs)}",
                "",
                narration.narrative,
                "",
            ]
        )
    return lines


def _proposal_section(recorded: Sequence[RecordedProposal]) -> list[str]:
    if not recorded:
        return ["## 改善提案", "", "今回の振り返りで台帳に記録された提案はない。", ""]
    lines = [
        "## 改善提案",
        "",
        "| RP-ID | level | proposal_key | タイトル | 証拠の種類 |",
        "|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {item.rp_id} | {item.proposal.level} | `{item.proposal.proposal_key}` "
        f"| {item.proposal.title} | {item.proposal.evidence_basis} |"
        for item in recorded
    )
    lines.append("")
    for item in recorded:
        proposal = item.proposal
        lines.extend(
            [
                f"### {item.rp_id} {proposal.title}",
                "",
                f"- 対象: `{proposal.target}`",
                f"- 証拠: {_references(proposal.evidence_refs)}",
                f"- 主張: {proposal.claim}",
                f"- 期待効果: {proposal.expected_effect}",
                f"- 検証計画: {proposal.verification_plan or '（L3 のため個別計画なし）'}",
                *[f"- リスク: {risk}" for risk in proposal.risks],
                f"- 全文: `{item.document_path}`",
                "",
            ]
        )
    return lines


def _withheld_section(withheld: Sequence[WithheldItem]) -> list[str]:
    if not withheld:
        return []
    return [
        "## 非表示（fail-closed、リトライなし）",
        "",
        "| 種別 | 識別子 | 理由 |",
        "|---|---|---|",
        *[
            f"| {item.kind} | {item.identifier or _WITHHELD_IDENTIFIER_PLACEHOLDER} "
            f"| {item.reason} |"
            for item in withheld
        ],
        "",
    ]


def _notes_section(notes: Sequence[str]) -> list[str]:
    if not notes:
        return []
    return ["## エクスポート時の注記", "", *[f"- {note}" for note in notes], ""]


def _references(refs: Sequence[str]) -> str:
    return ", ".join(f"`{ref}`" for ref in refs)
