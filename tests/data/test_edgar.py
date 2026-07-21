"""Tests for the EDGAR fundamentals/filings adapter (FR-03)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest

from swing_copilot.data.edgar import EdgarClient, FilingRef
from swing_copilot.storage.market_store import FundamentalsRecord

if TYPE_CHECKING:
    from collections.abc import Callable

IDENTITY = "swing-copilot tester tmasuyama1114@gmail.com"


class FakeFinancials:
    def __init__(self, **values: float | None) -> None:
        self._values = values

    def get_revenue(self):
        return self._values.get("revenue")

    def get_net_income(self):
        return self._values.get("net_income")

    def get_free_cash_flow(self):
        return self._values.get("fcf")

    def get_operating_cash_flow(self):
        return self._values.get("ocf")

    def get_capital_expenditures(self):
        return self._values.get("capex")

    def get_stockholders_equity(self):
        return self._values.get("equity")

    def get_total_assets(self):
        return self._values.get("assets")

    def get_shares_outstanding_basic(self):
        return self._values.get("shares")


class FakeFilingObj:
    def __init__(self, financials: FakeFinancials) -> None:
        self.financials = financials


class FakeFiling:
    DEFAULT_URL = "https://www.sec.gov/example"

    def __init__(
        self,
        accession_number: str,
        form: str,
        filing_date: date,
        period_of_report: date,
        financials: FakeFinancials,
    ) -> None:
        self.accession_number = accession_number
        self.form = form
        self.filing_date = filing_date
        self.period_of_report = period_of_report
        self.filing_url = self.DEFAULT_URL
        self._obj = FakeFilingObj(financials)

    def obj(self):
        return self._obj


class FakeCompany:
    def __init__(self, filings: list[FakeFiling]) -> None:
        self._filings = filings
        self.get_filings_calls: list[list[str]] = []

    def get_filings(self, *, form):
        self.get_filings_calls.append(list(form))
        return list(self._filings)


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
        financials = FakeFinancials(
            revenue=100.0,
            net_income=20.0,
            fcf=15.0,
            equity=500.0,
            assets=1000.0,
            shares=1_000_000.0,
        )
        filing = FakeFiling(
            "0001-26-000001",
            "10-Q",
            date(2026, 7, 10),
            date(2026, 6, 30),
            financials,
        )
        company = FakeCompany([filing])
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
                source_url="https://www.sec.gov/example",
                fetched_at=records[0].fetched_at,
            )
        ]

    def test_requests_10k_and_10q_forms(self):
        company = FakeCompany([])
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert company.get_filings_calls == [["10-K", "10-Q"]]

    def test_excludes_filings_filed_after_as_of(self):
        financials = FakeFinancials(revenue=1.0)
        old_filing = FakeFiling(
            "0001-26-000001", "10-Q", date(2026, 7, 10), date(2026, 6, 30), financials
        )
        future_filing = FakeFiling(
            "0001-26-000002", "10-Q", date(2026, 7, 25), date(2026, 6, 30), financials
        )
        company = FakeCompany([old_filing, future_filing])
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert [record.accession_no for record in records] == ["0001-26-000001"]

    def test_missing_fcf_falls_back_to_operating_cash_flow_minus_capex(self):
        financials = FakeFinancials(revenue=1.0, ocf=50.0, capex=10.0)
        filing = FakeFiling(
            "0001-26-000001", "10-Q", date(2026, 7, 10), date(2026, 6, 30), financials
        )
        company = FakeCompany([filing])
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert records[0].fcf == pytest.approx(40.0)

    def test_fcf_is_none_when_no_source_data_is_available(self):
        financials = FakeFinancials(revenue=1.0)
        filing = FakeFiling(
            "0001-26-000001", "10-Q", date(2026, 7, 10), date(2026, 6, 30), financials
        )
        company = FakeCompany([filing])
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        records = client.fetch_fundamentals("AAPL", datetime(2026, 7, 20, tzinfo=UTC))

        assert records[0].fcf is None


class TestFetchRecentFilings:
    def test_returns_filing_refs_for_requested_form_types(self):
        financials = FakeFinancials()
        filing = FakeFiling(
            "0001-26-000003", "8-K", date(2026, 7, 18), date(2026, 7, 18), financials
        )
        company = FakeCompany([filing])
        client = EdgarClient(
            IDENTITY,
            company_factory=_company_factory(company),
            sleep_fn=lambda _s: None,
        )

        refs = client.fetch_recent_filings("AAPL", ["8-K"])

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
