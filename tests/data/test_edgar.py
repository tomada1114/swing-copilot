"""Tests for the EDGAR fundamentals/filings adapter (FR-03)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from swing_copilot.data.edgar import EdgarClient, FilingRef
from swing_copilot.storage.market_store import FundamentalsRecord

if TYPE_CHECKING:
    from collections.abc import Callable

IDENTITY = "swing-copilot tester tmasuyama1114@gmail.com"


class FakeFiling:
    DEFAULT_URL = "https://www.sec.gov/example"

    def __init__(
        self,
        accession_number: str,
        form: str,
        filing_date: date,
        period_of_report: date,
    ) -> None:
        self.accession_number = accession_number
        self.form = form
        self.filing_date = filing_date
        self.period_of_report = period_of_report
        self.filing_url = self.DEFAULT_URL
        self.filing_text = "Full filing text content."

    def text(self):
        return self.filing_text


@dataclass
class FakeFact:
    """A single XBRL fact, shaped like edgartools' `FinancialFact`."""

    concept: str
    accession: str
    form_type: str
    filing_date: date | None
    period_end: date | None
    numeric_value: float | None
    period_start: date | None = None
    dimensions: dict[str, str] | None = None


class FakeEntityFacts:
    """Bulk per-company facts, shaped like edgartools' `EntityFacts`."""

    def __init__(self, cik: int, facts: list[FakeFact]) -> None:
        self.cik = cik
        self._facts = facts

    def get_all_facts(self):
        return list(self._facts)


DEFAULT_CIK = 320193

# Each metric's first-choice concept tag, matching `edgar.py`'s priority lists.
_METRIC_CONCEPTS = {
    "revenue": "Revenues",
    "net_income": "NetIncomeLoss",
    "ocf": "NetCashProvidedByUsedInOperatingActivities",
    "capex": "PaymentsToAcquirePropertyPlantAndEquipment",
    "equity": "StockholdersEquity",
    "assets": "Assets",
    "shares": "WeightedAverageNumberOfSharesOutstandingBasic",
}


@dataclass
class FilingKey:
    """Identifies one filing's facts: accession, form, and reporting dates."""

    accession: str
    form: str
    filing_date: date
    period_end: date
    period_start: date | None = None


def _filing_facts(key: FilingKey, **metric_values: float | None) -> list[FakeFact]:
    """Build one filing's worth of facts, keyed by metric name (e.g. `revenue=`)."""
    return [
        FakeFact(
            f"us-gaap:{_METRIC_CONCEPTS[metric]}",
            key.accession,
            key.form,
            key.filing_date,
            key.period_end,
            value,
            period_start=key.period_start,
        )
        for metric, value in metric_values.items()
        if value is not None
    ]


class FakeCompany:
    def __init__(
        self,
        filings: list[FakeFiling] | None = None,
        facts: FakeEntityFacts | None = None,
    ) -> None:
        self._filings = filings or []
        self._facts = facts
        self.get_filings_calls: list[list[str]] = []
        self.get_facts_calls = 0

    def get_filings(self, *, form):
        self.get_filings_calls.append(list(form))
        return list(self._filings)

    def get_facts(self):
        self.get_facts_calls += 1
        return self._facts


class FakeClock:
    def __init__(self, times: list[float]) -> None:
        self._times = list(times)

    def __call__(self) -> float:
        return self._times.pop(0)


def _company_factory(company: FakeCompany) -> Callable[[str], FakeCompany]:
    def factory(_symbol: str) -> FakeCompany:
        return company

    return factory


@pytest.fixture(autouse=True)
def _no_real_edgar_identity_mutation(monkeypatch):
    """Prevent every test's EdgarClient from mutating the real process env.

    `edgar.set_identity()` sets the real `EDGAR_IDENTITY` environment
    variable; without this, it would leak between tests (and pollute
    `Secrets()` in unrelated test modules within the same pytest process).
    """
    monkeypatch.setattr(
        "swing_copilot.data.edgar.edgar.set_identity", lambda _identity: None
    )


