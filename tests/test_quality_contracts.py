"""Repository-level contracts for the documented quality gates."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import tomllib
from pathlib import Path

import duckdb
import pytest

from swing_copilot.pipeline import daily, daily_composition, daily_runner
from swing_copilot.storage.database import DEFAULT_DB_PATH
from swing_copilot.storage.market_store import DEFAULT_PARQUET_ROOT
from tests.conftest import _REPO_DATA_DIR, _guard_repo_directory

PROJECT_ROOT = Path(__file__).parents[1]
REQUIREMENTS = tuple(
    [f"FR-{number:02d}" for number in range(1, 13)]
    + [f"NFR-{number:02d}" for number in range(1, 9)]
    + [f"CON-{number:02d}" for number in range(1, 5)]
)
REVIEW_ISSUES = ("#54", "#55", "#56", "#57", "#58", "#59")
TEST_NODE_PATTERN = re.compile(r"`(tests/[\w/]+\.py(?:::[A-Za-z_]\w*)+)`")
#: Which module owns each console script's domain-error -> `SystemExit`
#: conversion. `copilot-daily`'s entry point is a compatibility facade over
#: `pipeline/daily_composition.py`, which is where its `main()` actually lives.
CLI_ERROR_CONVERSION_MODULES = {
    "copilot-daily": "src/swing_copilot/pipeline/daily_composition.py",
    "copilot-backfill": "src/swing_copilot/pipeline/backfill.py",
    "copilot-history": "src/swing_copilot/report/history_cli.py",
    "copilot-backtest": "src/swing_copilot/backtest/cli.py",
    "copilot-filter-matrix": "src/swing_copilot/screening/filter_matrix_cli.py",
    "copilot-dd-forward": "src/swing_copilot/regime/dd_forward_cli.py",
    "copilot-ingest-analysis": "src/swing_copilot/analysis/cli.py",
    "copilot-verify-analysis": "src/swing_copilot/analysis/verify_cli.py",
    "copilot-export-slices": "src/swing_copilot/analysis/slice_cli.py",
    "copilot-retro": "src/swing_copilot/retro/cli.py",
    "copilot-track": "src/swing_copilot/tracking/cli.py",
    "copilot-dashboard": "src/swing_copilot/dashboard/cli.py",
}
#: The deadline watcher `swing-daily` background-launches, and the single
#: allowlisted shape of its invocation (#264).
TIMEBOX_SCRIPT = PROJECT_ROOT / "scripts/timebox.sh"
DAILY_SKILL = PROJECT_ROOT / ".claude/skills/swing-daily/SKILL.md"
DAILY_WORKFLOW = PROJECT_ROOT / ".github/workflows/swing-daily.yml"
CLAUDE_SETTINGS = PROJECT_ROOT / ".claude/settings.json"
TIMEBOX_COMMAND = "./scripts/timebox.sh"
TIMEBOX_ALLOW_ENTRY = f"Bash({TIMEBOX_COMMAND}:*)"
DAILY_BASH_ALLOW_ENTRIES = (
    "Bash(uv run copilot-daily:*)",
    "Bash(uv run copilot-verify-analysis:*)",
    "Bash(uv run copilot-ingest-analysis:*)",
    "Bash(uv run copilot-history:*)",
    "Bash(uv run copilot-export-slices:*)",
    TIMEBOX_ALLOW_ENTRY,
)
TIMEBOX_INVOCATION = re.compile(
    rf"^\s*{re.escape(TIMEBOX_COMMAND)} (\d+)\s*$",
    re.MULTILINE,
)
#: The cwd-independent escape hatch for the relative form above (#323). The
#: relative path is what keeps the launch allowlisted, and it is also what makes
#: it exit 127 from any working directory other than the repository root.
TIMEBOX_FALLBACK_COMMAND = "<REPO_ROOT>/scripts/timebox.sh"
TIMEBOX_FALLBACK_INVOCATION = re.compile(
    rf"^\s*{re.escape(TIMEBOX_FALLBACK_COMMAND)} (\d+)\s*$",
    re.MULTILINE,
)


def test_invariant_matrix_maps_every_requirement_and_review_fix_to_real_tests():
    """Keep requirement/test traceability valid when tests are renamed."""
    matrix_path = PROJECT_ROOT / "docs/07_invariant_test_matrix.md"
    assert matrix_path.is_file(), "docs/07_invariant_test_matrix.md is required"
    matrix = matrix_path.read_text(encoding="utf-8")

    for requirement in REQUIREMENTS:
        assert requirement in matrix, f"{requirement} is missing from the matrix"
    for issue in REVIEW_ISSUES:
        assert issue in matrix, f"{issue} is missing from the review-fix mapping"

    test_nodes = TEST_NODE_PATTERN.findall(matrix)
    assert test_nodes, "the matrix must reference collected pytest nodes"
    for test_node in test_nodes:
        relative_path, _, _qualified_name = test_node.partition("::")
        test_path = PROJECT_ROOT / relative_path
        assert test_path.is_file(), f"missing test file for {test_node}"
        assert test_node in _collected_test_nodes(test_path), (
            f"missing collected test for {test_node}"
        )


def test_no_cover_pragmas_are_limited_to_main_and_abstract_protocol_bodies():
    """Enforce NFR-08 without relying on reviewer memory."""
    violations: list[str] = []
    for source_path in (PROJECT_ROOT / "src").rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        pragma_lines = {
            line_number
            for line_number, line in enumerate(source.splitlines(), start=1)
            if "pragma: no cover" in line
        }
        if not pragma_lines:
            continue

        tree = ast.parse(source, filename=str(source_path))
        allowed_lines = {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and ast.get_source_segment(source, node.test) == '__name__ == "__main__"'
        }
        for class_node in (
            node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        ):
            is_protocol_or_abc = any(
                _is_protocol_or_abc(base) for base in class_node.bases
            )
            for method in (
                node for node in class_node.body if isinstance(node, ast.FunctionDef)
            ):
                if is_protocol_or_abc or _is_abstract_method(method):
                    end_line = method.end_lineno or method.lineno
                    allowed_lines.update(range(method.lineno, end_line + 3))

        violations.extend(
            f"{source_path.relative_to(PROJECT_ROOT)}:{pragma_line}"
            for pragma_line in pragma_lines
            if pragma_line not in allowed_lines
        )

    assert not violations, "unexpected # pragma: no cover: " + ", ".join(violations)


def test_daily_entrypoint_remains_a_compatible_facade_over_split_boundaries():
    """Keep the documented console-script target and direct API stable."""
    assert daily.run_daily.__module__ == "swing_copilot.pipeline.daily"
    assert daily.main.__module__ == "swing_copilot.pipeline.daily"
    assert daily_runner.run_daily.__module__ == "swing_copilot.pipeline.daily_runner"
    assert (
        daily_composition.main.__module__ == "swing_copilot.pipeline.daily_composition"
    )
    assert daily.DailyDependencies is daily_runner.DailyDependencies


def test_daily_console_script_facade_delegates_to_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep ``swing_copilot.pipeline.daily:main`` executable after the split."""
    received: list[list[str] | None] = []
    monkeypatch.setattr(daily_composition, "main", received.append)

    daily.main(["--dry-run"])

    assert received == [["--dry-run"]]


