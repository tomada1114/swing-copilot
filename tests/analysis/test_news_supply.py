"""Symbol-specific news supply measurement (`analysis/news_supply.py`)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from swing_copilot.analysis.news_supply import (
    DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS,
    measure_news_supply,
)
from swing_copilot.analysis.schemas import NewsInput
from swing_copilot.text.base import TextItem

STAMP = datetime(2027, 3, 1, tzinfo=UTC)


def _exported(*bodies: str, headline: str | None = "headline") -> list[NewsInput]:
    """Exported news items whose summaries are the given bodies."""
    return [
        NewsInput(
            source_id=f"finnhub:{index}",
            published_at=STAMP,
            headline=headline,
            summary=body,
            url="https://example.com/news",
            provider="finnhub",
        )
        for index, body in enumerate(bodies)
    ]


def _collected(count: int, source_type: str = "news") -> list[TextItem]:
    """Collected text items of one type, only their number being relevant."""
    return [
        TextItem(
            source_id=f"collected:{source_type}:{index}",
            symbol="JBHT",
            source_type=source_type,
            published_at=STAMP,
            title="headline",
            source_url="https://example.com/collected",
            content_text="body",
            fetched_at=STAMP,
        )
        for index in range(count)
    ]


class TestSupplyLevel:
    def test_at_the_threshold_the_supply_is_sufficient(self):
        bodies = ["JBHT reported something."] * DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS

        supply = measure_news_supply(
            "JBHT", _collected(len(bodies)), _exported(*bodies)
        )

        assert supply.symbol_mention_items == DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS
        assert supply.level == "sufficient"

    def test_one_below_the_threshold_the_supply_is_sparse(self):
        bodies = ["JBHT reported something."] * (
            DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS - 1
        )
        padding = ["Schneider Q2 earnings beat estimates."] * 16

        supply = measure_news_supply(
            "JBHT", _collected(len(bodies) + len(padding)), _exported(*bodies, *padding)
        )

        assert (
            supply.symbol_mention_items == DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS - 1
        )
        assert supply.level == "sparse"

    def test_a_full_feed_that_never_names_the_symbol_reports_none(self):
        bodies = ["ArcBest Q2 earnings beat estimates."] * 20

        supply = measure_news_supply("JBHT", _collected(20), _exported(*bodies))

        assert supply.exported_items == 20
        assert supply.symbol_mention_items == 0
        assert supply.level == "none"

    def test_an_empty_feed_reports_none_with_zero_counts(self):
        supply = measure_news_supply("JBHT", [], [])

        assert (supply.collected_items, supply.exported_items) == (0, 0)
        assert supply.level == "none"


class TestMentionCounting:
    @pytest.mark.parametrize(
        ("body", "is_counted"),
        [
            pytest.param("Shares of JBHT rose.", True, id="bare-token"),
            pytest.param("J.B. Hunt (JBHT) rose.", True, id="parenthesized"),
            pytest.param("The quarter belonged to JBHT.", True, id="sentence-final"),
            pytest.param("ARCB or JBHT: which is better?", True, id="colon-delimited"),
            pytest.param("JBHTX fund holdings changed.", False, id="longer-token"),
            pytest.param("Ticker XJBHT is unrelated.", False, id="suffix-of-token"),
            pytest.param("jbht in lower case is prose.", False, id="lower-case"),
            pytest.param(
                "J.B. Hunt celebrates 65 years.", False, id="name-without-tag"
            ),
        ],
    )
    def test_only_a_standalone_upper_case_ticker_counts(self, body, is_counted):
        supply = measure_news_supply("JBHT", _collected(1), _exported(body))

        assert supply.symbol_mention_items == int(is_counted)

    def test_a_mention_in_the_headline_alone_counts(self):
        exported = _exported("Sector volumes rose.", headline="JBHT lifts guidance")

        supply = measure_news_supply("JBHT", _collected(1), exported)

        assert supply.symbol_mention_items == 1

    def test_a_headless_item_is_measured_by_its_summary(self):
        exported = _exported("JBHT lifted guidance.", headline=None)

        supply = measure_news_supply("JBHT", _collected(1), exported)

        assert supply.symbol_mention_items == 1

    def test_a_dictionary_word_ticker_is_not_matched_by_its_lower_case_word(self):
        exported = _exported("The stock gave back all of its gains.")

        supply = measure_news_supply("ALL", _collected(1), exported)

        assert supply.symbol_mention_items == 0


class TestCollectedCount:
    def test_only_news_items_count_as_collected_supply(self):
        collected = [*_collected(3), *_collected(2, source_type="filing")]

        supply = measure_news_supply(
            "JBHT", collected, _exported("JBHT reported something.")
        )

        assert supply.collected_items == 3

    def test_a_feed_larger_than_the_export_budget_keeps_both_counts(self):
        supply = measure_news_supply(
            "JBHT", _collected(40), _exported("JBHT reported something.")
        )

        assert (supply.collected_items, supply.exported_items) == (40, 1)


class TestConfiguredThreshold:
    """Issue #191: the `sufficient` floor is an operator setting, not a constant."""

    def test_a_lowered_floor_grades_the_same_feed_as_sufficient(self):
        exported = _exported(*(["JBHT reported something."] * 3))

        supply = measure_news_supply("JBHT", _collected(3), exported, 3)

        assert supply.level == "sufficient"

    def test_a_raised_floor_grades_the_same_feed_as_sparse(self):
        exported = _exported(*(["JBHT reported something."] * 6))

        supply = measure_news_supply("JBHT", _collected(6), exported, 10)

        assert supply.level == "sparse"

    def test_a_zero_mention_feed_stays_none_whatever_the_floor(self):
        """`none` is a measured absence, not a position relative to the floor."""
        supply = measure_news_supply("JBHT", _collected(1), _exported("Nothing."), 1)

        assert supply.level == "none"

    def test_the_default_is_the_shipped_calibration(self):
        exported = _exported(
            *(["JBHT reported something."] * DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS)
        )

        supply = measure_news_supply("JBHT", _collected(5), exported)

        assert supply.level == "sufficient"
