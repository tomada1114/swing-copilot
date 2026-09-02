"""Select and run only the tests a local diff can plausibly affect.

`just verify`'s old shape ran the whole 3,200-node suite plus a wheel build on
every pre-PR check, duplicating what `.github/workflows/ci.yml` already runs
on every PR. This tool replaces that with a deterministic, dependency-free
selector: a path -> pytest-target rule table (mirrors `src/swing_copilot/**`
onto `tests/**`), widened by a one-hop "which test files import this module"
reverse map built fresh from an AST walk every run (no cache, no staleness).

The design deliberately trades completeness for speed and stays fail-closed
at the edges: an unrecognized path, a shared-fixture file, or a selection
whose estimated cost (from `.test_durations`) exceeds roughly half the full
suite all degrade to running everything. CI is the backstop for whatever this
under-selects -- when that happens, the fix is a rule-table gap here, in the
same PR, not a slower local gate. See docs/goal-prompts or the PR description
for the fuller design rationale (composition-root blast radius, why the
importer map matches exact modules only, coverage-scoping decisions).

Two subcommands:
    select  Print the selected pytest targets (or "ALL") and stop.
    test    Select, run pytest, and gate the changed source files (only) at
            90% line+branch coverage -- the repo-wide 95% floor stays a
            CI-only concern, since a partial run's package-wide number would
            be systematically and confusingly pessimistic.
"""

from __future__ import annotations

import argparse
import ast
import json
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src/swing_copilot"
TESTS_ROOT = REPO_ROOT / "tests"
DURATIONS_FILE = REPO_ROOT / ".test_durations"

#: Historical mean serial seconds per test file (`.test_durations`,
#: 2026-08-20), used only when a file has no recorded duration at all -- a
#: brand-new test file, or a stale/missing `.test_durations`.
FALLBACK_MEAN_SECONDS = 4.3

#: A selection estimated above this (roughly half of the ~516s full suite)
#: degrades to ALL: running most of the suite through the selector buys
#: nothing but extra risk over just running all of it.
BUDGET_SECONDS = 250.0

#: Below this estimate, `-n auto`'s worker-spawn cost (~1.2s/worker observed)
#: outweighs the parallelism it buys.
PARALLEL_THRESHOLD_SECONDS = 8.0

#: Line+branch coverage floor for changed source files only. Deliberately
#: below the repo-wide 95% floor CI enforces: a partial local run is exercised
#: by fewer tests than the full suite, so its number is a floor for *this*
#: file, not a substitute for the repo-wide gate.
CHANGED_FILE_COVERAGE_THRESHOLD = 90.0

#: When more than this fraction of a selection's estimated cost comes from
#: files absent in `.test_durations`, the estimate is noted as unreliable
#: rather than presented as precise.
UNKNOWN_DURATION_WARNING_RATIO = 0.10

ENV_SANITY_TARGETS = ("tests/test_package.py", "tests/test_quality_contracts.py")
_ALWAYS_APPEND = ("tests/test_quality_contracts.py",)
_SRC_CHANGE_APPEND = ("tests/test_e2e_smoke.py",)
_QUALITY_CONTRACT_TARGETS = (
    "tests/test_quality_contracts.py",
    "tests/analysis/test_skill_contract.py",
)

#: Paths whose blast radius the selector cannot reason about (a shared
#: fixture, a lockfile, a build config) -- and this module's own source,
#: because a bug here must not be able to hide its own regression tests from
#: itself.
_FORCE_ALL_EXACT = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "justfile",
        ".python-version",
        "scripts/diff_gate.py",
        "tests/conftest.py",
        "tests/__init__.py",
    }
)
_FORCE_ALL_PREFIXES = ("tests/support/", "config/")

_IGNORED_EXACT = frozenset(
    {
        "mkdocs.yml",
        "typos.toml",
        ".pre-commit-config.yaml",
        "LICENSE",
        ".gitignore",
        ".editorconfig",
        ".gitattributes",
    }
)
_IGNORED_PREFIXES = ("docs/", "data/", "reports/", "dist/", "site/")


