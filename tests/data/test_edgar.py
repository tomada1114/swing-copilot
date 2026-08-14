"""Tests for the EDGAR fundamentals/filings adapter (FR-03)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from swing_copilot.analysis.filing_selection import select_filing_text
from swing_copilot.data.edgar import EdgarClient, FilingRef
from swing_copilot.storage.market_store import FundamentalsRecord

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from swing_copilot.text.base import TextItem

IDENTITY = "swing-copilot tester tmasuyama1114@gmail.com"


class FakeAttachment:
    """One filed document, shaped like edgartools' `Attachment`.

    `content` is the raw filed payload, downloaded on access: a `str` for a
    text/HTML document, `bytes` for anything else. Conversion to text is the
    adapter's own job (Issue #156), so these fakes never render anything
    themselves.
    """

    def __init__(
        self,
        document_type: str,
        document: str,
        content: str | bytes | None = "",
        error: Exception | None = None,
    ) -> None:
        self.document_type = document_type
        self.document = document
        self._content = content
        self._error = error
        self.content_calls = 0

    @property
    def content(self):
        self.content_calls += 1
        if self._error is not None:
            raise self._error
        return self._content


class FakeAttachments:
    """A filing's attachment set, shaped like edgartools' `Attachments`."""

    def __init__(self, documents: list[FakeAttachment]) -> None:
        self.documents = documents


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
        self.report: FakeTenQReport | None = None
        self.obj_error: Exception | None = None
        self.documents: list[FakeAttachment] = []
        self.attachments_error: Exception | None = None
        self.attachments_calls = 0

    @property
    def attachments(self):
        self.attachments_calls += 1
        if self.attachments_error is not None:
            raise self.attachments_error
        return FakeAttachments(self.documents)

    def text(self):
        return self.filing_text

    def obj(self):
        if self.obj_error is not None:
            raise self.obj_error
        return self.report


class FakeTenQReport:
    """A parsed 10-Q, shaped like edgartools' `TenQ`.

    A section's value may be an `Exception` instead of its text: issuer-specific
    markup defeats section detection per item, so the fake raises for exactly
    the items that would fail and returns text for the rest (Issue #155).
    """

    def __init__(self, sections: Mapping[tuple[str, str], str | Exception]) -> None:
        self._sections = sections

    def get_item_with_part(self, part, item, markdown=True):
        del markdown
        section = self._sections.get((part, item))
        if isinstance(section, Exception):
            raise section
        return section


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

    def test_does_not_retry_validation_error(self):
        calls = 0

        def invalid_request(_symbol: str) -> FakeCompany:
            nonlocal calls
            calls += 1
            msg = "invalid accession"
            raise ValueError(msg)

        sleeps: list[float] = []
        client = EdgarClient(
            IDENTITY,
            company_factory=invalid_request,
            sleep_fn=sleeps.append,
        )

        with pytest.raises(ValueError, match="invalid accession"):
            client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))
        assert calls == 1
        assert sleeps == []


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

    def test_ten_q_carries_structured_priority_sections_with_full_audit_text(self):
        filing = FakeFiling(
            "0001-26-000005", "10-Q", date(2026, 7, 18), date(2026, 6, 30)
        )
        filing.report = FakeTenQReport(
            {
                ("Part I", "Item 1"): "financial statements",
                ("Part I", "Item 2"): "management discussion",
                ("Part II", "Item 1A"): "risk factors",
            }
        )
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(FakeCompany([filing])),
            sleep_fn=lambda _s: None,
        )

        item = client.fetch_filing_texts(
            "AAPL", ["10-Q"], as_of=datetime(2026, 7, 20, tzinfo=UTC)
        )[0]

        assert item.content_text == "Full filing text content."
        assert [
            (section.name, section.content_text) for section in item.filing_sections
        ] == [
            ("part_i_item_1", "financial statements"),
            ("part_i_item_2", "management discussion"),
            ("part_ii_item_1a", "risk factors"),
        ]

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


class TestFetchFilingTextsBounds:
    """P6-26: `since`/`limit` bound disclosure fetch instead of unlimited."""

    def test_since_boundary_is_inclusive_immediately_before_at_and_after(self):
        filings = [
            FakeFiling("before", "8-K", date(2026, 7, 17), date(2026, 7, 17)),
            FakeFiling("at", "8-K", date(2026, 7, 18), date(2026, 7, 18)),
            FakeFiling("after", "8-K", date(2026, 7, 19), date(2026, 7, 19)),
        ]
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(FakeCompany(filings)),
            sleep_fn=lambda _s: None,
        )

        items = client.fetch_filing_texts(
            "AAPL",
            ["8-K"],
            as_of=datetime(2026, 7, 20, tzinfo=UTC),
            since=datetime(2026, 7, 18, tzinfo=UTC),
        )

        assert [item.source_id for item in items] == ["edgar:after", "edgar:at"]

    def test_limit_caps_the_returned_filing_count(self):
        filings = [
            FakeFiling(f"acc-{i}", "8-K", date(2026, 7, 10 + i), date(2026, 7, 10 + i))
            for i in range(5)
        ]
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(FakeCompany(filings)),
            sleep_fn=lambda _s: None,
        )

        items = client.fetch_filing_texts(
            "AAPL", ["8-K"], as_of=datetime(2026, 7, 20, tzinfo=UTC), limit=2
        )

        assert len(items) == 2

    def test_results_are_sorted_filed_at_descending_regardless_of_source_order(self):
        filings = [
            FakeFiling("oldest", "8-K", date(2026, 7, 10), date(2026, 7, 10)),
            FakeFiling("newest", "8-K", date(2026, 7, 18), date(2026, 7, 18)),
            FakeFiling("middle", "8-K", date(2026, 7, 14), date(2026, 7, 14)),
        ]
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(FakeCompany(filings)),
            sleep_fn=lambda _s: None,
        )

        items = client.fetch_filing_texts(
            "AAPL", ["8-K"], as_of=datetime(2026, 7, 20, tzinfo=UTC)
        )

        assert [item.source_id for item in items] == [
            "edgar:newest",
            "edgar:middle",
            "edgar:oldest",
        ]

    def test_limit_keeps_the_most_recent_filings_after_sorting(self):
        filings = [
            FakeFiling("oldest", "8-K", date(2026, 7, 10), date(2026, 7, 10)),
            FakeFiling("newest", "8-K", date(2026, 7, 18), date(2026, 7, 18)),
            FakeFiling("middle", "8-K", date(2026, 7, 14), date(2026, 7, 14)),
        ]
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(FakeCompany(filings)),
            sleep_fn=lambda _s: None,
        )

        items = client.fetch_filing_texts(
            "AAPL", ["8-K"], as_of=datetime(2026, 7, 20, tzinfo=UTC), limit=1
        )

        assert [item.source_id for item in items] == ["edgar:newest"]


def _ten_q_filing(
    accession: str = "0001-26-000012", filing_date: date = date(2026, 7, 18)
) -> FakeFiling:
    """A 10-Q whose primary document is already downloaded when parsing starts."""
    return FakeFiling(accession, "10-Q", filing_date, date(2026, 6, 30))


def _fetch_one_ten_q(filing: FakeFiling) -> TextItem:
    """Fetch `filing`'s single `TextItem` through the public entry point."""
    client = EdgarClient(
        IDENTITY,
        company_factory=_company_factory(FakeCompany([filing])),
        sleep_fn=lambda _s: None,
    )
    return client.fetch_filing_texts(
        "AAPL", ["10-Q"], as_of=datetime(2026, 7, 20, tzinfo=UTC)
    )[0]


class TestTenQSectionFailSoft:
    """Issue #155: a parser failure costs the sections, never the filing.

    Section detection runs on issuer-authored markup, so it is the part of a
    10-Q fetch most likely to break on one company's HTML. By then the filing
    itself has already been downloaded, so a detection failure is a
    data-quality outcome: the audit text stays, the structured sections degrade
    to empty, and the export says `head_fallback`.
    """

    _LOG_PREFIX = "10-Q section extraction failed for accession 0001-26-000012"

    @staticmethod
    def _parse_failure() -> Exception:
        """A fresh parser failure per test; a raised instance keeps its traceback."""
        return ValueError("edgartools could not parse the primary document")

    def test_report_parse_failure_keeps_the_filing_text_without_sections(self, caplog):
        filing = _ten_q_filing()
        filing.obj_error = self._parse_failure()

        with caplog.at_level("ERROR"):
            item = _fetch_one_ten_q(filing)

        assert item.filing_sections == ()
        assert item.content_text == "Full filing text content."
        assert self._LOG_PREFIX in caplog.text

    def test_item_lookup_failure_keeps_the_filing_text_without_sections(self, caplog):
        filing = _ten_q_filing()
        filing.report = FakeTenQReport(
            {("Part I", "Item 1"): RuntimeError("item boundary detection failed")}
        )

        with caplog.at_level("ERROR"):
            item = _fetch_one_ten_q(filing)

        assert item.filing_sections == ()
        assert item.content_text == "Full filing text content."
        assert self._LOG_PREFIX in caplog.text

    def test_sections_found_before_the_failure_are_discarded_rather_than_half_kept(
        self, caplog
    ):
        # The requested items are collected as one tuple, so a later item
        # raising drops the earlier ones too. That is the intended reading:
        # a partial section set is indistinguishable from a filing that simply
        # lacks those sections, and `head_fallback` at least keeps the leading
        # slice of the real filing.
        filing = _ten_q_filing()
        filing.report = FakeTenQReport(
            {
                ("Part I", "Item 1"): "financial statements",
                ("Part I", "Item 2"): "management discussion",
                ("Part II", "Item 1A"): RuntimeError("risk-factor heading not found"),
            }
        )

        with caplog.at_level("ERROR"):
            item = _fetch_one_ten_q(filing)

        assert item.filing_sections == ()
        assert self._LOG_PREFIX in caplog.text

    def test_section_extraction_failure_does_not_abort_the_remaining_filings(
        self, caplog
    ):
        broken = _ten_q_filing()
        broken.obj_error = self._parse_failure()
        healthy = _ten_q_filing("0001-26-000013", date(2026, 7, 17))
        healthy.report = FakeTenQReport({("Part I", "Item 1"): "financial statements"})
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(FakeCompany([broken, healthy])),
            sleep_fn=lambda _s: None,
        )

        with caplog.at_level("ERROR"):
            items = client.fetch_filing_texts(
                "AAPL", ["10-Q"], as_of=datetime(2026, 7, 20, tzinfo=UTC)
            )

        assert [item.source_id for item in items] == [
            "edgar:0001-26-000012",
            "edgar:0001-26-000013",
        ]
        assert [len(item.filing_sections) for item in items] == [0, 1]

    def test_the_degraded_filing_is_exported_as_head_fallback(self):
        # The documented consequence of the fail-soft branch: with no sections
        # to prefer, the export falls back to the historic leading slice.
        filing = _ten_q_filing()
        filing.filing_text = "x" * 200
        filing.obj_error = self._parse_failure()

        item = _fetch_one_ten_q(filing)
        selection = select_filing_text(item, "10-Q", 100)

        assert selection.coverage.selection_mode == "head_fallback"
        assert selection.text == "x" * 100

    def test_the_failure_log_carries_a_traceback_and_no_edgar_identity(self, caplog):
        # `logger.exception` attaches the traceback, which is what makes an
        # issuer-specific parser break diagnosable; the identity passed to
        # `set_identity` is a credential-shaped value and must not ride along.
        filing = _ten_q_filing()
        filing.obj_error = self._parse_failure()

        with caplog.at_level("ERROR"):
            _fetch_one_ten_q(filing)

        record = next(
            record
            for record in caplog.records
            if record.getMessage().startswith(self._LOG_PREFIX)
        )
        assert record.exc_info is not None
        assert IDENTITY not in caplog.text


