"""DataFrame → view-model conversion, one module per page.

This layer owns every interpretation of the accumulated history: collapsing
`v_verdict_scorecard`'s (verdict x horizon) grain, resolving each column's
NULL into the specific token that says why it is absent, and stratifying the
tracking ledger by `recommendation`. The routes below it only pick a run and
hand frames over; the templates above it only render what arrives.
"""

from __future__ import annotations

from swing_copilot.dashboard.viewmodels.history import HistorySources, build_history
from swing_copilot.dashboard.viewmodels.run import RunSources, build_run_overview
from swing_copilot.dashboard.viewmodels.symbol import (
    SymbolSources,
    build_symbol_detail,
)

__all__ = [
    "HistorySources",
    "RunSources",
    "SymbolSources",
    "build_history",
    "build_run_overview",
    "build_symbol_detail",
]