@dataclass(frozen=True, slots=True)
class RepoShape:
    """Structural facts about the repo the pure rule engine needs.

    Injected rather than read from disk inside `select()` so the engine stays
    a pure function of `(changed, shape)` -- every rule in the table can be
    exercised with a literal `RepoShape`, no git or filesystem required.
    """

    src_packages: frozenset[str]
    existing_test_files: frozenset[str]
    existing_test_dirs: frozenset[str]
    importers: Mapping[str, frozenset[str]]
    durations: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class Selection:
    """The result of scoping a diff to the tests it can plausibly affect."""

    is_all: bool
    is_empty_diff: bool
    degraded: bool
    targets: frozenset[str]
    reasons: tuple[str, ...]
    estimated_seconds: float
    unknown_duration_count: int
    file_count: int
    use_parallel: bool


def module_name(src_path: str) -> str:
    """Dotted module name for a `src/swing_copilot/**` file.

    `.../screening/__init__.py` -> `swing_copilot.screening` (the package
    import, not a literal `.__init__` module), matching how a test actually
    spells the import it depends on.
    """
    dotted = src_path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    return dotted.removesuffix(".__init__")


def _files_under(target: str, shape: RepoShape) -> frozenset[str]:
    """Expand one pytest target (a file or a `tests/<pkg>` directory) to files."""
    if target in shape.existing_test_files:
        return frozenset({target})
    prefix = f"{target}/"
    return frozenset(f for f in shape.existing_test_files if f.startswith(prefix))


def _estimate(targets: Iterable[str], shape: RepoShape) -> tuple[float, int, int]:
    """Estimate a selection's serial runtime from `.test_durations`.

    Returns:
        `(estimated_seconds, unknown_file_count, file_count)`.
    """
    mean = (
        statistics.fmean(shape.durations.values())
        if shape.durations
        else FALLBACK_MEAN_SECONDS
    )
    seen: set[str] = set()
    total = 0.0
    unknown = 0
    for target in targets:
        seen.update(_files_under(target, shape))
    for file_path in seen:
        known = shape.durations.get(file_path)
        if known is None:
            total += mean
            unknown += 1
        else:
            total += known
    return total, unknown, len(seen)


def _dedupe(targets: set[str]) -> frozenset[str]:
    """Drop a file target whose immediate parent directory is also selected."""
    dirs = {t for t in targets if not t.endswith(".py")}
    return frozenset(
        t
        for t in targets
        if t.endswith(".py") is False or t.rsplit("/", 1)[0] not in dirs
    )


class _Classification:
    """One rule's verdict for a single changed path."""

    __slots__ = ("kind", "reason", "targets")

    def __init__(self, kind: str, targets: frozenset[str], reason: str) -> None:
        self.kind = kind  # "ALL" | "NONE" | "TARGETS"
        self.targets = targets
        self.reason = reason


def _classify_tests_path(path: str) -> _Classification | None:
    """Rule branch for `tests/**` -- a test itself, its conftest, or a helper."""
    if not (path.startswith("tests/") and path.endswith(".py")):
        return None
    name = path.rsplit("/", 1)[-1]
    parent = path.rsplit("/", 1)[0]
    if name == "conftest.py":
        return _Classification(
            "TARGETS", frozenset({parent}), f"{path}: package conftest -> {parent}"
        )
    if name.startswith("test_"):
        return _Classification(
            "TARGETS", frozenset({path}), f"{path}: test file -> itself"
        )
    return _Classification(
        "TARGETS", frozenset({parent}), f"{path}: test helper -> {parent}"
    )


