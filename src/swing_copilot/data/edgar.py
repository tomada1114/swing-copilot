"""SEC EDGAR client: point-in-time fundamentals and recent filings (FR-03).

Wraps `edgartools`, whose `Financials` object exposes direct getters
(`get_revenue()`, `get_net_income()`, ...) for both the company's latest
period (`Company.get_financials()`) and one specific filing's period
(`filing.obj().financials`) — verified against the installed edgartools
5.36.0. `get_free_cash_flow()` returned `None` for every real filing tried
during implementation (its underlying XBRL concept lookup did not resolve),
so `fcf` falls back to `operating_cash_flow - capital_expenditures` when
both are available, and stays `None` (a valid, nullable column) otherwise —
never a guessed value.

Requests are throttled to at most 10/second (SEC fair-access, `docs/00_human_
preparation.md`) via an injectable clock/sleep pair so the throttle itself is
unit-testable without real waiting.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

import edgar

from swing_copilot.storage.market_store import FundamentalsRecord
from swing_copilot.text.base import TextItem

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

_FUNDAMENTALS_FORMS = ("10-K", "10-Q")
_MIN_REQUEST_INTERVAL_SECONDS = 0.1  # 10 requests/second cap


class _FinancialsLike(Protocol):
    def get_revenue(self) -> float | None: ...  # pragma: no cover
    def get_net_income(self) -> float | None: ...  # pragma: no cover
    def get_free_cash_flow(self) -> float | None: ...  # pragma: no cover
    def get_operating_cash_flow(self) -> float | None: ...  # pragma: no cover
    def get_capital_expenditures(self) -> float | None: ...  # pragma: no cover
    def get_stockholders_equity(self) -> float | None: ...  # pragma: no cover
    def get_total_assets(self) -> float | None: ...  # pragma: no cover
    def get_shares_outstanding_basic(self) -> float | None: ...  # pragma: no cover


class _FilingObjLike(Protocol):
    financials: _FinancialsLike


class _FilingLike(Protocol):
    accession_number: str
    form: str
    filing_date: date
    period_of_report: date
    filing_url: str

    def obj(self) -> _FilingObjLike: ...  # pragma: no cover
    def text(self) -> str: ...  # pragma: no cover


class _CompanyLike(Protocol):
    def get_filings(self, *, form: list[str]) -> list[_FilingLike]:
        """Return filings matching the requested form types."""
        ...  # pragma: no cover


@dataclass(frozen=True, slots=True)
class FilingRef:
    """A recent filing reference, used by text collection (FR-07)."""

    accession_no: str
    symbol: str
    form: str
    filed_at: datetime
    source_url: str


def _to_utc_datetime(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time(), tzinfo=UTC)


def _extract_fcf(financials: _FinancialsLike) -> float | None:
    fcf = financials.get_free_cash_flow()
    if fcf is not None:
        return fcf
    operating_cash_flow = financials.get_operating_cash_flow()
    capex = financials.get_capital_expenditures()
    if operating_cash_flow is not None and capex is not None:
        return operating_cash_flow - capex
    return None


class EdgarClient:
    """Throttled, point-in-time SEC EDGAR fundamentals/filings client."""

    def __init__(
        self,
        identity: str,
        *,
        company_factory: Callable[[str], _CompanyLike] = edgar.Company,
        clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        """Create the client and declare `identity` to EDGAR.

        Args:
            identity: SEC-required User-Agent identity (`"Name email"`).
            company_factory: Injectable `edgar.Company` constructor, used by
                tests to avoid real network calls.
            clock: Injectable monotonic clock for rate-limit tests.
            sleep_fn: Injectable sleep function for rate-limit tests.
        """
        edgar.set_identity(identity)
        self._company_factory = company_factory
        self._clock = clock or time.monotonic
        self._sleep_fn = sleep_fn or time.sleep
        self._last_request_at: float | None = None

    def _throttle(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            wait = _MIN_REQUEST_INTERVAL_SECONDS - (now - self._last_request_at)
            if wait > 0:
                self._sleep_fn(wait)
        self._last_request_at = now

    def fetch_fundamentals(
        self, symbol: str, as_of: datetime
    ) -> list[FundamentalsRecord]:
        """Fetch normalized fundamentals filed on or before `as_of`.

        Args:
            symbol: Ticker symbol.
            as_of: Only filings with `filed_at <= as_of` are returned.

        Returns:
            One `FundamentalsRecord` per qualifying 10-K/10-Q filing.
        """
        self._throttle()
        company = self._company_factory(symbol)
        filings = company.get_filings(form=list(_FUNDAMENTALS_FORMS))

        records = []
        for filing in filings:
            filed_at = _to_utc_datetime(filing.filing_date)
            if filed_at > as_of:
                continue
            self._throttle()
            financials = filing.obj().financials
            records.append(
                FundamentalsRecord(
                    accession_no=filing.accession_number,
                    symbol=symbol,
                    form=filing.form,
                    fiscal_period_end=filing.period_of_report,
                    filed_at=filed_at,
                    revenue=financials.get_revenue(),
                    net_income=financials.get_net_income(),
                    fcf=_extract_fcf(financials),
                    equity=financials.get_stockholders_equity(),
                    assets=financials.get_total_assets(),
                    shares=financials.get_shares_outstanding_basic(),
                    source_url=filing.filing_url,
                    fetched_at=datetime.now(UTC),
                )
            )
        return records

    def fetch_recent_filings(
        self, symbol: str, form_types: list[str]
    ) -> list[FilingRef]:
        """Return recent filing references for the given form types (FR-07).

        Args:
            symbol: Ticker symbol.
            form_types: SEC form types to fetch (e.g. `["8-K"]`).

        Returns:
            One `FilingRef` per matching filing.
        """
        self._throttle()
        company = self._company_factory(symbol)
        filings = company.get_filings(form=form_types)
        return [
            FilingRef(
                accession_no=filing.accession_number,
                symbol=symbol,
                form=filing.form,
                filed_at=_to_utc_datetime(filing.filing_date),
                source_url=filing.filing_url,
            )
            for filing in filings
        ]

    def fetch_filing_texts(self, symbol: str, form_types: list[str]) -> list[TextItem]:
        """Return recent filings' full text, normalized for text collection (FR-07).

        Args:
            symbol: Ticker symbol.
            form_types: SEC form types to fetch (e.g. `["8-K", "10-Q"]`).

        Returns:
            One `TextItem` per matching filing (`source_type="filing"`).
        """
        self._throttle()
        company = self._company_factory(symbol)
        filings = company.get_filings(form=form_types)

        items = []
        for filing in filings:
            self._throttle()
            items.append(
                TextItem(
                    source_id=f"edgar:{filing.accession_number}",
                    symbol=symbol,
                    source_type="filing",
                    published_at=_to_utc_datetime(filing.filing_date),
                    title=f"{filing.form} - {symbol}",
                    source_url=filing.filing_url,
                    content_text=filing.text(),
                    fetched_at=datetime.now(UTC),
                )
            )
        return items
