"""DDL for every table `StateStore` owns (`docs/04_detailed_design.md` 4.2).

Split out from `state_store.py` to keep that module under the project's
300-line guideline; this file is schema only, no behavior.

There is no formal migration runner: `INIT_SCHEMA_STATEMENTS`' `CREATE TABLE
IF NOT EXISTS` is a no-op against a database that already has an older shape
of a table. Additive column changes to an existing table (e.g. P1-03's
`risk_assessments` columns) go in `ALTER_SCHEMA_STATEMENTS` instead, run
after `INIT_SCHEMA_STATEMENTS` in `StateStore.init_schema()`, using `ALTER
TABLE ... ADD COLUMN IF NOT EXISTS` so re-running is a no-op on both a fresh
database (columns already exist from `CREATE TABLE`) and an already-upgraded
one. DuckDB (as of 1.5.x) rejects `ADD COLUMN` with an inline `CHECK` or
`NOT NULL` constraint ("Adding columns with constraints not yet supported"),
so altered columns are unconstrained at the database level even where the
matching `CREATE TABLE` column has a `CHECK`/`NOT NULL` — application code
is the sole enforcement point for rows added to a pre-P1-03 database via this
path.

A promoted column (Issue #192: a value that used to live only inside a JSON
column) additionally carries a backfill `UPDATE` in the same tuple, right
after its `ADD COLUMN`. That is deliberate and distinct from the "not
recorded, do not guess" columns above: the value is already present in the
existing rows, just in the wrong shape, so restating it in a column is not an
invention. Each backfill is guarded by `WHERE <column> IS NULL` on a column
the writer always populates, which makes it idempotent and a no-op scan on
every later start. A value that was never persisted at all — `candidates`'
`execution_state`/`execution_distance` — is explicitly *not* backfilled and
stays NULL, meaning "not recorded".

An entirely new table (Issue #189's `retro_sessions` / `retro_narrations` /
`config_versions`) needs no `ALTER_SCHEMA_STATEMENTS` entry at all: `CREATE
TABLE IF NOT EXISTS` creates it on a fresh database and on the production one
alike, and it starts empty in both cases because the history it would hold was
never written anywhere. Only a change to an *existing* table's shape needs the
ALTER path.

`verdict_reasons`/`verdict_reason_sources` are backfilled the same way but in
Python (`verdict_records.backfill_verdict_reasons`), because normalizing
`reasons_json` reuses that module's own parsing rather than duplicating it as
nested JSON SQL.

Removal is the mirror image: `DROP_SCHEMA_STATEMENTS` runs *before* the
create/alter tuples in `StateStore.init_schema()`, so a table this project no
longer owns disappears — with its rows — from every copy of the database the
next time one is opened. `init_schema()` is on the daily run's path and on
every test's path, which is what makes it reach the operator's file, the copy
restored from R2, and a CI runner's alike, rather than only wherever a
one-shot migration script happened to be run.
"""

from __future__ import annotations

# 2026-08: the real-trade record feature (FR-11/CON-04's paper-trading gate and
# its decision journal) and the virtual ledger's human notes were removed so
# the daily analysis can be published as a track record without exposing what
# the operator personally bought or chose to skip. Dropping them here, rather
# than in a one-shot script, is what makes the removal reach every database
# copy; see the module docstring. `verdict_positions`/`verdict_position_marks`
# — the mechanical virtual tracking — are deliberately untouched.
DROP_SCHEMA_STATEMENTS: tuple[str, ...] = (
    "DROP TABLE IF EXISTS trades_journal",
    "DROP TABLE IF EXISTS position_excursions",
    "DROP TABLE IF EXISTS positions",
    "DROP TABLE IF EXISTS verdict_position_notes",
)

