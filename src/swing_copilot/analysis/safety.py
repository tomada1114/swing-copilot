"""Post-output CON-03 guard: reject imperative buy/sell language (FR-08, CON-03).

Skill instructions alone cannot guarantee a model won't slip into giving
trading advice, so every user-visible free-text field of a parsed
`AnalysisResult` is checked here before it can reach a report. A violation
degrades that symbol's qualitative section without a retry
(`analysis/validate.py`).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from swing_copilot.exceptions import SwingCopilotError

if TYPE_CHECKING:
    from collections.abc import Iterable

# REQ-009/021: a small, documented (not exhaustive) list of Japanese/English
# keywords asserting an investor/management psychological or behavioral state.
# Presence alone isn't forbidden -- only when it isn't paired with concrete
# actual-vs-planned numeric evidence in the same text (see `_HEDGE_PATTERN`/
# `_NUMERIC_EVIDENCE_PATTERN` below).
_BEHAVIORAL_KEYWORDS: tuple[str, ...] = (
    "動揺",
    "パニック",
    "狼狽",
    "投資家心理",
    "panic",
    "investor sentiment",
    "management is anxious",
)
# A hedge phrase ("〜の可能性" / "possible"/"possibly") permitted only when
# co-occurring with concrete evidence in the same text (below).
_HEDGE_PATTERN = re.compile(r"可能性|possibly|possible", re.IGNORECASE)
# Concrete actual-vs-planned numeric discrepancy requires all three signals
# in the same text: a percentage, an actual marker, and a plan marker.
_PERCENTAGE_PATTERN = re.compile(r"\d+(\.\d+)?\s*%")
_ACTUAL_PATTERN = re.compile(r"実績|actual", re.IGNORECASE)
_PLAN_PATTERN = re.compile(r"計画|予想|planned|forecast", re.IGNORECASE)

FORBIDDEN_PHRASES: tuple[str, ...] = (
    "買うべき",
    "売るべき",
    "今すぐ買う",
    "今すぐ売る",
    "買いです",
    "売りです",
    "強く推奨",
    "you should buy",
    "you should sell",
    "buy now",
    "sell now",
    "must buy",
    "must sell",
    "recommend buying",
    "recommend selling",
    "strong buy",
    "strong sell",
    "購入すべき",
    "売却すべき",
    "購入してください",
    "売却してください",
    "買い推奨",
    "売り推奨",
)


class ForbiddenLanguageError(SwingCopilotError):
    """Raised when skill output contains prohibited imperative trading language."""


def check_no_imperative_language(texts: Iterable[str]) -> None:
    """Raise if any text contains a forbidden imperative buy/sell phrase.

    Args:
        texts: Free-text fields from a parsed `AnalysisResult`.

    Raises:
        ForbiddenLanguageError: A forbidden phrase was found.
    """
    for text in texts:
        lowered = text.lower()
        for phrase in FORBIDDEN_PHRASES:
            if phrase.lower() in lowered:
                msg = f"Output contains forbidden imperative language: {phrase!r}"
                raise ForbiddenLanguageError(msg)


def check_no_unevidenced_behavioral_claims(texts: Iterable[str]) -> None:
    """Raise if a bare psychological/behavioral diagnosis lacks paired evidence.

    REQ-009/REQ-021: "〜の可能性(possible pattern)" language describing
    investor/management behavior is only permitted when the same statement
    carries a concrete actual-vs-planned numeric discrepancy (a hedge phrase
    co-occurring with a percentage or an 実績/計画/予想/actual/planned marker
    in the same text). A bare assertion of an emotional/behavioral state
    without that co-occurring evidence is an unfalsifiable psychological
    diagnosis and is forbidden.

    Args:
        texts: Free-text fields from a parsed `AnalysisResult`.

    Raises:
        ForbiddenLanguageError: A behavioral/psychological keyword appears
            without the required hedge + numeric-evidence co-occurrence.
    """
    for text in texts:
        lowered = text.lower()
        has_keyword = any(
            keyword.lower() in lowered for keyword in _BEHAVIORAL_KEYWORDS
        )
        if not has_keyword:
            continue
        has_evidence = (
            _PERCENTAGE_PATTERN.search(text)
            and _ACTUAL_PATTERN.search(text)
            and _PLAN_PATTERN.search(text)
        )
        if _HEDGE_PATTERN.search(text) and has_evidence:
            continue
        msg = f"Output contains an unevidenced behavioral/psychological claim: {text!r}"
        raise ForbiddenLanguageError(msg)


def check_display_texts(texts: Iterable[str]) -> None:
    """Apply every CON-03 check to one collection of user-visible strings.

    Args:
        texts: Every free-text field that would be rendered for this symbol.

    Raises:
        ForbiddenLanguageError: A prohibited phrase, or an unevidenced
            behavioral/psychological claim, appears in any checked field.
    """
    materialized = list(texts)
    check_no_imperative_language(materialized)
    check_no_unevidenced_behavioral_claims(materialized)
