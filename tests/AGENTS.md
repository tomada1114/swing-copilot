Applies to: `*.py` under this directory.

Depth: `writing-tests` (how one test is named, what it asserts, and how it fakes
the world), `placing-tests` (where the file goes, which command runs it, and
which coverage floor applies). The rules below are the ones that hold whether or
not those skills are loaded.

## Structure

- Test files mirror the source tree: `tests/<package>/test_<module>.py`.
- Shared fixtures go in `tests/conftest.py`, at the narrowest scope that works.
- `test_<what>_<scenario>_<expected_result>`, Arrange-Act-Assert, one behavior
  per test.

## What to Test

- Test *behavior and contracts*, not implementation details. Never test a
  private helper directly; test through the public interface.
- Test the happy path AND the error path of every public function.
- **An expected value comes from outside the code under test** — a hand-worked
  number, a literal, the spec. Never recompute it the way the implementation
  computes it: such an assertion passes by construction and can never disagree
  with the implementation, even when the implementation is wrong.
- `pytest.raises(XError, match=r"...")` — assert the type and the message
  pattern. Test that errors propagate through the call chain and that cleanup
  runs on the failure path too.

## Edge Cases (always consider these)

The ordinary sweep — empty inputs, 0/1/-1/max/`inf`/`nan`, long and unicode
strings, single-element and duplicate collections, state after repetition and
after error recovery — is table stakes. These five are the ones specific to this
domain, and they are the ones reviews actually miss:

- **Point-in-time boundaries**: immediately before, exactly at, and immediately
  after `as_of`.
- **Batch atomicity**: inject failure after an earlier row succeeded, and assert
  full rollback and recovery.
- **Series joins**: mismatched dates, duplicates, insufficient overlap, constant
  values.
- **Exact accounting**: hand-calculate entry/exit costs and final liquidation
  equity.
- **Reused artifacts**: validate provenance and policy constraints on reused or
  loaded artifacts, not only on freshly produced ones.

## Isolation and Fakes

- `tmp_path` for filesystem tests — never a real directory. `monkeypatch` for
  environment variables — never `os.environ` mutation.
- Mock at boundaries only: I/O, network, clock, external services. Never mock
  the unit under test or an internal collaborator; prefer an in-memory fake to a
  mock for a repository or store.
- Assert on behavior and outputs — but a call count or its *absence* is the right
  assertion when the call itself is the contract: retry and rate ceilings,
  fail-soft isolation, budget skips, or proving no external call happened.

## Independence and Reliability

- No shared mutable state, no ordering dependency; each test passes when run
  alone. No dependence on timezone, locale, or wall-clock time.
- No `time.sleep`: use deterministic fakes for time.
- Nothing is left `@pytest.mark.skip` or as a TODO test on main, and a flaky
  test is fixed rather than retried.

## Coverage Philosophy

Coverage is a *floor*, not a *ceiling* — 95% line+branch minimum repo-wide, but
aim for meaningful coverage, not a percentage. Branch coverage matters more than
line coverage; test both sides of a conditional. Missing coverage should prompt
"is this code reachable?" — if not, delete the code rather than writing a test
for it.


## Offline and Cross-cutting Contracts

- The autouse socket blocker in `tests/conftest.py` stays: an ordinary test must
  fail fast on uninjected network access.
- The autouse `reports/` and `data/` guards stay alongside it — the suite must
  not write operator-owned data. `data/` is guarded twice on purpose: an mtime
  check catches writes, and a `duckdb.connect` interception catches the *open*,
  because `init_schema()` against an already-initialized file changes no mtime
  yet still takes DuckDB's exclusive file lock and can fail whatever the
  operator is doing with that file. A test that trips a guard is a bug in the
  test; fix the test's path, never the guard.
- Storage tests cover correction upsert, replacement deletion, Nth-write
  rollback, previous-file preservation, and rerun after failure.
- External adapter tests cover retryable and non-retryable failures, exact total
  attempts and backoff, timeout, and throttling on every attempt, using fake
  time.
- Analysis-boundary tests cover strict (`extra="forbid"`) schema rejection in
  both directions, non-empty source IDs proven to be a subset of the exported
  input, CON-03 checks over every user-visible free-text field, per-symbol
  fail-closed withholding without retry, and hard-fail boundaries (broken JSON,
  `as_of` mismatch, unknown symbol).
- Backtest tests use exact arithmetic for adverse slippage and commission on
  entry and every exit path, including forced liquidation.
