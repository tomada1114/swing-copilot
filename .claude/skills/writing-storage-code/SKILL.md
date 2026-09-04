---
name: writing-storage-code
description: >
  Covers everything under `src/swing_copilot/storage/**` and the read-only
  `swing_copilot.research` accessors: `Database.transaction()`/`atomic()`
  atomicity, correction-vs-immutability upserts, the raw-bar 0.5%
  correction/quarantine gate (`market_store.py`), split/dividend
  adjust-on-read, snapshot-replacement deletion, `io_atomic` Parquet/report
  replacement, and `v_symbol_sector_asof`. Use when touching
  `storage/market_store.py`, `storage/state_store.py`, `storage/schema.py`,
  `storage/database.py`, `research/frames.py`, adding a DuckDB write path, or
  reviewing a diff that opens a DuckDB connection.
---

# Writing Storage Code

**Owns:** DuckDB transaction atomicity, correction-vs-immutability semantics,
replacement semantics (snapshots, Parquet, reports), adjust-on-read, and
connection discipline for `storage/**` and `research/**`. **Does not own:**
the `as_of` predicate itself — *what* counts as visible
(`enforcing-point-in-time`); syncing the DuckDB file and Parquet tree to and
from R2 (`operating-shared-data`); external fetching before it reaches a
store (`writing-external-adapters`).

## One logical write is one transaction

`storage/database.py`'s `atomic()` is the one place `BEGIN
TRANSACTION`/`COMMIT`/`ROLLBACK` are spelled out; `Database.transaction()`
wraps it and owns opening/closing the connection too. Every multi-row write
path in `storage/` goes through one of these instead of hand-written
try/except boilerplate — `state_store.py`, `verdict_records.py`,
`tracking_records.py`, and `market_store.py`'s corporate-actions/fundamentals
writers all call `database.transaction()`.

```python
def test_a_failure_after_an_earlier_statement_rolls_it_back_and_reraises(self, tmp_path):
    database = Database(tmp_path / "copilot.duckdb")
    with database.connect() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")

    def _write_then_fail() -> None:
        with database.transaction() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("simulated failure")

    with pytest.raises(RuntimeError, match="simulated failure"):
        _write_then_fail()

    with database.connect() as conn:
        rows = conn.execute("SELECT a FROM t").fetchall()
    assert rows == []  # the first successful INSERT did not survive
```

The required test injects the failure **after at least one statement already
succeeded** (`tests/storage/test_database.py::TestTransaction`) — a test that
only fails before the first statement proves nothing about rollback. DuckDB
has no nested transactions: never call `transaction()`/`atomic()` from inside
another one already open on the same connection.

## Correction, not `ON CONFLICT DO NOTHING`

Natural-key reruns must incorporate corrected input. `ON CONFLICT DO NOTHING`
silently keeps the first value forever, which is wrong wherever a rerun means
"the input changed and we want the new value" — a restated fundamentals
filing, a corrected split factor. The right shape is `ON CONFLICT (...) DO
UPDATE SET ... = EXCLUDED....`, as in `market_store.py`'s
`_UPSERT_CORPORATE_ACTION` and `_UPSERT_FUNDAMENTALS`. Reject a PR that
reaches for `DO NOTHING` on a natural key without an explicit, written reason
why *this* key is append-only.

## The deliberate exception: raw, immutable bars

Daily price bars are the one place `AGENTS.md` calls out as immutable by
design, and `market_store.write_bars` enforces it with two independent
fail-closed gates, both scoped per symbol (`_quarantine_reasons`):

1. **Correction tolerance.** A re-fetched row overlapping a stored
   `(symbol, date)` may differ by at most `_MAX_CORRECTION_RATIO` (0.5%,
   `market_store.py`) on `close`. Above that, the two rows are not the same
   fact revised — they are quoted on two different adjustment bases.
2. **Mixed-basis signature.** `data/adjustments.has_mixed_basis_signature`
   checks the incoming batch itself, before it ever touches stored rows: a
   provider response that silently mixes adjusted and unadjusted rows for
   one symbol (Issue #413) is a batch to reject on its own, even against an
   empty store.

Either gate quarantines *that symbol's whole batch*: nothing is written for
it, and its existing rows are untouched — reported back via
`BarWriteResult.quarantined`, never raised, so one bad symbol never costs the
run the other 499. This is fail-closed on purpose: a bar store silently
averaging two adjustment bases together produces a series that looks like
real data and is wrong in a way no downstream reader — screening, backtest,
or a human staring at a chart — can detect after the fact. Best-effort
("write it anyway, flag it") would make the corruption permanent and
invisible; quarantine keeps the store either right or visibly incomplete.

Test both edges of the gate directly:
`tests/storage/test_market_store.py::TestWriteAndReadBars::test_write_bars_correction_replaces_same_natural_key`
proves the accept side; the reject side needs a row past the ratio and a
batch carrying the mixed-basis signature, each quarantining independently of
the other.

## Splits and dividends adjust only on read

`write_bars` never rewrites a stored close for a split. `read_bars(symbols,
start, end, as_of)` applies every split with `ex_date <= as_of` at read time
(`data/adjustments.adjust_bars`), so what a caller gets back depends on the
requested point in time, never on when the bar happened to be fetched.
Dividends are recorded in `corporate_actions` (`kind = 'dividend'`) but never
applied to price — they exist as an event record, not a price adjustment.
Reject a change that starts mutating stored `close`/`open`/`high`/`low` for a
split anywhere outside `read_bars`'s adjustment path, and reject one that
starts subtracting a dividend from a stored or returned price at all.

## Snapshot replacement must delete, not just insert

`state_store.record_universe_membership` deletes the whole
`snapshot_date` slice before re-inserting it:

```python
with self._database.transaction() as conn:
    conn.execute(
        "DELETE FROM universe_membership WHERE snapshot_date = ?",
        [snapshot_date],
    )
    for member in members:
        conn.execute("INSERT INTO universe_membership (...) VALUES (...)", [...])
