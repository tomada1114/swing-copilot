---
name: public-api-contract
description: >
  Covers what is public in this package: `swing_copilot/__init__.py`'s `__all__`, the
  `copilot-*` console scripts in `pyproject.toml`'s `[project.scripts]`, `py.typed`, and
  the "one shared primitive per invariant" rule `tests/test_quality_contracts.py`
  enforces for `io_atomic`, `exceptions.SwingCopilotError`, `strict_model.StrictModel`,
  and `cli_support.run_cli`. Use when adding or removing a name from `__all__`, adding a
  thirteenth `copilot-*` entry point (twelve exist today), reimplementing atomic file
  replacement or a strict schema base instead of importing the shared one, or running
  `just smoke`.
---

# Public API Contract

**Owns:** what is public — `__init__.py`'s `__all__`, the `copilot-*` console scripts,
`py.typed`, and the shared-primitive-implemented-once rule. **Does not own:** the semver
level and CHANGELOG wording for a surface change (`release-impact`), where a surface
change gets documented (`updating-docs`), the `analysis_input.json`/`analysis_result.json`
schemas at the skill boundary (`guarding-analysis-boundary`).

## `__all__` is a deliberate allowlist

`swing_copilot/__init__.py`'s `__all__` currently names eight symbols:
`ConfigError`, `Secrets`, `Settings`, `SwingCopilotError`, `__version__`,
`load_secrets`, `load_settings`, `require_secrets` — the settings/secrets loading
surface, the package-level exception base, and the version string. It is not a dump of
everything importable from the package; most of `src/swing_copilot/` (screening, risk,
storage, retro, tracking, ...) is reachable by dotted import but never re-exported here,
because a caller outside this repository has no business constructing a `ScreeningResult`
or a `Database` directly — those are internal to the pipeline, not the package's stated
contract. `tests/test_package.py`'s `test_public_exports` pins the exact set; adding or
removing a name is a deliberate edit to that test in the same change, not a side effect.

A name qualifies for `__all__` when an external caller of the package (not a `copilot-*`
CLI, not this repository's own tests) plausibly needs to import it directly — today that
is config loading and the shared error base, nothing else. Reject in review: an addition
made because "it's exported from its own module anyway" — that is true of nearly
everything in `src/`, and is not the bar.

## The `copilot-*` console scripts are a skill-facing contract

`pyproject.toml`'s `[project.scripts]` lists twelve `copilot-*` entry points. Their
names, flags, exit codes, and stderr tags (`PREFLIGHT_ABORT[<reason>]:`, for example) are
consumed by Claude Code skills and by CI — `.claude/skills/swing-daily/SKILL.md` and
`.github/workflows/swing-daily.yml` invoke these commands by exact name, and
`scripts/check_daily_complete.py` parses `copilot-daily`'s stderr tag. Consolidating the
twelve commands into fewer entry points was explicitly considered and rejected:
`cli_support.py`'s module docstring states it outright — "each is a stable, skill-facing
entry point, and consolidating them is explicitly not the goal (Issue #193)." Renaming or
removing one is therefore a break of that contract, not an internal refactor, even if the
underlying Python function it wraps stays the same.

`tests/test_quality_contracts.py`'s `test_every_console_script_converts_its_errors_through_cli_support`
maps each script name to the module that owns its `main()` (`CLI_ERROR_CONVERSION_MODULES`)
and asserts `pyproject.toml`'s script set matches that mapping exactly and that every one
imports `run_cli` from `cli_support`. A thirteenth `copilot-*` script therefore fails this
test until it is added to `CLI_ERROR_CONVERSION_MODULES` — which is also the moment to
decide, deliberately, whether it may hand-write its own error-to-exit-code conversion or
must go through `cli_support.run_cli` like every other one (it must). **REQUIRED:**
`designing-errors` for what `run_cli`/`ExitPolicy` require of that module.

## Shared primitives: implemented exactly once

Four cross-cutting invariants each have exactly one implementation, and
`tests/test_quality_contracts.py` enforces each mechanically via AST inspection — not
just documented and hoped for:

| Primitive | Module | Test that enforces it |
|---|---|---|
| Atomic file replacement | `swing_copilot.io_atomic` | `test_only_io_atomic_replaces_files_in_place` |
| Exception base | `swing_copilot.exceptions.SwingCopilotError` | `test_every_error_class_derives_from_the_package_base` |
| Strict (`extra="forbid"`) schema base | `swing_copilot.strict_model.StrictModel` | `test_strict_schema_config_is_declared_once` |
| Domain error → `SystemExit` | `swing_copilot.cli_support.run_cli` | `test_every_console_script_converts_its_errors_through_cli_support` |

`test_only_io_atomic_replaces_files_in_place` is the strictest of the four: it does not
just ban an *import* of a competing implementation, it walks the AST for a hand-rolled
atomic replace anywhere outside `io_atomic.py` — `os.replace`/`os.rename`,
`tempfile.mkstemp`/`NamedTemporaryFile`, `shutil.move` (including via `from <module>
import <name>`), and the method-style `Path.replace(...)`/`Path.rename(...)` (matched by
call shape — exactly one positional argument, no keywords — since the method name alone
collides with `str.replace`/`datetime.replace`). Two call sites are named exceptions in
`_ATOMIC_REPLACEMENT_ALLOWLIST`, each with its own recorded reason
(`scripts/data_sync.py`'s `_download_verified` streams a large download to a staging file
without doubling memory; `scripts/bootstrap.py`'s `_rename_source_directory` renames a
whole directory, which is out of `io_atomic`'s one-file-of-bytes scope entirely) — adding
a new exception means adding a new named, reasoned entry, never silencing the test
another way.

`test_every_error_class_derives_from_the_package_base` and
`test_strict_schema_config_is_declared_once` are covered in depth in `designing-errors`
and `guarding-analysis-boundary` respectively; this skill's stake in them is narrower: a
reader who sees one of these four tests fail should know it means "a second
implementation of a primitive that must have exactly one" before opening the diff.

## `py.typed`

`src/swing_copilot/py.typed` marks the package as inline-typed (PEP 561): a downstream
project's own mypy/pyright run type-checks against this package's real signatures, not
against `Any`. A public function's parameter and return types are therefore part of the
contract the moment it is reachable from `__all__` or a console script's argument
parsing — changing one is the same weight as changing the function's behavior.

## `just smoke` verifies the surface as built

`just smoke` (`scripts/smoke_test.py`) builds the wheel, installs it into a fresh
temporary virtualenv, and imports every top-level module the wheel's own file manifest
implies — then fails if any imported module's `__file__` resolves back into this
repository's working tree instead of the installed wheel. A failure here almost always means
one of two things: `[tool.hatch.build.targets.wheel]`'s `packages` list (or a
`.gitignore`/build-exclude rule) left a module out of the built wheel even though the
source tree has it, or the check ran with the repository itself still on `sys.path` (a
stale editable install, or a test invoked from inside the source tree) and is silently
validating the working copy instead of the artifact. Either way, treat it as a packaging
defect to fix, not a test to loosen.

## Documenting a public addition

A new `__all__` export or console script also needs `docs/reference.md` and the README
examples kept current — AGENTS.md's Architecture section states this directly. This
skill covers *what* qualifies as public; **REQUIRED:** `updating-docs` for *where* that
documentation lives and its own conventions.
