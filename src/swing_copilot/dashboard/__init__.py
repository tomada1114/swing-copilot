"""Local read-only viewer over the accumulated decision history.

`copilot-dashboard` serves three pages — one run's overview, one symbol's
reasoning, and the history of both — from the same DuckDB file the daily
pipeline writes. It is a viewer and nothing else: no route mutates state, no
connection is held across a request, and `research.ensure_views()` is never
called from inside the process, because DuckDB's file lock is exclusive and
the unattended 18:30 run must never lose a day to an open browser tab.

Layers, top to bottom: `app` (routes) → `viewmodels` (semantics) →
`queries` (thin `research` wrappers) → `swing_copilot.research`.
"""

from __future__ import annotations

from swing_copilot.dashboard.app import create_app

__all__ = ["create_app"]
