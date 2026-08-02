"""Shared builders for the verdict-tracking tests.

Every scenario starts from the same flat prelude so the arithmetic stays
hand-checkable: twenty consecutive sessions closing at 100.00 with a 2.00-wide
range, which makes every true range exactly 2.00 and therefore Wilder ATR(14)
exactly 2.00 on the entry date.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pandas as pd
import pytest

from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.verdict_records import VerdictReasonRecord, VerdictRecord

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.storage.state_store import StateStore

RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
OTHER_RUN_ID = UUID("33333333-3333-3333-3333-333333333333")
SYMBOL = "AAA"

#: The verdict's run date, the last session of the flat prelude, and therefore
#: the virtual entry date.
ENTRY_DATE = date(2027, 3, 20)
#: The two sessions after the entry that most scenarios replay.
DAY_1 = ENTRY_DATE + timedelta(days=1)
DAY_2 = ENTRY_DATE + timedelta(days=2)

FLAT_CLOSE = 100.0
#: Wilder ATR(14) over the flat prelude: every true range is `high - low`.
FLAT_ATR = 2.0
#: `config/settings.yaml`'s backtest exit multiple, restated for the arithmetic.
EXIT_ATR_MULTIPLE = 2.5
#: `entry - 2.5 * ATR(14)`, the stop the risk assessment records.
RISK_STOP = FLAT_CLOSE - EXIT_ATR_MULTIPLE * FLAT_ATR
_PRELUDE_SESSIONS = 20
_FETCHED_AT = datetime(2027, 3, 20, tzinfo=UTC)


@pytest.fixture
def market_store(tmp_path: Path) -> MarketStore:
    """Bars source sharing the `state_store` fixture's database path."""
    return MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )


def bar(  # noqa: PLR0913 - one OHLCV row is genuinely six fields
    session_date: date,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    symbol: str = SYMBOL,
) -> dict[str, Any]:
    """One tidy OHLCV row."""
    return {
        "symbol": symbol,
        "date": session_date,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1_000_000,
        "provider": "test",
        "fetched_at": _FETCHED_AT,
    }


def flat_prelude(
    *, symbol: str = SYMBOL, sessions: int = _PRELUDE_SESSIONS
) -> list[dict[str, Any]]:
    """Sessions ending on `ENTRY_DATE`, all closing at `FLAT_CLOSE`."""
    return [
        bar(
            ENTRY_DATE - timedelta(days=offset),
            open_price=FLAT_CLOSE,
            high=FLAT_CLOSE + 1.0,
            low=FLAT_CLOSE - 1.0,
            close=FLAT_CLOSE,
            symbol=symbol,
        )
        for offset in reversed(range(sessions))
    ]


def write_bars(market_store: MarketStore, rows: list[dict[str, Any]]) -> None:
    """Persist bar rows through the real Parquet writer."""
    market_store.write_bars(pd.DataFrame(rows))


def seed_verdict(  # noqa: PLR0913 - a verdict row's own columns
    state_store: StateStore,
    *,
    run_id: UUID = RUN_ID,
    symbol: str = SYMBOL,
    as_of: date = ENTRY_DATE,
    recommendation: str = "proceed",
    no_trade: bool = False,
    strategy_key: str = "default",
) -> None:
    """Archive one run's single verdict through the production writer."""
    state_store.replace_run_verdicts(
        run_id,
        [
            VerdictRecord(
                run_id=run_id,
                symbol=symbol,
                as_of=as_of,
                strategy_key=strategy_key,
                recommendation=recommendation,
                reasons=(VerdictReasonRecord(text="押し目が浅い", source_ids=()),),
                no_trade=no_trade,
            )
        ],
        [],
    )


def seed_risk(
    state_store: StateStore,
    *,
    run_id: UUID = RUN_ID,
    symbol: str = SYMBOL,
    entry_price: float | None = FLAT_CLOSE,
    stop_price: float | None = RISK_STOP,
) -> None:
    """Insert the `risk_assessments` row the tracker reads its price seeds from."""
    with state_store.database.connect() as conn:
        conn.execute(
            """
            INSERT INTO risk_assessments (
                run_id, symbol, status, max_shares, entry_price, stop_price,
                reasons_json, warnings_json, sizing_warnings_json
            ) VALUES (?, ?, 'approved', 100, ?, ?, '[]', '[]', '[]')
            """,
            [str(run_id), symbol, entry_price, stop_price],
        )
