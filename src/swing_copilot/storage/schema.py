"""DDL for every table `StateStore` owns (`docs/04_detailed_design.md` 4.2).

Split out from `state_store.py` to keep that module under the project's
300-line guideline; this file is schema only, no behavior.
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
        created_at    TIMESTAMPTZ NOT NULL
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
)