def _eight_k_with_exhibits(*exhibits: FakeAttachment) -> FakeFiling:
    """An 8-K whose primary document is the usual Item 2.02 notice."""
    filing = FakeFiling("0001-26-000009", "8-K", date(2026, 7, 18), date(2026, 7, 18))
    filing.filing_text = "Item 2.02 Results of Operations. See Exhibit 99.1."
    filing.documents = list(exhibits)
    return filing


def _fetch_one(
    filing: FakeFiling,
    sleep_fn: Callable[[float], None] = lambda _s: None,
    clock: FakeClock | None = None,
) -> TextItem:
    """Fetch `filing`'s single `TextItem` through the public entry point."""
    client = EdgarClient(
        IDENTITY,
        company_factory=_company_factory(FakeCompany([filing])),
        clock=clock,
        sleep_fn=sleep_fn,
    )
    return client.fetch_filing_texts(
        "AAPL", ["8-K"], as_of=datetime(2026, 7, 20, tzinfo=UTC)
    )[0]


class TestEightKExhibits:
    """Issue #128: an earnings 8-K's substance lives in Exhibit 99.1."""

    def test_exhibit_text_is_appended_to_the_primary_document_text(self):
        release = FakeAttachment(
            "EX-99.1", "release.htm", "Q2 revenue rose 8%. FY guidance raised."
        )

        item = _fetch_one(_eight_k_with_exhibits(release))

        assert item.content_text == (
            "Item 2.02 Results of Operations. See Exhibit 99.1."
            "\n\n[EXHIBIT EX-99.1 release.htm]\n"
            "Q2 revenue rose 8%. FY guidance raised."
        )

    def test_every_exhibit_ninety_nine_variant_is_collected_in_filed_order(self):
        exhibits = (
            FakeAttachment("EX-99.1", "release.htm", "press release"),
            FakeAttachment("EX-99.2", "supplement.htm", "supplemental detail"),
        )

        item = _fetch_one(_eight_k_with_exhibits(*exhibits))

        assert item.content_text.endswith(
            "\n\n[EXHIBIT EX-99.1 release.htm]\npress release"
            "\n\n[EXHIBIT EX-99.2 supplement.htm]\nsupplemental detail"
        )

    def test_exhibits_outside_the_ninety_nine_series_are_excluded(self):
        exhibits = (
            FakeAttachment("EX-10.1", "contract.htm", "material contract"),
            FakeAttachment("EX-99.1", "release.htm", "press release"),
            FakeAttachment("GRAPHIC", "logo.jpg", "binary"),
        )

        item = _fetch_one(_eight_k_with_exhibits(*exhibits))

        assert "material contract" not in item.content_text
        assert "logo.jpg" not in item.content_text
        assert item.content_text.endswith(
            "\n\n[EXHIBIT EX-99.1 release.htm]\npress release"
        )

    def test_eight_k_without_exhibits_keeps_the_primary_document_text_alone(self):
        item = _fetch_one(_eight_k_with_exhibits())

        assert item.content_text == "Item 2.02 Results of Operations. See Exhibit 99.1."

    def test_binary_exhibit_without_extractable_text_is_skipped(self):
        exhibits = (
            FakeAttachment("EX-99.1", "deck.pdf", None),
            FakeAttachment("EX-99.2", "release.htm", "press release"),
        )

        item = _fetch_one(_eight_k_with_exhibits(*exhibits))

        assert "deck.pdf" not in item.content_text
        # A slide deck holds an encoded blob, not prose: it is recognized from
        # its extension and never downloaded.
        assert exhibits[0].content_calls == 0
        assert item.content_text.endswith(
            "\n\n[EXHIBIT EX-99.2 release.htm]\npress release"
        )

    def test_text_exhibit_delivered_as_bytes_is_skipped(self):
        # Defensive: EDGAR returns text for a text extension, so this is not
        # expected. If it ever happens, guessing an encoding would risk
        # mangling the very figures the exhibit exists for.
        exhibits = (
            FakeAttachment("EX-99.1", "release.htm", b"<html>1,543,210</html>"),
            FakeAttachment("EX-99.2", "supplement.htm", "supplemental detail"),
        )

        item = _fetch_one(_eight_k_with_exhibits(*exhibits))

        assert "release.htm" not in item.content_text
        assert item.content_text.endswith(
            "\n\n[EXHIBIT EX-99.2 supplement.htm]\nsupplemental detail"
        )

    def test_blank_exhibit_text_is_skipped(self):
        item = _fetch_one(
            _eight_k_with_exhibits(FakeAttachment("EX-99.1", "empty.htm", "   \n  "))
        )

        assert item.content_text == "Item 2.02 Results of Operations. See Exhibit 99.1."

    def test_at_most_three_exhibits_are_retrieved(self):
        exhibits = [
            FakeAttachment(f"EX-99.{n}", f"doc{n}.htm", f"body {n}")
            for n in range(1, 6)
        ]

        item = _fetch_one(_eight_k_with_exhibits(*exhibits))

        assert [exhibit.content_calls for exhibit in exhibits] == [1, 1, 1, 0, 0]
        assert "doc4.htm" not in item.content_text

    def test_forms_other_than_eight_k_never_request_attachments(self):
        filing = FakeFiling(
            "0001-26-000010", "10-Q", date(2026, 7, 18), date(2026, 6, 30)
        )
        filing.documents = [FakeAttachment("EX-99.1", "release.htm", "press release")]
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(FakeCompany([filing])),
            sleep_fn=lambda _s: None,
        )

        item = client.fetch_filing_texts(
            "AAPL", ["10-Q"], as_of=datetime(2026, 7, 20, tzinfo=UTC)
        )[0]

        assert filing.attachments_calls == 0
        assert item.content_text == "Full filing text content."


