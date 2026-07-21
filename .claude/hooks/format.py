# /// script
# requires-python = ">=3.12"
# ///
"""PostToolUse hook: format and lint the single Python file that was edited.

Reads the hook payload from stdin and runs ruff on the edited file only.
Exit code 2 feeds remaining (unfixable) violations back to Claude as context;
it does not block, because the edit has already happened.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Drop the wrapper script's own venv so the nested `uv run` targets .venv.
SUBPROCESS_ENV = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}


def main() -> int:
    """Run ruff fix + format on the file reported in the hook payload."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    raw_path = payload.get("tool_input", {}).get("file_path", "")
    if not raw_path or not raw_path.endswith(".py"):
        return 0

    file_path = Path(raw_path)
    project_dir = Path(payload.get("cwd", ".")).resolve()
    if not file_path.is_file() or not file_path.resolve().is_relative_to(project_dir):
        return 0

    failed = False
    for args in (
        ["uv", "run", "ruff", "check", "--fix", str(file_path)],
        ["uv", "run", "ruff", "format", str(file_path)],
    ):
        result = subprocess.run(  # noqa: S603
            args, capture_output=True, text=True, check=False, env=SUBPROCESS_ENV
        )
        if result.returncode != 0:
            failed = True
            sys.stderr.write(result.stdout)
            sys.stderr.write(result.stderr)

    # Exit 2 surfaces the remaining violations to Claude so it can fix them.
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
