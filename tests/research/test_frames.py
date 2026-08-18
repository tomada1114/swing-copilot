"""Contract tests for the read-only research surface (`swing_copilot.research`).

The views under test (`v_verdict_scorecard`, `v_candidates`,
`v_truncated_candidates`, `v_universe_forward_returns`,
`v_tracked_positions`, `v_symbol_sector_asof`) live in `storage/schema.py`;
these tests exercise them through the public DataFrame accessors, including
the as-of sector boundary, the immature-verdict row, read-only enforcement,
and the `ensure_views` recovery path for a pre-view database.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from uuid import uuid4

import duckdb
import pandas as pd
import pytest

from swing_copilot import research
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore

RUN_DATE = date(2027, 2, 1)


def _insert_run(store, run_id, run_date=RUN_DATE, status="success"):
    with store._database.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
            "started_at) VALUES (?, ?, 'live', 'cfg', ?, ?)",
            [str(run_id), run_date, status, datetime(2027, 2, 1, 15, 0, tzinfo=UTC)],
        )


def _insert_universe(store, snapshot_date, sector="Information Technology"):
    with store._database.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO universe_membership VALUES (?, 'AAPL', 'AAPL', "
            "'Apple Inc.', ?, 'test')",
            [snapshot_date, sector],
        )


def _insert_candidate(store, run_id, metrics_json):
    with store._database.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO candidates VALUES (?, 'AAPL', 'default', 1, ['trend_sma'], ?)",
            [str(run_id), metrics_json],
        )


def _insert_verdict(store, run_id, recommendation="proceed"):
    with store._database.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO verdicts (run_id, symbol, as_of, strategy_key, "
            "recommendation, reasons_json, no_trade) VALUES (?, 'AAPL', ?, "
            "'default', ?, '[]', FALSE)",
            [str(run_id), RUN_DATE, recommendation],
        )


class TestScorecard:
    def test_joins_every_leg_onto_one_verdict_row(self, state_store, tmp_path):
        run_id = uuid4()
        _insert_universe(state_store, date(2027, 1, 15))
        _insert_run(state_store, run_id)
        _insert_candidate(
            state_store,
            run_id,
            '{"score": 0.7, "score_rsi_pullback": 0.4, "rsi14": 33.0,'
            ' "close": 190.5, "avg_volume": 10000000.0}',
        )
        _insert_verdict(state_store, run_id)
        with state_store._database.connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO verdict_outcomes (run_id, symbol, horizon_days, "
                "as_of, recommendation, forward_return_pct, classification) "
                "VALUES (?, 'AAPL', 5, '2027-02-08', 'proceed', 3.2, 'HIT')",
                [str(run_id)],
            )
            conn.execute(
                "INSERT INTO regime_snapshots VALUES (?, ?, 'NORMAL', 1.0, "
                "2.0, 'calm', 'OK', '{}')",
                [str(run_id), RUN_DATE],
            )

        df = research.scorecard(db_path=tmp_path / "copilot.duckdb")

        assert len(df) == 1
        row = df.iloc[0]
        assert row["recommendation"] == "proceed"
        assert row["horizon_days"] == 5
        assert row["forward_return_pct"] == pytest.approx(3.2)
        assert row["classification"] == "HIT"
        assert row["score"] == pytest.approx(0.7)
        assert row["rsi14"] == pytest.approx(33.0)
        assert row["gate_verdict"] == "NORMAL"
        assert row["gics_sector"] == "Information Technology"

    def test_a_verdict_with_no_matured_outcome_still_appears(
        self, state_store, tmp_path
    ):
        # An immature verdict must not vanish from the scorecard: it keeps a
        # single row whose horizon columns are NULL.
        run_id = uuid4()
        _insert_run(state_store, run_id)
        _insert_verdict(state_store, run_id, recommendation="skip")

        df = research.scorecard(db_path=tmp_path / "copilot.duckdb")

        assert len(df) == 1
        assert df.iloc[0]["recommendation"] == "skip"
        assert pd.isna(df.iloc[0]["horizon_days"])

    def test_sector_uses_the_latest_snapshot_at_or_before_run_date(
        self, state_store, tmp_path
    ):
        # As-of boundary: a snapshot dated exactly on run_date is visible
        # (inclusive), a later one is not, and among the visible ones the
        # latest wins.
        run_id = uuid4()
        _insert_universe(state_store, date(2027, 1, 1), sector="Old Sector")
        _insert_universe(state_store, RUN_DATE, sector="Boundary Sector")
        _insert_universe(state_store, date(2027, 2, 2), sector="Future Sector")
        _insert_run(state_store, run_id)
        _insert_verdict(state_store, run_id)

        df = research.scorecard(db_path=tmp_path / "copilot.duckdb")

        assert df.iloc[0]["gics_sector"] == "Boundary Sector"

    def test_a_snapshot_strictly_after_run_date_leaves_sector_null(
        self, state_store, tmp_path
    ):
        run_id = uuid4()
        _insert_universe(state_store, date(2027, 2, 2), sector="Future Sector")
        _insert_run(state_store, run_id)
        _insert_verdict(state_store, run_id)

        df = research.scorecard(db_path=tmp_path / "copilot.duckdb")

        assert len(df) == 1
        assert df.iloc[0]["gics_sector"] is None or pd.isna(df.iloc[0]["gics_sector"])


class TestCandidates:
    def test_lifts_score_components_out_of_metrics_json(self, state_store, tmp_path):
        run_id = uuid4()
        _insert_run(state_store, run_id)
        _insert_candidate(
            state_store,
            run_id,
            '{"score": 0.61, "score_rsi_pullback": 0.31,'
            ' "score_trend_quality": 0.2, "score_liquidity": 0.1,'
            ' "rsi14": 41.5, "sma50": 100.0, "sma200": 95.0,'
            ' "atr14": 3.5, "close": 120.0, "avg_volume": 2000000.0}',
        )

        df = research.candidates(db_path=tmp_path / "copilot.duckdb")

        row = df.iloc[0]
        assert row["score"] == pytest.approx(0.61)
        assert row["score_rsi_pullback"] == pytest.approx(0.31)
        assert row["score_trend_quality"] == pytest.approx(0.2)
        assert row["sma200"] == pytest.approx(95.0)
        assert row["avg_volume"] == pytest.approx(2000000.0)

    def test_a_metrics_json_missing_a_component_yields_null_not_an_error(
        self, state_store, tmp_path
    ):
        # Rows written before a score component existed have no such key;
        # the view must surface NULL, not fail the whole query.
        run_id = uuid4()
        _insert_run(state_store, run_id)
        _insert_candidate(state_store, run_id, '{"rsi14": 41.5}')

        df = research.candidates(db_path=tmp_path / "copilot.duckdb")

        assert df.iloc[0]["rsi14"] == pytest.approx(41.5)
        assert math.isnan(df.iloc[0]["score"])


class TestTableAccessors:
    def test_runs_verdicts_outcomes_rejections_and_regime(self, state_store, tmp_path):
        run_id = uuid4()
        _insert_run(state_store, run_id)
        _insert_verdict(state_store, run_id)
        with state_store._database.connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO verdict_outcomes (run_id, symbol, horizon_days, "
                "as_of, recommendation, forward_return_pct, classification) "
                "VALUES (?, 'AAPL', 20, '2027-03-01', 'proceed', -6.0, "
                "'MISS_SEVERE')",
                [str(run_id)],
            )
            conn.execute(
                "INSERT INTO screening_rejections VALUES (?, 'XYZ', "
                "'fundamental_filter', 'FILTER_NEGATIVE_FCF', '{}', ?)",
                [str(run_id), RUN_DATE],
            )
            conn.execute(
                "INSERT INTO regime_snapshots VALUES (?, ?, 'CAUTION', 4.0, "
                "5.0, 'elevated', 'OK', '{}')",
                [str(run_id), RUN_DATE],
            )

        db_path = tmp_path / "copilot.duckdb"
        assert research.runs(db_path=db_path).iloc[0]["status"] == "success"
        assert research.verdicts(db_path=db_path).iloc[0]["symbol"] == "AAPL"
        outcomes = research.verdict_outcomes(db_path=db_path)
        assert outcomes.iloc[0]["classification"] == "MISS_SEVERE"
        rejections = research.screening_rejections(db_path=db_path)
        assert rejections.iloc[0]["reason_code"] == "FILTER_NEGATIVE_FCF"
        regime = research.regime_snapshots(db_path=db_path)
        assert regime.iloc[0]["gate_verdict"] == "CAUTION"
        assert regime.iloc[0]["run_date"] == pd.Timestamp(RUN_DATE)

    def test_tracked_positions_carry_the_verdict_recommendation(
        self, state_store, tmp_path
    ):
        run_id = uuid4()
        _insert_run(state_store, run_id)
        _insert_verdict(state_store, run_id)
        with state_store._database.connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO verdict_positions (run_id, symbol, strategy_key, "
                "no_trade, entry_date, entry_price, stop_price, days_held, "
                "status, last_marked_date) "
                "VALUES (?, 'AAPL', 'default', FALSE, ?, 190.0, 182.0, 3, "
                "'open', ?)",
                [str(run_id), RUN_DATE, date(2027, 2, 4)],
            )

        df = research.tracked_positions(db_path=tmp_path / "copilot.duckdb")

        assert len(df) == 1
        assert df.iloc[0]["recommendation"] == "proceed"
        assert df.iloc[0]["entry_price"] == pytest.approx(190.0)


def _insert_truncation(store, run_id, symbol="NEAR", strategy_key="default", rank=6):
    with store._database.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO screening_truncations VALUES "
            "(?, ?, ?, ?, 0.42, 0.1, 0.2, 0.3, 0.4, 'READY', 0.02, ?)",
            [str(run_id), symbol, strategy_key, rank, RUN_DATE],
        )


def _insert_universe_return(store, run_id, symbol, outcome_class, reason_code=None):
    with store._database.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO universe_forward_returns VALUES "
            "(?, ?, 5, '2027-02-08', ?, ?, -3.5)",
            [str(run_id), symbol, outcome_class, reason_code],
        )


class TestControlGroupAccessors:
    """Issue #188: the truncated tail and the whole-universe forward returns."""

    def test_truncated_candidates_expose_the_score_breakdown_as_columns(
        self, state_store, tmp_path
    ):
        run_id = uuid4()
        _insert_run(state_store, run_id)
        _insert_truncation(state_store, run_id)

        df = research.truncated_candidates(db_path=tmp_path / "copilot.duckdb")

        assert len(df) == 1
        row = df.iloc[0]
        assert row["symbol"] == "NEAR"
        assert row["rank"] == 6
        assert row["score_liquidity"] == pytest.approx(0.3)
        assert row["run_date"] == pd.Timestamp(RUN_DATE)

    def test_universe_forward_returns_join_rank_score_and_sector(
        self, state_store, tmp_path
    ):
        run_id = uuid4()
        _insert_run(state_store, run_id)
        _insert_universe(state_store, RUN_DATE)
        _insert_candidate(state_store, run_id, '{"score": 0.8}')
        _insert_truncation(state_store, run_id)
        _insert_universe_return(state_store, run_id, "AAPL", "candidate")
        _insert_universe_return(state_store, run_id, "NEAR", "truncated")
        _insert_universe_return(
            state_store, run_id, "XYZ", "rejected", "FILTER_NEGATIVE_FCF"
        )

        df = research.universe_forward_returns(db_path=tmp_path / "copilot.duckdb")
        by_symbol = df.set_index("symbol")

        assert len(df) == 3
        assert by_symbol.loc["AAPL", "rank"] == 1
        assert by_symbol.loc["AAPL", "score"] == pytest.approx(0.8)
        assert by_symbol.loc["AAPL", "gics_sector"] == "Information Technology"
        assert by_symbol.loc["NEAR", "rank"] == 6
        assert by_symbol.loc["XYZ", "reason_code"] == "FILTER_NEGATIVE_FCF"
        # A rejected symbol was never ranked at all, so both ranking legs of
        # the view stay null rather than defaulting to a position.
        assert pd.isna(by_symbol.loc["XYZ", "rank"])

    def test_a_symbol_ranked_by_two_strategies_stays_one_row(
        self, state_store, tmp_path
    ):
        # `universe_forward_returns` records a decision about a symbol on a
        # day, not per strategy: the view must not fan one decision out into
        # one row per strategy the run happened to score.
        run_id = uuid4()
        _insert_run(state_store, run_id)
        _insert_truncation(state_store, run_id, strategy_key="default", rank=6)
        _insert_truncation(state_store, run_id, strategy_key="vcp", rank=9)
        _insert_universe_return(state_store, run_id, "NEAR", "truncated")

        df = research.universe_forward_returns(db_path=tmp_path / "copilot.duckdb")

        assert len(df) == 1
        assert df.iloc[0]["rank"] == 6