def _exhibit_block(content_text: str, header: str) -> str:
    """Return just the body appended under `header`, up to the next block."""
    return content_text.split(f"[EXHIBIT {header}]\n", 1)[1].split("\n\n[EXHIBIT ", 1)[
        0
    ]


#: A furnished earnings release whose financial table is wide enough that a
#: fixed-width renderer has to drop characters to fit it. Rendered through
#: `Attachment.text()` (Issue #156) every figure below came back clipped --
#: `1,543,…`, `135,8…` -- and the unit caption read `(In thous… except per
#: share amoun…`, so no quote could be reconciled with the filing (AC16).
_WIDE_TABLE_RELEASE_HTML = """\
<html><body>
<p>Example Corp. Reports Fourth-Quarter and Full-Year Results</p>
<table>
<tr>
<td>(In thousands, except per share amounts)</td>
<td>Three Months Ended December 31, 2025</td>
<td>Three Months Ended December 31, 2024</td>
<td>Percent Change</td>
<td>Six Months Ended December 31, 2025</td>
<td>Six Months Ended December 31, 2024</td>
<td>Percent Change</td>
<td>Twelve Months Ended December 31, 2025</td>
<td>Twelve Months Ended December 31, 2024</td>
<td>Percent Change</td>
</tr>
<tr>
<td>Revenue from operations</td>
<td>1,543,210</td><td>1,498,765</td><td>3.0</td>
<td>3,087,654</td><td>2,987,123</td><td>3.4</td>
<td>6,087,654</td><td>5,987,123</td><td>1.7</td>
</tr>
<tr>
<td>Operating income</td>
<td>135,876</td><td>145,899</td><td>(6.9)</td>
<td>1,983,835</td><td>1,987,001</td><td>(0.2)</td>
<td>2,983,835</td><td>2,987,001</td><td>(0.1)</td>
</tr>
<tr>
<td>Diluted earnings per share</td>
<td>10.45</td><td>9.87</td><td>5.9</td>
<td>15.30</td><td>14.88</td><td>2.8</td>
<td>20.12</td><td>19.44</td><td>3.5</td>
</tr>
</table>
</body></html>
"""

