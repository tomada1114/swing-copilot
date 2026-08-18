"""Atomic replacement semantics at their dependency-zero home (Issue #193)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swing_copilot.analysis import export
from swing_copilot.io_atomic import write_json_atomically, write_text_atomically


class TestTemporaryFileLocation:
    def test_the_temporary_file_sits_in_the_destinations_own_directory(
        self, tmp_path, monkeypatch
    ):
        """AGENTS.md: `os.replace` must be a rename inside one directory."""
        destination = tmp_path / "nested" / "report.md"
        destination.parent.mkdir()
        seen: list[tuple[Path, Path]] = []

        def _record(source: str | Path, target: str | Path) -> None:
            seen.append((Path(source), Path(target)))
            Path(source).unlink()

        monkeypatch.setattr("swing_copilot.io_atomic.os.replace", _record)

        write_text_atomically(destination, "body")

        (source, target) = seen[0]
        assert source.parent == destination.parent
        assert target == destination


class TestFailureIsAllOrNothing:
    @staticmethod
    def _explode(*_args: object, **_kwargs: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    def test_a_failed_text_write_preserves_the_previous_file_and_leaves_no_temp(
        self, tmp_path, monkeypatch
    ):
        destination = tmp_path / "ledger.md"
        destination.write_text("previous", encoding="utf-8")
        monkeypatch.setattr("swing_copilot.io_atomic.os.replace", self._explode)

        with pytest.raises(OSError, match="disk full"):
            write_text_atomically(destination, "next")

        assert destination.read_text(encoding="utf-8") == "previous"
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_a_failed_json_write_preserves_the_previous_file_and_leaves_no_temp(
        self, tmp_path, monkeypatch
    ):
        destination = tmp_path / "payload.json"
        destination.write_text('{"previous": true}', encoding="utf-8")
        monkeypatch.setattr("swing_copilot.io_atomic.os.replace", self._explode)

        with pytest.raises(OSError, match="disk full"):
            write_json_atomically(destination, {"new": True})

        assert json.loads(destination.read_text(encoding="utf-8")) == {"previous": True}
        assert list(tmp_path.glob(".*.tmp")) == []


class TestSerializedForm:
    def test_json_is_written_unescaped_and_indented_with_a_trailing_newline(
        self, tmp_path
    ):
        destination = tmp_path / "payload.json"

        write_json_atomically(destination, {"note": "受注が伸びている"})

        assert destination.read_text(encoding="utf-8") == (
            '{\n  "note": "受注が伸びている"\n}\n'
        )

    def test_a_rerun_replaces_the_previous_content(self, tmp_path):
        destination = tmp_path / "payload.json"
        write_json_atomically(destination, {"generation": 1})

        write_json_atomically(destination, {"generation": 2})

        assert json.loads(destination.read_text(encoding="utf-8")) == {"generation": 2}


class TestCompatibilityFacade:
    def test_analysis_export_re_exports_the_same_functions(self):
        """Callers and design docs that name `analysis/export.py` still work."""
        assert export.write_json_atomically is write_json_atomically
        assert export.write_text_atomically is write_text_atomically
