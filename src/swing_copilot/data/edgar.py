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

An earnings 8-K's primary document is usually only the Item 2.02 notice that a
press release was furnished; the revenue/EPS figures, the full-year guidance,
and management's demand commentary all live in Exhibit 99.1 (Issue #128). So
for 8-K forms the `EX-99*` exhibits are downloaded and appended to the primary
document's text, under their own character ceiling, producing one combined
`content_text` per accession. Their HTML is converted to markdown rather than
taken from `Attachment.text()`, which lays tables out at a fixed console width
and therefore elides cells mid-word and mid-number (Issue #156; see
`_exhibit_plain_text`).

Requests are throttled to at most 10/second (SEC fair-access, `docs/00_human_
preparation.md`) via an injectable clock/sleep pair so the throttle itself is
unit-testable without real waiting.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol

import edgar
from edgar.core import has_html_content, text_extensions
from edgar.files.html_documents import get_clean_html
from edgar.files.markdown import to_markdown

from swing_copilot.clock import SystemClock
from swing_copilot.retry import retry_external_call
from swing_copilot.storage.market_store import FundamentalsRecord
from swing_copilot.text.base import (
    EXHIBIT_OMISSION_MARKER,
    EXHIBIT_TRUNCATION_MARKER,
    FilingSection,
    TextItem,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date

    from swing_copilot.clock import Clock

_FUNDAMENTALS_FORMS = ("10-K", "10-Q")
_FINANCIAL_TAXONOMY_PREFIX = "us-gaap:"
_MIN_REQUEST_INTERVAL_SECONDS = 0.1  # 10 requests/second cap
_DEFAULT_FUNDAMENTALS_LOOKBACK_DAYS = 400  # SEC filing lookback window; owned independently of pipeline/daily.py's price-history lookback
_SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar"
logger = logging.getLogger(__name__)

# Exhibit collection (Issue #128). Restricted to 8-K because that is the form
# whose primary document is a bare notice; a 10-K/10-Q already carries its
# substance in the primary document and its exhibits are mostly certifications
# and legal boilerplate.
_EXHIBIT_FORMS = frozenset({"8-K", "8-K/A"})
#: EDGAR's `document_type` for the Item 2.02 press release and its siblings
#: ("EX-99", "EX-99.1", "EX-99.01", "EX-99.2", ...). Item 2.02 material is
#: always furnished under 99; other exhibit numbers (EX-10 contracts, EX-23
#: consents) are not the earnings narrative and are deliberately excluded.
_EXHIBIT_DOCUMENT_TYPE_PREFIX = "EX-99"
#: A furnished earnings release occasionally splits across 99.1 (release) and
#: 99.2 (supplement/presentation); beyond three, an 8-K is attaching a document
#: set rather than one release. What the cap costs is declared in the collected
#: text with `EXHIBIT_OMISSION_MARKER` (Issue #163) -- the value is a judgement
#: about how much is worth fetching, never a claim that nothing was lost.
_MAX_EXHIBITS_PER_FILING = 3
#: Safety valve on the exhibit characters appended to one filing, not an export
#: budget (Issue #180). Fitting the export ceilings is `analysis/
#: filing_selection.py`'s job: it runs per export and can choose *what* to keep,
#: whereas a cut here is written into `text_items.content_text` and is
#: irreversible short of a same-key rerun. So collection keeps the whole
#: exhibit and only a pathological document is stopped: the largest earnings
#: 8-K measured in the Issue #165 replay came to 375,000 characters, and
#: markdown conversion shrinks even a 3.1 MB raw HTML exhibit to roughly a
#: tenth of its size.
_MAX_EXHIBIT_CHARS_PER_FILING = 500_000
#: Document extensions edgartools itself treats as text (`Attachment.is_text`).
#: Borrowed rather than restated so the two cannot drift apart.
_TEXT_DOCUMENT_EXTENSIONS = frozenset(text_extensions)

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


class _AttachmentLike(Protocol):
    """One filing attachment, shaped like edgartools' `Attachment`.

    `content` is the attachment's raw filed bytes/markup, downloaded on
    access. It is used in place of `Attachment.text()` so the HTML-to-text
    conversion is ours to choose (Issue #156, `_exhibit_plain_text`).
    """

    document_type: str
    document: str

    @property
    def content(self) -> str | bytes | None: ...  # pragma: no cover


class _AttachmentsLike(Protocol):
    """A filing's attachment set, shaped like edgartools' `Attachments`.

    Only `documents` (the filed document list) is used; `data_files` holds
    XBRL/graphic side-cars, which carry no narrative.
    """

    documents: Sequence[_AttachmentLike]


class _FilingLike(Protocol):
    accession_number: str
    form: str
    filing_date: date
    period_of_report: date
    filing_url: str
    attachments: _AttachmentsLike

    def text(self) -> str: ...  # pragma: no cover

    def obj(self) -> _CompanyReportLike | None: ...  # pragma: no cover


class _CompanyReportLike(Protocol):
    def get_item_with_part(
        self, part: str, item: str, markdown: bool = True
    ) -> str | None: ...  # pragma: no cover


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
        # Record when the request is actually issued (after any wait), not when
        # the throttle decision started. Recording the pre-sleep reading drops
        # the slept interval from the next gap calculation and lets the
        # effective request rate exceed 1/_MIN_REQUEST_INTERVAL_SECONDS.
        now = self._clock()
        issued_at = now
        if self._last_request_at is not None:
            wait = _MIN_REQUEST_INTERVAL_SECONDS - (now - self._last_request_at)
            if wait > 0:
                self._sleep_fn(wait)
                issued_at = now + wait
        self._last_request_at = issued_at

    def _with_retries[T](self, operation: Callable[[], T]) -> T:
        """Run one EDGAR boundary operation with a bounded retry policy."""
        return retry_external_call(
            operation,
            before_attempt=self._throttle,
            sleep_fn=self._sleep_fn,
        )

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
        self,
        symbol: str,
        form_types: list[str],
        *,
        as_of: datetime,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[TextItem]:
        """Return recent filings' full text, normalized for text collection (FR-07).

        Unlike `fetch_fundamentals()` (explicitly `filed_at`-sorted), the
        external `get_filings()` result order is whatever edgartools returns
        it in. `since`/`limit` bound *which* filings qualify (independent of
        as-of visibility) and the result is always sorted `filed_at`
        descending before the `limit` cut, so a caller asking for "the 3 most
        recent filings" reliably gets the most recent ones rather than
        whatever the external library happened to list first (roadmap §5
        P6-26).

        Args:
            symbol: Ticker symbol.
            form_types: SEC form types to fetch (e.g. `["8-K", "10-Q"]`).
            as_of: Only filings submitted at or before this instant are
                returned (inclusive upper bound; point-in-time visibility).
            since: Only filings submitted at or after this instant are
                returned (inclusive lower bound). `None` means no lower bound.
            limit: Maximum number of filings to return, most-recent
                (`filed_at` descending) first. `None` means no cap.

        Returns:
            One `TextItem` per matching filing (`source_type="filing"`),
            ordered `filed_at` descending.
        """
        filings = self._with_retries(
            lambda: self._company_factory(symbol).get_filings(form=form_types)
        )

        matching = [
            filing
            for filing in filings
            if _to_utc_datetime(filing.filing_date) <= as_of
            and (since is None or _to_utc_datetime(filing.filing_date) >= since)
        ]
        matching.sort(
            key=lambda filing: _to_utc_datetime(filing.filing_date), reverse=True
        )
        if limit is not None:
            matching = matching[:limit]

        return [self._filing_text_item(filing, symbol) for filing in matching]

    def _filing_text_item(self, filing: _FilingLike, symbol: str) -> TextItem:
        """Build one audit-complete item plus optional structured 10-Q sections."""
        content_text = self._with_retries(filing.text) + self._exhibit_text(filing)
        return TextItem(
            source_id=f"edgar:{filing.accession_number}",
            symbol=symbol,
            source_type="filing",
            published_at=_to_utc_datetime(filing.filing_date),
            title=f"{filing.form} - {symbol}",
            source_url=filing.filing_url,
            content_text=content_text,
            fetched_at=self._date_clock.now(),
            filing_sections=_extract_ten_q_sections(filing),
        )

    def _exhibit_text(self, filing: _FilingLike) -> str:
        """Return an 8-K's `EX-99*` exhibit text, as appendable blocks.

        An earnings 8-K's primary document usually only states that a press
        release was furnished as Exhibit 99.1; the figures, the guidance, and
        management's commentary are in the exhibit itself (Issue #128). The
        exhibits are therefore concatenated onto the same `content_text` -- one
        accession stays one `TextItem`, so `text_items`, the exported
        `coverage`, and `analysis_source_coverage.selection_mode` all keep
        their existing shape.

        Each exhibit is downloaded and converted to text by
        `_exhibit_plain_text`, which keeps table cells whole (Issue #156).

        Retrieval is fail-soft, like 10-Q section extraction: an exhibit that
        cannot be downloaded or parsed is a data-quality outcome, not a reason
        to lose either the notice text or the exhibits that did arrive.
        Whatever was already assembled is kept and the failure is logged with
        its traceback.

        Returns:
            The exhibit blocks to append, each introduced by an
            `[EXHIBIT ...]` header so a reader never mistakes the join for
            continuous text. `""` for a non-8-K, for an 8-K with no readable
            `EX-99*` exhibit, and when the attachment list itself could not be
            retrieved. Total exhibit characters are bounded only by the
            `_MAX_EXHIBIT_CHARS_PER_FILING` safety valve; whatever it costs is
            marked inline with `EXHIBIT_TRUNCATION_MARKER`, whether it cut one
            exhibit short or was spent before a later one could be fetched.
            Exhibits past `_MAX_EXHIBITS_PER_FILING` are never fetched and are
            marked with `EXHIBIT_OMISSION_MARKER` instead (Issue #163).
            Those markers are the only trace of either cap that survives into
            `content_text`, and `analysis/filing_selection.py` reads them back
            as `FilingCoverage.exhibit_truncated` (Issue #157), so a silent
            break here would report the filing as complete.
        """
        if filing.form not in _EXHIBIT_FORMS:
            return ""
        blocks: list[str] = []
        remaining = _MAX_EXHIBIT_CHARS_PER_FILING
        omitted_count = 0
        try:
            attachments = self._with_retries(lambda: filing.attachments)
            exhibits = _earnings_exhibits(attachments)
            omitted_count = max(len(exhibits) - _MAX_EXHIBITS_PER_FILING, 0)
            for exhibit in exhibits[:_MAX_EXHIBITS_PER_FILING]:
                if remaining <= 0:
                    # The budget, not the filing, ended the exhibit text: this
                    # exhibit exists and is being dropped whole, so say so --
                    # unless the exhibit that exhausted the budget was itself
                    # cut and already carries the marker.
                    if not blocks[-1].endswith(EXHIBIT_TRUNCATION_MARKER):
                        blocks.append(EXHIBIT_TRUNCATION_MARKER)
                    break
                # `None` for an exhibit that carries no text at all (a PDF
                # slide deck furnished as 99.1, for example): an absence, not
                # a failure, so it is skipped rather than raised on.
                text = self._with_retries(partial(_exhibit_plain_text, exhibit))
                if not text or not text.strip():
                    continue
                kept = text[:remaining]
                remaining -= len(kept)
                marker = "" if len(kept) == len(text) else EXHIBIT_TRUNCATION_MARKER
                header = f"[EXHIBIT {exhibit.document_type} {exhibit.document}]"
                blocks.append(f"\n\n{header}\n{kept}{marker}")
        except Exception:  # exhibit retrieval is a documented fail-soft boundary
            logger.exception(
                "Exhibit retrieval failed for accession %s; keeping %d exhibit(s)",
                filing.accession_number,
                len(blocks),
            )
        if omitted_count:
            # Outside the `try` on purpose: the count cap is decided from the
            # attachment list alone, so a later download failure must not also
            # swallow the fact that exhibits were never offered a chance.
            blocks.append(EXHIBIT_OMISSION_MARKER)
        return "".join(blocks)


def _exhibit_plain_text(exhibit: _AttachmentLike) -> str | None:
    """Download one exhibit and convert it to text with its tables intact.

    Not `Attachment.text()`: that renders the exhibit's HTML through Rich at a
    fixed console width, which lays every table out inside that width and
    elides whatever does not fit, mid-word and mid-digit -- `1,543,…` for a
    revenue line, `(In th… ex… per sh…` for a unit caption (Issue #156). The
    lost digits cannot be recovered from the rendered text, so a quote taken
    from such a table is necessarily wrong (AC16). The same markdown
    conversion `Attachment.markdown()` performs is used instead: it has no
    width to fit into, so every cell survives whole, and it is also more
    compact than the column-aligned rendering, which leaves more of the
    per-filing character budget for actual content.

    Content that does not look like HTML at all is returned verbatim, and
    markup that cannot be rooted as an HTML document is kept verbatim too:
    raw markup still carries its digits, whereas dropping the exhibit would
    lose the earnings release entirely.

    Returns:
        The exhibit's text, or `None` when the attachment carries no text: a
        binary exhibit (a PDF slide deck furnished as 99.1, for example), or
        HTML the converter itself rejects. Both are an absence, not a failure,
        and the caller skips them.
    """
    # Same gate as `Attachment.is_text()`, and for the same reason: a binary
    # exhibit's document holds an encoded blob, not prose. Checking it before
    # touching `content` also means such an exhibit is never downloaded.
    if PurePosixPath(exhibit.document).suffix not in _TEXT_DOCUMENT_EXTENSIONS:
        return None
    content = exhibit.content
    if not isinstance(content, str):
        # A text extension whose payload still arrived as bytes: an encoding
        # we have no safe way to append to the filing's text.
        return None
    if not has_html_content(content):
        return content
    # `get_clean_html` strips the inline-XBRL header, scripts, styles and
    # table-of-contents links first, exactly as `Attachment.markdown()` does.
    clean_html = get_clean_html(content)
    if clean_html is None:
        logger.warning(
            "Exhibit %s has no HTML root to convert; keeping its content verbatim",
            exhibit.document,
        )
        return content
    # edgartools ships no type information, so the converter's declared
    # `Optional[str]` arrives as `Any`; pin it back to the contract here.
    markdown: str | None = to_markdown(clean_html)
    return markdown


def _earnings_exhibits(attachments: _AttachmentsLike) -> list[_AttachmentLike]:
    """Return every `EX-99*` exhibit the filing offers, in filed order.

    Filed order is EDGAR's own sequence numbering, so 99.1 (the press release)
    precedes 99.2 (supplements) and gets the character budget first.

    Uncapped on purpose: `_MAX_EXHIBITS_PER_FILING` is applied by the caller,
    which needs the full count to tell how many exhibits the cap cost and to
    declare that loss in the collected text (Issue #163). Nothing here is
    downloaded, so returning the tail costs no request.
    """
    return [
        attachment
        for attachment in attachments.documents
        if (attachment.document_type or "")
        .strip()
        .upper()
        .startswith(_EXHIBIT_DOCUMENT_TYPE_PREFIX)
    ]


def _extract_ten_q_sections(filing: _FilingLike) -> tuple[FilingSection, ...]:
    """Extract priority sections without making their absence a fetch failure.

    EdgarTools parses the primary HTML already loaded by `filing.text()`.
    Issuer-specific markup can still defeat section detection; that is an
    expected data-quality outcome, surfaced later as `head_fallback`.
    """
    if filing.form not in {"10-Q", "10-Q/A"}:
        return ()
    try:
        report = filing.obj()
        if report is None:
            return ()
        requested = (
            ("part_i_item_1", "Part I", "Item 1"),
            ("part_i_item_2", "Part I", "Item 2"),
            ("part_ii_item_1a", "Part II", "Item 1A"),
            ("part_ii_item_1", "Part II", "Item 1"),
        )
        return tuple(
            FilingSection(name=name, content_text=text)
            for name, part, item in requested
            if (text := report.get_item_with_part(part, item, markdown=False))
            and text.strip()
        )
    except Exception:  # edgartools parser failures are a documented fail-soft boundary
        logger.exception(
            "10-Q section extraction failed for accession %s; using head fallback",
            filing.accession_number,
        )
        return ()