_WIDE_TABLE_FIGURES = (
    "1,543,210",
    "1,498,765",
    "3,087,654",
    "2,987,123",
    "6,087,654",
    "5,987,123",
    "135,876",
    "145,899",
    "1,983,835",
    "1,987,001",
    "2,983,835",
    "2,987,001",
    "10.45",
    "15.30",
    "20.12",
)


class TestEightKExhibitTableFidelity:
    """Issue #156: an exhibit's table cells must survive conversion whole."""

    def _release_block(self) -> str:
        exhibit = FakeAttachment("EX-99.1", "release.htm", _WIDE_TABLE_RELEASE_HTML)

        item = _fetch_one(_eight_k_with_exhibits(exhibit))

        return _exhibit_block(item.content_text, "EX-99.1 release.htm")

    def test_every_figure_in_a_wide_table_survives_with_all_its_digits(self):
        block = self._release_block()

        assert "…" not in block
        assert [value for value in _WIDE_TABLE_FIGURES if value not in block] == []

    def test_unit_caption_is_not_clipped(self):
        assert "(In thousands, except per share amounts)" in self._release_block()

    def test_markup_without_an_html_root_is_kept_verbatim(self, caplog):
        # A stray doctype with no document under it: there is nothing to
        # convert, and dropping it would silently lose whatever it carries.
        orphan_doctype = (
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN">'
        )
        exhibit = FakeAttachment("EX-99.1", "release.htm", orphan_doctype)

        with caplog.at_level("WARNING"):
            item = _fetch_one(_eight_k_with_exhibits(exhibit))

        assert (
            _exhibit_block(item.content_text, "EX-99.1 release.htm") == orphan_doctype
        )
        assert "release.htm has no HTML root" in caplog.text

    def test_plain_text_exhibit_is_appended_unchanged(self):
        # Not every furnished exhibit is HTML; a plain-text one must not be
        # run through the HTML converter, which would empty it.
        exhibit = FakeAttachment(
            "EX-99.1", "release.txt", "Revenue from operations was 1,543,210."
        )

        item = _fetch_one(_eight_k_with_exhibits(exhibit))

        assert _exhibit_block(item.content_text, "EX-99.1 release.txt") == (
            "Revenue from operations was 1,543,210."
        )


