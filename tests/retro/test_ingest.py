"""P8-32: `copilot-retro ingest` turns a verified result into a report + ledger.

Covers E32.5's mandatory matrix end to end: the happy path, the identity hard
fail, per-item withholding that spares its siblings, the re-proposal guard,
RP-ID numbering, and atomic replacement of both artifacts.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from swing_copilot.retro.export import RETRO_INPUT_FILENAME
from swing_copilot.retro.ingest import (
    RETRO_REPORT_FILENAME,
    RETRO_RESULT_FILENAME,
    RetroIngestRequest,
    ingest_retro_result,
)
from swing_copilot.retro.validate import RetroIngestError
from tests.retro.conftest import (
    SURPRISE_ID,
    narration_payload,
    proposal_payload,
    retro_input_payload,
    retro_result_payload,
)

if TYPE_CHECKING:
    from pathlib import Path

FORBIDDEN_TEXT = "この銘柄は今すぐ買うべき"


@pytest.fixture
def retro_dir(tmp_path: Path) -> Path:
    """`reports/retro/<as_of>/` holding the dossier the skill answered."""
    directory = tmp_path / "reports" / "retro" / "2027-03-29"
    directory.mkdir(parents=True)
    (directory / RETRO_INPUT_FILENAME).write_text(
        json.dumps(retro_input_payload()), encoding="utf-8"
    )
    return directory


def _write_result(retro_dir: Path, **overrides: Any) -> None:
    (retro_dir / RETRO_RESULT_FILENAME).write_text(
        json.dumps(retro_result_payload(**overrides)), encoding="utf-8"
    )


def _request(retro_dir: Path, tmp_path: Path) -> RetroIngestRequest:
    return RetroIngestRequest(
        retro_dir=retro_dir, ledger_path=tmp_path / "docs" / "retro" / "proposals.md"
    )


def _report(retro_dir: Path) -> str:
    return (retro_dir / RETRO_REPORT_FILENAME).read_text(encoding="utf-8")


class TestHappyPath:
    def test_renders_the_report_beside_the_dossier(
        self, retro_dir: Path, tmp_path: Path
    ) -> None:
        _write_result(retro_dir)

        summary = ingest_retro_result(_request(retro_dir, tmp_path))

        assert summary.report_path == (retro_dir / RETRO_REPORT_FILENAME).resolve()
        assert summary.withheld == ()

    def test_reports_the_evidence_the_retrospective_produced(
        self, retro_dir: Path, tmp_path: Path
    ) -> None:
        _write_result(retro_dir)

        ingest_retro_result(_request(retro_dir, tmp_path))

        report = _report(retro_dir)
        for expected in (
            "2027-03-29",
            "再点検の上で L2/L3 相当の構造的観察はなし",
            "AAPL",
            "information_absent",
            "当時の入力に材料が無く、後から出た開示に兆候が読める",
            "RP-001",
            "重大境界の見直し",
            "metric:separation:5d",
        ):
            assert expected in report

    def test_records_the_proposal_in_a_generated_ledger(
        self, retro_dir: Path, tmp_path: Path
    ) -> None:
        _write_result(retro_dir)
        request = _request(retro_dir, tmp_path)

        summary = ingest_retro_result(request)

        ledger = request.ledger_path.read_text(encoding="utf-8")
        assert "| RP-001 |" in ledger
        assert "proposed" in ledger
        assert [item.rp_id for item in summary.recorded] == ["RP-001"]
        assert summary.recorded[0].document_path.is_file()

    def test_carries_the_dossiers_data_quality_notes_into_the_report(
        self, retro_dir: Path, tmp_path: Path
    ) -> None:
        _write_result(retro_dir)

        ingest_retro_result(_request(retro_dir, tmp_path))

        assert "AAPL: 鮮度開示を取得できなかったため空欄" in _report(retro_dir)

    def test_re_ingesting_the_same_result_does_not_duplicate_the_ledger_row(
        self, retro_dir: Path, tmp_path: Path
    ) -> None:
        _write_result(retro_dir)
        request = _request(retro_dir, tmp_path)
        ingest_retro_result(request)

        summary = ingest_retro_result(request)

        rows = [
            line
            for line in request.ledger_path.read_text(encoding="utf-8").splitlines()
            if line.startswith("| RP-0")
        ]
        assert rows == [rows[0]]
        assert summary.recorded[0].rp_id == "RP-001"


class TestHardFailures:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("as_of", "2027-04-30", id="as-of"),
            pytest.param("input_digest", "a" * 64, id="input-digest"),
        ],
    )
    def test_refuses_a_result_that_answers_another_export(
        self, retro_dir: Path, tmp_path: Path, field: str, value: str
    ) -> None:
        _write_result(retro_dir, **{field: value})
        request = _request(retro_dir, tmp_path)

        with pytest.raises(RetroIngestError, match=field):
            ingest_retro_result(request)

        assert not (retro_dir / RETRO_REPORT_FILENAME).exists()
        assert not request.ledger_path.exists()

    def test_refuses_a_missing_result(self, retro_dir: Path, tmp_path: Path) -> None:
        with pytest.raises(RetroIngestError, match="could not be read"):
            ingest_retro_result(_request(retro_dir, tmp_path))

    def test_refuses_a_result_with_an_unknown_field(
        self, retro_dir: Path, tmp_path: Path
    ) -> None:
        _write_result(retro_dir, unexpected=1)

        with pytest.raises(RetroIngestError, match="schema validation"):
            ingest_retro_result(_request(retro_dir, tmp_path))


class TestWithholding:
    def test_withholds_only_the_proposal_with_a_fabricated_reference(
        self, retro_dir: Path, tmp_path: Path
    ) -> None:
        _write_result(
            retro_dir,
            proposals=[
                proposal_payload(evidence_refs=["metric:invented:9d"]),
                proposal_payload(
                    proposal_key="config:analysis.max_news_items", title="健全な案"
                ),
            ],
        )
        request = _request(retro_dir, tmp_path)

        summary = ingest_retro_result(request)

        assert [item.proposal.title for item in summary.recorded] == ["健全な案"]
        assert len(summary.withheld) == 1
        ledger = request.ledger_path.read_text(encoding="utf-8")
        assert "健全な案" in ledger
        assert "重大境界の見直し" not in ledger

    def test_reports_a_withheld_item_without_retrying_it(
        self, retro_dir: Path, tmp_path: Path
    ) -> None:
        _write_result(retro_dir, proposals=[proposal_payload(claim=FORBIDDEN_TEXT)])
        request = _request(retro_dir, tmp_path)

        summary = ingest_retro_result(request)

        report = _report(retro_dir)
        assert summary.recorded == ()
        assert "CON-03" in report
        assert FORBIDDEN_TEXT not in report
        assert not request.ledger_path.exists()

    def test_names_a_withheld_narration_in_the_report(
        self, retro_dir: Path, tmp_path: Path
    ) -> None:
        _write_result(
            retro_dir,
            narrations=[narration_payload(surprise_id="surprise:invented:ZZZ")],
        )

        summary = ingest_retro_result(_request(retro_dir, tmp_path))

        assert summary.narration_count == 0
        assert "surprise:invented:ZZZ" in _report(retro_dir)


class TestReproposalGuard:
    def _closed_ledger(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "| RP-ID | 日付 | level | proposal_key | タイトル | status | メモ |\n"
            "|---|---|---|---|---|---|---|\n"
            "| RP-004 | 2027-03-01 | L1 | config:postmortem.severe_threshold_pct "
            "| 旧案 | rejected | 却下 |\n",
            encoding="utf-8",
        )

    def test_sends_back_a_re_proposal_without_a_justification(
        self, retro_dir: Path, tmp_path: Path
    ) -> None:
        request = _request(retro_dir, tmp_path)
        self._closed_ledger(request.ledger_path)
        _write_result(retro_dir)

        summary = ingest_retro_result(request)

        assert summary.recorded == ()
        assert "reopen_justification" in summary.withheld[0].reason

    def test_accepts_a_re_proposal_that_justifies_itself_and_numbers_it_next(
        self, retro_dir: Path, tmp_path: Path
    ) -> None:
        request = _request(retro_dir, tmp_path)
        self._closed_ledger(request.ledger_path)
        _write_result(
            retro_dir,
            proposals=[proposal_payload(reopen_justification="新しい証拠が出た")],
        )

        summary = ingest_retro_result(request)

        assert [item.rp_id for item in summary.recorded] == ["RP-005"]


class TestAtomicWrites:
    def test_preserves_the_previous_report_when_the_write_fails(
        self, retro_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_result(retro_dir)
        request = _request(retro_dir, tmp_path)
        ingest_retro_result(request)
        before = _report(retro_dir)

        def failing_replace(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("swing_copilot.io_atomic.os.replace", failing_replace)
        _write_result(
            retro_dir, narrations=[narration_payload(narrative="書き換えられた叙述")]
        )

        with pytest.raises(OSError, match="disk full"):
            ingest_retro_result(request)

        assert _report(retro_dir) == before
        assert list(retro_dir.glob(".retro_report.md*")) == []

    def test_leaves_no_narration_section_when_the_result_narrates_nothing(
        self, retro_dir: Path, tmp_path: Path
    ) -> None:
        _write_result(retro_dir, narrations=[], proposals=[])

        summary = ingest_retro_result(_request(retro_dir, tmp_path))

        assert (summary.narration_count, summary.recorded) == (0, ())
        assert SURPRISE_ID not in _report(retro_dir)


class TestEarlyOperation:
    def test_says_so_when_no_verdict_has_matured_yet(self, tmp_path: Path) -> None:
        directory = tmp_path / "reports" / "retro" / "2027-03-29"
        directory.mkdir(parents=True)
        (directory / RETRO_INPUT_FILENAME).write_text(
            json.dumps(
                retro_input_payload(
                    aggregates={
                        "separation": [],
                        "proceed_severe_miss_rate": [],
                        "skip_hit_rate": [],
                        "verdict_mix": {
                            "metric_id": "verdict_mix",
                            "run_count": 0,
                            "verdict_count": 0,
                            "proceed_count": 0,
                            "skip_count": 0,
                            "proceed_ratio": None,
                            "is_flagged": False,
                        },
                    },
                    notes=[],
                )
            ),
            encoding="utf-8",
        )
        (directory / RETRO_RESULT_FILENAME).write_text(
            json.dumps(
                retro_result_payload(
                    input_digest=_digest_of(directory), narrations=[], proposals=[]
                )
            ),
            encoding="utf-8",
        )

        ingest_retro_result(_request(directory, tmp_path))

        report = _report(directory)
        assert "満期を迎えた verdict がまだ無い" in report
        assert "エクスポート時の注記" not in report

    def test_renders_a_design_proposal_without_a_verification_plan(
        self, retro_dir: Path, tmp_path: Path
    ) -> None:
        _write_result(
            retro_dir,
            proposals=[proposal_payload(level="L3", verification_plan=None)],
        )

        ingest_retro_result(_request(retro_dir, tmp_path))

        assert "（L3 のため個別計画なし）" in _report(retro_dir)

    def test_marks_a_metric_that_has_left_the_preliminary_range(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "reports" / "retro" / "2027-03-29"
        directory.mkdir(parents=True)
        aggregates = retro_input_payload()["aggregates"]
        aggregates["separation"][0] |= {"is_preliminary": False, "sample_size": 45}
        (directory / RETRO_INPUT_FILENAME).write_text(
            json.dumps(retro_input_payload(aggregates=aggregates)), encoding="utf-8"
        )
        (directory / RETRO_RESULT_FILENAME).write_text(
            json.dumps(
                retro_result_payload(input_digest=_digest_of(directory), proposals=[])
            ),
            encoding="utf-8",
        )

        ingest_retro_result(_request(directory, tmp_path))

        assert "| separation | 5日 | -0.90 | - | 45 | いいえ |" in _report(directory)


def _digest_of(directory: Path) -> str:
    """Read back the dossier's own digest so the result can copy it verbatim."""
    payload = json.loads((directory / RETRO_INPUT_FILENAME).read_text(encoding="utf-8"))
    return str(payload["input_digest"])
