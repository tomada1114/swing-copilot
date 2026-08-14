"""The proposal ledger: `docs/retro/proposals.md` and its full texts (D3, E32.1).

History, audit trail, and duplicate suppressor -- never an approval gate. The
approval model lives in D10: L1 proposals are applied straight away and land in
a PR, L2/L3 need design approval first. What this file gives that flow is a
durable record.

Two rules shape the code here:

* **`ingest` only appends `proposed` rows** (D10). Every later status is
  written by the applying skill or by a human, so an append must preserve the
  file it found -- unknown columns, hand edits, trailing prose and all.
* **Re-ingesting the same result must not duplicate a row** (P8-32's roadmap
  seed). A `proposal_key` already carried by an open ledger row is recognized
  as the same proposal and reuses its RP-ID; a key whose only rows are closed
  is a *reopening* and gets a new one, which is why the re-proposal guard
  (`retro/validate.py`) runs before this module ever sees it.

Divergence from the design sketch: design §8.2 lists the ledger's columns as
RP-ID / 日付 / level / タイトル / status / PR・決裁メモ / リンク, with no
`proposal_key`. E32.2 nonetheless requires the guard to match a re-proposal
against closed rows by exactly that key, which those columns cannot express, so
the table carries a `proposal_key` column. Rows are still read structurally, so
a ledger written without the column keeps parsing -- it simply cannot take part
in key matching.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from swing_copilot.analysis.export import write_text_atomically
from swing_copilot.documents import read_text_document
from swing_copilot.retro.validate import RetroIngestError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date
    from pathlib import Path

    from swing_copilot.retro.schemas import Proposal

#: Statuses that close a proposal: a re-proposal under the same key needs an
#: explicit `reopen_justification` (E32.2, design §5.4).
CLOSED_STATUSES: Final = frozenset({"rejected", "verification_failed"})
#: The only status `ingest` writes (D10).
PROPOSED_STATUS: Final = "proposed"
#: Every status the ledger's lifecycle defines (design §8.2). Used to find the
#: status cell structurally, so a column reorder does not silently blind the
#: re-proposal guard.
KNOWN_STATUSES: Final = CLOSED_STATUSES | {
    PROPOSED_STATUS,
    "applied",
    "deferred",
    "merged",
    "reverted",
}

#: Full texts live beside the ledger, in `docs/retro/proposals/` (E32.3).
PROPOSALS_SUBDIR: Final = "proposals"
_RP_ID_PATTERN: Final = re.compile(r"^RP-(\d{3,})$")
_DOCUMENT_NAME_PATTERN: Final = re.compile(r"^RP-(\d{3,})-")
_SLUG_SEPARATOR_PATTERN: Final = re.compile(r"[^a-z0-9]+")
_RP_ID_DIGITS: Final = 3
_MAX_SLUG_LENGTH: Final = 48
#: Used when a `proposal_key` and title are both non-ASCII: the RP-ID already
#: identifies the file, and a transliterated stem would only look meaningful.
_FALLBACK_SLUG: Final = "proposal"

_COLUMNS: Final = (
    "RP-ID",
    "日付",
    "level",
    "proposal_key",
    "タイトル",
    "status",
    "PR/決裁メモ",
    "リンク",
)
_LEDGER_HEADER: Final = f"""# 改善提案台帳

`copilot-retro ingest` が検証を通った提案を status={PROPOSED_STATUS} で追記する。
以降の遷移（applied / rejected / deferred / verification_failed、および
applied 後の merged / reverted）は適用段階のスキルと人間が記録する（D10）。

{"| " + " | ".join(_COLUMNS) + " |"}
{"|" + "---|" * len(_COLUMNS)}
"""


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One parsed ledger row.

    `proposal_key` is `None` for a row written before the column existed, or
    by hand without it: such a row still reserves its RP-ID but cannot be
    matched by key.
    """

    rp_id: str
    status: str
    proposal_key: str | None


