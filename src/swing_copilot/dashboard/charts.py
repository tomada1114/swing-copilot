"""Server-rendered inline SVG charts.

No JavaScript, no chart library, no CDN, no external font: the dashboard has
to work with the machine offline, and a chart that needs the network is a
chart that is blank exactly when the operator wants it. Every colour is a CSS
custom property defined in `static/app.css`, so light/dark and the app-wide
tone semantics (`good` / `quiet` / `serious` / `critical`) are declared once
and the charts inherit them.

The hover layer is the SVG `<title>` element — the browser's own tooltip,
which costs no script.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from swing_copilot.dashboard import formatting as fmt

if TYPE_CHECKING:
    from swing_copilot.dashboard.models import (
        ClassificationBar,
        ClassificationPanel,
        RegimePoint,
    )

_BAR_SLOT = 46
_BAR_WIDTH = 26
_SEGMENT_GAP = 2
_CORNER = 4
_AXIS_LEFT = 36
_PAD_TOP = 18
_PAD_BOTTOM = 30
_PLOT_HEIGHT = 150
_MARKER_RADIUS = 4
_STRIP_HEIGHT = 14
_TICK_COUNT = 2

#: Minimum plotted slots. A facet with two bars must not stretch to fill the
#: column: the SVG carries its intrinsic pixel size and the container scrolls,
#: so bars keep the same width in every facet and the labels stay legible.
_MIN_SLOTS = 6


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _tone_var(tone: str) -> str:
    return f"var(--tone-{tone})"


def _top_rounded_bar(x: float, y: float, width: float, height: float) -> str:
    """Path for a bar whose data end (top) carries a 4px radius."""
    radius = min(_CORNER, height, width / 2)
    return (
        f"M{x:.1f},{y + height:.1f}"
        f"V{y + radius:.1f}"
        f"a{radius:.1f},{radius:.1f} 0 0 1 {radius:.1f},-{radius:.1f}"
        f"H{x + width - radius:.1f}"
        f"a{radius:.1f},{radius:.1f} 0 0 1 {radius:.1f},{radius:.1f}"
        f"V{y + height:.1f}Z"
    )


def _nice_max(value: int) -> int:
    """Round an axis maximum up to a readable step."""
    if value <= _TICK_COUNT:
        return max(value, 1)
    for step in (5, 10, 20, 25, 50, 100):
        if value <= step:
            return step
    return ((value // 100) + 1) * 100


def _svg(width: int, height: int, body: str, *, label: str) -> str:
    """Wrap chart geometry in a responsive, labelled SVG root."""
    return (
        f'<svg class="chart" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{_escape(label)}" preserveAspectRatio="xMinYMin meet">'
        f"{body}</svg>"
    )


def _grid(width: int, maximum: int) -> str:
    """Horizontal hairlines plus their value labels."""
    parts: list[str] = []
    for index in range(_TICK_COUNT + 1):
        value = maximum * index / _TICK_COUNT
        y = _PAD_TOP + _PLOT_HEIGHT - _PLOT_HEIGHT * index / _TICK_COUNT
        parts.append(
            f'<line class="chart-grid" x1="{_AXIS_LEFT}" y1="{y:.1f}" '
            f'x2="{width}" y2="{y:.1f}"/>'
            f'<text class="chart-tick" x="{_AXIS_LEFT - 6}" y="{y + 3:.1f}" '
            f'text-anchor="end">{value:.0f}</text>'
        )
    return "".join(parts)


def classification_chart(panel: ClassificationPanel) -> str:
    """Stacked bars of matured HIT/MISS counts for one facet.

    One facet is one (`recommendation`, horizon) pair — never a pooled
    series, because `skip` is tracked as the counterfactual of `proceed`.

    Args:
        panel: The facet to draw.

    Returns:
        An inline `<svg>` fragment; the caller marks it safe.
    """
    width = _AXIS_LEFT + max(len(panel.bars), _MIN_SLOTS) * _BAR_SLOT + 8
    height = _PAD_TOP + _PLOT_HEIGHT + _PAD_BOTTOM
    maximum = _nice_max(max((bar.total for bar in panel.bars), default=1))
    parts = [_grid(width, maximum)]
    for index, bar in enumerate(panel.bars):
        x = _AXIS_LEFT + index * _BAR_SLOT + (_BAR_SLOT - _BAR_WIDTH) / 2
        parts.append(_stacked_bar(bar, x=x, maximum=maximum))
        parts.append(
            f'<text class="chart-label" x="{x + _BAR_WIDTH / 2:.1f}" '
            f'y="{_PAD_TOP + _PLOT_HEIGHT + 14}" text-anchor="middle">'
            f"{_escape(bar.run_date.strftime('%m-%d'))}</text>"
        )
    label = (
        f"{panel.recommendation} の {panel.horizon_days} 営業日判定、"
        f"run 日付ごとの分類件数"
    )
    return _svg(width, height, "".join(parts), label=label)


def _stacked_bar(bar: ClassificationBar, *, x: float, maximum: int) -> str:
    """One run date's stack, drawn bottom-up in severity order."""
    counts = bar.counts
    total = bar.total
    baseline = _PAD_TOP + _PLOT_HEIGHT
    parts: list[str] = []
    cursor: float = baseline
    for position, (name, count) in enumerate(counts):
        segment = _PLOT_HEIGHT * count / maximum
        gap = _SEGMENT_GAP if position < len(counts) - 1 else 0
        drawn = max(segment - gap, 1.0)
        top = cursor - segment
        tone = _tone_var(fmt.CLASSIFICATION_TONES.get(name, "quiet"))
        is_top = position == len(counts) - 1
        shape = _top_rounded_bar(x, top, _BAR_WIDTH, drawn) if is_top else None
        title = f"<title>{_escape(name)}: {count}</title>"
        if shape is None:
            parts.append(
                f'<rect x="{x:.1f}" y="{top + gap:.1f}" width="{_BAR_WIDTH}" '
                f'height="{drawn:.1f}" fill="{tone}">{title}</rect>'
            )
        else:
            parts.append(f'<path d="{shape}" fill="{tone}">{title}</path>')
        cursor = top
    parts.append(
        f'<text class="chart-value" x="{x + _BAR_WIDTH / 2:.1f}" '
        f'y="{cursor - 5:.1f}" text-anchor="middle">{total}</text>'
    )
    return "".join(parts)


