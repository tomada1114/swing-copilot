# /// script
# requires-python = ">=3.12"
# ///
"""Survey open Dependabot PRs and emit a triage table.

Usage:
    uv run --script .claude/skills/merge-dependabot/scripts/survey_prs.py [--json]

Requires the `gh` CLI, authenticated against the current repository.
Read-only: this script never mutates PR or branch state.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

Row = dict[str, Any]
"""One triage row, or one raw `gh` JSON object — heterogeneous by nature."""

FIELDS = (
    "number,title,author,headRefName,baseRefName,mergeable,mergeStateStatus,"
    "statusCheckRollup,labels,files,createdAt,url"
)

# Matches Dependabot titles such as "bump actions/checkout from 7.0.0 to 7.1.0"
# and "update mypy requirement from >=2.1.0 to >=2.2.0".
BUMP_RE = re.compile(
    r"(?:bump|update)\s+(?P<pkg>\S+?)(?:\s+requirement)?\s+from\s+"
    r"(?P<old>\S+)\s+to\s+(?P<new>\S+)",
    re.IGNORECASE,
)
# Two-part versions are common in pip constraints (">=3.7"), so minor is optional.
VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

FAILED_STATES = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "ERROR"}
)
PENDING_STATES = frozenset({"PENDING", "IN_PROGRESS", "QUEUED", "WAITING", "EXPECTED"})


def emit(line: str = "") -> None:
    """Write one line to stdout."""
    sys.stdout.write(f"{line}\n")


def gh_json(*args: str) -> list[Row]:
    """Run a `gh ... --json` command and return the parsed payload."""
    proc = subprocess.run(  # noqa: S603
        ["gh", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.exit(f"gh {' '.join(args)} failed:\n{proc.stderr.strip()}")
    parsed: list[Row] = json.loads(proc.stdout or "[]")
    return parsed


def parse_versions(title: str) -> tuple[str | None, str | None, str | None]:
    """Return (package, old_version, new_version) parsed from a PR title."""
    match = BUMP_RE.search(title)
    if not match:
        return None, None, None
    return match["pkg"], match["old"], match["new"]


def semver_level(old: str | None, new: str | None) -> str:
    """Classify a bump as major, minor or patch; unknown when unparsable."""
    if not old or not new:
        return "unknown"
    old_match, new_match = VERSION_RE.search(old), VERSION_RE.search(new)
    if not old_match or not new_match:
        return "unknown"
    before = tuple(int(part or 0) for part in old_match.groups())
    after = tuple(int(part or 0) for part in new_match.groups())
    if after[0] != before[0]:
        return "major"
    if after[1] != before[1]:
        return "minor"
    return "patch"


def check_summary(rollup: list[Row] | None) -> tuple[str, list[str]]:
    """Reduce statusCheckRollup to an overall state plus failing check names."""
    if not rollup:
        return "NONE", []
    failing: list[str] = []
    pending = False
    for check in rollup:
        # Check runs report conclusion/status; commit statuses report state.
        state = (check.get("conclusion") or check.get("state") or "").upper()
        status = (check.get("status") or "").upper()
        name = check.get("name") or check.get("context") or "?"
        if status and status != "COMPLETED" and not state:
            pending = True
        elif state in FAILED_STATES:
            failing.append(f"{name}={state}")
        elif state in PENDING_STATES:
            pending = True
    if failing:
        return "FAILING", failing
    return ("PENDING", []) if pending else ("PASSING", [])


def collect() -> list[Row]:
    """Return one triage row per open bot-authored PR, ordered by PR number."""
    prs = gh_json("pr", "list", "--state", "open", "--limit", "100", "--json", FIELDS)
    rows: list[Row] = []
    for pr in prs:
        login = (pr.get("author") or {}).get("login", "")
        if "dependabot" not in login and "renovate" not in login:
            continue
        pkg, old, new = parse_versions(pr["title"])
        state, failing = check_summary(pr.get("statusCheckRollup"))
        rows.append(
            {
                "number": pr["number"],
                "title": pr["title"],
                "url": pr["url"],
                "branch": pr["headRefName"],
                "base": pr["baseRefName"],
                "package": pkg,
                "from": old,
                "to": new,
                "level": semver_level(old, new),
                "ecosystem": (
                    "github_actions"
                    if "github_actions" in pr["headRefName"]
                    else "python"
                ),
                "mergeable": pr.get("mergeable"),
                "merge_state": pr.get("mergeStateStatus"),
                "checks": state,
                "failing_checks": failing,
                "files": [f["path"] for f in pr.get("files") or []],
                "labels": [label["name"] for label in pr.get("labels") or []],
                "created_at": pr.get("createdAt"),
            }
        )
    rows.sort(key=lambda row: row["number"])
    return rows


def contested_files(rows: list[Row]) -> dict[str, list[int]]:
    """Map each file touched by more than one PR to the PR numbers touching it."""
    seen: dict[str, list[int]] = {}
    for row in rows:
        for path in row["files"]:
            seen.setdefault(path, []).append(row["number"])
    return {path: nums for path, nums in seen.items() if len(nums) > 1}


def report(rows: list[Row]) -> None:
    """Print the human-readable triage table."""
    emit(f"{len(rows)} open bot PR(s)")
    emit()
    for row in rows:
        emit(
            f"  #{row['number']:<4} [{row['ecosystem']:<14}] {row['level']:<7} "
            f"checks={row['checks']:<8} merge={row['merge_state'] or '?'}"
        )
        emit(f"        {row['title']}")
        if row["failing_checks"]:
            emit(f"        FAILING: {', '.join(row['failing_checks'])}")
        emit(f"        files: {', '.join(row['files']) or '(none)'}")
    contested = contested_files(rows)
    if contested:
        emit()
        emit("Overlapping files (favor a combined branch):")
        for path, nums in sorted(contested.items()):
            emit(f"  {path}: {', '.join(f'#{n}' for n in nums)}")


def main() -> int:
    """Parse arguments, survey the PRs and emit the requested format."""
    parser = argparse.ArgumentParser(description="Survey open Dependabot PRs.")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()

    rows = collect()
    if args.json:
        emit(json.dumps(rows, indent=2))
    elif not rows:
        emit("No open Dependabot/Renovate PRs.")
    else:
        report(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
