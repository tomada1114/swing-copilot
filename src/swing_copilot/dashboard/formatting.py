"""Presentation vocabulary for the dashboard, including the NULL tokens.

The accumulated history uses NULL for at least seven different reasons, and
conflating any two of them misreads the data: a verdict whose horizon has not
matured yet is not a zero forward return, `news_supply_level IS NULL` is not
`none`, and `execution_state IS NULL` is not `UNKNOWN`. So no formatter here
ever renders a missing value as `0`, `-`, or an empty cell by default. Each
absence is rendered as a named token whose meaning the page's own footer
legend spells out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class NullToken:
    """One reason a value is absent, and the label that stands for it."""

    key: str
    label: str
    explanation: str


#: Every absence the dashboard can render. The `key` is what a view passes
#: around; the `label` is what a cell shows; the `explanation` is what the
#: footer legend and the cell's `title` attribute say.
NULL_TOKENS: Mapping[str, NullToken] = {
    "immature": NullToken(
        "immature",
        "未成熟",
        "判定期日（5/20営業日）に未到達。フォワードリターン0でも中立判定でもない",
    ),
    "not_ingested": NullToken(
        "not_ingested",
        "verdict未取込",
        "定性判断は reports/ に存在しうるが DuckDB へ未アーカイブ。"
        "verdicts は次の run の retro collect で取り込まれる",
    ),
    "pre_measurement": NullToken(
        "pre_measurement",
        "計測導入前",
        "この計測が導入される前の run。値が none だったのではなく測っていない",
    ),
    "unrecorded": NullToken(
        "unrecorded",
        "未記録",
        "列の導入前に書かれた行で復元不能。UNKNOWN と読み替えてはならない",
    ),
    "untracked": NullToken(
        "untracked",
        "追跡未開始",
        "仮想ポジションがまだ建っていない",
    ),
    "absent": NullToken(
        "absent",
        "該当なし",
        "この run の candidates に無い、またはスコア成分の導入前",
    ),
    "no_snapshot": NullToken(
        "no_snapshot",
        "snapshotなし",
        "この run にレジーム snapshot が記録されていない",
    ),
    "pre_tagging": NullToken(
        "pre_tagging",
        "タグ導入前",
        "basis タグ（#191）の導入より前に書かれた理由",
    ),
    "none": NullToken(
        "none",
        "—",
        "値なし",
    ),
}


@dataclass(frozen=True, slots=True)
class Cell:
    """One rendered value, or the named reason it is absent.

    Attributes:
        text: What the cell shows.
        absence: The `NULL_TOKENS` key when the underlying value is missing;
            `None` for a real value. Templates style the two differently, so
            an absence can never be mistaken for a number.
        tone: Optional CSS modifier (`pos`/`neg`) for signed quantities.
        title: Hover text; the token's explanation for an absence.
    """

    text: str
    absence: str | None = None
    tone: str = ""
    title: str = ""


def missing(key: str = "none") -> Cell:
    """Return the cell that stands for one named kind of absence."""
    token = NULL_TOKENS[key]
    return Cell(text=token.label, absence=key, title=token.explanation)


#: The missing-value sentinels DuckDB's DataFrames actually produce, next to
#: `None` and NaN. Compared by identity rather than with `pd.isna`, which
#: returns an array (and therefore an ambiguous truth value) for a sequence.
_MISSING_SENTINELS: tuple[object, ...] = (pd.NA, pd.NaT)


def is_missing(value: object) -> bool:
    """Whether a DataFrame value is NULL, NaN, NaT, or pandas' NA sentinel."""
    if value is None:
        return True
    if isinstance(value, float):  # covers numpy.float64 and math.nan
        return math.isnan(value)
    return any(value is sentinel for sentinel in _MISSING_SENTINELS)


def text(value: object, *, key: str = "none") -> Cell:
    """Render any scalar as text, or the named token when it is absent."""
    if is_missing(value):
        return missing(key)
    rendered = str(value).strip()
    return missing(key) if not rendered else Cell(text=rendered)


def number(
    value: object,
    *,
    digits: int = 2,
    suffix: str = "",
    key: str = "none",
    signed: bool = False,
) -> Cell:
    """Render a float with fixed precision, or the named absence token.

    Args:
        value: The raw DataFrame value.
        digits: Decimal places.
        suffix: Unit appended verbatim (e.g. `"%"`).
        key: Which `NULL_TOKENS` entry a missing value means here.
        signed: Show an explicit `+` and tint the cell by sign. Use for
            quantities where direction is the point (returns), not for
            magnitudes (price, ATR).

    Returns:
        A `Cell`.
    """
    if is_missing(value):
        return missing(key)
    numeric = float(value)  # type: ignore[arg-type]
    if math.isnan(numeric) or math.isinf(numeric):
        return missing(key)
    body = f"{numeric:+.{digits}f}" if signed else f"{numeric:,.{digits}f}"
    tone = ""
    if signed:
        tone = "pos" if numeric > 0 else "neg" if numeric < 0 else ""
    return Cell(text=f"{body}{suffix}", tone=tone)


def integer(value: object, *, suffix: str = "", key: str = "none") -> Cell:
    """Render an integral value, or the named absence token."""
    if is_missing(value):
        return missing(key)
    return Cell(text=f"{int(value):,d}{suffix}")  # type: ignore[call-overload]


#: `recommendation` → badge modifier. `skip` is deliberately not an alarm
#: colour: it is the ordinary outcome of a cautious pipeline, not a failure.
RECOMMENDATION_TONES: Mapping[str, str] = {
    "proceed": "good",
    "skip": "quiet",
}

#: `runs.status` → badge modifier.
RUN_STATUS_TONES: Mapping[str, str] = {
    "success": "good",
    "degraded": "warning",
    "failed": "critical",
    "running": "info",
}

#: `verdict_outcomes.classification` → badge modifier. Severity is ordered,
#: and every use is paired with a written label (never colour alone).
CLASSIFICATION_TONES: Mapping[str, str] = {
    "HIT": "good",
    "NEUTRAL": "quiet",
    "MISS_MILD": "serious",
    "MISS_SEVERE": "critical",
}

#: `risk_assessments.status` → badge modifier. The closed vocabulary is
#: `approved` / `rejected` / `not_calculable` (see `storage/schema.py`).
RISK_STATUS_TONES: Mapping[str, str] = {
    "approved": "good",
    "rejected": "critical",
    "not_calculable": "quiet",
}

#: Market-regime gate verdict → badge modifier.
GATE_TONES: Mapping[str, str] = {
    "BULL": "good",
    "NEUTRAL": "quiet",
    "BEAR": "critical",
}

#: Drawdown pressure level → badge modifier.
DD_LEVEL_TONES: Mapping[str, str] = {
    "CALM": "good",
    "NORMAL": "quiet",
    "CAUTION": "warning",
    "HIGH": "critical",
}

#: Screening stage → the Japanese section label the rejection panel uses.
STAGE_LABELS: Mapping[str, str] = {
    "data_quality": "データ品質",
    "fundamental_filter": "ファンダメンタル",
    "technical_signal": "テクニカル",
}


def tone_of(table: Mapping[str, str], value: str | None) -> str:
    """Look a badge modifier up, defaulting to the neutral one."""
    return "quiet" if value is None else table.get(value, "quiet")


def legend(keys: tuple[str, ...]) -> tuple[NullToken, ...]:
    """Resolve a page's declared token keys into their definitions."""
    return tuple(NULL_TOKENS[key] for key in keys)
