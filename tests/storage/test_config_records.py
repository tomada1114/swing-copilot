"""Issue #189: the `config_versions` ledger.

What a `runs.config_hash` stood for is the one thing here that cannot be
recomputed later, so the contracts under test are the correction upsert
(`first_seen_run_date` only ever moves backwards), the round trip of the
recorded sections, and the point-in-time cutoff of the run-to-config map.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from swing_copilot.models import RunMode
from swing_copilot.storage.config_records import ConfigVersionRecord

if TYPE_CHECKING:
    from swing_copilot.storage.state_store import StateStore

_SECTIONS = {"risk": {"risk_per_trade_pct": 1.0}, "retro": {"max_surprises": 5}}


def _record(
    config_hash: str = "cfg-a", first_seen: date = date(2027, 3, 1)
) -> ConfigVersionRecord:
    return ConfigVersionRecord(
        config_hash=config_hash,
        first_seen_run_date=first_seen,
        snapshot_hash="b" * 64,
        sections=_SECTIONS,
    )


class TestUpsertConfigVersion:
    def test_records_the_sections_the_hash_stood_for(
        self, state_store: StateStore
    ) -> None:
        state_store.upsert_config_version(_record())

        (row,) = state_store.get_config_versions()
        assert row.sections == _SECTIONS
        assert row.first_seen_run_date == date(2027, 3, 1)

    def test_a_later_run_keeps_the_earlier_first_seen_date(
        self, state_store: StateStore
    ) -> None:
        """Seeing the same configuration again is not a correction of when it began."""
        state_store.upsert_config_version(_record(first_seen=date(2027, 3, 1)))

        state_store.upsert_config_version(_record(first_seen=date(2027, 3, 8)))

        (row,) = state_store.get_config_versions()
        assert row.first_seen_run_date == date(2027, 3, 1)

    def test_a_backfilled_older_run_moves_first_seen_backwards(
        self, state_store: StateStore
    ) -> None:
        """The correction case: `DO NOTHING` would keep a first date that is wrong."""
        state_store.upsert_config_version(_record(first_seen=date(2027, 3, 8)))

        state_store.upsert_config_version(_record(first_seen=date(2027, 2, 1)))

        (row,) = state_store.get_config_versions()
        assert row.first_seen_run_date == date(2027, 2, 1)

    def test_the_recorded_sections_are_refreshed_in_place(
        self, state_store: StateStore
    ) -> None:
        state_store.upsert_config_version(_record())

        state_store.upsert_config_version(
            ConfigVersionRecord(
                config_hash="cfg-a",
                first_seen_run_date=date(2027, 3, 1),
                snapshot_hash="c" * 64,
                sections={"risk": {"risk_per_trade_pct": 2.0}},
            )
        )

        (row,) = state_store.get_config_versions()
        assert row.snapshot_hash == "c" * 64
        assert row.sections == {"risk": {"risk_per_trade_pct": 2.0}}

    def test_distinct_configurations_are_separate_rows(
        self, state_store: StateStore
    ) -> None:
        state_store.upsert_config_version(_record("cfg-b", date(2027, 3, 8)))
        state_store.upsert_config_version(_record("cfg-a", date(2027, 3, 1)))

        assert [row.config_hash for row in state_store.get_config_versions()] == [
            "cfg-a",
            "cfg-b",
        ]


class TestRunConfigHashes:
    @pytest.mark.parametrize(
        ("as_of", "is_visible"),
        [
            pytest.param(date(2027, 2, 28), False, id="before"),
            pytest.param(date(2027, 3, 1), True, id="exactly-at"),
            pytest.param(date(2027, 3, 2), True, id="after"),
        ],
    )
    def test_the_run_date_cutoff_is_inclusive(
        self, state_store: StateStore, as_of: date, is_visible: bool
    ) -> None:
        run_id = state_store.start_run(date(2027, 3, 1), RunMode.LIVE, "cfg-a")

        mapping = state_store.get_run_config_hashes(as_of)

        assert (run_id in mapping) is is_visible

    def test_maps_each_run_to_its_own_configuration(
        self, state_store: StateStore
    ) -> None:
        first = state_store.start_run(date(2027, 3, 1), RunMode.LIVE, "cfg-a")
        second = state_store.start_run(date(2027, 3, 8), RunMode.LIVE, "cfg-b")

        mapping = state_store.get_run_config_hashes(date(2027, 3, 29))

        assert mapping == {first: "cfg-a", second: "cfg-b"}

    def test_an_unknown_run_is_simply_absent(self, state_store: StateStore) -> None:
        assert uuid4() not in state_store.get_run_config_hashes(date(2027, 3, 29))