#: `data/edgar.py::_MAX_EXHIBIT_CHARS_PER_FILING`, restated so the tests pin the
#: value rather than follow it (Issue #180).
_EXHIBIT_SAFETY_VALVE_CHARS = 500_000
#: Per-filing export ceiling used where the assertion is about the collection
#: stage alone. Deliberately above the safety valve: with the real 120,000 the
#: export stage would truncate too, and `coverage.is_truncated` could no longer
#: show that the collection-stage cut is invisible to the character counts.
_EXPORT_CEILING_ABOVE_THE_VALVE = _EXHIBIT_SAFETY_VALVE_CHARS + 100_000


class TestEightKExhibitBudget:
    """A 500,000-character safety valve per filing, shared across its exhibits.

    Issue #180: the collection stage stores the whole exhibit and leaves fitting
    the export budget to `analysis/filing_selection.py`, because a cut made here
    is persisted into `text_items.content_text`. What remains is a bound on a
    pathological document, and it still declares itself in the text.
    """

    def test_exhibit_longer_than_the_budget_is_cut_with_an_inline_marker(self):
        oversized = FakeAttachment(
            "EX-99.1", "release.htm", "x" * (_EXHIBIT_SAFETY_VALVE_CHARS + 1)
        )

        item = _fetch_one(_eight_k_with_exhibits(oversized))

        assert _exhibit_block(item.content_text, "EX-99.1 release.htm") == (
            "x" * _EXHIBIT_SAFETY_VALVE_CHARS + "\n[... exhibit truncated ...]"
        )

    @pytest.mark.parametrize(
        "exhibit_chars",
        [
            pytest.param(_EXHIBIT_SAFETY_VALVE_CHARS - 1, id="one-under-the-valve"),
            pytest.param(_EXHIBIT_SAFETY_VALVE_CHARS, id="exactly-at-the-valve"),
        ],
    )
    def test_exhibit_up_to_the_budget_is_kept_whole_and_unmarked(
        self, exhibit_chars: int
    ) -> None:
        exact = FakeAttachment("EX-99.1", "release.htm", "x" * exhibit_chars)

        item = _fetch_one(_eight_k_with_exhibits(exact))

        assert _exhibit_block(item.content_text, "EX-99.1 release.htm") == (
            "x" * exhibit_chars
        )

    @pytest.mark.parametrize(
        ("exhibit_chars", "expected"),
        [
            pytest.param(
                _EXHIBIT_SAFETY_VALVE_CHARS - 1, False, id="one-under-the-valve"
            ),
            pytest.param(_EXHIBIT_SAFETY_VALVE_CHARS, False, id="exactly-at-the-valve"),
            pytest.param(
                _EXHIBIT_SAFETY_VALVE_CHARS + 1, True, id="one-over-the-valve"
            ),
        ],
    )
    def test_the_budget_boundary_is_what_the_exported_coverage_reports(
        self, exhibit_chars: int, expected: bool
    ) -> None:
        # Issue #157: the collection-stage cut is invisible to the character
        # counts (the truncated text *is* the "original"), so the marker in
        # `content_text` is what carries it across into `coverage`.
        exhibit = FakeAttachment("EX-99.1", "release.htm", "x" * exhibit_chars)

        item = _fetch_one(_eight_k_with_exhibits(exhibit))
        selection = select_filing_text(item, "8-K", _EXPORT_CEILING_ABOVE_THE_VALVE)

        assert selection.coverage.is_truncated is False
        assert selection.coverage.selection_mode == "full"
        assert selection.coverage.exhibit_truncated is expected

    def test_exhausted_budget_skips_the_next_exhibit_without_downloading_it(self):
        exhibits = (
            FakeAttachment("EX-99.1", "release.htm", "a" * _EXHIBIT_SAFETY_VALVE_CHARS),
            FakeAttachment("EX-99.2", "supplement.htm", "b" * 100),
        )

        item = _fetch_one(_eight_k_with_exhibits(*exhibits))

        assert exhibits[1].content_calls == 0
        assert "supplement.htm" not in item.content_text

    def test_an_exhibit_dropped_whole_by_the_budget_is_still_marked(self):
        # The skipped exhibit leaves no text of its own to mark, so without an
        # explicit marker the filing would claim to be complete (Issue #157).
        exhibits = (
            FakeAttachment("EX-99.1", "release.htm", "a" * _EXHIBIT_SAFETY_VALVE_CHARS),
            FakeAttachment("EX-99.2", "supplement.htm", "b" * 100),
        )

        item = _fetch_one(_eight_k_with_exhibits(*exhibits))
        selection = select_filing_text(item, "8-K", _EXPORT_CEILING_ABOVE_THE_VALVE)

        assert item.content_text.endswith("\n[... exhibit truncated ...]")
        assert selection.coverage.is_truncated is False
        assert selection.coverage.exhibit_truncated is True

    def test_a_cut_exhibit_that_exhausts_the_budget_is_marked_only_once(self):
        exhibits = (
            FakeAttachment(
                "EX-99.1", "release.htm", "a" * (_EXHIBIT_SAFETY_VALVE_CHARS - 10)
            ),
            FakeAttachment("EX-99.2", "supplement.htm", "b" * 100),
            FakeAttachment("EX-99.3", "slides.htm", "c" * 50),
        )

        item = _fetch_one(_eight_k_with_exhibits(*exhibits))

        assert item.content_text.count("[... exhibit truncated ...]") == 1

    def test_partially_spent_budget_truncates_the_next_exhibit(self):
        exhibits = (
            FakeAttachment(
                "EX-99.1", "release.htm", "a" * (_EXHIBIT_SAFETY_VALVE_CHARS - 10)
            ),
            FakeAttachment("EX-99.2", "supplement.htm", "b" * 100),
        )

        item = _fetch_one(_eight_k_with_exhibits(*exhibits))

        assert _exhibit_block(item.content_text, "EX-99.2 supplement.htm") == (
            "b" * 10 + "\n[... exhibit truncated ...]"
        )

    def test_a_filing_past_the_old_export_budget_is_stored_whole(self):
        # Issue #180's point: everything between the old 60,000 ceiling and the
        # safety valve now reaches `content_text` intact, so the export stage --
        # not the collection stage -- decides what a reader sees. The largest
        # 8-K measured in the Issue #165 replay was 375,403 characters.
        measured_worst_case = 375_403
        exhibit = FakeAttachment("EX-99.1", "release.htm", "x" * measured_worst_case)

        item = _fetch_one(_eight_k_with_exhibits(exhibit))
        selection = select_filing_text(item, "8-K", 120_000)

        assert "exhibit truncated" not in item.content_text
        assert _exhibit_block(item.content_text, "EX-99.1 release.htm") == (
            "x" * measured_worst_case
        )
        # The export stage takes over from here: the ceiling is unchanged, so
        # the same filing is now cut where a `coverage` reader can see it.
        assert selection.coverage.exhibit_truncated is False
        assert selection.coverage.is_truncated is True
        assert selection.coverage.exported_chars == 120_000


