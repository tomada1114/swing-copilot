"""P8-31: surprise selection and the freshness fetch around it (E31.3).

Selection is deterministic and never silently truncating; the fetch runs
through the existing text adapters and degrades per symbol rather than
failing the export. Every adapter here is a fake -- the suite stays offline.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from swing_copilot.config import AnalysisConfig
from swing_copilot.retro.surprises import (
    FreshnessSources,
    fetch_freshness,
    select_surprises,
)
from swing_copilot.storage.verdict_records import VerdictOutcomeRecord
from swing_copilot.text.base import EXHIBIT_TRUNCATION_MARKER, TextItem

if TYPE_CHECKING:
    from collections.abc import Sequence

RUN_A = UUID("11111111-1111-1111-1111-111111111111")
RUN_DATE = date(2027, 3, 1)
AS_OF = date(2027, 3, 29)
MATURITY = date(2027, 3, 8)


def _outcome(
    symbol: str,
    recommendation: str,
    forward_return_pct: float,
    classification: str,
    *,
    horizon_days: int = 5,
) -> VerdictOutcomeRecord:
    return VerdictOutcomeRecord(
        run_id=RUN_A,
        symbol=symbol,
        horizon_days=horizon_days,
        as_of=MATURITY,
        recommendation=recommendation,
        forward_return_pct=forward_return_pct,
        classification=classification,
    )


class TestSelectSurprises:
    def test_selects_severe_misses_in_both_directions(self) -> None:
        outcomes = (
            _outcome("AAA", "proceed", -8.0, "MISS_SEVERE"),
            _outcome("BBB", "skip", 9.0, "MISS_SEVERE"),
            _outcome("CCC", "proceed", 1.0, "HIT"),
        )

        selection = select_surprises(outcomes, max_surprises=5)

        assert [item.symbol for item in selection.selected] == ["BBB", "AAA"]
        assert selection.dropped_count == 0

    def test_carries_every_horizon_of_the_selected_symbol(self) -> None:
        outcomes = (
            _outcome("AAA", "proceed", -8.0, "MISS_SEVERE"),
            _outcome("AAA", "proceed", 1.0, "HIT", horizon_days=20),
        )

        selection = select_surprises(outcomes, max_surprises=5)

        assert [
            (row.horizon_days, row.classification)
            for row in selection.selected[0].outcomes
        ] == [(5, "MISS_SEVERE"), (20, "HIT")]
        assert selection.selected[0].peak_abs_return_pct == pytest.approx(8.0)

    def test_ranks_by_absolute_move_and_reports_what_the_cap_dropped(self) -> None:
        outcomes = (
            _outcome("AAA", "proceed", -3.0, "MISS_SEVERE"),
            _outcome("BBB", "proceed", -9.0, "MISS_SEVERE"),
            _outcome("CCC", "skip", 5.0, "MISS_SEVERE"),
        )

        selection = select_surprises(outcomes, max_surprises=2)

        assert [item.symbol for item in selection.selected] == ["BBB", "CCC"]
        assert selection.dropped_count == 1

    def test_ranks_a_symbol_by_its_largest_severe_horizon(self) -> None:
        outcomes = (
            _outcome("AAA", "proceed", -3.0, "MISS_SEVERE"),
            _outcome("AAA", "proceed", -11.0, "MISS_SEVERE", horizon_days=20),
            _outcome("BBB", "proceed", -9.0, "MISS_SEVERE"),
        )

        selection = select_surprises(outcomes, max_surprises=1)

        assert [item.symbol for item in selection.selected] == ["AAA"]
        assert selection.dropped_count == 1

    def test_gives_each_selection_a_stable_reference_id(self) -> None:
        selection = select_surprises(
            (_outcome("AAA", "proceed", -8.0, "MISS_SEVERE"),), max_surprises=5
        )

        assert selection.selected[0].surprise_id == f"surprise:{RUN_A}:AAA"

    def test_breaks_equal_moves_deterministically(self) -> None:
        outcomes = (
            _outcome("BBB", "proceed", -5.0, "MISS_SEVERE"),
            _outcome("AAA", "proceed", -5.0, "MISS_SEVERE"),
        )

        first = select_surprises(outcomes, max_surprises=1)
        second = select_surprises(tuple(reversed(outcomes)), max_surprises=1)

        assert [item.symbol for item in first.selected] == ["AAA"]
        assert [item.symbol for item in second.selected] == ["AAA"]

    def test_selects_nothing_when_no_verdict_missed_severely(self) -> None:
        selection = select_surprises(
            (_outcome("AAA", "proceed", 1.0, "HIT"),), max_surprises=5
        )

        assert (selection.selected, selection.dropped_count) == ((), 0)


class _FakeNewsClient:
    def __init__(self, items: Sequence[TextItem]) -> None:
        self._items = list(items)
        self.calls: list[tuple[str, date, date]] = []

    def fetch_company_news(
        self, symbol: str, since: date, *, as_of: date
    ) -> list[TextItem]:
        self.calls.append((symbol, since, as_of))
        return list(self._items)


class _FakeEdgarClient:
    def __init__(self, items: Sequence[TextItem]) -> None:
        self._items = list(items)
        self.calls: list[tuple[str, datetime, datetime | None, int | None]] = []

    def fetch_filing_texts(
        self,
        symbol: str,
        form_types: list[str],
        *,
        as_of: datetime,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[TextItem]:
        self.calls.append((symbol, as_of, since, limit))
        assert form_types
        return list(self._items)


class _RaisingClient:
    def fetch_company_news(
        self, symbol: str, since: date, *, as_of: date
    ) -> list[TextItem]:
        msg = f"news provider unavailable for {symbol} ({since}..{as_of})"
        raise RuntimeError(msg)

    def fetch_filing_texts(
        self,
        symbol: str,
        form_types: list[str],
        *,
        as_of: datetime,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[TextItem]:
        msg = f"EDGAR unavailable for {symbol} {form_types} ({since}..{as_of}, {limit})"
        raise RuntimeError(msg)


def _news_item(source_id: str, day: int, summary: str = "本文") -> TextItem:
    return TextItem(
        source_id=source_id,
        symbol="AAA",
        source_type="news",
        published_at=datetime(2027, 3, day, tzinfo=UTC),
        title="見出し",
        source_url="https://example.test/news",
        content_text=summary,
        fetched_at=datetime(2027, 3, 29, tzinfo=UTC),
    )


def _filing_item(source_id: str, text: str = "開示本文") -> TextItem:
    return TextItem(
        source_id=source_id,
        symbol="AAA",
        source_type="filing",
        published_at=datetime(2027, 3, 10, tzinfo=UTC),
        title="8-K - Example Corp (2027-03-10)",
        source_url="https://example.test/filing",
        content_text=text,
        fetched_at=datetime(2027, 3, 29, tzinfo=UTC),
    )


class TestFetchFreshness:
    def test_fetches_the_window_between_the_run_and_the_retrospective(self) -> None:
        news_client = _FakeNewsClient([_news_item("finnhub:1", 5)])
        edgar_client = _FakeEdgarClient([_filing_item("edgar:1")])

        bundle, notes = fetch_freshness(
            FreshnessSources(news_client=news_client, edgar_client=edgar_client),
            "AAA",
            since=RUN_DATE,
            as_of=AS_OF,
            limits=AnalysisConfig(),
        )

        assert news_client.calls == [("AAA", RUN_DATE, AS_OF)]
        # The filing lookback is the same window expressed in days, so the
        # freshness fetch cannot reach back before the run being reviewed.
        symbol, filing_as_of, since, limit = edgar_client.calls[0]
        assert (symbol, filing_as_of.date(), limit) == ("AAA", AS_OF, 3)
        assert since is not None
        assert since.date() == RUN_DATE
        assert [item.source_id for item in bundle.news] == ["finnhub:1"]
        assert [item.form_type for item in bundle.filings] == ["8-K"]
        assert (bundle.fetch_failed, notes) == (False, ())

    def test_applies_the_existing_analysis_budgets(self) -> None:
        limits = AnalysisConfig(
            max_news_items_per_symbol=1, max_news_chars_per_item=3, max_filing_chars=2
        )
        news_client = _FakeNewsClient(
            [
                _news_item("finnhub:1", 5, "あいうえお"),
                _news_item("finnhub:2", 9, "本文"),
            ]
        )
        edgar_client = _FakeEdgarClient([_filing_item("edgar:1", "かきくけこ")])

        bundle, _ = fetch_freshness(
            FreshnessSources(news_client=news_client, edgar_client=edgar_client),
            "AAA",
            since=RUN_DATE,
            as_of=AS_OF,
            limits=limits,
        )

        # Newest first, one item, truncated to the per-item character budget.
        assert [(item.source_id, item.summary) for item in bundle.news] == [
            ("finnhub:2", "本文")
        ]
        assert [item.text for item in bundle.filings] == ["かき"]

    def test_reports_an_exhibit_cut_off_at_collection_on_this_path_too(self) -> None:
        # Issue #157: the retrospective builds its coverage with the daily
        # export's `select_filing_inputs`, but from `TextItem`s that crossed
        # a boundary (re-fetched here, read back from storage elsewhere). The
        # signal is the marker inside `content_text`, which survives that
        # crossing, so this path must report the cut exactly as the daily
        # export does rather than re-asserting "nothing was lost".
        edgar_client = _FakeEdgarClient(
            [_filing_item("edgar:1", "決算発表" + EXHIBIT_TRUNCATION_MARKER)]
        )

        bundle, _ = fetch_freshness(
            FreshnessSources(edgar_client=edgar_client),
            "AAA",
            since=RUN_DATE,
            as_of=AS_OF,
            limits=AnalysisConfig(),
        )

        coverage = bundle.filings[0].coverage
        assert coverage is not None
        assert coverage.is_truncated is False
        assert coverage.exhibit_truncated is True

    def test_a_refetched_filing_without_the_marker_reports_no_exhibit_cut(self) -> None:
        edgar_client = _FakeEdgarClient([_filing_item("edgar:1", "決算発表")])

        bundle, _ = fetch_freshness(
            FreshnessSources(edgar_client=edgar_client),
            "AAA",
            since=RUN_DATE,
            as_of=AS_OF,
            limits=AnalysisConfig(),
        )

        coverage = bundle.filings[0].coverage
        assert coverage is not None
        assert coverage.exhibit_truncated is False

    def test_degrades_to_an_empty_side_when_one_adapter_raises(self) -> None:
        bundle, notes = fetch_freshness(
            FreshnessSources(
                news_client=_RaisingClient(),
                edgar_client=_FakeEdgarClient([_filing_item("edgar:1")]),
            ),
            "AAA",
            since=RUN_DATE,
            as_of=AS_OF,
            limits=AnalysisConfig(),
        )

        assert bundle.news == ()
        assert [item.source_id for item in bundle.filings] == ["edgar:1"]
        assert bundle.fetch_failed is True
        assert len(notes) == 1
        assert "AAA" in notes[0]

    def test_reports_both_adapters_failing_without_raising(self) -> None:
        bundle, notes = fetch_freshness(
            FreshnessSources(
                news_client=_RaisingClient(), edgar_client=_RaisingClient()
            ),
            "AAA",
            since=RUN_DATE,
            as_of=AS_OF,
            limits=AnalysisConfig(),
        )

        assert (bundle.news, bundle.filings, bundle.fetch_failed) == ((), (), True)
        assert len(notes) == 2

    def test_returns_an_empty_bundle_when_no_adapter_is_configured(self) -> None:
        bundle, notes = fetch_freshness(
            FreshnessSources(),
            "AAA",
            since=RUN_DATE,
            as_of=AS_OF,
            limits=AnalysisConfig(),
        )

        # An absent API key is a configuration state, not a fetch failure:
        # nothing was attempted, so nothing failed.
        assert (bundle.news, bundle.filings, bundle.fetch_failed) == ((), (), False)
        assert notes == ()
