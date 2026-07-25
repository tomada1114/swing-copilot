"""DuckDB correction semantics for earnings-calendar snapshots (P4-18)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import cast

import duckdb
import pytest

from swing_copilot.data.earnings import EarningsEvent


def test_changed_earnings_date_correction_upserts_latest_value(state_store):
    original = EarningsEvent(
        "AAPL",
        date(2026, 8, 5),
        "amc",
        datetime(2026, 7, 21, 12, tzinfo=UTC),
    )
    corrected = EarningsEvent(
        "AAPL",
        date(2026, 8, 3),
        "bmo",
        datetime(2026, 7, 22, 12, tzinfo=UTC),
    )

    state_store.upsert_earnings_calendar([original])
    state_store.upsert_earnings_calendar([corrected])

    assert state_store.get_earnings_event("AAPL") == corrected


def test_batch_rolls_back_when_later_event_is_invalid(state_store):
    valid = EarningsEvent(
        "AAPL",
        date(2026, 8, 5),
        "amc",
        datetime(2026, 7, 21, 12, tzinfo=UTC),
    )
    invalid = EarningsEvent(
        cast("str", None),
        date(2026, 8, 6),
        "bmo",
        datetime(2026, 7, 21, 12, tzinfo=UTC),
    )

    with pytest.raises(duckdb.ConstraintException, match="NOT NULL"):
        state_store.upsert_earnings_calendar([valid, invalid])

    assert state_store.get_earnings_event("AAPL") is None
