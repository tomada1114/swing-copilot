"""`copilot-verify-analysis` end-to-end behavior (`analysis/verify_cli.py`)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ANALYSIS_RESULT_FILENAME,
)
from swing_copilot.analysis.schemas import AnalysisInput, AnalysisResult
from swing_copilot.analysis.snapshot import REPORT_CONTEXT_FILENAME
from swing_copilot.analysis.validate import AnalysisIngestError, validate_analysis
from swing_copilot.analysis.verify_cli import main, verify_paths
from swing_copilot.report.rejections import REJECTIONS_FILENAME
from tests.analysis.conftest import (
    AS_OF,
    NEWS_ID,
    NEWS_QUOTE,
    RUN_ID,
    fragment_payload,
    input_payload,
    result_payload,
    symbol_payload,
)

if TYPE_CHECKING:
    from pathlib import Path

#: A news payload whose imperative CON-03 violation withholds the symbol.
_VIOLATING_NEWS = {
    "facts": [
        {
            "text": "A new product line was announced.",
            "source_ids": [NEWS_ID],
            "evidence_quote": NEWS_QUOTE,
        }
    ],
    "interpretation": ["この銘柄は買うべきである。"],
    "risk_flags": [],
}


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A run directory holding the exported input and an empty `analysis_work/`."""
    directory = tmp_path / "reports" / AS_OF.isoformat() / RUN_ID
    (directory / "analysis_work").mkdir(parents=True)
    _dump(directory / ANALYSIS_INPUT_FILENAME, input_payload())
    return directory