def test_every_console_script_converts_its_errors_through_cli_support():
    """Issue #193: one owner for "domain error -> SystemExit", not eleven.

    The commands stay separate — each is a stable, skill-facing entry point —
    so this pins the conversion, not the entry points. Adding a twelfth
    `copilot-*` script fails here until it is mapped, which is the moment to
    decide whether it may hand-write the boilerplate again.
    """
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert set(pyproject["project"]["scripts"]) == set(CLI_ERROR_CONVERSION_MODULES)
    for command, module_path in sorted(CLI_ERROR_CONVERSION_MODULES.items()):
        tree = ast.parse((PROJECT_ROOT / module_path).read_text(encoding="utf-8"))
        assert any(
            isinstance(node, ast.ImportFrom)
            and node.module == "swing_copilot.cli_support"
            and any(alias.name == "run_cli" for alias in node.names)
            for node in ast.walk(tree)
        ), f"{command} must convert its errors through cli_support.run_cli()"


def test_atomic_writers_live_in_a_dependency_zero_module():
    """Issue #193: wanting an atomic write must not import anything."""
    tree = ast.parse(
        (PROJECT_ROOT / "src/swing_copilot/io_atomic.py").read_text(encoding="utf-8")
    )
    imported = {
        module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (module := node.module) is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert not [name for name in imported if name.startswith("swing_copilot")]


def test_no_package_reaches_into_analysis_for_atomic_writes():
    """Issue #193: no reverse dependency on `analysis` just to replace a file.

    `regime`, `screening`, `report` and `retro` import the writers from
    `swing_copilot.io_atomic`. `analysis/export.py` itself keeps the
    compatibility re-export, so it is the one allowed reference.
    """
    writers = {"write_json_atomically", "write_text_atomically"}
    violations: list[str] = []
    for source_path in (PROJECT_ROOT / "src").rglob("*.py"):
        if source_path.name == "export.py" and source_path.parent.name == "analysis":
            continue
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"), filename=str(source_path)
        )
        violations.extend(
            f"{source_path.relative_to(PROJECT_ROOT)}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "swing_copilot.analysis.export"
            and writers.intersection(alias.name for alias in node.names)
        )

    assert not violations, (
        "import the atomic writers from swing_copilot.io_atomic: "
        + ", ".join(violations)
    )


