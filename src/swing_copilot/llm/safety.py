"""Post-output CON-03 guard: reject imperative buy/sell language (FR-08, CON-03).

Prompt instructions alone cannot guarantee the model won't slip into giving
trading advice, so every parsed `NewsSummary`/`FilingAnalysis` is checked here
after the fact; a violation degrades that analysis to a failure without a
retry (`docs/04_detailed_design.md` 3.15).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from swing_copilot.exceptions import SwingCopilotError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pydantic import BaseModel

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
    """Raised when model output contains prohibited imperative trading language."""


def check_no_imperative_language(texts: Iterable[str]) -> None:
    """Raise if any text contains a forbidden imperative buy/sell phrase.

    Args:
        texts: Free-text fields from a parsed `NewsSummary`/`FilingAnalysis`.

    Raises:
        ForbiddenLanguageError: A forbidden phrase was found.
    """
    for text in texts:
        lowered = text.lower()
        for phrase in FORBIDDEN_PHRASES:
            if phrase.lower() in lowered:
                msg = f"Output contains forbidden imperative language: {phrase!r}"
                raise ForbiddenLanguageError(msg)


def check_structured_output(parsed: BaseModel) -> None:
    """Check every user-visible free-text field in a supported LLM schema.

    Args:
        parsed: Parsed `NewsSummary` or `FilingAnalysis` output.

    Raises:
        ForbiddenLanguageError: A prohibited phrase appears in any checked field.
    """
    texts: list[str] = []
    facts = getattr(parsed, "facts", ())
    texts.extend(fact.statement for fact in facts)
    for field_name in ("interpretation", "risk_flags", "red_flags", "yoy_changes"):
        values = getattr(parsed, field_name, ())
        texts.extend(value for value in values if isinstance(value, str))
    check_no_imperative_language(texts)
