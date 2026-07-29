GOAL: Implement roadmap item P8-30 of swing-copilot's retrospective mechanism exactly as designed: three new DuckDB tables (`verdicts`, `verdict_sources`, `verdict_outcomes`), a new `copilot-retro` CLI with `collect` and `evaluate` subcommands in a new `src/swing_copilot/retro/` package, and extraction of shared forward-return primitives into `src/swing_copilot/pipeline/forward_returns.py` — on a new branch, ending with an open PR.

CONTEXT: Resolve every path from the repository root. Read in this order before changing anything:
1. `docs/goal-prompts/swing-copilot-retrospective/design.md` — §4, §5.1, §5.2, §10 are the binding contract for this phase
2. same dir `decisions.md` (D1–D10)
3. same dir `execution-decisions.md` (ED1–ED7 and E30.1–E30.4)
4. same dir `research.md` (verified code anchors: R0, R1, R2, R5, R6)
5. `docs/06_reliability_roadmap.md` — the P8-30 seed

BASELINE: main CI was green on 2026-07-29, but derive branch, worktree, and test state from fresh commands; never copy counts from prose. Preflight: all five documents above must exist on main — if any is missing, print `GOAL_STOPPED: P8-30 support docs missing` and stop. Preserve unrelated worktree changes; never read or print `.env`.

DO: Create branch `feat/p8-30-retro-storage` from main. Then, per ED3 (test-first with pasted failing output, one logical unit per commit):
(a) Extract forward-return primitives into `pipeline/forward_returns.py` per E30.3 — behavior-preserving for postmortem — and add the new forward `find_maturity_trading_day` with calendar round-trip consistency tests.
(b) Add the three DDLs from design §4 to `storage/schema.py` and storage write functions with full-replacement single-transaction semantics (imitate `replace_signal_outcomes`, research R2), including failure-injection rollback tests (failure after ≥1 successful statement).
(c) Implement `retro/collect.py` per design §4 write contract and E30.2/E30.4 (idempotent re-ingest picks up corrections; zero-scan is success).
(d) Implement `retro/evaluate.py` per design §5.2: maturity-day `as_of`, prices `date <= as_of` only, fail-soft on missing bars, asymmetric classification table of §3.3 with boundary tests immediately before/at/after each threshold and maturity cutoff.
(e) Add `retro/cli.py` with only `collect`/`evaluate` (E30.1) and the `copilot-retro` entry in `pyproject.toml` (research R5); update `docs/reference.md` per ED5 if it lists CLI entry points.
Finish per ED1: push the branch and open the PR.

DONE WHEN: `just verify` exits 0 on the committed branch; every 動作確認 bullet of the roadmap P8-30 seed is demonstrated by fresh pytest output; `git status --short` shows no uncommitted implementation changes; the PR is open. Then print exactly `GOAL_DONE: P8-30 just verify exit 0, PR <url>`. If push/PR still fails after ED1's retries, print `GOAL_DONE: P8-30 just verify exit 0, PR skipped (<reason>)`.

VERIFY: narrowest pytest target per unit first (`tests/pipeline/test_postmortem.py`, new `tests/retro/`, `tests/storage/`), then `just verify` and `git status --short` — fresh output and exit codes pasted, no prose-only claims.

CONSTRAINTS:
- Integrity: never skip/xfail/disable/delete tests, weaken assertions, lower the 95% coverage gate, or stub the unit under test. The default suite stays fully offline (autouse socket guard, research R6); inject fakes at boundaries.
- Scope: only P8-30. No `export`/`ingest`/`prepare` subcommands, no `RetroConfig`, no skill, no daily-flow or existing-table changes, no new dependencies (ED6). `signal_outcomes` behavior unchanged — `tests/pipeline/test_postmortem.py` stays green (import-path edits only).
- ED1–ED7 apply in full; divergence rule is ED4. Conventional Commits; never `--no-verify`, force-push, or hand-edits to `uv.lock`.

STOP RULES: Stop after 60 turns. If one unit stays red after 4 materially different attempts, preserve green commits and print `GOAL_STOPPED: P8-30 <unit> blocked — <reason>`.
