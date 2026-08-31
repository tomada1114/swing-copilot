"""`verdicts` / `verdict_sources` / `verdict_outcomes` repository (P8-30).

The retrospective mechanism's three tables, written only by `retro/`
(`docs/goal-prompts/swing-copilot-retrospective/design.md` §4). Both writers
are *full replacements* rather than plain upserts:

* `replace_run_verdicts` replaces one run's entire verdict set, so
  re-ingesting a corrected `analysis_result.json` both updates changed rows
  and drops symbols that are no longer part of the answer.
* `replace_verdict_outcomes` replaces one `(run_id, horizon_days)` slice,
  mirroring `audit_records.replace_signal_outcomes`, so re-evaluating after a
  price correction reclassifies instead of duplicating.

Each replacement is one transaction: a failure after an earlier statement
succeeded rolls the whole write back and leaves the previous state intact.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

from swing_copilot.storage.json_guard import dumps_safe

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import duckdb

    from swing_copilot.storage.database import Database

#: Earliest verdict `as_of` guaranteed free of reader-account-dependent share
#: counts. Issue #348 dropped account-dependent sizing from the production
#: risk path; Issue #352 (merged 2026-08-21) then removed the last
#: account-dependent fields from `risk_constraints` in `analysis_input.json`,
#: so a verdict dated on or after this day was written from an export that
#: never carried "final shares" language in the first place. A verdict dated
#: before it may still describe a reader's account (share counts, "口座規模")
#: verbatim in `reasons_json` / `verdict_reasons.text` -- those rows are kept
#: forever as the historical record and are never rewritten (Issue #385), so
#: both re-injection into `<prior_verdicts>` (`get_prior_verdicts` below) and
#: the dashboard's per-symbol reason display gate on this same constant.
ACCOUNT_INDEPENDENT_VERDICT_CUTOFF: Final = date(2026, 8, 21)

#: The instant Issue #352 merged (PR #352, merge commit `78dd2f1`; `gh pr
#: view 352 --json mergedAt` -> `2026-08-21T19:14:55Z`), removing the last
#: account-dependent field from `risk_constraints` in `analysis_input.json`.
#: `runs.started_at` is `now()` at the moment a run actually *executed*
#: (`storage/state_store.py::start_run` inserts it as DuckDB's `now()`) --
#: wall time, never touched by `--as-of` (AGENTS.md: "wall time is metadata,
#: never a substitute for `as_of`"). So a run that *started* on or
#: after this instant necessarily ran account-independent code and produced
#: an account-independent export, no matter how far in the past `--as-of`
#: told it to replay (Issue #389: `ACCOUNT_INDEPENDENT_VERDICT_CUTOFF` alone
#: looks at `run_date`, which `--as-of` sets to the replayed date, not the
#: date the run actually happened -- so a replay of an old date was
#: permanently withholding an already account-independent reason).
#: `reason_text_visible_sql`/`is_reason_text_visible` below combine the two
#: cutoffs as a pure relaxation of `ACCOUNT_INDEPENDENT_VERDICT_CUTOFF` alone.
ACCOUNT_INDEPENDENT_EXPORT_SINCE: Final = datetime(2026, 8, 21, 19, 14, 55, tzinfo=UTC)


def reason_text_visible_sql(
    started_at_column: str = "started_at", run_date_column: str = "run_date"
) -> str:
    """The shared SQL predicate for whether a verdict's reason text may be shown (Issue #389).

    A pure relaxation of the plain `run_date_column >= ACCOUNT_INDEPENDENT_VERDICT_CUTOFF`
    rule Issue #385 shipped: nothing visible under that rule becomes hidden
    here. A reason is visible when the run that wrote it either *started* at
    or after `ACCOUNT_INDEPENDENT_EXPORT_SINCE` (git-verifiable proof its
    export was already account-independent, however early `--as-of` dated
    the run itself), or is *dated* at or after
    `ACCOUNT_INDEPENDENT_VERDICT_CUTOFF`. Both `dashboard/queries.py`'s
    `reasons_for_symbol` and `get_prior_verdicts` below build their SQL from
    this one function so the two never drift apart -- what one shows, the
    other may re-inject.

    `started_at_column` reading `NULL` (e.g. a `LEFT JOIN runs` that found no
    row) makes the first term `NULL`, which is never true, so the expression
    degrades to the second term alone -- the pre-#389 rule, unchanged for a
    verdict whose owning run cannot be resolved.

    Args:
        started_at_column: SQL expression for the candidate `runs.started_at`
            (qualify it, e.g. `"r.started_at"`, in a multi-table query).
        run_date_column: SQL expression for the run's date -- `runs.run_date`,
            or `verdicts.as_of` where the two are equal by construction (see
            `storage/schema.py`'s `verdicts.as_of` comment).

    Returns:
        A boolean SQL expression with exactly two `?` placeholders. Bind
        `(ACCOUNT_INDEPENDENT_EXPORT_SINCE, ACCOUNT_INDEPENDENT_VERDICT_CUTOFF)`
        to them, in that order.
    """
    return f"({started_at_column} >= ? OR {run_date_column} >= ?)"


def is_reason_text_visible(
    *, started_at: datetime | None, run_date: date | None
) -> bool:
    """Pure-Python mirror of `reason_text_visible_sql`'s bound predicate (Issue #389).

    For a caller holding already-fetched values (the symbol page's
    `RunRef`) rather than building SQL. Must always agree with
    `reason_text_visible_sql` for the same `(started_at, run_date)` pair --
    see `tests/storage/test_verdict_records.py` for the equivalence check.

    Args:
        started_at: The candidate `runs.started_at`, or `None` when the run's
            own `runs` row is unresolved.
        run_date: The run's date (or the verdict's own `as_of`, equal to it
            by construction), or `None` when unknown.

    Returns:
        Whether a reason written by this run's export may be shown.
    """
    if started_at is not None and started_at >= ACCOUNT_INDEPENDENT_EXPORT_SINCE:
        return True
    return run_date is not None and run_date >= ACCOUNT_INDEPENDENT_VERDICT_CUTOFF


_INSERT_VERDICT = """
    INSERT INTO verdicts (
        run_id, symbol, as_of, strategy_key, recommendation, reasons_json, no_trade,
        news_supply_collected_items, news_supply_exported_items,
        news_supply_symbol_mention_items, news_supply_level
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_VERDICT_SOURCE = """
    INSERT INTO verdict_sources (run_id, symbol, source_id, source_type)
    VALUES (?, ?, ?, ?)
"""

# Issue #192: `reasons_json` normalized into rows. Written in the same
# transaction as the verdict it belongs to, so the projection can never
# describe a verdict the run did not commit.
_INSERT_VERDICT_REASON = """
    INSERT INTO verdict_reasons (
        run_id, symbol, reason_index, text, basis, source_id_count
    ) VALUES (?, ?, ?, ?, ?, ?)
"""

_INSERT_VERDICT_REASON_SOURCE = """
    INSERT INTO verdict_reason_sources (run_id, symbol, reason_index, source_id)
    VALUES (?, ?, ?, ?)
"""

_INSERT_ANALYSIS_COVERAGE = """
    INSERT INTO analysis_source_coverage (
        run_id, symbol, source_id, original_chars, exported_chars,
        is_truncated, selection_mode, exhibit_truncated, sections_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_VERDICT_COLLECTION = """
    INSERT INTO verdict_collections (run_id, document_digest) VALUES (?, ?)
"""

_INSERT_VERDICT_OUTCOME = """
    INSERT INTO verdict_outcomes (
        run_id, symbol, horizon_days, as_of, recommendation,
        forward_return_pct, benchmark_return_pct, classification
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


@dataclass(frozen=True, slots=True)
class VerdictReasonRecord:
    """One reason behind a verdict, as persisted inside `reasons_json`.

    `source_ids` may be empty: a reason resting only on deterministic inputs
    the pipeline itself computed has no news/filing source to cite (mirrors
    `analysis.schemas.VerdictReason`).

    `basis` is that schema's closed evidence-kind vocabulary, or `None` for a
    reason written before Issue #191 introduced the tag (or left untagged).
    It lives inside `reasons_json` rather than in a column of its own because
    a reason is already only addressable through that document; no DDL
    changes for it.
    """

    text: str
    source_ids: tuple[str, ...]
    basis: str | None = None


@dataclass(frozen=True, slots=True)
class NewsSupplyRecord:
    """How much company-specific news the verdict was made under (Issue #130).

    Archived alongside the verdict rather than derived later: the exported
    feed is not kept, so the only place this can be observed is the
    `analysis_input.json` that produced the judgement. All three counts are
    stored, not just `level`, so a retrospective can re-grade the window at a
    different threshold without re-scanning `reports/`.
    """

    collected_items: int
    exported_items: int
    symbol_mention_items: int
    level: str


@dataclass(frozen=True, slots=True)
class VerdictRecord:
    """One symbol's qualitative verdict from one past run."""

    run_id: UUID
    symbol: str
    as_of: date
    strategy_key: str
    recommendation: str
    reasons: tuple[VerdictReasonRecord, ...]
    no_trade: bool
    #: Code-owned, resolved from `analysis_input.json` like `strategy_key`.
    #: `None` means the archive predates Issue #130's measurement: not
    #: recorded, which readers must not conflate with a measured `none`.
    news_supply: NewsSupplyRecord | None = None


@dataclass(frozen=True, slots=True)
class VerdictSourceRecord:
    """One `source_id` the analysis of `symbol` cited in that run.

    `source_type` is resolved from the code-owned `analysis_input.json`, never
    echoed back from the skill's answer (design §4).
    """

    run_id: UUID
    symbol: str
    source_id: str
    source_type: str


@dataclass(frozen=True, slots=True)
class AnalysisSourceCoverageRecord:
    """One filing source's code-owned export completeness for a past run."""

    run_id: UUID
    symbol: str
    source_id: str
    original_chars: int
    exported_chars: int
    is_truncated: bool
    selection_mode: str
    sections: tuple[tuple[str, str], ...]
    #: Whether the filing's 8-K exhibits were cut off at collection, which the
    #: character counts above cannot express (Issue #157). `None` means the row
    #: predates the column: not recorded, which is not the same as `False`.
    exhibit_truncated: bool | None = None


@dataclass(frozen=True, slots=True)
class VerdictOutcomeRecord:
    """One matured `(run, symbol, horizon)` verdict classification."""

    run_id: UUID
    symbol: str
    horizon_days: int
    as_of: date
    recommendation: str
    forward_return_pct: float
    classification: str
    #: The benchmark's return over the identical span (Issue #190), so
    #: separation can be restated in excess terms instead of being read off a
    #: number the market's own move is baked into. `None` means "not
    #: measured" -- a row classified before the column existed, or one whose
    #: benchmark bars were missing -- and must never be read as a flat market.
    benchmark_return_pct: float | None = None


@dataclass(frozen=True, slots=True)
class PriorVerdictOutcome:
    """How one horizon of an earlier verdict on the same symbol turned out."""

    horizon_days: int
    classification: str
    forward_return_pct: float


@dataclass(frozen=True, slots=True)
class PriorVerdictRecord:
    """One earlier verdict on a symbol, with whatever has since matured.

    Fed back into the next `analysis_input.json` for the same symbol (Issue
    #191) so the analysis can see which of its own reasons keep preceding a
    miss. `outcomes` is empty while the horizons are still open, which is the
    normal state for a verdict only a few sessions old -- not an error, and
    not a neutral result.
    """

    run_id: UUID
    as_of: date
    symbol: str
    strategy_key: str
    recommendation: str
    reasons: tuple[VerdictReasonRecord, ...]
    outcomes: tuple[PriorVerdictOutcome, ...]


@dataclass(frozen=True, slots=True)
class VerdictReasonBasisRow:
    """One `(run, symbol, basis)` the window's matured verdicts rested on.

    Deduplicated per verdict: a symbol whose analysis wrote three separate
    `filing_fundamental` reasons counts that basis once, so the tally measures
    "how often this kind of evidence decided a verdict", not how verbose the
    writer was about it. `basis` is `None` for an untagged reason.
    """

    run_id: UUID
    symbol: str
    basis: str | None


@dataclass(frozen=True, slots=True)
class VerdictRow:
    """The `verdicts` columns `retro/evaluate.py` needs to classify a run."""

    run_id: UUID
    symbol: str
    as_of: date
    recommendation: str
    #: The news supply the verdict was made under, or `None` when the row was
    #: collected from an archive written before Issue #130 measured it.
    news_supply: NewsSupplyRecord | None = None


@dataclass(frozen=True, slots=True)
class VerdictCitationRow:
    """One source a matured verdict's analysis cited (P8-31, design §5.3 item 4).

    `source_url` comes from the `text_items` join and is `None` when the cited
    item is no longer (or was never) recorded there. That is data quality to
    report, not a reason to drop the citation: the contribution table still
    needs to know the source was used.
    """

    run_id: UUID
    symbol: str
    source_id: str
    source_type: str
    source_url: str | None


def _news_supply_columns(
    supply: NewsSupplyRecord | None,
) -> tuple[int | None, int | None, int | None, str | None]:
    """Flatten the optional supply block into its four nullable columns.

    An absent measurement writes four `NULL`s rather than zeros: the column
    has to keep saying "not recorded", which is not the `none` level.
    """
    if supply is None:
        return (None, None, None, None)
    return (
        supply.collected_items,
        supply.exported_items,
        supply.symbol_mention_items,
        supply.level,
    )


def _news_supply_from_row(
    collected_items: int | None,
    exported_items: int | None,
    symbol_mention_items: int | None,
    level: str | None,
) -> NewsSupplyRecord | None:
    """Rebuild the supply block, or `None` when the row never recorded one.

    `level` is the discriminator: `_news_supply_columns` writes all four
    together or none of them, so a row with a level has the counts too.
    """
    if (
        level is None
        or collected_items is None
        or exported_items is None
        or symbol_mention_items is None
    ):
        return None
    return NewsSupplyRecord(
        collected_items=collected_items,
        exported_items=exported_items,
        symbol_mention_items=symbol_mention_items,
        level=level,
    )


@dataclass(frozen=True, slots=True)
class CollectedRunRecords:
    """Everything one `retro collect` write replaces for a single run.

    Grouped into one value rather than five parameters because they are one
    replacement unit: the rows and the fingerprint of the documents they came
    from are committed together or not at all (Issue #209).
    """

    run_id: UUID
    verdicts: Sequence[VerdictRecord] = ()
    sources: Sequence[VerdictSourceRecord] = ()
    coverages: Sequence[AnalysisSourceCoverageRecord] = ()
    document_digest: str | None = None


def replace_run_verdicts(
    database: Database,
    run_id: UUID,
    verdicts: Sequence[VerdictRecord],
    sources: Sequence[VerdictSourceRecord],
    coverages: Sequence[AnalysisSourceCoverageRecord] = (),
) -> None:
    """Atomically replace one run's verdict and citation set, unfingerprinted.

    The digest-free entry point: it clears any previous `verdict_collections`
    fingerprint, so rows written from documents this call cannot name never
    let a later scan skip the run (Issue #209).

    Args:
        database: Shared DuckDB connection owner.
        run_id: The run whose rows are being replaced wholesale.
        verdicts: The run's verdicts. Empty clears the run (a re-ingest of a
            result that no longer analyzes any symbol).
        sources: The `source_id`s those verdicts' analyses cited.
        coverages: Every filing source offered to the analysis, cited or not.
    """
    replace_collected_run(
        database,
        CollectedRunRecords(
            run_id=run_id,
            verdicts=verdicts,
            sources=sources,
            coverages=coverages,
        ),
    )


def replace_collected_run(database: Database, records: CollectedRunRecords) -> None:
    """Atomically replace one run's complete verdict, citation, and digest set.

    Args:
        database: Shared DuckDB connection owner.
        records: The run's replacement rows plus, optionally, the fingerprint
            of the two documents they were built from. The fingerprint is
            written in the same transaction so a later scan can prove the
            archive unchanged; `None` *removes* any previous fingerprint
            rather than leaving it behind.

    Raises:
        ValueError: A record belongs to a different run than `records.run_id`.
    """
    run_id = records.run_id
    verdicts, sources = records.verdicts, records.sources
    coverages = records.coverages
    _reject_foreign_run(run_id, (record.run_id for record in verdicts))
    _reject_foreign_run(run_id, (record.run_id for record in sources))
    _reject_foreign_run(run_id, (record.run_id for record in coverages))

    with database.connect() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute("DELETE FROM verdicts WHERE run_id = ?", [str(run_id)])
            conn.execute("DELETE FROM verdict_sources WHERE run_id = ?", [str(run_id)])
            # Issue #192: the normalized projection is replaced with the
            # document it projects. Deleting it here rather than per symbol is
            # what keeps a re-ingest that dropped a symbol from leaving that
            # symbol's reasons behind as orphans.
            conn.execute("DELETE FROM verdict_reasons WHERE run_id = ?", [str(run_id)])
            conn.execute(
                "DELETE FROM verdict_reason_sources WHERE run_id = ?", [str(run_id)]
            )
            conn.execute(
                "DELETE FROM analysis_source_coverage WHERE run_id = ?",
                [str(run_id)],
            )
            conn.execute(
                "DELETE FROM verdict_collections WHERE run_id = ?", [str(run_id)]
            )
            if records.document_digest is not None:
                conn.execute(
                    _INSERT_VERDICT_COLLECTION,
                    [str(run_id), records.document_digest],
                )
            for verdict in verdicts:
                conn.execute(
                    _INSERT_VERDICT,
                    [
                        str(verdict.run_id),
                        verdict.symbol,
                        verdict.as_of,
                        verdict.strategy_key,
                        verdict.recommendation,
                        dumps_safe(
                            [
                                {
                                    "text": reason.text,
                                    "source_ids": list(reason.source_ids),
                                    "basis": reason.basis,
                                }
                                for reason in verdict.reasons
                            ]
                        ),
                        verdict.no_trade,
                        *_news_supply_columns(verdict.news_supply),
                    ],
                )
                _insert_reason_rows(
                    conn, str(verdict.run_id), verdict.symbol, verdict.reasons
                )
            for source in sources:
                conn.execute(
                    _INSERT_VERDICT_SOURCE,
                    [
                        str(source.run_id),
                        source.symbol,
                        source.source_id,
                        source.source_type,
                    ],
                )
            for coverage in coverages:
                conn.execute(
                    _INSERT_ANALYSIS_COVERAGE,
                    [
                        str(coverage.run_id),
                        coverage.symbol,
                        coverage.source_id,
                        coverage.original_chars,
                        coverage.exported_chars,
                        coverage.is_truncated,
                        coverage.selection_mode,
                        coverage.exhibit_truncated,
                        dumps_safe(
                            [
                                {"name": name, "status": status}
                                for name, status in coverage.sections
                            ]
                        ),
                    ],
                )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def _insert_reason_rows(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    symbol: str,
    reasons: Sequence[VerdictReasonRecord],
) -> None:
    """Write one verdict's normalized reason rows inside the caller's transaction.

    `reason_index` is the reason's position in `reasons_json`, so a row here
    can always be traced back to the exact element of the document it
    projects (Issue #192).
    """
    for index, reason in enumerate(reasons):
        conn.execute(
            _INSERT_VERDICT_REASON,
            [run_id, symbol, index, reason.text, reason.basis, len(reason.source_ids)],
        )
        for source_id in dict.fromkeys(reason.source_ids):
            conn.execute(
                _INSERT_VERDICT_REASON_SOURCE, [run_id, symbol, index, source_id]
            )


def backfill_verdict_reasons(conn: duckdb.DuckDBPyConnection) -> int:
    """Normalize `reasons_json` for verdicts that have no reason rows yet.

    Issue #192's migration for a database with accumulated history. The rows
    are a derived projection of `reasons_json`, which every existing verdict
    already carries, so this restates a recorded fact rather than inventing
    one — the same reasoning as `schema.py`'s column backfills, done in
    Python because it re-uses `_reasons_from_json` instead of duplicating
    that parsing as nested JSON SQL.

    Idempotent: a verdict that already has rows is skipped, so re-running it
    on every `init_schema()` is a no-op. A verdict whose `reasons_json` is
    empty legitimately produces no rows and is simply re-examined next time.

    The whole backfill is one transaction. Without it a failure partway
    through would leave some verdict half-projected — and because the skip
    guard is "does this verdict have any rows", that half would then be
    skipped forever instead of being completed on the next start.

    Args:
        conn: The caller's connection, so the migration runs against the same
            database handle `init_schema()` is already holding.

    Returns:
        How many reasons were written, for tests and logging.
    """
    rows = conn.execute(
        """
        SELECT v.run_id, v.symbol, v.reasons_json
        FROM verdicts v
        WHERE NOT EXISTS (
            SELECT 1 FROM verdict_reasons r
            WHERE r.run_id = v.run_id AND r.symbol = v.symbol
        )
        ORDER BY v.run_id, v.symbol
        """
    ).fetchall()
    if not rows:
        return 0
    written = 0
    conn.execute("BEGIN TRANSACTION")
    try:
        for run_id, symbol, reasons_json in rows:
            reasons = _reasons_from_json(str(reasons_json))
            _insert_reason_rows(conn, str(run_id), str(symbol), reasons)
            written += len(reasons)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    return written


def get_verdict_collection_digests(database: Database) -> dict[UUID, str]:
    """Return every collected run's document fingerprint (Issue #209).

    One query for the whole table rather than one per run directory: the scan
    compares against every archive it walks, and the table holds one short row
    per already-collected run.
    """
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT run_id, document_digest FROM verdict_collections"
        ).fetchall()
    return {UUID(str(row[0])): str(row[1]) for row in rows}


def get_recorded_outcome_slices(
    database: Database, run_ids: Sequence[UUID]
) -> dict[tuple[UUID, int], frozenset[tuple[str, str]]]:
    """Return each recorded `(run, horizon)` slice's `(symbol, recommendation)` set.

    Issue #209: what an evaluation would have to reproduce for a slice to be
    left alone. The recommendation travels with the symbol so a corrected
    verdict -- a symbol added, dropped, or flipped between `proceed` and
    `skip` -- shows up as a different set and forces the slice to be
    reclassified rather than silently kept.

    Args:
        database: Shared DuckDB connection owner.
        run_ids: The runs to report on. Empty returns an empty mapping
            without querying.
    """
    if not run_ids:
        return {}
    placeholders = ", ".join("?" for _ in run_ids)
    with database.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT run_id, horizon_days, symbol, recommendation
            FROM verdict_outcomes
            WHERE run_id IN ({placeholders})
            """,  # noqa: S608 - placeholders only, every value stays bound
            [str(run_id) for run_id in run_ids],
        ).fetchall()
    slices: dict[tuple[UUID, int], set[tuple[str, str]]] = defaultdict(set)
    for row in rows:
        slices[(UUID(str(row[0])), int(row[1]))].add((str(row[2]), str(row[3])))
    return {key: frozenset(value) for key, value in slices.items()}


def get_analysis_source_coverages(
    database: Database, run_id: UUID, symbol: str
) -> tuple[AnalysisSourceCoverageRecord, ...]:
    """Return one symbol's archived filing coverage in source order."""
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT source_id, original_chars, exported_chars, is_truncated,
                   selection_mode, sections_json, exhibit_truncated
            FROM analysis_source_coverage
            WHERE run_id = ? AND symbol = ?
            ORDER BY source_id
            """,
            [str(run_id), symbol],
        ).fetchall()
    return tuple(
        AnalysisSourceCoverageRecord(
            run_id=run_id,
            symbol=symbol,
            source_id=row[0],
            original_chars=row[1],
            exported_chars=row[2],
            is_truncated=row[3],
            selection_mode=row[4],
            sections=tuple(
                (str(section["name"]), str(section["status"]))
                for section in json.loads(str(row[5]))
            ),
            exhibit_truncated=row[6],
        )
        for row in rows
    )


def get_analysis_source_coverages_in_window(
    database: Database, window_start: date, as_of: date
) -> tuple[AnalysisSourceCoverageRecord, ...]:
    """Return coverage for run-symbols with an outcome maturing in the window."""
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ac.run_id, ac.symbol, ac.source_id,
                   ac.original_chars, ac.exported_chars, ac.is_truncated,
                   ac.selection_mode, ac.sections_json, ac.exhibit_truncated
            FROM analysis_source_coverage ac
            JOIN verdict_outcomes vo
              ON vo.run_id = ac.run_id AND vo.symbol = ac.symbol
            WHERE vo.as_of >= ? AND vo.as_of <= ?
            ORDER BY ac.run_id, ac.symbol, ac.source_id
            """,
            [window_start, as_of],
        ).fetchall()
    return tuple(
        AnalysisSourceCoverageRecord(
            run_id=UUID(str(row[0])),
            symbol=row[1],
            source_id=row[2],
            original_chars=row[3],
            exported_chars=row[4],
            is_truncated=row[5],
            selection_mode=row[6],
            sections=tuple(
                (str(section["name"]), str(section["status"]))
                for section in json.loads(str(row[7]))
            ),
            exhibit_truncated=row[8],
        )
        for row in rows
    )