@dataclass(frozen=True, slots=True)
class LedgerState:
    """The ledger as the guard and the numbering need to see it."""

    exists: bool
    rows: tuple[LedgerRow, ...]

    def closed_proposal_keys(self) -> frozenset[str]:
        """Return the keys a re-proposal may only revisit with a justification."""
        return frozenset(
            row.proposal_key
            for row in self.rows
            if row.proposal_key is not None and row.status in CLOSED_STATUSES
        )

    def closed_rp_ids(self) -> tuple[str, ...]:
        """Return the RP-IDs of closed rows, in ledger order and deduplicated."""
        return tuple(
            dict.fromkeys(
                row.rp_id for row in self.rows if row.status in CLOSED_STATUSES
            )
        )

    def rp_id_for_key(self, proposal_key: str) -> str | None:
        """Return the open row's RP-ID for this key, if one is already recorded.

        Closed rows are deliberately ignored: a proposal whose only history is
        a rejection is being reopened, and a reopening is a new entry rather
        than an edit of the one that was turned down.
        """
        return next(
            (
                row.rp_id
                for row in self.rows
                if row.proposal_key == proposal_key
                and row.status not in CLOSED_STATUSES
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class RecordedProposal:
    """One proposal's place in the ledger after an append.

    `is_new` is `False` when a previous ingest already recorded this key, which
    is how a repeated `copilot-retro ingest` stays idempotent.
    """

    rp_id: str
    proposal: Proposal
    document_path: Path
    is_new: bool


def read_ledger(path: Path) -> LedgerState:
    """Parse the ledger, tolerating its absence and its hand edits.

    A line is a row when it carries an `RP-NNN` cell; its status is whichever
    cell names a documented lifecycle state. Matching structurally rather than
    by column position means reordering or adding a column does not silently
    empty the re-proposal guard. `proposal_key` is read from the column the
    header names, and is absent when the header does not name one.

    An absent ledger is the ordinary first-run state and reads as empty. A
    ledger that exists but cannot be read is not: it is the one input the
    re-proposal guard has, so silently treating a permission error or a
    wrongly encoded file as "no closed proposals" would let a rejected proposal
    back in under a fresh RP-ID. Both `export` and `ingest` read it, so both
    fail loudly instead.

    Args:
        path: Ledger location, typically `docs/retro/proposals.md`.

    Returns:
        Whether the file exists and every data row it holds.

    Raises:
        RetroIngestError: The ledger exists but could not be read or decoded
            as UTF-8.
    """
    if not path.is_file():
        return LedgerState(exists=False, rows=())
    text = read_text_document(
        path, label="Proposal ledger", error_type=RetroIngestError
    )
    key_index: int | None = None
    rows: list[LedgerRow] = []
    for line in text.splitlines():
        cells = _table_cells(line)
        if cells is None:
            continue
        if key_index is None and "proposal_key" in cells:
            key_index = cells.index("proposal_key")
        row = _ledger_row(cells, key_index)
        if row is not None:
            rows.append(row)
    return LedgerState(exists=True, rows=tuple(rows))


def record_proposals(
    path: Path, proposals: Sequence[Proposal], as_of: date
) -> tuple[RecordedProposal, ...]:
    """Append every verified proposal to the ledger as `proposed`.

    Generates the ledger with its header when absent (E32.1) and writes each
    proposal's full text to `proposals/RP-NNN-<slug>.md`. Both writes replace
    their destination atomically, so a failure leaves the previous ledger and
    the previous full texts intact.

    Args:
        path: Ledger location, typically `docs/retro/proposals.md`.
        proposals: The proposals that survived verification, in result order.
        as_of: The retrospective's date, recorded in each new row.

    Returns:
        One `RecordedProposal` per input proposal, in the same order.

    Raises:
        OSError: Writing the ledger or a full text failed.
    """
    if not proposals:
        return ()
    state = read_ledger(path)
    documents = path.parent / PROPOSALS_SUBDIR
    next_number = _next_number(state, documents)

    recorded: list[RecordedProposal] = []
    for proposal in proposals:
        existing = state.rp_id_for_key(proposal.proposal_key)
        rp_id = existing or _format_rp_id(next_number)
        if existing is None:
            next_number += 1
        recorded.append(
            RecordedProposal(
                rp_id=rp_id,
                proposal=proposal,
                document_path=documents / _document_name(rp_id, proposal),
                is_new=existing is None,
            )
        )

    for item in recorded:
        if item.is_new or not item.document_path.is_file():
            _write_atomically(item.document_path, _proposal_document(item, as_of))
    added = [item for item in recorded if item.is_new]
    if added:
        _write_atomically(path, _appended_ledger(path, added, as_of))
    return tuple(recorded)


def _table_cells(line: str) -> list[str] | None:
    """Split a markdown table line into unescaped cells, or `None` if it is prose."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    return [cell.strip().replace(r"\|", "|") for cell in stripped.strip("|").split("|")]


def _ledger_row(cells: list[str], key_index: int | None) -> LedgerRow | None:
    """Build a row from its cells, or `None` for a header/separator line.

    An RP-ID is what makes a line a row. A status cell outside the documented
    lifecycle leaves `status` empty rather than dropping the row, so a
    hand-written status still reserves its number and is never read as closed.
    """
    rp_id = next((cell for cell in cells if _RP_ID_PATTERN.match(cell)), None)
    if rp_id is None:
        return None
    status = next((cell for cell in cells if cell in KNOWN_STATUSES), "")
    proposal_key = (
        cells[key_index]
        if key_index is not None and key_index < len(cells) and cells[key_index]
        else None
    )
    return LedgerRow(rp_id=rp_id, status=status, proposal_key=proposal_key)


def _next_number(state: LedgerState, documents: Path) -> int:
    """Return the next RP number, above every ledger row and every full text.

    Full texts are consulted too so an interrupted append -- a written document
    whose ledger row never landed -- cannot have its number handed to a
    different proposal on the next run.
    """
    numbers = [
        int(match.group(1))
        for row in state.rows
        if (match := _RP_ID_PATTERN.match(row.rp_id))
    ]
    if documents.is_dir():
        numbers.extend(
            int(match.group(1))
            for entry in documents.iterdir()
            if (match := _DOCUMENT_NAME_PATTERN.match(entry.name))
        )
    return max(numbers, default=0) + 1


def _format_rp_id(number: int) -> str:
    return f"RP-{number:0{_RP_ID_DIGITS}d}"


def _document_name(rp_id: str, proposal: Proposal) -> str:
    return f"{rp_id}-{_slug(proposal)}.md"


def _slug(proposal: Proposal) -> str:
    """Derive a filesystem-safe stem from the key, then the title.

    The key first because it is the proposal's stable identity (E32.2): the
    same proposal re-ingested must resolve to the same file, even if the skill
    reworded its title. The result is `[a-z0-9-]` only, so it cannot escape the
    proposals directory.
    """
    for source in (proposal.proposal_key, proposal.title):
        normalized = unicodedata.normalize("NFKD", source).encode("ascii", "ignore")
        slug = _SLUG_SEPARATOR_PATTERN.sub("-", normalized.decode().lower()).strip("-")
        if slug:
            return slug[:_MAX_SLUG_LENGTH].strip("-")
    return _FALLBACK_SLUG


def _appended_ledger(path: Path, added: Sequence[RecordedProposal], as_of: date) -> str:
    """Return the ledger's full text with the new rows inserted after the table.

    Insertion targets the last table line rather than the end of the file, so
    notes a human left below the table stay below it.
    """
    existing = path.read_text(encoding="utf-8") if path.is_file() else _LEDGER_HEADER
    lines = existing.splitlines()
    rows = [_ledger_line(item, as_of) for item in added]
    last_table_line = max(
        (index for index, line in enumerate(lines) if line.strip().startswith("|")),
        default=len(lines) - 1,
    )
    merged = lines[: last_table_line + 1] + rows + lines[last_table_line + 1 :]
    return "\n".join(merged) + "\n"


def _ledger_line(item: RecordedProposal, as_of: date) -> str:
    proposal = item.proposal
    link = f"[全文]({PROPOSALS_SUBDIR}/{item.document_path.name})"
    cells = (
        item.rp_id,
        as_of.isoformat(),
        proposal.level,
        proposal.proposal_key,
        proposal.title,
        PROPOSED_STATUS,
        "",
        link,
    )
    return "| " + " | ".join(_escaped(cell) for cell in cells) + " |"


def _escaped(cell: str) -> str:
    """Keep one cell on one line and inside its own column."""
    return cell.replace("|", r"\|").replace("\n", " ").strip()


def _proposal_document(item: RecordedProposal, as_of: date) -> str:
    """Render one proposal's full text, evidence and verification plan included."""
    proposal = item.proposal
    plan = proposal.verification_plan or "（L3 設計見直しのため個別の検証計画なし）"
    sections = [
        f"# {item.rp_id} {proposal.title}",
        "",
        f"- 提案日: {as_of.isoformat()}",
        f"- level: {proposal.level}",
        f"- status: {PROPOSED_STATUS}",
        f"- proposal_key: `{proposal.proposal_key}`",
        f"- 対象: `{proposal.target}`",
        f"- 証拠の種類: {proposal.evidence_basis}",
        "",
        "## 主張",
        "",
        proposal.claim,
        "",
        "## 期待効果",
        "",
        proposal.expected_effect,
        "",
        "## 証拠",
        "",
        *[f"- `{ref}`" for ref in proposal.evidence_refs],
        "",
        "## 検証計画",
        "",
        plan,
        "",
        "## リスク",
        "",
        *[f"- {risk}" for risk in proposal.risks],
    ]
    if proposal.reopen_justification is not None:
        sections.extend(["", "## 再提案の理由", "", proposal.reopen_justification])
    return "\n".join(sections) + "\n"


def _write_atomically(destination: Path, content: str) -> None:
    """Create the destination's directory, then replace it atomically."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(destination, content)