class TestEightKExhibitCountCap:
    """Issue #163: three exhibits per filing, and the rest must be declared."""

    _OMISSION_MARKER = "\n[... exhibit omitted: per-filing exhibit count cap ...]"

    def _exhibits(self, count: int) -> tuple[FakeAttachment, ...]:
        return tuple(
            FakeAttachment(f"EX-99.{n}", f"doc{n}.htm", f"body {n}")
            for n in range(1, count + 1)
        )

    @pytest.mark.parametrize(
        ("exhibit_count", "expected"),
        [
            pytest.param(2, False, id="one-under-the-cap"),
            pytest.param(3, False, id="exactly-at-the-cap"),
            pytest.param(4, True, id="one-over-the-cap"),
        ],
    )
    def test_the_count_boundary_is_what_the_exported_coverage_reports(
        self, exhibit_count: int, expected: bool
    ) -> None:
        # Like the character ceiling, the count cap cuts before export, so the
        # character counts see nothing and only the marker crosses over.
        item = _fetch_one(_eight_k_with_exhibits(*self._exhibits(exhibit_count)))
        selection = select_filing_text(item, "8-K", 120_000)

        assert selection.coverage.is_truncated is False
        assert selection.coverage.selection_mode == "full"
        assert selection.coverage.exhibit_truncated is expected

    def test_a_filing_at_the_cap_keeps_every_exhibit_and_stays_unmarked(self):
        item = _fetch_one(_eight_k_with_exhibits(*self._exhibits(3)))

        assert "exhibit omitted" not in item.content_text
        assert item.content_text.endswith("\n\n[EXHIBIT EX-99.3 doc3.htm]\nbody 3")

    def test_exhibits_past_the_cap_are_declared_without_being_downloaded(self):
        exhibits = self._exhibits(5)

        item = _fetch_one(_eight_k_with_exhibits(*exhibits))

        assert [exhibit.content_calls for exhibit in exhibits] == [1, 1, 1, 0, 0]
        assert item.content_text.endswith(self._OMISSION_MARKER)

    def test_the_two_collection_caps_stay_distinguishable_in_the_text(self):
        # One filing that hit both ceilings: 99.1 exhausts the character
        # budget and 99.4 is past the count cap. A shortened exhibit and one
        # that was never fetched call for different readings, so the text
        # keeps them apart even though `coverage` reports one boolean.
        exhibits = (
            FakeAttachment(
                "EX-99.1", "release.htm", "a" * (_EXHIBIT_SAFETY_VALVE_CHARS + 10_000)
            ),
            FakeAttachment("EX-99.2", "supplement.htm", "b" * 100),
            FakeAttachment("EX-99.3", "slides.htm", "c" * 100),
            FakeAttachment("EX-99.4", "tables.htm", "d" * 100),
        )

        item = _fetch_one(_eight_k_with_exhibits(*exhibits))

        assert item.content_text.count("[... exhibit truncated ...]") == 1
        assert item.content_text.endswith(self._OMISSION_MARKER)

    def test_a_failed_exhibit_download_does_not_swallow_the_count_cap(self, caplog):
        # The cap is decided from the attachment list alone: a fail-soft
        # download failure must not also erase the exhibits that were never
        # offered a chance to be fetched.
        exhibits = (
            FakeAttachment("EX-99.1", "release.htm", "press release"),
            FakeAttachment("EX-99.2", "gone.htm", error=ConnectionError("404")),
            FakeAttachment("EX-99.3", "slides.htm", "slides"),
            FakeAttachment("EX-99.4", "tables.htm", "tables"),
        )

        with caplog.at_level("ERROR"):
            item = _fetch_one(_eight_k_with_exhibits(*exhibits))

        assert "keeping 1 exhibit(s)" in caplog.text
        assert item.content_text.endswith(self._OMISSION_MARKER)


