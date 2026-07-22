"""Tests for S&P 500 universe fetch/snapshot/override (FR-01)."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, TypedDict

import httpx
import pandas as pd
import pytest

from swing_copilot.universe import (
    WIKIPEDIA_SP500_URL,
    UniverseError,
    UniverseFetchOptions,
    UniverseMember,
    fetch_from_wikipedia,
    get_sp500_universe,
    refresh_universe,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

AS_OF = date(2026, 7, 20)

_CONSTITUENTS_HTML = """
<table>
<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th></tr>
<tr><td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td></tr>
<tr><td>BRK.B</td><td>Berkshire Hathaway</td><td>Financials</td></tr>
</table>
"""


class _RecordedCall(TypedDict):
    """One recorded `httpx.get` invocation, as seen by `_fake_httpx_get`."""

    url: str
    headers: dict[str, str]
    timeout: float
    follow_redirects: bool


def _fake_httpx_get(
    calls: list[_RecordedCall], responses: list[httpx.Response | Exception]
) -> Callable[..., httpx.Response]:
    """Build a fake `httpx.get` recording each call and popping a canned reply."""

    def _get(
        url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool
    ) -> httpx.Response:
        calls.append(
            {
                "url": url,
                "headers": headers,
                "timeout": timeout,
                "follow_redirects": follow_redirects,
            }
        )
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return _get


def _ok_response(text: str = _CONSTITUENTS_HTML) -> httpx.Response:
    return httpx.Response(
        200, text=text, request=httpx.Request("GET", WIKIPEDIA_SP500_URL)
    )


def _status_error_response(status_code: int) -> httpx.Response:
    return httpx.Response(
        status_code, text="", request=httpx.Request("GET", WIKIPEDIA_SP500_URL)
    )


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
        self, as_of: date | None = None
    ) -> tuple[date, tuple[UniverseMember, ...]] | None:
        eligible = [item for item in self.history if as_of is None or item[0] <= as_of]
        return eligible[-1] if eligible else None


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
        calls: list[_RecordedCall] = []
        monkeypatch.setattr(
            "swing_copilot.universe.httpx.get",
            _fake_httpx_get(calls, [_ok_response()]),
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

    def test_sends_wikimedia_compliant_user_agent_and_timeout(self, monkeypatch):
        calls: list[_RecordedCall] = []
        monkeypatch.setattr(
            "swing_copilot.universe.httpx.get",
            _fake_httpx_get(calls, [_ok_response()]),
        )

        fetch_from_wikipedia()

        assert len(calls) == 1
        assert calls[0]["url"] == WIKIPEDIA_SP500_URL
        assert calls[0]["timeout"] == 10.0
        assert calls[0]["follow_redirects"] is True
        user_agent = calls[0]["headers"]["User-Agent"]
        assert user_agent.startswith("swing-copilot/")
        assert "github.com/tomada1114/swing-copilot" in user_agent

    def test_retries_with_backoff_then_propagates_after_persistent_403(
        self, monkeypatch
    ):
        calls: list[_RecordedCall] = []
        responses: list[httpx.Response | Exception] = [
            _status_error_response(403) for _ in range(3)
        ]
        monkeypatch.setattr(
            "swing_copilot.universe.httpx.get",
            _fake_httpx_get(calls, responses),
        )
        sleeps: list[float] = []

        with pytest.raises(httpx.HTTPStatusError):
            fetch_from_wikipedia(sleep_fn=sleeps.append)

        assert len(calls) == 3
        assert sleeps == [1.0, 2.0]

    def test_retries_transient_failure_then_succeeds(self, monkeypatch):
        table = pd.DataFrame(
            {
                "Symbol": ["AAPL"],
                "Security": ["Apple Inc."],
                "GICS Sector": ["Information Technology"],
            }
        )
        calls: list[_RecordedCall] = []
        responses: list[httpx.Response | Exception] = [
            _status_error_response(403),
            _ok_response(),
        ]
        monkeypatch.setattr(
            "swing_copilot.universe.httpx.get",
            _fake_httpx_get(calls, responses),
        )
        monkeypatch.setattr("swing_copilot.universe.pd.read_html", lambda _url: [table])
        sleeps: list[float] = []

        result = fetch_from_wikipedia(sleep_fn=sleeps.append)

        assert len(calls) == 2
        assert sleeps == [1.0]
        assert result == [
            UniverseMember(
                symbol="AAPL",
                company_name="Apple Inc.",
                gics_sector="Information Technology",
                source_symbol="AAPL",
            )
        ]

    def test_parse_error_after_successful_fetch_is_not_retried(self, monkeypatch):
        calls: list[_RecordedCall] = []
        monkeypatch.setattr(
            "swing_copilot.universe.httpx.get",
            _fake_httpx_get(calls, [_ok_response()]),
        )

        def _boom(_html):
            msg = "malformed table"
            raise ValueError(msg)

        monkeypatch.setattr("swing_copilot.universe.pd.read_html", _boom)
        sleeps: list[float] = []

        with pytest.raises(ValueError, match="malformed table"):
            fetch_from_wikipedia(sleep_fn=sleeps.append)

        assert len(calls) == 1
        assert sleeps == []