class TestIdentity:
    def test_sets_edgar_identity_on_construction(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr("swing_copilot.data.edgar.edgar.set_identity", calls.append)

        EdgarClient(IDENTITY, company_factory=_company_factory(FakeCompany([])))

        assert calls == [IDENTITY]


class TestFetchFundamentals:
    def test_normalizes_to_fundamentals_record_schema(self):
        facts = _filing_facts(
            FilingKey("0001-26-000001", "10-Q", date(2026, 7, 10), date(2026, 6, 30)),
            revenue=100.0,
            net_income=20.0,
            ocf=25.0,
            capex=10.0,
            equity=500.0,
            assets=1000.0,
            shares=1_000_000.0,
        )
        company = FakeCompany(facts=FakeEntityFacts(DEFAULT_CIK, facts))
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert records == [
            FundamentalsRecord(
                accession_no="0001-26-000001",
                symbol="AAPL",
                form="10-Q",
                fiscal_period_end=date(2026, 6, 30),
                filed_at=datetime(2026, 7, 10, tzinfo=UTC),
                revenue=100.0,
                net_income=20.0,
                fcf=15.0,
                equity=500.0,
                assets=1000.0,
                shares=1_000_000.0,
                source_url=(
                    f"https://www.sec.gov/Archives/edgar/data/{DEFAULT_CIK}/"
                    "000126000001/0001-26-000001-index.htm"
                ),
                fetched_at=records[0].fetched_at,
            )
        ]

    def test_issues_one_bulk_facts_request_instead_of_per_filing_requests(self):
        company = FakeCompany(facts=FakeEntityFacts(DEFAULT_CIK, []))
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert company.get_facts_calls == 1
        assert company.get_filings_calls == []

    def test_excludes_filings_filed_after_as_of(self):
        facts = _filing_facts(
            FilingKey("old", "10-Q", date(2026, 7, 10), date(2026, 6, 30)), revenue=1.0
        ) + _filing_facts(
            FilingKey("future", "10-Q", date(2026, 7, 25), date(2026, 6, 30)),
            revenue=1.0,
        )
        company = FakeCompany(facts=FakeEntityFacts(DEFAULT_CIK, facts))
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert [record.accession_no for record in records] == ["old"]

    def test_includes_filing_filed_exactly_at_as_of(self):
        facts = _filing_facts(
            FilingKey("on-cutoff", "10-Q", date(2026, 7, 20), date(2026, 6, 30)),
            revenue=1.0,
        )
        company = FakeCompany(facts=FakeEntityFacts(DEFAULT_CIK, facts))
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert [record.accession_no for record in records] == ["on-cutoff"]

    def test_excludes_filing_filed_before_default_lookback_window(self):
        as_of = datetime(2026, 7, 20, tzinfo=UTC)
        too_old_date = (as_of - timedelta(days=401)).date()
        facts = _filing_facts(
            FilingKey("too-old", "10-Q", too_old_date, too_old_date), revenue=1.0
        )
        company = FakeCompany(facts=FakeEntityFacts(DEFAULT_CIK, facts))
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", as_of)

        assert records == []

    def test_includes_filing_filed_exactly_at_lookback_boundary(self):
        as_of = datetime(2026, 7, 20, tzinfo=UTC)
        boundary_date = (as_of - timedelta(days=400)).date()
        facts = _filing_facts(
            FilingKey("on-boundary", "10-Q", boundary_date, boundary_date),
            revenue=1.0,
        )
        company = FakeCompany(facts=FakeEntityFacts(DEFAULT_CIK, facts))
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", as_of)

        assert [record.accession_no for record in records] == ["on-boundary"]

    def test_custom_lookback_days_widens_the_window(self):
        as_of = datetime(2026, 7, 20, tzinfo=UTC)
        old_date = (as_of - timedelta(days=401)).date()
        facts = _filing_facts(
            FilingKey("outside-default", "10-Q", old_date, old_date), revenue=1.0
        )
        company = FakeCompany(facts=FakeEntityFacts(DEFAULT_CIK, facts))
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", as_of, lookback_days=500)

        assert [record.accession_no for record in records] == ["outside-default"]

    def test_returns_empty_list_when_symbol_has_no_facts(self):
        company = FakeCompany(facts=None)
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert records == []

    def test_skips_malformed_facts_without_crashing(self):
        malformed = FakeFact(
            "us-gaap:Revenues",
            "0001-26-000001",
            "10-Q",
            date(2026, 7, 10),
            None,  # missing period_end: cannot be attributed to a period
            999.0,
        )
        valid = _filing_facts(
            FilingKey("0001-26-000001", "10-Q", date(2026, 7, 10), date(2026, 6, 30)),
            revenue=100.0,
        )
        company = FakeCompany(facts=FakeEntityFacts(DEFAULT_CIK, [malformed, *valid]))
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert len(records) == 1
        assert records[0].revenue == 100.0

    def test_missing_fcf_falls_back_to_operating_cash_flow_minus_capex(self):
        facts = _filing_facts(
            FilingKey("0001-26-000001", "10-Q", date(2026, 7, 10), date(2026, 6, 30)),
            revenue=1.0,
            ocf=50.0,
            capex=10.0,
        )
        company = FakeCompany(facts=FakeEntityFacts(DEFAULT_CIK, facts))
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert records[0].fcf == pytest.approx(40.0)

    def test_fcf_is_none_when_no_source_data_is_available(self):
        facts = _filing_facts(
            FilingKey("0001-26-000001", "10-Q", date(2026, 7, 10), date(2026, 6, 30)),
            revenue=1.0,
        )
        company = FakeCompany(facts=FakeEntityFacts(DEFAULT_CIK, facts))
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert records[0].fcf is None

    def test_prefers_shorter_duration_fact_for_same_period_end(self):
        period_end = date(2026, 6, 30)
        quarterly = FakeFact(
            "us-gaap:Revenues",
            "0001-26-000001",
            "10-Q",
            date(2026, 7, 10),
            period_end,
            100.0,
            period_start=date(2026, 4, 1),  # 3-month duration
        )
        year_to_date = FakeFact(
            "us-gaap:Revenues",
            "0001-26-000001",
            "10-Q",
            date(2026, 7, 10),
            period_end,
            250.0,
            period_start=date(2026, 1, 1),  # 6-month cumulative duration
        )
        company = FakeCompany(
            facts=FakeEntityFacts(DEFAULT_CIK, [year_to_date, quarterly])
        )
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert records[0].revenue == 100.0

    def test_dei_cover_page_fact_does_not_hijack_fiscal_period_end(self):
        """Regression for P6-25 (a `dei` fact must not hijack the period end).

        Reproduces the real AMD accession 0000002488-25-000108 (10-Q filed
        for the quarter ended 2025-06-28): its `dei:
        EntityCommonStockSharesOutstanding` cover-page fact is dated
        2025-07-30, weeks after every us-gaap financial fact's period end.
        Before this fix, `max(period_end)` picked up the dei date, so every
        us-gaap concept's exact-match lookup failed and every metric came
        back `None`.
        """
        period_end = date(2025, 6, 28)
        us_gaap_facts = _filing_facts(
            FilingKey("0000002488-25-000108", "10-Q", date(2025, 7, 30), period_end),
            revenue=100.0,
            net_income=20.0,
        )
        dei_cover_page_fact = FakeFact(
            "dei:EntityCommonStockSharesOutstanding",
            "0000002488-25-000108",
            "10-Q",
            date(2025, 7, 30),
            date(2025, 7, 30),  # weeks after the fiscal period end
            1_000_000.0,
        )
        company = FakeCompany(
            facts=FakeEntityFacts(DEFAULT_CIK, [*us_gaap_facts, dei_cover_page_fact])
        )
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AMD", datetime(2025, 8, 1, tzinfo=UTC))

        assert len(records) == 1
        assert records[0].fiscal_period_end == period_end
        assert records[0].revenue == 100.0
        assert records[0].net_income == 20.0

    def test_filing_with_only_dei_facts_is_dropped(self):
        """A filing with only `dei` facts has no derivable fiscal period."""
        dei_only = FakeFact(
            "dei:EntityCommonStockSharesOutstanding",
            "0001-26-000001",
            "10-Q",
            date(2026, 7, 10),
            date(2026, 6, 30),
            1_000_000.0,
        )
        company = FakeCompany(facts=FakeEntityFacts(DEFAULT_CIK, [dei_only]))
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert records == []

    def test_excludes_dimensioned_segment_facts(self):
        period_end = date(2026, 6, 30)
        segment_fact = FakeFact(
            "us-gaap:Revenues",
            "0001-26-000001",
            "10-Q",
            date(2026, 7, 10),
            period_end,
            999.0,
            dimensions={"Segment": "Hardware"},
        )
        consolidated_fact = FakeFact(
            "us-gaap:Revenues",
            "0001-26-000001",
            "10-Q",
            date(2026, 7, 10),
            period_end,
            100.0,
        )
        company = FakeCompany(
            facts=FakeEntityFacts(DEFAULT_CIK, [segment_fact, consolidated_fact])
        )
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert records[0].revenue == 100.0


class TestFetchRecentFilings:
    def test_returns_filing_refs_for_requested_form_types(self):
        filing = FakeFiling(
            "0001-26-000003", "8-K", date(2026, 7, 18), date(2026, 7, 18)
        )
        company = FakeCompany([filing])
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        refs = client.fetch_recent_filings(
            "AAPL", ["8-K"], as_of=datetime(2026, 7, 20, tzinfo=UTC)
        )

        assert refs == [
            FilingRef(
                accession_no="0001-26-000003",
                symbol="AAPL",
                form="8-K",
                filed_at=datetime(2026, 7, 18, tzinfo=UTC),
                source_url="https://www.sec.gov/example",
            )
        ]
        assert company.get_filings_calls == [["8-K"]]


class TestRateLimiting:
    def test_throttles_when_calls_are_faster_than_ten_per_second(self):
        sleeps: list[float] = []
        clock = FakeClock([0.0, 0.02])
        company = FakeCompany([])
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            clock=clock,
            sleep_fn=sleeps.append,
        )

        client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))
        client.fetch_fundamentals("MSFT", datetime(2026, 7, 20, tzinfo=UTC))

        assert sleeps == [pytest.approx(0.08)]

    def test_does_not_throttle_when_calls_are_already_spaced_out(self):
        sleeps: list[float] = []
        clock = FakeClock([0.0, 5.0])
        company = FakeCompany([])
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            clock=clock,
            sleep_fn=sleeps.append,
        )

        client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))
        client.fetch_fundamentals("MSFT", datetime(2026, 7, 20, tzinfo=UTC))

        assert sleeps == []


