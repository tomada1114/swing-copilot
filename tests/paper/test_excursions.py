"""Daily MAE/MFE tracking acceptance tests (P4-20)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pandas as pd
import pytest

from swing_copilot.models import Position
from swing_copilot.paper.excursions import update_position_excursions
from swing_copilot.paper.journal import PaperJournal
from swing_copilot.storage.market_store import MarketStore


@pytest.fixture
def market_store(state_store, tmp_path):
    return MarketStore(state_store._database, parquet_root=tmp_path / "bars")  # noqa: SLF001


def _position(symbol: str = "AAPL") -> Position:
    return Position(uuid4(), symbol, True, date(2026, 7, 19), 150.0, 10, "open")


def _write_bars(
    market_store: MarketStore,
    symbol: str,
    rows: list[tuple[date, float, float]],
) -> None:
    market_store.write_bars(
        pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "date": day,
                    "open": 150.0,
                    "high": high,
                    "low": low,
                    "close": 150.0,
                    "volume": 1_000,
                    "provider": "test",
                    "fetched_at": datetime(2026, 7, 25, tzinfo=UTC),
                }
                for day, high, low in rows
            ]
        )
    )


def test_mae_mfe_hand_calculated_values_are_persisted_and_ignore_future(
    state_store, market_store
):
    position = _position()
    state_store.upsert_position(position)
    _write_bars(
        market_store,
        "AAPL",
        [
            (date(2026, 7, 19), 152.0, 148.0),
            (date(2026, 7, 20), 155.0, 151.0),
            (date(2026, 7, 21), 153.0, 149.0),
            (date(2026, 7, 22), 170.0, 130.0),
        ],
    )

    update_position_excursions(state_store, market_store, date(2026, 7, 19))
    update_position_excursions(state_store, market_store, date(2026, 7, 20))
    PaperJournal(state_store).close_position(
        position.position_id, date(2026, 7, 21), 152.0, "target"
    )
    summary = update_position_excursions(state_store, market_store, date(2026, 7, 21))
    record = state_store.get_position_excursions(
        [position.position_id], date(2026, 7, 21)
    )[position.position_id]

    assert summary.updated_count == 1
    assert record.mae_per_share == pytest.approx(-2.0)
    assert record.mfe_per_share == pytest.approx(5.0)
    assert record.data_quality == "OK"
    with state_store._database.connect() as conn:  # noqa: SLF001
        count = conn.execute(
            "SELECT count(*) FROM position_excursions WHERE position_id = ?",
            [str(position.position_id)],
        ).fetchone()[0]
    assert count == 3


@pytest.mark.parametrize(
    "case",
    [(110.0, 101.0, 0.0, 10.0), (99.0, 90.0, -10.0, 0.0)],
)
def test_mae_mfe_clamp_away_opposite_direction(
    state_store,
    market_store,
    case,
):
    high, low, expected_mae, expected_mfe = case
    position = Position(uuid4(), "AAPL", True, date(2026, 7, 21), 100.0, 10, "open")
    state_store.upsert_position(position)
    _write_bars(market_store, "AAPL", [(date(2026, 7, 21), high, low)])

    update_position_excursions(state_store, market_store, date(2026, 7, 21))
    record = state_store.get_position_excursions(
        [position.position_id], date(2026, 7, 21)
    )[position.position_id]

    assert record.mae_per_share == expected_mae
    assert record.mfe_per_share == expected_mfe


def test_mae_mfe_missing_today_preserves_prior_extremes_and_records_quality(
    state_store, market_store
):
    position = _position()
    state_store.upsert_position(position)
    _write_bars(market_store, "AAPL", [(date(2026, 7, 19), 152.0, 148.0)])

    summary = update_position_excursions(state_store, market_store, date(2026, 7, 21))
    record = state_store.get_position_excursions(
        [position.position_id], date(2026, 7, 21)
    )[position.position_id]

    assert summary.missing_symbols == ("AAPL",)
    assert record.mae_per_share == -2.0
    assert record.mfe_per_share == 2.0
    assert record.data_quality == "MISSING_BAR"


def test_mae_mfe_historical_rerun_includes_position_closed_later(
    state_store, market_store
):
    position = _position()
    state_store.upsert_position(position)
    PaperJournal(state_store).close_position(
        position.position_id, date(2026, 7, 22), 154.0, "target"
    )
    _write_bars(market_store, "AAPL", [(date(2026, 7, 21), 155.0, 149.0)])

    summary = update_position_excursions(state_store, market_store, date(2026, 7, 21))
    record = state_store.get_position_excursions(
        [position.position_id], date(2026, 7, 21)
    )[position.position_id]

    assert summary.updated_count == 1
    assert record.mfe_per_share == 5.0
