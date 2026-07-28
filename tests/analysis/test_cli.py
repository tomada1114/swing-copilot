"""`copilot-ingest-analysis` end-to-end behavior (`analysis/cli.py`)."""

from __future__ import annotations

import json
import socket
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from swing_copilot.analysis.cli import ingest, main
from swing_copilot.analysis.snapshot import ReportContext, write_report_context
from swing_copilot.models import RunStatus
from swing_copilot.report.daily_brief import (
    NO_TRADE_MESSAGE,
    PENDING_ANALYSIS_MESSAGE,
    BriefAnalysis,
)
from tests.analysis.conftest import AS_OF, result_payload, symbol_payload
from tests.analysis.test_snapshot import _populated_brief

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def report_dir(tmp_path: Path) -> Path:
    return tmp_path / "reports" / AS_OF.isoformat()


@pytest.fixture
def archived_context(tmp_path: Path, report_dir: Path) -> Path:
    """A daily-run report context whose candidate is still pending analysis."""
    brief = _populated_brief()
    pending = replace(
        brief,
        candidates=(
            replace(
                brief.candidates[0],
                analysis=BriefAnalysis(True, PENDING_ANALYSIS_MESSAGE),
            ),
        ),
        no_trade=False,
        no_trade_reason=None,
    )
    return write_report_context(
        ReportContext(pending, RunStatus.SUCCESS, tmp_path / "reports"), report_dir
    )


class TestIngestRewritesTheReport:
    def test_verified_analysis_replaces_the_pending_placeholder(
        self,
        write_documents,
        archived_context,
        capsys,
    ):
        input_path, result_path = write_documents()

        report_path = ingest(input_path, result_path, archived_context)

        markdown = report_path.read_text(encoding="utf-8")
        assert PENDING_ANALYSIS_MESSAGE not in markdown
        assert "Survived on trend quality with adequate liquidity." in markdown
        assert "✓ 定性: 懸念なし" in markdown
        assert "### 定性評価" in markdown
        assert "- 強み: Trend intact" in markdown
        assert "[finnhub:1](https://example.com/news)" in markdown
        assert "### 開示分析: 10-Q (2027-02-20)" in markdown
        # The terminal summary is written to stdout for the operator.
        assert "定性: Survived on trend quality" in capsys.readouterr().out

    def test_deterministic_screening_output_is_never_rewritten(
        self,
        write_documents,
        archived_context,
    ):
        input_path, result_path = write_documents(
            None,
            result_payload(
                symbols=[symbol_payload(verdict={"recommendation": "skip"})]
            ),
        )

        markdown = ingest(input_path, result_path, archived_context).read_text(
            encoding="utf-8"
        )

        # Score, sizing, and execution state come from the archived brief.
        assert "| Total: 0.812" not in markdown
        assert "0.812" in markdown
        assert "128株（制約: リスク1.0%）" in markdown
        assert "FAIR (d=1.20)" in markdown

    def test_a_skip_verdict_is_shown_with_its_leading_reason(
        self,
        write_documents,
        archived_context,
    ):
        payload = symbol_payload(
            verdict={
                "recommendation": "skip",
                "reasons": [{"text": "Guidance was withdrawn.", "source_ids": []}],
            }
        )
        input_path, result_path = write_documents(
            None, result_payload(symbols=[payload])
        )

        markdown = ingest(input_path, result_path, archived_context).read_text(
            encoding="utf-8"
        )

        assert "⚠ 定性: 見送り推奨（Guidance was withdrawn.）" in markdown

    def test_no_trade_is_announced_before_anything_else(
        self,
        write_documents,
        archived_context,
        capsys,
    ):
        input_path, result_path = write_documents(
            None, result_payload(no_trade=True, no_trade_reason="地合いが不安定。")
        )

        markdown = ingest(input_path, result_path, archived_context).read_text(
            encoding="utf-8"
        )

        banner_index = markdown.index(NO_TRADE_MESSAGE)
        assert banner_index < markdown.index("## Market")
        assert "地合いが不安定。" in markdown
        terminal = capsys.readouterr().out
        assert terminal.index(NO_TRADE_MESSAGE) < terminal.index("即検討可")

    def test_a_withheld_symbol_shows_the_verification_notice_only(
        self,
        write_documents,
        archived_context,
    ):
        payload = symbol_payload(
            screening_assessment={
                "summary": "今すぐ買うべき局面。",
                "strengths": [],
                "concerns": [],
            }
        )
        input_path, result_path = write_documents(
            None, result_payload(symbols=[payload])
        )

        markdown = ingest(input_path, result_path, archived_context).read_text(
            encoding="utf-8"
        )

        assert "検証不合格のため非表示" in markdown
        assert "今すぐ買うべき局面。" not in markdown
        assert "定性: 懸念なし" not in markdown

    def test_a_symbol_missing_from_the_result_is_marked_as_unanalyzed(
        self,
        write_documents,
        archived_context,
    ):
        input_path, result_path = write_documents(None, result_payload(symbols=[]))

        markdown = ingest(input_path, result_path, archived_context).read_text(
            encoding="utf-8"
        )

        assert "定性分析なし" in markdown