def test_repo_data_guard_rejects_a_duckdb_connection_under_the_repo_data_dir():
    """Issue #233: the autouse `data/` guard must actually fire.

    This is the exact call `Database.connect()` makes for the repo-relative
    `DEFAULT_DB_PATH` when a composition-root test forgets
    `monkeypatch.chdir(tmp_path)`. The guard raises before DuckDB opens
    anything, so this test never takes the operator's file lock.
    """
    repo_db = str(PROJECT_ROOT.resolve() / DEFAULT_DB_PATH)

    with pytest.raises(AssertionError, match=r"DuckDB file under the repository"):
        duckdb.connect(repo_db)


def test_repo_data_guard_leaves_isolated_and_in_memory_connections_alone(tmp_path):
    """The guard keys on the resolved path, so normal tests are unaffected."""
    with duckdb.connect(str(tmp_path / "copilot.duckdb")) as isolated:
        assert isolated.execute("SELECT 1").fetchone() == (1,)
    with duckdb.connect() as in_memory:
        assert in_memory.execute("SELECT 1").fetchone() == (1,)


def test_repo_directory_guard_fails_when_a_watched_file_appears(tmp_path):
    """The mtime half shared by the `reports/` and `data/` guards must fire."""
    guarded_dir = tmp_path / "data"
    guarded_dir.mkdir()
    watched = guarded_dir / "copilot.duckdb"

    with (
        pytest.raises(AssertionError, match=r"directory changed while this test ran"),
        _guard_repo_directory(guarded_dir, watched),
    ):
        watched.write_bytes(b"fixture")


def test_repo_directory_guard_message_names_the_concurrent_external_process(tmp_path):
    """Issue #257: the guard fires on the 18:30 routine's writes too.

    This working copy is also the unattended execution environment, so a
    message that blames the test sends the operator hunting a bug that is not
    there. Both causes must be on the failure itself.
    """
    guarded_dir = tmp_path / "data"
    guarded_dir.mkdir()
    watched = guarded_dir / "copilot.duckdb"

    with (
        pytest.raises(AssertionError) as failure,
        _guard_repo_directory(guarded_dir, watched),
    ):
        watched.write_bytes(b"fixture")

    message = str(failure.value)
    assert "concurrent external process" in message
    assert "18:30" in message
    assert "copilot-daily" in message
    assert "tmp_path" in message


