"""Atomic file replacement (AGENTS.md "Storage, correction, and atomicity").

"Write a temporary file in the destination's own directory, then `os.replace`"
is a repository-wide invariant, not one package's concern: screening,
regime, report, retro and analysis all replace files this way. The two
writers therefore live here, in a module that imports nothing from
`swing_copilot`, so wanting an atomic write never means depending on
`analysis`.

`swing_copilot.analysis.export` re-exports both names for the callers (and
docs) that have always found them there.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = [
    "write_json_atomically",
    "write_json_batch_atomically",
    "write_text_atomically",
]


def write_json_atomically(destination: Path, payload: object) -> None:
    """Replace `destination` with `payload` as JSON, all-or-nothing.

    Uses a temporary file in the destination's own directory plus
    `os.replace`, so a failure mid-write preserves the previous destination
    and leaves no temporary artifact behind.

    Args:
        destination: Final path to (re)write.
        payload: Any JSON-serializable object.

    Raises:
        OSError: Serialization/write/replace failed.
    """
    write_text_atomically(destination, _render_json(payload))


def write_json_batch_atomically(items: Sequence[tuple[Path, object]]) -> None:
    """Write many JSON files as one logical write.

    AGENTS.md requires a logical multi-row write to commit or roll back as a
    whole; a command that produces a *set* of files owes its caller the same
    thing. Writing them one at a time does not: a batch that dies on its
    eighth file leaves seven behind while the command reports failure, and the
    caller -- which is told nothing was produced -- never learns they are
    there.

    So everything is serialized up front, the content is then written into a
    temporary file beside each destination, and only then are the temporaries
    renamed into place. The failure a batch actually hits (running out of
    space, or reaching an unwritable destination partway through) therefore
    happens in phase one, where no destination has been touched and every
    temporary is removed again -- including the one being written when the
    failure struck, which is why each path is recorded *before* it is opened
    rather than after a successful write. ENOSPC does not spare the file it
    was filling.

    The rename phase is deliberately not wrapped in a second transaction:
    `os.replace` within one directory allocates nothing and each temporary is
    already proven writable there, so it can essentially only fail for a cause
    that would have failed phase one. Should it still fail, the destinations
    renamed so far stay -- each one complete, none half-written -- and the
    temporaries not yet renamed are removed.

    Args:
        items: `(destination, payload)` pairs. Each destination's directory
            must exist. Destinations must be distinct; two pairs naming one
            path would stage into the same temporary file.

    Raises:
        OSError: Writing or replacing failed.
        TypeError: A payload is not JSON-serializable. Raised while rendering,
            before any file exists, so there is nothing to undo.
    """
    rendered = [(destination, _render_json(payload)) for destination, payload in items]
    staged: list[tuple[Path, Path]] = []
    try:
        for destination, content in rendered:
            tmp_path = _temporary_path(destination)
            staged.append((tmp_path, destination))
            tmp_path.write_text(content, encoding="utf-8")
        for tmp_path, destination in staged:
            os.replace(tmp_path, destination)  # noqa: PTH105 - atomic by design
    except OSError:
        for tmp_path, _ in staged:
            tmp_path.unlink(missing_ok=True)
        raise


def _render_json(payload: object) -> str:
    """The canonical serialized form, defined once for both writers."""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def _temporary_path(destination: Path) -> Path:
    """The staging path for `destination`, in its own directory."""
    return destination.with_name(f".{destination.name}.tmp")


def write_text_atomically(destination: Path, content: str) -> None:
    """Replace `destination` with `content`, all-or-nothing.

    The same guarantee `write_json_atomically` gives, for the documents that
    are rendered rather than serialized (the retrospective's report and its
    proposal ledger).

    Args:
        destination: Final path to (re)write. Its directory must exist.
        content: The complete text to write.

    Raises:
        OSError: Writing or replacing failed. The previous destination is left
            untouched and the temporary artifact is removed.
    """
    tmp_path = _temporary_path(destination)
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, destination)  # noqa: PTH105 - atomic replace by design
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
