"""`copilot-export-slices` end-to-end behavior (`analysis/slice_cli.py`)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from swing_copilot.analysis.export import ANALYSIS_INPUT_FILENAME
from swing_copilot.analysis.slice_cli import export_slices, main
from swing_copilot.analysis.slices import InputSlice
from tests.analysis.conftest import AS_OF, RUN_ID, input_payload
from tests.analysis.test_slices import candidate_payload, mixed_payload

if TYPE_CHECKING:
    from pathlib import Path

#: The console script's body, run in a fresh interpreter. Importing `main` is
#: what the `copilot-export-slices` entry point itself does, so this exercises
#: the same code path without depending on the wheel being installed.
_ENTRY_POINT = "from swing_copilot.analysis.slice_cli import main; main()"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A run directory holding an exported input with four candidates."""
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
        ["news", "NVDA"],
        ["filings", "AAPL"],
        ["screening", "AAPL"],
        ["screening", "MSFT"],
        ["screening", "NVDA"],
        ["screening", "TSLA"],
    ]
    assert lines[-1] == "8 slice(s) written: news=3 filings=1 screening=4"
    assert sorted(path.name for path in out_dir.iterdir()) == [
        "slice-filings-AAPL.json",
        "slice-news-AAPL.json",
        "slice-news-MSFT.json",
        "slice-news-NVDA.json",
        "slice-screening-AAPL.json",
        "slice-screening-MSFT.json",
        "slice-screening-NVDA.json",
        "slice-screening-TSLA.json",
    ]


def test_main_accepts_the_run_directory_instead_of_the_input_file(
    workdir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "slices"

    with pytest.raises(SystemExit) as exit_info:
        main([str(workdir), "--out-dir", str(out_dir)])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("8 slice(s) written")


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


def test_two_fresh_interpreters_write_the_same_bytes(
    workdir: Path, tmp_path: Path
) -> None:
    """Fix the property Issue #261 actually depends on: across processes.

    Two builds inside one process share a hash seed and every module-level
    object, so they cannot see the failure mode that matters -- a set or dict
    ordering that differs from run to run and silently changes a slice's
    bytes, invalidating a body hash that was supposed to prove "unchanged".
    Running the entry point twice, in separate interpreters under different
    `PYTHONHASHSEED` values, is what makes that observable.
    """
    written: list[dict[str, bytes]] = []
    for seed, name in (("0", "first"), ("524287", "second")):
        out_dir = tmp_path / name
        result = subprocess.run(  # noqa: S603 - fixed interpreter, static code
            [
                sys.executable,
                "-c",
                _ENTRY_POINT,
                str(workdir / ANALYSIS_INPUT_FILENAME),
                "--out-dir",
                str(out_dir),
            ],
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        written.append({path.name: path.read_bytes() for path in out_dir.iterdir()})

    assert written[0] == written[1]
    assert len(written[0]) == 8


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
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _dump(run_dir / ANALYSIS_INPUT_FILENAME, document)

    with pytest.raises(SystemExit) as exit_info:
        main([str(run_dir), "--out-dir", str(tmp_path / "slices")])

    assert exit_info.value.code == 1
    assert expected in capsys.readouterr().err


def test_an_input_violating_its_own_schema_fails_before_any_slice_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    payload = input_payload(candidates=[candidate_payload("AAPL")])
    payload["candidates"][0]["unknown_field"] = "x"
    _dump(run_dir / ANALYSIS_INPUT_FILENAME, payload)
    out_dir = tmp_path / "slices"

    with pytest.raises(SystemExit) as exit_info:
        main([str(run_dir), "--out-dir", str(out_dir)])

    assert exit_info.value.code == 1
    assert "failed schema validation" in capsys.readouterr().err
    assert not out_dir.exists()


@pytest.mark.parametrize(
    ("relative_out_dir", "expected"),
    [
        pytest.param(".", "is inside the run directory", id="the-run-directory"),
        pytest.param(
            "analysis_work", "is inside the run directory", id="beneath-the-run"
        ),
        pytest.param("..", "contains the run directory", id="above-the-run"),
    ],
)
def test_an_out_dir_in_the_operators_output_tree_is_refused(
    workdir: Path,
    capsys: pytest.CaptureFixture[str],
    relative_out_dir: str,
    expected: str,
) -> None:
    """Requiring `--out-dir` states the rule; this one enforces it.

    Nothing stops a caller from passing back the very path it read the input
    from, and the workflow never deletes what lands there, so the run
    directory would collect `slice-*.json` one run at a time.
    """
    out_dir = workdir / relative_out_dir

    with pytest.raises(SystemExit) as exit_info:
        main([str(workdir), "--out-dir", str(out_dir)])

    assert exit_info.value.code == 1
    assert expected in capsys.readouterr().err
    assert not list(workdir.glob("slice-*.json"))


def test_an_out_dir_holding_another_runs_input_is_refused(
    workdir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other_run = tmp_path / "elsewhere"
    other_run.mkdir()
    _dump(other_run / ANALYSIS_INPUT_FILENAME, mixed_payload())

    with pytest.raises(SystemExit) as exit_info:
        main([str(workdir), "--out-dir", str(other_run)])

    assert exit_info.value.code == 1
    assert "is a run directory of its own" in capsys.readouterr().err
    assert not list(other_run.glob("slice-*.json"))


def test_a_scratch_directory_beside_the_run_is_accepted(
    workdir: Path, tmp_path: Path
) -> None:
    """The positive control: an unrelated directory is not refused."""
    exported = export_slices(workdir, tmp_path / "scratch" / "slices")

    assert len(exported) == 8


def test_the_output_directory_is_required(workdir: Path) -> None:
    """Slices belong in the session scratchpad, never beside the fragments."""
    with pytest.raises(SystemExit) as exit_info:
        main([str(workdir)])

    assert exit_info.value.code == 2
