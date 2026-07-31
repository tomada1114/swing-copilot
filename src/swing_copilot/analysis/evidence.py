"""Verbatim-quote verification for skill-produced facts (FR-08).

Provenance by ID proves a cited source was *supplied* for that symbol. It
cannot prove the sentence was *written from* that source. A 2026-07-30 run
showed the gap concretely: an expert subagent read another symbol's filing,
wrote the finding into its own symbol's fragment, and cited its own -- entirely
correct -- `source_id`. Every ID check passed.

`SourcedFact.evidence_quote` closes it. A fact carries the short verbatim
excerpt it was written from, and `validate.py` proves that excerpt occurs in
the exported body of one of the IDs the fact cites. A statement written from a
slice the expert was never given has no such excerpt to offer.

Matching is deliberately lenient about presentation and strict about wording.
A filing body reaches the export through HTML extraction, so demanding byte
equality would withhold honest analyses over a non-breaking space or a curly
apostrophe. Normalization therefore applies NFKC, folds typographic quotes and
dashes to ASCII, collapses whitespace runs to one space, and case-folds --
none of which can make text from a different filing match.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Shortest normalized quote accepted. A quote below this is common enough
#: wording ("the company") to occur in any body, which would let the check pass
#: without evidencing anything.
MIN_EVIDENCE_QUOTE_CHARS: Final = 12

#: Longest normalized quote accepted. The contract asks for a short excerpt,
#: not a re-export of the filing into the result document.
MAX_EVIDENCE_QUOTE_CHARS: Final = 300

#: Single quotation marks: U+2018/2019 curly pair, U+201A low-9, U+201B
#: high-reversed-9.
_SINGLE_QUOTE_CODEPOINTS: Final = (0x2018, 0x2019, 0x201A, 0x201B)
#: Double quotation marks: U+201C/201D curly pair, U+201E low-9, U+201F
#: high-reversed-9.
_DOUBLE_QUOTE_CODEPOINTS: Final = (0x201C, 0x201D, 0x201E, 0x201F)
#: Dashes: U+2010..U+2015 hyphen through horizontal bar, plus U+2212 minus.
_DASH_CODEPOINTS: Final = (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)

#: Keyed by codepoint rather than by literal character: the literals are
#: exactly the ambiguous-glyph set that lint forbids in source.
_TYPOGRAPHIC_FOLDING: Final = str.maketrans(
    dict.fromkeys(_SINGLE_QUOTE_CODEPOINTS, "'")
    | dict.fromkeys(_DOUBLE_QUOTE_CODEPOINTS, '"')
    | dict.fromkeys(_DASH_CODEPOINTS, "-")
)


def normalize_evidence_text(text: str) -> str:
    """Return `text` in the form both sides of a quote comparison are held to.

    Args:
        text: Either a skill-supplied quote or an exported source body.

    Returns:
        The NFKC-normalized, typographically folded, whitespace-collapsed,
        case-folded text. Both operands of a containment check must pass
        through this function for the result to mean anything.
    """
    folded = unicodedata.normalize("NFKC", text).translate(_TYPOGRAPHIC_FOLDING)
    return " ".join(folded.split()).casefold()


def normalized_source_bodies(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Index `(source_id, raw body)` pairs by ID with bodies normalized.

    Args:
        items: One pair per citable source, whose body is every part of the
            exported item a fact could legitimately quote.

    Returns:
        `source_id` -> normalized body, ready for a containment check against
        a quote passed through `normalize_evidence_text`.
    """
    return {source_id: normalize_evidence_text(body) for source_id, body in items}
