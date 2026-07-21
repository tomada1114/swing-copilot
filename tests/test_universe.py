"""Tests for S&P 500 universe fetch/snapshot/override (FR-01)."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from swing_copilot.universe import (
    UniverseError,
    UniverseFetchOptions,
    UniverseMember,
    fetch_from_wikipedia,
    get_sp500_universe,
    refresh_universe,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

AS_OF = date(2026, 7, 20)


def _members(*rows: tuple[str, str, str, str]) -> list[UniverseMember]:
    return [
        UniverseMember(
            symbol=symbol, company_name=name, gics_sector=sector, source_symbol=source
        )
        for symbol, name, sector, source in rows
    ]


class FakeUniverseStateStore:
    """In-memory stand-in for the StateStore capability refresh_universe() needs."""

    def __init__(self) -> None:
        self.history: list[tuple[date, tuple[UniverseMember, ...]]] = []

    def record_universe_membership(
        self, snapshot_date: date, members: Sequence[UniverseMember]
    ) -> None:
        self.history.append((snapshot_date, tuple(members)))

    def get_latest_universe_membership(
        self,
    ) -> tuple[date, tuple[UniverseMember, ...]] | None:
        return self.history[-1] if self.history else None


class TestGetSp500Universe:
    def test_fetches_and_caches_on_first_run(self, tmp_path):
        snapshot_path = tmp_path / "universe_snapshot.csv"
        fetched = _members(
            ("AAPL", "Apple Inc.", "Information Technology", "AAPL"),
            ("BRK-B", "Berkshire Hathaway", "Financials", "BRK.B"),
        )

        result = get_sp500_universe(
            AS_OF,
            options=UniverseFetchOptions(
                snapshot_path=snapshot_path, fetch_fn=lambda: fetched
            ),
        )

        assert result == fetched
        assert snapshot_path.is_file()

    def test_symbol_normalization_preserves_source_symbol_and_sector(self, tmp_path):
        snapshot_path = tmp_path / "universe_snapshot.csv"
        fetched = _members(
            ("BRK-B", "Berkshire Hathaway", "Financials", "BRK.B"),
        )

        result = get_sp500_universe(
            AS_OF,
            options=UniverseFetchOptions(
                snapshot_path=snapshot_path, fetch_fn=lambda: fetched
            ),
        )

        assert result[0].symbol == "BRK-B"
        assert result[0].source_symbol == "BRK.B"
        assert result[0].gics_sector == "Financials"

    def test_uses_cached_snapshot_without_fetching_when_present(self, tmp_path):
        snapshot_path = tmp_path / "universe_snapshot.csv"
        cached = _members(("AAPL", "Apple Inc.", "Information Technology", "AAPL"))
        get_sp500_universe(
            AS_OF,
            options=UniverseFetchOptions(
                snapshot_path=snapshot_path, fetch_fn=lambda: cached
            ),
        )

        def _boom():
            msg = "fetch_fn must not be called when cache is fresh"
            raise AssertionError(msg)

        result = get_sp500_universe(
            AS_OF,
            options=UniverseFetchOptions(snapshot_path=snapshot_path, fetch_fn=_boom),
        )

        assert result == cached

    def test_force_refresh_refetches_even_with_cache_present(self, tmp_path):
        snapshot_path = tmp_path / "universe_snapshot.csv"
        cached = _members(("AAPL", "Apple Inc.", "Information Technology", "AAPL"))
        get_sp500_universe(
            AS_OF,
            options=UniverseFetchOptions(
                snapshot_path=snapshot_path, fetch_fn=lambda: cached
            ),
        )

        refreshed = _members(
            ("MSFT", "Microsoft Corp.", "Information Technology", "MSFT")
        )
        result = get_sp500_universe(
            AS_OF,
            force_refresh=True,
            options=UniverseFetchOptions(
                snapshot_path=snapshot_path, fetch_fn=lambda: refreshed
            ),
        )

        assert result == refreshed

    def test_falls_back_to_snapshot_when_fetch_fails(self, tmp_path):
        snapshot_path = tmp_path / "universe_snapshot.csv"
        cached = _members(("AAPL", "Apple Inc.", "Information Technology", "AAPL"))
        get_sp500_universe(
            AS_OF,
            options=UniverseFetchOptions(
                snapshot_path=snapshot_path, fetch_fn=lambda: cached
            ),
        )

        def _boom():
            msg = "wikipedia unreachable"
            raise RuntimeError(msg)

        result = get_sp500_universe(
            AS_OF,
            force_refresh=True,
            options=UniverseFetchOptions(snapshot_path=snapshot_path, fetch_fn=_boom),
        )

        assert result == cached

    def test_raises_universe_error_when_fetch_fails_and_no_snapshot(self, tmp_path):
        snapshot_path = tmp_path / "universe_snapshot.csv"

        def _boom():
            msg = "wikipedia unreachable"
            raise RuntimeError(msg)

        with pytest.raises(UniverseError):
            get_sp500_universe(
                AS_OF,
                options=UniverseFetchOptions(
                    snapshot_path=snapshot_path, fetch_fn=_boom
                ),
            )

    def test_manual_exclude_removes_symbol(self, tmp_path):
        snapshot_path = tmp_path / "universe_snapshot.csv"
        fetched = _members(
            ("AAPL", "Apple Inc.", "Information Technology", "AAPL"),
            ("MSFT", "Microsoft Corp.", "Information Technology", "MSFT"),
        )

        result = get_sp500_universe(
            AS_OF,
            options=UniverseFetchOptions(
                snapshot_path=snapshot_path,
                manual_exclude=["MSFT"],
                fetch_fn=lambda: fetched,
            ),
        )

        assert [member.symbol for member in result] == ["AAPL"]

    def test_manual_include_adds_symbol_not_already_present(self, tmp_path):
        snapshot_path = tmp_path / "universe_snapshot.csv"
        fetched = _members(("AAPL", "Apple Inc.", "Information Technology", "AAPL"))

        result = get_sp500_universe(
            AS_OF,
            options=UniverseFetchOptions(
                snapshot_path=snapshot_path,
                manual_include=["NVDA"],
                fetch_fn=lambda: fetched,
            ),
        )

        assert {member.symbol for member in result} == {"AAPL", "NVDA"}

    def test_manual_include_of_existing_symbol_does_not_duplicate(self, tmp_path):
        snapshot_path = tmp_path / "universe_snapshot.csv"
        fetched = _members(("AAPL", "Apple Inc.", "Information Technology", "AAPL"))

        result = get_sp500_universe(
            AS_OF,
            options=UniverseFetchOptions(
                snapshot_path=snapshot_path,
                manual_include=["AAPL"],
                fetch_fn=lambda: fetched,
            ),
        )

        assert [member.symbol for member in result] == ["AAPL"]


class TestRefreshUniverse:
    def test_persists_to_state_store_and_returns_members(self, tmp_path):
        snapshot_path = tmp_path / "universe_snapshot.csv"
        state_store = FakeUniverseStateStore()
        fetched = _members(("AAPL", "Apple Inc.", "Information Technology", "AAPL"))

        result = refresh_universe(
            AS_OF,
            state_store,
            options=UniverseFetchOptions(
                snapshot_path=snapshot_path, fetch_fn=lambda: fetched
            ),
        )

        assert result == fetched
        assert state_store.get_latest_universe_membership() == (AS_OF, tuple(fetched))

    def test_second_refresh_appends_new_history_entry(self, tmp_path):
        snapshot_path = tmp_path / "universe_snapshot.csv"
        state_store = FakeUniverseStateStore()
        first = _members(("AAPL", "Apple Inc.", "Information Technology", "AAPL"))
        second = _members(("MSFT", "Microsoft Corp.", "Information Technology", "MSFT"))

        refresh_universe(
            AS_OF,
            state_store,
            options=UniverseFetchOptions(
                snapshot_path=snapshot_path, fetch_fn=lambda: first
            ),
        )
        refresh_universe(
            date(2026, 7, 27),
            state_store,
            options=UniverseFetchOptions(
                snapshot_path=snapshot_path, fetch_fn=lambda: second
            ),
        )

        assert len(state_store.history) == 2
        assert state_store.get_latest_universe_membership() == (
            date(2026, 7, 27),
            tuple(second),
        )


class TestFetchFromWikipedia:
    def test_parses_and_normalizes_the_constituents_table(self, monkeypatch):
        table = pd.DataFrame(
            {
                "Symbol": ["AAPL", "BRK.B"],
                "Security": ["Apple Inc.", "Berkshire Hathaway"],
                "GICS Sector": ["Information Technology", "Financials"],
            }
        )
        monkeypatch.setattr("swing_copilot.universe.pd.read_html", lambda _url: [table])

        result = fetch_from_wikipedia()

        assert result == [
            UniverseMember(
                symbol="AAPL",
                company_name="Apple Inc.",
                gics_sector="Information Technology",
                source_symbol="AAPL",
            ),
            UniverseMember(
                symbol="BRK-B",
                company_name="Berkshire Hathaway",
                gics_sector="Financials",
                source_symbol="BRK.B",
            ),
        ]