def replace_verdict_outcomes(
    database: Database,
    run_id: UUID,
    horizon_days: int,
    outcomes: Sequence[VerdictOutcomeRecord],
) -> None:
    """Atomically replace one `(run_id, horizon_days)` slice of classifications.

    Args:
        database: Shared DuckDB connection owner.
        run_id: The evaluated run.
        horizon_days: The evaluated horizon (5 or 20).
        outcomes: Classified rows. Empty clears the slice.

    Raises:
        ValueError: A record's `run_id`/`horizon_days` disagrees with the
            replacement scope, or a return is not a finite number.
    """
    if any(
        outcome.run_id != run_id or outcome.horizon_days != horizon_days
        for outcome in outcomes
    ):
        msg = "all outcomes must match the replacement run_id and horizon_days"
        raise ValueError(msg)

    # `forward_return_pct DOUBLE NOT NULL` cannot express "a measured, finite
    # return": DuckDB's NaN is not NULL, so a non-finite value would persist
    # as a row that is neither a win nor a loss and would silently skew every
    # aggregate over it (Issue #206, defense layer added by Issue #227). The
    # producers already return `None` instead of a non-finite return, so this
    # states the intent the column cannot -- and does so before the
    # transaction opens, leaving the previous slice untouched.
    non_finite = [
        f"{outcome.symbol}@{outcome.as_of}"
        for outcome in outcomes
        if not math.isfinite(outcome.forward_return_pct)
        or (
            outcome.benchmark_return_pct is not None
            and not math.isfinite(outcome.benchmark_return_pct)
        )
    ]
    if non_finite:
        msg = f"verdict outcome returns must be finite: {', '.join(non_finite)}"
        raise ValueError(msg)

    with database.connect() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(
                "DELETE FROM verdict_outcomes WHERE run_id = ? AND horizon_days = ?",
                [str(run_id), horizon_days],
            )
            for outcome in outcomes:
                conn.execute(
                    _INSERT_VERDICT_OUTCOME,
                    [
                        str(outcome.run_id),
                        outcome.symbol,
                        outcome.horizon_days,
                        outcome.as_of,
                        outcome.recommendation,
                        outcome.forward_return_pct,
                        outcome.benchmark_return_pct,
                        outcome.classification,
                    ],
                )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def get_verdicts_in_window(
    database: Database, window_start: date, as_of: date
) -> tuple[VerdictRow, ...]:
    """Return collected verdicts whose run date falls in `[window_start, as_of]`.

    Both ends are inclusive: the run dated exactly `as_of` is in scope, and a
    run dated one day later is not (the point-in-time cutoff is a `<=`).

    Args:
        database: Shared DuckDB connection owner.
        window_start: Inclusive earliest run date to evaluate.
        as_of: Inclusive latest run date to evaluate.

    Returns:
        Rows ordered by `(as_of, run_id, symbol)` for a deterministic
        evaluation order.
    """
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, symbol, as_of, recommendation,
                   news_supply_collected_items, news_supply_exported_items,
                   news_supply_symbol_mention_items, news_supply_level
            FROM verdicts
            WHERE as_of >= ? AND as_of <= ?
            ORDER BY as_of, run_id, symbol
            """,
            [window_start, as_of],
        ).fetchall()
    return tuple(
        VerdictRow(
            run_id=UUID(str(row[0])),
            symbol=row[1],
            as_of=row[2],
            recommendation=row[3],
            news_supply=_news_supply_from_row(row[4], row[5], row[6], row[7]),
        )
        for row in rows
    )


def get_verdict_outcomes_in_window(
    database: Database, window_start: date, as_of: date
) -> tuple[VerdictOutcomeRecord, ...]:
    """Return classifications whose *maturity* date falls in `[window_start, as_of]`.

    The window is matched against `verdict_outcomes.as_of`, which holds the
    maturity session rather than the observation date (decision D7). So the
    aggregate window means "verdicts that came due in this period", and
    re-running the retrospective later does not shuffle rows between windows.

    Args:
        database: Shared DuckDB connection owner.
        window_start: Inclusive earliest maturity date.
        as_of: Inclusive latest maturity date, the retrospective's cutoff.

    Returns:
        Rows ordered by `(as_of, run_id, symbol, horizon_days)` so every
        aggregate built from them is deterministic.
    """
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, symbol, horizon_days, as_of, recommendation,
                   forward_return_pct, classification, benchmark_return_pct
            FROM verdict_outcomes
            WHERE as_of >= ? AND as_of <= ?
            ORDER BY as_of, run_id, symbol, horizon_days
            """,
            [window_start, as_of],
        ).fetchall()
    return tuple(
        VerdictOutcomeRecord(
            run_id=UUID(str(row[0])),
            symbol=row[1],
            horizon_days=row[2],
            as_of=row[3],
            recommendation=row[4],
            forward_return_pct=row[5],
            classification=row[6],
            benchmark_return_pct=row[7],
        )
        for row in rows
    )


