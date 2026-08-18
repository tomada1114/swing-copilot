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
    from pathlib import Path

__all__ = ["write_json_atomically", "write_text_atomically"]


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
    write_text_atomically(
        destination,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
    )


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
    tmp_path = destination.with_name(f".{destination.name}.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, destination)  # noqa: PTH105 - atomic replace by design
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