def test_repo_directory_guard_message_reports_the_changed_paths_and_mtimes(tmp_path):
    """The evidence has to be on the failure: which paths moved, and when."""
    guarded_dir = tmp_path / "data"
    guarded_dir.mkdir()
    watched = guarded_dir / "copilot.duckdb"
    untouched = guarded_dir / "copilot_dry_run.duckdb"
    untouched.write_bytes(b"fixture")

    with (
        pytest.raises(AssertionError) as failure,
        _guard_repo_directory(guarded_dir, watched, untouched),
    ):
        watched.write_bytes(b"fixture")

    message = str(failure.value)
    assert "Changed watched paths (mtime before -> after):" in message
    # The file did not exist beforehand, so its "before" is reported as absent
    # rather than the evidence line being silently omitted.
    assert f"{watched}: absent -> " in message
    assert f"mtime_ns={watched.stat().st_mtime_ns}" in message
    # A watched path that did not move stays out of the evidence list.
    assert f"{untouched}: " not in message


def test_repo_directory_guard_stays_quiet_for_writes_outside_the_watched_tree(tmp_path):
    """A guard that fires on isolated writes would be unusable, so pin it."""
    guarded_dir = tmp_path / "data"
    guarded_dir.mkdir()

    with _guard_repo_directory(guarded_dir, guarded_dir / "copilot.duckdb"):
        (tmp_path / "isolated.duckdb").write_bytes(b"fixture")


def test_storage_defaults_stay_inside_the_directory_the_guard_watches():
    """Moving a repo-relative default out of `data/` must not silently unguard it.

    `_REPO_DATA_DIR` is what both `data/` guards key on. If `DEFAULT_DB_PATH`
    or `DEFAULT_PARQUET_ROOT` were ever repointed elsewhere, the guards would
    keep passing while protecting nothing, so pin the relationship here.
    """
    assert PROJECT_ROOT.resolve() / "data" == _REPO_DATA_DIR
    for default in (DEFAULT_DB_PATH, DEFAULT_PARQUET_ROOT):
        assert not default.is_absolute()
        assert (PROJECT_ROOT.resolve() / default).parent == _REPO_DATA_DIR


def _run_timebox(
    *arguments: str, cwd: Path = PROJECT_ROOT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed repo script, static arguments
        [str(TIMEBOX_SCRIPT), *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("is_launched_from_repo_root", [True, False])
def test_timebox_watcher_marks_the_deadline_and_stays_directly_executable(
    tmp_path, is_launched_from_repo_root
):
    """`swing-daily` launches the watcher and waits for its marker.

    The completion notification *is* the wave's deadline, so both halves of the
    contract are pinned here: the file stays executable (it is invoked as
    `./scripts/timebox.sh`, not through an interpreter) and it announces the
    deadline with the single `TIMEBOX_REACHED` line the skill looks for.

    The second case is the precondition for #323's recovery. The documented
    launch is relative, which is the only shape the allowlist can prefix-match —
    and also why a shell sitting anywhere else exits 127. The skill recovers by
    relaunching the same file through an absolute path, which only works while
    the script itself resolves nothing against the caller's cwd.
    """
    cwd = PROJECT_ROOT if is_launched_from_repo_root else tmp_path

    result = _run_timebox("1", cwd=cwd)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "TIMEBOX_REACHED"


@pytest.mark.parametrize(
    "arguments",
    [(), ("0",), ("-5",), ("abc",), ("1.5",), ("900", "extra")],
    ids=["missing", "zero", "negative", "non-numeric", "fractional", "too-many"],
)
def test_timebox_watcher_fails_loudly_on_an_unusable_timebox(arguments):
    """An unusable argument must fail fast instead of waiting forever or not at all.

    A silent 0-second or non-numeric watcher would fire immediately and cancel a
    wave that had barely started, so the skill's fallback rule needs a visible
    failure to key on.
    """
    result = _run_timebox(*arguments)

    assert result.returncode == 2
    assert "TIMEBOX_REACHED" not in result.stdout
    assert result.stderr.strip()


def test_daily_skill_watcher_stays_in_its_allowlisted_script_form():
    """#264: the watcher stays one allowlisted script call, with its fallback intact.

    Expanded back into a `date`/`sleep` one-liner, the command would prefix-match
    no allowlist entry, so an unattended run would block on approval and the
    cancellation mechanism itself would become the reason a wave overruns. The
    fallback for a watcher that still cannot start is what keeps that failure
    survivable, so it must survive too.
    """
    skill_text = DAILY_SKILL.read_text(encoding="utf-8")
    allow = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))["permissions"][
        "allow"
    ]
    timeboxes = {int(seconds) for seconds in TIMEBOX_INVOCATION.findall(skill_text)}

    assert timeboxes, "the skill must show how to launch the watcher"
    assert all(seconds > 0 for seconds in timeboxes)
    assert "date +%s" not in skill_text
    assert [entry for entry in allow if "timebox" in entry] == [TIMEBOX_ALLOW_ENTRY]
    assert "ウォッチャを起動できない場合" in skill_text
    assert "**再試行せず**" in skill_text


