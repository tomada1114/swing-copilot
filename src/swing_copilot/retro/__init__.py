"""Retrospective mechanism: verdict persistence and outcome evaluation (P8).

Deliberately a separate package from `analysis/`, whose charter is to touch
neither the network nor the database. `retro/` reads and writes DuckDB, so it
cannot live there without breaking that invariant -- and keeping them apart is
what preserves `copilot-ingest-analysis`' own "never touches the database"
guarantee (decision D8).

This package is observation-only: nothing here rewrites configuration, code,
or any deterministic screening/sizing/ranking value.
"""

from __future__ import annotations
