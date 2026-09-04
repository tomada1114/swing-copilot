---
name: writing-tests
description: >
  Covers how one pytest test in tests/**/test_*.py is written: naming
  (test_<what>_<scenario>_<expected_result>), asserting through the public
  interface, why an expected value must never be recomputed the way the
  implementation computes it, pytest.raises(XError, match=...), factory
  fixtures, fakes vs mocks, and the four autouse guards in tests/conftest.py
  (the socket blocker, the reports/ mtime guard, the data/ mtime guard, and
  the data/ duckdb.connect interceptor). Use when writing or reviewing a test,
  when a test trips
  "Real network access is forbidden", a test trips the repository's real
  data/ or reports/ guard, deciding what an assertion's expected value should
  come from, or writing the regression test a coverage gap is asking for.
---

# Writing Tests

**Owns:** how one pytest test is written — its name, what it asserts, how it
fakes the world, and the anti-patterns to reject in review. **Does not own:**
which file a test lives in and which coverage floor governs it
(`placing-tests`); the domain-specific coverage each layer owes — storage
transactions (`writing-storage-code`), external retry/timeout/rate limiting
(`writing-external-adapters`), the skill boundary (`guarding-analysis-boundary`),
backtest arithmetic (`writing-backtests`), risk math (`checking-risk-math`),
as-of boundaries (`enforcing-point-in-time`); or the error classes under
assertion (`designing-errors`).

## Naming and structure

`test_<what>_<scenario>_<expected_result>` — e.g.
`test_write_bars_correction_replaces_same_natural_key`,
`test_a_bad_row_in_a_later_year_leaves_the_earlier_year_unwritten`
(`tests/storage/test_market_store.py`). The name is the spec; a reviewer who
disagrees should be able to tell from the name alone, before reading the
body. Group related tests in a `class Test<Feature>:` the way
`tests/storage/test_market_store.py` does (`TestReadBarsEmptyState`,
`TestWriteAndReadBars`) — it documents the behavior surface without a
comment. Arrange-Act-Assert; one behavior per test, branching belongs in a
separate test or a `parametrize` row, never an `if` in the test body.

## Test through the public interface

Call the module's real entry points — `MarketStore.write_bars` /
`.read_bars`, `diff_gate.select`, `calc_position_size` — never a
single-underscore helper. Wanting to reach past the public interface into
`_classify` or `_estimate` is a signal the function is doing something the
interface doesn't expose, not a reason to add `# noqa: SLF001` and move on.
The one deliberate exception is `tests/conftest.py`'s
`plant_non_finite_bars`, which calls `MarketStore._write_partition` directly
(`# noqa: SLF001`, with a docstring explaining why): it reproduces a
historical on-disk state — bars written before `write_bars`' finite-value
guard existed — that no *validated* writer can produce today. That is the
bar for reaching past the public surface: recreating a state the interface
now forbids, not convenience.

## Expected values come from outside the code

This is the rule most worth enforcing in review, because it fails silently:
a test that recomputes its expected value the way the implementation
computes it can never disagree with a wrong implementation.

```python
# src/swing_copilot/risk/position_sizing.py
def calc_position_size(
    account_equity, entry_price, stop_price, max_position_pct, max_trade_risk_pct
) -> PositionSizeResult: ...
```

```python
# WRONG — restates the implementation's own arithmetic
def test_calc_position_size_shares_by_risk() -> None:
    result = calc_position_size(100_000, 50.0, 45.0, 0.25, 0.01)
    risk_budget = 100_000 * 0.01
    risk_per_share = 50.0 - 45.0
    assert result.shares_by_risk == risk_budget // risk_per_share
```

This passes whether `calc_position_size` uses `Fraction` for exact floor
division (the whole reason that module exists, per its docstring) or plain
float division with its rounding risk — test and code share one formula, so
a regression in the formula shows up in both places at once.

```python
# RIGHT — a hand-worked literal, independent of how the code gets there
def test_calc_position_size_shares_by_risk() -> None:
    result = calc_position_size(100_000, 50.0, 45.0, 0.25, 0.01)
    assert result.shares_by_risk == 200  # 100_000 * 0.01 = $1000 / $5 per share
```

Take the expected value from a hand-worked number, a literal, or the spec —
never from calling the same computation in slightly different syntax.
`test_write_bars_correction_replaces_same_natural_key` does this correctly:
it asserts `stored.iloc[0]["close"] == pytest.approx(10.23)`, the literal
written into the input, not a value re-derived from the merge logic.

## Errors

`pytest.raises(XError, match=...)`, always checking the pattern, not just
the class — `tests/storage/test_market_store.py` asserts
`pytest.raises(NonFiniteBarsError, match="MSFT 2026-07-16")` to prove the
message names the actual offending row, not just any row.
`test_replace_failure_preserves_partition_and_cleans_unique_temp` covers
both propagation and the failure path in one test: it patches
`io_atomic.os.replace` to fail mid-write, then asserts
`pytest.raises(OSError, match="replace failed")` *and* that the previous
partition's bytes are untouched.

## Parametrize

