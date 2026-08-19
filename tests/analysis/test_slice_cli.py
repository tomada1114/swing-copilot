"""`copilot-export-slices` end-to-end behavior (`analysis/slice_cli.py`)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from swing_copilot.analysis.export import ANALYSIS_INPUT_FILENAME
from swing_copilot.analysis.slice_cli import export_slices, main
from swing_copilot.analysis.slices import InputSlice
from tests.analysis.conftest import AS_OF, RUN_ID, input_payload
from tests.analysis.test_slices import candidate_payload, mixed_payload

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A run directory holding an exported input with three candidates."""
    directory = tmp_path / "reports" / AS_OF.isoformat() / RUN_ID
    directory.mkdir(parents=True)
    _dump(directory / ANALYSIS_INPUT_FILENAME, mixed_payload())
    return directory


def _dump(path: Path, payload: Any) -> Path:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


def test_main_writes_every_slice_and_lists_them_with_their_body_size(
    workdir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "scratch" / "slices"

    with pytest.raises(SystemExit) as exit_info:
        main([str(workdir / ANALYSIS_INPUT_FILENAME), "--out-dir", str(out_dir)])

    assert exit_info.value.code == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("\t")[1:3] for line in lines[:-1]] == [
        ["news", "AAPL"],
        ["news", "MSFT"],
        ["filings", "AAPL"],
        ["screening", "AAPL"],
        ["screening", "MSFT"],
        ["screening", "NVDA"],
    ]
    assert lines[-1] == "6 slice(s) written: news=2 filings=1 screening=3"
    assert sorted(path.name for path in out_dir.iterdir()) == [
        "slice-filings-AAPL.json",
        "slice-news-AAPL.json",
        "slice-news-MSFT.json",
        "slice-screening-AAPL.json",
        "slice-screening-MSFT.json",
        "slice-screening-NVDA.json",
    ]


def test_main_accepts_the_run_directory_instead_of_the_input_file(
    workdir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "slices"

    with pytest.raises(SystemExit) as exit_info:
        main([str(workdir), "--out-dir", str(out_dir)])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("6 slice(s) written")


def test_the_listed_paths_are_absolute_and_hold_the_written_slice(
    workdir: Path, tmp_path: Path
) -> None:
    """The orchestrator hands these paths to subagents, so they must resolve."""
    exported = export_slices(workdir, tmp_path / "slices")

    for document, path in exported:
        assert path.is_absolute()
        parsed = InputSlice.model_validate(json.loads(path.read_text(encoding="utf-8")))
        assert (parsed.kind, parsed.candidate.symbol) == (
            document.kind,
            document.symbol,
        )


def test_rerunning_the_export_rewrites_the_same_bytes(
    workdir: Path, tmp_path: Path
) -> None:
    out_dir = tmp_path / "slices"

    first = [path.read_bytes() for _, path in export_slices(workdir, out_dir)]
    second = [path.read_bytes() for _, path in export_slices(workdir, out_dir)]

    assert first == second


def test_a_missing_input_document_fails_without_writing_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "slices"

    with pytest.raises(SystemExit) as exit_info:
        main([str(tmp_path / "absent.json"), "--out-dir", str(out_dir)])

    assert exit_info.value.code == 1
    assert "slice export failed" in capsys.readouterr().err
    assert not out_dir.exists()


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param("[]", "not a JSON object", id="json-array"),
        pytest.param("{oops", "not valid JSON", id="broken-json"),
    ],
)
def test_a_document_that_is_not_an_analysis_input_object_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], document: str, expected: str
) -> None:
    _dump(tmp_path / ANALYSIS_INPUT_FILENAME, document)

    with pytest.raises(SystemExit) as exit_info:
        main([str(tmp_path), "--out-dir", str(tmp_path / "slices")])

    assert exit_info.value.code == 1
    assert expected in capsys.readouterr().err


def test_an_input_violating_its_own_schema_fails_before_any_slice_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = input_payload(candidates=[candidate_payload("AAPL")])
    payload["candidates"][0]["unknown_field"] = "x"
    _dump(tmp_path / ANALYSIS_INPUT_FILENAME, payload)
    out_dir = tmp_path / "slices"

    with pytest.raises(SystemExit) as exit_info:
        main([str(tmp_path), "--out-dir", str(out_dir)])

    assert exit_info.value.code == 1
    assert "failed schema validation" in capsys.readouterr().err
    assert not out_dir.exists()


def test_the_output_directory_is_required(workdir: Path) -> None:
    """Slices belong in the session scratchpad, never beside the fragments."""
    with pytest.raises(SystemExit) as exit_info:
        main([str(workdir)])

    assert exit_info.value.code == 2