class TestIngestIsOffline:
    def test_it_never_opens_a_socket(self, write_documents, archived_context):
        # The autouse guard in `tests/conftest.py` already fails any real
        # connect(); this asserts the contract explicitly for the entry point
        # the skill invokes.
        input_path, result_path = write_documents()

        ingest(input_path, result_path, archived_context)

        assert socket.socket.connect.__name__ == "blocked_connect"


class TestCliEntryPoint:
    @pytest.mark.usefixtures("archived_context")
    def test_it_defaults_the_sibling_input_and_context_paths(
        self,
        write_documents,
        capsys,
    ):
        _input_path, result_path = write_documents()

        with pytest.raises(SystemExit) as exit_info:
            main([str(result_path)])

        assert exit_info.value.code == 0
        assert "定性:" in capsys.readouterr().out

    @pytest.mark.usefixtures("archived_context")
    def test_a_directory_argument_resolves_to_the_result_file(
        self,
        write_documents,
        report_dir,
    ):
        write_documents()

        with pytest.raises(SystemExit) as exit_info:
            main([str(report_dir)])

        assert exit_info.value.code == 0

    @pytest.mark.usefixtures("archived_context")
    def test_an_as_of_mismatch_exits_nonzero_without_rewriting(
        self,
        write_documents,
        tmp_path,
        caplog,
    ):
        _input_path, result_path = write_documents(
            None, result_payload(as_of="2027-03-02")
        )

        with pytest.raises(SystemExit) as exit_info:
            main([str(result_path)])

        assert exit_info.value.code == 1
        assert "analysis ingest failed" in caplog.text
        assert not list((tmp_path / "reports").glob("*.md"))

    def test_a_missing_result_file_exits_nonzero(self, tmp_path):
        with pytest.raises(SystemExit) as exit_info:
            main([str(tmp_path / "analysis_result.json")])

        assert exit_info.value.code == 1

    def test_explicit_input_and_context_paths_are_honored(
        self,
        write_documents,
        archived_context,
        tmp_path,
    ):
        input_path, result_path = write_documents()
        moved = tmp_path / "elsewhere.json"
        moved.write_text(result_path.read_text(encoding="utf-8"), encoding="utf-8")

        with pytest.raises(SystemExit) as exit_info:
            main(
                [
                    str(moved),
                    "--input",
                    str(input_path),
                    "--context",
                    str(archived_context),
                    "--log-level",
                    "DEBUG",
                ]
            )

        assert exit_info.value.code == 0


class TestRewrittenReportLocation:
    def test_it_overwrites_the_same_archive_the_daily_run_produced(
        self,
        write_documents,
        archived_context,
        tmp_path,
    ):
        input_path, result_path = write_documents()

        report_path = ingest(input_path, result_path, archived_context)

        context_payload = json.loads(archived_context.read_text(encoding="utf-8"))
        run_id = context_payload["brief"]["run_id"]
        assert report_path == tmp_path / "reports" / AS_OF.isoformat() / f"{run_id}.md"
        assert (tmp_path / "reports" / "latest.md").is_file()
