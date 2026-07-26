"""SEC EDGAR client: point-in-time fundamentals and recent filings (FR-03).

`fetch_fundamentals` uses `Company.get_facts()` (one bulk `companyfacts.json`
request per symbol, verified against the installed edgartools 5.40.1) instead
of listing every historical 10-K/10-Q filing and parsing each one's XBRL
document individually — the previous approach made 1 + N requests per symbol
(N = every historical filing, unbounded) and was the dominant cost of a daily
run. Each `FinancialFact` already carries its own filing metadata (`accession`,
`form_type`, `filing_date`), so filings are reconstructed by grouping facts on
`accession` rather than via a separate `get_filings()` call.

There is no standard US-GAAP concept for free cash flow, so `fcf` is always
`operating_cash_flow - capital_expenditures` when both are available, and
`None` (a valid, nullable column) otherwise — never a guessed value.

A company's XBRL data frequently tags both a quarterly-only and a
cumulative year-to-date duration fact ending on the same `period_end` (for
example a 10-Q's "3 months ended" and "6 months ended" revenue). When more
than one non-dimensioned candidate remains for a concept, the shortest
duration is preferred, which favors the discrete-quarter figure over the
cumulative one; ties fall back to fact order (deterministic, not otherwise
meaningful).

Requests are throttled to at most 10/second (SEC fair-access, `docs/00_human_
preparation.md`) via an injectable clock/sleep pair so the throttle itself is
unit-testable without real waiting.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

import edgar

from swing_copilot.clock import SystemClock
from swing_copilot.storage.market_store import FundamentalsRecord
from swing_copilot.text.base import TextItem

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    from swing_copilot.clock import Clock

_FUNDAMENTALS_FORMS = ("10-K", "10-Q")
_FINANCIAL_TAXONOMY_PREFIX = "us-gaap:"
_MIN_REQUEST_INTERVAL_SECONDS = 0.1  # 10 requests/second cap
_RETRY_DELAYS_SECONDS = (1.0, 2.0)  # 3 total attempts
_DEFAULT_FUNDAMENTALS_LOOKBACK_DAYS = 400  # SEC filing lookback window; owned independently of pipeline/daily.py's price-history lookback
_SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar"

# US-GAAP concept tag variants, tried in priority order per metric. Facts are
# matched by the concept's local name (namespace prefix such as `us-gaap:` is
# stripped), so a single list covers both `us-gaap` and any equivalent tag.
_REVENUE_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
)
_NET_INCOME_CONCEPTS = ("NetIncomeLoss", "ProfitLoss")
_EQUITY_CONCEPTS = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)
_ASSETS_CONCEPTS = ("Assets",)
_SHARES_CONCEPTS = (
    "WeightedAverageNumberOfSharesOutstandingBasic",
    "WeightedAverageNumberOfSharesOutstandingBasicAndDiluted",
    "CommonStockSharesOutstanding",
)
_OPERATING_CASH_FLOW_CONCEPTS = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
_CAPITAL_EXPENDITURES_CONCEPTS = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsForCapitalImprovements",
    "PaymentsToAcquireProductiveAssets",
)


class _FilingLike(Protocol):
    accession_number: str
    form: str
    filing_date: date
    period_of_report: date
    filing_url: str

    def text(self) -> str: ...  # pragma: no cover


class _FactLike(Protocol):
    """One XBRL fact, shaped like edgartools' `FinancialFact`."""

    concept: str
    accession: str
    form_type: str
    filing_date: date | None
    period_start: date | None
    period_end: date | None
    numeric_value: float | None
    dimensions: dict[str, str] | None


class _EntityFactsLike(Protocol):
    """Bulk per-company facts, shaped like edgartools' `EntityFacts`."""

    cik: int

    def get_all_facts(self) -> list[_FactLike]: ...  # pragma: no cover


class _CompanyLike(Protocol):
    def get_filings(self, *, form: list[str]) -> list[_FilingLike]:
        """Return filings matching the requested form types."""
        ...  # pragma: no cover

    def get_facts(self) -> _EntityFactsLike | None:
        """Return this company's bulk XBRL facts, or `None` if it has none."""
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


def _extract_fcf(
    operating_cash_flow: float | None, capital_expenditures: float | None
) -> float | None:
    if operating_cash_flow is not None and capital_expenditures is not None:
        return operating_cash_flow - capital_expenditures
    return None


def _duration_days(fact: _FactLike) -> int:
    """Length of the fact's reporting period, `0` for instant facts.

    Used to prefer the discrete-quarter fact over a same-`period_end`
    cumulative year-to-date fact when a concept has both (see module
    docstring).
    """
    if fact.period_start is None or fact.period_end is None:
        return 0
    return (fact.period_end - fact.period_start).days


