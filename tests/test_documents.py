"""Tests for the shared on-disk document readers.

The contract under test is exception translation: every way a file fails to
become text or JSON must leave as the boundary's own domain error, so a caller
that dispatches on the type never sees a raw `OSError`, `UnicodeDecodeError`,
or `JSONDecodeError` leak through.
"""

from __future__ import annotations

import pytest

from swing_copilot.documents import read_json_document, read_text_document
from swing_copilot.exceptions import ConfigError, SwingCopilotError


class _BoundaryError(SwingCopilotError):
    """Stand-in for a caller's domain error, distinct from every real one."""


class TestReadTextDocument:
    def test_returns_the_decoded_text(self, tmp_path):
        path = tmp_path / "doc.txt"
        path.write_text("こんにちは\n", encoding="utf-8")

        assert read_text_document(path, label="Doc", error_type=_BoundaryError) == (
            "こんにちは\n"
        )

    def test_missing_file_raises_the_callers_error_type(self, tmp_path):
        with pytest.raises(_BoundaryError, match=r"Doc could not be read: "):
            read_text_document(
                tmp_path / "absent.txt", label="Doc", error_type=_BoundaryError
            )

    def test_non_utf8_bytes_raise_the_callers_error_type(self, tmp_path):
        # `UnicodeDecodeError` is a `ValueError`, not an `OSError`, which is
        # how it escaped hand-rolled `except OSError` guards (Issue #164).
        path = tmp_path / "doc.txt"
        path.write_bytes("これはUTF-8ではない".encode("shift_jis"))

        with pytest.raises(_BoundaryError, match=r"Doc could not be read: "):
            read_text_document(path, label="Doc", error_type=_BoundaryError)

    def test_error_type_is_honoured_per_boundary(self, tmp_path):
        with pytest.raises(ConfigError):
            read_text_document(
                tmp_path / "absent.txt", label="Doc", error_type=ConfigError
            )


class TestReadJsonDocument:
    def test_returns_the_decoded_value(self, tmp_path):
        path = tmp_path / "doc.json"
        path.write_text('{"a": 1}', encoding="utf-8")

        assert read_json_document(path, label="Doc", error_type=_BoundaryError) == {
            "a": 1
        }

    def test_unreadable_file_reuses_the_text_failure_message(self, tmp_path):
        with pytest.raises(_BoundaryError, match=r"Doc could not be read: "):
            read_json_document(
                tmp_path / "absent.json", label="Doc", error_type=_BoundaryError
            )

    def test_non_utf8_bytes_raise_the_callers_error_type(self, tmp_path):
        path = tmp_path / "doc.json"
        path.write_bytes('{"a": "日本語"}'.encode("shift_jis"))

        with pytest.raises(_BoundaryError, match=r"Doc could not be read: "):
            read_json_document(path, label="Doc", error_type=_BoundaryError)

    def test_malformed_json_is_reported_separately_from_a_read_failure(self, tmp_path):
        path = tmp_path / "doc.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(_BoundaryError, match=r"Doc is not valid JSON: "):
            read_json_document(path, label="Doc", error_type=_BoundaryError)
