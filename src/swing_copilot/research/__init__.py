"""Read-only research surface over the accumulated decision history.

The notebook-facing counterpart to the `storage` repositories: every accessor
returns a pandas DataFrame from a short-lived read-only DuckDB connection, so
exploring "verdict outcomes x score breakdown x regime" is one line of Python
and can never take the daily run's writer lock.

Example::

    from swing_copilot import research

    df = research.scorecard()
    df.groupby(["recommendation", "gate_verdict"])["forward_return_pct"].mean()
"""

from __future__ import annotations

from swing_copilot.research.frames import (
    ResearchError,
    bars,
    candidates,
    ensure_views,
    query,
    regime_snapshots,
    runs,
    scorecard,
    screening_rejections,
    tracked_positions,
    verdict_outcomes,
    verdicts,
)

__all__ = [
    "ResearchError",
    "bars",
    "candidates",
    "ensure_views",
    "query",
    "regime_snapshots",
    "runs",
    "scorecard",
    "screening_rejections",
    "tracked_positions",
    "verdict_outcomes",
    "verdicts",
]
