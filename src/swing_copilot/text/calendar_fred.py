"""FRED economic release calendar client (FR-07).

Uses FRED's `releases/dates` endpoint — the closest official equivalent to
an "economic calendar" (upcoming/past release dates for series like the
Employment Situation) — since FRED itself indexes data series, not a
calendar product.

`releases/dates` only returns `release_id` / `date` / `release_name`, which
made `title` and `content_text` byte-identical and left the exported
`calendar_events[]` summary without any judgement material (Issue #82). Each
release is therefore enriched through a two-step chain —
`release/series` (pick the most popular series of the release) then
`series/observations` (its latest and prior values under the `as_of`
cutoff) — so the summary carries actual and prior values rather than
repeating the release name. Market consensus is not published by FRED and is
stated as unavailable rather than invented.

Every request (including every retry attempt) is throttled to FRED's
documented 120 requests/minute ceiling, and the enrichment is bounded: the
per-release lookups are memoized for the duration of one call and only the
`max_enriched_releases` newest releases are enriched, so a wide date range
cannot turn into hundreds of external calls.

The fetch is wrapped in a bounded retry so a single transient FRED failure
(timeout, connection error, 408, 429, or 5xx) does not fail the whole run.
Other 4xx errors (auth/validation) and response-parsing failures are not
transient and propagate immediately. Enrichment is additionally fail-soft:
if the value lookup cannot complete, the event is still returned with a
summary that states what is missing.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as date_type
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from swing_copilot.clock import SystemClock
from swing_copilot.retry import retry_external_call
from swing_copilot.text.base import TextItem

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date

    from swing_copilot.clock import Clock

logger = logging.getLogger(__name__)

FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/releases/dates"
FRED_RELEASE_SERIES_URL = "https://api.stlouisfed.org/fred/release/series"
FRED_SERIES_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"

_MIN_REQUEST_INTERVAL_SECONDS = 0.5  # 120 requests/minute cap
_DEFAULT_MAX_ENRICHED_RELEASES = 20
_OBSERVATION_FETCH_LIMIT = 8
_REPORTED_OBSERVATIONS = 2
_MISSING_OBSERVATION_VALUE = "."
_NO_CONSENSUS_NOTE = "Market consensus is not published by FRED."

_SECRET_QUERY_PATTERN = re.compile(r"(api_key=)[^&\s]+")


def _redact(text: str) -> str:
    """Mask the FRED API key that HTTP errors echo back inside the request URL."""
    return _SECRET_QUERY_PATTERN.sub(r"\1***", text)


class _HttpGet(Protocol):
    # Any: FRED's decoded response is an untyped JSON object.
    def __call__(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Return the parsed JSON object from a GET request."""
        ...  # pragma: no cover


# Any: the HTTP client returns FRED JSON before endpoint-specific validation.
def _real_http_get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    response = httpx.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    result: dict[str, Any] = response.json()  # Any: FRED has no typed JSON schema
    return result


@dataclass(frozen=True, slots=True)
class FredCalendarTiming:
    """The client's injectable time seams, grouped to keep the ctor small.

    Attributes:
        clock: Wall clock used for the deterministic `fetched_at` stamp.
        rate_clock: Monotonic clock the throttle measures spacing against.
        sleep_fn: Sleep used by both the throttle and the retry backoff.
    """

    clock: Clock | None = None
    rate_clock: Callable[[], float] | None = None
    sleep_fn: Callable[[float], None] | None = None


@dataclass(frozen=True, slots=True)
class _Observation:
    """One `series/observations` row that carries a usable numeric value."""

    observed_on: date_type
    value: str


@dataclass(frozen=True, slots=True)
class _ReleaseValues:
    """The representative series of a release plus its latest/prior values."""

    series_id: str
    series_title: str
    units: str
    observations: tuple[_Observation, ...]


