"""The one shared home for display-layer number formatting (Issue #399).

Before this module existed, the same four-formatter shape (`_number`,
`_money`, `_one_r`, `_percent`) was hand-copied into `markdown_report.py`,
`terminal_report.py`, and `verdict_notification.py`, and a differently-unit
`_fmt_pct`/`_fmt_ratio`/... family was hand-copied into five CLIs
(`backtest/cli.py`, `tracking/cli.py`, `regime/dd_forward_cli.py`,
`report/history_cli.py`, `screening/filter_matrix_cli.py`). Two copies of
`_number` had already silently diverged (`verdict_notification.py` lost its
thousands separator in #390), and the identically-named `_fmt_pct` meant a
*fraction* in `backtest/cli.py` but an *already-multiplied percent* in
`tracking/cli.py` -- copying a value between the two silently produced a
100x-off display with no test catching it.

Every function here names its input unit in its own name, so there is
nothing left to guess at a call site: `format_fraction_pct` always takes a
0..1 fraction, never an already-multiplied percent number. This module
deliberately does not offer an "accepts an already-multiplied percent"
formatter -- the one caller that used to need one
(`tracking/cli.py`'s `win_rate` display) now passes the fraction through
unchanged instead.

`tests/test_quality_contracts.py::test_no_local_reimplementation_of_shared_formatters`
enforces that none of these are re-defined locally under `src/`.
"""

from __future__ import annotations


def format_number(value: float | None, *, digits: int = 2) -> str:
    """A plain, thousands-separated number, e.g. `1,234.56`."""
    return "N/A" if value is None else f"{value:,.{digits}f}"


def format_money(value: float | None) -> str:
    """A USD amount, e.g. `$1,234.56`."""
    return "N/A" if value is None else f"${value:,.2f}"


def format_one_r(value: float | None) -> str:
    """A 0..1 fraction as an unsigned percent, e.g. `12.34%` (1R risk display)."""
    return format_fraction_pct(value, digits=2, signed=False)


def format_fraction_pct(
    value: float | None, *, digits: int = 2, signed: bool = False
) -> str:
    """A 0..1 fraction as a percent, e.g. `0.1234` -> `12.34%`.

    Args:
        value: The fraction to format; `None` renders as `N/A`.
        digits: Decimal places after the point.
        signed: Whether a positive value carries an explicit `+`.

    Returns:
        The formatted percent string, or `N/A` for `None`.
    """
    if value is None:
        return "N/A"
    sign = "+" if signed else ""
    return f"{value:{sign}.{digits}%}"


def format_ratio(value: float | None, *, digits: int = 3) -> str:
    """A plain, unsigned ratio/score with no separator, e.g. `1234.568`."""
    return "N/A" if value is None else f"{value:.{digits}f}"