`@pytest.mark.parametrize` with `pytest.param(..., id=...)` for input/output
variations — `tests/storage/test_market_store.py` stacks
`pytest.param(float("nan"), id="nan")` / `id="inf"` / `id="-inf"` against a
second `@pytest.mark.parametrize("column", ["open", "high", ...])` to cover
the full matrix without copy-pasting near-identical tests. An un-`id`'d
param list reports as `test_x[0]`, `test_x[1]` in a failure — useless when
triaging a red run days later.

## Fixtures

Prefer a factory over a static fixture. `tests/support/runs.py`'s
`seed_run(state_store, run_id, run_date, **overrides)` wraps
`StateStore.insert_run()` with sane defaults — it replaced eleven test
modules that each hand-wrote `INSERT INTO runs (...)` against
`state_store._database` (`# noqa: SLF001` at every call site). A factory is
customizable per test; a static fixture invites either a proliferation of
near-identical fixtures or tests that mutate a shared object.

Scope a fixture to where it's used (`placing-tests` has the full tier
breakdown): `market_store` is defined inside
`tests/storage/test_market_store.py` because only that file needs it, not
promoted to a conftest just because it could be reused.

`tmp_path` for anything filesystem-shaped — never a real directory.
`monkeypatch.setenv`/`.delenv`, never direct `os.environ` mutation —
`tests/conftest.py`'s `_block_real_secrets` fixture is the model, closing
both the shell-env and `.env` sources rather than mutating `os.environ`.

## The four autouse guards in tests/conftest.py

Every test in the suite runs under four autouse guards (plus
`_block_real_secrets`, above). Tripping one is a bug in the test, not the
guard — the fix is always an isolated `tmp_path` (or
`monkeypatch.chdir(tmp_path)` before a composition root that resolves a
repo-relative default), never weakening or working around the guard.

1. **`_block_real_network`** patches `socket.socket.connect` to raise
   `AssertionError("Real network access is forbidden in the test suite")`.
   Inject a fake at the client boundary instead of letting a test reach the
   real network.
2. **`_block_repo_report_writes`** and **`_block_repo_data_writes`**
   fingerprint `reports/` and `data/` (including `copilot.duckdb`,
   `copilot_dry_run.duckdb`, and `bars/`) by mtime before and after each
   test, failing if anything moved — catching a default `output_dir` or
   `DEFAULT_DB_PATH` that silently resolved to the operator's real,
   repo-relative data instead of `tmp_path`.
3. **`_block_repo_data_connections`** intercepts `duckdb.connect` itself,
   because the mtime guard above structurally cannot see this failure mode:
   `init_schema()` against an *already-initialized* database writes nothing,
   yet the connection still takes DuckDB's exclusive file lock, which can
   fail whatever the operator is doing with that file (a `just
   data-pull`/`data-push`, or a live `copilot-daily` run) even though the
   test wrote no bytes.

## Fakes over mocks

Mock only at true boundaries: I/O, network, clock, external services.
`tests/support/fakes.py`'s `FixedClock`, `StubDataProvider`,
`StubNewsClient`, `StubCalendarClient`, and `StubEdgarClient` are in-memory
fakes shared across the suite, each pinned against the real `Protocol` it
stands in for by `tests/support/test_fakes.py` — added after a copy-pasted
`FakeDataProvider` silently dropped a constructor argument and left a
`failures`-dependent behavior untested in one module while its siblings
still covered it. Never mock the unit under test or an internal
collaborator; a mock there breaks on refactor while behavior is unchanged.

Assert on behavior and outputs, with one carve-out: a call count or absence
*is* the contract for a retry ceiling, a rate limit, a fail-soft skip, or
proving no network call happened — `tests/data/test_edgar.py`'s
`test_stops_after_three_attempts` and `test_does_not_retry_validation_error`
assert exactly this. `StubDataProvider.requested_symbols` records call scope
for the same reason: a fail-soft-isolation test proves one failing symbol
didn't widen the fetch.

## Determinism and independence

No `time.sleep` in a test. `tests/conftest.py`'s `ThrottleTimeline` is the
shared model for time-dependent external-adapter tests: a monotonic clock
that only `sleep()` and a request's own round-trip latency advance, with
`issue_gaps`/`gaps_below()` asserting the real issue interval rather than
just the sleep arguments passed — shared by four client test modules
(`data/edgar.py`, `text/news_finnhub.py`, `data/earnings_finnhub.py`,
`text/calendar_fred.py`) instead of each carrying a drifted copy. No
`@pytest.mark.skip` or TODO test on `main`; a flaky test is fixed
immediately, not retried — intermittent failure is a real race or a
non-deterministic fake, not CI infrastructure.

## Coverage is a floor, not proof

When a line resists coverage, the right question is "is this code reachable?",
not "how do I hit this line" — unreachable code gets deleted, not exercised by
a contrived test. `placing-tests` owns the floors themselves.

## Anti-patterns to reject in review

- A trivial property/getter test while a real edge case (an empty batch, a
  boundary `as_of`, a duplicate natural key) goes untested.
- `assert result is not None` where the actual value is checkable.
- Testing that a dependency works, not how `swing_copilot` uses it.
- Mocking so much the code under test never runs — e.g. mocking
  `MarketStore` wholesale instead of a `tmp_path`-backed real `Database`.
  Needing to mock more than two collaborators for one test is a design
  finding, not a reason to add a third mock.