def _classify_src_package_file(
    path: str, pkg: str, shape: RepoShape
) -> _Classification:
    """`src/swing_copilot/<pkg>/**` -> that package's tests dir plus importers."""
    targets: set[str] = set()
    pkg_dir = f"tests/{pkg}"
    if pkg_dir in shape.existing_test_dirs:
        targets.add(pkg_dir)
    if path.endswith(".py"):
        targets |= set(shape.importers.get(module_name(path), frozenset()))
    if not targets:
        return _Classification(
            "ALL",
            frozenset(),
            f"{path}: package {pkg} has no tests dir or importer -> ALL",
        )
    return _Classification(
        "TARGETS", frozenset(targets), f"{path}: -> {sorted(targets)}"
    )


def _classify_src_top_level_file(path: str, shape: RepoShape) -> _Classification:
    """`src/swing_copilot/<mod>.py` -> its dedicated test file plus importers."""
    module = module_name(path)
    dedicated = f"tests/test_{module.rsplit('.', 1)[-1]}.py"
    targets = set(shape.importers.get(module, frozenset()))
    if dedicated in shape.existing_test_files:
        targets.add(dedicated)
    if not targets:
        return _Classification(
            "ALL",
            frozenset(),
            f"{path}: top-level module, no dedicated test/importer -> ALL",
        )
    return _Classification(
        "TARGETS", frozenset(targets), f"{path}: -> {sorted(targets)}"
    )


def _classify_src_path(path: str, shape: RepoShape) -> _Classification | None:
    """Rule branch for `src/swing_copilot/**`."""
    if not path.startswith("src/swing_copilot/"):
        return None
    rest = path.removeprefix("src/swing_copilot/")
    if "/" in rest:
        pkg = rest.split("/", 1)[0]
        if pkg in shape.src_packages:
            return _classify_src_package_file(path, pkg, shape)
        return None
    if path.endswith(".py"):
        return _classify_src_top_level_file(path, shape)
    return None


def _classify_scripts_path(path: str, shape: RepoShape) -> _Classification | None:
    """Rule branch for `scripts/**`: a `.py` maps by name, a `.sh` is inert."""
    if not path.startswith("scripts/"):
        return None
    if path.endswith(".sh"):
        return _Classification(
            "NONE", frozenset(), f"{path}: shell script -> always-append set only"
        )
    if not path.endswith(".py"):
        return None
    name = path.removeprefix("scripts/").removesuffix(".py")
    dedicated = f"tests/test_{name}.py"
    if dedicated in shape.existing_test_files:
        return _Classification(
            "TARGETS", frozenset({dedicated}), f"{path}: script -> {dedicated}"
        )
    return _Classification(
        "ALL", frozenset(), f"{path}: script with no dedicated test -> ALL"
    )


def _classify_config_path(path: str) -> _Classification | None:
    """Rule branch for CI/skill config, docs/metadata, and generated/data paths."""
    if path.startswith((".github/workflows/", ".claude/")):
        return _Classification(
            "TARGETS",
            frozenset(_QUALITY_CONTRACT_TARGETS),
            f"{path}: CI/skill config -> quality-contract tests",
        )
    if path.startswith("docs/") or path.endswith(".md") or path in _IGNORED_EXACT:
        return _Classification(
            "NONE", frozenset(), f"{path}: docs/metadata -> no tests"
        )
    if path.startswith(_IGNORED_PREFIXES):
        return _Classification(
            "NONE", frozenset(), f"{path}: generated/data path -> no tests"
        )
    return None


def _classify(path: str, shape: RepoShape) -> _Classification:
    """Apply the rule table to one changed path. First match wins."""
    if path in _FORCE_ALL_EXACT or path.startswith(_FORCE_ALL_PREFIXES):
        return _Classification("ALL", frozenset(), f"{path}: shared/build path -> ALL")

    result = _classify_tests_path(path)
    if result is None:
        result = _classify_src_path(path, shape)
    if result is None:
        result = _classify_scripts_path(path, shape)
    if result is None:
        result = _classify_config_path(path)
    if result is not None:
        return result

    return _Classification("ALL", frozenset(), f"{path}: unrecognized path -> ALL")