INIT_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS universe_membership (
        snapshot_date  DATE NOT NULL,
        symbol         VARCHAR NOT NULL,
        source_symbol  VARCHAR NOT NULL,
        company_name   VARCHAR NOT NULL,
        gics_sector    VARCHAR NOT NULL,
        source         VARCHAR NOT NULL,
        PRIMARY KEY (snapshot_date, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id          UUID PRIMARY KEY,
        run_date        DATE NOT NULL,
        mode            VARCHAR NOT NULL CHECK (mode IN ('live', 'dry_run')),
        config_hash     VARCHAR NOT NULL,
        metadata_json   JSON NOT NULL DEFAULT '{}',
        status          VARCHAR NOT NULL
            CHECK (status IN ('running','success','degraded','failed')),
        started_at      TIMESTAMPTZ NOT NULL,
        completed_at    TIMESTAMPTZ,
        report_path     VARCHAR,
        error_summary   VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_steps (
        run_id       UUID NOT NULL,
        step         VARCHAR NOT NULL,
        status       VARCHAR NOT NULL CHECK (status IN ('success','failed','skipped')),
        detail       VARCHAR,
        duration_s   DOUBLE NOT NULL,
        PRIMARY KEY (run_id, step)
    )
    """,
    # Legacy (Issue #192): keyed by `run_date`, so a `dry_run` and a `live`
    # run of the same date overwrite each other's rows, and nothing can join
    # them to the `run_id`-keyed tables. Kept read-only for the history it
    # already holds; `signal_hits` below is the keyed successor every writer
    # now uses.
    """
    CREATE TABLE IF NOT EXISTS signals (
        run_date      DATE NOT NULL,
        symbol        VARCHAR NOT NULL,
        strategy_key  VARCHAR NOT NULL,
        signal_name   VARCHAR NOT NULL,
        strength      DOUBLE NOT NULL,
        metrics_json  JSON NOT NULL,
        PRIMARY KEY (run_date, symbol, strategy_key, signal_name)
    )
    """,
    # Issue #192: `signals`, re-keyed by `run_id`. DuckDB cannot change a
    # primary key in place, so this is a new table rather than an ALTER; the
    # old one keeps its rows and is never written again. Written inside
    # `record_screening_results`' transaction, because a run's hits and the
    # ranking built from them are one logical write -- a committed candidate
    # set whose hits are missing would misstate which signals fired.
    """
    CREATE TABLE IF NOT EXISTS signal_hits (
        run_id        UUID NOT NULL,
        symbol        VARCHAR NOT NULL,
        strategy_key  VARCHAR NOT NULL,
        signal_name   VARCHAR NOT NULL,
        strength      DOUBLE NOT NULL,
        metrics_json  JSON NOT NULL,
        PRIMARY KEY (run_id, symbol, strategy_key, signal_name)
    )
    """,
    # Issue #192: the ranking key and its components are real columns, not
    # `metrics_json` extractions. `score`/`score_*` were always *in*
    # `metrics_json` (so a database created before this change is backfilled
    # from it, see `ALTER_SCHEMA_STATEMENTS`), but `execution_state` /
    # `execution_distance` were `Candidate` fields that reached no column at
    # all -- recovering them for an old row would mean replaying that day's
    # config thresholds, so they stay NULL there. NULL therefore means "not
    # recorded", which readers must not read as the `UNKNOWN` state (a
    # measured "distance not computable"). `metrics_json` is preserved as the
    # full raw indicator set.
    """
    CREATE TABLE IF NOT EXISTS candidates (
        run_id              UUID NOT NULL,
        symbol              VARCHAR NOT NULL,
        strategy_key        VARCHAR NOT NULL,
        rank                INTEGER NOT NULL,
        signal_names        VARCHAR[] NOT NULL,
        metrics_json        JSON NOT NULL,
        score               DOUBLE,
        score_rsi_pullback  DOUBLE,
        score_trend_quality DOUBLE,
        score_liquidity     DOUBLE,
        score_atr_pct       DOUBLE,
        score_pivot_proximity DOUBLE,
        score_rs_percentile   DOUBLE,
        score_criteria_met    DOUBLE,
        execution_state     VARCHAR,
        execution_distance  DOUBLE,
        PRIMARY KEY (run_id, symbol, strategy_key),
        UNIQUE (run_id, strategy_key, rank)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS screening_rejections (
        run_id       UUID NOT NULL,
        symbol       VARCHAR NOT NULL,
        stage        VARCHAR NOT NULL CHECK (stage IN ('data_quality','fundamental_filter','technical_signal')),
        reason_code  VARCHAR NOT NULL CHECK (reason_code IN (
            'FILTER_NEGATIVE_NET_INCOME','FILTER_NEGATIVE_FCF','FILTER_LOW_EQUITY_RATIO',
            'FILTER_LOW_LIQUIDITY','SIGNAL_TREND_NOT_MET','SIGNAL_RSI_NOT_MET','DATA_INSUFFICIENT_HISTORY',
            'DATA_MISSING_NET_INCOME'
        )),
        detail       JSON NOT NULL,
        as_of        DATE NOT NULL,
        PRIMARY KEY (run_id, symbol)
    )
    """,
    # Issue #188: the other side of the same ranking `candidates` holds. A
    # symbol here failed nothing -- it was cut by `candidate_limit` -- so it
    # cannot be a `screening_rejections` row (that table's `reason_code` is a
    # closed enum guarded by a CHECK constraint, and "ranked 11th" is a
    # configuration cap, not a verdict). Written in the SAME transaction as
    # `candidates`, because the two are one ranking's two halves and a run
    # holding one without the other would silently misstate the cut.
    #
    # The score components are exploded into typed columns rather than kept
    # as `metrics_json`: unlike a candidate's metrics (which feed the report
    # and the analysis export), the only reason these rows exist is to be
    # aggregated -- "is rank 6-10 really worse than 1-5" is a GROUP BY, and
    # a JSON extraction per column would make every such query re-CAST.
    """
    CREATE TABLE IF NOT EXISTS screening_truncations (
        run_id              UUID NOT NULL,
        symbol              VARCHAR NOT NULL,
        strategy_key        VARCHAR NOT NULL,
        rank                INTEGER NOT NULL,
        score               DOUBLE NOT NULL,
        score_rsi_pullback  DOUBLE,
        score_trend_quality DOUBLE,
        score_liquidity     DOUBLE,
        score_atr_pct       DOUBLE,
        score_pivot_proximity DOUBLE,
        score_rs_percentile   DOUBLE,
        score_criteria_met    DOUBLE,
        execution_state     VARCHAR NOT NULL,
        execution_distance  DOUBLE,
        as_of               DATE NOT NULL,
        PRIMARY KEY (run_id, symbol, strategy_key)
    )
    """,
    # Issue #188: forward returns for the control groups, so the measurable
    # question stops being "how often was a candidate wrong" (false positives
    # only) and becomes "was the cut itself right". One row per
    # (evaluated run, symbol, horizon); `run_id` is the HISTORICAL run being
    # evaluated, exactly like `signal_outcomes.run_id`.
    #
    # `outcome_class` says which side of the run's own screening the symbol
    # was on, and `reason_code` carries `screening_rejections.reason_code` for
    # the rejected side (NULL for the other two, which were rejected by
    # nothing) -- which is what makes `SELECT reason_code,
    # avg(forward_return_pct) ... WHERE outcome_class = 'rejected' GROUP BY 1`
    # a one-line answer to "does this filter earn its place".
    # Deliberately not merged into `signal_outcomes`: that table is
    # per-signal-attributed and classified into hit/miss buckets, whereas this
    # one is a raw return per screening decision, including symbols no signal
    # ever fired on.
    """
    CREATE TABLE IF NOT EXISTS universe_forward_returns (
        run_id             UUID NOT NULL,
        symbol             VARCHAR NOT NULL,
        horizon_days       INTEGER NOT NULL CHECK (horizon_days IN (5, 20)),
        as_of              DATE NOT NULL,
        outcome_class      VARCHAR NOT NULL CHECK (outcome_class IN (
            'candidate','truncated','rejected'
        )),
        reason_code        VARCHAR,
        forward_return_pct DOUBLE NOT NULL,
        PRIMARY KEY (run_id, symbol, horizon_days)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS risk_assessments (
        run_id          UUID NOT NULL,
        symbol          VARCHAR NOT NULL,
        status          VARCHAR NOT NULL
            CHECK (status IN ('approved','rejected','not_calculable')),
        max_shares      BIGINT,
        entry_price     DOUBLE,
        limit_price     DOUBLE,
        stop_price      DOUBLE,
        reasons_json    JSON NOT NULL,
        warnings_json   JSON NOT NULL,
        shares_by_risk          BIGINT,
        shares_by_position_cap  BIGINT,
        binding_constraint      VARCHAR
            CHECK (binding_constraint IN (
                'trade_risk','position_cap','sector','correlation','regime','portfolio_heat','earnings','not_calculable'
            )),
        sizing_warnings_json    JSON NOT NULL DEFAULT '[]',
        PRIMARY KEY (run_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS earnings_calendar (
        symbol          VARCHAR PRIMARY KEY,
        earnings_date   DATE NOT NULL,
        session         VARCHAR NOT NULL,
        fetched_at      TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS text_items (
        source_id       VARCHAR PRIMARY KEY,
        symbol          VARCHAR,
        source_type     VARCHAR NOT NULL,
        published_at    TIMESTAMPTZ NOT NULL,
        title           VARCHAR,
        source_url      VARCHAR NOT NULL,
        content_text    VARCHAR NOT NULL,
        fetched_at      TIMESTAMPTZ NOT NULL,
        related_symbols VARCHAR,
        category        VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signal_outcomes (
        run_id             UUID NOT NULL,
        symbol             VARCHAR NOT NULL,
        horizon_days       INTEGER NOT NULL CHECK (horizon_days IN (5, 20)),
        as_of              DATE NOT NULL,
        signal_names       VARCHAR[] NOT NULL,
        forward_return_pct DOUBLE NOT NULL,
        classification     VARCHAR NOT NULL CHECK (classification IN (
            'TRUE_POSITIVE','FALSE_POSITIVE_MILD','FALSE_POSITIVE_SEVERE','NEUTRAL'
        )),
        PRIMARY KEY (run_id, symbol, horizon_days)
    )
    """,
    # P8-30: the qualitative verdict's own record of truth. Deliberately not
    # merged into `signal_outcomes`: a signal outcome is apportioned across
    # every signal that fired, whereas a verdict is one judgement per symbol
    # per run (design.md §4, decision D5).
    """
    CREATE TABLE IF NOT EXISTS verdicts (
        run_id         UUID NOT NULL,
        symbol         VARCHAR NOT NULL,
        as_of          DATE NOT NULL,
        strategy_key   VARCHAR NOT NULL,
        recommendation VARCHAR NOT NULL
            CHECK (recommendation IN ('proceed','skip')),
        reasons_json   JSON NOT NULL,
        no_trade       BOOLEAN NOT NULL,
        -- Issue #154: the code-owned news-supply measurement the verdict was
        -- made under (`analysis-input-v3`'s `news_supply`, Issue #130), so the
        -- retrospective can test the `sufficient` threshold against what the
        -- verdicts actually did. All four are nullable, and NULL means "the
        -- archive predates the measurement", never `none`/zero -- an archive
        -- written before Issue #130 has nothing to say here.
        news_supply_collected_items      INTEGER,
        news_supply_exported_items       INTEGER,
        news_supply_symbol_mention_items INTEGER,
        news_supply_level                VARCHAR
            CHECK (news_supply_level IN ('sufficient','sparse','none')),
        PRIMARY KEY (run_id, symbol)
    )
    """,
    # Issue #192: `verdicts.reasons_json` normalized, so questions about the
    # reasons themselves ("how did symbols whose only reason cited no source
    # perform?") are a GROUP BY instead of a JSON walk in Python.
    # `reasons_json` stays the record of truth and is still written -- these
    # rows are a derived projection of it, which is why a database created
    # before this change can be (and is) backfilled from it rather than
    # losing the history.
    #
    # `source_id_count` is denormalized from `verdict_reason_sources` on
    # purpose: "reasons resting on no source at all" is the question this
    # table exists for, and a count column answers it without a LEFT JOIN
    # whose zero rows would have to be distinguished from a missing reason.
    """
    CREATE TABLE IF NOT EXISTS verdict_reasons (
        run_id          UUID NOT NULL,
        symbol          VARCHAR NOT NULL,
        reason_index    INTEGER NOT NULL,
        text            VARCHAR NOT NULL,
        basis           VARCHAR,
        source_id_count INTEGER NOT NULL,
        PRIMARY KEY (run_id, symbol, reason_index)
    )
    """,
    # Issue #192: which `source_id`s each individual reason cited. Distinct
    # from `verdict_sources`, which is per (run, symbol): that table answers
    # "what did this symbol's analysis cite", this one answers "what did
    # *this reason* rest on", which is what Issue #191's `basis` tag is
    # checked against.
    """
    CREATE TABLE IF NOT EXISTS verdict_reason_sources (
        run_id       UUID NOT NULL,
        symbol       VARCHAR NOT NULL,
        reason_index INTEGER NOT NULL,
        source_id    VARCHAR NOT NULL,
        PRIMARY KEY (run_id, symbol, reason_index, source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS verdict_sources (
        run_id      UUID NOT NULL,
        symbol      VARCHAR NOT NULL,
        source_id   VARCHAR NOT NULL,
        source_type VARCHAR NOT NULL
            CHECK (source_type IN ('news','filing','calendar')),
        PRIMARY KEY (run_id, symbol, source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analysis_source_coverage (
        run_id          UUID NOT NULL,
        symbol          VARCHAR NOT NULL,
        source_id       VARCHAR NOT NULL,
        original_chars  INTEGER NOT NULL CHECK (original_chars >= 0),
        exported_chars  INTEGER NOT NULL CHECK (exported_chars >= 0),
        is_truncated    BOOLEAN NOT NULL,
        selection_mode  VARCHAR NOT NULL CHECK (selection_mode IN (
            'full','section_priority','section_priority_partial',
            'head_fallback','omitted_symbol_budget'
        )),
        -- Issue #157: collection-stage exhibit truncation, which the three
        -- columns above cannot express. Deliberately nullable even here, where
        -- `ALTER_SCHEMA_STATEMENTS`' inability to add a NOT NULL column does
        -- not force it: NULL means "not recorded", which readers must not
        -- conflate with FALSE ("no marker in the collected text"). Rows in a
        -- pre-existing table are one source of NULL; a `retro collect` of an
        -- archive written before the field existed is the other.
        exhibit_truncated BOOLEAN,
        sections_json   JSON NOT NULL,
        PRIMARY KEY (run_id, symbol, source_id)
    )
    """,
    # Issue #209: the fingerprint of the two documents a run's collected rows
    # were built from. It exists only so `retro collect` can prove a run is
    # unchanged and skip re-parsing it; it is never read as evidence about the
    # verdicts themselves. Written inside the same transaction as those rows,
    # so "the digest says collected" and "the rows exist" can never disagree.
    """
    CREATE TABLE IF NOT EXISTS verdict_collections (
        run_id           UUID PRIMARY KEY,
        document_digest  VARCHAR NOT NULL
    )
    """,
    # `as_of` here is the *maturity* session, not the observation date --
    # intentionally unlike `signal_outcomes.as_of`, so a batch retrospective
    # produces the same rows no matter which day it is run (design §5.2, D7).
    """
    CREATE TABLE IF NOT EXISTS verdict_outcomes (
        run_id             UUID NOT NULL,
        symbol             VARCHAR NOT NULL,
        horizon_days       INTEGER NOT NULL CHECK (horizon_days IN (5, 20)),
        as_of              DATE NOT NULL,
        recommendation     VARCHAR NOT NULL,
        forward_return_pct DOUBLE NOT NULL,
        -- Issue #190: the benchmark's return over the *same* span, so
        -- separation can be stated in excess terms instead of being confounded
        -- with the market's own move. Nullable even here: a row evaluated
        -- before the column existed, or one whose benchmark bars were
        -- unavailable, records NULL ("not measured"), which readers must not
        -- read as a flat benchmark.
        benchmark_return_pct DOUBLE,
        classification     VARCHAR NOT NULL CHECK (classification IN (
            'HIT','MISS_MILD','MISS_SEVERE','NEUTRAL'
        )),
        PRIMARY KEY (run_id, symbol, horizon_days)
    )
    """,
    # Verdict tracking: the virtual position a verdict implies and the daily
    # marks that follow it. Deliberately separate from `verdict_outcomes` (a
    # two-point 5/20 session classification): this layer replays the
    # backtest's own exit rules forward, one trading day at a time.
    #
    # Issue #190: `skip` verdicts are shadow-tracked under the *same* exit
    # rules, which is what makes "proceed only vs every screened candidate" a
    # counterfactual rather than two incomparable numbers. `recommendation`
    # records which side a row belongs to; NULL means `proceed`, because every
    # row written before this change could only have been one.
    """
    CREATE TABLE IF NOT EXISTS verdict_positions (
        run_id              UUID NOT NULL,
        symbol              VARCHAR NOT NULL,
        strategy_key        VARCHAR NOT NULL,
        recommendation      VARCHAR,
        no_trade            BOOLEAN NOT NULL,
        entry_date          DATE NOT NULL,
        entry_price         DOUBLE NOT NULL,
        stop_price          DOUBLE,
        days_held           INTEGER NOT NULL,
        -- Frozen at entry for display; exit replay still reads the active config.
        max_hold_days       INTEGER NOT NULL DEFAULT 25,
        status              VARCHAR NOT NULL CHECK (status IN ('open', 'closed')),
        exit_date           DATE,
        exit_price          DOUBLE,
        exit_reason         VARCHAR
            CHECK (exit_reason IN ('stop', 'max_hold', 'manual')),
        realized_return_pct DOUBLE,
        last_marked_date    DATE,
        PRIMARY KEY (run_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS verdict_position_marks (
        run_id                UUID NOT NULL,
        symbol                VARCHAR NOT NULL,
        as_of_date            DATE NOT NULL,
        close                 DOUBLE NOT NULL,
        stop_price            DOUBLE,
        unrealized_return_pct DOUBLE NOT NULL,
        PRIMARY KEY (run_id, symbol, as_of_date)
    )
    """,
    # Issue #192: the shorter distribution windows and the gate's own inputs
    # are columns, not `detail_json` extractions -- every threshold review
    # reads exactly these, e.g. "would 4 distribution days in 15 sessions have
    # been the better cut". `dd_count_spy`/`dd_count_qqq` remain the 25-session
    # counts they always were. The gate inputs are nullable by design, not
    # merely by ALTER limitation: an unavailable SPY/VIX bar is what produces
    # an `UNKNOWN` gate verdict.
    """
    CREATE TABLE IF NOT EXISTS regime_snapshots (
        run_id          UUID PRIMARY KEY,
        as_of           DATE NOT NULL,
        gate_verdict    VARCHAR NOT NULL,
        dd_count_spy    DOUBLE NOT NULL,
        dd_count_qqq    DOUBLE NOT NULL,
        dd_level        VARCHAR NOT NULL,
        data_quality    VARCHAR NOT NULL,
        detail_json     JSON NOT NULL,
        dd15_spy        DOUBLE,
        dd5_spy         DOUBLE,
        dd15_qqq        DOUBLE,
        dd5_qqq         DOUBLE,
        spy_close       DOUBLE,
        spy_ema         DOUBLE,
        vix_close       DOUBLE,
        spy_sma200      DOUBLE,
        spy_ftd_state   VARCHAR
    )
    """,
    # Issue #192: the inputs the exposure ceiling was derived from, promoted
    # out of `detail_json` for the same reason as `regime_snapshots` above --
    # The legacy multiplier column is retained for old rows; new REDUCE_ONLY
    # decisions are labels and carry 1.0 until Issue #342 removes the field.
    """
    CREATE TABLE IF NOT EXISTS exposure_decisions (
        run_id       UUID PRIMARY KEY,
        verdict      VARCHAR NOT NULL,
        data_quality VARCHAR NOT NULL,
        detail_json  JSON NOT NULL,
        gate_verdict VARCHAR,
        dd_level     VARCHAR,
        is_conservatively_downgraded BOOLEAN,
        reduce_only_risk_multiplier  DOUBLE,
        spy_sma200   DOUBLE,
        spy_ftd_state VARCHAR,
        ftd_active   BOOLEAN
    )
    """,
    # Issue #189: the retrospective's own record. Until now a `failure_class`
    # lived only in `reports/retro/<as_of>/retro_report.md`, which is
    # gitignored -- so the L2 qualitative gate ("the same failure_class five
    # times across the last three retrospectives") could never be evaluated
    # from anything but a human's memory. One row per ingested retrospective;
    # `input_digest` binds the session to the dossier it answered, so a session
    # can be traced back to the exact evidence set it read.
    """
    CREATE TABLE IF NOT EXISTS retro_sessions (
        retro_as_of     DATE PRIMARY KEY,
        window_start    DATE NOT NULL,
        input_digest    VARCHAR NOT NULL,
        generated_at    TIMESTAMPTZ NOT NULL,
        outcome_count   INTEGER NOT NULL,
        proposal_count  INTEGER NOT NULL
    )
    """,
    # Issue #189: one verified narration per surprise symbol. `run_id`/`symbol`
    # are resolved from the *exported* dossier rather than echoed back from the
    # skill's answer (AGENTS.md: code-owned metadata is never taken from an
    # untrusted result), which is also what makes this table joinable to
    # `verdicts`/`verdict_outcomes`. `failure_class` carries no CHECK
    # constraint even though the vocabulary is closed: `retro/schemas.py`'s
    # `FailureClass` literal is the enforcement point, and pinning a five-value
    # enum into DDL would make an added class a schema migration.
    """
    CREATE TABLE IF NOT EXISTS retro_narrations (
        retro_as_of        DATE NOT NULL,
        surprise_id        VARCHAR NOT NULL,
        run_id             UUID NOT NULL,
        symbol             VARCHAR NOT NULL,
        failure_class      VARCHAR NOT NULL,
        narrative          VARCHAR NOT NULL,
        evidence_refs_json JSON NOT NULL,
        PRIMARY KEY (retro_as_of, surprise_id)
    )
    """,
    # Issue #189: what a `runs.config_hash` actually stood for. The hash alone
    # is a one-way fingerprint -- "did the numbers move because the config
    # moved" was unanswerable the moment `config/settings.yaml` was edited,
    # and no amount of later analysis can recover a value that was never
    # written down.
    #
    # `config_hash` is `runs.config_hash` verbatim (the full effective-run
    # fingerprint, settings + selected strategy), so the join to `runs` needs
    # no new column there. `sections_json` holds only the eight
    # proposal-relevant settings sections (`config.CONFIG_SNAPSHOT_SECTIONS`),
    # and `snapshot_hash` is their digest: two runs whose only difference is a
    # notification or schedule edit share a `snapshot_hash` while their
    # `config_hash` differs, which is what stops an unrelated edit from
    # splitting a comparison window in two.
    """
    CREATE TABLE IF NOT EXISTS config_versions (
        config_hash         VARCHAR PRIMARY KEY,
        first_seen_run_date DATE NOT NULL,
        snapshot_hash       VARCHAR NOT NULL,
        sections_json       JSON NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ftd_state_history (
        run_id        UUID NOT NULL,
        symbol        VARCHAR NOT NULL CHECK (symbol IN ('SPY', 'QQQ')),
        sequence      INTEGER NOT NULL,
        as_of         DATE NOT NULL,
        state         VARCHAR NOT NULL,
        day_number    INTEGER,
        quality_score INTEGER,
        PRIMARY KEY (run_id, symbol, sequence)
    )
    """,
)

# P1-03: additive columns for a database created before this change. See the
# module docstring for why these are unconstrained (no CHECK/NOT NULL).
ALTER_SCHEMA_STATEMENTS = (
    # Issue #325: legacy risk rows gain the planned limit price lazily. DuckDB
    # cannot add a constrained column here, so application validation remains
    # the only guarantee for rows written after the migration.
    "ALTER TABLE risk_assessments ADD COLUMN IF NOT EXISTS limit_price DOUBLE",
    "ALTER TABLE risk_assessments ADD COLUMN IF NOT EXISTS shares_by_risk BIGINT",
    "ALTER TABLE risk_assessments "
    "ADD COLUMN IF NOT EXISTS shares_by_position_cap BIGINT",
    "ALTER TABLE risk_assessments ADD COLUMN IF NOT EXISTS binding_constraint VARCHAR",
    "ALTER TABLE risk_assessments "
    "ADD COLUMN IF NOT EXISTS sizing_warnings_json JSON DEFAULT '[]'",
    # I57: old databases gain the run-reconstruction metadata lazily. DuckDB
    # cannot add a NOT NULL JSON column, so legacy rows retain NULL while new
    # runs always write a canonical JSON object through `StateStore.start_run`.
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS metadata_json JSON",
    # verdict tracking: `no_trade` proceeds are now tracked too (flagged, not
    # excluded), so a database created before this change gains the column
    # lazily. Unconstrained at the DB level for the same reason as the other
    # entries above (DuckDB rejects `ADD COLUMN` with an inline `NOT NULL`);
    # `tracking/update.py` always writes an explicit `True`/`False`, never
    # `NULL`, so application code is the sole enforcement point for rows
    # added to a pre-existing `verdict_positions` table via this path. Every
    # row already in such a table was necessarily opened while `no_trade`
    # verdicts were still excluded, so backfilling `FALSE` is not a guess --
    # it restates a fact those rows already had. Both statements are
    # idempotent.
    "ALTER TABLE verdict_positions ADD COLUMN IF NOT EXISTS no_trade BOOLEAN",
    "UPDATE verdict_positions SET no_trade = FALSE WHERE no_trade IS NULL",
    # P8-123: Finnhub's `related`/`category` are now persisted for ticker-
    # collision observation instead of staying collection-time-only. No
    # backfill for rows written before this change -- both columns stay NULL
    # on them (re-fetching ~610k historical rows is not worth it).
    "ALTER TABLE text_items ADD COLUMN IF NOT EXISTS related_symbols VARCHAR",
    "ALTER TABLE text_items ADD COLUMN IF NOT EXISTS category VARCHAR",
    # Issue #157: an 8-K exhibit cut off while being collected is invisible to
    # `is_truncated`, so it is recorded separately. Explicitly *not* backfilled
    # to FALSE, unlike `positions.exit_reason` / `verdict_positions.no_trade`:
    # those restated a fact the existing rows already had, whereas nothing in
    # this table says whether a pre-existing row's filing carried the marker.
    # NULL therefore keeps meaning "not recorded" rather than "no exhibit was
    # cut", and readers must treat the two differently.
    "ALTER TABLE analysis_source_coverage "
    "ADD COLUMN IF NOT EXISTS exhibit_truncated BOOLEAN",
    # Issue #154: the news-supply measurement each verdict was made under, so
    # the `sufficient` threshold can be checked against outcomes. Not
    # backfilled, for the same reason as `exhibit_truncated` above: nothing in
    # an existing row says how much company-specific news that run supplied,
    # and a backfilled 0 would read as a measured `none`. A re-`collect` of an
    # archive that does carry the field fills the columns in, because
    # `replace_run_verdicts` replaces the run wholesale.
    "ALTER TABLE verdicts ADD COLUMN IF NOT EXISTS news_supply_collected_items INTEGER",
    "ALTER TABLE verdicts ADD COLUMN IF NOT EXISTS news_supply_exported_items INTEGER",
    "ALTER TABLE verdicts "
    "ADD COLUMN IF NOT EXISTS news_supply_symbol_mention_items INTEGER",
    "ALTER TABLE verdicts ADD COLUMN IF NOT EXISTS news_supply_level VARCHAR",
    # Issue #190: `skip` verdicts are now shadow-tracked alongside `proceed`
    # ones, so a position has to say which side it belongs to. Backfilled to
    # 'proceed', on the same reasoning as `verdict_positions.no_trade` above:
    # every row already in the table was necessarily opened while only
    # `proceed` verdicts were tracked, so this restates a fact those rows
    # already carry rather than guessing at one. Application code still reads
    # NULL as 'proceed' (`tracking_records._position`), so a database that
    # somehow skips the backfill still reads correctly. Both idempotent.
    "ALTER TABLE verdict_positions ADD COLUMN IF NOT EXISTS recommendation VARCHAR",
    "UPDATE verdict_positions SET recommendation = 'proceed' "
    "WHERE recommendation IS NULL",
    # Issue #343: retain the entry-time holding plan for display calculations.
    # The replay deliberately continues to use the current trade-plan config
    # in `_advance`; this value is not an exit-rule override.
    "ALTER TABLE verdict_positions ADD COLUMN IF NOT EXISTS max_hold_days INTEGER",
    "UPDATE verdict_positions SET max_hold_days = 25 WHERE max_hold_days IS NULL",
    # Issue #190: the benchmark's return over each classification's own span,
    # for the excess-return separation metric. Explicitly *not* backfilled --
    # nothing in an existing row says what the benchmark did over its span, and
    # a backfilled 0 would read as a measured flat market. A re-`evaluate`
    # fills it in, because `replace_verdict_outcomes` replaces the slice
    # wholesale.
    "ALTER TABLE verdict_outcomes ADD COLUMN IF NOT EXISTS benchmark_return_pct DOUBLE",
    # Issue #192: the ranking key and its components as real columns. The
    # score side IS backfilled, unlike most entries above: `metrics_json`
    # already holds `score`/`score_*` for every row ever written, so the
    # UPDATE restates a fact those rows carry rather than inventing one, a
    # value that was merely in the wrong shape. `execution_state`/
    # `execution_distance`
    # are NOT backfilled and cannot be: they were never persisted anywhere,
    # and recomputing them would mean replaying that day's execution config
    # against that day's bars. NULL there is a documented one-way cut,
    # meaning "not recorded" -- never the `UNKNOWN` state, which is a
    # measured "distance not computable".
    #
    # The backfill's `WHERE score IS NULL` is both the idempotence guard and
    # the correct predicate: `_score_rows` writes the composite and its four
    # components together, so a row missing one is missing all five, and a
    # row written after this change always has them. It stays a cheap no-op
    # scan on every subsequent start.
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS score DOUBLE",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS score_rsi_pullback DOUBLE",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS score_trend_quality DOUBLE",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS score_liquidity DOUBLE",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS score_atr_pct DOUBLE",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS execution_state VARCHAR",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS execution_distance DOUBLE",
    """
    UPDATE candidates SET
        score               = CAST(metrics_json->>'score' AS DOUBLE),
        score_rsi_pullback  = CAST(metrics_json->>'score_rsi_pullback' AS DOUBLE),
        score_trend_quality = CAST(metrics_json->>'score_trend_quality' AS DOUBLE),
        score_liquidity     = CAST(metrics_json->>'score_liquidity' AS DOUBLE),
        score_atr_pct       = CAST(metrics_json->>'score_atr_pct' AS DOUBLE)
    WHERE score IS NULL
    """,
    # Issue #251: the strategy-specific ranking components, on both halves of
    # one ranking. Deliberately NOT backfilled, unlike the four columns above:
    # a row written before this change has no `score_pivot_proximity` in its
    # `metrics_json` either, because the component did not exist when the run
    # scored it. NULL here therefore means "not recorded" -- which is what the
    # `v_*` views' JSON fallback already resolves to for those rows -- and a
    # backfilled 0.0 would instead read as a measured contribution of nothing.
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS score_pivot_proximity DOUBLE",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS score_rs_percentile DOUBLE",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS score_criteria_met DOUBLE",
    "ALTER TABLE screening_truncations "
    "ADD COLUMN IF NOT EXISTS score_pivot_proximity DOUBLE",
    "ALTER TABLE screening_truncations "
    "ADD COLUMN IF NOT EXISTS score_rs_percentile DOUBLE",
    "ALTER TABLE screening_truncations "
    "ADD COLUMN IF NOT EXISTS score_criteria_met DOUBLE",
    # Issue #192: the distribution sub-windows and gate inputs, backfilled
    # from `detail_json` on the same "restating a recorded fact" reasoning as
    # the candidate scores above. The guard is `dd15_spy IS NULL` rather than
    # one of the gate inputs: `spy_close`/`spy_sma200`/`vix_close` are legitimately
    # NULL whenever the gate could not be evaluated, so guarding on them would
    # re-run the UPDATE forever, whereas `d15` is always present in a
    # `DistributionResult`.
    "ALTER TABLE regime_snapshots ADD COLUMN IF NOT EXISTS dd15_spy DOUBLE",
    "ALTER TABLE regime_snapshots ADD COLUMN IF NOT EXISTS dd5_spy DOUBLE",
    "ALTER TABLE regime_snapshots ADD COLUMN IF NOT EXISTS dd15_qqq DOUBLE",
    "ALTER TABLE regime_snapshots ADD COLUMN IF NOT EXISTS dd5_qqq DOUBLE",
    "ALTER TABLE regime_snapshots ADD COLUMN IF NOT EXISTS spy_close DOUBLE",
    "ALTER TABLE regime_snapshots ADD COLUMN IF NOT EXISTS spy_ema DOUBLE",
    "ALTER TABLE regime_snapshots ADD COLUMN IF NOT EXISTS vix_close DOUBLE",
    "ALTER TABLE regime_snapshots ADD COLUMN IF NOT EXISTS spy_sma200 DOUBLE",
    "ALTER TABLE regime_snapshots ADD COLUMN IF NOT EXISTS spy_ftd_state VARCHAR",
    """
    UPDATE regime_snapshots SET
        dd15_spy  = CAST(detail_json->'spy'->>'d15' AS DOUBLE),
        dd5_spy   = CAST(detail_json->'spy'->>'d5' AS DOUBLE),
        dd15_qqq  = CAST(detail_json->'qqq'->>'d15' AS DOUBLE),
        dd5_qqq   = CAST(detail_json->'qqq'->>'d5' AS DOUBLE),
        spy_close = CAST(detail_json->'gate_inputs'->>'spy_close' AS DOUBLE),
        spy_ema   = CAST(detail_json->'gate_inputs'->>'spy_ema' AS DOUBLE),
        vix_close = CAST(detail_json->'gate_inputs'->>'vix_close' AS DOUBLE)
    WHERE dd15_spy IS NULL
    """,
    # Issue #192: the exposure decision's inputs, same backfill reasoning.
    # Guarded on `reduce_only_risk_multiplier`, which `determine_exposure`
    # always records.
    "ALTER TABLE exposure_decisions ADD COLUMN IF NOT EXISTS gate_verdict VARCHAR",
    "ALTER TABLE exposure_decisions ADD COLUMN IF NOT EXISTS dd_level VARCHAR",
    "ALTER TABLE exposure_decisions "
    "ADD COLUMN IF NOT EXISTS is_conservatively_downgraded BOOLEAN",
    "ALTER TABLE exposure_decisions "
    "ADD COLUMN IF NOT EXISTS reduce_only_risk_multiplier DOUBLE",
    "ALTER TABLE exposure_decisions ADD COLUMN IF NOT EXISTS spy_sma200 DOUBLE",
    "ALTER TABLE exposure_decisions ADD COLUMN IF NOT EXISTS spy_ftd_state VARCHAR",
    "ALTER TABLE exposure_decisions ADD COLUMN IF NOT EXISTS ftd_active BOOLEAN",
    """
    UPDATE exposure_decisions SET
        gate_verdict = detail_json->>'gate',
        dd_level     = detail_json->>'dd_level',
        is_conservatively_downgraded =
            CAST(detail_json->>'conservatively_downgraded' AS BOOLEAN),
        reduce_only_risk_multiplier =
            CAST(detail_json->>'reduce_only_risk_multiplier' AS DOUBLE)
    WHERE reduce_only_risk_multiplier IS NULL
    """,
)

# Read-only analysis views for `swing_copilot.research` and ad-hoc SQL
# (notebook / duckdb CLI). `CREATE OR REPLACE` makes every definition
# self-migrating: `init_schema()` re-runs them on each start, so editing a
# view here needs no ALTER bookkeeping. Views are catalog-persisted, which is
# what lets a `read_only=True` connection (no DDL allowed) still query them.
# Ordering matters: later views reference earlier ones.
#
# JSON extraction note: `metrics_json->>'key'` yields VARCHAR (NULL when the
# key is absent, e.g. rows written before a component existed), so every
# numeric field is CAST explicitly. The key names mirror
# `screening/pipeline.py`'s score components and `ranking_metrics`.
ANALYSIS_VIEW_STATEMENTS = (
    # The single blessed as-of sector resolution (`snapshot_date <= run_date`,
    # inclusive), so notebooks never re-implement the point-in-time rule.
    """
    CREATE OR REPLACE VIEW v_symbol_sector_asof AS
    SELECT
        r.run_id,
        um.symbol,
        um.gics_sector,
        um.company_name
    FROM runs r
    JOIN universe_membership um
      ON um.snapshot_date = (
          SELECT max(u2.snapshot_date)
          FROM universe_membership u2
          WHERE u2.snapshot_date <= r.run_date
            AND u2.symbol = um.symbol
      )
    """,
    """
    CREATE OR REPLACE VIEW v_candidates AS
    SELECT
        r.run_date,
        r.mode,
        r.status AS run_status,
        r.config_hash,
        c.run_id,
        c.symbol,
        c.strategy_key,
        c.rank,
        c.signal_names,
        -- Issue #192: the promoted column first, the JSON extraction as the
        -- fallback. The `ALTER_SCHEMA_STATEMENTS` backfill normally fills the
        -- columns in, so the fallback matters for a database whose migration
        -- has not run yet (a `read_only=True` research connection cannot run
        -- DDL, so it reads whatever shape the file is in) and for any row
        -- whose `metrics_json` predates a component.
        COALESCE(c.score, CAST(c.metrics_json->>'score' AS DOUBLE))            AS score,
        COALESCE(
            c.score_rsi_pullback,
            CAST(c.metrics_json->>'score_rsi_pullback' AS DOUBLE)
        ) AS score_rsi_pullback,
        COALESCE(
            c.score_trend_quality,
            CAST(c.metrics_json->>'score_trend_quality' AS DOUBLE)
        ) AS score_trend_quality,
        COALESCE(
            c.score_liquidity,
            CAST(c.metrics_json->>'score_liquidity' AS DOUBLE)
        ) AS score_liquidity,
        COALESCE(
            c.score_atr_pct,
            CAST(c.metrics_json->>'score_atr_pct' AS DOUBLE)
        ) AS score_atr_pct,
        COALESCE(
            c.score_pivot_proximity,
            CAST(c.metrics_json->>'score_pivot_proximity' AS DOUBLE)
        ) AS score_pivot_proximity,
        COALESCE(
            c.score_rs_percentile,
            CAST(c.metrics_json->>'score_rs_percentile' AS DOUBLE)
        ) AS score_rs_percentile,
        COALESCE(
            c.score_criteria_met,
            CAST(c.metrics_json->>'score_criteria_met' AS DOUBLE)
        ) AS score_criteria_met,
        -- No JSON fallback: these never reached `metrics_json` either, so a
        -- row from before the columns existed has NULL and must read as
        -- "not recorded" rather than as the `UNKNOWN` execution state.
        c.execution_state,
        c.execution_distance,
        CAST(c.metrics_json->>'rsi14' AS DOUBLE)               AS rsi14,
        CAST(c.metrics_json->>'sma50' AS DOUBLE)               AS sma50,
        CAST(c.metrics_json->>'sma200' AS DOUBLE)              AS sma200,
        CAST(c.metrics_json->>'atr14' AS DOUBLE)               AS atr14,
        CAST(c.metrics_json->>'close' AS DOUBLE)               AS close,
        CAST(c.metrics_json->>'avg_volume' AS DOUBLE)          AS avg_volume,
        c.metrics_json
    FROM candidates c
    JOIN runs r ON r.run_id = c.run_id
    """,
    # Issue #188: the near-misses, in the same column shape as `v_candidates`
    # so "rank 1-5 vs rank 6-10" is a UNION ALL away instead of a reshape.
    """
    CREATE OR REPLACE VIEW v_truncated_candidates AS
    SELECT
        r.run_date,
        r.mode,
        r.status AS run_status,
        r.config_hash,
        t.run_id,
        t.symbol,
        t.strategy_key,
        t.rank,
        t.score,
        t.score_rsi_pullback,
        t.score_trend_quality,
        t.score_liquidity,
        t.score_atr_pct,
        t.score_pivot_proximity,
        t.score_rs_percentile,
        t.score_criteria_met,
        t.execution_state,
        t.execution_distance,
        t.as_of
    FROM screening_truncations t
    JOIN runs r ON r.run_id = t.run_id
    """,
    # Issue #188: one row per (screening decision, matured horizon), with the
    # ranking evidence attached where the symbol had any. Both rank/score legs
    # are pre-aggregated subqueries rather than plain joins: `universe_
    # forward_returns` has no `strategy_key` (a decision about a symbol on a
    # day, not about a strategy's ranking of it), so joining the
    # strategy-keyed tables directly would fan a single decision out into one
    # row per strategy the run happened to score.
    """
    CREATE OR REPLACE VIEW v_universe_forward_returns AS
    SELECT
        r.run_date,
        r.mode,
        r.config_hash,
        u.run_id,
        u.symbol,
        u.horizon_days,
        u.as_of,
        u.outcome_class,
        u.reason_code,
        u.forward_return_pct,
        COALESCE(c.rank, t.rank)   AS rank,
        COALESCE(c.score, t.score) AS score,
        -- Issue #192: makes "how did each execution state's symbols actually
        -- do" a one-line groupby. `arg_min(state, rank)` takes the
        -- best-ranked *recorded* state (it skips NULL arguments), so a
        -- pre-#192 candidate row does not mask a strategy that does have one.
        COALESCE(c.execution_state, t.execution_state) AS execution_state,
        s.gics_sector
    FROM universe_forward_returns u
    JOIN runs r ON r.run_id = u.run_id
    LEFT JOIN (
        SELECT
            run_id,
            symbol,
            min(rank)  AS rank,
            max(score) AS score,
            arg_min(execution_state, rank) AS execution_state
        FROM v_candidates
        GROUP BY run_id, symbol
    ) c ON c.run_id = u.run_id AND c.symbol = u.symbol
    LEFT JOIN (
        SELECT
            run_id,
            symbol,
            min(rank)  AS rank,
            max(score) AS score,
            arg_min(execution_state, rank) AS execution_state
        FROM screening_truncations
        GROUP BY run_id, symbol
    ) t ON t.run_id = u.run_id AND t.symbol = u.symbol
    LEFT JOIN v_symbol_sector_asof s
      ON s.run_id = u.run_id AND s.symbol = u.symbol
    """,
    # Issue #192: the read path the legacy `signals` table never had. Only
    # `signal_hits` is read here: `signals` is keyed by `run_date`, so a
    # `dry_run` and a `live` run of the same date collided in it and its rows
    # cannot be attributed to a run at all.
    """
    CREATE OR REPLACE VIEW v_signal_hits AS
    SELECT
        r.run_date,
        r.mode,
        h.run_id,
        h.symbol,
        h.strategy_key,
        h.signal_name,
        h.strength,
        h.metrics_json
    FROM signal_hits h
    JOIN runs r ON r.run_id = h.run_id
    """,
    # Issue #192: one row per individual verdict reason, joined to the verdict
    # it belongs to, so "how did symbols whose reasons cited no source
    # perform" is a filter rather than a JSON walk.
    """
    CREATE OR REPLACE VIEW v_verdict_reasons AS
    SELECT
        r.run_date,
        r.mode,
        vr.run_id,
        vr.symbol,
        v.strategy_key,
        v.recommendation,
        v.no_trade,
        vr.reason_index,
        vr.text,
        vr.basis,
        vr.source_id_count
    FROM verdict_reasons vr
    JOIN runs r ON r.run_id = vr.run_id
    LEFT JOIN verdicts v ON v.run_id = vr.run_id AND v.symbol = vr.symbol
    """,
    """
    CREATE OR REPLACE VIEW v_tracked_positions AS
    SELECT
        r.run_date,
        p.run_id,
        p.symbol,
        p.strategy_key,
        -- Issue #190: the position's own side, falling back to the verdict's
        -- for a row written before the column existed (NULL == 'proceed').
        COALESCE(p.recommendation, v.recommendation) AS recommendation,
        p.no_trade,
        p.entry_date,
        p.entry_price,
        p.stop_price,
        p.days_held,
        p.max_hold_days,
        p.status,
        p.exit_date,
        p.exit_price,
        p.exit_reason,
        p.realized_return_pct,
        p.last_marked_date,
        latest_mark.as_of_date AS last_mark_date,
        latest_mark.close AS last_close,
        latest_mark.unrealized_return_pct
    FROM verdict_positions p
    JOIN runs r ON r.run_id = p.run_id
    LEFT JOIN verdicts v ON v.run_id = p.run_id AND v.symbol = p.symbol
    LEFT JOIN (
        SELECT run_id, symbol, as_of_date, close, unrealized_return_pct
        FROM verdict_position_marks
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY run_id, symbol ORDER BY as_of_date DESC
        ) = 1
    ) latest_mark
      ON latest_mark.run_id = p.run_id AND latest_mark.symbol = p.symbol
    """,
    # One row per (verdict, matured horizon); a verdict with no matured
    # outcome yet keeps a single row with NULL horizon columns, so the view
    # also serves "what did we decide today" queries. All joins besides
    # `runs` are LEFT: a scorecard row must survive any single missing leg
    # (no candidate row for a strategy mismatch, no regime snapshot on a
    # degraded run, ...).
    """
    CREATE OR REPLACE VIEW v_verdict_scorecard AS
    SELECT
        r.run_date,
        r.mode,
        r.config_hash,
        v.run_id,
        v.symbol,
        v.strategy_key,
        v.recommendation,
        v.no_trade,
        v.news_supply_level,
        o.horizon_days,
        o.forward_return_pct,
        o.classification,
        c.rank,
        c.score,
        c.score_rsi_pullback,
        c.score_trend_quality,
        c.score_liquidity,
        c.score_atr_pct,
        c.score_pivot_proximity,
        c.score_rs_percentile,
        c.score_criteria_met,
        c.execution_state,
        c.execution_distance,
        c.rsi14,
        c.atr14,
        c.close,
        c.avg_volume,
        ra.status AS risk_status,
        ra.binding_constraint,
        g.gate_verdict,
        g.dd_level,
        g.dd_count_spy,
        g.dd_count_qqq,
        g.dd15_spy,
        g.dd5_spy,
        g.vix_close,
        p.status AS position_status,
        p.exit_reason,
        p.realized_return_pct,
        p.days_held,
        s.gics_sector
    FROM verdicts v
    JOIN runs r ON r.run_id = v.run_id
    LEFT JOIN verdict_outcomes o
      ON o.run_id = v.run_id AND o.symbol = v.symbol
    LEFT JOIN v_candidates c
      ON c.run_id = v.run_id
     AND c.symbol = v.symbol
     AND c.strategy_key = v.strategy_key
    LEFT JOIN risk_assessments ra
      ON ra.run_id = v.run_id AND ra.symbol = v.symbol
    LEFT JOIN regime_snapshots g ON g.run_id = v.run_id
    LEFT JOIN verdict_positions p
      ON p.run_id = v.run_id AND p.symbol = v.symbol
    LEFT JOIN v_symbol_sector_asof s
      ON s.run_id = v.run_id AND s.symbol = v.symbol
    """,
    # Issue #189: one verified narration per row, joined to the run it re-read
    # and to that run's verdict, so "which failure_class keeps repeating, and
    # on which side of the verdict" is a GROUP BY rather than a walk through
    # gitignored markdown. Every join but `retro_narrations` itself is LEFT:
    # a narration must stay readable even if its run's verdict was later
    # replaced by a re-`collect` that no longer analyzes the symbol.
    """
    CREATE OR REPLACE VIEW v_retro_narrations AS
    SELECT
        n.retro_as_of,
        s.window_start,
        n.surprise_id,
        n.run_id,
        r.run_date,
        n.symbol,
        n.failure_class,
        n.narrative,
        n.evidence_refs_json,
        v.recommendation,
        v.no_trade
    FROM retro_narrations n
    LEFT JOIN retro_sessions s ON s.retro_as_of = n.retro_as_of
    LEFT JOIN runs r ON r.run_id = n.run_id
    LEFT JOIN verdicts v ON v.run_id = n.run_id AND v.symbol = n.symbol
    """,
    # Issue #189: every run with the settings it actually ran under. A LEFT
    # join on purpose -- runs written before `config_versions` existed have no
    # ledger row, and NULL there means "the values were never recorded", which
    # readers must not confuse with "the configuration was empty".
    """
    CREATE OR REPLACE VIEW v_run_configs AS
    SELECT
        r.run_id,
        r.run_date,
        r.mode,
        r.status AS run_status,
        r.config_hash,
        c.snapshot_hash,
        c.first_seen_run_date,
        c.sections_json
    FROM runs r
    LEFT JOIN config_versions c ON c.config_hash = r.config_hash
    """,
)
