"""Reading one on-disk document, with every read failure named by its boundary.

Turning a file into text has three ways to fail -- an unreadable path, bytes
that are not UTF-8, and (for JSON) text that is not JSON -- and callers tell a
broken artifact from an unexpected fault by the exception type alone. So each
boundary passes the domain error it wants and gets every failure back as that
type: `analysis/` raises `AnalysisIngestError`, `retro/` raises
`RetroIngestError`, `config.py` raises `ConfigError`.

The trap this module exists to close (Issue #153, then #164) is that
`UnicodeDecodeError` is a `ValueError`, not an `OSError`. A hand-rolled
`except OSError` at a call site therefore looks complete and still lets a
wrongly encoded file escape as a raw decode error -- which is exactly what an
unattended weekday run produces and nobody watches. Keeping one implementation
means a new reader cannot reopen that hole by omission.

It lives at package top level rather than under `analysis/` because `config.py`
is one of its callers, and a settings loader must not import the analysis
boundary to read a YAML file.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.exceptions import SwingCopilotError


def read_text_document(
    path: Path, *, label: str, error_type: type[SwingCopilotError]
) -> str:
    """Read one on-disk artifact as UTF-8 text, reporting failures as `error_type`.

    Args:
        path: The document to read.
        label: How the message names this kind of document, e.g. `"Report
            context"`; the failure reads `"<label> could not be read: <path>"`.
        error_type: The domain error this boundary's callers dispatch on.

    Returns:
        The decoded text.

    Raises:
        SwingCopilotError: An `error_type` instance -- the file could not be
            read or decoded as UTF-8.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"{label} could not be read: {path}"
        raise error_type(msg) from exc


def read_json_document(
    path: Path, *, label: str, error_type: type[SwingCopilotError]
) -> object:
    """Read one on-disk JSON artifact, reporting every read failure as `error_type`.

    The single implementation of "turn a file this pipeline wrote earlier into
    JSON". Only the exception type and the message prefix vary between
    boundaries, so both are parameters rather than a reason to copy the body.

    Args:
        path: The document to read.
        label: How the message names this kind of document, e.g. `"Report
            context"`; the failure reads `"<label> could not be read: <path>"`
            or `"<label> is not valid JSON: <path>"`.
        error_type: The domain error this boundary's callers dispatch on.

    Returns:
        The decoded JSON value, of whatever type the document holds.

    Raises:
        SwingCopilotError: An `error_type` instance -- the file could not be
            read, decoded as UTF-8, or parsed as JSON.
    """
    raw = read_text_document(path, label=label, error_type=error_type)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"{label} is not valid JSON: {path}"
        raise error_type(msg) from exc
