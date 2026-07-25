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
            'FILTER_LOW_LIQUIDITY','SIGNAL_TREND_NOT_MET','SIGNAL_RSI_NOT_MET','DATA_INSUFFICIENT_HISTORY'
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
        close_price   DOUBLE,
        exit_reason   VARCHAR CHECK (exit_reason IS NULL OR exit_reason IN (
            'stop_loss','target','time_stop','manual','other','unknown'
        )),
        created_at    TIMESTAMPTZ NOT NULL
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
        source_id      VARCHAR PRIMARY KEY,
        symbol         VARCHAR,
        source_type    VARCHAR NOT NULL,
        published_at   TIMESTAMPTZ NOT NULL,
        title          VARCHAR,
        source_url     VARCHAR NOT NULL,
        content_text   VARCHAR NOT NULL,
        fetched_at     TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_calls (
        call_id         UUID PRIMARY KEY,
        run_id          UUID NOT NULL,
        model           VARCHAR NOT NULL,
        schema_name     VARCHAR NOT NULL,
        schema_version  INTEGER NOT NULL,
        prompt_text     VARCHAR NOT NULL,
        prompt_hash     VARCHAR NOT NULL,
        source_ids      VARCHAR[] NOT NULL,
        status          VARCHAR NOT NULL CHECK(status IN ('success','failed','budget_skipped')),
        input_tokens    INTEGER NOT NULL,
        output_tokens   INTEGER NOT NULL,
        input_price_per_mtok   DOUBLE NOT NULL,
        output_price_per_mtok  DOUBLE NOT NULL,
        cost_usd        DOUBLE NOT NULL,
        response_json   JSON,
        error_detail    VARCHAR,
        created_at      TIMESTAMPTZ NOT NULL
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
    "UPDATE positions SET exit_reason = 'unknown' "
    "WHERE status = 'closed' AND exit_reason IS NULL",
)
