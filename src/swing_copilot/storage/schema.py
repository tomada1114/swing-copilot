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
"""

from __future__ import annotations

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
    """
    CREATE TABLE IF NOT EXISTS candidates (
        run_id         UUID NOT NULL,
        symbol         VARCHAR NOT NULL,
        strategy_key   VARCHAR NOT NULL,
        rank           INTEGER NOT NULL,
        signal_names   VARCHAR[] NOT NULL,
        metrics_json   JSON NOT NULL,
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
    """
    CREATE TABLE IF NOT EXISTS risk_assessments (
        run_id          UUID NOT NULL,
        symbol          VARCHAR NOT NULL,
        status          VARCHAR NOT NULL
            CHECK (status IN ('approved','rejected','not_calculable')),
        max_shares      BIGINT,
        entry_price     DOUBLE,
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
    CREATE TABLE IF NOT EXISTS positions (
        position_id   UUID PRIMARY KEY,
        symbol        VARCHAR NOT NULL,
        is_paper      BOOLEAN NOT NULL DEFAULT 1,
        entry_date    DATE NOT NULL,
        entry_price   DOUBLE NOT NULL,
        shares        BIGINT NOT NULL,
        stop_price    DOUBLE,
        status        VARCHAR NOT NULL CHECK(status IN ('open','closed')),
        close_date    DATE,
        close_at      TIMESTAMPTZ,
        close_price   DOUBLE,
        exit_reason   VARCHAR CHECK (exit_reason IS NULL OR exit_reason IN (
            'stop_loss','target','time_stop','manual','other','unknown'
        )),
        created_at    TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS position_excursions (
        position_id    UUID NOT NULL,
        as_of_date     DATE NOT NULL,
        mae_per_share  DOUBLE,
        mfe_per_share  DOUBLE,
        data_quality   VARCHAR NOT NULL
            CHECK(data_quality IN ('OK','MISSING_BAR')),
        created_at     TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (position_id, as_of_date)
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
    CREATE TABLE IF NOT EXISTS trades_journal (
        journal_id          UUID PRIMARY KEY,
        run_id              UUID NOT NULL,
        symbol              VARCHAR NOT NULL,
        strategy_key        VARCHAR NOT NULL,
        position_id         UUID,
        decision            VARCHAR NOT NULL CHECK(decision IN ('followed','ignored','modified')),
        reason_memo         VARCHAR,
        virtual_fill_price  DOUBLE,
        created_at          TIMESTAMPTZ NOT NULL,
        UNIQUE (run_id, symbol, strategy_key)
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
    # Verdict tracking: the virtual position a verdict implies, the daily marks
    # that follow it, and the human's notes about it. Deliberately separate
    # from `positions` (the FR-11/CON-04 paper-trading gate, which records what
    # a *human* decided to hold) and from `verdict_outcomes` (a two-point 5/20
    # session classification): this layer replays the backtest's own exit rules
    # forward, one trading day at a time.
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
    """
    CREATE TABLE IF NOT EXISTS verdict_position_notes (
        run_id     UUID NOT NULL,
        symbol     VARCHAR NOT NULL,
        note_date  DATE NOT NULL,
        note       VARCHAR NOT NULL,
        PRIMARY KEY (run_id, symbol, note_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS regime_snapshots (
        run_id          UUID PRIMARY KEY,
        as_of           DATE NOT NULL,
        gate_verdict    VARCHAR NOT NULL,
        dd_count_spy    DOUBLE NOT NULL,
        dd_count_qqq    DOUBLE NOT NULL,
        dd_level        VARCHAR NOT NULL,
        data_quality    VARCHAR NOT NULL,
        detail_json     JSON NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS exposure_decisions (
        run_id       UUID PRIMARY KEY,
        verdict      VARCHAR NOT NULL,
        data_quality VARCHAR NOT NULL,
        detail_json  JSON NOT NULL
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
    "ALTER TABLE risk_assessments ADD COLUMN IF NOT EXISTS shares_by_risk BIGINT",
    "ALTER TABLE risk_assessments "
    "ADD COLUMN IF NOT EXISTS shares_by_position_cap BIGINT",
    "ALTER TABLE risk_assessments ADD COLUMN IF NOT EXISTS binding_constraint VARCHAR",
    "ALTER TABLE risk_assessments "
    "ADD COLUMN IF NOT EXISTS sizing_warnings_json JSON DEFAULT '[]'",
    # P1-06: additive column for a database created before this change.
    # Unconstrained at the DB level for the same reason as above (DuckDB
    # rejects ADD COLUMN with an inline CHECK); `close_position()` and the
    # backfill below are the sole enforcement points for rows added via
    # this path. Deliberately no column-level DEFAULT: a plain DEFAULT would
    # also stamp 'unknown' onto still-open positions (wrong — they have no
    # exit reason at all and must stay NULL), so the backfill below is
    # scoped to already-closed rows only. Both statements are idempotent —
    # safe to re-run on every startup against a fresh or already-upgraded
    # database.
    "ALTER TABLE positions ADD COLUMN IF NOT EXISTS exit_reason VARCHAR",
    "ALTER TABLE positions ADD COLUMN IF NOT EXISTS close_at TIMESTAMPTZ",
    "UPDATE positions SET exit_reason = 'unknown' "
    "WHERE status = 'closed' AND exit_reason IS NULL",
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
    # it restates a fact those rows already had, the same way P1-06 backfills
    # `positions.exit_reason`. Both statements are idempotent.
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
    # Issue #190: the benchmark's return over each classification's own span,
    # for the excess-return separation metric. Explicitly *not* backfilled --
    # nothing in an existing row says what the benchmark did over its span, and
    # a backfilled 0 would read as a measured flat market. A re-`evaluate`
    # fills it in, because `replace_verdict_outcomes` replaces the slice
    # wholesale.
    "ALTER TABLE verdict_outcomes ADD COLUMN IF NOT EXISTS benchmark_return_pct DOUBLE",
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
        CAST(c.metrics_json->>'score' AS DOUBLE)               AS score,
        CAST(c.metrics_json->>'score_rsi_pullback' AS DOUBLE)  AS score_rsi_pullback,
        CAST(c.metrics_json->>'score_trend_quality' AS DOUBLE) AS score_trend_quality,
        CAST(c.metrics_json->>'score_liquidity' AS DOUBLE)     AS score_liquidity,
        CAST(c.metrics_json->>'score_atr_pct' AS DOUBLE)       AS score_atr_pct,
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
        p.status,
        p.exit_date,
        p.exit_price,
        p.exit_reason,
        p.realized_return_pct,
        p.last_marked_date
    FROM verdict_positions p
    JOIN runs r ON r.run_id = p.run_id
    LEFT JOIN verdicts v ON v.run_id = p.run_id AND v.symbol = p.symbol
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
)