def select(changed: Sequence[str], shape: RepoShape) -> Selection:
    """Scope `changed` (repo-relative paths) to the tests they can affect.

    Pure function of its arguments: no git, no filesystem, no `.test_durations`
    parsing. `main()` builds `RepoShape` and the changed-path list from the
    live repo and hands both in here.
    """
    total_suite_seconds = sum(shape.durations.values())

    if not changed:
        estimated, unknown, file_count = _estimate(ENV_SANITY_TARGETS, shape)
        return Selection(
            is_all=False,
            is_empty_diff=True,
            degraded=False,
            targets=frozenset(ENV_SANITY_TARGETS),
            reasons=("no changes vs base; ran env-sanity subset only",),
            estimated_seconds=estimated,
            unknown_duration_count=unknown,
            file_count=file_count,
            use_parallel=False,
        )

    raw_targets: set[str] = set()
    reasons: list[str] = []
    triggered_all = False
    for path in changed:
        classification = _classify(path, shape)
        reasons.append(classification.reason)
        if classification.kind == "ALL":
            triggered_all = True
        elif classification.kind == "TARGETS":
            raw_targets |= classification.targets

    if any(path.startswith("src/") for path in changed):
        raw_targets.update(_SRC_CHANGE_APPEND)
    raw_targets.update(_ALWAYS_APPEND)

    if triggered_all:
        return Selection(
            is_all=True,
            is_empty_diff=False,
            degraded=False,
            targets=frozenset(),
            reasons=tuple(reasons),
            estimated_seconds=total_suite_seconds,
            unknown_duration_count=0,
            file_count=len(shape.existing_test_files),
            use_parallel=True,
        )

    targets = _dedupe(raw_targets)
    estimated, unknown, file_count = _estimate(targets, shape)
    if estimated > BUDGET_SECONDS:
        reasons.append(
            f"budget degrade: estimated {estimated:.1f}s > budget "
            f"{BUDGET_SECONDS:.0f}s -> ALL"
        )
        return Selection(
            is_all=True,
            is_empty_diff=False,
            degraded=True,
            targets=frozenset(),
            reasons=tuple(reasons),
            estimated_seconds=total_suite_seconds,
            unknown_duration_count=0,
            file_count=len(shape.existing_test_files),
            use_parallel=True,
        )

    return Selection(
        is_all=False,
        is_empty_diff=False,
        degraded=False,
        targets=targets,
        reasons=tuple(reasons),
        estimated_seconds=estimated,
        unknown_duration_count=unknown,
        file_count=file_count,
        use_parallel=estimated > PARALLEL_THRESHOLD_SECONDS,
    )


# --------------------------------------------------------------------------- #
#  I/O: git, the source tree, and .test_durations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ChangedPaths:
    """Changed paths, plus which of them are newly added (not modified)."""

    paths: tuple[str, ...]
    added: frozenset[str]


