---
name: writing-python
description: >
  Covers how one module or function under src/swing_copilot/**/*.py or scripts/**/*.py
  is written: mypy-strict typing and `Any`, `@dataclass(frozen=True, slots=True)` vs
  pydantic `BaseModel` vs `TypedDict`, when a `Protocol` earns its place, the
  `from __future__ import annotations` + `TYPE_CHECKING` module shape, EAFP,
  `match`/`case`, `enum.Enum`/`StrEnum`, the 300-line-module and 40-line-function review
  triggers, parameter grouping, Google-style docstrings, and ruff's bandit (`S`) rules.
  Use when writing or reviewing a `src/`/`scripts/` module, deciding whether a new type
  needs a `Protocol`, adding a `# noqa`, or a function is growing past 40 lines.
---

# Writing Python

**Owns:** how one module or function under `src/swing_copilot/**/*.py` and
`scripts/**/*.py` is written — typing, value-object shape, control flow, naming,
module/function size, docstrings, performance idioms, security. **Does not own:** the
exception hierarchy and exit codes (`designing-errors`), what may be exported
(`public-api-contract`), how a test is written (`writing-tests`), any domain invariant —
point-in-time visibility, storage atomicity, external-boundary retry, the analysis
schema boundary (`enforcing-point-in-time`, `writing-storage-code`,
`writing-external-adapters`, `guarding-analysis-boundary`).

Formatting is already applied to every edited `.py` file by a PostToolUse hook, so do
not re-run a formatter by hand (`changing-gates` owns the hooks).

## `Any` under mypy strict

mypy strict (`pyproject.toml`'s `[tool.mypy]`) makes an unannotated or bare `Any` a type
hole that swallows everything downstream of it. When it is genuinely unavoidable — an
untyped third-party return, JSON with no static shape, a pandas cell — annotate the
narrowest container you can and add a same-line `# Any:` comment explaining *why*, not
restating that it is `Any`:

```python
metrics: dict[str, Any] = json.loads(metrics_json)  # Any: JSON has no static shape
```

Real examples: `storage/history_queries.py`'s `metrics`/`value` locals, `config.py`'s
`model_dump` result ("`model_dump` is untyped per-section"), `tracking/update.py`'s
`list[dict[Any, Any]]` ("pandas records are heterogeneous by column"),
`screening/rejection_classifier.py`'s `# type: ignore[arg-type]  # Any: object cell from
a DataFrame row". Reject in review: a bare `Any` with no comment, or a comment that just
says "Any: needed here" without naming the actual untyped source.

## Value objects: dataclass, pydantic, TypedDict

- `@dataclass(frozen=True, slots=True)` is the default for an internal value object —
  over 260 in `src/`. Immutable, memory-efficient, and needs nothing external to define.
- `pydantic.BaseModel` is reserved for a serialization boundary, concretely
  `swing_copilot.strict_model.StrictModel` for every schema that crosses the skill
  boundary (`analysis_input.json`, `analysis_result.json`, retro dossiers). Reaching for
  pydantic on a value that never serializes across that boundary buys runtime validation
  cost for nothing a frozen dataclass doesn't already give at the type level.
- `TypedDict` is for a structured dict shape you build before it becomes something else —
  `data/yfinance_provider.py`'s `_ActionRow`: "One `ACTIONS_COLUMNS` row, kept typed
  until it becomes a frame."

## `Protocol`: only volatile or failure-prone boundaries

A `Protocol` earns its place when the boundary crosses a process/network/wall-clock line,
or when the concrete implementation is swapped by config or by a test double and the
caller only needs a narrow structural slice of it — never as a blanket abstraction over
an internal, single-implementation collaborator.

Real examples and why each qualifies: `clock.Clock` (wall-clock access swapped for a
fixed instant in tests); `data/base.py`'s `DataProvider` (external market-data adapter,
swappable per provider); `screening/base.py`'s `Filter`/`Signal` (strategy-configured
plugin points, one implementation loaded per configured name); the private `_XLike`
Protocols — `pipeline/daily.py`'s `_EdgarClientLike`/`_NewsClientLike`, `data/edgar.py`'s
`_FilingLike`/`_CompanyLike`, `text/news_finnhub.py`'s `_HttpGet` — each names only the
handful of methods the calling module actually uses off a much larger real client, so a
test double needs to implement three methods, not the whole SDK.

Anti-pattern to reject in review: a `Protocol` wrapping an internal class that has, and
will only ever have, one implementation — that is an abstraction layer with no second
implementation to justify it. Use a concrete class and constructor injection instead.

## Module shape: `from __future__ import annotations` + `TYPE_CHECKING`

Every module starts with `from __future__ import annotations` — ruff's isort
(`[tool.ruff.lint.isort].required-imports`) enforces it, so a missing one is a lint
failure, not a style choice. Anything imported only for a type annotation goes under
`if TYPE_CHECKING:` — `cli_support.py` is the model, and its module docstring states the
reason out loud: "At runtime this module imports nothing from `swing_copilot` beyond
`Secrets` (under `TYPE_CHECKING` only, ... )". A `TYPE_CHECKING`-only import cannot
accidentally become a runtime dependency; ruff's `TC` rules (flake8-type-checking) flag a
name used outside an annotation that is still guarded that way.