class FredCalendarClient:
    """FRED economic release-date calendar client."""

    def __init__(
        self,
        api_key: str,
        *,
        http_get: _HttpGet = _real_http_get,
        timing: FredCalendarTiming | None = None,
        max_enriched_releases: int = _DEFAULT_MAX_ENRICHED_RELEASES,
    ) -> None:
        """Create the client.

        Args:
            api_key: FRED API key.
            http_get: Injectable HTTP GET, used by tests to avoid real calls.
            timing: Injectable clock/rate-clock/sleep seams; defaults to real time.
            max_enriched_releases: Ceiling on how many distinct releases get the
                two extra value lookups in one call. Bounds external I/O when a
                wide date range returns many releases.
        """
        timing = timing or FredCalendarTiming()
        self._api_key = api_key
        self._http_get = http_get
        self._clock = timing.clock or SystemClock()
        self._rate_clock = timing.rate_clock or time.monotonic
        self._sleep_fn = timing.sleep_fn or time.sleep
        self._max_enriched_releases = max_enriched_releases
        self._last_request_at: float | None = None

    def _throttle(self) -> None:
        """Space requests to FRED's 120/minute ceiling, before *every* attempt."""
        # Record when the request is actually issued (after any wait), not when
        # the throttle decision started. Recording the pre-sleep reading drops
        # the slept interval from the next gap calculation and lets the
        # effective request rate exceed 1/_MIN_REQUEST_INTERVAL_SECONDS.
        now = self._rate_clock()
        issued_at = now
        if self._last_request_at is not None:
            wait = _MIN_REQUEST_INTERVAL_SECONDS - (now - self._last_request_at)
            if wait > 0:
                self._sleep_fn(wait)
                issued_at = now + wait
        self._last_request_at = issued_at

    # Any: retrying still returns the provider's untyped JSON response.
    def _fetch_with_retries(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Run one FRED HTTP GET with throttling and bounded retry."""
        return retry_external_call(
            lambda: self._http_get(url, {**params, "api_key": self._api_key}),
            before_attempt=self._throttle,
            sleep_fn=self._sleep_fn,
        )

    def fetch_calendar_events(
        self, start: date, end: date, *, as_of: date
    ) -> list[TextItem]:
        """Fetch economic release dates within `[start, end]`.

        Args:
            start: Inclusive range start of the release dates to return.
            end: Inclusive range end of the release dates to return.
            as_of: Point-in-time cutoff for the observed values quoted in the
                summary; only observations dated on or before `as_of` are used.
                Never inferred from wall time.

        Returns:
            Release-date events normalized to `TextItem` (`source_type="calendar"`)
            whose `content_text` summarizes the release's latest and prior values.
        """
        payload = self._fetch_with_retries(
            FRED_RELEASE_DATES_URL,
            {
                "realtime_start": start.isoformat(),
                "realtime_end": end.isoformat(),
                "file_type": "json",
            },
        )
        raw_events = list(payload.get("release_dates", []))
        values = self._collect_release_values(raw_events, as_of)
        fetched_at = self._clock.now()
        return [
            TextItem(
                source_id=f"fred:{item['release_id']}:{item['date']}",
                symbol=None,
                source_type="calendar",
                published_at=datetime.fromisoformat(item["date"]).replace(tzinfo=UTC),
                title=item.get("release_name"),
                source_url=f"https://fred.stlouisfed.org/release?rid={item['release_id']}",
                content_text=_summarize(item, values.get(item["release_id"])),
                fetched_at=fetched_at,
            )
            for item in raw_events
        ]

    def _collect_release_values(
        self,
        raw_events: Sequence[dict[str, Any]],  # Any: raw FRED release rows
        as_of: date,
    ) -> dict[Any, _ReleaseValues | None]:  # Any: provider release IDs are untyped
        """Look up latest/prior values once per release, newest releases first.

        Releases beyond `max_enriched_releases` are deliberately left out and
        fall back to the value-less summary, so the number of extra external
        calls stays bounded regardless of the requested date range.
        """
        ordered = sorted(
            raw_events, key=lambda item: str(item.get("date", "")), reverse=True
        )
        # Any: release IDs are opaque values from FRED's JSON payload.
        values: dict[Any, _ReleaseValues | None] = {}
        for item in ordered:
            release_id = item["release_id"]
            if release_id in values:
                continue
            if len(values) >= self._max_enriched_releases:
                break
            values[release_id] = self._release_values(release_id, as_of)
        return values

    # Any: release IDs come directly from FRED's heterogeneous JSON rows.
    def _release_values(self, release_id: Any, as_of: date) -> _ReleaseValues | None:
        """Resolve one release's representative series and its recent values.

        Fail-soft: any transport, HTTP, or response-shape failure yields `None`
        so the calendar event is still exported with a degraded summary. The
        logged message is redacted rather than raised through
        `logging.exception()`, because FRED echoes the request URL — which
        carries the API key — inside HTTP status errors and tracebacks.
        """
        try:
            series = self._representative_series(release_id)
            if series is None:
                return None
            return _ReleaseValues(
                series_id=str(series["id"]),
                series_title=str(series.get("title", "")).strip(),
                units=str(series.get("units_short", "")).strip(),
                observations=self._recent_observations(str(series["id"]), as_of),
            )
        except (httpx.HTTPError, OSError, KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "FRED value lookup failed for release %s: %s: %s",
                release_id,
                type(exc).__name__,
                _redact(str(exc)),
            )
            return None

    def _representative_series(
        self,
        release_id: Any,  # Any: opaque FRED release identifier
    ) -> dict[str, Any] | None:  # Any: FRED series fields are heterogeneous JSON
        """Return the release's most popular series, or `None` if it has none."""
        payload = self._fetch_with_retries(
            FRED_RELEASE_SERIES_URL,
            {
                "release_id": release_id,
                "order_by": "popularity",
                "sort_order": "desc",
                "limit": 1,
                "file_type": "json",
            },
        )
        series_list = payload.get("seriess") or []
        return dict(series_list[0]) if series_list else None

    def _recent_observations(
        self, series_id: str, as_of: date
    ) -> tuple[_Observation, ...]:
        """Return the newest-first observations visible at `as_of`.

        The `as_of` boundary is inclusive and re-checked here as well as being
        pushed down through `observation_end`, so a provider that ignores the
        parameter still cannot leak a value dated after the cutoff.
        """
        payload = self._fetch_with_retries(
            FRED_SERIES_OBSERVATIONS_URL,
            {
                "series_id": series_id,
                "observation_end": as_of.isoformat(),
                "sort_order": "desc",
                "limit": _OBSERVATION_FETCH_LIMIT,
                "file_type": "json",
            },
        )
        observations = []
        for row in payload.get("observations", []):
            value = str(row["value"]).strip()
            observed_on = date_type.fromisoformat(row["date"])
            if value == _MISSING_OBSERVATION_VALUE or observed_on > as_of:
                continue
            observations.append(_Observation(observed_on=observed_on, value=value))
        observations.sort(key=lambda obs: obs.observed_on, reverse=True)
        return tuple(observations[:_REPORTED_OBSERVATIONS])


# Any: the summary reads heterogeneous fields from one raw FRED release row.
def _summarize(item: dict[str, Any], values: _ReleaseValues | None) -> str:
    """Build the event summary, which is never byte-identical to the title.

    Args:
        item: One raw `releases/dates` row.
        values: The resolved release values, or `None` when the lookup was
            skipped or failed.

    Returns:
        A one-paragraph summary naming the release, its scheduled date, and
        either the observed latest/prior values or why they are missing. It
        deliberately leads with the schedule rather than the release name, so
        even an aggressively truncated export cannot collapse back onto the
        title.
    """
    release_name = item.get("release_name") or f"FRED release {item['release_id']}"
    head = (
        f"Scheduled for {item['date']}: "
        f"{release_name} (FRED release {item['release_id']})."
    )
    return f"{head} {_values_sentence(values)} {_NO_CONSENSUS_NOTE}"


def _values_sentence(values: _ReleaseValues | None) -> str:
    """Describe the latest/prior values, or state precisely what is missing."""
    if values is None:
        return (
            "Latest and prior values are unavailable: "
            "the FRED series lookup did not return usable data."
        )
    label = values.series_id
    if values.series_title:
        label = f"{label} ({values.series_title})"
    units = f" {values.units}" if values.units else ""
    if not values.observations:
        return (
            f"Representative series {label} has no observation "
            "on or before the as-of date."
        )
    latest = values.observations[0]
    latest_text = (
        f"Representative series {label}: "
        f"latest {latest.observed_on.isoformat()} = {latest.value}{units}"
    )
    if len(values.observations) < _REPORTED_OBSERVATIONS:
        return f"{latest_text}; prior value unavailable."
    prior = values.observations[1]
    return (
        f"{latest_text}, "
        f"prior {prior.observed_on.isoformat()} = {prior.value}{units}"
        f"{_change_text(latest.value, prior.value)}."
    )


def _change_text(latest: str, prior: str) -> str:
    """Render the latest-minus-prior delta, omitted for non-numeric values."""
    try:
        delta = float(latest) - float(prior)
    except ValueError:
        return ""
    return f" (change {delta:+g})"
