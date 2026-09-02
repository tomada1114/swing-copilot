"""Tests for scripts/diff_gate.py."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.support.script_loader import load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]

diff_gate = load_script_module("diff_gate", "scripts/diff_gate.py")


def _shape(
    *,
    src_packages: frozenset[str] = frozenset({"screening", "report", "pipeline"}),
    existing_test_files: frozenset[str] = frozenset(
        {
            "tests/test_config.py",
            "tests/test_models.py",
            "tests/screening/test_vcp.py",
            "tests/screening/conftest.py",
            "tests/report/test_terminal_markdown_report.py",
            "tests/pipeline/test_failsoft.py",
            "tests/pipeline/test_daily_core.py",
            "tests/test_quality_contracts.py",
            "tests/test_e2e_smoke.py",
            "tests/test_package.py",
            "tests/analysis/test_skill_contract.py",
            "tests/test_check_daily_complete.py",
        }
    ),
    existing_test_dirs: frozenset[str] = frozenset(
        {"tests/screening", "tests/report", "tests/pipeline", "tests/analysis"}
    ),
    importers: dict[str, frozenset[str]] | None = None,
    durations: dict[str, float] | None = None,
) -> Any:
    """A small, hand-built `RepoShape` -- no git, no filesystem."""
    return diff_gate.RepoShape(
        src_packages=src_packages,
        existing_test_files=existing_test_files,
        existing_test_dirs=existing_test_dirs,
        importers=importers if importers is not None else {},
        durations=durations if durations is not None else {},
    )


# --------------------------------------------------------------------------- #
#  module_name
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("src_path", "expected"),
    [
        ("src/swing_copilot/config.py", "swing_copilot.config"),
        ("src/swing_copilot/screening/vcp.py", "swing_copilot.screening.vcp"),
        ("src/swing_copilot/screening/__init__.py", "swing_copilot.screening"),
        (
            "src/swing_copilot/dashboard/viewmodels/symbol.py",
            "swing_copilot.dashboard.viewmodels.symbol",
        ),
    ],
)
def test_module_name(src_path: str, expected: str) -> None:
    assert diff_gate.module_name(src_path) == expected


# --------------------------------------------------------------------------- #
#  select(): rule table
# --------------------------------------------------------------------------- #


def test_select_empty_diff_runs_env_sanity_subset_only() -> None:
    selection = diff_gate.select([], _shape())

    assert selection.is_empty_diff is True
    assert selection.is_all is False
    assert selection.targets == frozenset(diff_gate.ENV_SANITY_TARGETS)


def test_select_force_all_exact_path() -> None:
    selection = diff_gate.select(["pyproject.toml"], _shape())

    assert selection.is_all is True
    assert selection.degraded is False


def test_select_force_all_prefix_path() -> None:
    selection = diff_gate.select(["tests/support/fakes.py"], _shape())

    assert selection.is_all is True


def test_select_own_source_is_always_force_all() -> None:
    """A bug in the selector must not be able to hide its own regression tests."""
    selection = diff_gate.select(["scripts/diff_gate.py"], _shape())

    assert selection.is_all is True


def test_select_test_file_maps_to_itself() -> None:
    selection = diff_gate.select(["tests/screening/test_vcp.py"], _shape())

    assert selection.targets == frozenset(
        {"tests/screening/test_vcp.py", "tests/test_quality_contracts.py"}
    )
    assert selection.is_all is False


def test_select_test_conftest_maps_to_its_package_dir() -> None:
    selection = diff_gate.select(["tests/screening/conftest.py"], _shape())

    assert "tests/screening" in selection.targets


def test_select_test_helper_maps_to_its_package_dir() -> None:
    selection = diff_gate.select(["tests/screening/helpers.py"], _shape())

    assert "tests/screening" in selection.targets


def test_select_src_package_file_maps_to_tests_dir_and_appends_e2e_smoke() -> None:
    selection = diff_gate.select(["src/swing_copilot/screening/vcp.py"], _shape())

    assert selection.is_all is False
    assert "tests/screening" in selection.targets
    assert "tests/test_e2e_smoke.py" in selection.targets  # any src/** change
    assert "tests/test_quality_contracts.py" in selection.targets  # always-append


def test_select_src_package_file_widens_via_importer_map() -> None:
    """The one-hop reverse map recovers a cross-package dependency.

    A bare directory mirror would miss it: report/markdown_report.py is
    imported directly by tests/pipeline/test_failsoft.py, not just
    tests/report/.
    """
    shape = _shape(
        importers={
            "swing_copilot.report.markdown_report": frozenset(
                {"tests/pipeline/test_failsoft.py"}
            )
        }
    )

    selection = diff_gate.select(["src/swing_copilot/report/markdown_report.py"], shape)

    assert "tests/pipeline/test_failsoft.py" in selection.targets
    assert "tests/report" in selection.targets


def test_select_src_package_file_with_no_target_degrades_to_all() -> None:
    shape = _shape(
        src_packages=frozenset({"paper"}),
        existing_test_dirs=frozenset(),  # tests/paper doesn't exist
    )

    selection = diff_gate.select(["src/swing_copilot/paper/ledger.py"], shape)

    assert selection.is_all is True
    assert selection.degraded is False


def test_select_src_top_level_module_with_dedicated_test() -> None:
    selection = diff_gate.select(["src/swing_copilot/config.py"], _shape())

    assert "tests/test_config.py" in selection.targets
    assert selection.is_all is False


def test_select_src_top_level_module_uses_importer_map_by_exact_module_name() -> None:
    """Regression test for a wrong (double-prefixed) importer lookup key.

    `module_name` already returns the full `swing_copilot.<mod>` name.
    """
    shape = _shape(
        existing_test_files=frozenset({"tests/test_quality_contracts.py"}),
        importers={"swing_copilot.retry": frozenset({"tests/data/test_edgar.py"})},
    )

    selection = diff_gate.select(["src/swing_copilot/retry.py"], shape)

    assert selection.is_all is False
    assert "tests/data/test_edgar.py" in selection.targets


def test_select_src_top_level_module_with_no_test_or_importer_degrades_to_all() -> None:
    shape = _shape(existing_test_files=frozenset({"tests/test_quality_contracts.py"}))

    selection = diff_gate.select(["src/swing_copilot/retry.py"], shape)

    assert selection.is_all is True


def test_select_scripts_python_file_maps_to_dedicated_test() -> None:
    selection = diff_gate.select(["scripts/check_daily_complete.py"], _shape())

    assert "tests/test_check_daily_complete.py" in selection.targets
    assert selection.is_all is False


def test_select_scripts_python_file_with_no_dedicated_test_degrades_to_all() -> None:
    selection = diff_gate.select(["scripts/smoke_test.py"], _shape())

    assert selection.is_all is True


def test_select_scripts_shell_file_contributes_only_always_append() -> None:
    selection = diff_gate.select(["scripts/timebox.sh"], _shape())

    assert selection.is_all is False
    assert selection.targets == frozenset({"tests/test_quality_contracts.py"})


def test_select_workflow_change_selects_quality_contract_tests() -> None:
    selection = diff_gate.select([".github/workflows/ci.yml"], _shape())

    assert "tests/test_quality_contracts.py" in selection.targets
    assert "tests/analysis/test_skill_contract.py" in selection.targets
    assert selection.is_all is False


def test_select_claude_settings_change_selects_quality_contract_tests() -> None:
    selection = diff_gate.select([".claude/settings.json"], _shape())

    assert "tests/analysis/test_skill_contract.py" in selection.targets


def test_select_docs_markdown_change_selects_nothing_extra() -> None:
    selection = diff_gate.select(["docs/01_requirements.md"], _shape())

    assert selection.is_all is False
    assert selection.targets == frozenset({"tests/test_quality_contracts.py"})


def test_select_root_readme_change_selects_nothing_extra() -> None:
    selection = diff_gate.select(["README.md"], _shape())

    assert selection.targets == frozenset({"tests/test_quality_contracts.py"})


@pytest.mark.parametrize(
    "path", ["data/copilot.duckdb", "reports/latest.md", "dist/x.whl"]
)
def test_select_generated_or_data_path_selects_nothing_extra(path: str) -> None:
    selection = diff_gate.select([path], _shape())

    assert selection.targets == frozenset({"tests/test_quality_contracts.py"})


def test_select_unrecognized_path_fails_closed_to_all() -> None:
    selection = diff_gate.select(["some/brand/new/top-level/thing.txt"], _shape())

    assert selection.is_all is True


def test_select_config_directory_change_is_force_all() -> None:
    selection = diff_gate.select(["config/settings.yaml"], _shape())

    assert selection.is_all is True


def test_select_deduplicates_a_file_target_already_covered_by_its_directory() -> None:
    shape = _shape(
        importers={
            "swing_copilot.screening.vcp": frozenset({"tests/screening/test_vcp.py"})
        }
    )

    # Both the tests dir (via the package rule) and the specific file (via the
    # importer map) would otherwise appear -- the file target is redundant.
    selection = diff_gate.select(["src/swing_copilot/screening/vcp.py"], shape)

    assert "tests/screening" in selection.targets
    assert "tests/screening/test_vcp.py" not in selection.targets


# --------------------------------------------------------------------------- #
#  select(): estimation, budget degrade, parallel decision
# --------------------------------------------------------------------------- #


def test_select_budget_degrade_when_estimate_exceeds_threshold() -> None:
    huge_dir_files = frozenset(f"tests/screening/test_{i}.py" for i in range(60))
    shape = _shape(
        existing_test_files=huge_dir_files
        | frozenset({"tests/test_quality_contracts.py"}),
        durations=dict.fromkeys(huge_dir_files, 10.0),  # 600s, well over budget
    )

    selection = diff_gate.select(["src/swing_copilot/screening/vcp.py"], shape)

    assert selection.is_all is True
    assert selection.degraded is True
    assert "budget degrade" in selection.reasons[-1]


def test_select_uses_serial_below_parallel_threshold() -> None:
    shape = _shape(
        existing_test_files=frozenset(
            {"tests/screening/test_vcp.py", "tests/test_quality_contracts.py"}
        ),
        existing_test_dirs=frozenset({"tests/screening"}),
        durations={
            "tests/screening/test_vcp.py": 1.0,
            "tests/test_quality_contracts.py": 1.0,
        },
    )

    selection = diff_gate.select(["src/swing_copilot/screening/vcp.py"], shape)

    assert selection.use_parallel is False


def test_select_uses_parallel_above_parallel_threshold() -> None:
    shape = _shape(
        existing_test_files=frozenset(
            {"tests/screening/test_vcp.py", "tests/test_quality_contracts.py"}
        ),
        existing_test_dirs=frozenset({"tests/screening"}),
        durations={
            "tests/screening/test_vcp.py": 20.0,
            "tests/test_quality_contracts.py": 1.0,
        },
    )

    selection = diff_gate.select(["src/swing_copilot/screening/vcp.py"], shape)

    assert selection.use_parallel is True


def test_select_reports_unknown_durations_as_fallback_mean() -> None:
    shape = _shape(
        existing_test_files=frozenset(
            {"tests/screening/test_vcp.py", "tests/test_quality_contracts.py"}
        ),
        existing_test_dirs=frozenset({"tests/screening"}),
        durations={},  # nothing known -> both files fall back to the mean
    )

    selection = diff_gate.select(["src/swing_copilot/screening/vcp.py"], shape)

    assert selection.unknown_duration_count == selection.file_count
    assert selection.estimated_seconds == pytest.approx(
        diff_gate.FALLBACK_MEAN_SECONDS * selection.file_count
    )


# --------------------------------------------------------------------------- #
#  select(): reasons diagnostics
# --------------------------------------------------------------------------- #


def test_select_records_one_reason_per_changed_path() -> None:
    selection = diff_gate.select(
        ["docs/README.md", "src/swing_copilot/screening/vcp.py"], _shape()
    )

    assert len(selection.reasons) == 2


# --------------------------------------------------------------------------- #
#  Self-consistency against the real repository
# --------------------------------------------------------------------------- #


def test_every_real_test_file_is_reachable_from_itself() -> None:
    """Guard against a new tests/**/ subdirectory the rule table can't reach.

    Such a gap must fail here, not disappear silently from every future
    selection.
    """
    shape = diff_gate.build_shape()

    for test_file in sorted(shape.existing_test_files):
        if Path(test_file).name.startswith("test_"):
            selection = diff_gate.select([test_file], shape)
            assert selection.is_all or test_file in selection.targets, test_file


def test_every_real_source_and_script_file_classifies_without_crashing() -> None:
    shape = diff_gate.build_shape()
    all_files = sorted(
        str(p.relative_to(REPO_ROOT))
        for root in (REPO_ROOT / "src", REPO_ROOT / "scripts")
        for p in root.rglob("*.py")
    )

    for path in all_files:
        selection = diff_gate.select([path], shape)
        assert selection.is_all or selection.targets  # never an empty non-ALL result


def test_real_config_module_change_degrades_to_all() -> None:
    """`config.py` fans out to most of the suite via the importer map.

    The budget degrade should catch it rather than running most of the suite
    through the (riskier) selection path.
    """
    shape = diff_gate.build_shape()

    selection = diff_gate.select(["src/swing_copilot/config.py"], shape)

    assert selection.is_all is True


# --------------------------------------------------------------------------- #
#  evaluate_changed_coverage
# --------------------------------------------------------------------------- #


def test_evaluate_changed_coverage_passes_above_threshold() -> None:
    changed = ["src/swing_copilot/screening/vcp.py"]
    payload = {
        "files": {
            str(REPO_ROOT / changed[0]): {"summary": {"percent_covered": 95.0}},
        }
    }

    ok, lines = diff_gate.evaluate_changed_coverage(payload, changed)

    assert ok is True
    assert "95.0%" in lines[0]


def test_evaluate_changed_coverage_fails_below_threshold() -> None:
    changed = ["src/swing_copilot/screening/vcp.py"]
    payload = {
        "files": {
            str(REPO_ROOT / changed[0]): {"summary": {"percent_covered": 42.0}},
        }
    }

    ok, lines = diff_gate.evaluate_changed_coverage(payload, changed)

    assert ok is False
    assert "BELOW" in lines[0]


def test_evaluate_changed_coverage_matches_relative_report_keys() -> None:
    """A relative report key must resolve to the same file as an absolute one.

    `coverage.py` may key `files` either way depending on how `--cov`
    resolved the package.
    """
    changed = ["src/swing_copilot/screening/vcp.py"]
    payload = {"files": {changed[0]: {"summary": {"percent_covered": 91.0}}}}

    ok, _lines = diff_gate.evaluate_changed_coverage(payload, changed)

    assert ok is True


def test_evaluate_changed_coverage_treats_unexercised_file_as_failing() -> None:
    changed = ["src/swing_copilot/screening/vcp.py"]

    ok, lines = diff_gate.evaluate_changed_coverage({"files": {}}, changed)

    assert ok is False
    assert "not exercised" in lines[0]


def test_evaluate_changed_coverage_ignores_unrelated_report_entries() -> None:
    changed = ["src/swing_copilot/screening/vcp.py"]
    payload = {
        "files": {
            str(REPO_ROOT / changed[0]): {"summary": {"percent_covered": 99.0}},
            str(REPO_ROOT / "src/swing_copilot/config.py"): {
                "summary": {"percent_covered": 10.0}
            },
        }
    }

    ok, lines = diff_gate.evaluate_changed_coverage(payload, changed)

    assert ok is True
    assert len(lines) == 1


# --------------------------------------------------------------------------- #
#  git-backed I/O: diff_base / changed_paths, against a real throwaway repo
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def scratch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tiny real git repo with `main` and a feature branch ahead of it.

    Exercises `diff_base`/`changed_paths` against real git plumbing rather
    than mocking subprocess -- name-status parsing, rename handling, and the
    two-shape base resolution are all git-behavior-dependent enough that a
    mock would just re-assert the implementation.
    """
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "src.py").write_text("original\n", encoding="utf-8")
    _git(tmp_path, "add", "src.py")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    _git(tmp_path, "branch", "-q", "origin/main")  # stand-in for a remote ref

    _git(tmp_path, "switch", "-q", "-c", "feature")
    (tmp_path / "src.py").write_text("changed\n", encoding="utf-8")
    _git(tmp_path, "add", "src.py")
    _git(tmp_path, "commit", "-q", "-m", "feature commit")

    monkeypatch.setattr(diff_gate, "REPO_ROOT", tmp_path)
    return tmp_path


