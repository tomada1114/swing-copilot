"""DuckDB correction and atomicity tests for MAE/MFE snapshots."""

from __future__ import annotations

from datetime import date
from typing import cast
from uuid import uuid4

import duckdb
import pytest

from swing_copilot.storage.paper_records import PositionExcursionRecord


def test_same_day_correction_replaces_excursion_values(state_store):
    position_id = uuid4()
    original = PositionExcursionRecord(position_id, date(2026, 7, 21), -1.0, 2.0, "OK")
    corrected = PositionExcursionRecord(position_id, date(2026, 7, 21), -2.0, 5.0, "OK")

    state_store.upsert_position_excursions([original])
    state_store.upsert_position_excursions([corrected])

    assert (
        state_store.get_position_excursions([position_id], date(2026, 7, 21))[
            position_id
        ]
        == corrected
    )


def test_excursion_batch_rolls_back_after_one_valid_statement(state_store):
    valid = PositionExcursionRecord(uuid4(), date(2026, 7, 21), -1.0, 2.0, "OK")
    invalid = PositionExcursionRecord(
        uuid4(),
        date(2026, 7, 21),
        -1.0,
        2.0,
        cast("str", None),
    )

    with pytest.raises(duckdb.ConstraintException, match="NOT NULL"):
        state_store.upsert_position_excursions([valid, invalid])

    assert (
        state_store.get_position_excursions([valid.position_id], date(2026, 7, 21))
        == {}
    )