def regime_chart(points: tuple[RegimePoint, ...]) -> str:
    """VIX close as a line, with the drawdown-pressure level as a strip.

    One measure on the y axis. The drawdown level is a categorical state, so
    it gets its own strip below the plot in the same tone vocabulary the
    badges use, rather than a second y scale.

    Args:
        points: The regime timeline, oldest first.

    Returns:
        An inline `<svg>` fragment, or an empty string when there is nothing
        to plot.
    """
    values = [point.vix_close for point in points if point.vix_close is not None]
    if not values:
        return ""
    slot = _BAR_SLOT
    width = _AXIS_LEFT + max(len(points), _MIN_SLOTS) * slot + 8
    height = _PAD_TOP + _PLOT_HEIGHT + _PAD_BOTTOM + _STRIP_HEIGHT + 12
    low, high = min(values), max(values)
    span = high - low or 1.0
    low, high = low - span * 0.15, high + span * 0.15
    parts = [_vix_axis(width, low, high)]
    coordinates: list[tuple[float, float, RegimePoint]] = []
    for index, point in enumerate(points):
        x = _AXIS_LEFT + index * slot + slot / 2
        if point.vix_close is None:
            continue
        ratio = (point.vix_close - low) / (high - low)
        coordinates.append((x, _PAD_TOP + _PLOT_HEIGHT * (1 - ratio), point))
    if len(coordinates) > 1:
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in coordinates)
        parts.append(f'<polyline class="chart-line" points="{path}"/>')
    parts.extend(
        f'<circle class="chart-dot" cx="{x:.1f}" cy="{y:.1f}" r="{_MARKER_RADIUS}">'
        f"<title>{_escape(point.run_date)} VIX {point.vix_close:.2f}</title></circle>"
        for x, y, point in coordinates
    )
    parts.append(_dd_strip(points, slot))
    return _svg(width, height, "".join(parts), label="VIX 終値とドローダウン圧力の推移")


def _vix_axis(width: int, low: float, high: float) -> str:
    parts: list[str] = []
    for index in range(_TICK_COUNT + 1):
        value = low + (high - low) * index / _TICK_COUNT
        y = _PAD_TOP + _PLOT_HEIGHT - _PLOT_HEIGHT * index / _TICK_COUNT
        parts.append(
            f'<line class="chart-grid" x1="{_AXIS_LEFT}" y1="{y:.1f}" '
            f'x2="{width}" y2="{y:.1f}"/>'
            f'<text class="chart-tick" x="{_AXIS_LEFT - 6}" y="{y + 3:.1f}" '
            f'text-anchor="end">{value:.1f}</text>'
        )
    return "".join(parts)


def _dd_strip(points: tuple[RegimePoint, ...], slot: int) -> str:
    """One cell per run: drawdown level, in the badge tone vocabulary."""
    top = _PAD_TOP + _PLOT_HEIGHT + _PAD_BOTTOM - 10
    parts: list[str] = [
        f'<text class="chart-label" x="{_AXIS_LEFT - 6}" y="{top + 10}" '
        f'text-anchor="end">DD</text>'
    ]
    for index, point in enumerate(points):
        x = _AXIS_LEFT + index * slot + (slot - _BAR_WIDTH) / 2
        tone = _tone_var(fmt.tone_of(fmt.DD_LEVEL_TONES, point.dd_level))
        gate = point.gate_verdict or "gate 未記録"
        level = point.dd_level or "DD 未記録"
        parts.append(
            f'<rect x="{x:.1f}" y="{top}" width="{_BAR_WIDTH}" '
            f'height="{_STRIP_HEIGHT}" rx="2" fill="{tone}">'
            f"<title>{_escape(point.run_date)} {_escape(level)} / "
            f"{_escape(gate)}</title></rect>"
            f'<text class="chart-label" x="{x + _BAR_WIDTH / 2:.1f}" '
            f'y="{top + _STRIP_HEIGHT + 12:.1f}" text-anchor="middle">'
            f"{_escape(point.run_date.strftime('%m-%d'))}</text>"
        )
    return "".join(parts)
