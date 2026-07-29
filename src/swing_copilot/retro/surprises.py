"""Surprise selection and freshness collection (P8-31, design §5.3 item 5).

A "surprise" is a verdict that missed severely in either direction: a
`proceed` that fell through the severity boundary, or a `skip` that ran away
upward. Those are the cases where re-reading the original evidence can teach
something, so each one is handed to the skill with its verdict, its realized
path, and -- the point of this module's second half -- what the same adapters
would say about the symbol *now*.

The freshness window runs from the reviewed run's `as_of` to the
retrospective's own `as_of`, which is what makes "the information existed but
we did not have it" distinguishable from "nobody could have known" (design §7,
`information_absent` vs `exogenous`).

Two rules the rest of the file exists to keep:

* **No silent cap.** Overflow past `settings.retro.max_surprises` is dropped by
  smallest absolute move, and the dropped count travels with the selection.
* **Fetching is fail-soft per symbol and per adapter.** A provider outage
  empties that side of one dossier and leaves a note; it never fails the
  export, which is otherwise entirely offline work already in the database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from swing_copilot.analysis.filing_selection import select_filing_inputs
from swing_copilot.analysis.schemas import FilingInput, NewsInput
from swing_copilot.retro.evaluate import MISS_SEVERE
from swing_copilot.text.edgar_filings import (
    FilingLookbackBounds,
    fetch_recent_filings_text,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from uuid import UUID

    from swing_copilot.config import AnalysisConfig
    from swing_copilot.storage.verdict_records import VerdictOutcomeRecord
    from swing_copilot.text.base import TextItem

logger = logging.getLogger(__name__)

#: Forms whose text is worth re-reading after the fact, matching
#: `pipeline/daily.py`'s collection set so freshness and the original export
#: speak about the same universe of disclosures.
FRESHNESS_FORM_TYPES = ["8-K", "10-Q", "10-K"]

_SURPRISE_PREFIX = "surprise"
_NEWS = "news"


class NewsClientLike(Protocol):
    """Structural stand-in for `text.news_finnhub.FinnhubNewsClient`."""

    def fetch_company_news(
        self, symbol: str, since: date, *, as_of: date
    ) -> list[TextItem]:
        """Fetch news for `symbol` published in `[since, as_of]`."""
        ...  # pragma: no cover


class EdgarClientLike(Protocol):
    """Structural stand-in for `data.edgar.EdgarClient`."""

    def fetch_filing_texts(
        self,
        symbol: str,
        form_types: list[str],
        *,
        as_of: datetime,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[TextItem]:
        """Fetch recent filings' full text, normalized for text collection."""
        ...  # pragma: no cover


@dataclass(frozen=True, slots=True)
class _FetchWindow:
    """The freshness window: the reviewed run's date through the retrospective's."""

    since: date
    as_of: date


@dataclass(frozen=True, slots=True)
class FreshnessSources:
    """The text adapters the freshness fetch may use.

    Both are optional: without the matching API key the composition root has
    no client to pass, and the dossier simply carries no freshness for that
    side.
    """

    news_client: NewsClientLike | None = None
    edgar_client: EdgarClientLike | None = None


@dataclass(frozen=True, slots=True)
class FreshnessBundle:
    """What the adapters returned for one symbol after the reviewed run.

    `fetch_failed` distinguishes "asked and the provider raised" from "never
    asked": an unconfigured adapter is a configuration state, not a fetch
    failure, and only the former should make a reader distrust an empty list.
    """

    news: tuple[NewsInput, ...]
    filings: tuple[FilingInput, ...]
    fetch_failed: bool


@dataclass(frozen=True, slots=True)
class SurpriseCandidate:
    """One `(run, symbol)` selected for a full evidence dossier."""

    surprise_id: str
    run_id: UUID
    symbol: str
    #: Largest absolute forward return among the symbol's severe misses; the
    #: ranking key when the cap has to drop someone.
    peak_abs_return_pct: float
    #: Every evaluated horizon of this symbol, severe or not, so the dossier
    #: can show the realized path rather than only the failure point.
    outcomes: tuple[VerdictOutcomeRecord, ...]


@dataclass(frozen=True, slots=True)
class SurpriseSelection:
    """The capped selection plus how many severe misses it left out."""

    selected: tuple[SurpriseCandidate, ...]
    dropped_count: int


def select_surprises(
    outcomes: Sequence[VerdictOutcomeRecord], max_surprises: int
) -> SurpriseSelection:
    """Pick the severe misses worth a dossier, largest absolute move first.

    Args:
        outcomes: The window's classified rows, both horizons mixed.
        max_surprises: `settings.retro.max_surprises`.

    Returns:
        The selection and the number of severe-miss symbols the cap excluded.
        Ties are broken by `(run_id, symbol)` so two runs of the same
        retrospective produce the same dossiers.
    """
    grouped: dict[tuple[UUID, str], list[VerdictOutcomeRecord]] = {}
    for outcome in outcomes:
        grouped.setdefault((outcome.run_id, outcome.symbol), []).append(outcome)

    candidates = [
        _candidate(run_id, symbol, rows)
        for (run_id, symbol), rows in grouped.items()
        if any(row.classification == MISS_SEVERE for row in rows)
    ]
    candidates.sort(
        key=lambda item: (-item.peak_abs_return_pct, str(item.run_id), item.symbol)
    )
    return SurpriseSelection(
        selected=tuple(candidates[:max_surprises]),
        dropped_count=max(0, len(candidates) - max_surprises),
    )


