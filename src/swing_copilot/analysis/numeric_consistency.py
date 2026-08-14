"""Numeric agreement between a fact's `text` and its `evidence_quote` (FR-08).

`evidence.py` proves the quote occurs in a supplied body. It cannot prove the
statement *written from* that quote restates it correctly. A 2026-08-11 run
showed the gap concretely (Issue #131): a filing reported
`Total operating revenues ... 3,495,296` (thousands) and the quote cited was
verbatim, yet the Japanese fact said 3.5953 billion instead of 3.4953 billion.
Provenance, evidence, and CON-03 all passed -- none of them reads digits.

This module closes the mechanically checkable part of that gap. The check is
deliberately narrow, because the honest cases it must not disturb are the
common ones:

* **Only figures carrying a magnitude or currency marker are checked.** A
  unit conversion is what goes wrong, so a bare number (a year, a quarter, a
  share count, a percentage the analyst derived rather than read) is left to
  the human review step. Widening the net would flag arithmetic the quote was
  never meant to contain.
* **Powers of ten are ignored unless both sides name their own.** A filing
  table states thousands, its press release states billions, and the fact
  states 億/万; when only one side spells its magnitude out, nothing settles
  the conversion, so only the significand is compared and every conversion in
  that family is admitted. When *both* sides spell it out the power of ten is
  no longer a guess and is compared too. Issue #158: `$119.8B` restated as
  「119.8億ドル」 -- a factor of ten low -- agreed on its significand and
  passed silently, while the correct 「1,198億ドル」 agreed just as well.
* **Agreement is judged at the coarser side's precision, and trailing zeros
  are never significant.** `$3.50 billion` and 34億9,530万ドル agree at three
  digits; 35億9,530万ドル does not. Reading `3.50` as two significant digits
  rather than three is the lenient choice, and leniency is what keeps an
  honest rounding from being reported.

The result is a warning channel, not a gate: `validate.py` logs it and still
renders the symbol. A false positive must cost a second look, never a lost
analysis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Final

from swing_copilot.analysis.evidence import normalize_evidence_text

if TYPE_CHECKING:
    from collections.abc import Iterator

#: A decimal literal, with or without thousands separators.
_NUMBER_PATTERN: Final = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")

#: Japanese magnitude characters, which compose (34億9,530万) rather than
#: replacing one another, mapped to their power of ten.
_JAPANESE_SCALES: Final = {"兆": 12, "億": 8, "万": 4, "千": 3}

#: English magnitude words, which never compose.
_ENGLISH_SCALES: Final = {
    "trillion": 12,
    "billion": 9,
    "million": 6,
    "thousand": 3,
}
_ENGLISH_SCALE_PATTERN: Final = re.compile(
    rf"(?:{'|'.join(_ENGLISH_SCALES)})\b",
)

#: Single-letter abbreviations of the same words. Read only when a currency
#: symbol introduces the figure and the letter is written against the digits
#: ($119.8B), because a letter trailing a bare number is far more often
#: something else -- `Rule 10b-5`, an exhibit or note reference.
_ABBREVIATED_SCALES: Final = {"t": 12, "b": 9, "m": 6, "k": 3}
_ABBREVIATED_SCALE_PATTERN: Final = re.compile(
    rf"(?:{'|'.join(_ABBREVIATED_SCALES)})\b",
)

#: Currency markers that put an otherwise bare number in scope. Matched against
#: `normalize_evidence_text` output, hence lowercase.
_CURRENCY_SUFFIX_PATTERN: Final = re.compile(r"ドル|円|usd|dollars?|cents?")
_CURRENCY_PREFIXES: Final = frozenset({"$", "¥"})

_TEN: Final = Decimal(10)


@dataclass(frozen=True, slots=True)
class _Magnitude:
    """One numeric literal read out of a text, with its parsed value."""

    #: The literal exactly as it was written, for a human-readable warning.
    written: str
    value: Decimal
    #: Whether the literal states its own power of ten (億/万/兆/千, billion,
    #: `$119.8B`). Only when both sides state one is the power of ten compared.
    has_scale: bool
    #: Whether a magnitude word or a currency marker attaches to the literal.
    #: Only such figures are checked; see the module docstring.
    carries_unit: bool


def unsupported_magnitudes(text: str, evidence_quote: str) -> tuple[str, ...]:
    """Return the figures in `text` that no figure in `evidence_quote` explains.

    Args:
        text: A `SourcedFact.text`, as it would be rendered to the operator.
        evidence_quote: The verbatim excerpt that fact was written from.

    Returns:
        The unexplained money/magnitude literals, as written in `text`, in the
        order they appear. Empty when every such literal is reachable from a
        quoted figure, judged at the coarser side's precision -- by a power of
        ten where only one side names its magnitude, and exactly where both do
        -- and also empty when `text` states no such figure at all, which is
        the ordinary case for a purely qualitative fact.
    """
    quoted = tuple(_magnitudes(normalize_evidence_text(evidence_quote)))
    return tuple(
        magnitude.written
        for magnitude in _magnitudes(normalize_evidence_text(text))
        if magnitude.carries_unit
        and not any(_agrees(magnitude, other) for other in quoted)
    )


def _magnitudes(text: str) -> Iterator[_Magnitude]:
    """Yield every numeric literal in `text`, left to right."""
    position = 0
    while (match := _NUMBER_PATTERN.search(text, position)) is not None:
        magnitude, position = _read_magnitude(text, match)
        yield magnitude


def _read_magnitude(text: str, match: re.Match[str]) -> tuple[_Magnitude, int]:
    """Read the full figure starting at `match`, and where it ends.

    An English magnitude word closes the figure; Japanese ones compose, so the
    scan continues while the next characters keep forming a smaller term.
    """
    start = match.start()
    value = _decimal(match.group())
    cursor = match.end()
    has_currency_prefix = start > 0 and text[start - 1] in _CURRENCY_PREFIXES
    english = _read_english_scale(text, cursor, is_money=has_currency_prefix)
    if english is not None:
        scale, cursor = english
        value *= _TEN**scale
        is_scaled = True
    else:
        value, cursor, is_scaled = _read_japanese_terms(text, value, cursor)
    magnitude = _Magnitude(
        written=text[start:cursor],
        value=value,
        has_scale=is_scaled,
        carries_unit=is_scaled
        or has_currency_prefix
        or _has_currency_suffix(text, cursor),
    )
    return magnitude, cursor


def _read_english_scale(
    text: str, cursor: int, *, is_money: bool
) -> tuple[int, int] | None:
    """Read the English magnitude that closes a figure ending at `cursor`.

    Args:
        text: The normalized text being scanned.
        cursor: The index just past the figure's digits.
        is_money: Whether a currency symbol introduced the figure, which is
            what admits the single-letter abbreviations.

    Returns:
        The power of ten and the index just past the magnitude, or `None` when
        no magnitude follows.
    """
    word = _ENGLISH_SCALE_PATTERN.match(text, _skip_spaces(text, cursor))
    if word is not None:
        return _ENGLISH_SCALES[word.group()], word.end()
    if not is_money:
        return None
    letter = _ABBREVIATED_SCALE_PATTERN.match(text, cursor)
    if letter is None:
        return None
    return _ABBREVIATED_SCALES[letter.group()], letter.end()


def _read_japanese_terms(
    text: str, first: Decimal, cursor: int
) -> tuple[Decimal, int, bool]:
    """Accumulate a 兆/億/万/千 composite that starts with `first`.

    Only a term that carries a magnitude character of its own, smaller than the
    one before it, joins the composite. That is what a written composite always
    looks like (34億9,530万), and it keeps a following unrelated figure -- a
    year, a share count -- from being absorbed as a remainder.

    Returns:
        The composite value, the index just past it, and whether any magnitude
        character applied.
    """
    scale = _scale_at(text, cursor)
    if scale is None:
        return first, cursor, False
    total = first * _TEN**scale
    cursor = _skip_spaces(text, cursor) + 1
    while (term := _next_term(text, cursor, scale)) is not None:
        value, scale, cursor = term
        total += value * _TEN**scale
    return total, cursor, True


def _next_term(
    text: str, cursor: int, previous_scale: int
) -> tuple[Decimal, int, int] | None:
    """Read the next `<number><magnitude>` term of a composite, if there is one.

    Returns:
        The term's value, its power of ten, and the index just past it, or
        `None` when the text does not continue with a strictly smaller term.
    """
    number = _NUMBER_PATTERN.match(text, _skip_spaces(text, cursor))
    if number is None:
        return None
    scale = _scale_at(text, number.end())
    if scale is None or scale >= previous_scale:
        return None
    return _decimal(number.group()), scale, _skip_spaces(text, number.end()) + 1


def _scale_at(text: str, cursor: int) -> int | None:
    """Return the power of ten of the magnitude character at `cursor`, if any."""
    position = _skip_spaces(text, cursor)
    if position >= len(text):
        return None
    return _JAPANESE_SCALES.get(text[position])


def _has_currency_suffix(text: str, end: int) -> bool:
    """Report whether a currency word follows the figure ending at `end`."""
    return _CURRENCY_SUFFIX_PATTERN.match(text, _skip_spaces(text, end)) is not None


def _skip_spaces(text: str, position: int) -> int:
    """Return the next index at or after `position` that is not a space."""
    while position < len(text) and text[position] == " ":
        position += 1
    return position


def _decimal(literal: str) -> Decimal:
    return Decimal(literal.replace(",", ""))


def _agrees(stated: _Magnitude, quoted: _Magnitude) -> bool:
    """Report whether `quoted` can account for `stated`.

    The digits are always compared at the coarser side's precision. The power
    of ten joins the comparison only when both figures name their own
    magnitude; otherwise the quote does not say which unit it is in, and
    demanding a match would flag every honest conversion (Issue #158).
    """
    stated_significand, stated_digits, stated_exponent = _decompose(stated.value)
    quoted_significand, quoted_digits, quoted_exponent = _decompose(quoted.value)
    shared = min(stated_digits, quoted_digits)
    stated_rounded, stated_exponent = _rounded(
        stated_significand, stated_exponent, shared
    )
    quoted_rounded, quoted_exponent = _rounded(
        quoted_significand, quoted_exponent, shared
    )
    if stated_rounded != quoted_rounded:
        return False
    return not (stated.has_scale and quoted.has_scale) or (
        stated_exponent == quoted_exponent
    )


def _decompose(value: Decimal) -> tuple[Decimal, int, int]:
    """Split `value` into a `[1, 10)` significand, its digit count, and its power.

    `normalize` drops trailing zeros, which is what makes `3.50` count as two
    digits: written zeros cannot be distinguished from measured ones, so the
    lenient reading is taken deliberately.
    """
    normalized = value.copy_abs().normalize()
    digits = normalized.as_tuple().digits
    return (
        Decimal((0, digits, 1 - len(digits))),
        len(digits),
        normalized.adjusted(),
    )


def _rounded(significand: Decimal, exponent: int, digits: int) -> tuple[Decimal, int]:
    """Round a `[1, 10)` significand to `digits`, carrying into `exponent`."""
    rounded = significand.quantize(
        Decimal(1).scaleb(1 - digits), rounding=ROUND_HALF_UP
    )
    # Rounding up out of the decade (9.96 -> 10) has to fold back, or 9.96
    # would disagree with the 10 it rounds to while agreeing with 9.9 -- and
    # $996 million would stop explaining 約10億ドル.
    if rounded == _TEN:
        return Decimal(1), exponent + 1
    return rounded, exponent
