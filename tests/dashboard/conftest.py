"""Isolated fixtures for the dashboard tests.

Every test gets its own DuckDB file and its own `reports/` tree under
`tmp_path`. The repository's real `data/` and `reports/` are never touched --
the autouse guards in `tests/conftest.py` enforce that, and the factory below
makes obeying them the path of least resistance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from swing_copilot.storage.database import Database
from swing_copilot.storage.state_store import StateStore

if TYPE_CHECKING:
    from pathlib import Path

RUN_DATE = date(2027, 3, 1)
PRIOR_RUN_DATE = date(2027, 2, 26)
RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
PRIOR_RUN_ID = UUID("22222222-2222-4222-8222-222222222222")


@dataclass(frozen=True, slots=True)
class Fixture:
    """One isolated database plus its reports tree."""

    db_path: Path
    reports_root: Path


class Builder:
    """Insert rows straight into an initialized schema.

    Deliberately raw SQL rather than the production writers: these tests are
    about how the dashboard *reads* history, including shapes the current
    writers no longer produce (a pre-#192 candidate row, a verdict archived
    without a risk assessment).

    Every method writes for `run_id`; `for_run()` returns a builder bound to
    another run, which keeps each method's signature about the row rather
    than about which run it belongs to.

    Each statement opens and closes its own connection. That is not a
    performance choice: DuckDB refuses a read-only connection while a
    read-write one is open on the same file, so a builder that held its
    connection would break every `research` read the tests then make -- the
    same lock rule the dashboard itself is built around.
    """

    def __init__(self, database: Database, run_id: UUID = RUN_ID) -> None:
        self._database = database
        self._run_id = str(run_id)

    def for_run(self, run_id: UUID) -> Builder:
        """A builder writing into a different run."""
        return Builder(self._database, run_id)

    def _execute(self, sql: str, params: list[object]) -> None:
        with self._database.connect() as connection:
            connection.execute(sql, params)

    def run(
        self,
        run_date: date = RUN_DATE,
        status: str = "success",
        mode: str = "live",
        started_at: datetime | None = None,
    ) -> Builder:
        """Insert a `runs` row.

        `started_at` defaults to `run_date` at 18:00 UTC, mimicking an
        ordinary same-day run. Pass it explicitly to simulate a `--as-of`
        replay (Issue #389): `run_date` set to the replayed date while
        `started_at` reflects when the run actually executed, which may be
        much later.
        """
        self._execute(
            "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
            "started_at) VALUES (?, ?, ?, 'cfg0123456789abcdef', ?, ?)",
            [
                self._run_id,
                run_date,
                mode,
                status,
                started_at
                if started_at is not None
                else datetime(
                    run_date.year, run_date.month, run_date.day, 18, tzinfo=UTC
                ),
            ],
        )
        return self

    def universe(self, symbol: str, sector: str = "Information Technology") -> None:
        self._execute(
            "INSERT INTO universe_membership VALUES (?, ?, ?, ?, ?, 'test')",
            [date(2027, 1, 4), symbol, symbol, f"{symbol} Inc.", sector],
        )

    def candidate(
        self,
        symbol: str,
        *,
        rank: int = 1,
        score: float | None = 0.5,
        execution_state: str | None = "READY",
    ) -> None:
        self._execute(
            "INSERT INTO candidates (run_id, symbol, strategy_key, rank, "
            "signal_names, metrics_json, score, score_rsi_pullback, "
            "score_trend_quality, score_liquidity, score_atr_pct, "
            "execution_state, execution_distance) VALUES (?, ?, 'default', ?, "
            "['trend_sma'], ?, ?, 0.1, 0.3, 0.05, 0.02, ?, 0.4)",
            [
                self._run_id,
                symbol,
                rank,
                '{"rsi14": 41.5, "sma50": 100.0, "sma200": 90.0, "atr14": 2.5,'
                ' "close": 101.25, "avg_volume": 1200000.0}',
                score,
                execution_state,
            ],
        )

    def legacy_candidate(self, symbol: str) -> None:
        """A candidate row from before Issue #192 promoted the score columns.

        `execution_state` / `execution_distance` are unrecoverable for such a
        row, which the dashboard must show as "never recorded".
        """
        self._execute(
            "INSERT INTO candidates (run_id, symbol, strategy_key, rank, "
            "signal_names, metrics_json) VALUES (?, ?, 'default', 9, "
            "['trend_sma'], '{\"close\": 55.0}')",
            [self._run_id, symbol],
        )

    def verdict(
        self,
        symbol: str,
        *,
        recommendation: str = "proceed",
        no_trade: bool = False,
        news_supply_level: str | None = "sufficient",
        as_of: date = RUN_DATE,
    ) -> None:
        self._execute(
            "INSERT INTO verdicts (run_id, symbol, as_of, strategy_key, "
            "recommendation, reasons_json, no_trade, news_supply_level) "
            "VALUES (?, ?, ?, 'default', ?, '[]', ?, ?)",
            [self._run_id, symbol, as_of, recommendation, no_trade, news_supply_level],
        )

    def outcome(
        self,
        symbol: str,
        *,
        horizon_days: int,
        forward_return_pct: float,
        classification: str,
        recommendation: str = "proceed",
    ) -> None:
        self._execute(
            "INSERT INTO verdict_outcomes (run_id, symbol, horizon_days, as_of, "
            "recommendation, forward_return_pct, classification) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                self._run_id,
                symbol,
                horizon_days,
                RUN_DATE,
                recommendation,
                forward_return_pct,
                classification,
            ],
        )

    def reason(
        self,
        symbol: str,
        *,
        index: int,
        text: str,
        basis: str | None = "filing",
        source_id_count: int = 1,
    ) -> None:
        self._execute(
            "INSERT INTO verdict_reasons VALUES (?, ?, ?, ?, ?, ?)",
            [self._run_id, symbol, index, text, basis, source_id_count],
        )

    def risk(
        self,
        symbol: str,
        *,
        status: str = "approved",
        binding_constraint: str | None = "earnings",
    ) -> None:
        self._execute(
            "INSERT INTO risk_assessments (run_id, symbol, status, reasons_json, "
            "warnings_json, binding_constraint) VALUES (?, ?, ?, '[]', '[]', ?)",
            [self._run_id, symbol, status, binding_constraint],
        )

    # PLR0913: a row factory mirroring verdict_positions' own columns,
    # all keyword-only past the symbol.
    def position(  # noqa: PLR0913
        self,
        symbol: str,
        *,
        recommendation: str = "proceed",
        status: str = "open",
        realized_return_pct: float | None = None,
        exit_reason: str | None = None,
        entry_date: date = RUN_DATE,
    ) -> None:
        self._execute(
            "INSERT INTO verdict_positions (run_id, symbol, strategy_key, "
            "recommendation, no_trade, entry_date, entry_price, stop_price, "
            "days_held, status, exit_date, exit_reason, realized_return_pct) "
            "VALUES (?, ?, 'default', ?, FALSE, ?, 101.25, 95.0, 3, ?, ?, ?, ?)",
            [
                self._run_id,
                symbol,
                recommendation,
                entry_date,
                status,
                None if status == "open" else date(2027, 3, 10),
                exit_reason,
                realized_return_pct,
            ],
        )

    def regime(
        self,
        *,
        gate_verdict: str = "BULL",
        dd_level: str = "CAUTION",
        vix_close: float | None = 15.5,
        as_of: date = RUN_DATE,
    ) -> None:
        self._execute(
            "INSERT INTO regime_snapshots (run_id, as_of, gate_verdict, "
            "dd_count_spy, dd_count_qqq, dd_level, data_quality, detail_json, "
            "dd15_spy, dd5_spy, spy_close, spy_ema, vix_close) "
            "VALUES (?, ?, ?, 3.0, 4.0, ?, 'OK', '{}', 1.0, 0.0, 770.0, 750.0, ?)",
            [self._run_id, as_of, gate_verdict, dd_level, vix_close],
        )

    def legacy_regime(self) -> None:
        """A snapshot from before the drawdown/price columns were promoted.

        `dd15_spy`, `spy_close` and `spy_ema` are NULL there, so the panel
        must render the absence token instead of computing a gap from zero.
        """
        self._execute(
            "INSERT INTO regime_snapshots (run_id, as_of, gate_verdict, "
            "dd_count_spy, dd_count_qqq, dd_level, data_quality, detail_json) "
            "VALUES (?, ?, 'BULL', 3.0, 4.0, 'NORMAL', 'OK', '{}')",
            [self._run_id, RUN_DATE],
        )

    def rejection(self, symbol: str, *, stage: str, reason_code: str) -> None:
        self._execute(
            "INSERT INTO screening_rejections VALUES (?, ?, ?, ?, '{}', ?)",
            [self._run_id, symbol, stage, reason_code, RUN_DATE],
        )


@pytest.fixture
def dashboard_db(tmp_path: Path) -> Fixture:
    """An initialized, empty database plus an empty reports tree."""
    db_path = tmp_path / "copilot.duckdb"
    StateStore(Database(db_path)).init_schema()
    reports_root = tmp_path / "reports"
    reports_root.mkdir()
    return Fixture(db_path=db_path, reports_root=reports_root)


@pytest.fixture
def builder(dashboard_db: Fixture) -> Builder:
    """A row factory bound to the isolated database."""
    return Builder(Database(dashboard_db.db_path))


def write_run_archive(
    reports_root: Path,
    *,
    run_id: UUID = RUN_ID,
    run_date: date = RUN_DATE,
    has_result: bool = True,
) -> Path:
    """Create the `reports/<date>/<run_id>/` archive a finished run leaves.

    `has_result=False` reproduces Issue #129's failure mode: the deterministic
    pipeline wrote `analysis_input.json` and the analysis skill never wrote
    its answer back.
    """
    directory = reports_root / run_date.isoformat() / str(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "analysis_input.json").write_text("{}", encoding="utf-8")
    if has_result:
        (directory / "analysis_result.json").write_text("{}", encoding="utf-8")
    return directory