class TestQuery:
    def test_binds_parameters(self, state_store, tmp_path):
        run_id = uuid4()
        _insert_run(state_store, run_id)

        df = research.query(
            "SELECT run_id FROM runs WHERE run_date = ?",
            [RUN_DATE],
            db_path=tmp_path / "copilot.duckdb",
        )

        assert len(df) == 1

    def test_a_mutating_statement_fails_and_writes_nothing(self, state_store, tmp_path):
        # The read-only connection is the enforcement, not convention: an
        # INSERT must raise and leave the table untouched.
        run_id = uuid4()
        _insert_run(state_store, run_id)

        with pytest.raises(duckdb.Error):
            research.query("DELETE FROM runs", db_path=tmp_path / "copilot.duckdb")

        assert len(research.runs(db_path=tmp_path / "copilot.duckdb")) == 1

    def test_a_missing_database_file_raises_research_error(self, tmp_path):
        with pytest.raises(research.ResearchError, match="not found"):
            research.runs(db_path=tmp_path / "nope.duckdb")


class TestEnsureViews:
    def test_a_pre_view_database_gets_a_hint_then_recovers(self, tmp_path):
        # A database file that predates the analysis views: reading fails
        # with a pointer at ensure_views(), and ensure_views() fixes it.
        db_path = tmp_path / "legacy.duckdb"
        duckdb.connect(str(db_path)).close()

        with pytest.raises(research.ResearchError, match="ensure_views"):
            research.scorecard(db_path=db_path)

        research.ensure_views(db_path)

        assert research.scorecard(db_path=db_path).empty