def _pick_concept_value(
    by_concept: dict[str, list[_FactLike]],
    concept_variants: tuple[str, ...],
    fiscal_period_end: date,
) -> float | None:
    """Return the best-matching numeric value for one metric in one filing.

    Tries `concept_variants` in priority order; within the first variant that
    has any usable candidate, excludes dimensioned (segment-level) facts and
    facts whose `period_end` does not match this filing's reporting period,
    then prefers the shortest remaining duration (see `_duration_days`).
    """
    for concept in concept_variants:
        candidates = [
            fact
            for fact in by_concept.get(concept, ())
            if fact.period_end == fiscal_period_end
            and not fact.dimensions
            and fact.numeric_value is not None
        ]
        if candidates:
            return min(candidates, key=_duration_days).numeric_value
    return None


def _filing_index_url(cik: int, accession_no: str) -> str:
    """Build the filing's SEC index page URL.

    The accession number appears twice in different forms: without dashes as
    the folder segment, and with dashes (plus a `.htm`, not `.html`,
    extension) in the filename itself -- verified against the live SEC site;
    the dashed-only form 503s.
    """
    accession_folder = accession_no.replace("-", "")
    return f"{_SEC_ARCHIVE_URL}/data/{cik}/{accession_folder}/{accession_no}-index.htm"


@dataclass(frozen=True, slots=True)
class _FilingFacts:
    """One qualifying filing, reconstructed from its own XBRL facts."""

    accession_no: str
    form: str
    filed_at: datetime
    fiscal_period_end: date
    by_concept: dict[str, list[_FactLike]]


def _group_facts_by_filing(facts: list[_FactLike]) -> list[_FilingFacts]:
    """Reconstruct one `_FilingFacts` per accession from a flat fact list.

    Facts missing the metadata needed to attribute them to a specific filing
    and period (`accession`, `filing_date`, `period_end`) or filed under a
    form outside `_FUNDAMENTALS_FORMS` are dropped rather than raised on:
    they simply cannot contribute to a filing-level record.

    `fiscal_period_end` is derived from `us-gaap` facts only. A filing's flat
    fact list also carries `dei` (cover-page) facts such as
    `dei:EntityCommonStockSharesOutstanding`, an `instant` fact dated weeks
    after the quarter's actual period end (the filing date, not the fiscal
    period). Including those in the `max(period_end)` computation lets a
    cover-page fact silently hijack the derived period end, which then makes
    every financial concept's exact-match lookup in `_pick_concept_value`
    (which all use `us-gaap` concepts) fail and every metric come back `None`
    -- even though the filing does have well-formed financial facts.
    """
    grouped: dict[str, list[_FactLike]] = defaultdict(list)
    for fact in facts:
        if (
            fact.form_type not in _FUNDAMENTALS_FORMS
            or not fact.accession
            or fact.filing_date is None
            or fact.period_end is None
        ):
            continue
        grouped[fact.accession].append(fact)

    filings = []
    for accession_no, group_facts in grouped.items():
        # The filter above guarantees every fact in this group has a
        # non-`None` `filing_date`/`period_end`; re-filter here so mypy can
        # narrow the type without a `None`-can't-happen assert.
        filing_dates = [f.filing_date for f in group_facts if f.filing_date is not None]
        financial_period_ends = [
            f.period_end
            for f in group_facts
            if f.period_end is not None
            and f.concept.startswith(_FINANCIAL_TAXONOMY_PREFIX)
        ]
        if not financial_period_ends:
            # No us-gaap fact in this filing at all (e.g. a filing that only
            # ever surfaced dei cover-page facts): there is no financial
            # period to derive, so this accession cannot contribute a record.
            continue
        by_concept: dict[str, list[_FactLike]] = defaultdict(list)
        for fact in group_facts:
            by_concept[fact.concept.rsplit(":", 1)[-1]].append(fact)
        filings.append(
            _FilingFacts(
                accession_no=accession_no,
                form=group_facts[0].form_type,
                filed_at=_to_utc_datetime(filing_dates[0]),
                fiscal_period_end=max(financial_period_ends),
                by_concept=by_concept,
            )
        )
    return filings


