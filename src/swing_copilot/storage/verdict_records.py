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
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from uuid import UUID

from swing_copilot.storage.json_guard import dumps_safe

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date

    from swing_copilot.storage.database import Database

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

_INSERT_ANALYSIS_COVERAGE = """
    INSERT INTO analysis_source_coverage (
        run_id, symbol, source_id, original_chars, exported_chars,
        is_truncated, selection_mode, exhibit_truncated, sections_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_VERDICT_OUTCOME = """
    INSERT INTO verdict_outcomes (
        run_id, symbol, horizon_days, as_of, recommendation,
        forward_return_pct, classification
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
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


@dataclass(frozen=True, slots=True)
class VerdictDecisionRow:
    """One human decision paired with the verdict it accepted or overrode.

    `trades_journal` x `verdicts` x `verdict_outcomes` (E31.5). The realized
    figure is the horizon's `forward_return_pct`, deliberately not an
    execution-aware P&L: the retrospective adds no new realized-return
    calculation, and `skip` symbols were never actually traded anyway
    (design §12).
    """

    run_id: UUID
    symbol: str
    strategy_key: str
    decision: str
    recommendation: str
    horizon_days: int
    forward_return_pct: float
    classification: str


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


def replace_run_verdicts(
    database: Database,
    run_id: UUID,
    verdicts: Sequence[VerdictRecord],
    sources: Sequence[VerdictSourceRecord],
    coverages: Sequence[AnalysisSourceCoverageRecord] = (),
) -> None:
    """Atomically replace one run's complete verdict and citation set.

    Args:
        database: Shared DuckDB connection owner.
        run_id: The run whose rows are being replaced wholesale.
        verdicts: The run's verdicts. Empty clears the run (a re-ingest of a
            result that no longer analyzes any symbol).
        sources: The `source_id`s those verdicts' analyses cited.
        coverages: Every filing source offered to the analysis, cited or not.

    Raises:
        ValueError: A record belongs to a different run than `run_id`.
    """
    _reject_foreign_run(run_id, (record.run_id for record in verdicts))
    _reject_foreign_run(run_id, (record.run_id for record in sources))
    _reject_foreign_run(run_id, (record.run_id for record in coverages))

    with database.connect() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute("DELETE FROM verdicts WHERE run_id = ?", [str(run_id)])
            conn.execute("DELETE FROM verdict_sources WHERE run_id = ?", [str(run_id)])
            conn.execute(
                "DELETE FROM analysis_source_coverage WHERE run_id = ?",
                [str(run_id)],
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
            replacement scope.
    """
    if any(
        outcome.run_id != run_id or outcome.horizon_days != horizon_days
        for outcome in outcomes
    ):
        msg = "all outcomes must match the replacement run_id and horizon_days"
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
                   forward_return_pct, classification
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

    Deliberately a separate read from `paper_records.get_decision_history()`
    even though both answer "what happened last time". That one reports the
    *human* journal, which only has a row when the operator recorded one; this
    one reports the analysis layer's own past judgement, which exists for every
    symbol a past run analyzed. Joining the two would have made the feedback
    visible only for journaled symbols -- the case the issue is least about.

    Point-in-time: `as_of < before_date` strictly, matching the decision-history
    cutoff, so today's own verdict can never be fed back into today's input.

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
    with database.connect() as conn:
        rows = conn.execute(
            """
            WITH recent AS (
                SELECT run_id, as_of, symbol, strategy_key, recommendation,
                       reasons_json
                FROM verdicts
                WHERE symbol = ? AND strategy_key = ? AND as_of < ?
                ORDER BY as_of DESC, run_id DESC
                LIMIT ?
            )
            SELECT r.run_id, r.as_of, r.symbol, r.strategy_key, r.recommendation,
                   r.reasons_json, o.horizon_days, o.classification,
                   o.forward_return_pct
            FROM recent AS r
            LEFT JOIN verdict_outcomes AS o
              ON o.run_id = r.run_id AND o.symbol = r.symbol
            ORDER BY r.as_of DESC, r.run_id DESC, o.horizon_days
            """,
            [symbol, strategy_key, before_date, limit],
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


def get_verdict_decision_alignment(
    database: Database, window_start: date, as_of: date
) -> tuple[VerdictDecisionRow, ...]:
    """Return matured verdicts paired with the human decision recorded for them.

    Joined on `(run_id, symbol)` rather than also on `strategy_key`: a verdict
    is one judgement per symbol per run, so every strategy row the human
    journaled for that symbol is measured against the same verdict.

    Args:
        database: Shared DuckDB connection owner.
        window_start: Inclusive earliest maturity date.
        as_of: Inclusive latest maturity date.

    Returns:
        Rows ordered by `(run_id, symbol, strategy_key, horizon_days)`. Empty
        when the journal is empty -- the cross-tab is observation-only and a
        user who never journals simply has nothing to cross.
    """
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT tj.run_id, tj.symbol, tj.strategy_key, tj.decision,
                   v.recommendation, vo.horizon_days, vo.forward_return_pct,
                   vo.classification
            FROM trades_journal tj
            JOIN verdicts v ON v.run_id = tj.run_id AND v.symbol = tj.symbol
            JOIN verdict_outcomes vo
              ON vo.run_id = tj.run_id AND vo.symbol = tj.symbol
            WHERE vo.as_of >= ? AND vo.as_of <= ?
            ORDER BY tj.run_id, tj.symbol, tj.strategy_key, vo.horizon_days
            """,
            [window_start, as_of],
        ).fetchall()
    return tuple(
        VerdictDecisionRow(
            run_id=UUID(str(row[0])),
            symbol=row[1],
            strategy_key=row[2],
            decision=row[3],
            recommendation=row[4],
            horizon_days=row[5],
            forward_return_pct=row[6],
            classification=row[7],
        )
        for row in rows
    )


def _reject_foreign_run(run_id: UUID, candidates: Iterable[UUID]) -> None:
    """Raise if any record's run identity disagrees with the replacement scope."""
    if any(candidate != run_id for candidate in candidates):
        msg = "all records must match the replacement run_id"
        raise ValueError(msg)