class TestBars:
    def test_reads_partitions_and_filters_symbols(self, tmp_path):
        root = tmp_path / "parquet"
        store = MarketStore(Database(tmp_path / "copilot.duckdb"), root)
        stamp = datetime(2027, 2, 1, 21, 0, tzinfo=UTC)
        store.write_bars(
            pd.DataFrame(
                {
                    "symbol": ["AAPL", "MSFT"],
                    "date": [RUN_DATE, RUN_DATE],
                    "open": [1.0, 2.0],
                    "high": [1.1, 2.1],
                    "low": [0.9, 1.9],
                    "close": [1.05, 2.05],
                    "volume": [100, 200],
                    "provider": ["test", "test"],
                    "fetched_at": [stamp, stamp],
                }
            )
        )

        every = research.bars(parquet_root=root)
        only_aapl = research.bars(["AAPL"], parquet_root=root)

        assert sorted(every["symbol"]) == ["AAPL", "MSFT"]
        assert list(only_aapl["symbol"]) == ["AAPL"]
        assert only_aapl.iloc[0]["close"] == pytest.approx(1.05)

    def test_an_empty_parquet_root_returns_an_empty_typed_frame(self, tmp_path):
        df = research.bars(parquet_root=tmp_path / "nothing")

        assert df.empty
        assert "symbol" in df.columns
        assert "close" in df.columns