def _candidate(
    run_id: UUID, symbol: str, rows: Sequence[VerdictOutcomeRecord]
) -> SurpriseCandidate:
    return SurpriseCandidate(
        surprise_id=f"{_SURPRISE_PREFIX}:{run_id}:{symbol}",
        run_id=run_id,
        symbol=symbol,
        peak_abs_return_pct=max(
            abs(row.forward_return_pct)
            for row in rows
            if row.classification == MISS_SEVERE
        ),
        outcomes=tuple(sorted(rows, key=lambda row: row.horizon_days)),
    )


def fetch_freshness(
    sources: FreshnessSources,
    symbol: str,
    *,
    since: date,
    as_of: date,
    limits: AnalysisConfig,
) -> tuple[FreshnessBundle, tuple[str, ...]]:
    """Collect what the text adapters report for `symbol` since the reviewed run.

    Goes through the same adapters the daily pipeline uses, so their timeout,
    retry, and rate-limit contracts apply unchanged (E31.3); the only thing
    added here is the window and the per-symbol fail-soft boundary.

    Args:
        sources: Adapters to ask, each optional.
        symbol: The surprise symbol.
        since: The reviewed run's `as_of` -- the start of "what came out
            after we decided".
        as_of: The retrospective's cutoff; nothing later is requested.
        limits: `settings.analysis`, reused rather than duplicated into
            `RetroConfig` (E31.1).

    Returns:
        The bundle and one note per adapter that raised. Failures are logged
        with their traceback and reported, never re-raised: one dead provider
        must not cost the retrospective every other dossier.
    """
    notes: list[str] = []
    window = _FetchWindow(since=since, as_of=as_of)
    news, news_failed = _fetch_news(sources.news_client, symbol, window, notes)
    filings, filings_failed = _fetch_filings(
        sources.edgar_client, symbol, window, limits, notes
    )
    return (
        FreshnessBundle(
            news=_news_inputs(news, limits),
            filings=_filing_inputs(filings, limits),
            fetch_failed=news_failed or filings_failed,
        ),
        tuple(notes),
    )


def _fetch_news(
    client: NewsClientLike | None,
    symbol: str,
    window: _FetchWindow,
    notes: list[str],
) -> tuple[list[TextItem], bool]:
    if client is None:
        return [], False
    try:
        return list(
            client.fetch_company_news(symbol, window.since, as_of=window.as_of)
        ), False
    except Exception:
        logger.exception("retro export: %s の鮮度ニュース取得に失敗", symbol)
        notes.append(f"{symbol}: 鮮度ニュースを取得できなかったため空欄")
        return [], True


def _fetch_filings(
    client: EdgarClientLike | None,
    symbol: str,
    window: _FetchWindow,
    limits: AnalysisConfig,
    notes: list[str],
) -> tuple[list[TextItem], bool]:
    if client is None:
        return [], False
    try:
        return list(
            fetch_recent_filings_text(
                client,
                symbol,
                FRESHNESS_FORM_TYPES,
                window.as_of,
                # The lookback is the review window itself, so the fetch
                # cannot reach back before the run under review.
                FilingLookbackBounds(
                    lookback_days=max(0, (window.as_of - window.since).days),
                    limit=limits.max_filings_per_symbol,
                ),
            )
        ), False
    except Exception:
        logger.exception("retro export: %s の鮮度開示取得に失敗", symbol)
        notes.append(f"{symbol}: 鮮度開示を取得できなかったため空欄")
        return [], True


def _news_inputs(
    items: Sequence[TextItem], limits: AnalysisConfig
) -> tuple[NewsInput, ...]:
    """Newest-first news, under `analysis.*`'s count and length budgets."""
    news = sorted(
        (item for item in items if item.source_type == _NEWS),
        key=lambda item: (item.published_at, item.source_id),
        reverse=True,
    )
    return tuple(
        NewsInput(
            source_id=item.source_id,
            published_at=item.published_at,
            headline=item.title,
            summary=item.content_text[: limits.max_news_chars_per_item],
            url=item.source_url,
            provider=item.source_id.split(":", 1)[0],
        )
        for item in news[: limits.max_news_items_per_symbol]
    )


def _filing_inputs(
    items: Sequence[TextItem], limits: AnalysisConfig
) -> tuple[FilingInput, ...]:
    """Use the daily export's filing selection and coverage contract."""
    return tuple(
        select_filing_inputs(
            items,
            per_filing_chars=limits.max_filing_chars,
            per_symbol_chars=limits.max_filing_chars_per_symbol,
        )
    )
