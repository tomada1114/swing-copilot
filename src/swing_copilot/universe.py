"""S&P 500 universe fetch, snapshot fallback, and manual overrides (FR-01).

``get_sp500_universe`` returns the current membership, preferring the local
CSV snapshot when ``force_refresh`` is false and falling back to it if a
Wikipedia refetch fails (NFR-04). ``refresh_universe`` always refetches and
persists the result through ``UniverseStateStore``, a narrow structural
Protocol that ``storage.state_store.StateStore`` satisfies once it exists;
the daily pipeline decides when a refresh (vs. the cached snapshot) is due.
"""

from __future__ import annotations

import csv
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pandas as pd

from swing_copilot.exceptions import SwingCopilotError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date

WIKIPEDIA_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
DEFAULT_SNAPSHOT_PATH = Path("config/universe_snapshot.csv")
_SNAPSHOT_FIELDS = ("symbol", "company_name", "gics_sector", "source_symbol")


class UniverseError(SwingCopilotError):
    """Raised when the universe cannot be fetched and no snapshot exists."""


@dataclass(frozen=True, slots=True)
class UniverseMember:
    """One S&P 500 constituent, before and after symbol normalization."""

    symbol: str
    company_name: str
    gics_sector: str
    source_symbol: str


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


def fetch_from_wikipedia() -> list[UniverseMember]:
    """Fetch current S&P 500 membership from the Wikipedia constituents table."""
    table = pd.read_html(WIKIPEDIA_SP500_URL)[0]
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
    tmp_path = snapshot_path.with_name(snapshot_path.name + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_SNAPSHOT_FIELDS)
        writer.writeheader()
        writer.writerows(dataclasses.asdict(member) for member in members)
    tmp_path.replace(snapshot_path)


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
        fetched = opts.fetch_fn()
    except Exception as exc:
        fallback = _read_snapshot(path)
        if fallback is None:
            msg = "Failed to fetch the S&P 500 universe and no snapshot fallback exists"
            raise UniverseError(msg) from exc
        return _apply_manual_overrides(
            fallback, opts.manual_include, opts.manual_exclude
        )

    _write_snapshot(path, fetched)
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
    members = get_sp500_universe(as_of, force_refresh=True, options=options)
    state_store.record_universe_membership(as_of, members)
    return members
