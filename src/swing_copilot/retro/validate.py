"""Machine verification of the skill-produced `retro_result.json` (design §5.4).

The retrospective's trust boundary, and a close relative of
`analysis/validate.py`. Nothing the `swing-retro` skill writes is believed on
sight; before a proposal can reach a report or the proposal ledger this module
proves:

1. the document parses under `retro-result-v1` (`schemas.py`);
2. it answers *this* dossier -- same `as_of`, same `input_digest`;
3. every `evidence_refs` entry names an identifier the dossier actually
   supplied (E32.4);
4. no user-visible text violates CON-03 (`analysis/safety.py`);
5. a proposal the ledger already closed comes back only with an explicit
   `reopen_justification` (E32.2).

Rule 2 is a hard failure for the whole run: a result describing another export
cannot be partially believed. Rules 3-5 withhold exactly one proposal or one
narration, fail-closed and without retry, so one bad item never costs the rest
of the retrospective.

CON-03 is checked **first**, before the evidence and guard rules, and it
covers each item's own identifiers (`proposal_key`, `surprise_id`,
`evidence_refs`) as well as its prose. That ordering is what lets every later
withholding reason quote the offending value: by the time a reason is written,
the strings it names have already passed the output-policy check. An item
withheld *by* CON-03 is reported without any of its own strings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ValidationError

from swing_copilot.analysis.safety import ForbiddenLanguageError, check_display_texts
from swing_copilot.analysis.validate import WITHHELD_MESSAGE
from swing_copilot.documents import read_json_document
from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.retro.schemas import RetroInput, RetroResult

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator
    from datetime import date
    from pathlib import Path

    from swing_copilot.retro.schemas import Proposal, SurpriseNarration

logger = logging.getLogger(__name__)

WithheldKind = Literal["proposal", "narration", "structural_review_note"]

#: Deliberately carries no skill-authored text: it is written for an item that
#: failed the output-policy check, so quoting the offending field would route
#: the violation into the very report CON-03 protects. The detail is logged.
CON03_WITHHELD_REASON = "CON-03 違反のため非表示（リトライなし）。詳細はログを参照"


class RetroIngestError(SwingCopilotError):
    """Raised when a retrospective document cannot be read or trusted at all."""


@dataclass(frozen=True, slots=True)
class WithheldItem:
    """One proposal or narration that did not survive verification.

    `identifier` is `None` exactly when CON-03 withheld the item, because its
    own `proposal_key`/`surprise_id` is then unverified text.
    """

    kind: WithheldKind
    identifier: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class ValidatedRetro:
    """Everything from one `retro_result.json` that may be rendered."""

    as_of: date
    structural_review_note: str
    narrations: tuple[SurpriseNarration, ...]
    proposals: tuple[Proposal, ...]
    withheld: tuple[WithheldItem, ...]


def load_retro_input(path: Path) -> RetroInput:
    """Read and strictly validate `retro_input.json`.

    Args:
        path: Path to the exported dossier.

    Returns:
        The parsed dossier.

    Raises:
        RetroIngestError: The file is missing, is not JSON, or violates the
            input schema (including a digest that no longer matches its body).
    """
    return _load(path, RetroInput)


def load_retro_result(path: Path) -> RetroResult:
    """Read and strictly validate `retro_result.json`.

    Args:
        path: Path to the skill-produced result.

    Returns:
        The parsed result.

    Raises:
        RetroIngestError: The file is missing, is not JSON, or violates the
            result schema (including unknown fields).
    """
    return _load(path, RetroResult)


def validate_retro_identity(retro_input: RetroInput, result: RetroResult) -> None:
    """Hard-fail unless both documents describe the same export.

    Separate from the per-item rules on purpose: a mismatch means the numbers
    the skill reasoned over are not the numbers in this dossier, so no part of
    the result is safe to render -- unlike one bad proposal, which can be
    withheld while its siblings stand.

    Args:
        retro_input: The dossier that was exported.
        result: The skill's parsed answer.

    Raises:
        RetroIngestError: `as_of` or `input_digest` disagrees with the dossier.
    """
    checks = (
        ("retro_result as_of", str(result.as_of), str(retro_input.as_of)),
        ("retro_result input_digest", result.input_digest, retro_input.input_digest),
    )
    for document_field, actual, expected in checks:
        if actual != expected:
            msg = f"{document_field} {actual} does not match retro_input ({expected})"
            raise RetroIngestError(msg)


def validate_retro_result(
    retro_input: RetroInput,
    result: RetroResult,
    closed_proposal_keys: Collection[str],
) -> ValidatedRetro:
    """Apply the per-item evidence, CON-03, and re-proposal rules.

    Args:
        retro_input: The dossier the result answers; the source of the only
            identifiers a reference may name.
        result: The skill's parsed answer.
        closed_proposal_keys: `proposal_key`s the ledger records as `rejected`
            or `verification_failed` (E32.2).

    Returns:
        The narrations and proposals that may be rendered, plus one
        `WithheldItem` per item that may not.
    """
    space = evidence_id_space(retro_input)
    surprise_ids = frozenset(item.surprise_id for item in retro_input.surprises.items)
    withheld: list[WithheldItem] = []

    note = result.structural_review_note
    if _con03_error([note]) is not None:
        withheld.append(
            WithheldItem("structural_review_note", None, CON03_WITHHELD_REASON)
        )
        note = WITHHELD_MESSAGE

    narrations: list[SurpriseNarration] = []
    for narration in result.narrations:
        rejection = _narration_rejection(narration, space, surprise_ids)
        if rejection is None:
            narrations.append(narration)
        else:
            withheld.append(rejection)

    proposals: list[Proposal] = []
    for proposal in result.proposals:
        rejection = _proposal_rejection(proposal, space, closed_proposal_keys)
        if rejection is None:
            proposals.append(proposal)
        else:
            withheld.append(rejection)

    return ValidatedRetro(
        as_of=result.as_of,
        structural_review_note=note,
        narrations=tuple(narrations),
        proposals=tuple(proposals),
        withheld=tuple(withheld),
    )


def evidence_id_space(retro_input: RetroInput) -> frozenset[str]:
    """Return every identifier this dossier supplied (E32.4).

    The aggregate, surprise, and source identifiers the export named -- and
    nothing else. Signal-performance rows are carried verbatim from P2-11 and
    have no identifier of their own, so a proposal about a signal cites the
    surprises or metrics that show it, not the signal name.

    Args:
        retro_input: The exported dossier.

    Returns:
        The closed set an `evidence_refs` entry must belong to.
    """
    aggregates = retro_input.aggregates
    ids = {
        entry.metric_id
        for group in (
            aggregates.separation,
            aggregates.proceed_severe_miss_rate,
            aggregates.skip_hit_rate,
        )
        for entry in group
    }
    # `verdict_mix` is the one aggregate that never falls silent: the rate
    # metrics above lose their denominator in a zero-proceed window, which is
    # exactly the window a proposal most needs to argue about (P8-120). Leaving
    # its ID out of the space made that argument unwritable -- the skill is
    # told to read the metric first and could not then cite it.
    ids.add(aggregates.verdict_mix.metric_id)
    # Issue #189: the L2 gate rows and the per-configuration split are the two
    # newest citable populations. An L2 proposal argues from the gate row, and
    # a "the change helped/hurt" claim argues from one configuration's own
    # separation -- both would be uncitable, and therefore unwritable, if their
    # identifiers stayed outside the space.
    if (history := retro_input.failure_class_history) is not None:
        ids.update(row.count_id for row in history.counts)
    ids.update(
        entry.metric_id
        for group in retro_input.aggregates_by_config
        for entry in group.separation
    )
    ids.update(cell.cell_id for cell in retro_input.human_alignment)
    ids.update(row.contribution_id for row in retro_input.source_contribution)
    ids.update(row.basis_id for row in retro_input.basis_contribution)
    if (news_supply := aggregates.news_supply) is not None:
        # Issue #154: both the summary and its cells are citable, because a
        # proposal about the `sufficient` threshold argues from the whole
        # cross-tab as often as from one `(level, recommendation)` cell.
        ids.add(news_supply.metric_id)
        ids.update(cell.cell_id for cell in news_supply.cells)
    for surprise in retro_input.surprises.items:
        ids.add(surprise.surprise_id)
        ids.update(surprise.cited_source_ids)
        for reason in surprise.reasons:
            ids.update(reason.source_ids)
        ids.update(item.source_id for item in surprise.freshness.news)
        ids.update(item.source_id for item in surprise.freshness.filings)
    return frozenset(ids)


def _load[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    """Read and strictly validate one retrospective document.

    Reading is delegated to the shared ingest reader so that every way the
    bytes fail to become JSON -- including a file that is not UTF-8 (Issue
    #164) -- arrives as `RetroIngestError`, the type this boundary's callers
    use to tell a broken artifact from an unexpected fault.
    """
    payload = read_json_document(
        path, label="Retrospective document", error_type=RetroIngestError
    )
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        msg = f"Retrospective document failed schema validation: {path}\n{exc}"
        raise RetroIngestError(msg) from exc


def _narration_rejection(
    narration: SurpriseNarration,
    space: frozenset[str],
    surprise_ids: frozenset[str],
) -> WithheldItem | None:
    """Return why this narration is withheld, or `None` if it may be rendered."""
    if _con03_error(_narration_texts(narration)) is not None:
        _log_withheld("narration", CON03_WITHHELD_REASON)
        return WithheldItem("narration", None, CON03_WITHHELD_REASON)
    if narration.surprise_id not in surprise_ids:
        return _rejected(
            "narration",
            narration.surprise_id,
            f"dossier に無い surprise_id を叙述している: {narration.surprise_id}",
        )
    unknown = _unknown_references(narration.evidence_refs, space)
    if unknown:
        return _rejected(
            "narration",
            narration.surprise_id,
            f"供給されていない evidence_refs を含む: {unknown}",
        )
    return None


def _proposal_rejection(
    proposal: Proposal,
    space: frozenset[str],
    closed_proposal_keys: Collection[str],
) -> WithheldItem | None:
    """Return why this proposal is withheld, or `None` if it may be recorded."""
    if _con03_error(_proposal_texts(proposal)) is not None:
        _log_withheld("proposal", CON03_WITHHELD_REASON)
        return WithheldItem("proposal", None, CON03_WITHHELD_REASON)
    unknown = _unknown_references(proposal.evidence_refs, space)
    if unknown:
        return _rejected(
            "proposal",
            proposal.proposal_key,
            f"供給されていない evidence_refs を含む: {unknown}",
        )
    if (
        proposal.proposal_key in closed_proposal_keys
        and proposal.reopen_justification is None
    ):
        return _rejected(
            "proposal",
            proposal.proposal_key,
            "台帳で却下・検証不合格になった提案の再提案だが reopen_justification が無い",
        )
    return None


def _rejected(kind: WithheldKind, identifier: str, reason: str) -> WithheldItem:
    _log_withheld(kind, f"{identifier}: {reason}")
    return WithheldItem(kind, identifier, reason)


def _log_withheld(kind: WithheldKind, detail: str) -> None:
    logger.warning("retro %s withheld (no retry): %s", kind, detail)


def _unknown_references(refs: list[str], space: frozenset[str]) -> list[str]:
    return sorted(set(refs) - space)


def _con03_error(texts: Iterable[str]) -> str | None:
    """Return the output-policy failure for these strings, or `None`."""
    try:
        check_display_texts(texts)
    except ForbiddenLanguageError as exc:
        logger.warning("CON-03 violation in retro_result.json: %s", exc)
        return str(exc)
    return None


def _narration_texts(narration: SurpriseNarration) -> Iterator[str]:
    """Every string of this narration a report or the ledger could render."""
    yield narration.surprise_id
    yield narration.narrative
    yield from narration.evidence_refs


def _proposal_texts(proposal: Proposal) -> Iterator[str]:
    """Every string of this proposal a report or the ledger could render."""
    yield proposal.proposal_key
    yield proposal.target
    yield proposal.title
    yield proposal.claim
    yield proposal.expected_effect
    yield from proposal.evidence_refs
    yield from proposal.risks
    if proposal.verification_plan is not None:
        yield proposal.verification_plan
    if proposal.reopen_justification is not None:
        yield proposal.reopen_justification
