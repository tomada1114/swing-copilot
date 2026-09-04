---
name: wiring-the-pipeline
description: >
  Covers src/swing_copilot/config.py's strict pre-I/O settings/strategy
  parsing and src/swing_copilot/pipeline/**'s composition root: daily.py's
  eight steps, daily_runner.py's fatal-vs-fail-soft sequencing, and
  daily_composition.py's CLI parsing plus real-adapter wiring
  (DailyDependencies). Use when changing config.py, pipeline/daily.py,
  pipeline/daily_runner.py, pipeline/daily_composition.py, or
  pipeline/backfill.py, deciding whether a new step aborts or degrades a
  run, or adding a Protocol at a pipeline boundary.
---

# Wiring the Pipeline

**Owns:** `src/swing_copilot/config.py` and `src/swing_copilot/pipeline/**` —
strict configuration parsing, the composition root, the fatal-versus-fail-soft
boundary, and rerun safety. **Does not own:** individual adapters
(`writing-external-adapters`), storage upsert/transaction semantics
(`writing-storage-code`), diagnosing a *scheduled* run's outcome after the
fact (`diagnosing-daily-runs`).

## Config parses into strict typed values before any I/O

`config.load_settings`/`load_strategies` read and `yaml.safe_load` the file,
then hand the raw dict straight to `Settings.model_validate`/
`StrategiesConfig.model_validate` — every field crosses the `StrictModel`
boundary (`extra="forbid"`) before a single network call, database
connection, or price fetch happens. A bad config therefore fails in
milliseconds with a `ConfigError` naming the file and the validation error,
not partway through a run that already paid for a data pull.

What gets rejected at this boundary, verified in `config.py`: an unknown key
anywhere in `settings.yaml`/`strategies.yaml` (via `StrictModel`); a
`StrategySpec.candidate_limit` outside `(0, 10]`
(`Field(gt=0, le=10)`); a strategy whose `ranking.score_weights` do not sum
to `1.0` (`StrategiesConfig._require_score_weights_sum_to_one`); and a
strategy that weights a component (`pivot_proximity`, `rs_percentile`,
`criteria_met`) without configuring the signal that component depends on
(`_require_signal_for_weighted_component` — see `checking-risk-math` for why
this matters for ranking determinism). All three are `ValueError`s raised
from `load_strategies`, never discovered later inside `ScreeningPipeline`.

## `pipeline/` is the composition root — three modules, one split

The daily batch is deliberately split three ways, and each half of the split
is the tell for where new code belongs:

- **`pipeline/daily.py`** implements the eight steps as functions that take
  an already-built `DailyDependencies` and explicit `as_of`/`run_id`
  arguments — no argparse, no real HTTP client construction, nothing reads
  `sys.argv` or the environment here. This is the functional core: given the
  same `DailyDependencies` (which can be built from fakes in a test) and the
  same `as_of`, a step's behavior is reproducible.
- **`pipeline/daily_runner.py`** ("Run lifecycle for the daily batch... owns
  the imperative sequencing and terminal-state decisions") calls those step
  functions in order and decides the run's terminal `RunStatus`.
- **`pipeline/daily_composition.py`** ("CLI parsing and real-adapter
  composition for `copilot-daily`") is the only place that parses `argv`,
  calls `load_settings`/`load_secrets`, and constructs the real adapters
  (`YFinanceProvider`, `EdgarClient`, `FinnhubEarningsClient`, `SystemClock`)
  into a `DailyDependencies`, via `_compose_dependencies`.

If a change to a step needs `os.environ`, `sys.argv`, or a concrete adapter
class, that is the signal it belongs in `daily_composition.py`, not
`daily.py` — mixing wiring into the step functions is exactly what breaks
their testability against fakes.

## Fatal versus fail-soft is decided once, in the pipeline

`pipeline/daily.py`'s module docstring states the rule directly: **steps 1-4
(price update, fundamentals, screening, risk check) are fatal** — "screening
cannot meaningfully proceed without them" — any of their failures aborts the
run (`runs.status=failed`, nonzero exit code) without touching steps 5-8.
**Steps 5-6 (text collection, analysis-input export) are fail-soft**: their
failure sets `runs.status=degraded` but never aborts the run, and the local
output step always attempts to produce a screening-only brief regardless.
Notification is optional on top of that.

This decision lives in the orchestrator, not per adapter: `daily_runner.py`
accumulates a single `degraded` boolean across every fail-soft step
(`degraded = deps.universe_warning is not None`, then `degraded = degraded
or not text_outcome.success`, then folded again after the analysis export,
postmortem, and retro steps) and sets `RunStatus.DEGRADED` only if any of
them fired — an individual adapter never decides for itself whether its own
failure should abort the whole batch. A degraded run must say so in its
output rather than quietly producing a thinner report: that is what
`runs.status=degraded` and the per-step `detail` messages
(`_record_step`) are for. Adding a ninth step means deciding, once, up
front, which bucket it falls in — not copying whichever `try/except` shape
happened to be nearby.

## Rerun safety

A same-day rerun is guarded explicitly, not left to storage upserts alone:
`daily_composition.py`'s `_parse_args` reads `--allow-same-day-rerun`, and
without it a resolved `run_date` that already has a successful run raises
`PreflightAbort(reason="same_day_rerun")` before any step runs (see
`diagnosing-daily-runs` for the exit-code/stderr-tag contract this produces
in CI). Below that gate, correction-on-rerun is a storage-layer property —
**REQUIRED:** `writing-storage-code` for the natural-key upsert rule
(`ON CONFLICT DO NOTHING` is wrong wherever a rerun must incorporate
corrected input). The pipeline's job is only to decide *whether* a rerun for
this `as_of` should proceed at all, not how each table absorbs it.

## Protocols only at volatile or failure-prone boundaries

`pipeline/daily.py` declares `_EdgarClientLike`, `_NewsClientLike`, and
`_CalendarClientLike` as `Protocol`s — narrow structural types for the
external, failure-prone clients a step calls, so a test can inject a fake
without importing the real HTTP-backed class. Nothing inside the pipeline
defines a `Protocol` for a purely internal collaborator (a step function, a
dataclass like `DailyDependencies` or `_RunContext`) — those are called
directly, because there is nothing volatile about them to abstract over. A
new `Protocol` in this package is a signal to check: does this actually
cross an I/O/clock/network boundary, or is it wrapping an internal function
for no reason other than habit?

## Composition-root review checklist

- Does a new step read `sys.argv`, `os.environ`, or construct a concrete
  adapter class outside `daily_composition.py`? Move the wiring, keep the
  step a plain function of `DailyDependencies`.
- Is the new step's fatal-or-fail-soft classification made once in
  `daily_runner.py`'s sequencing, and does a fail-soft failure feed the one
  `degraded` accumulation rather than swallowing itself silently?
- Does the config field the step depends on get validated in `config.py`
  before this step could ever run, or does an invalid value only surface
  once the step executes?
- If a same-day rerun would touch this step's output, does the step rely on
  the storage layer's correction upsert rather than reimplementing its own
  idempotency check?
