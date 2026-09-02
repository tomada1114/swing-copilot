"""S&P 500 universe fetch, snapshot fallback, and manual overrides (FR-01).

``get_sp500_universe`` returns the current membership, preferring the local
CSV snapshot when ``force_refresh`` is false and falling back to it if a
Wikipedia refetch fails (NFR-04). ``resolve_daily_universe`` is the
point-in-time boundary used by the daily pipeline: historical runs select a
persisted snapshot at or before ``as_of``, while live runs reuse or refresh a
snapshot according to the configured interval.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx
import pandas as pd

from swing_copilot import __version__
from swing_copilot.exceptions import SwingCopilotError
from swing_copilot.io_atomic import write_text_atomically
from swing_copilot.retry import retry_external_call

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date

WIKIPEDIA_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
DEFAULT_SNAPSHOT_PATH = Path("config/universe_snapshot.csv")
_SNAPSHOT_FIELDS = ("symbol", "company_name", "gics_sector", "source_symbol")
_WIKIPEDIA_USER_AGENT = (
    f"swing-copilot/{__version__} (https://github.com/tomada1114/swing-copilot)"
)


class UniverseError(SwingCopilotError):
    """Raised when the universe cannot be fetched and no snapshot exists."""


@dataclass(frozen=True, slots=True)
class UniverseMember:
    """One S&P 500 constituent, before and after symbol normalization."""

    symbol: str
    company_name: str
    gics_sector: str
    source_symbol: str


@dataclass(frozen=True, slots=True)
class UniverseResolution:
    """Membership selected for one run, with any data-quality warning."""

    members: tuple[UniverseMember, ...]
    snapshot_date: date
    warning: str | None = None


class UniverseStateStore(Protocol):
    """Minimal StateStore capability ``refresh_universe`` depends on."""

    def record_universe_membership(
        self, snapshot_date: date, members: Sequence[UniverseMember]
    ) -> None:
        """Persist a universe snapshot keyed by its as-of date."""
        ...  # pragma: no cover

    def get_latest_universe_membership(
        self, as_of: date | None = None
    ) -> tuple[date, tuple[UniverseMember, ...]] | None:
        """Return the latest persisted snapshot not after `as_of`, if any."""
        ...  # pragma: no cover


def _normalize_symbol(source_symbol: str) -> str:
    """Translate a Wikipedia ticker (e.g. ``BRK.B``) into ``BRK-B`` form."""
    return source_symbol.strip().replace(".", "-")


def _get_wikipedia_page() -> str:
    """Fetch the Wikipedia constituents page with a compliant User-Agent.

    Wikipedia returns HTTP 403 to the default urllib User-Agent that a bare
    ``pd.read_html(url)`` call would send, so the HTML is fetched explicitly
    via `httpx` with a Wikimedia-UA-policy-compliant identity string first.
    """
    response = httpx.get(
        WIKIPEDIA_SP500_URL,
        headers={"User-Agent": _WIKIPEDIA_USER_AGENT},
        timeout=10.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def _fetch_wikipedia_html(sleep_fn: Callable[[float], None]) -> str:
    """Fetch the constituents page HTML with bounded retry on transient errors.

    Only transport errors and HTTP 408, 429, and 5xx responses are retried
    with fixed backoff. The final attempt is unguarded so a persistent failure
    propagates to the caller.
    """
    return retry_external_call(
        _get_wikipedia_page,
        before_attempt=lambda: None,
        sleep_fn=sleep_fn,
    )


def fetch_from_wikipedia(
    *, sleep_fn: Callable[[float], None] = time.sleep
) -> list[UniverseMember]:
    """Fetch current S&P 500 membership from the Wikipedia constituents table.

    Args:
        sleep_fn: Injectable sleep function used between retry attempts;
            tests inject a fake to stay instant and offline.

    Returns:
        Normalized S&P 500 constituents.
    """
    html = _fetch_wikipedia_html(sleep_fn)
    table = pd.read_html(io.StringIO(html))[0]
    return [
        UniverseMember(
            symbol=_normalize_symbol(str(row["Symbol"])),
            company_name=str(row["Security"]).strip(),
            gics_sector=str(row["GICS Sector"]).strip(),
            source_symbol=str(row["Symbol"]).strip(),
        )
        for _, row in table.iterrows()
    ]


def _read_snapshot(snapshot_path: Path) -> list[UniverseMember] | None:
    if not snapshot_path.is_file():
        return None
    with snapshot_path.open(newline="", encoding="utf-8") as handle:
        return [UniverseMember(**row) for row in csv.DictReader(handle)]


def _write_snapshot(snapshot_path: Path, members: Sequence[UniverseMember]) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=_SNAPSHOT_FIELDS)
    writer.writeheader()
    writer.writerows(dataclasses.asdict(member) for member in members)
    write_text_atomically(snapshot_path, buffer.getvalue())


def _fetch_and_write_snapshot(options: UniverseFetchOptions) -> list[UniverseMember]:
    """Fetch a non-empty provider membership and replace the local CSV cache."""
    try:
        fetched = options.fetch_fn()
    except Exception as exc:
        msg = "Failed to fetch the S&P 500 universe"
        raise UniverseError(msg) from exc

    if not fetched:
        msg = "Fetched S&P 500 universe is empty"
        raise UniverseError(msg)

    _write_snapshot(Path(options.snapshot_path), fetched)
    return fetched


def _apply_manual_overrides(
    members: Sequence[UniverseMember],
    manual_include: Sequence[str],
    manual_exclude: Sequence[str],
) -> list[UniverseMember]:
    excluded = {symbol.strip().upper() for symbol in manual_exclude}
    kept = [member for member in members if member.symbol.upper() not in excluded]

    present = {member.symbol.upper() for member in kept}
    for symbol in manual_include:
        normalized = symbol.strip().upper()
        if normalized and normalized not in present:
            kept.append(
                UniverseMember(
                    symbol=normalized,
                    company_name=normalized,
                    gics_sector="Unknown",
                    source_symbol=normalized,
                )
            )
            present.add(normalized)
    return kept


@dataclass(frozen=True, slots=True)
class UniverseFetchOptions:
    """Grouped, injectable knobs for universe fetch/cache/override behavior."""

    snapshot_path: Path | str = DEFAULT_SNAPSHOT_PATH
    manual_include: Sequence[str] = ()
    manual_exclude: Sequence[str] = ()
    fetch_fn: Callable[[], list[UniverseMember]] = fetch_from_wikipedia


def get_sp500_universe(
    as_of: date,  # noqa: ARG001 - reserved for future point-in-time universes
    force_refresh: bool = False,
    *,
    options: UniverseFetchOptions | None = None,
) -> list[UniverseMember]:
    """Return current S&P 500 membership, applying manual overrides.

    Args:
        as_of: Evaluation date (reserved; membership is not yet point-in-time
            historical — see docs/04_detailed_design.md 3.2).
        force_refresh: When true, always refetch instead of preferring the
            cached snapshot.
        options: Snapshot path, manual overrides, and fetch function
            (defaults to the live Wikipedia fetch); tests inject a
            fixture-backed fake here.

    Returns:
        The overridden membership list.

    Raises:
        UniverseError: The fetch failed and no snapshot fallback exists.
    """
    opts = options or UniverseFetchOptions()
    path = Path(opts.snapshot_path)

    if not force_refresh:
        cached = _read_snapshot(path)
        if cached is not None:
            return _apply_manual_overrides(
                cached, opts.manual_include, opts.manual_exclude
            )

    try:
        fetched = _fetch_and_write_snapshot(opts)
    except UniverseError as exc:
        fallback = _read_snapshot(path)
        if fallback is None:
            msg = "Failed to fetch the S&P 500 universe and no snapshot fallback exists"
            raise UniverseError(msg) from exc
        return _apply_manual_overrides(
            fallback, opts.manual_include, opts.manual_exclude
        )

    return _apply_manual_overrides(fetched, opts.manual_include, opts.manual_exclude)


def refresh_universe(
    as_of: date,
    state_store: UniverseStateStore,
    *,
    options: UniverseFetchOptions | None = None,
) -> list[UniverseMember]:
    """Force-refetch the universe, persist it, and return the new membership.

    Args:
        as_of: Snapshot date to persist the membership under.
        state_store: Store implementing ``UniverseStateStore``.
        options: Snapshot path, manual overrides, and fetch function.

    Returns:
        The refreshed, overridden membership list.

    Raises:
        UniverseError: The fetch failed and no snapshot fallback exists.
    """
    opts = options or UniverseFetchOptions()
    members = _fetch_and_write_snapshot(opts)
    state_store.record_universe_membership(as_of, members)
    return _apply_manual_overrides(members, opts.manual_include, opts.manual_exclude)


def _resolve_persisted_universe(
    snapshot_date: date,
    members: Sequence[UniverseMember],
    options: UniverseFetchOptions,
    *,
    warning: str | None = None,
) -> UniverseResolution:
    """Apply run-local overrides without mutating raw persisted membership."""
    return UniverseResolution(
        members=tuple(
            _apply_manual_overrides(
                members, options.manual_include, options.manual_exclude
            )
        ),
        snapshot_date=snapshot_date,
        warning=warning,
    )


def select_persisted_universe(
    as_of: date,
    state_store: UniverseStateStore,
    *,
    options: UniverseFetchOptions | None = None,
) -> UniverseResolution | None:
    """Select raw StateStore history visible at ``as_of`` without network I/O."""
    persisted = state_store.get_latest_universe_membership(as_of)
    if persisted is None:
        return None

    snapshot_date, members = persisted
    return _resolve_persisted_universe(
        snapshot_date, members, options or UniverseFetchOptions()
    )


def resolve_daily_universe(
    as_of: date,
    state_store: UniverseStateStore,
    *,
    is_historical: bool,
    refresh_interval_days: int,
    options: UniverseFetchOptions | None = None,
) -> UniverseResolution:
    """Resolve the one universe snapshot visible to a daily run.

    Explicit ``--as-of`` runs are historical and therefore never refresh: the
    latest StateStore membership with ``snapshot_date <= as_of`` is required.
    Live runs reuse a younger snapshot, otherwise refetch and persist raw
    provider membership to both CSV and DuckDB. A live refresh failure can use
    a previously persisted snapshot, but is returned with an explicit warning.
    """
    if refresh_interval_days < 1:
        msg = "refresh_interval_days must be at least 1"
        raise ValueError(msg)

    opts = options or UniverseFetchOptions()
    persisted = state_store.get_latest_universe_membership(as_of)

    if is_historical:
        if persisted is None:
            msg = (
                "No persisted universe snapshot is available at or before "
                f"{as_of.isoformat()} for this historical run"
            )
            raise UniverseError(msg)
        snapshot_date, members = persisted
        return _resolve_persisted_universe(snapshot_date, members, opts)

    if persisted is not None:
        snapshot_date, members = persisted
        if (as_of - snapshot_date).days < refresh_interval_days:
            return _resolve_persisted_universe(snapshot_date, members, opts)

    try:
        refreshed = _fetch_and_write_snapshot(opts)
        state_store.record_universe_membership(as_of, refreshed)
    except UniverseError as exc:
        if persisted is None:
            msg = (
                "Failed to resolve the live universe and no persisted universe "
                "snapshot is available"
            )
            raise UniverseError(msg) from exc
        snapshot_date, members = persisted
        warning = (
            f"Universe refresh failed; using persisted snapshot "
            f"{snapshot_date.isoformat()}: {exc.__cause__ or exc}"
        )
        return _resolve_persisted_universe(
            snapshot_date, members, opts, warning=warning
        )

    return _resolve_persisted_universe(as_of, refreshed, opts)
