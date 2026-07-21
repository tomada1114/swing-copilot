# /// script
# requires-python = ">=3.12"
# ///
"""PreToolUse hook: block edits to protected files and dangerous git commands.

Permission `deny` rules are advisory in some Claude Code versions
(anthropics/claude-code#6699), and hooks also fire in bypassPermissions mode,
so this hook is the enforcement backstop for the rules below.

Bash commands are split on shell control operators and each segment's argv is
inspected on its own, so a flag in one command can neither trigger nor excuse
a block for another. Static inspection stays best-effort: it catches the
plain spellings an agent falls back to, not every shell construction.

Exit code 2 blocks the tool call and shows the reason to Claude.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

ENV_EXAMPLE_SUFFIXES = (".example", ".sample", ".template")

# Shell control operators that separate commands within one Bash call.
SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\||&|\n")

# git global options that consume the following token (`git -C dir push ...`).
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}

# Commands whose last file argument is the destination they write.
DEST_LAST_COMMANDS = {"cp", "mv", "install"}


def _check_write(file_path: str) -> str | None:
    """Return a block reason when the target file must not be hand-edited."""
    path = PurePosixPath(file_path.replace("\\", "/"))
    name = path.name
    if name == "uv.lock":
        return "uv.lock is generated — run `uv lock` or `uv add` instead of editing it."
    # .env / .env.<stage> hold secrets; committed example files are fine.
    if (name == ".env" or name.startswith(".env.")) and not name.endswith(
        ENV_EXAMPLE_SUFFIXES
    ):
        return "Files named .env* may contain secrets and must not be written by the agent."
    # Write-side counterpart of the Read(secrets/**) deny in settings.json.
    if "secrets" in path.parts[:-1]:
        return "Files under secrets/ hold credentials and must not be written by the agent."
    return None


def _segments(command: str) -> Iterator[list[str]]:
    """Split a shell command into one argv list per command segment."""
    for raw in SEGMENT_SPLIT.split(command.replace("\\\n", " ")):
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()
        if tokens:
            yield tokens


def _short_cluster_has(arg: str, flag: str, value_options: str) -> bool:
    """Return True when a clustered short-option argument contains ``flag``.

    Characters after an option that takes a value (`-am"msg"`) are that
    option's value, not further flags, so scanning stops there.
    """
    if arg == "-" or not arg.startswith("-") or arg.startswith("--"):
        return False
    for char in arg[1:]:
        if char == flag:
            return True
        if char in value_options:
            return False
    return False


def _git_subcommand(tokens: list[str]) -> tuple[str, list[str]] | None:
    """Return (subcommand, args) when the segment runs git, else None."""
    if "git" not in tokens:
        return None
    rest = tokens[tokens.index("git") + 1 :]
    skip_value = False
    for index, token in enumerate(rest):
        if skip_value:
            skip_value = False
            continue
        if token in GIT_VALUE_OPTIONS:
            skip_value = True
            continue
        if token.startswith("-"):
            continue
        return token, rest[index + 1 :]
    return None


def _check_git(tokens: list[str]) -> str | None:
    """Return a block reason when a git command bypasses quality gates."""
    parsed = _git_subcommand(tokens)
    if parsed is None:
        return None
    subcommand, args = parsed

    # For commit, -m/-F/-C/-c/-t consume a value; -n anywhere is --no-verify.
    if subcommand == "commit" and (
        "--no-verify" in args
        or any(_short_cluster_has(arg, "n", "mFCct") for arg in args)
    ):
        return "git commit --no-verify skips the pre-commit hooks — fix the failing hook instead."

    if subcommand == "push":
        # For push, -o consumes a value; -f anywhere in a cluster forces.
        forced = "--force" in args or any(
            _short_cluster_has(arg, "f", "o") for arg in args
        )
        with_lease = any(
            arg == "--force-with-lease" or arg.startswith("--force-with-lease=")
            for arg in args
        )
        if forced and not with_lease:
            return "Plain force-push is blocked — use `git push --force-with-lease` if a force-push is really needed."

    return None


def _written_files(tokens: list[str]) -> list[str]:
    """Best-effort list of files a command segment writes to."""
    targets: list[str] = []
    for index, token in enumerate(tokens):
        if re.fullmatch(r"\d?>>?", token) and index + 1 < len(tokens):
            targets.append(tokens[index + 1])
        elif match := re.fullmatch(r"\d?>>?(.+)", token):
            targets.append(match.group(1))

    command, *args = tokens
    file_args = [arg for arg in args if not arg.startswith("-")]
    if command in DEST_LAST_COMMANDS and file_args:
        targets.append(file_args[-1])
    elif command in {"tee", "truncate"} or (
        command == "sed" and any(arg.startswith("-i") for arg in args)
    ):
        targets.extend(file_args)
    elif command == "dd":
        targets.extend(arg.removeprefix("of=") for arg in args if arg.startswith("of="))
    return targets


def _check_bash(command: str) -> str | None:
    """Return a block reason when the shell command bypasses the guards."""
    for tokens in _segments(command):
        if reason := _check_git(tokens):
            return reason
        for target in _written_files(tokens):
            if reason := _check_write(target):
                return reason
    return None


def main() -> int:
    """Inspect the pending tool call and block protected operations."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    reason = None
    if tool_name in {"Edit", "Write"}:
        reason = _check_write(tool_input.get("file_path", ""))
    elif tool_name == "Bash":
        reason = _check_bash(tool_input.get("command", ""))

    if reason:
        sys.stderr.write(f"Blocked: {reason}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