class TestEightKExhibitFailSoft:
    def test_unavailable_attachment_list_falls_back_to_the_primary_document(
        self, caplog
    ):
        filing = _eight_k_with_exhibits()
        filing.attachments_error = ConnectionError("EDGAR attachment index unavailable")

        with caplog.at_level("ERROR"):
            item = _fetch_one(filing)

        assert item.content_text == "Item 2.02 Results of Operations. See Exhibit 99.1."
        assert "Exhibit retrieval failed for accession 0001-26-000009" in caplog.text

    def test_failing_exhibit_keeps_the_exhibits_already_retrieved(self, caplog):
        exhibits = (
            FakeAttachment("EX-99.1", "release.htm", "press release"),
            FakeAttachment("EX-99.2", "gone.htm", error=ConnectionError("404")),
        )

        with caplog.at_level("ERROR"):
            item = _fetch_one(_eight_k_with_exhibits(*exhibits))

        assert item.content_text.endswith(
            "\n\n[EXHIBIT EX-99.1 release.htm]\npress release"
        )
        assert "keeping 1 exhibit(s)" in caplog.text

    def test_exhibit_fetch_failure_does_not_abort_the_remaining_filings(self, caplog):
        broken = _eight_k_with_exhibits()
        broken.attachments_error = ConnectionError("EDGAR attachment index unavailable")
        healthy = FakeFiling(
            "0001-26-000011", "8-K", date(2026, 7, 17), date(2026, 7, 17)
        )
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(FakeCompany([broken, healthy])),
            sleep_fn=lambda _s: None,
        )

        with caplog.at_level("ERROR"):
            items = client.fetch_filing_texts(
                "AAPL", ["8-K"], as_of=datetime(2026, 7, 20, tzinfo=UTC)
            )

        assert [item.source_id for item in items] == [
            "edgar:0001-26-000009",
            "edgar:0001-26-000011",
        ]

    def test_validation_error_on_an_exhibit_is_not_retried(self):
        exhibit = FakeAttachment("EX-99.1", "release.htm", error=ValueError("bad path"))
        sleeps: list[float] = []
        # Spaced far enough apart that no sleep can come from the throttle,
        # so an empty `sleeps` proves no retry backoff happened.
        clock = FakeClock([0.0, 5.0, 10.0, 15.0])

        item = _fetch_one(
            _eight_k_with_exhibits(exhibit), sleep_fn=sleeps.append, clock=clock
        )

        assert exhibit.content_calls == 1
        assert sleeps == []
        assert item.content_text == "Item 2.02 Results of Operations. See Exhibit 99.1."

    def test_transient_exhibit_failure_is_retried_before_giving_up(self):
        exhibit = FakeAttachment(
            "EX-99.1", "release.htm", error=ConnectionError("EDGAR timeout")
        )
        sleeps: list[float] = []
        clock = FakeClock([0.0, 5.0, 10.0, 15.0, 20.0, 25.0])

        item = _fetch_one(
            _eight_k_with_exhibits(exhibit), sleep_fn=sleeps.append, clock=clock
        )

        assert exhibit.content_calls == 3
        assert sleeps == [1.0, 2.0]
        assert item.content_text == "Item 2.02 Results of Operations. See Exhibit 99.1."


class TestEightKExhibitRateLimiting:
    def test_throttles_the_attachment_index_and_every_exhibit_download(self):
        """Rate limiting applies to every attempt, exhibit downloads included."""
        exhibits = (
            FakeAttachment("EX-99.1", "release.htm", "press release"),
            FakeAttachment("EX-99.2", "supplement.htm", "supplemental detail"),
        )
        sleeps: list[float] = []
        # One tick per throttled request: get_filings, filing.text,
        # filing.attachments, and one per exhibit download.
        clock = FakeClock([0.0, 0.0, 0.0, 0.0, 0.0])

        _fetch_one(
            _eight_k_with_exhibits(*exhibits), sleep_fn=sleeps.append, clock=clock
        )

        assert sleeps == [pytest.approx(0.1)] * 4