def get_run_verdicts(database: Database, run_id: UUID) -> tuple[VerdictRecord, ...]:
    """Return one run's collected verdicts, reasons included.

    Args:
        database: Shared DuckDB connection owner.
        run_id: The archived run to read back.

    Returns:
        Rows ordered by symbol, empty for a run that was never collected.
    """
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, symbol, as_of, strategy_key, recommendation,
                   reasons_json, no_trade, news_supply_collected_items,
                   news_supply_exported_items, news_supply_symbol_mention_items,
                   news_supply_level
            FROM verdicts
            WHERE run_id = ?
            ORDER BY symbol
            """,
            [str(run_id)],
        ).fetchall()
    return tuple(
        VerdictRecord(
            run_id=UUID(str(row[0])),
            symbol=row[1],
            as_of=row[2],
            strategy_key=row[3],
            recommendation=row[4],
            reasons=_reasons_from_json(row[5]),
            no_trade=row[6],
            news_supply=_news_supply_from_row(row[7], row[8], row[9], row[10]),
        )
        for row in rows
    )


def get_prior_verdicts(
    database: Database,
    symbol: str,
    strategy_key: str,
    before_date: date,
    limit: int,
) -> tuple[PriorVerdictRecord, ...]:
    """Return a symbol's most recent earlier verdicts, newest first (Issue #191).

    This reports the analysis layer's own past judgement, which exists for
    every symbol a past run analyzed. Since 2026-08 it is the only "what
    happened last time" feedback the export carries: the human decision
    journal it used to sit beside was removed with the rest of the real-trade
    record feature.

    Point-in-time: `as_of < before_date` strictly, so today's own verdict can
    never be fed back into today's input. Also bounded below by
    `reason_text_visible_sql()` (Issue #389, relaxing #385's plain
    `as_of >= ACCOUNT_INDEPENDENT_VERDICT_CUTOFF`): a verdict whose owning run
    neither started on/after `ACCOUNT_INDEPENDENT_EXPORT_SINCE` nor is dated
    on/after `ACCOUNT_INDEPENDENT_VERDICT_CUTOFF` may describe a reader's
    account verbatim in its reason text, and that text is never rewritten, so
    it must never be re-injected into a fresh `analysis_input.json`. The
    filter is applied inside the `recent` CTE, before `LIMIT` -- a verdict it
    excludes never consumes one of the `limit` slots.

    Args:
        database: Shared DuckDB connection owner.
        symbol: The candidate's ticker.
        strategy_key: Only verdicts produced by the same strategy are
            comparable feedback.
        before_date: Exclusive upper bound on the verdict's `as_of`.
        limit: Maximum number of verdicts, newest first. `<= 0` returns empty.

    Returns:
        Newest-first records, each carrying every horizon that has matured for
        it, ordered by horizon.
    """
    if limit <= 0:
        return ()
    predicate = reason_text_visible_sql("owning_run.started_at", "v.as_of")
    with database.connect() as conn:
        rows = conn.execute(
            f"""
            WITH recent AS (
                SELECT v.run_id, v.as_of, v.symbol, v.strategy_key,
                       v.recommendation, v.reasons_json
                FROM verdicts v
                LEFT JOIN runs owning_run ON owning_run.run_id = v.run_id
                WHERE v.symbol = ? AND v.strategy_key = ? AND v.as_of < ?
                  AND {predicate}
                ORDER BY v.as_of DESC, v.run_id DESC
                LIMIT ?
            )
            SELECT r.run_id, r.as_of, r.symbol, r.strategy_key, r.recommendation,
                   r.reasons_json, o.horizon_days, o.classification,
                   o.forward_return_pct
            FROM recent AS r
            LEFT JOIN verdict_outcomes AS o
              ON o.run_id = r.run_id AND o.symbol = r.symbol
            ORDER BY r.as_of DESC, r.run_id DESC, o.horizon_days
            """,  # noqa: S608 - predicate from a fixed internal helper, no interpolated input
            [
                symbol,
                strategy_key,
                before_date,
                ACCOUNT_INDEPENDENT_EXPORT_SINCE,
                ACCOUNT_INDEPENDENT_VERDICT_CUTOFF,
                limit,
            ],
        ).fetchall()

    outcomes: dict[str, list[PriorVerdictOutcome]] = defaultdict(list)
    heads: dict[str, tuple[object, ...]] = {}
    for row in rows:
        run_key = str(row[0])
        if run_key not in heads:
            heads[run_key] = tuple(row[1:6])
        if row[6] is not None:
            outcomes[run_key].append(
                PriorVerdictOutcome(
                    horizon_days=row[6],
                    classification=row[7],
                    forward_return_pct=row[8],
                )
            )
    return tuple(
        PriorVerdictRecord(
            run_id=UUID(run_key),
            as_of=cast("date", head[0]),
            symbol=str(head[1]),
            strategy_key=str(head[2]),
            recommendation=str(head[3]),
            reasons=_reasons_from_json(str(head[4])),
            outcomes=tuple(outcomes[run_key]),
        )
        for run_key, head in heads.items()
    )


def get_verdict_reason_bases_in_window(
    database: Database, window_start: date, as_of: date
) -> tuple[VerdictReasonBasisRow, ...]:
    """Return the distinct bases cited by verdicts that matured in the window.

    The `basis` counterpart of `get_verdict_citations_in_window`: one row per
    `(run, symbol, basis)`, so a hit rate can be computed per evidence kind
    the same way it already is per source provider (Issue #191).

    Args:
        database: Shared DuckDB connection owner.
        window_start: Inclusive earliest maturity date.
        as_of: Inclusive latest maturity date.

    Returns:
        Rows ordered by `(run_id, symbol, basis)`, untagged reasons last.
    """
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT v.run_id, v.symbol, v.reasons_json
            FROM verdicts AS v
            JOIN (
                SELECT DISTINCT run_id, symbol
                FROM verdict_outcomes
                WHERE as_of >= ? AND as_of <= ?
            ) AS m ON m.run_id = v.run_id AND m.symbol = v.symbol
            ORDER BY v.run_id, v.symbol
            """,
            [window_start, as_of],
        ).fetchall()
    return tuple(
        VerdictReasonBasisRow(run_id=UUID(str(row[0])), symbol=row[1], basis=basis)
        for row in rows
        for basis in sorted(
            {reason.basis for reason in _reasons_from_json(str(row[2]))},
            key=lambda value: (value is None, value or ""),
        )
    )


