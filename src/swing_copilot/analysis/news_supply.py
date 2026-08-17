"""Measure a candidate's own news supply inside its exported feed (Issue #130).

`_news_inputs()` decides *which* articles are exported and in what order; this
module measures *how much of the result is about the candidate at all*, so the
downstream analysis can tell "no bad news was reported" apart from "hardly any
company-specific news was supplied".

The measurement is a ticker mention over the exported headline and summary --
the same text the skill reads -- rather than the source-declared
`TextItem.related_symbols`. Two reasons: the collected Finnhub rows carry no
`related` value at all in practice (every persisted news row has it `NULL`,
so a metadata-based count would report the same "unmeasurable" verdict every
day), and metadata attribution is Issue #123's subject, whereas this issue is
about a feed whose attribution is right and whose *content* is somebody else's
story.

The count is deliberately coarse and biased low: an article that discusses the
company without printing its ticker is missed, and a one- or two-letter ticker
can be matched by an unrelated capitalized token. That is why the pipeline's
answer is a declared level a reader must qualify their conclusion with, not an
automated exclusion of the articles themselves.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from swing_copilot.analysis.schemas import NewsSupply

if TYPE_CHECKING:
    from collections.abc import Sequence

    from swing_copilot.analysis.schemas import NewsInput, NewsSupplyLevel
    from swing_copilot.text.base import TextItem

#: Default for `settings.analysis.sufficient_news_mention_items`: at or above
#: this many symbol-naming articles, the exported feed is treated as carrying
#: enough company-specific material for an absence of bad news to mean
#: something. Calibrated against the 2026-08-11 run under the default
#: `max_news_items_per_symbol: 20`: the eight healthy candidates supplied 6-15
#: such articles, while the J.B. Hunt feed Issue #130 reports supplied 4. The
#: floor is absolute, not a share of the budget -- four company-specific
#: articles are too thin to conclude anything from, however small the budget
#: that produced them.
#:
#: It is a *default*, not a constant, since Issue #191: the calibration above
#: is a single point estimate on one run, so the operator has to be able to
#: retune it from `settings.yaml` without a code change (the repo's "閾値は要
#: 検証・config 化" principle). Code that grades a feed must take the effective
#: value as an argument rather than importing this.
DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS = 5


def _mention_pattern(symbol: str) -> re.Pattern[str]:
    """Compile a case-sensitive standalone-token matcher for `symbol`.

    Case-sensitive because tickers are printed upper case, and matching case
    insensitively would count every English word that happens to spell one
    (`all`, `key`, `on`). Boundaries are non-alphanumeric so `"(JBHT)"` and
    `"JBHT."` count while `"JBHTX"` does not.
    """
    ticker = re.escape(symbol.strip().upper())
    return re.compile(rf"(?<![A-Za-z0-9]){ticker}(?![A-Za-z0-9])")


def measure_news_supply(
    symbol: str,
    collected: Sequence[TextItem],
    exported: Sequence[NewsInput],
    sufficient_mention_items: int = DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS,
) -> NewsSupply:
    """Count how many exported articles name `symbol`, and grade the supply.

    Args:
        symbol: The candidate's ticker.
        collected: Every text item collected for the candidate; non-news items
            are ignored, so callers can pass the candidate's whole set.
        exported: The news items actually written into `analysis_input.json`.
        sufficient_mention_items: The `sufficient` floor, from
            `settings.analysis.sufficient_news_mention_items`.

    Returns:
        The candidate-level supply block, graded `none` when no exported
        article names the symbol, `sparse` below `sufficient_mention_items`,
        and `sufficient` at or above it.
    """
    pattern = _mention_pattern(symbol)
    mentions = sum(
        1
        for item in exported
        if pattern.search(f"{item.headline or ''}\n{item.summary}")
    )
    return NewsSupply(
        collected_items=sum(1 for item in collected if item.source_type == "news"),
        exported_items=len(exported),
        symbol_mention_items=mentions,
        level=_level(mentions, sufficient_mention_items),
    )


def _level(mentions: int, sufficient_mention_items: int) -> NewsSupplyLevel:
    """Grade a symbol-mention count into the declared supply level."""
    if mentions == 0:
        return "none"
    if mentions < sufficient_mention_items:
        return "sparse"
    return "sufficient"