def test_diff_base_resolves_merge_base_on_a_feature_branch(scratch_repo: Path) -> None:
    base = diff_gate.diff_base()

    assert base is not None
    # The merge-base of `feature` and `origin/main` is the initial commit.
    initial = _git(scratch_repo, "rev-parse", "origin/main").stdout.strip()
    assert base == initial


def test_diff_base_compares_directly_against_ref_when_on_main(
    scratch_repo: Path,
) -> None:
    _git(scratch_repo, "switch", "-q", "main")

    base = diff_gate.diff_base()

    assert base == "origin/main"


def test_diff_base_returns_none_without_any_resolvable_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(tmp_path, "init", "-q", "-b", "solo")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-q", "-m", "only commit")
    monkeypatch.setattr(diff_gate, "REPO_ROOT", tmp_path)

    assert diff_gate.diff_base() is None


def test_changed_paths_includes_committed_staged_unstaged_and_untracked(
    scratch_repo: Path,
) -> None:
    (scratch_repo / "staged.py").write_text("s\n", encoding="utf-8")
    _git(scratch_repo, "add", "staged.py")
    (scratch_repo / "src.py").write_text("changed\nunstaged\n", encoding="utf-8")
    (scratch_repo / "untracked.py").write_text("u\n", encoding="utf-8")

    changed = diff_gate.changed_paths(None)

    assert set(changed.paths) == {"src.py", "staged.py", "untracked.py"}
    assert changed.added == {"staged.py", "untracked.py"}


