"""Repository-level contracts for the documented quality gates."""

from __future__ import annotations

import ast
import re
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
    "copilot-decision": "src/swing_copilot/paper/cli.py",
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