def _dump(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _write_fragment(workdir: Path, name: str, payload: Any) -> Path:
    return _dump(workdir / "analysis_work" / name, payload)


def _run(*argv: str) -> int:
    with pytest.raises(SystemExit) as exit_info:
        main(list(argv))
    assert isinstance(exit_info.value.code, int)
    return exit_info.value.code


class TestFragmentChecking:
    def test_a_contract_satisfying_fragment_passes(self, workdir, capsys):
        path = _write_fragment(workdir, "news-AAPL.json", fragment_payload())

        code = _run(str(path))

        assert code == 0
        out = capsys.readouterr().out
        assert f"PASS {path} (fragment news/AAPL)" in out
        assert "1 document(s) checked, 1 passed, 0 failed" in out

    def test_a_con03_violation_fails_with_the_ingest_reason(self, workdir, capsys):
        path = _write_fragment(
            workdir, "news-AAPL.json", fragment_payload(news_summary=_VIOLATING_NEWS)
        )

        code = _run(str(path))

        assert code == 1
        assert "FAIL" in capsys.readouterr().out

    def test_a_schema_violation_names_the_invented_field(self, workdir, capsys):
        path = _write_fragment(
            workdir, "news-AAPL.json", fragment_payload(sentiment="positive")
        )

        code = _run(str(path))

        assert code == 1
        out = capsys.readouterr().out
        assert "Fragment failed schema validation" in out
        assert "sentiment" in out

    def test_a_filename_that_disagrees_with_the_payload_fails(self, workdir, capsys):
        path = _write_fragment(workdir, "news-MSFT.json", fragment_payload())

        code = _run(str(path))

        assert code == 1
        assert "filename declares symbol 'MSFT'" in capsys.readouterr().out

    def test_broken_json_is_a_failing_document_not_a_crash(self, workdir, capsys):
        path = workdir / "analysis_work" / "news-AAPL.json"
        path.write_text('{"symbol": "AAPL",', encoding="utf-8")

        code = _run(str(path))

        assert code == 1
        assert "is not valid JSON" in capsys.readouterr().out

    def test_a_json_document_that_is_not_an_object_is_rejected(self, workdir, capsys):
        path = _write_fragment(workdir, "news-AAPL.json", ["not", "an", "object"])

        code = _run(str(path))

        assert code == 1
        assert "is not a JSON object" in capsys.readouterr().out

    def test_a_path_that_does_not_exist_stops_before_reporting(self, workdir, capsys):
        code = _run(str(workdir / "analysis_work" / "news-NOPE.json"))

        assert code == 2
        assert capsys.readouterr().out == ""

    def test_the_same_path_named_twice_is_checked_once(self, workdir, capsys):
        path = _write_fragment(workdir, "news-AAPL.json", fragment_payload())

        code = _run(str(path), str(path))

        assert code == 0
        assert "1 document(s) checked" in capsys.readouterr().out


class TestDirectoryExpansion:
    def test_a_work_directory_checks_every_fragment_it_holds(self, workdir, capsys):
        good = _write_fragment(
            workdir, "screening-AAPL.json", fragment_payload("screening")
        )
        bad = _write_fragment(
            workdir, "news-AAPL.json", fragment_payload(news_summary=_VIOLATING_NEWS)
        )

        code = _run(str(workdir / "analysis_work"))

        assert code == 1
        out = capsys.readouterr().out
        assert f"PASS {good}" in out
        assert f"FAIL {bad}" in out
        assert "2 document(s) checked, 1 passed, 1 failed" in out

    def test_a_run_directory_skips_the_documents_code_owns(self, workdir, capsys):
        _dump(workdir / REPORT_CONTEXT_FILENAME, {"unreadable": True})
        _dump(workdir / REJECTIONS_FILENAME, {"unreadable": True})
        result = _dump(workdir / ANALYSIS_RESULT_FILENAME, result_payload())

        code = _run(str(workdir))

        assert code == 0
        out = capsys.readouterr().out
        assert f"PASS {result} (result)" in out
        assert "1 document(s) checked" in out

    def test_one_unreadable_entry_does_not_hide_its_siblings(self, workdir, capsys):
        good = _write_fragment(workdir, "news-AAPL.json", fragment_payload())
        unreadable = workdir / "analysis_work" / "filings-AAPL.json"
        unreadable.mkdir()

        code = _run(str(workdir / "analysis_work"))

        assert code == 1
        out = capsys.readouterr().out
        assert f"PASS {good}" in out
        assert (
            f"FAIL {unreadable} (rejected): Analysis document could not be read" in out
        )

    def test_a_wrongly_encoded_fragment_is_reported_rather_than_raised(
        self, workdir, capsys
    ):
        path = workdir / "analysis_work" / "news-AAPL.json"
        path.write_bytes(b'{"symbol": "\xff\xfe"}')

        code = _run(str(path))

        assert code == 1
        assert "could not be read" in capsys.readouterr().out

    def test_an_empty_directory_checks_nothing_and_passes(self, workdir, capsys):
        code = _run(str(workdir / "analysis_work"))

        assert code == 0
        assert "0 document(s) checked, 0 passed, 0 failed" in capsys.readouterr().out


class TestInputResolution:
    def test_the_input_is_found_from_the_parent_of_the_work_directory(
        self, workdir, capsys
    ):
        path = _write_fragment(workdir, "news-AAPL.json", fragment_payload())

        assert _run(str(path)) == 0
        assert "PASS" in capsys.readouterr().out

    def test_an_explicit_input_is_used_when_none_sits_beside_the_target(
        self, workdir, tmp_path, capsys
    ):
        stray = tmp_path / "elsewhere"
        stray.mkdir()
        path = _dump(stray / "news-AAPL.json", fragment_payload())

        code = _run(str(path), "--input", str(workdir / ANALYSIS_INPUT_FILENAME))

        assert code == 0
        assert "PASS" in capsys.readouterr().out

    def test_an_unlocatable_input_stops_the_run(self, tmp_path, capsys):
        stray = tmp_path / "elsewhere"
        stray.mkdir()
        path = _dump(stray / "news-AAPL.json", fragment_payload())

        assert _run(str(path)) == 2
        assert capsys.readouterr().out == ""

    def test_an_unreadable_input_stops_the_run_rather_than_failing_targets(
        self, workdir
    ):
        (workdir / ANALYSIS_INPUT_FILENAME).write_text("{", encoding="utf-8")
        _write_fragment(workdir, "news-AAPL.json", fragment_payload())

        with pytest.raises(AnalysisIngestError, match="not valid JSON"):
            verify_paths([workdir / "analysis_work"], None)


class TestResultDryRun:
    def test_a_valid_result_passes_without_writing_anything(self, workdir, capsys):
        result = _dump(workdir / ANALYSIS_RESULT_FILENAME, result_payload())
        before = {path: path.stat().st_mtime_ns for path in workdir.rglob("*")}

        code = _run(str(result))

        assert code == 0
        assert "PASS" in capsys.readouterr().out
        assert {path: path.stat().st_mtime_ns for path in workdir.rglob("*")} == before

    def test_a_withheld_symbol_is_reported_per_symbol(self, workdir, capsys):
        result = _dump(
            workdir / ANALYSIS_RESULT_FILENAME,
            result_payload(symbols=[symbol_payload(news_summary=_VIOLATING_NEWS)]),
        )

        code = _run(str(result))

        assert code == 1
        assert "FAIL" in capsys.readouterr().out

    def test_a_result_bound_to_another_run_is_rejected(self, workdir, capsys):
        result = _dump(
            workdir / ANALYSIS_RESULT_FILENAME,
            result_payload(run_id="99999999-9999-4999-8999-999999999999"),
        )

        code = _run(str(result))

        assert code == 1
        out = capsys.readouterr().out
        assert "(rejected)" in out
        assert "run_id" in out

    def test_a_result_that_drops_an_input_symbol_is_rejected(self, workdir, capsys):
        result = _dump(workdir / ANALYSIS_RESULT_FILENAME, result_payload(symbols=[]))

        code = _run(str(result))

        assert code == 1
        assert "must exactly match analysis_input candidates" in capsys.readouterr().out

    def test_an_archived_v2_result_is_rejected_like_a_live_ingest(
        self, workdir, capsys
    ):
        result = _dump(
            workdir / ANALYSIS_RESULT_FILENAME,
            result_payload(schema_version="analysis-result-v2"),
        )

        code = _run(str(result))

        assert code == 1
        assert "analysis-result-v3 is required" in capsys.readouterr().out


class TestVerificationStrengthMatchesIngest:
    """The pre-flight command must not be a weaker stand-in for ingest."""

    def test_the_fragment_and_the_result_report_the_same_violation(
        self, workdir, capsys
    ):
        fragment = _write_fragment(
            workdir, "news-AAPL.json", fragment_payload(news_summary=_VIOLATING_NEWS)
        )
        result = _dump(
            workdir / ANALYSIS_RESULT_FILENAME,
            result_payload(symbols=[symbol_payload(news_summary=_VIOLATING_NEWS)]),
        )

        assert _run(str(fragment)) == 1
        fragment_out = capsys.readouterr().out
        assert _run(str(result)) == 1
        result_out = capsys.readouterr().out

        reason = _ingest_reason(workdir)
        assert reason in fragment_out
        assert reason in result_out

    def test_the_dry_run_reports_exactly_what_ingest_would_withhold(self, workdir):
        result = _dump(
            workdir / ANALYSIS_RESULT_FILENAME,
            result_payload(symbols=[symbol_payload(news_summary=_VIOLATING_NEWS)]),
        )

        reports = verify_paths([result], None)

        assert [report.errors for report in reports] == [
            (f"AAPL: {_ingest_reason(workdir)}",)
        ]


def _ingest_reason(workdir: Path) -> str:
    """The reason `copilot-ingest-analysis` itself would log for `_VIOLATING_NEWS`."""
    analysis_input = AnalysisInput.model_validate(
        json.loads((workdir / ANALYSIS_INPUT_FILENAME).read_text(encoding="utf-8"))
    )
    result = AnalysisResult.model_validate(
        result_payload(symbols=[symbol_payload(news_summary=_VIOLATING_NEWS)])
    )
    error = validate_analysis(analysis_input, result).outcomes[0].error
    assert error is not None
    return error