def test_changed_paths_drops_deleted_files(scratch_repo: Path) -> None:
    (scratch_repo / "src.py").unlink()

    changed = diff_gate.changed_paths(None)

    assert "src.py" not in changed.paths


def test_changed_paths_follows_rename_to_its_destination(scratch_repo: Path) -> None:
    (scratch_repo / "src.py").rename(scratch_repo / "renamed.py")
    _git(scratch_repo, "add", "-A")

    changed = diff_gate.changed_paths(None)

    assert "renamed.py" in changed.paths
    assert "src.py" not in changed.paths
    assert "renamed.py" in changed.added


def test_changed_paths_with_no_diff_and_no_working_tree_change_is_empty(
    scratch_repo: Path,
) -> None:
    _git(scratch_repo, "switch", "-q", "main")

    changed = diff_gate.changed_paths(None)

    assert changed.paths == ()


# --------------------------------------------------------------------------- #
#  CLI plumbing (select subcommand; `test` is exercised via `just` in CI, not
#  re-run here to avoid a pytest-inside-pytest recursive invocation)
# --------------------------------------------------------------------------- #


def test_run_select_prints_all_on_stdout_for_a_force_all_path(
    scratch_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `build_shape()` walks the real repo's src/tests trees via constants
    # captured at import time (SRC_ROOT/TESTS_ROOT), not the scratch repo the
    # `scratch_repo` fixture only repointed REPO_ROOT to -- so it is faked
    # out here rather than pointed at a repo tree it was never meant to see.
    # `changed_paths`/`diff_base` still run for real, against the scratch git
    # repo, which is what this test actually exercises end to end.
    monkeypatch.setattr(diff_gate, "build_shape", _shape)
    (scratch_repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    exit_code = diff_gate.main(["select"])

    assert exit_code == 0
    out, err = capsys.readouterr()
    assert out.strip() == "ALL"
    assert "diff_gate: base=" in err