def test_daily_skill_recovers_a_watcher_that_cannot_resolve_its_relative_path():
    """#323: exit 127 must have a documented, bounded recovery.

    The allowlisted form is relative, so a shell left outside the repository root
    kills the parent's only blocking wait before the first wave even starts — a
    2026-08-19 dry-run recorded exactly this exit 127 in its `headless_note.md`
    and fell through to the completion-based fallback. One absolute-path retry
    recovers it without widening the allowlist; the widened `Bash()` prefix is
    deliberately *not* added, so this pins that the recovery is documented
    instead.
    """
    skill_text = DAILY_SKILL.read_text(encoding="utf-8")
    allow = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))["permissions"][
        "allow"
    ]

    fallbacks = {
        int(seconds) for seconds in TIMEBOX_FALLBACK_INVOCATION.findall(skill_text)
    }
    assert fallbacks, "the skill must show the cwd-independent relaunch"
    assert all(seconds > 0 for seconds in fallbacks)
    assert "exit 127" in skill_text
    assert "**1 回だけ**" in skill_text
    # Both watchers launch from the same shell and fail the same way. Recovering
    # only the per-wave one silently drops the 45-minute final backstop.
    assert "すべてのウォッチャに等しく適用する" in skill_text
    # The repo root has to be derivable without counting directories: the live
    # and dry-run run directories sit at different depths.
    assert "から数えないこと" in skill_text
    # The escape hatch stays outside the allowlist on purpose: an absolute path
    # differs per checkout, so allowlisting it is impossible, and headless runs
    # use `dontAsk` anyway.
    assert [entry for entry in allow if "timebox" in entry] == [TIMEBOX_ALLOW_ENTRY]


def test_daily_workflow_uses_dont_ask_and_a_narrow_tool_allowlist():
    """Keep the untrusted-text analysis job behind an explicit permission boundary."""
    workflow = DAILY_WORKFLOW.read_text(encoding="utf-8")
    settings = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    project_allow = settings["permissions"]["allow"]

    assert "--permission-mode dontAsk" in workflow
    assert "bypassPermissions" not in workflow
    assert "--allowedTools" in workflow
    assert "--disallowedTools" in workflow
    assert "id: claude" in workflow
    assert "Summarize Claude permission denials" in workflow
    assert "permission_denials_count=" in workflow
    assert "permission_denials[" in workflow
    assert "Bash(uv run:*)" not in workflow
    for command in DAILY_BASH_ALLOW_ENTRIES:
        assert command in workflow
        assert command in project_allow
    assert "Bash(uv run:*)" not in project_allow
    for blocked in (
        "WebFetch",
        "WebSearch",
        "Write(src/**)",
        "Edit(src/**)",
        "Write(scripts/**)",
        "Edit(scripts/**)",
        "Write(.github/**)",
        "Edit(.github/**)",
    ):
        assert blocked in workflow


