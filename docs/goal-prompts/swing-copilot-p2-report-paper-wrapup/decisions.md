# Pre-answered decisions (P2-4 → FINAL phase)

These supplement — do not replace — `swing-copilot-p1p2-implementation/decisions.md`'s D1–D10,
which still govern (never touch `.env`, never stage `.agents/`/`.codex/`, no TA-Lib/SQLite, etc.).

## Pre-answered questions

Q: The "ファンダメンタル" report block in the UI mockup shows EPS YoY and a 4-quarter profitability
streak. `MarketStore.read_fundamentals(as_of)` now exists for internal screening, but should the
report expose and analyze that full history, or scope down?
A: Scope down. Add only `MarketStore.get_latest_fundamentals()` (single latest record) and drop the
two derived rows from the template. — user answer, 2026-07-21. See `design.md` §2.1 for the exact
fields to render instead. Do not expose the multi-quarter screening query to the report in this
phase.

Q: `docs/04_detailed_design.md` §3.20 shows `PaperJournal.record_decision(signal_id: int, ...)` and
`close_position(position_id: int, ...)`, but no `signal_id` column exists anywhere in the schema,
and `positions.position_id` is `UUID` (see `storage/schema.py`, `models.Position`). Follow the doc
literally, or the schema?
A: Follow the schema. Use `(run_id, symbol, strategy_key)` as `record_decision`'s natural key (matches
`trades_journal`'s `UNIQUE` constraint) and `position_id: UUID` throughout. — lead judgment,
2026-07-21, because the doc's pseudocode predates the actual schema and the schema is what the rest
of the codebase already depends on.

Q: P2-5's checklist acceptance requires "P&L and SPY comparison" but §3.20 shows no such method.
What shape?
A: `PaperJournal.summarize_performance(market_store, as_of) -> PerformanceSummary` — see `design.md`
§5 for the exact dataclass and computation. — lead judgment, 2026-07-21.

## Fallback rules

- If `templates/report.html.j2` or `reports/assets/style.css` don't yet exist when you start P2-4,
  create them (don't wait for `scripts/fetch_assets.py` — that script only fetches the vendored
  chart JS, never the template/CSS you author yourself).
- If a `tests/report`/`tests/paper` test would need real network access (Discord webhook, chart JS
  download) to pass, that's a design mistake — stop and fix the test to inject a fake, never relax
  the "offline" constraint.
- If `just verify`'s coverage gate (95%) is hard to hit for a specific fail-soft branch in
  `pipeline/daily.py`, add the missing behavioral test case rather than lowering the threshold.
- If blocked on a decision not covered here or in the original bundle's `decisions.md`, apply the
  narrowest change consistent with `docs/04_detailed_design.md`/`docs/05_ui_design.md`, note the
  divergence in your final report, and continue — do not stop for a decision you can reasonably infer.
