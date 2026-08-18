"""Tests for the `--limit` universe sampler shared by both CLIs (Issues #194/#205)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from swing_copilot.universe import UniverseMember
from swing_copilot.universe_sampling import select_universe_sample

if TYPE_CHECKING:
    from collections.abc import Sequence


def _members(symbols: Sequence[str], sector: str) -> list[UniverseMember]:
    return [
        UniverseMember(
            symbol=symbol,
            company_name=symbol,
            gics_sector=sector,
            source_symbol=symbol,
        )
        for symbol in symbols
    ]


def _universe() -> tuple[UniverseMember, ...]:
    return tuple(_members(("AAA", "BBB", "CCC"), "Information Technology"))


def _sectored_universe(sizes: dict[str, int]) -> tuple[UniverseMember, ...]:
    """One member per slot, named so alphabetical order is fully predictable."""
    members: list[UniverseMember] = []
    for sector, size in sorted(sizes.items()):
        offset = len(members)
        members += _members([f"S{offset + index:03d}" for index in range(size)], sector)
    return tuple(members)


class TestSelectUniverseSample:
    """Issue #194: `--limit` samples the universe instead of truncating it."""

    def test_no_limit_returns_the_whole_universe(self):
        sample = select_universe_sample(_universe(), None)

        assert sample.symbols == ("AAA", "BBB", "CCC")
        assert sample.is_stratified_sample is False
        assert sample.universe_size == 3
        assert sample.sector_counts == (("Information Technology", 3),)

    def test_limit_at_or_above_the_universe_size_returns_the_whole_universe(self):
        assert select_universe_sample(_universe(), 3).symbols == ("AAA", "BBB", "CCC")
        assert select_universe_sample(_universe(), 99).symbols == ("AAA", "BBB", "CCC")

    def test_limit_zero_selects_no_symbol(self):
        # `copilot-daily --limit 0`'s holdings-only contract reaches here.
        sample = select_universe_sample(_universe(), 0)

        assert sample.symbols == ()
        assert sample.is_stratified_sample is True
        assert sample.universe_size == 3
        assert sample.sector_counts == ()

    def test_empty_universe_is_not_a_sample(self):
        sample = select_universe_sample((), 5)

        assert sample.symbols == ()
        assert sample.is_stratified_sample is False
        assert sample.universe_size == 0

    def test_negative_limit_raises_value_error(self):
        with pytest.raises(ValueError, match=r"limit は0以上"):
            select_universe_sample(_universe(), -1)

    def test_limit_is_not_the_alphabetically_first_n_symbols(self):
        # The regression: `symbols[:limit]` returned exactly S000..S019.
        universe = _sectored_universe({"Energy": 100, "Utilities": 100})

        sample = select_universe_sample(universe, 20)

        alphabetical_head = tuple(f"S{index:03d}" for index in range(20))
        assert len(sample.symbols) == 20
        assert sample.symbols != alphabetical_head
        # Spread across the whole alphabet, not clustered at its start.
        assert max(sample.symbols) > "S150"

    def test_same_universe_and_limit_always_select_the_same_symbols(self):
        universe = _sectored_universe({"Energy": 40, "Utilities": 60})
        shuffled = tuple(reversed(universe))

        first = select_universe_sample(universe, 25)
        again = select_universe_sample(universe, 25)
        from_shuffled_input = select_universe_sample(shuffled, 25)

        assert first.symbols == again.symbols == from_shuffled_input.symbols

    def test_selection_is_pinned_to_a_known_set_across_machines_and_days(self):
        # A golden set, not just a same-process rerun: the salted blake2b order
        # must survive interpreter restarts (no PYTHONHASHSEED dependency) and
        # produce the same sample on every machine and every run date.
        universe = _sectored_universe({"Energy": 6, "Utilities": 6})

        sample = select_universe_sample(universe, 4)

        assert sample.symbols == ("S001", "S003", "S008", "S011")

    def test_sector_shares_are_proportional_to_the_universe(self):
        universe = _sectored_universe(
            {"Energy": 100, "Financials": 60, "Health Care": 30, "Utilities": 10}
        )

        sample = select_universe_sample(universe, 20)

        assert sample.is_stratified_sample is True
        assert sample.universe_size == 200
        assert sample.sector_counts == (
            ("Energy", 10),
            ("Financials", 6),
            ("Health Care", 3),
            ("Utilities", 1),
        )

    def test_leftover_seats_go_to_the_largest_remainder_ties_broken_by_name(self):
        # 4 seats over three equal sectors: every sector floors to 1 and the
        # single leftover goes to the alphabetically first of the tied three.
        universe = _sectored_universe({"Aaa": 3, "Bbb": 3, "Ccc": 3})

        sample = select_universe_sample(universe, 4)

        assert sample.sector_counts == (("Aaa", 2), ("Bbb", 1), ("Ccc", 1))

    def test_a_limit_smaller_than_the_sector_count_still_fills_every_seat(self):
        universe = _sectored_universe({"Energy": 100, "Utilities": 100})

        sample = select_universe_sample(universe, 1)

        assert len(sample.symbols) == 1

    def test_summary_lines_state_the_method_and_the_sector_composition(self):
        universe = _sectored_universe({"Energy": 100, "Utilities": 100})

        method, composition = select_universe_sample(universe, 20).summary_lines()

        assert "20/200" in method
        assert "blake2b" in method
        assert composition == "セクター構成: Energy 10, Utilities 10"

    def test_full_universe_summary_says_so(self):
        method, _composition = select_universe_sample(_universe(), None).summary_lines()

        assert "全 3 銘柄" in method

    def test_empty_sample_composition_says_none(self):
        _method, composition = select_universe_sample(_universe(), 0).summary_lines()

        assert composition == "セクター構成: (なし)"
