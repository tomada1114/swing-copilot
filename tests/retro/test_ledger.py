"""P8-32: the proposal ledger is generated, appended to, and never duplicated.

The ledger is history, audit trail, and duplicate suppressor (D3) -- not an
approval gate. `ingest` only ever appends `proposed` rows (D10); every later
status is written by the applying skill or by a human, and this module must
preserve both.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from swing_copilot.retro.ledger import (
    PROPOSALS_SUBDIR,
    read_ledger,
    record_proposals,
)
from swing_copilot.retro.schemas import Proposal
from swing_copilot.retro.validate import RetroIngestError
from tests.retro.conftest import proposal_payload

AS_OF = date(2027, 3, 29)
PROJECT_ROOT = Path(__file__).parents[2]
#: The empty ledger committed by P8-33, which `ingest` would otherwise create
#: on its first run (E32.1).
COMMITTED_LEDGER = PROJECT_ROOT / "docs/retro/proposals.md"


def _proposal(**overrides: Any) -> Proposal:
    return Proposal.model_validate(proposal_payload(**overrides))


def _ledger_rows(path: Path) -> list[str]:
    """Every data row of the ledger table, header and separator excluded."""
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.match(r"\| RP-\d", line)
    ]


class TestReadLedger:
    def test_reports_an_absent_ledger_without_failing(self, tmp_path: Path) -> None:
        state = read_ledger(tmp_path / "proposals.md")

        assert (state.exists, state.rows) == (False, ())

    def test_collects_the_keys_the_re_proposal_guard_must_block(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "proposals.md"
        path.write_text(
            "| RP-ID | 日付 | level | proposal_key | タイトル | status | メモ |\n"
            "|---|---|---|---|---|---|---|\n"
            "| RP-001 | 2027-03-01 | L1 | config:a | 案A | applied | #12 |\n"
            "| RP-002 | 2027-03-05 | L2 | config:b | 案B | rejected | 却下 |\n"
            "| RP-003 | 2027-03-09 | L1 | config:c | 案C | verification_failed | 差戻 |\n",
            encoding="utf-8",
        )

        state = read_ledger(path)

        assert state.closed_proposal_keys() == frozenset({"config:b", "config:c"})
        assert state.rp_id_for_key("config:a") == "RP-001"
        assert state.rp_id_for_key("config:b") is None

    def test_tolerates_a_legacy_row_without_a_proposal_key_column(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "proposals.md"
        path.write_text(
            "| RP-ID | 日付 | level | タイトル | status |\n"
            "|---|---|---|---|---|\n"
            "| RP-007 | 2027-03-01 | L1 | 手書きの行 | rejected |\n",
            encoding="utf-8",
        )

        state = read_ledger(path)

        assert state.closed_proposal_keys() == frozenset()
        assert state.rows[0].rp_id == "RP-007"

    def test_an_unreadable_ledger_fails_instead_of_reading_as_empty(
        self, tmp_path: Path
    ) -> None:
        # An absent ledger is the first-run state; a ledger that exists but
        # cannot be decoded is not. Reading it as empty would silently empty
        # the re-proposal guard and let a rejected proposal back in.
        path = tmp_path / "proposals.md"
        path.write_bytes("| RP-001 | 却下済み | rejected |\n".encode("shift_jis"))

        with pytest.raises(RetroIngestError, match="Proposal ledger could not be read"):
            read_ledger(path)


class TestRecordProposals:
    def test_generates_the_ledger_with_its_header_when_absent(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "proposals.md"

        recorded = record_proposals(path, [_proposal()], AS_OF)

        text = path.read_text(encoding="utf-8")
        assert "| RP-ID |" in text
        assert recorded[0].rp_id == "RP-001"
        assert _ledger_rows(path) == [
            "| RP-001 | 2027-03-29 | L1 | config:postmortem.severe_threshold_pct "
            "| 重大境界の見直し | proposed |  "
            "| [全文](proposals/RP-001-config-postmortem-severe-threshold-pct.md) |"
        ]

    def test_writes_the_full_text_beside_the_ledger(self, tmp_path: Path) -> None:
        path = tmp_path / "proposals.md"

        recorded = record_proposals(path, [_proposal()], AS_OF)

        document = recorded[0].document_path
        assert document.parent == tmp_path / PROPOSALS_SUBDIR
        body = document.read_text(encoding="utf-8")
        for expected in (
            "RP-001",
            "config:postmortem.severe_threshold_pct",
            "separation が負のまま推移している可能性がある",
            "copilot-backtest",
            "サンプルが小さく暫定域である",
            "metric:separation:5d",
        ):
            assert expected in body

    def test_numbers_continue_from_the_ledgers_existing_maximum(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "proposals.md"
        path.write_text(
            "| RP-ID | 日付 | level | proposal_key | タイトル | status | メモ |\n"
            "|---|---|---|---|---|---|---|\n"
            "| RP-008 | 2027-03-01 | L1 | config:old | 旧案 | applied | #12 |\n",
            encoding="utf-8",
        )

        recorded = record_proposals(path, [_proposal()], AS_OF)

        assert recorded[0].rp_id == "RP-009"
        assert len(_ledger_rows(path)) == 2

    def test_numbers_skip_an_orphaned_proposal_document(self, tmp_path: Path) -> None:
        path = tmp_path / "proposals.md"
        documents = tmp_path / PROPOSALS_SUBDIR
        documents.mkdir()
        (documents / "RP-004-orphan.md").write_text(
            "先に書かれた本文", encoding="utf-8"
        )

        recorded = record_proposals(path, [_proposal()], AS_OF)

        assert recorded[0].rp_id == "RP-005"

    def test_numbers_several_proposals_consecutively(self, tmp_path: Path) -> None:
        path = tmp_path / "proposals.md"

        recorded = record_proposals(
            path,
            [_proposal(), _proposal(proposal_key="config:analysis.max_news_items")],
            AS_OF,
        )

        assert [item.rp_id for item in recorded] == ["RP-001", "RP-002"]
        assert len(_ledger_rows(path)) == 2

    def test_re_ingesting_the_same_proposal_does_not_duplicate_the_row(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "proposals.md"
        first = record_proposals(path, [_proposal()], AS_OF)

        second = record_proposals(path, [_proposal()], AS_OF)

        assert len(_ledger_rows(path)) == 1
        assert second[0].rp_id == first[0].rp_id
        assert (first[0].is_new, second[0].is_new) == (True, False)

    def test_a_reopened_proposal_gets_its_own_row(self, tmp_path: Path) -> None:
        path = tmp_path / "proposals.md"
        path.write_text(
            "| RP-ID | 日付 | level | proposal_key | タイトル | status | メモ |\n"
            "|---|---|---|---|---|---|---|\n"
            "| RP-001 | 2027-03-01 | L1 | config:postmortem.severe_threshold_pct "
            "| 旧案 | rejected | 却下 |\n",
            encoding="utf-8",
        )

        recorded = record_proposals(
            path, [_proposal(reopen_justification="新しい証拠")], AS_OF
        )

        assert recorded[0].rp_id == "RP-002"
        assert len(_ledger_rows(path)) == 2

    def test_preserves_prose_that_follows_the_table(self, tmp_path: Path) -> None:
        path = tmp_path / "proposals.md"
        path.write_text(
            "| RP-ID | status |\n|---|---|\n| RP-001 | proposed |\n\n末尾の注記\n",
            encoding="utf-8",
        )

        record_proposals(path, [_proposal()], AS_OF)

        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[-1] == "末尾の注記"
        assert lines[3].startswith("| RP-002 |")

    def test_escapes_a_cell_separator_inside_a_title(self, tmp_path: Path) -> None:
        path = tmp_path / "proposals.md"

        record_proposals(path, [_proposal(title="A | B の比較")], AS_OF)

        row = _ledger_rows(path)[0]
        assert r"A \| B の比較" in row
        # 8 columns stay 8: the escaped separator must not open a ninth cell.
        assert row.count("|") - row.count(r"\|") == 9

    def test_slugs_a_key_that_cannot_form_an_ascii_name(self, tmp_path: Path) -> None:
        path = tmp_path / "proposals.md"

        recorded = record_proposals(
            path,
            [_proposal(proposal_key="設計:../../脱出", title="日本語だけの題")],
            AS_OF,
        )

        document = recorded[0].document_path
        assert document.parent == tmp_path / PROPOSALS_SUBDIR
        assert document.name == "RP-001-proposal.md"

    def test_records_nothing_when_every_proposal_was_withheld(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "proposals.md"

        assert record_proposals(path, [], AS_OF) == ()
        assert not path.exists()

    def test_preserves_the_previous_ledger_when_the_write_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "proposals.md"
        record_proposals(path, [_proposal()], AS_OF)
        before = path.read_text(encoding="utf-8")

        def failing_replace(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("swing_copilot.io_atomic.os.replace", failing_replace)

        with pytest.raises(OSError, match="disk full"):
            record_proposals(
                path, [_proposal(proposal_key="config:analysis.max_news_items")], AS_OF
            )

        assert path.read_text(encoding="utf-8") == before
        assert list(tmp_path.glob(".proposals.md*")) == []


class TestCommittedLedger:
    """P8-33's committed empty ledger must stay the header `ingest` generates.

    The ledger is initialized in the repository so a first retrospective does
    not have to create it, but that only helps if the committed bytes are the
    ones `record_proposals` would have written (E32.1). Two hand-maintained
    copies of the same header would drift, and the drift would only surface as
    a malformed table after a real ingest.
    """

    def test_matches_the_header_record_proposals_generates(
        self, tmp_path: Path
    ) -> None:
        generated_path = tmp_path / "proposals.md"
        record_proposals(generated_path, [_proposal()], AS_OF)
        generated = generated_path.read_text(encoding="utf-8").splitlines()

        committed = COMMITTED_LEDGER.read_text(encoding="utf-8").splitlines()

        assert generated[: len(committed)] == committed
        # The only line the generation added is the proposal row itself: the
        # committed file is the whole header, not a truncated prefix of it.
        assert _ledger_rows(generated_path) == generated[len(committed) :]

    def test_is_parsed_as_an_existing_ledger_with_no_rows(self) -> None:
        state = read_ledger(COMMITTED_LEDGER)

        assert state.exists
        assert state.rows == ()