class TestRetries:
    def test_retries_transient_facts_fetch_failure(self):
        calls = 0

        def failing_then_succeeding(_symbol: str) -> FakeCompany:
            nonlocal calls
            calls += 1
            if calls == 1:
                msg = "temporary EDGAR failure"
                raise ConnectionError(msg)
            return FakeCompany([])

        sleeps: list[float] = []
        client = EdgarClient(
            IDENTITY,
            company_factory=failing_then_succeeding,
            clock=FakeClock([0.0, 1.0]),
            sleep_fn=sleeps.append,
        )

        assert (
            client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC)) == []
        )
        assert calls == 2
        assert sleeps == [1.0]

    def test_stops_after_three_attempts(self):
        calls = 0

        def always_failing(_symbol: str) -> FakeCompany:
            nonlocal calls
            calls += 1
            msg = "persistent EDGAR failure"
            raise ConnectionError(msg)

        sleeps: list[float] = []
        client = EdgarClient(
            IDENTITY,
            company_factory=always_failing,
            clock=FakeClock([0.0, 1.0, 3.0]),
            sleep_fn=sleeps.append,
        )

        with pytest.raises(ConnectionError, match="persistent"):
            client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))
        assert calls == 3
        assert sleeps == [1.0, 2.0]


class TestFetchFilingTexts:
    def test_returns_one_text_item_per_filing(self):
        filing = FakeFiling(
            "0001-26-000004", "8-K", date(2026, 7, 18), date(2026, 7, 18)
        )
        company = FakeCompany([filing])
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        items = client.fetch_filing_texts(
            "AAPL", ["8-K"], as_of=datetime(2026, 7, 20, tzinfo=UTC)
        )

        assert len(items) == 1
        item = items[0]
        assert item.source_id == "edgar:0001-26-000004"
        assert item.symbol == "AAPL"
        assert item.source_type == "filing"
        assert item.content_text == "Full filing text content."
        assert item.source_url == FakeFiling.DEFAULT_URL
        assert company.get_filings_calls == [["8-K"]]

    def test_excludes_filing_text_published_after_as_of(self):
        filings = [
            FakeFiling("old", "8-K", date(2026, 7, 18), date(2026, 7, 18)),
            FakeFiling("future", "8-K", date(2026, 7, 25), date(2026, 7, 25)),
        ]
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(FakeCompany(filings)),
            sleep_fn=lambda _s: None,
        )

        items = client.fetch_filing_texts(
            "AAPL", ["8-K"], as_of=datetime(2026, 7, 20, tzinfo=UTC)
        )

        assert [item.source_id for item in items] == ["edgar:old"]
