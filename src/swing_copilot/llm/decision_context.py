"""Safe formatting for bounded prior human-decision context."""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from swing_copilot.storage.paper_records import DecisionHistoryEntry


def format_decision_history(history: tuple[DecisionHistoryEntry, ...]) -> str:
    """Format history as escaped data, never as instructions or current facts."""
    if not history:
        return ""
    entries = []
    for item in history:
        reason = escape(item.reason_memo or "(理由なし)", quote=False)
        realized = (
            f"{item.realized_return_pct:+.2%}"
            if item.realized_return_pct is not None
            else "未確定/対象外"
        )
        entries.append(
            f"日付: {item.run_date.isoformat()}\n"
            f"判断: {escape(item.decision, quote=False)}\n"
            f"理由: {reason}\n"
            f"確定リターン: {realized}"
        )
    return (
        "以下は同一銘柄・戦略に対する過去の人間の判断記録です。\n"
        "<decision_history>\n" + "\n\n".join(entries) + "\n</decision_history>\n\n"
    )