def _run_git(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_base_ref() -> str | None:
    """The remote-tracking `main`, falling back to a local `main` branch."""
    for ref in ("origin/main", "main"):
        if _run_git(["rev-parse", "--verify", ref]).returncode == 0:
            return ref
    return None


def _current_branch() -> str:
    result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else ""


def diff_base() -> str | None:
    """Resolve the diff base without ever fetching (stays offline-capable).

    On a feature branch this is `merge-base(origin/main, HEAD)`: a stale
    `origin/main` only moves the base backwards, which over-selects rather
    than under-selects. On `main` itself, `merge-base` collapses to `HEAD`
    the moment a commit lands, which would silently zero out the committed
    half of the diff -- so `main` compares directly against the resolved ref
    instead.
    """
    ref = _resolve_base_ref()
    if ref is None:
        return None
    if _current_branch() == "main":
        return ref
    result = _run_git(["merge-base", ref, "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else None


def _merge_name_status(statuses: dict[str, str], output: str) -> None:
    for line in output.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        # A rename/copy line is `status\told\tnew`; every other status is
        # `status\tpath`. The last field is always the path that matters.
        statuses[fields[-1]] = fields[0]


def changed_paths(base_override: str | None) -> ChangedPaths:
    """Every path this diff touches: committed, staged, unstaged, untracked.

    Filters to paths that still exist on disk, so a deleted file never
    reaches pytest as a target it can't find (exit code 4).
    """
    statuses: dict[str, str] = {}
    base = base_override or diff_base()
    if base:
        committed = _run_git(["diff", "--name-status", "-M", f"{base}..HEAD"])
        if committed.returncode == 0:
            _merge_name_status(statuses, committed.stdout)
    staged = _run_git(["diff", "--name-status", "-M", "--cached"])
    if staged.returncode == 0:
        _merge_name_status(statuses, staged.stdout)
    unstaged = _run_git(["diff", "--name-status", "-M"])
    if unstaged.returncode == 0:
        _merge_name_status(statuses, unstaged.stdout)
    untracked = _run_git(["ls-files", "--others", "--exclude-standard"])
    if untracked.returncode == 0:
        for path in untracked.stdout.splitlines():
            if path:
                statuses[path] = "A"

    existing = tuple(sorted(p for p in statuses if (REPO_ROOT / p).exists()))
    added = frozenset(p for p in existing if statuses[p].startswith(("A", "R", "C")))
    return ChangedPaths(paths=existing, added=added)


def _src_packages() -> frozenset[str]:
    return frozenset(
        path.name
        for path in SRC_ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").exists()
    )


def _all_test_files() -> frozenset[str]:
    return frozenset(
        str(path.relative_to(REPO_ROOT))
        for path in TESTS_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _existing_test_dirs() -> frozenset[str]:
    return frozenset(
        f"tests/{path.name}"
        for path in TESTS_ROOT.iterdir()
        if path.is_dir() and path.name != "__pycache__"
    )


def _build_importers(test_files: frozenset[str]) -> dict[str, frozenset[str]]:
    """Reverse map: dotted `swing_copilot.*` module -> test files that import it.

    Exact-module match only, deliberately not resolved to ancestor packages:
    a test importing bare `swing_copilot.screening` would otherwise pull in
    every test file for every change under `screening/`, which is exactly the
    composition-root over-selection this design avoids elsewhere.
    """
    importers: dict[str, set[str]] = {}
    for test_file in sorted(test_files):
        try:
            source = (REPO_ROOT / test_file).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=test_file)
        except OSError, SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_swing_copilot_module(alias.name):
                        importers.setdefault(alias.name, set()).add(test_file)
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module
            if module is None or not _is_swing_copilot_module(module):
                continue
            importers.setdefault(module, set()).add(test_file)
            for alias in node.names:
                key = f"{module}.{alias.name}"
                importers.setdefault(key, set()).add(test_file)
    return {module: frozenset(files) for module, files in importers.items()}


def _is_swing_copilot_module(name: str | None) -> bool:
    return name is not None and (
        name == "swing_copilot" or name.startswith("swing_copilot.")
    )


def _load_durations() -> dict[str, float]:
    """Aggregate `.test_durations` (per pytest node) to per-file seconds.

    Missing or unparsable is not an error -- the estimate degrades to the
    fallback mean and `select()` says so, rather than failing the gate over a
    stale or absent optimization hint.
    """
    if not DURATIONS_FILE.exists():
        return {}
    try:
        raw = json.loads(DURATIONS_FILE.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return {}
    per_file: dict[str, float] = {}
    if not isinstance(raw, dict):
        return {}
    for node_id, seconds in raw.items():
        file_path = str(node_id).split("::", 1)[0]
        try:
            per_file[file_path] = per_file.get(file_path, 0.0) + float(seconds)
        except TypeError, ValueError:
            continue
    return per_file


def build_shape() -> RepoShape:
    """Assemble `RepoShape` from the live repository."""
    test_files = _all_test_files()
    return RepoShape(
        src_packages=_src_packages(),
        existing_test_files=test_files,
        existing_test_dirs=_existing_test_dirs(),
        importers=_build_importers(test_files),
        durations=_load_durations(),
    )


# --------------------------------------------------------------------------- #
#  Diagnostics and changed-file coverage
# --------------------------------------------------------------------------- #


def _print_diagnostics(
    selection: Selection, changed: ChangedPaths, base: str | None
) -> None:
    """Everything needed to diagnose a red CI after a green local gate."""
    print(f"diff_gate: base={base or '(none; working tree only)'}", file=sys.stderr)
    for reason in selection.reasons:
        print(f"  {reason}", file=sys.stderr)

    if selection.is_empty_diff:
        print(
            "diff_gate: no changes vs base -- env-sanity subset only", file=sys.stderr
        )
    elif selection.is_all:
        note = "budget degrade" if selection.degraded else "explicit rule"
        print(f"diff_gate: selected ALL ({note})", file=sys.stderr)
    else:
        print(
            f"diff_gate: selected {selection.file_count} file(s), "
            f"~{selection.estimated_seconds:.1f}s serial "
            f"({'parallel' if selection.use_parallel else 'serial'})",
            file=sys.stderr,
        )
        if (
            selection.file_count
            and selection.unknown_duration_count / selection.file_count
            > UNKNOWN_DURATION_WARNING_RATIO
        ):
            print(
                f"diff_gate: {selection.unknown_duration_count}/"
                f"{selection.file_count} files have no recorded duration -- "
                "estimate above is unreliable",
                file=sys.stderr,
            )

    new_src_files = sorted(
        p
        for p in changed.added
        if p.startswith("src/swing_copilot/") and p.endswith(".py")
    )
    touched_tests = any(
        p.startswith("tests/") and p.endswith(".py") for p in changed.paths
    )
    if new_src_files and not touched_tests:
        print(
            "diff_gate: new src file(s) added with no tests/**/test_*.py touched "
            f"in this diff ({', '.join(new_src_files)}) -- CI's repo-wide "
            "coverage gate will likely fail",
            file=sys.stderr,
        )

    print(
        "diff_gate: repo-wide line+branch coverage is enforced in CI only "
        f"(this gate only checks changed files, >= "
        f"{CHANGED_FILE_COVERAGE_THRESHOLD:.0f}%)",
        file=sys.stderr,
    )


def evaluate_changed_coverage(
    payload: Mapping[str, object], changed_src_files: Sequence[str]
) -> tuple[bool, tuple[str, ...]]:
    """Check `coverage json`'s per-file summary against the changed-file floor.

    Matches by resolved absolute path rather than the report's raw key,
    because `coverage.py` may key `files` by a path relative to the working
    directory or an absolute one depending on how `--cov` resolved the
    package -- string-matching the raw key against `changed_src_files` would
    be fragile to that.

    Returns:
        `(all_ok, report_lines)` -- `report_lines` are diagnostic, not proof;
        the caller prints them.
    """
    files_raw = payload.get("files")
    files: Mapping[str, object] = files_raw if isinstance(files_raw, dict) else {}
    by_abs_path: dict[str, Mapping[str, object]] = {}
    for key, entry in files.items():
        if not isinstance(entry, dict):
            continue
        key_path = Path(key)
        absolute = key_path if key_path.is_absolute() else REPO_ROOT / key_path
        try:
            by_abs_path[str(absolute.resolve())] = entry
        except OSError:
            continue

    all_ok = True
    lines: list[str] = []
    for src_file in sorted(changed_src_files):
        target = str((REPO_ROOT / src_file).resolve())
        entry = by_abs_path.get(target)
        if entry is None:
            lines.append(f"{src_file}: not exercised by the selected tests (0%)")
            all_ok = False
            continue
        summary = entry.get("summary")
        percent = summary.get("percent_covered") if isinstance(summary, dict) else None
        if not isinstance(percent, int | float):
            lines.append(f"{src_file}: no coverage summary in the report")
            all_ok = False
            continue
        status = "OK" if percent >= CHANGED_FILE_COVERAGE_THRESHOLD else "BELOW"
        lines.append(f"{src_file}: {percent:.1f}% line+branch ({status})")
        if percent < CHANGED_FILE_COVERAGE_THRESHOLD:
            all_ok = False
    return all_ok, tuple(lines)


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #


def _run_select(base_override: str | None) -> int:
    shape = build_shape()
    changed = changed_paths(base_override)
    selection = select(changed.paths, shape)
    _print_diagnostics(selection, changed, base_override or diff_base())
    if selection.is_all:
        print("ALL")
    else:
        for target in sorted(selection.targets):
            print(target)
    return 0


def _run_test(base_override: str | None, extra_pytest_args: list[str]) -> int:
    shape = build_shape()
    changed = changed_paths(base_override)
    selection = select(changed.paths, shape)
    _print_diagnostics(selection, changed, base_override or diff_base())

    changed_src_files = [
        p
        for p in changed.paths
        if p.startswith("src/swing_copilot/") and p.endswith(".py")
    ]

    pytest_args: list[str] = ["-q"]
    if selection.use_parallel:
        pytest_args += ["-n", "auto"]

    coverage_json = REPO_ROOT / ".diff_gate_coverage.json"
    if changed_src_files:
        pytest_args += [
            "--cov=swing_copilot",
            "--cov-branch",
            f"--cov-report=json:{coverage_json}",
            "--cov-report=",
        ]

    if not selection.is_all:
        pytest_args += sorted(selection.targets)
    pytest_args += extra_pytest_args

    import pytest  # noqa: PLC0415 - kept out of `select`'s import path on purpose

    try:
        exit_code = int(pytest.main(pytest_args))
        if exit_code != 0 or not changed_src_files:
            return exit_code
        return _gate_changed_coverage(coverage_json, changed_src_files)
    finally:
        # Always cleaned up, whichever branch above returned: a no-op when the
        # coverage flags were never added (no changed source files).
        coverage_json.unlink(missing_ok=True)


def _gate_changed_coverage(
    coverage_json: Path, changed_src_files: Sequence[str]
) -> int:
    """Read the coverage report `_run_test` produced and apply the 90% floor."""
    try:
        payload = json.loads(coverage_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(
            f"diff_gate: warning: could not read coverage report ({error}); "
            "skipping the changed-file coverage gate",
            file=sys.stderr,
        )
        return 0

    ok, lines = evaluate_changed_coverage(payload, changed_src_files)
    print("diff_gate: changed-file coverage:", file=sys.stderr)
    for line in lines:
        print(f"  {line}", file=sys.stderr)
    if not ok:
        print(
            f"diff_gate: error: changed-file coverage below "
            f"{CHANGED_FILE_COVERAGE_THRESHOLD:.0f}%. A test outside this "
            "diff's selection may cover the gap -- re-check with "
            "`just verify-full`, or add a unit test for the changed file.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `select` prints targets, `test` runs the gate."""
    parser = argparse.ArgumentParser(
        prog="diff_gate", description="Scope the local test run to the current diff."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    select_parser = subparsers.add_parser(
        "select", help="Print the pytest targets this diff selects (or ALL)"
    )
    select_parser.add_argument(
        "--base", default=None, help="Override the diff base ref (e.g. for calibration)"
    )

    test_parser = subparsers.add_parser(
        "test", help="Run the selected tests, then gate changed files at 90%% coverage"
    )
    test_parser.add_argument("--base", default=None)
    test_parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Extra arguments forwarded to pytest",
    )

    args = parser.parse_args(argv)
    if args.command == "select":
        return _run_select(args.base)
    return _run_test(args.base, list(args.pytest_args))


if __name__ == "__main__":
    raise SystemExit(main())
