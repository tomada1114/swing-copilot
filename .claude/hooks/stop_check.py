# /// script
# requires-python = ">=3.12"
# ///
"""Stop hook: lightweight quality gate before Claude ends its turn.

Runs the same ruff lint + format checks and mypy as `just lint` (no tests —
those stay in `just check` and CI) whenever the working tree contains
modified Python files or pyproject.toml. Exit code 2 blocks the stop and
feeds the failures back to Claude so it fixes them before declaring the
turn done.

The hook's wall-clock budget is the `timeout` on the Stop hook entry in
.claude/settings.json; a timed-out hook is skipped, not blocking. If this
template grows into a project whose cold-cache whole-tree mypy run exceeds
that budget, raise the timeout there.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

CHECKS = (
    ["uv", "run", "ruff", "check", "."],
    ["uv", "run", "ruff", "format", "--check", "."],
    ["uv", "run", "mypy", "src", "scripts", "tests"],
)

# Drop the wrapper script's own venv so the nested `uv run` targets .venv.
SUBPROCESS_ENV = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}


def _python_files_changed() -> bool:
    """Return True when uncommitted changes touch Python code or its config."""
    result = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        path = line[3:].split(" -> ")[-1].strip('"')
        if path.endswith(".py") or path == "pyproject.toml":
            return True
    return False


def main() -> int:
    """Run the lint/type gate unless this stop is a hook-driven continuation."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    # A previous block already continued the turn once — never loop.
    if payload.get("stop_hook_active"):
        return 0

    if not _python_files_changed():
        return 0

    for args in CHECKS:
        result = subprocess.run(  # noqa: S603
            args, capture_output=True, text=True, check=False, env=SUBPROCESS_ENV
        )
        if result.returncode != 0:
            sys.stderr.write(f"Quality gate failed ({' '.join(args)}):\n")
            sys.stderr.write(result.stdout)
            sys.stderr.write(result.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
