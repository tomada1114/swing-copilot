# Pre-decided implementation choices

No human is reachable during the goal run. Apply these choices; do not reopen them.

## D1. Repository and naming

- Work in the existing repository. Never copy `uv-template` over it and never run `git init`.
- Project/package/CLI names are `swing-copilot`, `swing_copilot`, and `copilot-daily`.
- Run the existing `scripts/bootstrap.py` in place for the rename, then update application metadata. Keep build and wheel smoke verification; remove only publishing configuration that is genuinely inapplicable.

## D2. Branch and pre-existing worktree

- If on `main`, create `feat/p1-p2-implementation` before any implementation commit.
- Expected untracked user paths are `.agents/` and `.codex/`; never touch or stage them.
- Reviewed `docs/00_human_preparation.md` through `docs/05_ui_design.md`, `docs/goal-prompts/`, and `mkdocs.yml` may already be modified. If dirty, commit only those reviewed documentation/configuration files first as `docs: refine P1 and P2 architecture`. If already clean/committed, do not create an empty commit.
- Commit at least once per checklist item. Conventional Commit type/scope tokens stay English; summaries may be Japanese or English. Test, implementation, and required canonical design correction belong in the same logical commit. Never amend around a failed hook; re-stage hook fixes and commit again.

## D3. Architecture

- Modular monolith and one CLI process. Functional core/imperative shell.
- Protocols only for volatile external boundaries and clock. Explicit composition root; no dynamic plugin discovery.
- pandas is the only DataFrame library. Internal values are frozen/slotted dataclasses; Pydantic is for configuration and external/LLM boundaries.
- Price history is adjusted OHLCV in Parquet. All structured state is in `data/copilot.duckdb`; no SQLite.

## D4. Time and idempotency

- Every calculation receives `as_of`. The default daily `run_date` is the latest completed market date found in fetched bars; `--as-of` overrides it for tests/backfill.
- Apply inclusive before/equal/after cutoffs to prices, filings/fundamentals, and universe snapshots. Wall time comes only from injected `Clock` and never replaces `as_of`.
- Every invocation gets a new UUID `run_id`. Natural-key upserts prevent duplicated business data. A prior successful run never causes an unconditional whole-step skip.
- Multi-row writes are one transaction; correction upserts update existing rows; snapshot reruns remove absent rows; file replacement preserves the old destination on failure.
- Cache successful LLM results only by `(model, prompt_hash, schema_version)`, where the hash covers system+user prompt. Revalidate cache provenance and CON-03.

## D5. Strategy and indicators

- Implement SMA with pandas rolling; RSI14 and ATR14 with Wilder smoothing. Do not add TA-Lib.
- Filters and required signals use AND semantics. Aggregate hits into one Candidate per symbol.
- Rank by RSI14 ascending, 20-day average volume descending, symbol ascending; keep at most 10. Do not invent a score.

## D6. Backtest

- Use the in-house deterministic multi-symbol engine specified in `docs/04_detailed_design.md` 3.19. Do not add backtesting.py.
- Reuse production indicators and ScreeningPipeline. Enforce filed-at/as-of visibility and next-session fills.
- Apply adverse slippage and commission on entry and every exit including final liquidation; verify final cash by hand calculation and retain SPY residual cash.

## D7. Secrets and external failure

- Configuration loads without secrets. Validate only secrets needed by enabled features.
- The ordinary pytest suite and dry-run/E2E must use injected fakes. An autouse socket guard makes accidental network access fail immediately.
- If `.env` is absent or live services fail, complete all offline work and report `live canary: blocked — <reason>`; do not alter tests or emit a different success sentinel.

## D8. External APIs

- Consult only official documentation/repositories. Pin behavior to the installed version and record deviations.
- edgartools identity uses `EDGAR_IDENTITY`/its official setter. SEC requests remain at or below 10 requests/second.
- EDGAR transient operations use three total attempts with deterministic 1s/2s backoff and throttling on every attempt; validation/programming errors are not retried.
- Claude structured output uses the current Python SDK helper supported by the selected model. Total attempts are capped at 3 and total backoff at 60 seconds; do not double-retry SDK retries.
- Lightweight Charts v5 uses `chart.addSeries(LightweightCharts.<SeriesType>, options)`.

## D9. Security and reporting

- Never log secrets. Store prompt/response audit data only after secret redaction; `data/` stays gitignored.
- Treat news, filings, company names, and LLM output as untrusted. Jinja autoescape stays enabled; JSON script payloads use safe JSON serialization; never mark external content safe.
- Keep LLM system and user/untrusted content in separate API fields. Escape external content inside explicit untrusted delimiters. Require non-empty, non-blank, known source IDs and centrally inspect every user-visible output field for CON-03 before caching.
- Generate dated report and `latest.html` via temporary file plus atomic rename.

## D10. Quality

- Keep 95% line+branch coverage, but prefer behavior/error/boundary tests over trivial coverage tests.
- `just verify` is the non-mutating completion gate: lint/type/format check, tests, strict docs build, and wheel smoke.
- Unknown model pricing is a configuration error, never zero cost. Budget exhaustion yields a degraded report without an API call.
- Do not use skip/xfail or `# pragma: no cover` except abstract Protocol/ABC bodies and `if __name__ == "__main__"`.
