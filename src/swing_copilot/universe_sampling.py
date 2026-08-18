"""Deterministic `--limit` sampling of the universe, shared by both CLIs.

`copilot-backtest --limit N` (Issue #194) and `copilot-daily --limit N`
(Issue #205) both mean "run against N of the universe". Truncating a
`symbol`-sorted membership made that "the N tickers starting with A", whose
sector mix is not the S&P 500's and which changes what a universe-relative
check *means*: Minervini's RS percentile (condition 7) ranks candidates within
the set it is given. The sampler here keeps each `gics_sector`'s proportional
share and decides which of its members are taken by a salted blake2b order —
unrelated to the alphabet, identical on every machine and every rerun.

The two CLIs deliberately share the salt, so a `--limit N` smoke run and a
`--limit N` backtest over the same universe cover the same symbols.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from swing_copilot.universe import UniverseMember

#: Salt of the `--limit` sample's shuffle order (Issue #194). Fixed forever:
#: changing it re-draws every sample and silently breaks comparability with
#: previously published reports. The value keeps its original `backtest`
#: wording for exactly that reason, even though `copilot-daily` now shares it
#: (Issue #205).
_SAMPLING_SALT = b"swing-copilot/backtest/limit/v1"

__all__ = ["UniverseSample", "select_universe_sample"]


@dataclass(frozen=True, slots=True)
class UniverseSample:
    """Which symbols a run actually covered, and how they were chosen.

    Carried into every backtest report because a `--limit` run measures a
    *sample*: without the method and the sector composition beside the
    metrics, a truncated run reads exactly like a full-universe one
    (Issue #194).
    """

    symbols: tuple[str, ...]
    universe_size: int
    is_stratified_sample: bool
    sector_counts: tuple[tuple[str, int], ...]

    def summary_lines(self) -> tuple[str, ...]:
        """The two lines every report prepends: method, then composition."""
        if not self.is_stratified_sample:
            method = f"ユニバース: 全 {self.universe_size} 銘柄（--limit 指定なし）"
        else:
            method = (
                f"ユニバース: {len(self.symbols)}/{self.universe_size} 銘柄の"
                "決定論的サンプル（gics_sector 比例配分 + blake2b ハッシュ順、"
                "シード固定・再現可能）"
            )
        composition = "セクター構成: " + (
            ", ".join(f"{sector} {count}" for sector, count in self.sector_counts)
            or "(なし)"
        )
        return (method, composition)


def _hash_rank(symbol: str) -> bytes:
    """Deterministic, universe-independent shuffle key for one symbol."""
    return hashlib.blake2b(
        _SAMPLING_SALT + symbol.encode("utf-8"), digest_size=8
    ).digest()


def _sector_counts(members: Sequence[UniverseMember]) -> tuple[tuple[str, int], ...]:
    counts = Counter(member.gics_sector for member in members)
    return tuple(sorted(counts.items()))


def _sector_quotas(sizes: dict[str, int], limit: int) -> dict[str, int]:
    """Split `limit` across sectors proportionally, by largest remainder.

    Every sector keeps its floor share; the seats left over go to the largest
    fractional remainders, ties broken by sector name so the allocation is a
    pure function of the universe and the limit.
    """
    total = sum(sizes.values())
    exact = {sector: size * limit / total for sector, size in sizes.items()}
    quotas = {sector: int(value) for sector, value in exact.items()}
    # Each remainder is < 1 and they sum to an integer, so only sectors with a
    # non-zero remainder can be topped up and no quota can exceed its sector.
    leftover = limit - sum(quotas.values())
    by_remainder = sorted(exact, key=lambda sector: (-(exact[sector] % 1), sector))
    for sector in by_remainder[:leftover]:
        quotas[sector] += 1
    return quotas


def select_universe_sample(
    universe: Sequence[UniverseMember], limit: int | None
) -> UniverseSample:
    """Pick `limit` symbols without the alphabetical bias of `[:limit]`.

    Args:
        universe: Resolved universe membership for the run's `as_of`, in any
            order (the result does not depend on it).
        limit: `--limit`, or `None` for the whole universe. `0` selects no
            symbol at all, which is `copilot-daily --limit 0`'s
            holdings-only contract.

    Returns:
        The selected symbols (alphabetical) plus the provenance a backtest
        report prints beside its metrics.

    Raises:
        ValueError: If `limit` is negative. Both CLIs reject that earlier;
            reaching here would mean a negative slice silently selecting
            symbols from the end of the universe.
    """
    if limit is not None and limit < 0:
        msg = f"limit は0以上でなければなりません: {limit}"
        raise ValueError(msg)
    members = sorted(universe, key=lambda member: member.symbol)
    if limit is None or limit >= len(members):
        return UniverseSample(
            symbols=tuple(member.symbol for member in members),
            universe_size=len(members),
            is_stratified_sample=False,
            sector_counts=_sector_counts(members),
        )

    by_sector: dict[str, list[UniverseMember]] = defaultdict(list)
    for member in members:
        by_sector[member.gics_sector].append(member)
    quotas = _sector_quotas(
        {sector: len(group) for sector, group in by_sector.items()}, limit
    )
    picked = [
        member
        for sector, group in sorted(by_sector.items())
        for member in sorted(group, key=lambda member: _hash_rank(member.symbol))[
            : quotas[sector]
        ]
    ]
    return UniverseSample(
        symbols=tuple(sorted(member.symbol for member in picked)),
        universe_size=len(members),
        is_stratified_sample=True,
        sector_counts=_sector_counts(picked),
    )
