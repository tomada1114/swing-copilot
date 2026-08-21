"""Persistence contracts for P3-13 regime snapshots."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

from swing_copilot.regime.distribution import (
    DataQuality,
    DistributionLevel,
    DistributionResult,
)
from swing_copilot.regime.gate import GateVerdict, MarketGate, RegimeSnapshot

if TYPE_CHECKING:
    from swing_copilot.storage.state_store import StateStore


def _snapshot(
    *,
    gate: GateVerdict = GateVerdict.BULL,
    spy: DistributionResult | None = None,
    qqq: DistributionResult | None = None,
    gate_inputs: tuple[float | None, float | None, float | None] = (520.0, 500.0, 15.0),
) -> RegimeSnapshot:
    result = DistributionResult(1.0, 0.0, 0.0, DistributionLevel.NORMAL, DataQuality.OK)
    return RegimeSnapshot(
        date(2026, 7, 21),
        MarketGate(gate, *gate_inputs),
        spy if spy is not None else result,
        qqq if qqq is not None else result,
        DistributionLevel.NORMAL,
        DataQuality.OK,
    )


def test_regime_snapshot_upserts_corrections(state_store: StateStore) -> None:
    run_id = uuid4()
    state_store.record_regime_snapshot(run_id, _snapshot())
    state_store.record_regime_snapshot(run_id, _snapshot(gate=GateVerdict.BEAR))

    with state_store.database.connect() as conn:
        rows = conn.execute(
            "SELECT gate_verdict, dd_level, data_quality FROM regime_snapshots WHERE run_id = ?",
            [str(run_id)],
        ).fetchall()
    assert rows == [("BEAR", "NORMAL", "OK")]


class TestPromotedRegimeColumns:
    """Issue #192: the sub-windows and gate inputs as columns, not JSON."""

    def test_records_sub_window_counts_and_gate_inputs(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        spy = DistributionResult(
            3.0, 2.0, 1.0, DistributionLevel.NORMAL, DataQuality.OK
        )
        qqq = DistributionResult(
            6.0, 4.0, 2.0, DistributionLevel.CAUTION, DataQuality.OK
        )

        state_store.record_regime_snapshot(run_id, _snapshot(spy=spy, qqq=qqq))

        with state_store.database.connect() as conn:
            row = conn.execute(
                "SELECT dd_count_spy, dd_count_qqq, dd15_spy, dd5_spy, dd15_qqq, "
                "dd5_qqq, spy_close, spy_sma200, vix_close FROM regime_snapshots "
                "WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()

        # `dd_count_*` keep meaning the 25-session counts they always did.
        assert row == (3.0, 6.0, 2.0, 1.0, 4.0, 2.0, 520.0, 500.0, 15.0)

    def test_an_unevaluable_gate_records_null_inputs(
        self, state_store: StateStore
    ) -> None:
        """A missing SPY/VIX bar is what produces `UNKNOWN`; it must stay NULL."""
        run_id = uuid4()

        state_store.record_regime_snapshot(
            run_id,
            _snapshot(gate=GateVerdict.UNKNOWN, gate_inputs=(None, None, None)),
        )

        with state_store.database.connect() as conn:
            row = conn.execute(
                "SELECT spy_close, spy_sma200, vix_close FROM regime_snapshots "
                "WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()

        assert row == (None, None, None)

    def test_a_correction_rewrites_the_promoted_columns(
        self, state_store: StateStore
    ) -> None:
        run_id = uuid4()
        state_store.record_regime_snapshot(run_id, _snapshot())

        state_store.record_regime_snapshot(
            run_id,
            _snapshot(
                spy=DistributionResult(
                    9.0, 8.0, 7.0, DistributionLevel.NORMAL, DataQuality.OK
                ),
                gate_inputs=(1.0, 2.0, 3.0),
            ),
        )

        with state_store.database.connect() as conn:
            row = conn.execute(
                "SELECT dd15_spy, dd5_spy, spy_close FROM regime_snapshots "
                "WHERE run_id = ?",
                [str(run_id)],
            ).fetchone()

        assert row == (8.0, 7.0, 1.0)