```

An insert-only replacement would leave a member absent from the new
membership list still marked present for that snapshot date — a delisted or
removed constituent that silently keeps showing up in every `as_of` read that
resolves to this snapshot. The DELETE-then-INSERT, in one transaction, is
what makes "absent from the replacement" mean "removed" rather than
"untouched." Any other full-replacement write path in `storage/` (signal
hits, truncated candidates — see `audit_records.py`) follows the same
DELETE-then-INSERT-in-one-transaction shape for the same reason.

## `io_atomic` for Parquet and rendered documents

Bar partitions and every rendered/serialized document (Parquet buffers, JSON
archives, Markdown reports) replace through `io_atomic.py`'s writers: a
temporary file in the **destination's own directory**, then `os.replace`.
`market_store._publish_partition` follows this even under concurrent writers
by staging into a UUID-suffixed temp name rather than `io_atomic`'s default
`.{name}.tmp`, so two racing `write_bars` calls targeting the same year
partition never collide on one temp file. On any `OSError` mid-write, the
previous destination is left byte-identical and the temporary artifact is
removed — `tests/storage/test_market_store.py`'s
`test_replace_failure_preserves_partition_and_cleans_unique_temp` is the
model regression test. Never write a destination file in place (`open(...,
"w")` directly on the final path); that has no atomicity and a crash
mid-write corrupts the previous, working version.

## Ad-hoc reads go through `research`, never a hand-rolled join

Notebook/CLI exploration of the DuckDB history uses `swing_copilot.research`
(`research/frames.py`): one `read_only=True` connection, opened, queried, and
closed within a single function call — `research.query`, `research.bars`,
`research.scorecard`, etc. Two things make this the only correct entry
point, not a convenience wrapper:

- **`v_symbol_sector_asof`** (`storage/schema.py`'s `ANALYSIS_VIEW_STATEMENTS`)
  is the single blessed as-of sector join (`snapshot_date <= run_date`,
  inclusive, latest match). Re-implementing this join by hand in a notebook
  risks getting the boundary or the "latest match" tie-break wrong in a way
  that quietly disagrees with every other view built on it.
- A read-only connection structurally cannot mutate operator-owned state —
  an `INSERT` or DDL statement fails loudly instead of corrupting it.

## Never hold a connection across think-time

Every `research/frames.py` accessor opens, queries, and closes within one
function call; write your own ad-hoc query the same way, never
`conn = Database(...).connect()` followed by exploration across several tool
calls. **REQUIRED:** `operating-shared-data` owns DuckDB's exclusive file lock
and what a stranded connection costs the shared pull/push cycle.

## Storage review checklist

- Every multi-row write path: is it inside one `database.transaction()`, and
  does a test inject failure after at least one statement succeeds?
- Every natural-key upsert: `DO UPDATE`, not `DO NOTHING`, unless the key is
  deliberately append-only (state that in the docstring)?
- Any code path that could rewrite a stored bar's OHLCV for a corporate
  action: does it exist only inside `read_bars`'s adjustment step, never in
  `write_bars`?
- Any full-replacement write (snapshot, signal hits, truncated tail): does it
  DELETE the old slice before inserting the new one, in the same transaction?
- Any new Parquet/report/JSON writer: does it go through `io_atomic`, with
  the previous file provably intact on a simulated write failure?
- Any new notebook/ad-hoc query: does it call `swing_copilot.research`
  (or `research.query` for the escape hatch) rather than opening
  `duckdb.connect()`/`Database(...)` directly, and does it close before any
  think-time?
