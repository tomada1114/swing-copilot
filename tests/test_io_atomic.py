"""Atomic replacement semantics at their dependency-zero home (Issue #193)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from swing_copilot.analysis import export
from swing_copilot.io_atomic import (
    write_bytes_atomically,
    write_json_atomically,
    write_json_batch_atomically,
    write_text_atomically,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def partial_write_then_fail(*, on_call: int) -> Callable[..., int]:
    """A `Path.write_text` that fills the file, then fails, on the Nth call.

    ENOSPC does not spare the file it was writing: what fits is already on
    disk when the error surfaces. A fake that raises *before* touching the
    filesystem therefore cannot observe a temporary file leaked by the very
    write that failed -- which is exactly the bug the batch writer had.

    Shared with the analysis-slice tests so both layers are held to the same
    failure shape.

    Args:
        on_call: Which call to `Path.write_text` fails, counting from 1.

    Returns:
        A drop-in replacement for `Path.write_text`.
    """
    original = Path.write_text
    calls: list[Path] = []

    def _write_text(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        calls.append(self)
        if len(calls) == on_call:
            original(self, data[: len(data) // 2], encoding="utf-8")
            msg = "disk full"
            raise OSError(msg)
        return original(self, data, encoding, errors, newline)

    return _write_text


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

    def test_a_failed_bytes_write_preserves_the_previous_file_and_leaves_no_temp(
        self, tmp_path, monkeypatch
    ):
        destination = tmp_path / "data.parquet"
        destination.write_bytes(b"previous-bytes")
        monkeypatch.setattr("swing_copilot.io_atomic.os.replace", self._explode)

        with pytest.raises(OSError, match="disk full"):
            write_bytes_atomically(destination, b"next-bytes")

        assert destination.read_bytes() == b"previous-bytes"
        assert list(tmp_path.glob(".*.tmp")) == []


class TestBytesWriter:
    """Issue #394: the bytes-body sibling of `write_text_atomically`."""

    def test_writes_the_given_bytes(self, tmp_path):
        destination = tmp_path / "data.parquet"

        write_bytes_atomically(destination, b"\x00\x01payload")

        assert destination.read_bytes() == b"\x00\x01payload"

    def test_a_rerun_replaces_the_previous_content(self, tmp_path):
        destination = tmp_path / "data.parquet"
        write_bytes_atomically(destination, b"generation-1")

        write_bytes_atomically(destination, b"generation-2")

        assert destination.read_bytes() == b"generation-2"

    def test_default_temporary_path_sits_beside_the_destination(
        self, tmp_path, monkeypatch
    ):
        destination = tmp_path / "data.parquet"
        seen: list[tuple[Path, Path]] = []

        def _record(source: str | Path, target: str | Path) -> None:
            seen.append((Path(source), Path(target)))
            Path(source).unlink()

        monkeypatch.setattr("swing_copilot.io_atomic.os.replace", _record)

        write_bytes_atomically(destination, b"body")

        (source, target) = seen[0]
        assert source == tmp_path / ".data.parquet.tmp"
        assert target == destination

    def test_a_caller_supplied_temporary_path_is_used_instead_of_the_default(
        self, tmp_path
    ):
        destination = tmp_path / "data.parquet"
        temporary_path = tmp_path / ".data.parquet.deadbeef.tmp"

        write_bytes_atomically(destination, b"body", temporary_path=temporary_path)

        assert destination.read_bytes() == b"body"
        assert not temporary_path.exists()

    def test_a_temporary_path_outside_the_destination_directory_is_rejected(
        self, tmp_path
    ):
        destination = tmp_path / "nested" / "data.parquet"
        destination.parent.mkdir()
        temporary_path = tmp_path / ".data.parquet.tmp"

        with pytest.raises(ValueError, match="temporary_path must be in"):
            write_bytes_atomically(destination, b"body", temporary_path=temporary_path)

        assert not destination.exists()
        assert not temporary_path.exists()

    def test_a_failed_replace_with_a_caller_supplied_temporary_path_cleans_it_up(
        self, tmp_path, monkeypatch
    ):
        destination = tmp_path / "data.parquet"
        destination.write_bytes(b"previous")
        temporary_path = tmp_path / ".data.parquet.deadbeef.tmp"

        def _explode(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("swing_copilot.io_atomic.os.replace", _explode)

        with pytest.raises(OSError, match="disk full"):
            write_bytes_atomically(destination, b"next", temporary_path=temporary_path)

        assert destination.read_bytes() == b"previous"
        assert not temporary_path.exists()


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


class TestBatchIsOneLogicalWrite:
    """A set of files commits or rolls back together (Issue #260 review)."""

    def test_every_destination_is_written(self, tmp_path):
        write_json_batch_atomically(
            [(tmp_path / "a.json", {"n": 1}), (tmp_path / "b.json", {"n": 2})]
        )

        assert json.loads((tmp_path / "a.json").read_text(encoding="utf-8")) == {"n": 1}
        assert json.loads((tmp_path / "b.json").read_text(encoding="utf-8")) == {"n": 2}

    def test_a_failure_partway_leaves_no_destination_and_no_temp(
        self, tmp_path, monkeypatch
    ):
        """The eighth file failing must not leave the first seven behind.

        The fake fills the file before raising, the way ENOSPC actually
        arrives: a fake that raises *before* creating anything cannot see a
        temporary left behind by the write that failed, which is the leak this
        test exists for.
        """
        monkeypatch.setattr(Path, "write_text", partial_write_then_fail(on_call=3))

        with pytest.raises(OSError, match="disk full"):
            write_json_batch_atomically(
                [(tmp_path / f"{index}.json", {"n": index}) for index in range(4)]
            )

        assert sorted(path.name for path in tmp_path.iterdir()) == []

    def test_an_existing_destination_survives_a_failed_batch(
        self, tmp_path, monkeypatch
    ):
        destination = tmp_path / "0.json"
        destination.write_text('{"previous": true}', encoding="utf-8")
        monkeypatch.setattr(Path, "write_text", partial_write_then_fail(on_call=1))

        with pytest.raises(OSError, match="disk full"):
            write_json_batch_atomically([(destination, {"next": True})])

        assert json.loads(destination.read_text(encoding="utf-8")) == {"previous": True}
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_an_empty_batch_writes_nothing(self, tmp_path):
        write_json_batch_atomically([])

        assert list(tmp_path.iterdir()) == []

    def test_the_batch_writes_the_same_bytes_as_the_single_file_writer(self, tmp_path):
        """One serialized form, so a batch cannot drift from a single write."""
        payload = {"note": "受注が伸びている"}
        write_json_atomically(tmp_path / "single.json", payload)

        write_json_batch_atomically([(tmp_path / "batch.json", payload)])

        assert (tmp_path / "batch.json").read_bytes() == (
            tmp_path / "single.json"
        ).read_bytes()


class TestCompatibilityFacade:
    def test_analysis_export_re_exports_the_same_functions(self):
        """Callers and design docs that name `analysis/export.py` still work."""
        assert export.write_json_atomically is write_json_atomically
        assert export.write_text_atomically is write_text_atomically