def _reasons_from_json(raw: str) -> tuple[VerdictReasonRecord, ...]:
    """Rebuild the reason list `replace_run_verdicts` serialized.

    `basis` is read with `.get()`: rows archived before Issue #191 carry no
    such key, and an untagged reason must come back as `None` rather than
    fail the whole run's read.
    """
    reasons: list[dict[str, object]] = json.loads(str(raw))
    return tuple(
        VerdictReasonRecord(
            text=str(reason["text"]),
            source_ids=tuple(str(value) for value in list(reason["source_ids"])),  # type: ignore[call-overload]
            basis=_optional_str(reason.get("basis")),
        )
        for reason in reasons
    )


def _optional_str(value: object) -> str | None:
    """Return `value` as text, preserving a missing/null tag as `None`."""
    return None if value is None else str(value)


def get_verdict_citations_in_window(
    database: Database, window_start: date, as_of: date
) -> tuple[VerdictCitationRow, ...]:
    """Return the sources cited by verdicts that matured in the window.

    One row per `(run, symbol, source)`, not per horizon: a source cited once
    must not be counted twice because both horizons came due.

    Args:
        database: Shared DuckDB connection owner.
        window_start: Inclusive earliest maturity date.
        as_of: Inclusive latest maturity date.

    Returns:
        Rows ordered by `(run_id, symbol, source_id)`.
    """
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT vs.run_id, vs.symbol, vs.source_id, vs.source_type,
                   ti.source_url
            FROM verdict_sources vs
            JOIN verdict_outcomes vo
              ON vo.run_id = vs.run_id AND vo.symbol = vs.symbol
            LEFT JOIN text_items ti ON ti.source_id = vs.source_id
            WHERE vo.as_of >= ? AND vo.as_of <= ?
            ORDER BY vs.run_id, vs.symbol, vs.source_id
            """,
            [window_start, as_of],
        ).fetchall()
    return tuple(
        VerdictCitationRow(
            run_id=UUID(str(row[0])),
            symbol=row[1],
            source_id=row[2],
            source_type=row[3],
            source_url=row[4],
        )
        for row in rows
    )


def _reject_foreign_run(run_id: UUID, candidates: Iterable[UUID]) -> None:
    """Raise if any record's run identity disagrees with the replacement scope."""
    if any(candidate != run_id for candidate in candidates):
        msg = "all records must match the replacement run_id"
        raise ValueError(msg)