class EdgarClient:
    """Throttled, point-in-time SEC EDGAR fundamentals/filings client."""

    def __init__(
        self,
        identity: str,
        *,
        company_factory: Callable[[str], _CompanyLike] = edgar.Company,
        clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        date_clock: Clock | None = None,
    ) -> None:
        """Create the client and declare `identity` to EDGAR.

        Args:
            identity: SEC-required User-Agent identity (`"Name email"`).
            company_factory: Injectable `edgar.Company` constructor, used by
                tests to avoid real network calls.
            clock: Injectable monotonic clock for rate-limit tests.
            sleep_fn: Injectable sleep function for rate-limit tests.
            date_clock: Injectable wall clock for deterministic fetch timestamps.
        """
        edgar.set_identity(identity)
        self._company_factory = company_factory
        self._clock = clock or time.monotonic
        self._sleep_fn = sleep_fn or time.sleep
        self._date_clock = date_clock or SystemClock()
        self._last_request_at: float | None = None

    def _throttle(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            wait = _MIN_REQUEST_INTERVAL_SECONDS - (now - self._last_request_at)
            if wait > 0:
                self._sleep_fn(wait)
        self._last_request_at = now

    def _with_retries[T](self, operation: Callable[[], T]) -> T:
        """Run one EDGAR boundary operation with a bounded retry policy."""
        for delay in _RETRY_DELAYS_SECONDS:
            self._throttle()
            try:
                return operation()
            except Exception:
                self._sleep_fn(delay)
        self._throttle()
        return operation()

    def fetch_fundamentals(
        self,
        symbol: str,
        as_of: datetime,
        *,
        lookback_days: int = _DEFAULT_FUNDAMENTALS_LOOKBACK_DAYS,
    ) -> list[FundamentalsRecord]:
        """Fetch normalized fundamentals filed within the lookback window.

        Issues one bulk company-facts request (`Company.get_facts()`) instead
        of listing every historical filing and parsing each one's XBRL
        document, then reconstructs qualifying filings by grouping facts on
        their own `accession` metadata.

        Args:
            symbol: Ticker symbol.
            as_of: Only filings with `filed_at <= as_of` are returned
                (inclusive).
            lookback_days: Only filings with
                `filed_at >= as_of - lookback_days` are returned (inclusive).
                The bulk request itself always returns full history; this
                only bounds which filings are converted into records.

        Returns:
            One `FundamentalsRecord` per qualifying 10-K/10-Q filing, oldest
            first. Empty if the symbol has no XBRL facts on file.
        """
        entity_facts = self._with_retries(
            lambda: self._company_factory(symbol).get_facts()
        )
        if entity_facts is None:
            return []

        earliest_filed_at = as_of - timedelta(days=lookback_days)
        records = []
        for filing in _group_facts_by_filing(entity_facts.get_all_facts()):
            if filing.filed_at > as_of or filing.filed_at < earliest_filed_at:
                continue
            operating_cash_flow = _pick_concept_value(
                filing.by_concept,
                _OPERATING_CASH_FLOW_CONCEPTS,
                filing.fiscal_period_end,
            )
            capital_expenditures = _pick_concept_value(
                filing.by_concept,
                _CAPITAL_EXPENDITURES_CONCEPTS,
                filing.fiscal_period_end,
            )
            records.append(
                FundamentalsRecord(
                    accession_no=filing.accession_no,
                    symbol=symbol,
                    form=filing.form,
                    fiscal_period_end=filing.fiscal_period_end,
                    filed_at=filing.filed_at,
                    revenue=_pick_concept_value(
                        filing.by_concept, _REVENUE_CONCEPTS, filing.fiscal_period_end
                    ),
                    net_income=_pick_concept_value(
                        filing.by_concept,
                        _NET_INCOME_CONCEPTS,
                        filing.fiscal_period_end,
                    ),
                    fcf=_extract_fcf(operating_cash_flow, capital_expenditures),
                    equity=_pick_concept_value(
                        filing.by_concept, _EQUITY_CONCEPTS, filing.fiscal_period_end
                    ),
                    assets=_pick_concept_value(
                        filing.by_concept, _ASSETS_CONCEPTS, filing.fiscal_period_end
                    ),
                    shares=_pick_concept_value(
                        filing.by_concept, _SHARES_CONCEPTS, filing.fiscal_period_end
                    ),
                    source_url=_filing_index_url(entity_facts.cik, filing.accession_no),
                    fetched_at=self._date_clock.now(),
                )
            )
        records.sort(key=lambda record: record.filed_at)
        return records

    def fetch_recent_filings(
        self, symbol: str, form_types: list[str], *, as_of: datetime
    ) -> list[FilingRef]:
        """Return recent filing references for the given form types (FR-07).

        Args:
            symbol: Ticker symbol.
            form_types: SEC form types to fetch (e.g. `["8-K"]`).
            as_of: Only filings submitted at or before this instant are returned.

        Returns:
            One `FilingRef` per matching filing.
        """
        filings = self._with_retries(
            lambda: self._company_factory(symbol).get_filings(form=form_types)
        )
        return [
            FilingRef(
                accession_no=filing.accession_number,
                symbol=symbol,
                form=filing.form,
                filed_at=_to_utc_datetime(filing.filing_date),
                source_url=filing.filing_url,
            )
            for filing in filings
            if _to_utc_datetime(filing.filing_date) <= as_of
        ]

    def fetch_filing_texts(
        self, symbol: str, form_types: list[str], *, as_of: datetime
    ) -> list[TextItem]:
        """Return recent filings' full text, normalized for text collection (FR-07).

        Args:
            symbol: Ticker symbol.
            form_types: SEC form types to fetch (e.g. `["8-K", "10-Q"]`).
            as_of: Only filings submitted at or before this instant are returned.

        Returns:
            One `TextItem` per matching filing (`source_type="filing"`).
        """
        filings = self._with_retries(
            lambda: self._company_factory(symbol).get_filings(form=form_types)
        )

        items = []
        for filing in filings:
            if _to_utc_datetime(filing.filing_date) > as_of:
                continue
            items.append(
                TextItem(
                    source_id=f"edgar:{filing.accession_number}",
                    symbol=symbol,
                    source_type="filing",
                    published_at=_to_utc_datetime(filing.filing_date),
                    title=f"{filing.form} - {symbol}",
                    source_url=filing.filing_url,
                    content_text=self._with_retries(filing.text),
                    fetched_at=self._date_clock.now(),
                )
            )
        return items