def test_headless_daily_run_uses_tool_reads_and_exact_bash_shapes():
    """Keep routine file access out of denied Bash calls in the headless job."""
    workflow = DAILY_WORKFLOW.read_text(encoding="utf-8")
    skill_text = DAILY_SKILL.read_text(encoding="utf-8")

    for text in (workflow, skill_text):
        assert "Read / Glob / Grep" in text
        assert "Write / Edit" in text
        assert "前置きなしで直接" in text
        for forbidden in ("cat", "ls", "find", "sed", "rm", "git", "python"):
            assert forbidden in text
        assert "シェル演算子" in text


def test_daily_skill_forbids_a_text_only_turn_while_subagents_are_running():
    """#323: in an SDK run the parent's final text turn ends the whole session.

    The 2026-08-19 live run returned a text-only turn while the first wave was
    still fanned out, so the session died with 2 of 30 fragments, no
    `analysis_result.json`, and no `headless_note.md` — and the job still went
    green. The rule has to be in the headless policy *and* in the prohibitions,
    because the parent reads the latter when it is deciding to stop.
    """
    skill_text = DAILY_SKILL.read_text(encoding="utf-8")
    # Partition on the headless heading first: splitting only on 禁止事項 would
    # leave the whole document above it as "the headless policy", and the rule
    # could drift into any other step while this test stayed green.
    _, heading, below = skill_text.partition("## 無人実行（headless）時の方針")
    headless_policy, _, prohibitions = below.partition("## 禁止事項")

    assert heading, "the headless policy section must exist"
    assert prohibitions, "the prohibitions must follow the headless policy"
    for section in (headless_policy, prohibitions):
        assert "ツール呼び出しを含まないターン" in section
    # The alternative to waiting must be named, or "do not idle" reads as "do not
    # launch subagents at all". Naming it is not enough either: a background
    # watcher returns immediately, so the mechanism has to be the foreground
    # call, chunked under the Bash tool's own timeout ceiling.
    assert "待つこと自体をツール呼び出しにする" in headless_policy
    assert "run_in_background を付けない" in headless_policy
    # Cancelling a wave seconds after fanning out turns a 2-of-30 day into a
    # 0-of-30 day, so the clock-free fallback needs a floor.
    assert "起動直後の波を打ち切らない" in skill_text
    # A note written only at the end is lost with the session it was describing.
    assert "最初の波を起動する前に一度書き" in headless_policy


def test_daily_skill_makes_the_per_subagent_time_limit_self_enforced():
    """#323: the only cutoff that survives a parent that stopped listening.

    The watcher and `TaskStop` both assume the parent session is alive and still
    receiving background-task notifications. A subagent that may — but need not —
    stop itself leaves no cutoff at all when that assumption breaks, so the
    permissive wording must not come back.
    """
    skill_text = DAILY_SKILL.read_text(encoding="utf-8")

    assert "自ら終了してよい" not in skill_text
    assert "**サブエージェント自身が必ず打ち切る**" in skill_text
    assert "親に何も起きなくても効く唯一の機構" in skill_text
    # Self-termination must not become a licence to emit half-written fragments.
    assert "親に打ち切られた場合とまったく同じ" in skill_text


def _is_protocol_or_abc(base: ast.expr) -> bool:
    if isinstance(base, ast.Name):
        return base.id in {"Protocol", "ABC"}
    return isinstance(base, ast.Attribute) and base.attr in {"Protocol", "ABC"}


def _is_abstract_method(method: ast.FunctionDef) -> bool:
    return any(
        (isinstance(decorator, ast.Name) and decorator.id == "abstractmethod")
        or (isinstance(decorator, ast.Attribute) and decorator.attr == "abstractmethod")
        for decorator in method.decorator_list
    )


def _collected_test_nodes(test_path: Path) -> set[str]:
    """Return the default pytest nodes derivable from this test module's AST."""
    relative_path = test_path.relative_to(PROJECT_ROOT).as_posix()
    module = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
    nodes: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            nodes.add(f"{relative_path}::{node.name}")
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for method in node.body:
                if isinstance(method, ast.FunctionDef) and method.name.startswith(
                    "test_"
                ):
                    nodes.add(f"{relative_path}::{node.name}::{method.name}")
    return nodes