The one deliberate exception: pydantic evaluates annotations at runtime, so a name used
as a `StrictModel` field type needs a real import, not a `TYPE_CHECKING` one.
`[tool.ruff.lint.flake8-type-checking].runtime-evaluated-base-classes` names
`pydantic.BaseModel` and `swing_copilot.strict_model.StrictModel` so ruff does not
wrongly suggest guarding those imports.

## Control flow

- EAFP over LBYL for I/O and duck typing — try the operation and catch the specific
  failure, rather than checking preconditions that can still race.
- `match`/`case` for dispatch over a closed enum, not a chain of `if`/`elif`.
  `screening/indicators.py`'s `_compute` is the model: one `case` per `_IndicatorKind`
  member, each returning the indicator's own computation.
- Context managers for every resource — a DuckDB connection, an open file — never a bare
  `open()`/`connect()` without `with`.
- `enum.Enum`/`enum.StrEnum` for a fixed vocabulary instead of string constants.
  `models.py`'s `RunStatus`/`StepStatus` are plain `Enum`; `regime/gate.py`'s
  `GateVerdict` and `backtest/policy.py`'s `EntryPolicyArm` are `StrEnum` — reach for
  `StrEnum` specifically when the value must also compare or serialize as a plain string
  (a storage column, report text), plain `Enum` when it stays internal to the process.

## Size: 300-line modules, 40-line functions are triggers, not laws

Several modules in this repository are well past 300 lines on purpose:
`pipeline/daily.py` (1878), `backtest/cli.py` (1458), `storage/market_store.py` (1336),
`retro/aggregate.py` (1196), `storage/verdict_records.py` and `storage/schema.py` (1166
each). A legitimate reason to exceed the trigger looks like theirs: the module owns one
cohesive responsibility that splitting would scatter across files — a storage repository
grouping every query against one schema area, or a composition root whose function is one
documented linear lifecycle. `pipeline/daily_runner.py`'s `run_daily` carries
`# noqa: PLR0915 - the documented batch lifecycle is intentionally linear`; `regime/ftd.py`'s
`transition` carries `# noqa: PLR0911 - each explicit state branch is part of the audit
trail`. Both name the responsibility the length is protecting, not just suppress the
count.

Reject in review: a module or function that grew past the trigger by accreting unrelated
concerns (a storage module absorbing screening logic, a function doing three unrelated
things in sequence) rather than depth in one responsibility. Split there; the trigger did
its job.

## Parameters: ≤3, group with a dataclass

Prefer three or fewer parameters; group related ones into a dataclass or `TypedDict`.
`models.py`'s `DailyRunOptions` groups eight `copilot-daily` CLI options into one frozen
dataclass instead of an eight-parameter function signature. When a function genuinely
needs more — a storage write matching a wide schema row, or several independent
keyword-only injection seams — ruff's `PLR0913` is suppressed. `backtest/runner.py`'s
`run_backtest` is the shape to copy:
`# noqa: PLR0913 - the three keyword-only injection seams` names the reason inline. The
four other suppressions in `src/` (`storage/state_store.py`'s `insert_run`,
`text/news_finnhub.py`'s `__init__`, `pipeline/earnings.py`'s
`collect_earnings_calendar`, `backtest/exits.py`'s `evaluate_exit`) are bare
`# noqa: PLR0913` — do not treat those as the precedent; a new suppression states why on
the same line.

## Docstrings: Google-style, document *why*

`cli_support.py`'s module docstring is the model: it does not restate "converts a domain
error to `SystemExit`" (the signature already says that); it explains why the module
exists at all — eleven CLIs used to hand-write the same three lines, the exit code is a
contract `swing-daily` branches on, and it states what a caller must *not* do (a CLI with
no authenticated boundary should configure its own plain logging instead of reaching for
this module's `SecretRedactionFilter`, "it has nothing to redact"). Reject a docstring
that only restates the type signature ("`Args: x: the x value`") — that is redundant with
the annotation `Any` review already covers above.

## Security: `S` (bandit) rules

Ruff's bandit (`S`) rules must not be silenced without a written reason on the same
`noqa`, `<code> - <reason>` — and the same convention applies to every other suppressed
rule. Real examples from this repo: the many
`# noqa: S608 - placeholders are bound parameters, not interpolated values` across
`storage/market_store.py`, `storage/tracking_records.py`, `storage/verdict_records.py`
(an f-string building SQL text whose actual values are still `?`-bound, which `S608`
cannot distinguish from real interpolation by itself); `backtest/sensitivity.py`'s
`assert value is not None  # noqa: S101 - callers only pass non-gray cells`; and, outside
`S`, `io_atomic.py`'s `os.replace(...)  # noqa: PTH105 - atomic by design`. A `noqa`
with no reason, or a reason that just repeats the rule name, is not an accepted
justification — reject it in review.

Sanitize a path built from untrusted input before touching the filesystem:
`scripts/data_sync.py`'s `_local_path` rejects a remote manifest key with `..`
or an absolute component up front, then re-checks with
`root.local_dir.resolve() not in candidate.resolve().parents` before ever opening the
path — treat a manifest, an API response, or any other external key as untrusted for
path purposes the same way.

Domain/adapter code must never call `date.today()`/`datetime.now()` directly — an
injected `Clock` stands in instead. **REQUIRED:** `enforcing-point-in-time` owns that
invariant in full; this skill only points at it.
