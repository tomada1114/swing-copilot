"""Repository-level contracts for the documented quality gates."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TYPE_CHECKING

from swing_copilot.pipeline import daily, daily_composition, daily_runner

if TYPE_CHECKING:
    import pytest

PROJECT_ROOT = Path(__file__).parents[1]
REQUIREMENTS = tuple(
    [f"FR-{number:02d}" for number in range(1, 13)]
    + [f"NFR-{number:02d}" for number in range(1, 9)]
    + [f"CON-{number:02d}" for number in range(1, 5)]
)
REVIEW_ISSUES = ("#54", "#55", "#56", "#57", "#58", "#59")
TEST_NODE_PATTERN = re.compile(r"`(tests/[\w/]+\.py(?:::[A-Za-z_]\w*)+)`")


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
